"""Offline comparison of effective model estimates under the user's key-field rule."""
from __future__ import annotations
import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import math
import re
from statistics import NormalDist

VERSION = 'key-comparison-2026-09-08-both-nonpoolable-v3'
KEY_FIELDS = ('total_n', 'effect_size', 'ci_lower', 'ci_upper')
ALIASES = {
    'total_n': ('total_n', 'sample_size', 'Sample Size', 'Participants'),
    'effect_size': ('effect_size', 'Effect Size', 'point_estimate'),
    'ci_lower': ('ci_lower', 'Lower CI', 'lower_ci'),
    'ci_upper': ('ci_upper', 'Upper CI', 'upper_ci'),
    'p_value': ('p_value', 'P Value'),
    'effect_type': ('effect_type', 'Effect Type'),
}
MISSING = {'', '-', 'na', 'n/a', 'none', 'null', 'nan', 'unknown', 'unclear', 'not reported', 'not available', 'unspecified'}
PROTECTED = set(KEY_FIELDS) | {alias for field in KEY_FIELDS for alias in ALIASES[field]} | {
    'confidence_interval', 'ci', 'p_value', 'P Value', 'p_value_relation',
    'context_id', 'record_id', 'pmid', 'PMID', 'exposure', 'cancer', 'disease', 'outcome',
    'eligibility', 'final_status', 'full_text_status', 'decision_authority', 'track',
    'additional_records', 'additional_extractions', 'results', 'studies', 'extraction',
    'packet_sha256', 'extraction_sha256', 'source_extraction_sha256', 'estimate_digest',
    # These describe how a particular numeric bundle was derived, not general study metadata.
    'calculation', 'standard_error', 'variance', 'se', 'log_effect', 'log_effect_size',
    'confidence_level', 'confidence_interval_level', 'imputation_flags',
    'effect_size_imputed', 'p_value_imputed', 'p_value_method', 'p_value_is_bound',
    'direction_imputed', 'effect_direction_imputed', 'effect_direction_imputation_reason',
}


def missing(value):
    if value is None: return True
    if isinstance(value, bool): return False
    if isinstance(value, float) and not math.isfinite(value): return True
    if isinstance(value, str): return value.strip().casefold() in MISSING
    return isinstance(value, (list, dict, tuple)) and not value


def number(value):
    if missing(value) or isinstance(value, bool): return None
    try:
        text = str(value).strip().replace(',', '').replace('\u2212', '-')
        result = Decimal(text)
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError): return None


def _places(value):
    value = number(value)
    return max(0, -value.normalize().as_tuple().exponent) if value is not None else 0


def numeric_equal(a, b, kind='ratio'):
    """Counts compare exactly; estimates round to common precision, at least 2 dp.

    The 2-decimal floor prevents an integer ratio such as 1 from accepting 1.4.
    Small numbers retain their finer stored precision; no fixed relative tolerance.
    """
    av, bv = number(a), number(b)
    if av is None or bv is None:
        return missing(a) and missing(b)
    if av == bv: return True
    if kind in {'count', 'total_n', 'sample_size'}: return False
    precision = max(2, min(_places(a), _places(b)))
    quantum = Decimal(1).scaleb(-precision)
    return av.quantize(quantum, rounding=ROUND_HALF_UP) == bv.quantize(quantum, rounding=ROUND_HALF_UP)


def normalise_estimate(value):
    result = copy.deepcopy(value or {})
    for key, aliases in ALIASES.items():
        if key not in result:
            result[key] = next((result[a] for a in aliases if a in result), None)
    ci = result.get('confidence_interval') or result.get('ci')
    if isinstance(ci, (list, tuple)) and len(ci) >= 2:
        if missing(result.get('ci_lower')): result['ci_lower'] = ci[0]
        if missing(result.get('ci_upper')): result['ci_upper'] = ci[1]
    elif isinstance(ci, dict):
        if missing(result.get('ci_lower')): result['ci_lower'] = ci.get('lower', ci.get('low'))
        if missing(result.get('ci_upper')): result['ci_upper'] = ci.get('upper', ci.get('high'))
    return result


def extracts(record):
    """Flatten actual primary/additional estimates; do not inherit missing key values."""
    if not isinstance(record, dict): return []
    output = []
    primary = record.get('extraction')
    if not isinstance(primary, dict) and any(k in record for k in KEY_FIELDS): primary = record
    candidates = [primary] if isinstance(primary, dict) else []
    for obj in (record, primary):
        if not isinstance(obj, dict): continue
        for field in ('additional_records', 'additional_extractions'):
            candidates.extend(v for v in obj.get(field, []) or [] if isinstance(v, dict))
    seen = set()
    for item in candidates:
        normal = normalise_estimate(item)
        for field in ('additional_records', 'additional_extractions'): normal.pop(field, None)
        signature = json.dumps(normal, sort_keys=True, default=str)
        if signature not in seen: output.append(normal); seen.add(signature)
    return output


def is_excluded(record):
    return isinstance(record, dict) and (
        str(record.get('final_status') or '').upper() == 'EXCLUDE'
        or str(record.get('eligibility') or '').upper() == 'EXCLUDE'
    )


def key_differences(a, b):
    if a is None and b is None: return []
    if a is None or b is None: return ['total_n', 'effect_size', 'ci']
    result = []
    if not numeric_equal(a.get('total_n'), b.get('total_n'), 'count'): result.append('total_n')
    if not numeric_equal(a.get('effect_size'), b.get('effect_size')): result.append('effect_size')
    if any(not numeric_equal(a.get(k), b.get(k)) for k in ('ci_lower', 'ci_upper')): result.append('ci')
    return result


def wald_scale(estimate):
    label = str(estimate.get('effect_type') or '').casefold().strip()
    if re.search(r'\b(a?rr|a?or|a?hr|irr)\b', label) or any(s in label for s in ('risk ratio','relative risk','odds ratio','hazard ratio','rate ratio','geometric mean ratio')):
        return 'log_ratio'
    if label in {'md','smd','wmd','beta','change','rd'} or label.startswith('β') or any(s in label for s in ('mean difference','difference of means','adjusted difference','beta coefficient','beta-coefficient','risk difference')):
        return 'additive'
    return None


def wald_p_value(estimate):
    estimate = normalise_estimate(estimate)
    values = [number(estimate.get(k)) for k in ('effect_size', 'ci_lower', 'ci_upper')]
    if any(v is None for v in values): return None
    effect, lower, upper = map(float, values)
    scale = wald_scale(estimate)
    if not lower < upper or scale is None: return None
    if scale == 'log_ratio':
        if not (lower > 0 and effect > 0): return None
        estimate_value = math.log(effect)
        se = (math.log(upper) - math.log(lower)) / (2 * NormalDist().inv_cdf(.975))
    else:
        estimate_value = effect
        se = (upper - lower) / (2 * NormalDist().inv_cdf(.975))
    return math.erfc(abs(estimate_value / se) / math.sqrt(2)) if se > 0 else None


def _p_parts(estimate):
    value = estimate.get('p_value')
    relation = str(estimate.get('p_value_relation') or '=').strip().casefold()
    relation = {
        'exact':'=', 'derived_exact':'=', 'equal':'=', 'equals':'=', 'eq':'=',
        'less_than':'<', 'less than':'<', 'lt':'<',
        'greater_than':'>', 'greater than':'>', 'gt':'>',
        'less_than_or_equal':'<=', 'less_than_or_equal_to':'<=', 'lte':'<=',
        'greater_than_or_equal':'>=', 'greater_than_or_equal_to':'>=', 'gte':'>=',
    }.get(relation, relation)
    if isinstance(value, str):
        match = re.fullmatch(r'\s*(?:p\s*)?([<>=\u2264\u2265]+)?\s*([.\d]+(?:[eE][+-]?\d+)?)\s*', value)
        if match:
            relation = match.group(1) or relation; value = match.group(2)
    numeric = number(value)
    return numeric, relation.replace('\u2264','<=').replace('\u2265','>='), value


def _p_matches(estimate, wald):
    p, relation, raw = _p_parts(estimate)
    if p is None or not (0 <= p <= 1): return False
    if relation in {'<','<='}: return wald < float(p) if relation == '<' else wald <= float(p)
    if relation in {'>','>='}: return wald > float(p) if relation == '>' else wald >= float(p)
    if p in {Decimal(0), Decimal(1)}: return abs(wald-float(p)) <= 1e-12
    quantum = Decimal(1).scaleb(-_places(raw))
    return Decimal(str(wald)).quantize(quantum, rounding=ROUND_HALF_UP) == p


def resolve_p_value(chatgpt, claude_gemini, agreed_estimate=None):
    a, b = normalise_estimate(chatgpt), normalise_estimate(claude_gemini)
    pa, ra, _ = _p_parts(a); pb, rb, _ = _p_parts(b)
    both_missing = pa is None and pb is None
    same = (both_missing or (pa is not None and pb is not None and ra == rb and numeric_equal(pa,pb)))
    calculation = copy.deepcopy(agreed_estimate or b)
    if missing(calculation.get('effect_type')) and not missing(a.get('effect_type')): calculation['effect_type'] = a['effect_type']
    scales = {scale for scale in (wald_scale(a),wald_scale(b)) if scale is not None}
    wald = wald_p_value(calculation) if len(scales) < 2 else None
    result = {'needed':not same, 'wald_p_value':wald, 'chatgpt_matches':None, 'claude_gemini_matches':None,
              'matching_source':None, 'combined_p_value': b.get('p_value'), 'combined_p_value_relation':b.get('p_value_relation'),
              'method':('Two-sided Wald test on the additive scale (null 0), using the 95% CI width.' if wald_scale(calculation)=='additive' else 'Two-sided Wald test on the log ratio (null 1), using the 95% CI width.')}
    if same: result['resolution']='no_p_discrepancy'; return result
    if wald is None: result['resolution']='wald_unavailable'; return result
    am, bm = _p_matches(a,wald), _p_matches(b,wald)
    result.update(chatgpt_matches=am, claude_gemini_matches=bm,
                  matching_source='both' if am and bm else 'chatgpt' if am else 'claude_gemini' if bm else 'neither',
                  combined_p_value=wald, combined_p_value_relation='=', resolution='computed_two_sided_wald')
    return result


def merge_missing_fields(preferred, other, protected_fields=()):
    """Fill missing non-key values; preserve complete atomic key and p bundles."""
    result = copy.deepcopy(preferred or {}); fills = []
    protected = PROTECTED | set(protected_fields)
    def visit(target, donor, prefix=''):
        for key, value in (donor or {}).items():
            if key in protected or key.startswith(('raw_', 'source_', 'original_', 'reported_', 'ci_', 'p_source_', 'imputed_', 'wald_', 'calculated_')): continue
            path = f'{prefix}.{key}' if prefix else key
            current = target.get(key)
            if missing(current) and not missing(value):
                target[key] = copy.deepcopy(value); fills.append({'field':path,'value':copy.deepcopy(value)})
            elif isinstance(current,dict) and isinstance(value,dict): visit(current,value,path)
    visit(result, other)
    return result, fills


def nonpoolable_status_resolution(chatgpt, claude_gemini):
    """Resolve this one status pair for display without a pooling decision.

    Explicit human decisions remain authoritative. No agreement about numeric
    values or eligibility for pooling is inferred from the automatic resolution.
    """
    records = {'chatgpt': chatgpt, 'claude_gemini': claude_gemini}
    if not all(isinstance(row, dict) for row in records.values()):
        return None
    for row in records.values():
        authority = str(row.get('decision_authority') or row.get('decision_origin') or '').strip().lower()
        resolution = str((row.get('combined_adjudication') or {}).get('resolution') or '').upper()
        if (row.get('manual_review') or row.get('manual_adjudication') or row.get('human_adjudication')
                or row.get('manual_full_text_adjudication') or row.get('manual_full_text_review')
                or ('PENDING' not in resolution and any(word in resolution for word in ('HUMAN', 'MANUAL')))
                or row.get('combined_manual_authority') or authority in {'human', 'manual', 'human_manual_review'}):
            return None
    statuses = {}
    for track, row in records.items():
        eligibility = row.get('eligibility')
        if isinstance(eligibility, dict):eligibility = eligibility.get('decision')
        fallback = 'EXCLUDE' if str(eligibility or '').strip().upper() == 'EXCLUDE' else ''
        statuses[track] = str(row.get('final_status') or row.get('full_text_status') or fallback).strip().upper()
    if set(statuses.values()) == {'ELIGIBLE_NOT_POOLABLE'}:
        from scripts.nonpoolable_display_selection import select_display_source
        return select_display_source(chatgpt, claude_gemini)
    if set(statuses.values()) != {'EXCLUDE', 'ELIGIBLE_NOT_POOLABLE'}:
        return None
    return {'retained_track': next(track for track, status in statuses.items() if status == 'ELIGIBLE_NOT_POOLABLE'),
            'final_status': 'ELIGIBLE_NOT_POOLABLE'}


def compare_context(chatgpt, claude_gemini):
    """Pair numeric estimates, using non-key metadata only to break equal-cost ties."""
    resolution = nonpoolable_status_resolution(chatgpt, claude_gemini)
    if resolution:
        return {'pairs': [], 'has_key_discrepancy': False, 'both_excluded': False,
                'nonpoolable_status_resolution': resolution}
    if is_excluded(chatgpt) and is_excluded(claude_gemini):
        return {'pairs':[], 'has_key_discrepancy':False, 'both_excluded':True}
    a = [] if is_excluded(chatgpt) else extracts(chatgpt)
    b = [] if is_excluded(claude_gemini) else extracts(claude_gemini)
    # An accepted context without any extraction still differs from an excluded/missing counterpart.
    if not a and isinstance(chatgpt,dict) and not is_excluded(chatgpt): a=[normalise_estimate({})]
    if not b and isinstance(claude_gemini,dict) and not is_excluded(claude_gemini): b=[normalise_estimate({})]
    matched = []
    if a and b:
        from scipy.optimize import linear_sum_assignment
        costs=[]
        for x in a:
            row=[]
            for y in b:
                difference=key_differences(x,y)
                proximity=0.0
                for key in KEY_FIELDS:
                    xv,yv=number(x.get(key)),number(y.get(key))
                    if xv is not None and yv is not None:
                        proximity += min(abs(float(xv-yv))/max(abs(float(xv)),abs(float(yv)),1e-9),1.0)
                metadata=sum(str(x.get(k) or '').casefold()!=str(y.get(k) or '').casefold() for k in ('gender','stratum','subgroup','exposure_subtype'))
                row.append(len(difference)*100 + proximity + metadata*.001)
            costs.append(row)
        ai,bi=linear_sum_assignment(costs)
        matched=list(zip(map(int,ai),map(int,bi)))
    paired_a={i for i,_ in matched};paired_b={j for _,j in matched}
    matched += [(i,None) for i in range(len(a)) if i not in paired_a]
    matched += [(None,j) for j in range(len(b)) if j not in paired_b]
    pairs=[]
    for i,j in matched:
        left=a[i] if i is not None else None;right=b[j] if j is not None else None
        diff=key_differences(left,right)
        pairs.append({'chatgpt':left,'claude_gemini':right,'chatgpt_index':i,'claude_gemini_index':j,
                      'differences':diff,'key_agreement':not diff,
                      'p_resolution':resolve_p_value(left,right) if not diff and left is not None and right is not None else None})
    return {'pairs':pairs,'has_key_discrepancy':any(p['differences'] for p in pairs),'both_excluded':False}
