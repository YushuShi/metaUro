"""Publish an audited scientific correction over immutable MetaUro snapshots."""
from __future__ import annotations
import argparse, contextlib, copy, hashlib, io, json, os, shutil, sys, tempfile
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runtime_code.trend_ci_projection import correct_tree
from automation.sync_sources import allowed, load_overlay, write, sha
VERSION='trend-p-ci-2026-09-27'
AUDIT=ROOT/'evidence-revisions'/VERSION


def targets(source):
    base=ROOT/'sources'/source
    files=[]
    if source=='metaPee':
        files += [base/'combined/effective_results.json',base/'combined/provisional_ui/effective_results.json']
        roots=['Cached_results','combined','Subexposure_results','Disease_hierarchy_results']
    else:
        files += [base/'combined/final_screening.json']
        roots=['Cached_results','combined','Subexposure_results']
    for folder in roots:
        for p in (base/folder).rglob('*.json'):
            if p.name.endswith(('_core.json','_all.json')) and not any(x in p.parts for x in ['_history','static']):files.append(p)
    return sorted(set(p for p in files if p.is_file() and allowed(p.relative_to(base).as_posix())))


def plan():
    files=[];protected={'evidence-manifest.json':sha(ROOT/'evidence-manifest.json')}
    for source in ['metaPee','MetaVir']:
        for p in targets(source):
            protected[str(p.relative_to(ROOT))]=sha(p)
            value=json.loads(p.read_text());changes=correct_tree(value)
            if changes:files.append({'source':source,'file':p.relative_to(ROOT/'sources'/source).as_posix(),
                                     'source_sha256':sha(p),'changes':changes})
    audit={'mode':'read_only_before_mutation','protected_sha256':protected,'affected_files':files,
           'summary':{'files':len(files),'objects':sum(len(f['changes']) for f in files)},'network_calls':0,'model_calls':0}
    write(AUDIT/'audit_expanded_before.json',audit)
    print(audit['summary'],flush=True)
    return audit


def apply():
    audit=json.loads((AUDIT/'audit_expanded_before.json').read_text())
    for relative,h in audit['protected_sha256'].items():
        if sha(ROOT/relative)!=h:raise ValueError('Immutable source differs from audited baseline: '+relative)
    by_source={};changes=[]
    for item in audit['affected_files']:
        source=item['source'];p=ROOT/'sources'/source/item['file'];value=json.loads(p.read_text());new=correct_tree(value)
        if not new:continue
        if isinstance(value.get('results'),list):
            counts=dict(Counter(r.get('final_status') for r in value['results']))
            for key in ['counts','status_counts']:
                if key in value:value[key]=counts
        value['trend_ci_correction_version']=VERSION
        stored='versions/'+VERSION+'/files/'+item['file'];output=ROOT/'updates'/source/stored
        write(output,value)
        by_source.setdefault(source,{})[item['file']]={'sha256':sha(output),'original_sha256':sha(p),
            'stored_path':stored,'run_id':VERSION,'source_commit':'immutable-baseline'}
        changes.append({**item,'stored_path':str(output.relative_to(ROOT)),'after_sha256':sha(output)})
    write(AUDIT/'application_report.json',{'status':'intervals_retracted_refresh_pending','changes':changes})
    # Pointer publication happens only after the affected outputs are rebuilt.
    write(AUDIT/'proposed_overlay_files.json',by_source)
    print('Prepared',len(changes),'overlay corrections',flush=True)


def publish():
    audit=json.loads((AUDIT/'audit_expanded_before.json').read_text())
    for relative,h in audit['protected_sha256'].items():
        if sha(ROOT/relative)!=h:raise ValueError('Immutable source changed: '+relative)
    refresh=json.loads((AUDIT/'cache_refresh_report.json').read_text())
    checked={item['stored_path']:item for item in refresh['refreshed_files']}
    application=json.loads((AUDIT/'application_report.json').read_text())
    for item in application['changes']:
        if any(len(c['json_path']) == 2 and c['json_path'][0] == 'studies' for c in item['changes']):
            current=checked.get(item['stored_path'])
            if not current or current['output_sha256'] != sha(ROOT/item['stored_path']):
                raise ValueError('Corrected cache has not passed the required output refresh: '+item['file'])
    proposed=json.loads((AUDIT/'proposed_overlay_files.json').read_text())
    index=json.loads((ROOT/'updates/manifest.json').read_text()) if (ROOT/'updates/manifest.json').exists() else {'schema_version':1,'sources':{}}
    for source in proposed:
        old=load_overlay(ROOT,source);files=copy.deepcopy((old or {}).get('files',{}))
        directory=ROOT/'updates'/source/'versions'/VERSION/'files'
        for path in directory.rglob('*'):
            if not path.is_file():continue
            relative=path.relative_to(directory).as_posix()
            if not allowed(relative):raise ValueError('Unexpected correction path: '+relative)
            original=ROOT/'sources'/source/relative
            files[relative]={'sha256':sha(path),'original_sha256':sha(original) if original.is_file() else None,
                 'stored_path':path.relative_to(ROOT/'updates'/source).as_posix(),
                 'source_commit':'immutable-baseline','run_id':VERSION}
        manifest={'schema_version':1,'kind':'scientific_correction','source':source,'run_id':VERSION,
                  'source_commit':'immutable-baseline','files':files,'review_files':(old or {}).get('review_files',[]),
                  'audit':str((AUDIT/'application_report.json').relative_to(ROOT)),
                  'audit_sha256':sha(AUDIT/'application_report.json'),'new_search_performed':False,'new_screening_performed':False,
                  'search_through':(old or {}).get('search_through'),'human_review_pending':(old or {}).get('human_review_pending',0)}
        path=ROOT/'updates'/source/'versions'/VERSION/'manifest.json';write(path,manifest)
        index['sources'][source]={'manifest':path.relative_to(ROOT/'updates').as_posix(),'sha256':sha(path)}
    write(ROOT/'updates/manifest.json',index)
    print('Published local correction overlay pointer',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['plan','apply','publish']);args=p.parse_args()
    globals()[args.action]()
