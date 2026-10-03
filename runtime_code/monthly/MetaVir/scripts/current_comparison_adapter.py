"""Current cross-model evidence inputs and historical decision lineage for MetaVir.

The original model packets and previous Fable/manual decisions stay immutable.
Effective current track rows include the subsequently approved extraction rules.
"""
from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_combined_results as builder

CURRENT_EFFECTIVE_ROOT = ROOT / 'outputs/fair_display_2026-09-05'
CURRENT_OUTPUT_ROOT = ROOT / 'outputs/current_comparison_2026-09-05'


def current_tracks(args=None):
    """Return full selected tracks, overlaying the current effective evidence."""
    args = args or builder.parse_args([])
    chat, selected, source_packets = builder.selected_chatgpt_lineage(args.monthly_root, args.parallel_root)
    other = builder.claude_gemini_lineage(args.parallel_root)
    tracks = {'CHATGPT': copy.deepcopy(chat), 'CLAUDE_GEMINI': copy.deepcopy(other)}
    effective_paths = {}
    for track in tracks:
        path = CURRENT_EFFECTIVE_ROOT / (track.lower() + '_poolability.json')
        if not path.exists():
            raise ValueError('Current effective evidence is missing: ' + str(path))
        _, effective = builder.load_result_map(path)
        unexpected = set(effective) - set(tracks[track])
        if unexpected:
            raise ValueError(f'{track} effective rows are outside the selected lineage: {sorted(unexpected)[:3]}')
        tracks[track].update(copy.deepcopy(effective))
        effective_paths[track] = path
    return tracks, selected, source_packets, effective_paths


def historical_final_records(args=None, monthly=None):
    """Reconstruct exactly the already saved Fable/manual final decisions."""
    args = args or builder.parse_args([])
    if monthly is None:
        _, monthly = builder._load_project_modules()
    chat, selected, source_packets = builder.selected_chatgpt_lineage(args.monthly_root, args.parallel_root)
    other = builder.claude_gemini_lineage(args.parallel_root)
    fable_rows = builder.load_fable_rows(args.adjudication_csv)
    manual_packet = builder.load_manual_overrides(args.output_root / '_manual_overrides.json')
    overrides = builder.manual_override_map(manual_packet, fable_rows)
    combined, ledger = builder.apply_fable_adjudications(chat, other, fable_rows, overrides)
    poolability_audit = builder.reclassify_poolability(combined, monthly)
    return combined, ledger, poolability_audit, fable_rows, manual_packet


def historical_field_keys(fable_rows):
    """Historical adjudication universe; CI and p shared one review bundle."""
    return {(row['context_id'], row['discrepant_field']) for row in fable_rows}


def is_retained(row, track):
    return builder.normalized_status(row, claude_gemini=(track == 'CLAUDE_GEMINI')) != 'EXCLUDE'


def shared_scope_summary(tracks):
    chat, other = tracks['CHATGPT'], tracks['CLAUDE_GEMINI']
    missing = set(other) - set(chat)
    if missing:
        raise ValueError('Claude+Gemini reviewed contexts without selected ChatGPT evidence: ' + str(len(missing)))
    matrix = Counter()
    for context in other:
        matrix[('include' if is_retained(chat[context], 'CHATGPT') else 'exclude') + '|' +
               ('include' if is_retained(other[context], 'CLAUDE_GEMINI') else 'exclude')] += 1
    return {'shared_reviewed_contexts': len(other), 'status_matrix': dict(matrix)}


def _manual(record):
    return bool(record.get('manual_review') or record.get('manual_full_text_adjudication'))


def _merge_metadata(preferred, donors):
    from scripts import key_comparison_policy as policy
    result = copy.deepcopy(preferred)
    fills = []
    for track, donor in donors:
        if not isinstance(donor, dict):
            continue
        result, root_fills = policy.merge_missing_fields(result, donor,
            protected_fields=('jbi', 'manual_review', 'manual_full_text_adjudication', 'combined_adjudicated_fields',
                              'reason', 'exclusion_code', 'screen_layer', 'needs_full_text', 'full_text_eligible'))
        fills.extend(dict(fill, source=track) for fill in root_fills)
        if policy.missing(result.get('jbi')) and not policy.missing(donor.get('jbi')):
            result['jbi'] = copy.deepcopy(donor['jbi'])
            fills.append({'field': 'jbi', 'value': copy.deepcopy(donor['jbi']), 'source': track,
                          'method': 'Copied the complete assessment; no item-by-item cross-model rating was assembled.'})
        if isinstance(result.get('extraction'), dict) and isinstance(donor.get('extraction'), dict):
            result['extraction'], extract_fills = policy.merge_missing_fields(result['extraction'], donor['extraction'])
            fills.extend(dict(fill, field='extraction.' + fill['field'], source=track) for fill in extract_fills)
    return result, fills


def _current_fields(pair, screening_difference):
    if screening_difference:
        return {'SCREENING_DECISION'}
    mapping = {'total_n': 'SAMPLE_SIZE', 'effect_size': 'EFFECT_SIZE', 'ci': 'CI_OR_P_VALUE'}
    return {mapping[field] for field in pair['differences']}


def prepare(args, monthly):
    """Compare current tracks and reuse only historically covered final decisions."""
    from scripts import key_comparison_policy as policy
    tracks, selected, source_packets, effective_paths = current_tracks(args)
    prior, prior_ledger, prior_poolability, fable_rows, manual_packet = historical_final_records(args, monthly)
    historical_keys = historical_field_keys(fable_rows)
    historical_contexts = {context for context, _ in historical_keys}
    prior_ledger_map = {(row['context_id'], row['discrepant_field']): row for row in prior_ledger}
    combined = copy.deepcopy(prior)
    ledger = []
    comparison_rows = []
    fill_rows = []
    p_rows = []
    current_keys = set()
    reused_contexts = set()
    new_pending = set()
    key_agreements = 0
    per_context = {}
    for context in sorted(tracks['CLAUDE_GEMINI']):
        chat, other = tracks['CHATGPT'][context], tracks['CLAUDE_GEMINI'][context]
        from scripts import nonpoolable_status_retention as retention
        resolution = policy.nonpoolable_status_resolution(chat, other)
        if resolution:
            retained = retention.resolve_record(chat, other, prior.get(context))
            if retained is None:
                combined[context] = copy.deepcopy(prior[context])
                per_context[context] = {'action': 'retained_manual_final', 'current_fields': []}
            else:
                combined[context] = retained
                per_context[context] = retained['key_comparison']
                ledger.append({'PMID': str(retained.get('pmid') or ''), 'exposure': retained.get('exposure'),
                    'cancer': retained.get('cancer'), 'context_id': context,
                    'discrepant_field': 'SCREENING_DECISION', 'discrepancy_type': 'SCREENING',
                    'merge_action': 'retained_eligible_not_poolable', 'pending_reason': '',
                    'combined_value': 'ELIGIBLE_NOT_POOLABLE',
                    'nonpoolable_status_resolution': retained['nonpoolable_status_resolution']})
            continue
        comparison = policy.compare_context(chat, other)
        if comparison['both_excluded']:
            continue
        if len(comparison['pairs']) != 1:
            raise ValueError('MetaVir current records require explicit handling of multiple estimates: ' + context)
        pair = comparison['pairs'][0]
        screening_difference = is_retained(chat, 'CHATGPT') != is_retained(other, 'CLAUDE_GEMINI')
        fields = _current_fields(pair, screening_difference)
        keys = {(context, field) for field in fields}
        current_keys.update(keys)
        uncovered = keys - historical_keys
        manual = _manual(chat) or _manual(other)
        if manual:
            preferred = chat if _manual(chat) else other
            action = 'retained_manual_final'
        elif fields and uncovered:
            preferred = prior.get(context, other)
            action = 'unresolved_new_key_difference'
        elif fields:
            preferred = prior[context]
            action = 'reused_prior_fable_combined_final'
            reused_contexts.add(context)
        else:
            preferred = prior.get(context, other)
            action = 'retained_prior_combined_key_agreement' if context in prior else 'claude_gemini_key_agreement'
            key_agreements += 1
        donors = [('CLAUDE_GEMINI', other), ('CHATGPT', chat)]
        result, fills = _merge_metadata(preferred, donors)
        for fill in fills:
            fill_rows.append(dict(fill, context_id=context, PMID=str(result.get('pmid') or ''), exposure=result.get('exposure')))
        if uncovered and not manual:
            new_pending.add(context)
            result.update(final_status='ELIGIBLE_NOT_POOLABLE', full_text_status='ELIGIBLE_NOT_POOLABLE',
                key_comparison_pending=True, manual_poolable=False, policy_poolable=False,
                key_comparison_candidates=[dict(copy.deepcopy(chat), comparison_candidate_track='CHATGPT'),
                                          dict(copy.deepcopy(other), comparison_candidate_track='CLAUDE_GEMINI')])
        if pair['key_agreement'] and isinstance(result.get('extraction'), dict):
            # The preferred final bundle supplies the CI width; no direction inversion is introduced.
            resolution = policy.resolve_p_value(pair['chatgpt'], pair['claude_gemini'], result['extraction'])
            if resolution['needed']:
                p_rows.append(dict(resolution, context_id=context, PMID=str(result.get('pmid') or ''), exposure=result.get('exposure'),
                    chatgpt_p_value=(pair['chatgpt'] or {}).get('p_value'), claude_gemini_p_value=(pair['claude_gemini'] or {}).get('p_value')))
                result['key_comparison_p_resolution'] = copy.deepcopy(resolution)
                if resolution['resolution'] == 'computed_two_sided_wald':
                    extraction = result['extraction']
                    extraction['cross_model_original_p_value'] = extraction.get('p_value')
                    extraction.update(p_value=resolution['combined_p_value'], p_value_relation='=', p_value_is_exact=True,
                        p_value_imputed=True, p_value_method=monthly.dual_screening.WALD_P_VALUE_METHOD,
                        key_comparison_p_resolution=copy.deepcopy(resolution))
        result['key_comparison'] = {'policy_version': policy.VERSION, 'action': action, 'current_fields': sorted(fields),
                                   'historical_field_coverage': not uncovered, 'missing_nonkey_fields_filled': len(fills)}
        combined[context] = result
        per_context[context] = result['key_comparison']
        for field in sorted(fields):
            old = prior_ledger_map.get((context, field))
            if old and action != 'unresolved_new_key_difference':
                entry = copy.deepcopy(old)
                entry.update(reused_prior_final_result=True, comparison_policy_version=policy.VERSION)
            else:
                entry = {'PMID': str(result.get('pmid') or ''), 'exposure': result.get('exposure'),
                    'cancer': result.get('cancer'), 'context_id': context, 'discrepant_field': field,
                    'discrepancy_type': 'SCREENING' if field == 'SCREENING_DECISION' else 'EXTRACTION',
                    'merge_action': 'retained_manual_final' if manual else 'pending_manual_review',
                    'pending_reason': '' if manual else 'A current key difference lacks complete historical field coverage; both source candidates are retained unpooled.',
                    'combined_value': builder.value_for_field(result, field), 'new_key_difference': True}
            if manual:
                entry['merge_action'] = 'retained_manual_final'
                entry['pending_reason'] = ''
                entry['manual_final_precedence'] = True
            if action == 'unresolved_new_key_difference':
                entry['merge_action'] = 'pending_manual_review'
                entry['pending_reason'] = 'The estimate has a newly uncovered key difference; both source candidates are retained unpooled.'
            ledger.append(entry)
        if fields:
            left, right = pair['chatgpt'] or {}, pair['claude_gemini'] or {}
            comparison_rows.append({'repo': 'MetaVir', 'PMID': str(chat.get('pmid') or other.get('pmid') or ''),
                'first_author': str((chat.get('authors') or other.get('authors') or [''])[0]),
                'exposure': str(chat.get('exposure') or other.get('exposure') or ''), 'disease': 'Prostate cancer',
                'context_id': context, 'discrepant_fields': '; '.join(sorted(fields)),
                'chatgpt_status': builder.normalized_status(chat),
                'claude_gemini_status': builder.normalized_status(other, claude_gemini=True),
                'chatgpt_sample_size': left.get('total_n'), 'claude_gemini_sample_size': right.get('total_n'),
                'chatgpt_effect_type': left.get('effect_type'), 'claude_gemini_effect_type': right.get('effect_type'),
                'chatgpt_effect_size': left.get('effect_size'), 'claude_gemini_effect_size': right.get('effect_size'),
                'chatgpt_ci_lower': left.get('ci_lower'), 'chatgpt_ci_upper': left.get('ci_upper'),
                'claude_gemini_ci_lower': right.get('ci_lower'), 'claude_gemini_ci_upper': right.get('ci_upper'),
                'chatgpt_p_value': left.get('p_value'), 'claude_gemini_p_value': right.get('p_value'),
                'historical_field_coverage': not uncovered, 'combined_action': action,
                'remaining_review': any(row['context_id'] == context and row['merge_action'] == 'pending_manual_review' for row in ledger),
                'title': chat.get('title') or other.get('title'), 'pubmed_url': f'https://pubmed.ncbi.nlm.nih.gov/{chat.get("pmid") or other.get("pmid")}/'})
    current_contexts = {context for context, _ in current_keys}
    summary = {'policy_version': policy.VERSION, **shared_scope_summary(tracks),
        'historical_discrepancy_contexts': len(historical_contexts), 'current_discrepancy_contexts': len(current_contexts),
        'historical_discrepancy_fields': len(historical_keys), 'current_discrepancy_fields': len(current_keys),
        'current_context_set_is_subset_of_previous': current_contexts <= historical_contexts,
        'current_field_set_is_subset_of_previous': current_keys <= historical_keys,
        'new_discrepancy_contexts': sorted(current_contexts - historical_contexts),
        'new_discrepancy_fields': [list(key) for key in sorted(current_keys - historical_keys)],
        'reused_prior_final_contexts': len(reused_contexts), 'new_unpooled_pending_contexts': len(new_pending),
        'key_agreement_contexts': key_agreements, 'metadata_fields_filled': len(fill_rows),
        'p_value_resolutions': len(p_rows), 'p_value_resolution_counts': dict(Counter(row['resolution'] for row in p_rows)),
        'wald_matching_source_counts': dict(Counter(row['matching_source'] for row in p_rows)),
        'current_pending_review_fields': sum(row['merge_action'] == 'pending_manual_review' for row in ledger),
        'new_gateway_calls': 0, 'comparison_fields': ['total_n', 'effect_size', 'ci'],
        'numeric_direction_inversion_applied': False,
        'effective_source_sha256': {track: builder.sha256_file(path) for track, path in effective_paths.items()}}
    return {'combined_rows': combined, 'ledger': ledger, 'historical_ledger': prior_ledger,
        'comparison_rows': comparison_rows, 'metadata_fills': fill_rows, 'p_resolutions': p_rows, 'summary': summary,
        'selected': selected, 'source_packets': source_packets, 'chatgpt': tracks['CHATGPT'],
        'claude_gemini': tracks['CLAUDE_GEMINI'], 'manual_packet': manual_packet, 'fable_rows': fable_rows,
        'per_context': per_context}
