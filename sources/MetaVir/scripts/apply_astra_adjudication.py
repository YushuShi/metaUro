#!/usr/bin/env python3
"""Prepare or install a durable combined-only Astra title/abstract checkpoint.

No model calls and no article full-text access. Dry-run is the default. Existing
human review decisions always take precedence over automated adjudication.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
from collections import Counter

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import astra_combined_overlay as overlay


def read(path):return json.loads(Path(path).read_text())
def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False,default=str)+'\n')


def inputs():
    combined=ROOT/'combined'
    packet=read(combined/'final_screening.json')
    records={row['context_id']:row for row in packet['results']}
    if ROOT.name=='metaFemina':
        tracks={track:{row['context_id']:row for row in read(ROOT/'data/poolability_policy/2026-09-05'/f'{track}_effective_results.json')['results']}
                for track in ('chatgpt','claude_gemini')}
        protected={row['context_id'] for row in read(ROOT/'data/manual_review/2026-09-05/overrides.json')['decisions']}
        from scripts import build_combined_results as release
        protected.update(cid for cid,row in release.existing_manual_rows(combined/'manual_review.csv').items() if row.get('manual_resolution'))
    else:
        from scripts import current_comparison_adapter as adapter
        selected,_,_,_=adapter.current_tracks()
        tracks={key.lower():rows for key,rows in selected.items()}
        manual=read(combined/'_manual_overrides.json') if (combined/'_manual_overrides.json').exists() else {}
        protected={row['context_id'] for row in manual.get('overrides',[])}
    protected.update(cid for cid,row in records.items() if overlay.is_human(row))
    for track in tracks.values():protected.update(cid for cid,row in track.items() if overlay.is_human(row))
    return packet,records,tracks,protected


def prepare(packet_path):
    source=read(packet_path);entries=overlay.packet_entries(source)
    checkpoint=ROOT/'combined'/overlay.CHECKPOINT_NAME
    prior=read(checkpoint) if checkpoint.exists() else {'version':overlay.VERSION,'entries':[]}
    # Re-running the same input replaces that review id rather than duplicating it.
    merged={str(row.get('id') or overlay.entry_context(row)[0]):row for row in overlay.packet_entries(prior)}
    for row in entries:
        key=str(row.get('id') or overlay.entry_context(row)[0])
        if not key:raise ValueError('An Astra decision lacks both review id and context id.')
        merged[key]=row
    checkpoint_packet={'version':overlay.VERSION,'model':'gpt-6-astra','source_scope':'TITLE_ABSTRACT_ONLY',
        'source_packet':str(Path(packet_path).resolve()),'source_packet_sha256':overlay.digest(source),'entries':list(merged.values())}
    packet,records,tracks,protected=inputs()
    updated,audit=overlay.apply_entries(records,tracks,checkpoint_packet['entries'],protected)
    for cid in protected:
        if cid in records and updated.get(cid)!=records[cid]:raise AssertionError('Human review changed: '+cid)
    proposed=copy.deepcopy(packet);proposed['results']=[updated[cid] for cid in sorted(updated)]
    report={'repository':ROOT.name,'version':overlay.VERSION,'source_scope':'TITLE_ABSTRACT_ONLY','source_packet':str(packet_path),
        'decisions':len(checkpoint_packet['entries']),'applied_contexts':sum(row['applied'] for row in audit),
        'retained_for_review':sum(not row['applied'] for row in audit),'reason_counts':dict(Counter(row['reason'] for row in audit)),
        'protected_human_contexts':len(protected),'original_tracks_unchanged':True,'model_calls':0,'records':audit}
    return checkpoint_packet,proposed,report


def apply(checkpoint,report,skip_figures):
    combined=ROOT/'combined';destination=combined/overlay.CHECKPOINT_NAME
    audit_root=ROOT/'outputs/astra_adjudication_2026-09-07'
    backup=audit_root/'before_application'
    backup.mkdir(parents=True,exist_ok=True)
    for name in ['final_screening.json',overlay.CHECKPOINT_NAME,'manual_review.csv','_manual_overrides.json','merge_manifest.json','_merge_manifest.json']:
        path=combined/name
        if path.exists() and not (backup/name).exists():shutil.copy2(path,backup/name)
    write(destination,checkpoint)
    if ROOT.name=='metaFemina':
        from scripts import recompare_current_effective_results as comparison
        current=comparison.load_inputs();result=comparison.prepare_comparison(current)
        comparison.write_reports(*result)
        comparison.publish(result[0],result[-1],current,rebuild_figures=not skip_figures)
        write(combined/'astra_application_audit.json',report)
        if skip_figures:
            from scripts import subexposure_views
            subexposure_views.build(generate_plots=False)
    else:
        from scripts import build_combined_results as builder
        args=builder.parse_args(['--skip-plots'] if skip_figures else [])
        builder.build(args)
        from scripts import rebuild_subexposure_results
        rebuild_subexposure_results.build(skip_summary=skip_figures)
    installed={row['context_id']:row for row in read(combined/'final_screening.json')['results']}
    dispositions=overlay.review_dispositions(report['records'],installed)
    write(combined/'astra_adjudication/review_dispositions.json',dispositions)
    write(combined/'astra_adjudication/application_audit.json',report)
    prior={row['context_id']:row for row in read(backup/'final_screening.json')['results']}
    changed=[cid for cid in prior if installed.get(cid)!=prior[cid]]
    write(audit_root/'installation.json',{'version':overlay.VERSION,'changed_contexts':len(changed),
        'contexts_with_astra_metadata':sum(bool(row.get('astra_adjudication')) for row in installed.values()),
        'final_counts':dict(Counter(row.get('final_status') or row.get('full_text_status') for row in installed.values())),
        'checkpoint_retained':destination.exists(),'new_model_calls':0})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--skip-figures',action='store_true')
    args=parser.parse_args()
    checkpoint,proposed,report=prepare(args.packet)
    output=ROOT/'outputs/astra_adjudication_2026-09-07'
    write(output/'proposed_checkpoint.json',checkpoint)
    write(output/'proposed_final_screening.json',proposed)
    write(output/'application_audit.json',report)
    print(json.dumps({key:value for key,value in report.items() if key!='records'},indent=2),flush=True)
    if args.apply:
        apply(checkpoint,report,args.skip_figures)
        print('Durable combined checkpoint and derived results updated.',flush=True)

if __name__=='__main__':main()
