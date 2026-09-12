#!/usr/bin/env python3
"""Derive parent disease views and current-default summary data from saved ChatGPT evidence.

No source cache, frozen SEER file, or live plot is modified. Output must be a new
empty directory. The source disease labels and overlap decisions remain in each
projection JSON. SUMMARY_DATASETS.json is suitable for build_summary_plots.py's
workbook/forest_plot/heterogeneity_plot functions; its estimates use the exact
current app default finite-interval baseline, or the declared identity proxy.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import io
import json
import math
import sys
import warnings
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pandas as pd
import app
import meta_analysis
import analysis_settings_backend as settings_backend
from disease_hierarchy import compose_result, descendant_keys, SOURCES
from scripts import build_summary_plots as plots


def clean(value):
    if isinstance(value,dict):return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [clean(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value):return None
    if hasattr(value,'item'):return clean(value.item())
    return value


def dump(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(clean(value),indent=2,ensure_ascii=False,allow_nan=False)+'\n')


def summarize(payload, disease, exposure, dietary, catalog):
    frame=pd.DataFrame(payload['studies'])
    if frame.empty:return None,[]
    if dietary:
        frame=frame.loc[frame.get('exposure_measurement_type',pd.Series('',index=frame.index)).eq('dietary_intake')].copy()
    if frame.empty:return None,[]
    for name in ('Effect Size','Lower CI','Upper CI','SE','Sample Size','Cases'):
        if name not in frame:frame[name]=None
        frame[name]=pd.to_numeric(frame[name].astype(str).str.replace(',','',regex=False),errors='coerce')
    frame['Estimated Cases']=None
    settings=settings_backend.normalize_settings(None,catalog)
    baseline=settings_backend.resolve_baseline(catalog,disease,None,settings)
    context={'settings':settings,'baseline':baseline,'disease':disease,'exposure':exposure,
        'repository':ROOT.name,'quality_filter':'Moderate+','pooled_plot_rows':[],'pooled_study_keys':set()}
    token=settings_backend._ACTIVE.set(context)
    try:
        # Request-aware conversion/baseline helpers stay active. Invoke the core
        # pooling function to avoid rendering hundreds of per-exposure figures.
        original=getattr(meta_analysis.perform_meta_analysis,'__wrapped__',meta_analysis.perform_meta_analysis)
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
            warnings.simplefilter('ignore')
            result=original(frame,disease,exposure,outcome='Incidence',exclude_meta=True,
                df_all=frame,generate_plots=False)
        pooled=context['pooled_plot_rows']
    finally:settings_backend._ACTIVE.reset(token)
    headline=(result or {}).get('headline') or {}
    canonical=headline.get('canonical_rr') or headline
    if not pooled or canonical.get('pooled_es') is None:return None,[]
    if any(canonical.get(k) is None for k in ('pooled_es','ci_low','ci_upp')):return None,[]
    def numeric(row,name):
        try:
            value=float(row.get(name));return value if math.isfinite(value) else 0
        except (TypeError,ValueError):return 0
    def cases(row):
        actual=settings_backend._number(row.get('Cases'))
        if actual is not None:return actual
        size=numeric(row,'Sample Size')
        return math.floor(size*baseline['p0']+.5) if baseline['p0'] is not None else 0
    count_name='number studies' if ROOT.name=='MetaCutis_et_ Capillus' else 'number estimates'
    record={'Exposure':exposure,count_name:len(pooled), 'Pooled RR':canonical['pooled_es'],
        'lower CI RR':canonical['ci_low'],'upper CI RR':canonical['ci_upp'],
        'lower PI RR':canonical.get('pi_low'),'upper PI RR':canonical.get('pi_upp'),
        'I^2 (%)':round(float(headline.get('i2') or 0),1),'eggers p-value':headline.get('eggers_p'),
        'total N':int(sum(numeric(r,'Sample Size') for r in pooled)),
        'total Cases':int(sum(cases(r) for r in pooled)), 'category':plots.exposure_category(exposure),
        'baseline_risk':baseline,'settings':settings}
    return record,[r['disease_hierarchy_projection_id'] for r in pooled]


def build(mode,output):
    if output.exists() and any(output.iterdir()):raise ValueError('Output directory must be empty.')
    output.mkdir(parents=True,exist_ok=True)
    cache_root=ROOT/'Subexposure_results'/mode
    if not cache_root.is_dir():raise ValueError(f'Missing saved mode directory: {cache_root}')
    scopes=app.get_scope_config()
    catalog_path=ROOT/'static/seer-baseline.json'
    catalog=json.loads(catalog_path.read_text())
    index={};source_hashes={};projections=0;visible_rows=0;selected_rows=0
    for path in sorted(cache_root.glob('*/*_incidence_true_core.json')):
        payload=json.loads(path.read_text())
        if payload.get('canonical_track')!='chatgpt':continue
        key=app.get_major_scope_key(payload.get('disease') or payload.get('cancer'),scopes)
        exposure=payload.get('exposure')
        if not key or not exposure:continue
        index[(key,exposure)]=(payload,path)
    parents=[key for key in scopes if scopes[key].get('children')]
    result={'schema_version':1,'repository':ROOT.name,'include_subexposures':mode,
        'source_baseline_sha256':hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        'hierarchy_sources':SOURCES,'baseline_policy':'Current app default finite-interval reference; inherited subtype rows use selected parent reference as proxy. Missing exact Skin cancer aggregate risk uses identity approximation.',
        'eligibility_note':'Good or Moderate JBI, saved clinical/manual gates, and eligible quantitative ratios. Parent-direct estimate preferred within overlapping PMID/population; otherwise one eligible adjusted/primary child. Only explicitly disjoint populations retained separately.',
        'scopes':{},'source_checksums':source_hashes}
    for parent in parents:
        child_keys=descendant_keys(scopes,parent)
        exposures=sorted({exposure for key,exposure in index if key in [parent,*child_keys]},key=str.casefold)
        entry={'label':scopes[parent]['disease'],'children':child_keys,'datasets':{'combined':[],'dietary':[]},'projections':[]}
        for exposure in exposures:
            direct=index.get((parent,exposure));sources=[]
            for child in child_keys:
                saved=index.get((child,exposure))
                if saved:sources.append((child,saved[0],str(saved[1].relative_to(ROOT))))
            payload=compose_result(direct[0] if direct else None,sources,parent,scopes,exposure,'Incidence',
                source_filter=meta_analysis.filter_curated_meta_analysis_exclusions,include_direct_only=True)
            relative=Path('projections')/app.safe_path_component(exposure)/(parent+'_incidence_true_core.json')
            pooled_ids={}
            for dataset,dietary in [('combined',False),('dietary',True)]:
                record,ids=summarize(payload,scopes[parent]['disease'],exposure,dietary,catalog)
                if record:entry['datasets'][dataset].append(record)
                pooled_ids[dataset]=ids
            payload['summary_pooling_ids']=pooled_ids
            dump(output/relative,payload)
            entry['projections'].append(str(relative));projections+=1
            visible_rows+=len(payload['studies']);selected_rows+=len(pooled_ids['combined'])
            for saved in ([direct] if direct else [])+[index[(child,exposure)] for child in child_keys if (child,exposure) in index]:
                path=saved[1];source_hashes[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
        result['scopes'][parent]=entry
    result['counts']={'parent_scopes':len(parents),'projections':projections,'visible_rows':visible_rows,
        'pooled_rows_across_exposure_contexts':selected_rows,'source_files':len(source_hashes)}
    # Verify every source again after derivation; fail on concurrent mutation.
    assert all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest for path,digest in source_hashes.items())
    dump(output/'SUMMARY_DATASETS.json',result)
    dump(output/'CREATED_FILES_MANIFEST.json',{str(path.relative_to(output)):hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob('*')) if path.is_file()})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include-subexposures',choices=['yes','no'],default='no')
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();result=build(args.include_subexposures,args.output_dir)
    print(json.dumps(result['counts'],indent=2))

if __name__=='__main__':main()
