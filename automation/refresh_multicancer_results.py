"""Recalculate only appended, poolable source-review contexts in the Uro overlay."""
from __future__ import annotations
import argparse, base64, contextlib, copy, hashlib, inspect, io, json, os, shutil, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from automation.import_multicancer_review import AUDIT, VERSION, REVIEW
from automation.sync_sources import assembled_source, read, write, sha


def clean(v):
    if isinstance(v,dict):return {k:clean(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)):return [clean(x) for x in v]
    if hasattr(v,'item'):return clean(v.item())
    if isinstance(v,float) and not __import__('math').isfinite(v):return None
    return v


def main(source):
    old_root=assembled_source(ROOT,source)
    sys.path[:0]=[str(ROOT/'runtime_code'/source),str(old_root)]
    import pandas as pd
    import requests
    requests.sessions.Session.request=lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('Offline rebuild'))
    import meta_analysis
    import analysis_settings_backend as settings
    from scripts import subexposure_policy as policy
    from runtime_code.trend_ci_runtime import install_core,install_completion
    install_core(meta_analysis);install_completion()
    stage=ROOT/'updates'/source/'versions'/VERSION/'files'
    release=read(AUDIT/'source_release.json')
    reports=[];memo={}
    for relative,entry in list(release[source]['overlay_files'].items()):
        path=stage/relative;data=read(path)
        if not isinstance(data.get('studies'),list):continue
        rows=data['studies'];new=[r for r in rows if r.get('evidence_revision')==VERSION]
        candidates=[r for r in policy.pooling_rows(rows) if r.get('disease_hierarchy_pooling_selected') is not False and r.get('disease_scope_pooling_selected') is not False]
        new_ids={r.get('Effect ID') for r in new}
        if not any(r.get('Effect ID') in new_ids for r in candidates):
            reports.append({'path':relative,'held_rows_only':True,'saved_result_unchanged':True});continue
        disease=data.get('disease') or data['cancer'];exposure=data['exposure']
        mode='yes' if '/yes/' in relative else 'no'
        key=hashlib.sha256(json.dumps([rows,mode,disease,exposure],sort_keys=True).encode()).hexdigest()[:16]
        if key not in memo:
            catalog=read(old_root/'static/seer-baseline.json')
            options=settings.normalize_settings(None,catalog)
            baseline=settings.resolve_baseline(catalog,disease,None,options)
            previous=(data.get('headline') or {}).get('baseline_risk')
            if previous is not None:baseline.update(p0=previous,risk_percent=previous*100,available=True,identity_approximation=False)
            context={'settings':options,'baseline':baseline,'disease':disease,'exposure':exposure,
                'repository':'metaUro','quality_filter':'Moderate+','min_cases':0,'pooled_plot_rows':[],'pooled_study_keys':set()}
            token=settings._ACTIVE.set(context);oldcwd=Path.cwd()
            try:
                with tempfile.TemporaryDirectory(prefix='multicancer-uro-plot-') as folder:
                    os.chdir(folder)
                    kwargs={'outcome':'Incidence','exclude_meta':True,'df_all':pd.DataFrame(rows),'generate_plots':True}
                    with contextlib.redirect_stdout(io.StringIO()):
                        result=meta_analysis.perform_meta_analysis(pd.DataFrame(candidates),disease,exposure,**kwargs)
                    if result.get('error'):raise ValueError((relative,result['error']))
                    for field in ('plot_url','funnel_plot_url','baujat_plot_url'):
                        url=result.get(field)
                        if not url:continue
                        if str(url).startswith('data:image/png;base64,'):
                            original=Path(folder)/'static'/(field+'.png')
                            original.parent.mkdir(parents=True,exist_ok=True)
                            original.write_bytes(base64.b64decode(str(url).split(',',1)[1]))
                        else:
                            original=Path(folder)/str(url).split('?',1)[0]
                        assert original.is_file(),original
                        tail=original.relative_to(Path(folder)/'static')
                        if source=='metaPee' and 'provisional_ui' in relative:
                            target=stage/'combined/provisional_ui/static'/mode/VERSION/key/tail
                            result[field]=f'provisional-assets/{mode}/{VERSION}/{key}/'+str(tail)
                        else:
                            target=stage/'static/subexposures'/mode/'interactive'/key/tail
                            result[field]=f'static/subexposures/{mode}/interactive/{key}/'+str(tail)
                        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original,target)
                    result.pop('studies',None);result['pooled_study_count']=len(context['pooled_plot_rows'])
                    memo[key]=clean(result)
            finally:os.chdir(oldcwd);settings._ACTIVE.reset(token)
        data.update(copy.deepcopy(memo[key]),studies=rows)
        if isinstance(data.get('forest_plot'),dict):data['forest_plot']['url']=data.get('plot_url')
        if isinstance(data.get('screening_stats'),dict):data['screening_stats']['extracted']=len(rows)
        write(path,data);reports.append({'path':relative,'calculation_id':key,'pool_count':data['pooled_study_count'],'after_sha256':sha(path)})
        print(source,relative,data['pooled_study_count'],flush=True)
    # Preserve the existing app-integrity manifest and bind only changed files.
    if source=='metaPee':
        relative='combined/provisional_ui/manifest.json';manifest=read(old_root/relative)
        for name in manifest['protected_source_hashes']:
            if (stage/name).exists():manifest['protected_source_hashes'][name]=sha(stage/name)
        manifest['evidence_revision']=VERSION;write(stage/relative,manifest)
    for p in stage.rglob('*'):
        if not p.is_file():continue
        relative=str(p.relative_to(stage));baseline=ROOT/'sources'/source/relative
        release[source]['overlay_files'][relative]={'sha256':sha(p),'original_sha256':sha(baseline) if baseline.is_file() else None,
            'stored_path':str(p.relative_to(ROOT/'updates'/source)),'source_commit':release[source]['commit'],'run_id':VERSION}
    write(AUDIT/'source_release.json',release)
    write(AUDIT/f'cache_refresh_{source}.json',{'network_calls':0,'model_calls':0,'calculations':len(memo),'files':reports})
    print(source,'refreshed',len(reports),'caches',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True,choices=['MetaVir','metaPee']);main(parser.parse_args().source)
