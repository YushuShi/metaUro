"""Retain supported nonpoolable pipeline resolutions without numeric adjudication."""
from __future__ import annotations
import copy
from scripts import key_comparison_policy as policy
from scripts.astra_fulltext_common import is_human, source_digest

VERSION = 'nonpoolable-status-retention-2026-09-08-both-v2'
REASON = ('One pipeline excluded this study and the other retained it as eligible but not poolable. '
          'Retained for display without pooling; this comparison does not require manual review.')
BOTH_REASON = ('Both pipelines classify this study as eligible but not poolable. '
               'The pipeline with more populated study and result fields is displayed; '
               'equal information uses ChatGPT. This study is not pooled and this comparison does not require manual review.')


def resolution_reason(resolution):
    return BOTH_REASON if (resolution or {}).get('resolution_kind') == 'both_eligible_not_poolable' else REASON


def resolve_record(chatgpt, claude_gemini, previous=None, protected=False):
    resolution = policy.nonpoolable_status_resolution(chatgpt, claude_gemini)
    if not resolution or protected or any(is_human(row) for row in (chatgpt, claude_gemini, previous)):
        return None
    fingerprint = source_digest(chatgpt, claude_gemini)
    receipt = (previous or {}).get('nonpoolable_status_resolution') or {}
    if ((previous or {}).get('nonpoolable_status_retained') and receipt.get('source_fingerprint') == fingerprint
            and receipt.get('retained_track') == resolution['retained_track']
            and receipt.get('resolution_kind') == resolution.get('resolution_kind')
            and (resolution.get('resolution_kind') != 'both_eligible_not_poolable' or receipt.get('version') == VERSION)):
        return copy.deepcopy(previous)
    selected = chatgpt if resolution['retained_track'] == 'chatgpt' else claude_gemini
    result = copy.deepcopy(selected)
    result.update(eligibility='INCLUDE', final_status='ELIGIBLE_NOT_POOLABLE',
                  full_text_status='ELIGIBLE_NOT_POOLABLE', key_comparison_pending=False,
                  manual_review_pending=False, manual_poolable=False, policy_poolable=False,
                  scientific_pooling_eligible=False, nonpoolable_status_retained=True,
                  reason=resolution_reason(resolution))
    result.pop('key_comparison_candidates', None)
    result['nonpoolable_status_resolution'] = {
        'version': VERSION, **resolution, 'source_fingerprint': fingerprint,
        'chatgpt_status': chatgpt.get('final_status') or chatgpt.get('full_text_status'),
        'claude_gemini_status': claude_gemini.get('final_status') or claude_gemini.get('full_text_status'),
        'previous_combined_status': (previous or {}).get('final_status'),
        'manual_review_required': False, 'pooling_eligible': False,
    }
    result['key_comparison'] = {'action': 'retained_eligible_not_poolable', 'current_fields': [],
                                'policy_version': VERSION}
    # Keep later automated decisions as history without giving them pooling authority.
    history = {key: copy.deepcopy(previous[key]) for key in
               ('astra_adjudication', 'astra_fulltext_adjudication', 'poolability_policy')
               if isinstance(previous, dict) and previous.get(key)}
    if history:
        result['nonpoolable_status_resolution']['previous_automated_adjudications'] = history
    return result


def apply_records(records, tracks, protected=()):
    result = copy.deepcopy(records)
    resolved = []
    for context in sorted(set(tracks.get('chatgpt', {})) & set(tracks.get('claude_gemini', {}))):
        row = resolve_record(tracks['chatgpt'][context], tracks['claude_gemini'][context],
                             result.get(context), context in protected)
        if row is not None:
            result[context] = row
            resolved.append(context)
    return result, resolved


def display_records(record):
    """Expose each distinct selected-source estimate without borrowing fields."""
    if (record.get('nonpoolable_status_resolution') or {}).get('resolution_kind') != 'both_eligible_not_poolable':
        return [copy.deepcopy(record)]
    estimates = policy.extracts(record)
    return [dict(copy.deepcopy(record), extraction=copy.deepcopy(estimate)) for estimate in estimates] or [copy.deepcopy(record)]


def decorate_study(row):
    result = copy.deepcopy(row)
    reason = resolution_reason(result.get('nonpoolable_status_resolution'))
    result.update(final_status='ELIGIBLE_NOT_POOLABLE', scientific_pooling_eligible=False,
                  nonpoolable_status_retained=True, display_only=True, ui_display_only=True,
                  policy_poolable=False, manual_poolable=False, pool_in_parent_meta_analysis=False,
                  key_comparison_pending=False, manual_review_pending=False,
                  subexposure_pooling_selected=False, display_only_reason=reason,
                  ui_display_reason=reason, fair_quality_opt_in_available=False)
    result['Primary Estimate'] = False
    for field in ('disease_scope_pooling_selected', 'Settings Pooling Selected'):
        if field in result:
            result[field] = False
    if isinstance(result.get('disease_scope_original_row'), dict):
        result['disease_scope_original_row'] = decorate_study(result['disease_scope_original_row'])
    if isinstance(result.get('provisional_ui_selection'), dict):
        result['provisional_ui_selection'].update(pending=False, selection_poolable_candidate=False,
                                                   effective_poolable=False, reason=reason)
    return result
