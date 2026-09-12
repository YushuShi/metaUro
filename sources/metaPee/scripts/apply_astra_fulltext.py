#!/usr/bin/env python3
"""Apply verified PMC-text Astra decisions to combined evidence. Dry run by default."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import astra_fulltext_common as common
from scripts import astra_fulltext_overlay as overlay
from scripts import apply_astra_adjudication as abstract
from scripts import reconcile_screening_tracks as reconcile
from scripts import key_comparison_policy as policy
VERSION=common.VERSION
read=abstract.read
dump=abstract.dump
digest=common.digest
human=common.is_human
apply_context=overlay.apply_context
candidates_for_record=overlay.candidates_for_record

def build_combined_modes(paths,destination):
    previous=abstract.VERSION
    try:
        abstract.VERSION=VERSION
        return abstract.build_combined_modes(paths,destination)
    finally:abstract.VERSION=previous

def run(packet_path,output,apply=False):
    packet=read(packet_path)
    common.packet_entries(packet)
    if packet.get('complete') is False:raise ValueError('The validated adjudication packet is incomplete.')
    if output.exists() and any(output.iterdir()):raise ValueError('Use a new empty output directory for this run.')
    output.mkdir(parents=True,exist_ok=True)
    source_paths={track:reconcile.SOURCE_DIR/f'{track}_effective_results.json' for track in ('chatgpt','claude_gemini')}
    source_hashes={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths.values()}
    tracks={track:{row['context_id']:row for row in read(path)['results']} for track,path in source_paths.items()}
    combined_path=ROOT/'combined/effective_results.json';combined=read(combined_path)
    grouped=defaultdict(list);audit=[]
    for entry in packet.get('results',[]):
        row=entry.get('source_row') or {};cid=row.get('Context ID')
        if not cid:raise ValueError('Every adjudication requires the original CSV Context ID.')
        if entry.get('id')!=entry.get('adjudication',{}).get('id'):raise ValueError('Decision and packet IDs differ.')
        grouped[cid].append(entry)
    records=[];changed=set();all_items={}
    for original in combined['results']:
        cid=original['context_id']
        if cid not in grouped:records.append(original);continue
        for entry in grouped[cid]:
            row=entry['source_row']
            if str(row.get('PMID'))!=str(original.get('pmid')) or row.get('Exposure')!=original.get('exposure') or row.get('Disease')!=(original.get('disease') or original.get('cancer')):raise ValueError(f'CSV context identity mismatch: {entry["id"]}')
            if not policy.missing(row.get('Endpoint')) and row['Endpoint']!=(original.get('endpoint') or original.get('outcome') or 'Incidence'):raise ValueError(f'CSV endpoint mismatch: {entry["id"]}')
        updated,items,decisions=apply_context(original,tracks,grouped[cid])
        for decision in decisions:decision['context_id']=cid
        audit.extend(decisions);records.append(updated);all_items[cid]=items
        if digest(updated)!=digest(original):changed.add(cid)
    missing=set(grouped)-{row['context_id'] for row in combined['results']}
    if missing:raise ValueError(f'Contexts absent from combined: {sorted(missing)}')
    report={'version':VERSION,'model':'gpt-6-astra','source_scope':common.SOURCE_SCOPE,'apply_requested':apply,
        'changed_contexts':len(changed),'decision_counts':dict(Counter('applied' if row['applied'] else 'retained_for_review' for row in audit)),
        'combined_status_counts':dict(Counter(row.get('final_status') for row in records)),
        'changed_context_ids':sorted(changed),'source_hashes':source_hashes,'combined_before_sha256':hashlib.sha256(combined_path.read_bytes()).hexdigest()}
    dump(output/'decisions.json',audit);dump(output/'input_packet.json',packet)
    if apply and changed:
        scopes={reconcile.scope(row) for row in records if row['context_id'] in changed}
        groups=defaultdict(list)
        for record in records:
            key=reconcile.scope(record)
            if key in scopes:groups[key].extend(all_items.get(record['context_id']) or candidates_for_record(record,tracks))
        stage=output/'staged_combined';old_combined,old_audit=reconcile.COMBINED,reconcile.AUDIT
        try:
            reconcile.COMBINED=stage;reconcile.AUDIT=output/'builder_audit'
            report['materialization']=reconcile.materialize(groups)
            if report['materialization']['errors']:raise RuntimeError('Combined cache materialization has errors; nothing applied.')
        finally:reconcile.COMBINED=old_combined;reconcile.AUDIT=old_audit
        # Explicitly empty fully excluded scopes so no old candidate survives.
        from scripts.rebuild_relaxed_poolability import cache_paths
        for scope,items in groups.items():
            if items:continue
            exposure,disease,endpoint=scope
            empty={'canonical_track':'combined','exposure':exposure,'disease':disease,'cancer':disease,'endpoint':endpoint,'outcome':endpoint,'studies':[],
                'headline':None,'plot_url':None,'funnel_plot_url':None,'baujat_plot_url':None,'requires_key_review':False,'astra_adjudication_version':VERSION}
            for original in cache_paths(*scope):dump(stage/original.relative_to(ROOT/'Cached_results'),empty)
        source_caches={str(path.relative_to(ROOT/'combined')):path for path in (ROOT/'combined').glob('*/*_true_all.json')}
        source_caches.update({str(path.relative_to(stage)):path for path in stage.glob('*/*_true_all.json')})
        report['subexposure_modes']=build_combined_modes(source_caches.values(),stage/'subexposure_views')
        combined['results']=records;combined['astra_fulltext_version']=VERSION
        dump(stage/'effective_results.json',combined)
        previous_packet_path=ROOT/'combined/astra_fulltext/current_decisions.json'
        previous_packet=read(previous_packet_path) if previous_packet_path.exists() else {}
        cumulative={entry['id']:entry for entry in previous_packet.get('results',[])}
        cumulative.update({entry['id']:entry for entry in packet.get('results',[])})
        snapshots=copy.deepcopy(previous_packet.get('record_snapshots') or {})
        for row in records:
            if row['context_id'] in changed:
                cid=row['context_id'];snapshots[cid]={'record':copy.deepcopy(row),'record_sha256':common.digest(row),'source_fingerprint':common.source_digest(tracks['chatgpt'].get(cid),tracks['claude_gemini'].get(cid))}
        stored_packet={**packet,'results':list(cumulative.values()),'record_snapshots':snapshots}
        dump(stage/'astra_fulltext/current_decisions.json',stored_packet)
        dump(stage/'astra_fulltext/application_audit.json',audit)
        by_context={row['context_id']:row for row in records}
        dispositions=[{key:row.get(key) for key in ('id','applied','reason','follow_up_required')} | {'context_status':by_context[row['context_id']].get('final_status'),'context_authority':'human' if human(by_context[row['context_id']]) else 'astra_and_saved_sources'} for row in audit]
        dump(stage/'astra_fulltext/review_dispositions.json',dispositions)
        assert all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==checksum for path,checksum in source_hashes.items())
        for path in sorted(stage.rglob('*')):
            if not path.is_file():continue
            relative=path.relative_to(stage);target=ROOT/'combined'/relative
            if target.exists():
                backup=output/'before/combined'/relative;backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,backup)
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
        report['combined_after_sha256']=hashlib.sha256(combined_path.read_bytes()).hexdigest()
    else:
        report['combined_after_sha256']=report['combined_before_sha256']
    if apply and not changed:
        by_context={row['context_id']:row for row in records}
        dump(ROOT/'combined/astra_fulltext/review_dispositions.json',[{key:row.get(key) for key in ('id','applied','reason','follow_up_required')} | {'context_status':by_context[row['context_id']].get('final_status')} for row in audit])
    report['source_packets_verified_unchanged']=all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==checksum for path,checksum in source_hashes.items())
    if not report['source_packets_verified_unchanged']:raise RuntimeError('Original source packet hash changed.')
    dump(output/'application_report.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet',type=Path,default=ROOT/'combined'/common.CHECKPOINT_NAME)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--summary-plots-only',action='store_true')
    args=parser.parse_args()
    if args.summary_plots_only:
        abstract.VERSION=VERSION
        result=abstract.build_summary_figures(args.output)
    else:result=run(args.packet,args.output,args.apply)
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
