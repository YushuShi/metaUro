#!/usr/bin/env python3
"""Publish reversible pending-review UI choices without changing final decisions."""
import argparse
import copy
import hashlib
import json
import shutil
import sys
from collections import Counter,defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import apply_astra_adjudication as original
from scripts import provisional_ui_overlay as ui
from scripts import provisional_review_policy as policy


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare():
    packet,records,tracks,protected=original.inputs()
    entries=policy.load_pending(ROOT,ui.ALIAS)
    if not entries:raise ValueError('No pending manifest for '+ui.ALIAS)
    saved=ui.checkpoint(records,tracks,entries)
    views,audit=ui.project(records,tracks,saved,protected)
    views,supplemental=ui.project_supplemental(views,tracks,ROOT/'combined',protected)
    effective=copy.deepcopy(packet);effective['results']=[views[cid] for cid in sorted(views)]
    effective['provisional_ui_version']=policy.VERSION
    effective['provisional_supplemental_audit']=supplemental
    report={'repository':ui.ALIAS,'version':policy.VERSION,'review_rows':len(entries),
        'projected_rows':sum(x.get('projected') is True for x in audit),
        'temporary_poolable_contexts':sum(x.get('effective_poolable') is True for x in audit),
        'selected_tracks':dict(Counter(x.get('selected_track') or 'none' for x in audit)),
        'source_sha256':{k:ui.common.digest(v) for k,v in tracks.items()},
        'canonical_final_sha256':sha(ROOT/'combined/final_screening.json'),
        'records':audit,'model_calls':0}
    return packet,records,tracks,protected,saved,effective,report


def femina_cache_publication(effective):
    from scripts import build_combined_results as release
    combined=ROOT/'combined';release.configure_combined_roots(combined)
    included=[r for r in effective['results'] if r.get('full_text_status')=='INCLUDE_META'
        and not release.monthly.meta_analysis.is_curated_meta_analysis_exclusion(r.get('pmid'),r.get('cancer'),r.get('exposure'),'Incidence')]
    if any(not release.screening_contract.is_poolable(r.get('extraction'),r.get('jbi')) for r in included):
        raise ValueError('Effective UI row failed independent pooling validation.')
    prepared=json.loads((release.comparison.PARALLEL_DIR/'prepared_candidates.json').read_text())
    synthetic={'searched_exposures':prepared['catalog_exposures'],'revision_saved_contexts':[],
               'revision_retired_subcategory_contexts':[]}
    release.np.random.seed(0)
    release.monthly.apply_revision_rescreen_authoritatively(synthetic,effective,included,combined)
    from scripts.project_fair_ui import project_packet
    ui.write(combined/'fair_ui_projection.json',project_packet(effective,combined/'Cached_results',
        subcategory_root=combined/'subcategory_results',row_builder=release.monthly.study_row,
        authoritative=True,source_path=combined/'provisional_ui/effective_screening.json'))
    manifest_path=combined/'merge_manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest.update(provisional_ui_version=policy.VERSION,provisional_ui_effective_sha256=sha(combined/'provisional_ui/effective_screening.json'))
    ui.write(manifest_path,manifest)
    release.build_parent_summary_plots(combined)
    from scripts.build_subexposure_outputs import refresh_saved_views
    refresh_saved_views()


def visibility(audit):
    by_context={x['context_id']:x for x in audit};observed={mode:defaultdict(dict) for mode in ['yes','no']}
    for mode in observed:
        if ROOT.name=='metaFemina':
            base=ROOT/'data/subexposure_views'/mode
            paths=[*base.glob('Cached_results/*/*_core.json'),*base.glob('subcategory_results/*/*/*.json')]
        else:paths=list((ROOT/'Subexposure_results'/mode).glob('*/*_core.json'))
        for path in paths:
            for row in json.loads(path.read_text()).get('studies',[]):
                cid=(row.get('provisional_origin_context_id') or row.get('original_screening_context_id')
                     or row.get('Original Screening Context ID') or row.get('context_id'))
                if cid not in by_context:continue
                key=(str(row.get('context_id')),str(row.get('provisional_candidate_track')),str(row.get('subexposure_source')))
                observed[mode][cid][key]={'source':row.get('provisional_candidate_track') or by_context[cid].get('selected_track'),
                    'jbi_grade':row.get('Quality Score'),'selected_for_pooling':row.get('subexposure_pooling_selected') is True,
                    'sample_size':row.get('Sample Size'),'effect_size':row.get('Effect Size'),
                    'ci_lower':row.get('Lower CI'),'ci_upper':row.get('Upper CI')}
    return [{**entry,'ui_modes':{mode:{'visible_rows':len(observed[mode][entry['context_id']]),
        'pooling_selected_rows':sum(r['selected_for_pooling'] for r in observed[mode][entry['context_id']].values()),
        'rows':list(observed[mode][entry['context_id']].values())} for mode in observed}} for entry in audit]


def apply(prepared,output):
    packet,records,tracks,protected,saved,effective,report=prepared;combined=ROOT/'combined'
    paths=['combined','data/subexposure_views'] if ROOT.name=='metaFemina' else ['combined','Subexposure_results','Plot/subexposures','static']
    backup=output/'before_application';backup.mkdir(parents=True,exist_ok=False)
    for name in paths:
        path=ROOT/name
        if path.exists():shutil.copytree(path,backup/name)
    scientific_files=['final_screening.json','manual_review.csv','_manual_overrides.json','_pending_review.json',
                      'astra_adjudication/review_dispositions.json','astra_fulltext/review_dispositions.json']
    original_bytes={name:(combined/name).read_bytes() for name in scientific_files if (combined/name).exists()}
    old_readme=(combined/'README.md').read_text() if (combined/'README.md').exists() else ''
    try:
        ui.write(combined/ui.CHECKPOINT,saved);ui.persist(combined,effective,report['records'])
        if ROOT.name=='metaFemina':femina_cache_publication(effective)
        else:
            from scripts import build_combined_results as builder
            builder.build(builder.parse_args([]))
            installed={r['context_id']:r for r in json.loads((combined/'final_screening.json').read_text())['results']}
            assert set(installed)==set(records)
            assert all(ui.scientific_fingerprint(installed[cid])==ui.scientific_fingerprint(row) for cid,row in records.items()), 'Authoritative scientific record changed during UI rebuild.'
            for name,data in original_bytes.items():(combined/name).write_bytes(data)
            from scripts import rebuild_subexposure_results
            rebuild_subexposure_results.build(skip_summary=False)
        for name,data in original_bytes.items():assert (combined/name).read_bytes()==data,'Scientific file changed: '+name
        _,_,current_tracks,_=original.inputs()
        assert report['source_sha256']=={k:ui.common.digest(v) for k,v in current_tracks.items()},'Original pipeline track changed.'
        installed_audit=json.loads((combined/'provisional_ui/application_audit.json').read_text())
        assert {r['id'] for r in installed_audit}=={r['id'] for r in report['records']}
        final_audit=visibility(installed_audit);ui.write(combined/'provisional_ui/review_dispositions.json',final_audit)
        readme=old_readme.split('<!-- PROVISIONAL_UI_PHASE_BEGIN -->')[0].rstrip()
        (combined/'README.md').write_text(readme+f'''\n\n<!-- PROVISIONAL_UI_PHASE_BEGIN -->
## Temporary UI choices during human review — 2026-09-07

The main and summary caches share reversible temporary pipeline choices for {len(final_audit)} pending review items. When both pipelines supply a complete eligible estimate, ChatGPT is used; otherwise the eligible complete pipeline is preferred. The higher saved valid JBI assessment is used during review, subject to the existing independent pooling restrictions. Canonical final decisions, human authority, review queues and original screening tracks remain unchanged.

The durable inputs are in `provisional_ui/pending_manifest.json`; the separate effective projection and per-review UI audit are `provisional_ui/effective_screening.json` and `provisional_ui/review_dispositions.json`. These choices do not resolve the reviews. Changed source or final-decision fingerprints withhold stale projections on subsequent builds.
<!-- PROVISIONAL_UI_PHASE_END -->\n''')
        summary={k:v for k,v in report.items() if k!='records'}
        summary.update(canonical_final_unchanged=True,scientific_files_verified_unchanged=list(original_bytes),
            original_tracks_unchanged=True,visible_review_contexts={mode:sum(x['ui_modes'][mode]['visible_rows']>0 for x in final_audit) for mode in ['yes','no']},
            pooling_review_contexts={mode:sum(x['ui_modes'][mode]['pooling_selected_rows']>0 for x in final_audit) for mode in ['yes','no']})
        ui.write(output/'installation.json',summary)
    except BaseException:
        for name in paths:
            path=ROOT/name
            if path.exists():shutil.rmtree(path)
            if (backup/name).exists():shutil.copytree(backup/name,path)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    prepared=prepare();report=prepared[-1]
    ui.write(args.output/'proposed_checkpoint.json',prepared[4]);ui.write(args.output/'proposed_effective_screening.json',prepared[5]);ui.write(args.output/'application_audit.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='records'},indent=2),flush=True)
    if args.apply:apply(prepared,args.output);print('Provisional UI caches and both summary modes updated; canonical decisions unchanged.',flush=True)


if __name__=='__main__':main()
