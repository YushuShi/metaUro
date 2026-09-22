#!/usr/bin/env python3
"""Import the supplied metaPee manual decisions offline; default is a dry run.

The immutable model packets are identity checked, never rewritten. The reviewed
CSV is preserved verbatim. Apply to a staging checkout, then review its audit.
"""
from __future__ import annotations
import argparse
import copy
import csv
import hashlib
import json
import math
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import astra_fulltext_common as common
from scripts import astra_fulltext_overlay as fulltext
from scripts import apply_astra_adjudication as abstract
from scripts import build_provisional_review_ui as provisional
from scripts import human_review_overlay as human
from scripts import key_comparison_policy as policy
from scripts import reconcile_screening_tracks as reconcile

VERSION = human.VERSION
read = abstract.read
dump = abstract.dump
SOURCE_NAMES = ('chatgpt', 'claude_gemini')
CORRECTIONS = {
    'metaPee-0116': (493179, 'HR', 1.78, 1.05, 3.03),
    'metaPee-0117': (493179, 'HR', 1.36, 1.14, 1.62),
    'metaPee-0157': (646222, 'HR', 0.85, 0.77, 0.93),
    'metaPee-0125': (37442, 'HR', 0.89, 0.62, 1.26),
}
CORRECTION_NOTES = {
    'metaPee-0116': 'sample size 493,179, HR = 1.78 (1.05–3.03)',
    'metaPee-0117': 'sample size 493,179, HR = 1.36 (1.14–1.62)',
    'metaPee-0157': 'Both Incorrect. Sample Size: 646,222. HR = 0.85 (95% CI 0.77–0.93)',
    'metaPee-0125': 'Both Incorrect. Use HR 0.89 (0.62–1.26)',
}
BUNDLE_FIELDS = ('total_n', 'effect_type', 'effect_size', 'ci_lower', 'ci_upper')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bundle(extraction):
    return {key: copy.deepcopy((extraction or {}).get(key)) for key in BUNDLE_FIELDS}


def protected_hashes():
    roots = ['data', 'Cached_results', 'ClaudeGemini_results', 'Disease_hierarchy_results',
             'Subexposure_results', 'outputs/poolability_policy_2026-09-05']
    return {str(path.relative_to(ROOT)): sha(path) for name in roots
            for path in (ROOT / name).rglob('*') if path.is_file() and '__pycache__' not in path.parts}


def classify(text):
    text = text.strip()
    if text == "Use Claude's": return 'select', 'claude_gemini'
    if text == "Use ChatGPT's": return 'select', 'chatgpt'
    if text == 'Duplicated row. Should Remove.': return 'remove_duplicate', None
    if text in {'No estimate available for dairy. Should remove',
                'No estimate available for pooled Processed meat intake. Should remove.'}:
        return 'remove_no_estimate', None
    return 'correct', None


def load_entries(csv_path, tracks, queue):
    rows = list(csv.DictReader(Path(csv_path).open(encoding='utf-8-sig', newline='')))
    by_id = {entry['id']: entry for entry in queue}
    if len({row['Review ID'] for row in rows}) != len(rows):
        raise ValueError('Duplicate review IDs in the input.')
    entries = []
    for row in rows:
        if not row['Manual review results'].strip():
            continue
        rid, cid = row['Review ID'], row['Context ID']
        if rid not in by_id:
            raise ValueError('Review ID not found in frozen queue: ' + rid)
        entry = copy.deepcopy(by_id[rid])
        original = entry['source_row']
        for key in ('Context ID', 'PMID', 'Exposure', 'Disease', 'Endpoint', 'Estimate pair'):
            if row[key] != original.get(key):
                raise ValueError('Review identity differs from queue: ' + rid + ' / ' + key)
        a, b = (tracks[name].get(cid) for name in SOURCE_NAMES)
        pairs = policy.compare_context(a, b)['pairs']
        index = int(row['Estimate pair']) - 1
        if not 0 <= index < len(pairs):
            raise ValueError('Missing source estimate pair: ' + rid)
        provisional.verify_pair(pairs[index], [entry])
        for name, prefix in (('chatgpt', 'ChatGPT'), ('claude_gemini', 'Claude+Gemini')):
            extraction = pairs[index].get(name) or {}
            for key, suffix in (('total_n', 'sample size'), ('effect_size', 'effect size'),
                                ('ci_lower', 'CI lower'), ('ci_upper', 'CI upper')):
                if not common.source_value_equal(row[prefix + ' ' + suffix], extraction.get(key),
                                                  'count' if key == 'total_n' else 'ratio'):
                    raise ValueError('Input source values changed: ' + rid)
            if row[prefix + ' effect type'].strip() != str(extraction.get('effect_type') or '').strip():
                raise ValueError('Input source effect type changed: ' + rid)
        entry.update(manual_review=row['Manual review results'], input_row=row,
                     action=classify(row['Manual review results'])[0],
                     selected_track=classify(row['Manual review results'])[1])
        entries.append(entry)
    return entries


def mark_candidate(source, extraction, pair_index, source_index, entry, csv_sha):
    source = copy.deepcopy(source)
    source.update(manual_review=True, decision_authority='human_manual_review',
                  decision_origin='human', combined_manual_authority=True,
                  manual_review_pending=False, requires_key_review=False)
    source.pop('provisional_ui_candidates', None)
    source.pop('provisional_ui_selection', None)
    item = reconcile.candidate(source, extraction, 'human_manual_review', pair_index,
                               source_index=source_index)
    item = abstract.checked_status(item)
    item['human_review_id'] = entry['id']
    item['human_review_receipt'] = {'id': entry['id'], 'input_sha256': csv_sha,
        'raw_review': entry['manual_review'], 'source_track': entry.get('selected_track'),
        'source_estimate_index': source_index, 'estimate_pair': pair_index + 1}
    item['requires_key_review'] = False
    item.pop('manual_review_pending', None)
    item.pop('provisional_ui_selection', None)
    return item


def corrected_extraction(entry, pair):
    rid = entry['id']
    original = pair.get('chatgpt') or pair.get('claude_gemini') or {}
    extraction = copy.deepcopy(original)
    if rid == 'metaPee-0133':
        if entry['manual_review'].strip() != 'RR=2.06 was lowest vs highest. Should use the reciprocal of it, then recalculate CI.':
            raise ValueError('Unexpected Selenium correction instruction.')
        if not policy.numeric_equal(extraction.get('effect_size'), 2.06):
            raise ValueError('The Selenium source estimate changed.')
        effect, lower, upper = (float(extraction[key]) for key in ('effect_size', 'ci_lower', 'ci_upper'))
        extraction = reconcile.preserve_source_receipts(extraction, original)
        extraction.update(effect_size=1/effect, ci_lower=1/upper, ci_upper=1/lower,
            effect_type='OR', comparison_direction='higher_vs_lower', needs_inversion=False,
            inversion_applied=True, reported_comparison_direction='lower_vs_higher',
            comparison_type='Highest tertile versus lowest tertile of serum selenium',
            comparison_group='Highest serum selenium tertile', reference_group='Lowest serum selenium tertile',
            human_direction_harmonization={'method': 'Reciprocal ratio with reversed reciprocal CI bounds',
                'original_bundle': bundle(original), 'inversion_count': 1})
        return extraction
    if rid not in CORRECTIONS:
        raise ValueError('Unrecognized manual correction: ' + rid)
    if entry['manual_review'].strip() != CORRECTION_NOTES[rid]:
        raise ValueError('Unexpected correction instruction: ' + rid)
    requested = dict(zip(BUNDLE_FIELDS, CORRECTIONS[rid]))
    # These three corrections exactly match an already accepted primary bundle;
    # apply_context consolidates them rather than fabricating male population data.
    if rid != 'metaPee-0125':
        return requested
    if entry['manual_review'].strip() != 'Both Incorrect. Use HR 0.89 (0.62–1.26)':
        raise ValueError('Unexpected Mediterranean-diet correction instruction.')
    # Table 2 provides the unique matching contrast, replacing the old per-SD label.
    source_path = ROOT / 'data/claude_gemini_screening/all_history_through_2026-08-29/verified_pmc_sources/PMC4992047/article.xml'
    source_text = source_path.read_text()
    if not all(value in source_text for value in ('0.89', '0.62', '1.26', 'Q5 (healthy)')):
        raise ValueError('Saved table evidence for the corrected contrast is absent.')
    extraction = reconcile.preserve_source_receipts(extraction, original)
    # Remove calculation/receipt fields tied to the replaced per-SD estimate.
    for key in list(extraction):
        if key.startswith(('combined_p_value', 'reported_effect', 'reported_ci', 'reported_p_value')) or key in {
            'p_value', 'p_value_method', 'p_value_imputed', 'p_source_ci_role', 'p_value_relation',
            'standard_error', 'variance', 'se', 'log_effect', 'log_effect_size', 'unit', 'scale',
            'reported_comparison_direction', 'comparison_labels_imputed', 'direction_imputed'}:
            extraction.pop(key, None)
    extraction.update(requested)
    extraction.update(comparison_type='Mediterranean Diet Score Q5 (healthy) versus Q1 (unhealthy), all UCC',
        comparison_group='Q5 (healthy)', reference_group='Q1 (unhealthy)',
        comparison_direction='higher_vs_lower', needs_inversion=False, gender='Mixed',
        stratum='All urothelial cell carcinoma cases', effect_is_adjusted=True,
        ci_confidence_level=95.0, ci_level=95.0, p_value=0.50,
        p_value_method='Reported two-sided p value for Table 2 MDS Q5 versus Q1, all cases',
        p_value_imputed=False)
    evidence = {'source': 'human_review_with_saved_PMC_table', 'url': 'https://pmc.ncbi.nlm.nih.gov/articles/PMC4992047/',
        'section': 'Table 2, MDS, all cases, Q5 (healthy) versus Q1 (unhealthy)',
        'source_path': str(source_path.relative_to(ROOT)), 'source_sha256': sha(source_path),
        'excerpt': 'Q5 (healthy) | 60 | 0.89 | 0.62 1.26 | 0.50'}
    extraction['evidence'] = copy.deepcopy(extraction.get('evidence') or {})
    for key in ('effect_size', 'confidence_interval', 'comparison', 'p_value'):
        extraction['evidence'][key] = copy.deepcopy(evidence)
    return extraction


def matches(extraction, requested):
    return all(str(extraction.get(key)) == str(value) if key == 'effect_type' else
               policy.numeric_equal(extraction.get(key), value, 'count' if key == 'total_n' else 'ratio')
               for key, value in requested.items())


def synchronize_discrepancy_report(decisions, records):
    """Keep the original comparison rows, marking only reviewed pairs complete."""
    path = ROOT / 'combined/current_discrepancies.csv'
    if not path.exists():
        return 0
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    by_pair = {(row['context_id'], str(row['estimate_pair'])): row for row in decisions}
    statuses = {row['context_id']: row['final_status'] for row in records}
    updated = 0
    for row in rows:
        event = by_pair.get((row['Context ID'], row['Estimate pair']))
        if event is None:
            continue
        row.update({'Manual review results': event['raw_review'], 'Review pending': 'No',
                    'Combined resolution': 'Human manual review: ' + event['action'],
                    'Combined context status': statuses[event['context_id']]})
        updated += 1
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return updated


def apply_context(prior, tracks, entries, csv_sha):
    cid = prior['context_id']
    a, b = (tracks[name][cid] for name in SOURCE_NAMES)
    pairs = policy.compare_context(a, b)['pairs']
    items = copy.deepcopy(fulltext.candidates_for_record(prior, tracks))
    audit = []
    for entry in sorted(entries, key=common.pair_index):
        index = common.pair_index(entry)
        current = [item for item in items if item['pair_index'] == index]
        pair = pairs[index]
        event = {'id': entry['id'], 'context_id': cid, 'estimate_pair': index + 1,
            'action': entry['action'], 'raw_review': entry['manual_review'],
            'selected_track': entry.get('selected_track'), 'applied': True,
            'before': [bundle(item['extraction']) for item in current],
            'source_indices': {name: pair.get(name + '_index') for name in SOURCE_NAMES}}
        items = [item for item in items if item['pair_index'] != index]
        if entry['action'].startswith('remove'):
            event['after'] = []
            if entry['id'] == 'metaPee-0013':
                event['interpretation_note'] = 'Removal applied to the Lactobacillus row identified by the CSV key; its reason says dairy. No other context is affected by that wording.'
        else:
            selected = entry.get('selected_track')
            extraction = copy.deepcopy(pair[selected]) if selected else corrected_extraction(entry, pair)
            if not selected and entry['id'] in {'metaPee-0116', 'metaPee-0117', 'metaPee-0157'}:
                existing = [item for item in items if not item['requires_key_review'] and matches(item['extraction'], extraction)]
                if len(existing) != 1:
                    raise ValueError('Expected one already accepted matching corrected estimate: ' + entry['id'])
                keep = existing[0]
                # Its accepted population metadata is compatible with the exact
                # human-specified bundle; do not reuse the stale reviewed male row.
                updated = mark_candidate(keep['source_record'], keep['extraction'], keep['pair_index'],
                                         keep['source_estimate_index'], entry, csv_sha)
                items[items.index(keep)] = updated
                event.update(action='correct_consolidated_existing', retained_estimate_pair=keep['pair_index'] + 1,
                    after=[bundle(updated['extraction'])],
                    interpretation_note='Human-corrected numeric bundle already exists as an accepted estimate; retained once with its compatible population metadata.')
            else:
                selected = selected or 'chatgpt'
                source_index = pair.get(selected + '_index')
                item = mark_candidate(tracks[selected][cid], extraction, index,
                    source_index if source_index is not None else index, entry, csv_sha)
                items.append(item)
                event['after'] = [bundle(item['extraction'])]
                if entry['id'] == 'metaPee-0133':
                    event['interpretation_note'] = 'Human RR wording interpreted as direction correction; source OR retained. Original full-precision CI endpoints reciprocated once.'
        audit.append(event)
    items.sort(key=lambda item: (item['pair_index'], item['source_estimate_index'], item['source']))
    accepted = [item for item in items if not item['requires_key_review']]
    pending = [item for item in items if item['requires_key_review']]
    result = copy.deepcopy(prior)
    for key in ('provisional_ui_candidates', 'provisional_ui_review_ids', 'provisional_ui_selection'):
        result.pop(key, None)
    result.update(extraction=copy.deepcopy(accepted[0]['extraction']) if accepted else None,
        additional_extractions=[copy.deepcopy(item['extraction']) for item in accepted[1:]],
        combined_accepted_estimate_sources=[abstract.candidate_receipt(item) for item in accepted],
        key_review_candidates=[{k: copy.deepcopy(v) for k, v in item.items() if k != 'source_record'} for item in pending],
        requires_key_review=bool(pending), manual_review_pending=bool(pending),
        combined_remaining_key_review_pairs=sorted({item['pair_index'] for item in pending}),
        combined_numeric_resolved=bool(accepted) and not pending,
        manual_review=True, combined_manual_authority=True, decision_origin='human',
        decision_authority='human_manual_review', combined_decision_authority='human_manual_review',
        human_review_version=VERSION, human_review_input_sha256=csv_sha,
        human_review_history=[*prior.get('human_review_history', []), *audit],
        needs_full_text=bool(pending), astra_fulltext_follow_up_required=bool(pending))
    poolable = any(reconcile.app_row(item)['policy_poolable'] for item in accepted)
    status = 'INCLUDE_META' if poolable else 'ELIGIBLE_NOT_POOLABLE' if items else 'EXCLUDE'
    result.update(eligibility='EXCLUDE' if not items else 'INCLUDE', final_status=status, full_text_status=status,
        poolability_reason='Human manual review applied; existing quantitative, quality, exposure, and clinical criteria retained.' if items else 'Human review removed all estimates in this context.')
    return result, items, audit


def run(csv_path, output, apply=False, summaries=True):
    csv_path, output = Path(csv_path).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()): raise ValueError('Use a new empty audit directory.')
    protected = protected_hashes()
    tracks = {name: {row['context_id']: row for row in read(reconcile.SOURCE_DIR / (name + '_effective_results.json'))['results']} for name in SOURCE_NAMES}
    packet_path = ROOT / 'combined/effective_results.json'
    packet = read(packet_path)
    csv_sha = sha(csv_path)
    ledger_path = ROOT / 'combined' / human.CHECKPOINT
    previous = read(ledger_path) if ledger_path.exists() else {}
    if previous.get('input_sha256') == csv_sha:
        raise ValueError('This exact review input is already applied; verify the saved audit instead of duplicating it.')
    queue = read(ROOT / 'combined/provisional_ui/pending_manifest.json')['rows']
    entries = load_entries(csv_path, tracks, queue)
    grouped = defaultdict(list)
    for entry in entries: grouped[entry['source_row']['Context ID']].append(entry)
    records, items_by_context, decisions = [], {}, []
    before = {}
    for prior in packet['results']:
        cid = prior['context_id']
        if cid not in grouped:
            records.append(prior)
            continue
        before[cid] = copy.deepcopy(prior)
        result, items, audit = apply_context(prior, tracks, grouped[cid], csv_sha)
        records.append(result); items_by_context[cid] = items; decisions.extend(audit)
    if len(decisions) != len(entries): raise ValueError('Some reviews were not mapped.')
    untouched = {row['context_id']: common.digest(row) for row in packet['results'] if row['context_id'] not in grouped}
    assert all(common.digest(row) == untouched[row['context_id']] for row in records if row['context_id'] in untouched)
    report = {'version': VERSION, 'input_path': str(csv_path), 'input_sha256': csv_sha,
        'review_rows': len(entries), 'articles': len({entry['source_row']['PMID'] for entry in entries}),
        'changed_contexts': len(grouped), 'unrelated_contexts_verified_unchanged': len(untouched),
        'decision_counts': dict(Counter(row['action'] for row in decisions)), 'model_calls': 0,
        'decisions': decisions, 'apply_requested': apply}
    dump(output / 'application_report.json', report)
    dump(output / 'before_contexts.json', before)
    dump(output / 'source_hashes_before.json', protected)
    dump(output / 'review_mapping.json', entries)
    dump(output / 'after_contexts.json', {row['context_id']: row for row in records if row['context_id'] in grouped})
    if not apply: return report
    scopes = {reconcile.scope(row) for row in records if row['context_id'] in grouped}
    groups = defaultdict(list)
    for record in records:
        key = reconcile.scope(record)
        if key in scopes:
            groups[key].extend(items_by_context[record['context_id']] if record['context_id'] in items_by_context
                               else fulltext.candidates_for_record(record, tracks))
    previous_audit = reconcile.AUDIT
    try:
        reconcile.AUDIT = output / 'cache_builder_audit'
        report['materialization'] = reconcile.materialize(groups)
    finally:
        reconcile.AUDIT = previous_audit
    if report['materialization']['errors']: raise RuntimeError('Canonical cache materialization failed.')
    from scripts.rebuild_relaxed_poolability import cache_paths
    for scope, items in groups.items():
        if items: continue
        exposure, disease, endpoint = scope
        empty = dict(canonical_track='combined', exposure=exposure, disease=disease, cancer=disease,
            endpoint=endpoint, outcome=endpoint, studies=[], headline=None, plot_url=None,
            funnel_plot_url=None, baujat_plot_url=None, requires_key_review=False)
        for original in cache_paths(*scope): dump(ROOT / 'combined' / original.relative_to(ROOT / 'Cached_results'), empty)
    snapshots = copy.deepcopy(previous.get('record_snapshots') or {})
    for record in records:
        cid = record['context_id']
        if cid in grouped:
            snapshots[cid] = {'record': record, 'record_sha256': common.digest(record),
                'before_record_sha256': common.digest(before[cid]),
                'source_fingerprint': common.source_digest(tracks['chatgpt'][cid], tracks['claude_gemini'][cid])}
    destination = ROOT / 'combined/human_review'
    reviewed_name = 'metaPee_human_manual_review_2026-09-08_both_nonpoolable_reviewed_2026-09-14.csv'
    shutil.copy2(csv_path, destination / reviewed_name)
    ledger = {'version': VERSION, 'decision_authority': 'human_manual_review',
        'input_file': reviewed_name, 'input_sha256': csv_sha, 'source_hashes': {name: sha(reconcile.SOURCE_DIR / (name + '_effective_results.json')) for name in SOURCE_NAMES},
        'decisions': [*(previous.get('decisions') or []), *decisions], 'record_snapshots': snapshots}
    dump(ledger_path, ledger)
    dump(packet_path, {**packet, 'results': records, 'human_review_version': VERSION})
    report['comparison_report_rows_resolved'] = synchronize_discrepancy_report(decisions, records)
    dump(destination / 'application_audit_2026-09-14.json', report)
    remaining = [entry for entry in queue if entry['id'] not in {row['id'] for row in decisions}]
    dump(ROOT / 'combined/provisional_ui/pending_manifest.json',
         {'version': provisional.provisional.VERSION, 'rows': remaining})
    status = read(destination / 'review_status.json')
    status.update(status='Human review complete' if not remaining else 'Human review partially complete',
        review_rows=len(remaining), articles=len({entry['source_row']['PMID'] for entry in remaining}),
        completed_review_rows=len(decisions), completed_articles=report['articles'],
        file=reviewed_name, csv_sha256=csv_sha, applied_decisions_file='current_decisions.json',
        human_review_version=VERSION, temporary_pooling_rows=0 if not remaining else status.get('temporary_pooling_rows'),
        temporary_pipeline_counts={} if not remaining else status.get('temporary_pipeline_counts'))
    dump(destination / 'review_status.json', status)
    report['canonical_subexposure_modes'] = abstract.build_combined_modes(
        (ROOT / 'combined').glob('*/*_true_all.json'), ROOT / 'combined/subexposure_views')
    if summaries:
        report['canonical_summaries'] = abstract.build_summary_figures(output / 'canonical_summary_refresh')
        from scripts.build_summary_plots import canonical_asset_path
        for mode in ('yes', 'no'):
            summary_path = canonical_asset_path(
                ROOT, ROOT / 'combined/subexposure_views' / mode / 'Plot/summary_manifest.json')
            summary = read(summary_path)
            summary['human_review_version'] = VERSION
            dump(summary_path, summary)
    # Build the actual serving view via the same established pipeline, including
    # disease-parent projections and its protected-source manifest.
    report['provisional'] = provisional.build(output / 'provisional_refresh', apply=True, summary=summaries)
    report['protected_source_files_verified_unchanged'] = len(protected)
    changed = [name for name, digest in protected.items() if not (ROOT / name).exists() or sha(ROOT / name) != digest]
    if changed: raise RuntimeError('Protected original evidence changed: ' + ', '.join(changed))
    report['unrelated_contexts_verified_unchanged'] = len(untouched)
    dump(output / 'application_report.json', report)
    dump(destination / 'application_audit_2026-09-14.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--skip-summary', action='store_true')
    args = parser.parse_args()
    result = run(args.csv, args.output, args.apply, not args.skip_summary)
    print(json.dumps({key: value for key, value in result.items() if key != 'decisions'}, indent=2))
