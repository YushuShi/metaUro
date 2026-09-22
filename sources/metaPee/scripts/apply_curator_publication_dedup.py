#!/usr/bin/env python3
"""Apply the user-authorized, scope-specific publication choice offline.

Run in a staging checkout; the default is a dry run. Original source packets
and the preceding human-review receipts are preserved and verified by hash.
"""
from __future__ import annotations
import argparse
import copy
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import apply_human_manual_review as prior_review
from scripts import apply_astra_adjudication as abstract
from scripts import astra_fulltext_common as common
from scripts import astra_fulltext_overlay as fulltext
from scripts import build_provisional_review_ui as provisional
from scripts import curator_dedup_overlay as overlay
from scripts import reconcile_screening_tracks as reconcile

read, dump, sha = abstract.read, abstract.dump, prior_review.sha
DECISION = {
    'id': 'metaPee-curator-dedup-2026-09-14-001',
    'action': 'exclude_duplicate_publication',
    'decision_authority': 'human_curator_dedup',
    'raw_user_instruction': 'keep only\n\nPMID: 15514967\n\nPMID: 25866035\n\nPMID: 39417685\n\nPMID: 19066370\n\nfor those exposure/disease combinations',
    'interpretation': 'Keep PMID 25866035 and remove duplicate PMID 28159493 from Alcohol / Kidney and renal pelvis cancer / Incidence only, including inherited rows in that displayed scope.',
    'audit_reference': 'reports/duplicate_sample_size_effect_2026-09-14/possible_shared_datasets.md',
    'exposure': 'Alcohol', 'disease': 'Kidney and renal pelvis cancer', 'endpoint': 'Incidence',
    'retained_pmid': '25866035', 'rejected_pmid': '28159493',
    'applies_to_related_exposure_modes': ['yes', 'no'],
}
TARGET = '28159493|Alcohol|Kidney and renal pelvis cancer'
RETAINED = '25866035|Alcohol|Kidney and renal pelvis cancer'


def excluded_record(prior):
    result = copy.deepcopy(prior)
    result.update(extraction=None, additional_extractions=[], key_review_candidates=[],
        combined_accepted_estimate_sources=[], combined_remaining_key_review_pairs=[],
        eligibility='EXCLUDE', final_status='EXCLUDE', full_text_status='EXCLUDE',
        manual_review=True, combined_manual_authority=True, decision_origin='human',
        decision_authority='human_curator_dedup', combined_decision_authority='human_curator_dedup',
        manual_poolable=False, requires_key_review=False, manual_review_pending=False,
        needs_full_text=False, astra_fulltext_follow_up_required=False,
        combined_numeric_resolved=False,
        curator_dedup_version=overlay.VERSION, curator_dedup_decision_id=DECISION['id'],
        poolability_reason='Curator retained PMID 25866035 and excluded duplicate publication PMID 28159493 in this exposure/disease context.',
        curator_dedup_history=[*prior.get('curator_dedup_history', []), copy.deepcopy(DECISION)])
    for key in ('provisional_ui_candidates', 'provisional_ui_selection', 'provisional_ui_review_ids'):
        result.pop(key, None)
    return result


def protected_hashes():
    result = prior_review.protected_hashes()
    for path in (ROOT / 'combined/human_review').rglob('*'):
        if path.is_file():
            result[str(path.relative_to(ROOT))] = sha(path)
    return result


def preserve_unaffected_outputs(before_root, combined_root):
    """Retain unchanged disease figures and prove unrelated cache scopes stable."""
    target_key = 'kidney_and_renal_pelvis_cancer'
    target_scope = ('Alcohol', 'Kidney and renal pelvis cancer', 'Incidence')
    checked, restored = 0, 0
    for before_path in before_root.rglob('*_true_*.json'):
        relative = before_path.relative_to(before_root)
        current_path = combined_root / relative
        if not current_path.exists():
            raise ValueError('Previously saved cache disappeared: ' + str(relative))
        prior = read(before_path)
        scope = (prior.get('exposure'), prior.get('disease') or prior.get('cancer'),
                 prior.get('endpoint') or prior.get('outcome') or 'Incidence')
        if scope == target_scope:
            continue
        if before_path.read_bytes() != current_path.read_bytes():
            raise ValueError('Unrelated saved cache changed: ' + str(relative))
        checked += 1
    from scripts import build_summary_plots as summary_builder
    if summary_builder.METAAURO_ROOT is not None:
        # metaUro publishes one verified summary tree; never restore legacy
        # copies from a pre-review backup into the embedded evidence folders.
        return {'unrelated_cache_files_verified_byte_identical': checked,
                'unrelated_summary_artifacts_preserved': 0}
    for prefix in ('subexposure_views', 'provisional_ui/subexposure_views'):
        for mode in ('yes', 'no'):
            relative = Path(prefix) / mode / 'Plot'
            previous, current = before_root / relative, combined_root / relative
            old_manifest = read(previous / 'summary_manifest.json')
            new_manifest = read(current / 'summary_manifest.json')
            for key, scope in old_manifest['scopes'].items():
                if key != target_key and new_manifest['scopes'].get(key) != scope:
                    raise ValueError('Unrelated summary scope changed: ' + key)
            for path in previous.rglob('*'):
                if path.suffix not in {'.pdf', '.xlsx'} or target_key in path.name:
                    continue
                destination = current / path.relative_to(previous)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                restored += 1
            if old_manifest.get('human_review_version'):
                new_manifest['human_review_version'] = old_manifest['human_review_version']
            new_manifest['curator_dedup_version'] = overlay.VERSION
            dump(current / 'summary_manifest.json', new_manifest)
    return {'unrelated_cache_files_verified_byte_identical': checked,
            'unrelated_summary_artifacts_preserved': restored}


def verify_target_views(before_root, combined_root):
    checked = {}
    for mode in ('yes', 'no'):
        relative = Path('provisional_ui/subexposure_views') / mode / 'Cached_results/alcohol/kidney_and_renal_pelvis_cancer_incidence_true_core.json'
        before, after = read(before_root / relative), read(combined_root / relative)
        expected = [row for row in before['studies'] if str(row.get('PMID')) != DECISION['rejected_pmid']]
        if after['studies'] != expected:
            raise ValueError('Target view changed beyond the authorized publication exclusion: ' + mode)
        checked[mode] = {'visible_rows_before': len(before['studies']), 'visible_rows_after': len(after['studies']),
            'removed_rows': len(before['studies']) - len(after['studies']),
            'all_other_rows_preserved_exactly': True,
            'default_analyzed_estimates_before': len(before['headline']['loo_results']),
            'default_analyzed_estimates_after': len(after['headline']['loo_results'])}
    return checked


def run(output, apply=False, summaries=True):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('Use a new empty audit directory.')
    combined = ROOT / 'combined'
    packet = read(combined / 'effective_results.json')
    before = {record['context_id']: record for record in packet['results']}
    ledger_path = combined / overlay.CHECKPOINT
    previous = overlay.checkpoint(combined)
    if previous:
        if previous.get('decisions') != [DECISION]:
            raise ValueError('Different curator decisions already exist; explicitly reconcile them.')
        saved = previous['record_snapshots'][TARGET]
        if common.digest(before[TARGET]) != saved['record_sha256'] or common.digest(before[RETAINED]) != previous['retained_record_sha256']:
            raise ValueError('Previously applied curator choice no longer matches canonical evidence.')
        report = {'version': overlay.VERSION, 'already_applied': True, 'changed_contexts': 0, 'writes_to_source': 0}
        dump(output / 'application_report.json', report)
        return report
    protected = protected_hashes()
    tracks = {name: {row['context_id']: row for row in read(reconcile.SOURCE_DIR / (name + '_effective_results.json'))['results']}
              for name in ('chatgpt', 'claude_gemini')}
    expected = dict(total_n=477325, effect_type='HR', effect_size=.91, ci_lower=.63, ci_upper=1.3)
    if any(prior_review.bundle(before[cid]['extraction']) != expected for cid in (TARGET, RETAINED)):
        raise ValueError('The authorized duplicate or retained numeric bundle changed.')
    after_target = excluded_record(before[TARGET])
    records = [after_target if record['context_id'] == TARGET else record for record in packet['results']]
    unrelated = {cid: common.digest(record) for cid, record in before.items() if cid != TARGET}
    report = {'version': overlay.VERSION, 'apply_requested': apply, 'decision': DECISION,
        'changed_contexts': 1, 'changed_context_ids': [TARGET], 'canonical_contexts': len(records),
        'unrelated_contexts_verified_unchanged': len(unrelated),
        'retained_record_sha256': common.digest(before[RETAINED]),
        'before_record_sha256': common.digest(before[TARGET]), 'after_record_sha256': common.digest(after_target),
        'protected_source_and_prior_review_files': len(protected), 'model_calls': 0}
    dump(output / 'source_hashes_before.json', protected)
    dump(output / 'unrelated_context_sha256.json', unrelated)
    dump(output / 'application_report.json', report)
    if not apply:
        return report
    shutil.copytree(combined, output / 'combined_before')
    audit_ref = ROOT.parents[1] / DECISION['audit_reference']
    ledger = {'version': overlay.VERSION, 'decision_authority': 'human_curator_dedup',
        'decisions': [DECISION], 'decisions_sha256': common.digest([DECISION]),
        'audit_reference_sha256': sha(audit_ref) if audit_ref.exists() else None,
        'prior_human_checkpoint_sha256': sha(combined / 'human_review/current_decisions.json'),
        'retained_context_id': RETAINED, 'retained_record_sha256': common.digest(before[RETAINED]),
        'record_snapshots': {TARGET: {'record': after_target, 'record_sha256': common.digest(after_target),
            'before_record': before[TARGET], 'before_record_sha256': common.digest(before[TARGET]),
            'source_fingerprint': common.source_digest(tracks['chatgpt'].get(TARGET), tracks['claude_gemini'].get(TARGET))}}}
    dump(ledger_path, ledger)
    dump(combined / 'effective_results.json', {**packet, 'results': records, 'curator_dedup_version': overlay.VERSION})
    groups = defaultdict(list)
    changed_scope = reconcile.scope(after_target)
    for record in records:
        if reconcile.scope(record) == changed_scope:
            groups[changed_scope].extend(fulltext.candidates_for_record(record, tracks))
    old_audit = reconcile.AUDIT
    try:
        reconcile.AUDIT = output / 'canonical_cache_refresh'
        report['materialization'] = reconcile.materialize(groups)
    finally:
        reconcile.AUDIT = old_audit
    if report['materialization']['errors']:
        raise RuntimeError('Canonical cache materialization failed.')
    report['canonical_subexposure_modes'] = abstract.build_combined_modes(
        combined.glob('*/*_true_all.json'), combined / 'subexposure_views')
    if summaries:
        report['canonical_summaries'] = abstract.build_summary_figures(output / 'canonical_summary_refresh')
    report['provisional'] = provisional.build(output / 'provisional_refresh', apply=True, summary=summaries)
    if summaries:
        report['unaffected_outputs'] = preserve_unaffected_outputs(output / 'combined_before', combined)
    report['target_views_verified'] = verify_target_views(output / 'combined_before', combined)
    current = {record['context_id']: record for record in read(combined / 'effective_results.json')['results']}
    assert all(common.digest(current[cid]) == value for cid, value in unrelated.items())
    changed = [name for name, value in protected.items() if not (ROOT / name).exists() or sha(ROOT / name) != value]
    if changed:
        raise RuntimeError('Protected original evidence or historical human receipts changed: ' + ', '.join(changed))
    report['protected_source_and_prior_review_files_verified_unchanged'] = len(protected)
    report['unrelated_contexts_verified_unchanged'] = len(unrelated)
    dump(output / 'application_report.json', report)
    dump(combined / 'curator_dedup/application_audit_2026-09-14.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--skip-summary', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.output, args.apply, not args.skip_summary), indent=2))
