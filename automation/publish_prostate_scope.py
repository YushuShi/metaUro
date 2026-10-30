"""Publish a derived evidence scope without rewriting a completed monthly release."""
from __future__ import annotations
import argparse, copy, json, os, sys
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from automation.sync_sources import assembled_source, load_overlay, read, write, sha
from prostate_scopes import VERSION, LABEL, SCOPE_ID, TRANSLATIONS, extend_catalog
AUDIT=ROOT/'evidence-revisions'/VERSION


def initialize():
    source=assembled_source(ROOT,'MetaVir')
    os.environ['METAURO_SUMMARY_SOURCE_ROOT']=str(source)
    sys.path[:0]=[str(ROOT/'runtime_code/MetaVir'),str(source)]
    return source


def results():
    from automation.autonomous_monthly import validate_completed_release
    release=validate_completed_release(ROOT)
    if not release:
        raise ValueError('A completed monthly source release is required')
    write(AUDIT/'previous_overlay_index.json',read(ROOT/'updates/manifest.json'))
    write(AUDIT/'parent_monthly_validation.json',{'status':'validated',
        'run_id':release['run_id'],'validated_at':datetime.now(timezone.utc).isoformat()})
    source=initialize()
    from automation.prostate_scope_results import materialize
    stage=ROOT/'updates/MetaVir/versions'/VERSION/'files'
    report=materialize(source,stage)
    write(stage/'static/seer-baseline.json',extend_catalog(read(source/'static/seer-baseline.json')))
    translations=read(source/'static/i18n-translations.json')
    translations[LABEL]=TRANSLATIONS
    write(stage/'static/i18n-translations.json',translations)
    write(AUDIT/'cache_projection.json',{'version':VERSION,'model_calls':0,'new_search':False,
        'definition':'Remove only estimates explicitly classified as aggressive endpoints; preserve all remaining scientific rows and pooling gates.',
        'files':report})
    old=load_overlay(ROOT,'MetaVir'); manifest=copy.deepcopy(old)
    manifest.update(kind='evidence_scope_revision',run_id=VERSION,new_search_performed=False,
                    new_screening_performed=False,derived_from_overlay=read(AUDIT/'previous_overlay_index.json')['sources']['MetaVir'])
    for path in sorted(stage.rglob('*')):
        if not path.is_file():continue
        rel=path.relative_to(stage).as_posix()
        base=ROOT/'sources/MetaVir'/rel
        manifest['files'][rel]={'sha256':sha(path),'original_sha256':sha(base) if base.exists() else None,
            'stored_path':str(path.relative_to(ROOT/'updates/MetaVir')),'run_id':VERSION,
            'source_commit':'derived-current-prostate-evidence'}
    mp=stage.parent/'manifest.json';write(mp,manifest)
    index=read(ROOT/'updates/manifest.json')
    index['sources']['MetaVir']={'manifest':str(mp.relative_to(ROOT/'updates')),'sha256':sha(mp)}
    write(ROOT/'updates/manifest.json',index)
    print(f'Published {len(report)} derived caches',flush=True)


def summaries():
    source=initialize()
    import summary_outputs
    from scripts import build_summary_plots as summary
    old_inventory=summary.inventory;old_finalize=summary.finalize_build
    summary.inventory=lambda *a,**kw:{k:v for k,v in old_inventory(*a,**kw).items() if k=='Prostate cancer\\aggressive'}
    def finish(root,stage,manifest):
        previous=read(stage/'summary_manifest.json')
        owned=previous['sources']['MetaVir']['scopes']
        manifest['scopes']={**{k:previous['scopes'][k] for k in owned},**manifest['scopes']}
        manifest['evidence_revision']=VERSION
        return old_finalize(root,stage,manifest)
    summary.finalize_build=finish
    for mode in ('no','yes'):
        summary.build(summary_outputs.cache_dir(ROOT,'MetaVir',mode),ROOT/'summary'/mode)
        print(f'Published {mode} summary scope',flush=True)
    # Browser-only changes and the additive scope did not alter urinary
    # summaries. Bind their conserved artifacts to the current input inventory.
    for mode in ('no','yes'):
        path=ROOT/'summary'/mode/'summary_manifest.json';m=read(path)
        for source_name in ('MetaVir','metaPee'):
            m['sources'][source_name]['input_sha256']=summary_outputs.input_fingerprint(ROOT,source_name,mode)
        write(path,m)


def finalize():
    state=read(ROOT/'updates/monthly/state.json');parent=read(ROOT/state['manifest_path'])
    projection=read(AUDIT/'cache_projection.json')
    previous=read(AUDIT/'previous_overlay_index.json')
    # Parent scientific inputs must be byte-identical after projection.
    source=assembled_source(ROOT,'MetaVir')
    for f in projection['files']:
        parent_path=source/f['file'].replace(SCOPE_ID+'_','prostate_cancer_',1)
        if sha(parent_path)!=f['parent_sha256']:raise ValueError('Parent evidence changed')
    for relative,digest in parent['audit_artifact_sha256'].items():
        if sha(ROOT/relative)!=digest:raise ValueError('Monthly audit changed')
    code=['prostate_scopes.py','source_worker.py','app.py','study_selection.py','summary_outputs.py',
        'automation/prostate_scope_results.py','automation/publish_prostate_scope.py',
        'automation/autonomous_projection.py','automation/autonomous_monthly.py','runtime_code/manifest.json',
        'runtime_code/MetaVir/static/script.js','runtime_code/metaPee/static/script.js']
    write(AUDIT/'release.json',{'schema_version':1,'repository':'metaUro','status':'completed',
        'revision_id':VERSION,'completed_at':datetime.now(timezone.utc).isoformat(),
        'active_overlay_index':read(ROOT/'updates/manifest.json'),
        'parent_monthly':{'manifest_path':state['manifest_path'],'manifest_sha256':state['manifest_sha256'],
            'run_id':state['run_id'],'cutoff':state['search_through']['MetaVir']},
        'parent_audit_artifact_sha256':parent['audit_artifact_sha256'],
        'summary_sha256':{str(p.relative_to(ROOT)):sha(p) for p in sorted((ROOT/'summary').rglob('*')) if p.is_file() and p.suffix in {'.json','.pdf','.xlsx'}},
        'code_sha256':{p:sha(ROOT/p) for p in code},
        'projection_sha256':sha(AUDIT/'cache_projection.json'),
        'new_search_performed':False,'new_screening_performed':False})
    print('Completed evidence-view revision; parent monthly audit preserved',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['results','summaries','finalize']);args=p.parse_args()
    globals()[args.action]()
