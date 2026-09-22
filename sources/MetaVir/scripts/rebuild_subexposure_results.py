"""Build both subexposure UI/summary projections offline from saved direct evidence."""
from pathlib import Path
import argparse,contextlib,copy,hashlib,io,json,os,shutil,sys,tempfile,warnings
from collections import defaultdict,Counter
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import dietary_only_policy as dietary_policy
import pandas as pd
import meta_analysis
from scripts import subexposure_policy as policy
from scripts import monthly_evidence_update as monthly
from scripts import build_summary_plots as summary
from scripts import curator_dedup as curator


def read(path):return json.loads(path.read_text())
def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(monthly.json_compatible(data),ensure_ascii=False,indent=2,allow_nan=False)+'\n')

def build(skip_summary=False):
    # Prefer the authoritative combined projection when it is present.  The
    # prior directory-name check made isolated release builds silently fall
    # back to the legacy cache merely because the checkout had been staged
    # under a temporary directory name.
    combined_root=ROOT/'combined'
    source_root=combined_root if (combined_root/'_merge_manifest.json').exists() else ROOT/'Cached_results'
    curator_packet=curator.load_checkpoint(source_root) if ROOT.name=='MetaVir' else None
    hierarchy=read(ROOT/'data/exposure_hierarchy.json')
    for name in hierarchy:policy.descendants(name,hierarchy)
    groups=defaultdict(dict);payloads={};source_hashes={}
    for path in sorted(source_root.glob('*/*_true_all.json')):
        raw=read(path);source_hashes[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
        exposure=raw.get('exposure') or path.parent.name
        disease=raw.get('disease') or raw.get('cancer');endpoint=raw.get('endpoint') or raw.get('outcome') or 'Incidence'
        if not disease:continue
        groups[(disease,endpoint)][exposure]=curator.filter_rows(raw.get('studies',[]),exposure,disease,curator_packet)
        payloads[(disease,endpoint,policy.key(exposure))]=(raw,path.name)
    audit={'source_cache_files':len(source_hashes),'new_gateway_calls':0,'modes':{}}
    with tempfile.TemporaryDirectory(prefix='subexposure-build-') as td:
        stage=Path(td)
        for mode in ['yes','no']:
            cache_root=stage/mode/'cache';plot_root=stage/mode/'work';plot_root.mkdir(parents=True)
            count=Counter();changed=[]
            for (disease,endpoint),sources in groups.items():
                targets=set(sources)
                for source in sources:
                    for parent in {p for ps in hierarchy.values() for p in ps}:
                        if policy.key(source) in {policy.key(c) for c in policy.descendants(parent,hierarchy)}:targets.add(parent)
                for target in sorted(targets,key=policy.key):
                    rows=[dietary_policy.apply_row(row,target) for row in policy.project_rows(sources,target,hierarchy,include=mode)]
                    rows=curator.filter_rows(rows,target,disease,curator_packet)
                    original_entry=payloads.get((disease,endpoint,policy.key(target)))
                    original,filename=original_entry or next(payloads[(disease,endpoint,policy.key(source))] for source in sources)
                    output=copy.deepcopy(original)
                    for row in rows:
                        row['subexposure_original_pool_in_parent_meta_analysis']=row.get('pool_in_parent_meta_analysis')
                        if row['subexposure_inherited']:
                            row['Hierarchy Inherited']=True
                            # Only this newly established exposure relationship is lifted.
                            if mode=='yes' and row['subexposure_pooling_selected']:
                                row['pool_in_parent_meta_analysis']=True
                    pool=policy.pooling_rows(rows)
                    # Saved human/clinical restrictions remain effective after inheritance.
                    frame=pd.DataFrame(pool)
                    if not frame.empty:
                        frame=meta_analysis.filter_curated_meta_analysis_exclusions(frame,disease,target,endpoint)
                        if hasattr(meta_analysis,'human_pooling_rows'):frame=meta_analysis.human_pooling_rows(frame)
                    all_frame=pd.DataFrame(rows)
                    result={}
                    if not frame.empty:
                        kwargs=dict(outcome=endpoint,exclude_meta=True,df_all=all_frame,generate_plots=False)
                        with contextlib.redirect_stdout(io.StringIO()),warnings.catch_warnings():
                            warnings.simplefilter('ignore');result=meta_analysis.perform_meta_analysis(frame,disease,target,**kwargs)
                        if result.get('headline'):
                            result['headline']['n_studies']=len(frame)
                            result['headline']['total_n']=int(pd.to_numeric(frame['Sample Size'],errors='coerce').fillna(0).sum())
                        original_head=copy.deepcopy(original.get('headline'));new_head=copy.deepcopy(result.get('headline'))
                        for h in [original_head,new_head]:
                            if isinstance(h,dict):h.pop('n_studies',None);h.pop('total_n',None)
                        changed_pool=not original_entry or original_head!=new_head
                        if changed_pool and result.get('headline'):
                            old_cwd=Path.cwd()
                            try:
                                os.chdir(plot_root);kwargs['generate_plots']=True
                                # The temporary plot root is copied beneath the
                                # final subexposure-mode directory below, so the
                                # namespace here is relative to that directory.
                                kwargs['plot_namespace']='incidence_endpoint_v1'
                                if ROOT.name=='metaBreath':kwargs['plot_root']=plot_root/'static'
                                with contextlib.redirect_stdout(io.StringIO()),warnings.catch_warnings():
                                    warnings.simplefilter('ignore');plotted=meta_analysis.perform_meta_analysis(frame,disease,target,**kwargs)
                            finally:os.chdir(old_cwd)
                            for k in ['plot_url','funnel_plot_url','baujat_plot_url']:
                                url=plotted.get(k)
                                result[k]=str(url).replace('static/',f'static/subexposures/{mode}/',1) if url else None
                            changed.append([target,disease,endpoint]);count['recomputed_forest_scopes']+=1
                        else:
                            for k in ['plot_url','funnel_plot_url','baujat_plot_url']:result[k]=original.get(k) if result.get('headline') else None
                    else:
                        result=dict(headline=None,plot_url=None,funnel_plot_url=None,baujat_plot_url=None)
                    output.update({k:result.get(k) for k in ['headline','plot_url','funnel_plot_url','baujat_plot_url']})
                    output.update(exposure=target,cancer=disease,disease=disease,endpoint=endpoint,outcome=endpoint,studies=rows,
                        include_subexposures=mode,subexposure_projection={'mode':mode,'table_rows':len(rows),'pooling_rows':len(frame),'policy':'transitive-child-closure-distinct-populations-v1'})
                    output['summary_html']=result.get('summary_html') or '<p>No studies meet the default pooling criteria. Eligible evidence remains listed below.</p>'
                    # Old endpoint-specific plot descriptors must not point at a prior pool.
                    if 'forest_plot' in output and isinstance(output['forest_plot'],dict):output['forest_plot']['url']=output.get('plot_url')
                    if output.get('screening_stats'):output['screening_stats']['extracted']=len(rows)
                    for suffix in ['all','core']:
                        write(cache_root/policy.key(target)/filename.replace('_true_all.json',f'_true_{suffix}.json'),output)
                    count['scopes']+=1;count['visible_rows']+=len(rows);count['pooling_rows']+=len(frame)
            if (source_root/'_merge_manifest.json').exists():
                (cache_root/'_merge_manifest.json').write_bytes((source_root/'_merge_manifest.json').read_bytes())
            write(cache_root/'_subexposure_manifest.json',{'include_subexposures':mode,'source_cache_sha256':source_hashes,'counts':dict(count)})
            destination=ROOT/'Subexposure_results'/mode
            if destination.exists():shutil.rmtree(destination)
            destination.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(cache_root,destination)
            images=plot_root/'static'
            if images.exists():shutil.copytree(images,ROOT/'static/subexposures'/mode,dirs_exist_ok=True)
            audit['modes'][mode]={'counts':dict(count),'changed_pool_scopes':changed}
            print(json.dumps({'mode':mode,**dict(count)}),flush=True)
            if not skip_summary:
                manifest=summary.build(destination,ROOT/'Plot/subexposures'/mode)
                audit['modes'][mode]['summary_scopes']=len(manifest['scopes'])
    mismatches=[path for path,digest in source_hashes.items() if hashlib.sha256((ROOT/path).read_bytes()).hexdigest()!=digest]
    if mismatches:raise RuntimeError(f'Source cache mutation detected: {mismatches}')
    audit['source_cache_hashes_verified_unchanged']=True
    write(ROOT/'outputs/subexposures_2026-09-05/validation_summary.json',audit)
    return audit

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--skip-summary',action='store_true');parser.add_argument('--summary-only',action='store_true');args=parser.parse_args()
    if args.summary_only:
        for mode in ['yes','no']:
            manifest=summary.build(ROOT/'Subexposure_results'/mode,ROOT/'Plot/subexposures'/mode)
    else:build(args.skip_summary)
