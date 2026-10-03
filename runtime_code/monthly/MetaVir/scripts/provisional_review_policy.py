"""Offline, reversible UI choices for pairs still awaiting human adjudication.

This layer never resolves a review or mixes key numbers from different estimates.
Repository adapters retain independent study/cohort restrictions and finalized data.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
from scripts import key_comparison_policy as policy
from scripts import astra_fulltext_common as common

VERSION = 'provisional-human-review-2026-09-07-v2'
GRADES = {'poor': 1, 'fair': 2, 'moderate': 3, 'good': 4}


def load_pending(repo_root, alias=None):
    root = Path(repo_root)
    local = root / 'combined/provisional_ui/pending_manifest.json'
    shared = root.parent / 'screening_status_reports/provisional_human_review_2026-09-07/pending_manifest.json'
    path = shared if shared.exists() else local
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if 'repos' in data:
        return data['repos'].get(alias or root.name, {}).get('rows', [])
    return data.get('rows', [])


def highest_jbi(*records):
    """Return one complete, actually saved assessment; never merge checklists."""
    candidates = []
    for index, record in enumerate(records):
        record = record or {}
        value = record.get('jbi') or record.get('jbi_assessment')
        if not isinstance(value, dict):
            continue
        grade = str(value.get('grade') or value.get('rating') or '').strip().lower()
        if grade not in GRADES:
            continue
        score = policy.number(value.get('score_percent'))
        candidates.append((GRADES[grade], float(score) if score is not None else -1, -index, value))
    return copy.deepcopy(max(candidates, key=lambda item:item[:3])[3]) if candidates else None


def _normalized(extraction):
    if not isinstance(extraction, dict):
        return None
    result = copy.deepcopy(extraction)
    result.update(common.normalize_extraction(policy.normalise_estimate(extraction)))
    return result


def select_pending_pair(chatgpt_record, claude_gemini_record, chatgpt_estimate,
                        claude_gemini_estimate, review_entries, *, baseline_estimate=None):
    """Select a display estimate and its provisional pooling candidacy.

    Callers pass only entries for the exact comparison pair. A complete selection
    may pool while pending, subject to normal scientific and quality gates.
    """
    entries = [x for x in review_entries if x.get('manual_review_required', True)]
    higher = highest_jbi(chatgpt_record, claude_gemini_record)
    result = {'version': VERSION, 'pending': bool(entries), 'selected_track': None,
              'extraction': None, 'higher_jbi': higher,
              'higher_jbi_grade': (higher or {}).get('grade', (higher or {}).get('rating')),
              'review_ids': [x['id'] for x in entries],
              'source_fingerprint': common.source_digest(chatgpt_record, claude_gemini_record),
              'selection_poolable_candidate': False,
              'protect_adjudicated_quantities': False, 'reason': 'Not pending human review.'}
    if not entries:
        result.update(higher_jbi=None, higher_jbi_grade=None)
        return result
    if common.is_human(chatgpt_record) or common.is_human(claude_gemini_record):
        result.update(reason='Existing human decision remains authoritative.', higher_jbi=None,
                      higher_jbi_grade=None, protect_adjudicated_quantities=True)
        return result
    resolution = policy.nonpoolable_status_resolution(chatgpt_record, claude_gemini_record)
    if resolution:
        from scripts.nonpoolable_status_retention import resolution_reason
        selected = resolution['retained_track']
        source = chatgpt_record if selected == 'chatgpt' else claude_gemini_record
        result.update(pending=False, selected_track=selected, extraction=copy.deepcopy(source.get('extraction') or {}),
                      higher_jbi=None, higher_jbi_grade=None, selection_poolable_candidate=False,
                      reason=resolution_reason(resolution))
        return result
    applied_partial = any((entry.get('application') or {}).get('applied') for entry in entries)
    source_records = [chatgpt_record, claude_gemini_record]
    normalized = [_normalized(chatgpt_estimate), _normalized(claude_gemini_estimate)]
    excluded = [policy.is_excluded(row) or not isinstance(row, dict) for row in source_records]
    complete = [not excluded[i] and common.coherent(normalized[i]) for i in range(2)]
    selected = None
    if all(complete):
        selected = 0
        reason = 'Use ChatGPT by default while both complete pipeline estimates await human review.'
    elif sum(complete) == 1:
        selected = complete.index(True)
        reason = 'Use the complete eligible pipeline estimate while the other is excluded or quantitatively incomplete.'
    elif sum(excluded) == 1:
        selected = excluded.index(False)
        reason = 'Use the non-excluded pipeline for display; quantitative inputs remain incomplete.'
    else:
        reason = 'Neither pipeline supplies a complete eligible estimate; retain unpooled review candidates.'
    if applied_partial and (selected is None or not complete[selected]):
        result.update(protect_adjudicated_quantities=True,
                      reason='Retain supported full-text correction; neither pipeline supplies a complete eligible estimate.')
        return result
    if selected is not None:
        extraction = normalized[selected] or {}
        # Keep the selected model's atomic bundle. Non-key metadata may be filled.
        extraction = common.fill_compatible_metadata(extraction,
            {'chatgpt': normalized[0], 'claude_gemini': normalized[1]}, {},
            {'chatgpt': chatgpt_record, 'claude_gemini': claude_gemini_record})
        fills = extraction.pop('astra_fulltext_metadata_fills', [])
        extraction = common.preserve_scientific_gates(extraction,
            [chatgpt_record, claude_gemini_record, chatgpt_estimate, claude_gemini_estimate, baseline_estimate])
        result.update(selected_track=['chatgpt','claude_gemini'][selected], extraction=extraction, metadata_fills=fills,
                      selection_poolable_candidate=complete[selected],
                      overrides_pending_astra_partial=applied_partial)
    result['reason'] = reason
    return result
