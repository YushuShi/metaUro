"""Apply saved trend-CI proof to exact copies in current result projections."""
from __future__ import annotations
import copy
import math
from runtime_code import p_value_guard as guard

PROVENANCE_FIELDS = ('combined_source_extraction', 'nonpoolable_source_extraction', 'extraction')
HISTORICAL_FIELDS = {
    'p_value_ci_retraction', 'policy_original_values', 'inversion_provenance',
    'original_extraction', 'raw_extraction', 'raw_response', 'raw_model_response',
    'source_response', 'source_receipt', 'raw_source_receipt', 'original_result',
    'before', 'after', 'audit_before', 'audit_after', 'original_values',
    'prior_extraction', 'superseded_unadjusted_estimates', 'history',
}


def _same_number(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    try:
        return math.isfinite(float(a)) and float(a) == float(b)
    except (TypeError, ValueError):
        return False


def exact_source(row):
    """Require all three numerical identities before borrowing CI provenance."""
    if not isinstance(row, dict) or 'Effect Size' not in row:
        return None
    for name in PROVENANCE_FIELDS:
        source = row.get(name)
        if not isinstance(source, dict):
            continue
        if not all(_same_number(row.get(target), source.get(key)) for target, key in
                   (('Effect Size', 'effect_size'), ('Lower CI', 'ci_lower'), ('Upper CI', 'ci_upper'))):
            continue
        if guard.ci_is_p_derived(source) and guard.trend_evidence(source, row):
            return name, source
    return None


def retract_projection(row, context=None):
    """Retract only proven uncertainty, retaining the exact effect orientation."""
    source = exact_source(row)
    if source is not None and not guard.ci_is_p_derived(row):
        name, proof = source
        # Work on a copy so borrowing source provenance does not rewrite the
        # study's independent reported p, CI method or original source receipt.
        work = copy.deepcopy(row)
        work['ci_method'] = 'Copied p-derived Wald CI from saved extraction'
        work['ci_source_p_value'] = proof.get('ci_source_p_value', proof.get('p_value'))
        guard.attach_context(work, proof)
        if not guard.retract_trend_ci(work, proof):
            return False
        removed = work['p_value_ci_retraction']['removed']
        for key in removed:
            if key in row:
                row[key] = None
        row['p_value_ci_retraction'] = work['p_value_ci_retraction']
        row['p_value_ci_retraction']['copied_from'] = name
        row['p_value_ci_retraction']['source_numerical_identity'] = {
            key: proof.get(key) for key in ('effect_size', 'ci_lower', 'ci_upper')}
        # The copy-only fields were never values in the visible study.
        row['p_value_ci_retraction']['removed'] = {
            key: value for key, value in removed.items()
            if key not in {'ci_method', 'ci_source_p_value'} or key in row}
        row['p_value_ci_ineligible_reason'] = guard.REASON
        row['p_value_ci_guard_version'] = guard.VERSION
        return True
    if not guard.ci_is_p_derived(row) or not guard.trend_evidence(row, context):
        return False
    return guard.retract_trend_ci(row, context)


def correct_tree(value, context=None, path=()):
    """Return row receipts; skip past retraction receipts and immutable history."""
    changes = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            changes.extend(correct_tree(item, context, path + (index,)))
    elif isinstance(value, dict):
        if value.get('pmid') or value.get('PMID') or value.get('context_id'):
            context = value
        # Parent projection proof must be tested before its nested source is cleared.
        if ('ci_lower' in value or 'Lower CI' in value) and retract_projection(value, context):
            receipt = copy.deepcopy(value['p_value_ci_retraction'])
            changes.append({'json_path': list(path), 'identity': {
                k: (context or {}).get(k) for k in ('context_id', 'pmid', 'PMID', 'cancer', 'exposure')
                if (context or {}).get(k) is not None}, 'receipt': receipt})
            if 'Lower CI' in value:
                value['scientific_pooling_eligible'] = False
                value['policy_poolable'] = False
                value['subexposure_pooling_selected'] = False
                value['pooling_reason'] = guard.REASON
                value['subexposure_pooling_reason'] = guard.REASON
            elif value.get('poolable') is True:
                value['poolable'] = False
        for key, child in list(value.items()):
            if key not in HISTORICAL_FIELDS and not key.startswith(('raw_', 'original_')) and isinstance(child, (list, dict)):
                changes.extend(correct_tree(child, context, path + (key,)))
        if isinstance(value.get('extraction'), dict) and value['extraction'].get('p_value_ci_retraction'):
            if value.get('final_status') == 'INCLUDE_META':
                value['final_status'] = 'ELIGIBLE_NOT_POOLABLE'
                if value.get('full_text_status') == 'INCLUDE_META':
                    value['full_text_status'] = 'ELIGIBLE_NOT_POOLABLE'
                value['scientific_pooling_eligible'] = False
                value['pooling_reason'] = guard.REASON
    return changes


def sanitized_frame(frame):
    """Protect direct analysis entry points and preserve caller-owned frames."""
    if frame is None or frame.empty:
        return frame
    rows = copy.deepcopy(frame.to_dict(orient='records'))
    if not correct_tree(rows):
        return frame
    import pandas as pd
    return pd.DataFrame(rows)
