#!/usr/bin/env python3
"""Dry-run or apply validated PMC-text Astra corrections to combined only."""
from __future__ import annotations
import argparse
import copy
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import astra_fulltext_overlay as overlay
from scripts import astra_fulltext_common as common
from scripts import apply_astra_adjudication as original_adapter


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str) + '\n')


def prepare(packet_path):
    source = read(packet_path)
    entries = common.packet_entries(source)
    packet, records, tracks, protected = original_adapter.inputs()
    protected.update(cid for cid, row in records.items() if common.is_human(row))
    for track in tracks.values():
        protected.update(cid for cid, row in track.items() if common.is_human(row))
    source_hashes = {track: overlay.digest(rows) for track, rows in tracks.items()}
    updated, audit = overlay.apply_entries(records, tracks, entries, protected)
    if ROOT.name == 'metaFemina':
        updated = overlay.normalize_femina(updated, audit)
    else:
        from scripts import build_combined_results as builder
        _, monthly = builder._load_project_modules()
        # Limit dry-run poolability normalization to the changed contexts.
        changed = {cid: updated[cid] for cid in {x['context_id'] for x in audit if x.get('applied')}}
        builder.reclassify_poolability(changed, monthly)
    for cid in protected:
        if cid in records and updated.get(cid) != records[cid]:
            raise AssertionError('Human review changed: ' + cid)
    checkpoint_path = ROOT / 'combined' / overlay.CHECKPOINT_NAME
    prior = read(checkpoint_path) if checkpoint_path.exists() else None
    checkpoint = overlay.snapshot_checkpoint(updated, tracks, entries, audit, prior)
    checkpoint.update(source_packet=str(Path(packet_path).resolve()), source_packet_sha256=overlay.digest(source))
    proposed = copy.deepcopy(packet)
    proposed['results'] = [updated[cid] for cid in sorted(updated)]
    report = {'repository': ROOT.name, 'version': overlay.VERSION,
              'model': 'gpt-6-astra', 'source_scope': overlay.SOURCE_SCOPE,
              'decisions': len(entries), 'applied_estimates': sum(x['applied'] for x in audit),
              'applied_contexts': len({x['context_id'] for x in audit if x['applied']}),
              'retained_for_review': sum(x['follow_up_required'] for x in audit),
              'protected_human_contexts': len(protected),
              'reason_counts': dict(Counter(x['reason'] for x in audit)),
              'original_tracks_unchanged': source_hashes == {track: overlay.digest(rows) for track, rows in tracks.items()},
              'source_sha256': source_hashes, 'records': audit, 'model_calls': 0}
    return checkpoint, proposed, report


def apply(checkpoint, report, output, skip_figures=False):
    combined = ROOT / 'combined'
    # Each application can roll back to its immediate predecessor, including
    # when a second packet is applied after an earlier successful full-text run.
    backup = output / 'before_application' / overlay.digest(read(combined / 'final_screening.json'))[:16] / 'combined'
    if not backup.exists():
        shutil.copytree(combined, backup)
    destination = combined / overlay.CHECKPOINT_NAME
    write(destination, checkpoint)
    try:
        if ROOT.name == 'metaFemina':
            from scripts import recompare_current_effective_results as comparison
            current = comparison.load_inputs()
            result = comparison.prepare_comparison(current)
            comparison.write_reports(*result)
            comparison.publish(result[0], result[-1], current, rebuild_figures=not skip_figures)
            if skip_figures:
                from scripts import subexposure_views
                subexposure_views.build(generate_plots=False)
        else:
            from scripts import build_combined_results as builder
            builder.build(builder.parse_args(['--skip-plots'] if skip_figures else []))
            from scripts import rebuild_subexposure_results
            rebuild_subexposure_results.build(skip_summary=skip_figures)
        _, installed, tracks, protected = original_adapter.inputs()
        if report['source_sha256'] != {track: overlay.digest(rows) for track, rows in tracks.items()}:
            raise AssertionError('A screening source changed during combined publication.')
        previous = {row['context_id']: row for row in read(backup / 'final_screening.json')['results']}
        for cid in protected:
            if cid not in previous or cid not in installed:
                continue
            fields = ('extraction', 'eligibility', 'final_status', 'full_text_status', 'jbi',
                      'human_adjudication', 'manual_review', 'manual_full_text_adjudication',
                      'decision_authority', 'manual_adjudication')
            if any(previous[cid].get(field) != installed[cid].get(field) for field in fields):
                raise AssertionError('Publication changed a protected human scientific decision: ' + cid)
        for cid, snapshot in checkpoint['snapshots'].items():
            if cid in protected:
                continue
            current = installed.get(cid) or {}
            if not current.get('astra_fulltext_adjudication'):
                raise AssertionError('Full-text checkpoint was not replayed: ' + cid)
            if policy_keys(current) != policy_keys(snapshot['record']):
                raise AssertionError('Publication changed the adjudicated numeric bundle: ' + cid)
    except BaseException:
        failed = output / 'failed_application_combined'
        if failed.exists():
            shutil.rmtree(failed)
        shutil.move(str(combined), failed)
        shutil.copytree(backup, combined)
        raise
    dispositions = []
    for event in report['records']:
        row = installed.get(event['context_id']) or {}
        dispositions.append({**event, 'context_status': row.get('final_status'),
                             'context_authority': overlay.authority(row)})
    write(combined / 'astra_fulltext/application_audit.json', report)
    write(combined / 'astra_fulltext/review_dispositions.json', dispositions)
    write_readme_phase(combined, report, dispositions)
    write(output / 'installation.json', {'applied': True, 'source_tracks_unchanged': True,
          'protected_human_decisions_verified_unchanged': len(protected),
          'checkpoint_retained': destination.exists(), 'model_calls': 0,
          'contexts_with_fulltext_metadata': sum(bool(row.get('astra_fulltext_adjudication')) for row in installed.values()),
          'counts': dict(Counter(row.get('final_status') for row in installed.values()))})


def write_readme_phase(combined, report, dispositions):
    """Describe this bounded authorized phase without changing historical audit."""
    path = combined / 'README.md'
    begin, end = '<!-- ASTRA_PMC_FULLTEXT_PHASE_BEGIN -->', '<!-- ASTRA_PMC_FULLTEXT_PHASE_END -->'
    previous = path.read_text() if path.exists() else '# Combined results\n'
    if begin in previous and end in previous:
        left, rest = previous.split(begin, 1)
        _, right = rest.split(end, 1)
        previous = left.rstrip() + '\n' + right.lstrip()
    applied = sum(item.get('applied') is True for item in dispositions)
    remaining = sum(item.get('follow_up_required') is True for item in dispositions)
    phase = f'''{begin}

## Authorized Astra PMC full-text adjudication — 2026-09-07

This subsequent phase uses the user's permission for GPT-6 Astra to review article text available through PMCID. PDF-only articles are skipped. The earlier Fable and title/abstract audit artifacts remain historical records of their respective phases.

This bounded pass reviewed {len(dispositions)} estimate-level items: {applied} supported corrections were applied across {report['applied_contexts']} contexts, and {remaining} items still require manual or quantitative follow-up. Follow-up counts include supported corrections whose quantitative inputs remain incomplete.

The durable checkpoint is [astra_fulltext/current_decisions.json](astra_fulltext/current_decisions.json). Per-item outcomes and remaining review status are in [astra_fulltext/review_dispositions.json](astra_fulltext/review_dispositions.json), with application validation in [astra_fulltext/application_audit.json](astra_fulltext/application_audit.json).

Human decisions and the two original screening tracks remain unchanged. Each saved correction is bound to the source-track fingerprints and verified PMC text checksum. Future rebuilds preserve the correction only while its source tracks remain unchanged; human decisions take precedence. Existing JBI, scientific, and overlap restrictions remain effective, and incomplete or non-ratio results are displayed without contributing to pooling.

{end}
'''
    path.write_text(previous.rstrip() + '\n\n' + phase)


def policy_keys(record):
    from scripts import key_comparison_policy as policy
    return [{key: item.get(key) for key in (*policy.KEY_FIELDS, 'effect_type')}
            for item in policy.extracts(record)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/astra_fulltext_2026-09-07')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--skip-figures', action='store_true')
    args = parser.parse_args()
    checkpoint, proposed, report = prepare(args.packet)
    write(args.output / 'proposed_checkpoint.json', checkpoint)
    write(args.output / 'proposed_final_screening.json', proposed)
    write(args.output / 'application_audit.json', report)
    print(json.dumps({key: value for key, value in report.items() if key != 'records'}, indent=2), flush=True)
    if args.apply:
        apply(checkpoint, report, args.output, args.skip_figures)
        print('Combined full-text checkpoint and derived results updated.', flush=True)


if __name__ == '__main__':
    main()
