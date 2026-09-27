"""Refresh affected saved analyses offline, retaining their recorded baseline."""
from pathlib import Path
import contextlib, copy, hashlib, io, json, os, shutil, tempfile
import pandas as pd
import numpy as np
import sys
_SOURCE=Path(__file__).resolve().parents[1]/'sources/metaPee'
sys.path[:0]=[str(_SOURCE.parent.parent/'runtime_code/metaPee'),str(_SOURCE)]
import meta_analysis
import analysis_settings_backend as settings_backend
from scripts import subexposure_policy

ROOT=Path(__file__).resolve().parents[1]
AUDIT=ROOT/'evidence-revisions/trend-p-ci-2026-09-27'

def clean(v):
    if isinstance(v,dict):return {k:clean(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)):return [clean(x) for x in v]
    if hasattr(v,'item'):return clean(v.item())
    if isinstance(v,float) and not np.isfinite(v):return None
    return v

def write(p,data):
    p.write_text(json.dumps(clean(data),indent=2,ensure_ascii=False,allow_nan=False)+'\n')

def refresh(path,data):
    rows=data['studies'];disease=data.get('disease') or data['cancer'];exposure=data['exposure']
    frame=pd.DataFrame(rows)
    for key in ['Effect Size','Lower CI','Upper CI','SE','Cases','Sample Size']:
        if key in frame:frame[key]=pd.to_numeric(frame[key],errors='coerce')
    # Use existing saved baseline and conversion assumptions for this narrow
    # correction; a trend-CI retraction must not silently alter those settings.
    old_base=(data.get('headline') or {}).get('baseline_risk')
    catalog=json.loads((_SOURCE/'static/seer-baseline.json').read_text())
    settings=settings_backend.normalize_settings(None,catalog)
    baseline=settings_backend.resolve_baseline(catalog,disease,None,settings)
    if old_base is not None:
        baseline.update(p0=old_base,risk_percent=old_base*100,available=True,identity_approximation=False)
    context={'settings':settings,'baseline':baseline,'disease':disease,'exposure':exposure,
             'repository':ROOT.name,'quality_filter':'Moderate+','min_cases':0,
             'pooled_plot_rows':[],'pooled_study_keys':set()}
    token=settings_backend._ACTIVE.set(context)
    oldcwd=Path.cwd()
    try:
        original=getattr(meta_analysis.perform_meta_analysis,'__wrapped__',meta_analysis.perform_meta_analysis)
        with tempfile.TemporaryDirectory(prefix='trend-ci-plots-') as scratch:
            os.chdir(scratch)
            options={'outcome':data.get('endpoint') or data.get('outcome') or 'Incidence','exclude_meta':True,
                     'df_all':frame.copy(),'generate_plots':True}
            with contextlib.redirect_stdout(io.StringIO()):
                result=original(frame.copy(),disease,exposure,**options)
            if result.get('error'):
                if not any(pd.notna(row.get('Lower CI')) and row.get('subexposure_pooling_selected') is not False for row in rows):
                    result={'success':True,'headline':None,'summary_html':'<p>Eligible reports are retained, but no supported confidence intervals are available for the current pool.</p>',
                            'plot_url':None,'funnel_plot_url':None,'baujat_plot_url':None}
                else:raise RuntimeError(str(path)+': '+str(result['error']))
            stage=ROOT/'updates/metaPee/versions/trend-p-ci-2026-09-27/files'
            relative=path.relative_to(stage);mode=data.get('include_subexposures') or 'no'
            namespace=hashlib.sha256(str(relative.parent).encode()).hexdigest()[:12]
            if ROOT.name=='metaUro':
                if 'provisional_ui' in relative.parts:
                    target=stage/'combined/provisional_ui/static'/mode/'trend-p-ci'/namespace;prefix=f'provisional-assets/{mode}/trend-p-ci/{namespace}'
                else:target=stage/'static/trend-p-ci'/namespace;prefix=f'static/trend-p-ci/{namespace}'
            elif ROOT.name=='metaFemina':
                if relative.parts[0]=='data':target=ROOT/'data/subexposure_views'/mode/'static/trend-p-ci';prefix=f'subexposure-assets/{mode}/trend-p-ci'
                elif 'subexposure_views' in relative.parts:target=ROOT/'chatgpt/subexposure_views'/mode/'static/trend-p-ci';prefix=f'chatgpt-subexposure-assets/{mode}/trend-p-ci'
                else:target=ROOT/relative.parts[0]/'static/trend-p-ci';prefix=relative.parts[0]+'-static/trend-p-ci'
            elif 'provisional_ui' in relative.parts:
                target=ROOT/'combined/provisional_ui/static'/mode/'trend-p-ci'/namespace;prefix=f'provisional-assets/{mode}/trend-p-ci/{namespace}'
            else:target=ROOT/'static/trend-p-ci'/namespace;prefix=f'static/trend-p-ci/{namespace}'
            for key in ['plot_url','funnel_plot_url','baujat_plot_url']:
                value=result.get(key)
                if not value:continue
                source=Path(scratch)/str(value).split('?',1)[0]
                if not source.is_file():raise FileNotFoundError(source)
                tail=source.relative_to(Path(scratch)/'static')
                output=target/tail;output.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,output)
                result[key]=prefix+'/'+tail.as_posix()
    finally:
        os.chdir(oldcwd);settings_backend._ACTIVE.reset(token)
    count=len(context['pooled_plot_rows'])
    result.pop('studies',None)
    data.update(clean(result),studies=rows,pooled_study_count=count)
    data.pop('error',None)
    return {'file':str(path.relative_to(ROOT)),'pool_count':count,'baseline_risk':baseline['p0'],
            'headline':data.get('headline')}

def main():
    audit=json.loads((AUDIT/'application_report.json').read_text());reports=[];calculated={}
    for item in audit['changes']:
        if not any(len(c['json_path'])==2 and c['json_path'][0]=='studies' for c in item['changes']):continue
        path=ROOT/item['stored_path'];data=json.loads(path.read_text());key=str(path).replace('_all.json','_core.json')
        if key in calculated:
            template=calculated[key]
            for field in template:
                if field!='studies':data[field]=copy.deepcopy(template[field])
            report={'file':item['file'],'reused_identical_scope':True,'pool_count':data.get('pooled_study_count')}
        else:
            report=refresh(path,data);calculated[key]=copy.deepcopy(data)
        write(path,data);report['output_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();report['stored_path']=str(path.relative_to(ROOT));reports.append(report);print(item['file'],report['pool_count'],flush=True)
    write(AUDIT/'cache_refresh_report.json',{'refreshed_files':reports,'model_calls':0,'network_calls':0})

if __name__=='__main__':main()
