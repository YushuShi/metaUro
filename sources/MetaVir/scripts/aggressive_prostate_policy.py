"""Deterministic, auditable disease hierarchy from Sol abstract adjudications."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import defaultdict

from scripts import incidence_endpoint_policy

GENERAL = 'Prostate cancer'
AGGRESSIVE = 'Aggressive prostate cancer'
POLICY_VERSION = 2
AUDIT_FIELDS = ['Disease Scope', 'Disease Endpoint', 'Aggressive Definition',
                'Disease Endpoint Definition', 'disease_scope_review',
                'disease_scope_original_row', 'disease_scope_pooling_selected',
                'disease_scope_pooling_reason']


def key(value):
    return re.sub(r'[^a-z0-9]+', '_', str(value or '').casefold()).strip('_')


def original_row(row):
    return copy.deepcopy(row.get('disease_scope_original_row') or row)


def source_rows(rows, excluded_source_rows=None):
    """Undo projection, including rows retained only in endpoint audit state."""
    result, seen = [], set()
    for row in list(rows) + list(excluded_source_rows or []):
        if (row.get('disease_scope_review') or {}).get('origin') == 'abstract_extraction':
            continue
        original = original_row(row)
        identity = json.dumps(original, sort_keys=True, default=str)
        if identity not in seen:
            seen.add(identity)
            result.append(original)
    return result


def positive_integer(value):
    try:
        n = float(str(value).replace(',', ''))
        return int(n) if math.isfinite(n) and n > 0 and n.is_integer() else None
    except (ValueError, TypeError):
        return None


def usable_effect(row):
    try:
        e, lo, hi = (float(row.get(k)) for k in ['Effect Size', 'Lower CI', 'Upper CI'])
        return all(math.isfinite(x) for x in [e, lo, hi]) and 0 < lo <= e <= hi and lo < hi and str(row.get('Effect Type')).upper() in {'RR', 'OR', 'HR', 'IRR'}
    except (ValueError, TypeError):
        return False


def estimate_signature(row):
    from effect_measure import normalize_effect_type
    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (ValueError, TypeError):
            return None
    return (row['Disease Endpoint'], normalize_effect_type(row.get('Effect Type')),
            *(number(row.get(field)) for field in ('Effect Size', 'Lower CI', 'Upper CI')),
            key(row.get('comparison_type')))


def annotate(row, review, endpoint, decision, row_id, origin='saved_estimate'):
    result = copy.deepcopy(row)
    result.update({
        'Disease Endpoint': endpoint,
        'Aggressive Definition': review.get('aggressive_definition'),
        'disease_scope_original_row': original_row(row),
        'disease_scope_review': {
            'policy_version': POLICY_VERSION, 'model': 'gpt-5.6-sol',
            'pmid': str(row.get('PMID')), 'source_row_id': row_id,
            'origin': origin, 'article_classification': review.get('article_classification'),
            'endpoint': endpoint, 'endpoint_excerpt': decision.get('endpoint_excerpt'),
            'endpoint_support_source': decision.get('endpoint_support_source', 'abstract' if decision.get('supported_by_abstract') else 'none'),
            'supported_by_abstract': decision.get('supported_by_abstract', False),
            'reason': decision.get('reason'),
        },
    })
    return result


def _comparison_orientation(comparison):
    """Recognize an explicit exposure ordering without inferring it from an effect."""
    text = str(comparison or '').strip().casefold().replace('_', ' ').split(';', 1)[0]
    if not text or re.search(r'\b(?:unclear|unknown|not (?:reported|stated|specified)|unspecified)\b', text):
        return None
    if re.search(r'\b(?:per|for each)\b', text):
        if re.search(r'\b(?:decrease|decrement|reduction|halving)\b', text):
            return 'per_unit_decrease'
        if re.search(r'\b(?:increase|increment|doubling|serving|higher)\b', text):
            return 'per_unit_increase'
    parts = re.split(r'\s+(?:versus|vs\.?|compared (?:with|to)|relative to)\s+', text, maxsplit=1)
    if len(parts) != 2:
        return None
    left, right = parts
    low = r'\b(?:low(?:er|est)?|bottom|deficient|below[- ]median)\b'
    high = r'\b(?:high(?:er|est)?|top|upper|adequate|sufficient|above[- ]median)\b'
    absent = r'\b(?:no(?:ne)?|non[- ]?(?:use(?:rs?)?|drinkers?|consumers?)|never|unexposed|rarely)\b'
    if re.search(low, left) and re.search(high, right):
        return 'lower_vs_higher'
    if re.search(high, left) and re.search(low, right):
        return 'higher_vs_lower'
    # Nonuse is a zero-exposure reference, with named use or a numeric dose.
    present = r'\b(?:yes|use(?:rs?)?|exposed|consum\w*|high(?:er|est)?|intake)\b|\d'
    if re.search(absent, left) and not re.search(absent, right) and re.search(present, right):
        return 'lower_vs_higher'
    if re.search(absent, right) and not re.search(absent, left) and re.search(present, left):
        return 'higher_vs_lower'

    def rank(part):
        match = re.search(r'\b([1-9])(?:st|nd|rd|th)\b|\b(?:quartile|quintile|tertile|q|t)\s*([1-9])\b', part)
        if match:
            return int(next(value for value in match.groups() if value))
        for word, value in [('first', 1), ('second', 2), ('third', 3), ('fourth', 4), ('fifth', 5)]:
            if re.search(r'\b' + word + r'\b', part):
                return value
        return None
    left_rank, right_rank = rank(left), rank(right)
    if left_rank is not None and right_rank is not None and left_rank != right_rank:
        return 'higher_vs_lower' if left_rank > right_rank else 'lower_vs_higher'
    if (left_rank and left_rank > 1 or re.search(high, left)) and re.match(r'(?:the )?(?:lower/)?reference\b', right):
        return 'higher_vs_lower'
    # Explicit high-dose / low-dose bounds establish ordering even when the
    # quantity's unit is only written once (e.g. >=1183 versus <825 mg/day).
    greater = r'(?:>|≥|greater than|at least)'
    less = r'(?:<|≤|less than)'
    if re.search(greater, left) and re.search(less, right):
        return 'higher_vs_lower'
    if re.search(less, left) and re.search(greater, right):
        return 'lower_vs_higher'
    if re.search(r'\d\s*\+', left) and re.search(less, right):
        return 'higher_vs_lower'
    if re.search(r'\d', left) and re.search(low, right):
        return 'higher_vs_lower'
    # A bounded lower reference may have no leading '<', e.g. >=60 vs 0.1–4.9.
    if re.search(greater, left):
        upper_dose = re.search(r'(?:>|≥|greater than|at least)\s*(?:or\s*=?\s*|=\s*)?([\d,.]+)', left)
        reference_range = re.match(r'([\d,.]+)\s*[-–]\s*<?\s*([\d,.]+)', right)
        if upper_dose and reference_range and float(upper_dose[1].replace(',', '')) > float(reference_range[2].replace(',', '')):
            return 'higher_vs_lower'
    doses = [re.search(r'([\d,.]+)\s*(mg|g|μg|ug|mcg|ng|iu|servings?)(/[a-z]+)?\b', part) for part in [left, right]]
    if all(doses) and doses[0].groups()[1:] == doses[1].groups()[1:]:
        amounts = [float(dose[1].replace(',', '')) for dose in doses]
        if amounts[0] != amounts[1]:
            return 'higher_vs_lower' if amounts[0] > amounts[1] else 'lower_vs_higher'
    return None


def _normalize_derived_direction(row, estimate):
    """Reset template metadata and retain the paper's numbers before inversion."""
    comparison = estimate.get('comparison') or 'Unclear'
    orientation = _comparison_orientation(comparison)
    for field in ('comparison_direction', 'comparison_group', 'reference_group',
                  'direction_original_value', 'direction_imputation_reason',
                  'effect_inversion_reason', 'comparison_labels_imputed'):
        row.pop(field, None)
    row.update(comparison=comparison, comparison_type=comparison,
               direction_original_comparison=comparison, direction_imputed=False,
               effect_inverted=False, needs_inversion=False)
    reported = {field: estimate.get(source) for field, source in [
        ('Effect Type', 'effect_type'), ('Effect Size', 'effect_size'),
        ('Lower CI', 'lower_ci'), ('Upper CI', 'upper_ci')]}
    for field, value in reported.items():
        row['Original ' + field] = value
    audit = {'reported_comparison': comparison, 'reported_estimate': reported,
             'reported_orientation': orientation, 'inverted': False}
    row['disease_scope_review']['direction_normalization'] = audit
    if orientation is None:
        if usable_effect(row):
            row.update(display_only=True, policy_poolable=False, manual_poolable=False,
                       pool_in_parent_meta_analysis=False, **{'Primary Estimate': False},
                       display_only_reason='The abstract reports a numeric endpoint, but its exposure comparison direction needs review.')
        audit['status'] = 'needs_review'
        return
    if orientation in {'lower_vs_higher', 'per_unit_decrease'} and usable_effect(row):
        effect, lower, upper = (float(row[field]) for field in ['Effect Size', 'Lower CI', 'Upper CI'])
        row['Effect Size'], row['Lower CI'], row['Upper CI'] = 1 / effect, 1 / upper, 1 / lower
        row['effect_inverted'] = audit['inverted'] = True
        row['comparison'] = row['comparison_type'] = 'Per unit increase' if orientation == 'per_unit_decrease' else 'Higher versus lower'
        row['effect_inversion_reason'] = 'Reciprocal of the explicitly reported lower-versus-higher exposure comparison; CI bounds reversed.'
    row['comparison_direction'] = 'per_unit_increase' if orientation.startswith('per_unit') else 'higher_vs_lower'
    audit['status'] = 'normalized' if audit['inverted'] else 'reported_direction_retained'


def _derived_row(template, estimate, review, row_id):
    endpoint = estimate.get('endpoint')
    row = annotate(template, review, endpoint, {
        'endpoint_excerpt': estimate.get('effect_excerpt'),
        'endpoint_support_source': 'abstract',
        'supported_by_abstract': True, 'reason': 'Separate endpoint evidence extracted from the abstract.'
    }, row_id, 'abstract_extraction')
    row.update({
        'Effect Type': estimate.get('effect_type') or 'NA',
        'Effect Size': estimate.get('effect_size'),
        'Lower CI': estimate.get('lower_ci'), 'Upper CI': estimate.get('upper_ci'),
        'Sample Size': positive_integer(estimate.get('sample_size')) or (
            positive_integer(review.get('cohort_total_n'))
            if review.get('prospective_cohort') is True and review.get('cohort_total_n_excerpt') else None),
        'Cases': positive_integer(estimate.get('cases')), 'Estimated Cases': None,
        'P Value': None, 'SE': None, 'effect_inverted': False,
        'comparison_type': estimate.get('comparison') or 'Unclear',
        'direction_imputed': False,
        'Disease Endpoint Definition': estimate.get('endpoint_definition'),
        'extraction_evidence': {},
        'extraction_supporting_text': {'effect_size': estimate.get('effect_excerpt'),
                                      'cases': estimate.get('count_excerpt')},
    })
    identity = hashlib.sha256(json.dumps(estimate, sort_keys=True).encode()).hexdigest()[:16]
    row['Effect ID'] = row['Record ID'] = f"{row['PMID']}:aggressive-review:{identity}"
    row['context_id'] = f"{row['PMID']}|{row.get('Direct Exposure')}|{AGGRESSIVE if endpoint == 'aggressive' else GENERAL}"
    # Adjustment and a primary-estimate designation belong to the original
    # estimate. Prior restrictive scientific/manual gates remain restrictive.
    for field in ('effect_is_adjusted', 'effect_adjusted', 'Effect Is Adjusted',
                  'reported_effect_type'):
        row.pop(field, None)
    if row.get('Primary Estimate') is True:
        row.pop('Primary Estimate')
    _normalize_derived_direction(row, estimate)
    return row


def select_scope_rows(rows, exposure):
    """Select overlapping endpoints once, retaining explicitly independent strata."""
    from scripts.subexposure_policy import project_rows, row_identity
    by_exposure = defaultdict(list)
    for row in rows:
        by_exposure[row.get('Direct Exposure') or exposure].append(row)
    for direct, candidates in by_exposure.items():
        projected = project_rows({direct: candidates}, direct, {}, include=False)
        selected = {r['subexposure_projection_id'] for r in projected if r['subexposure_pooling_selected']}
        for row in candidates:
            row['disease_scope_pooling_selected'] = row_identity(row, direct) in selected
            row['disease_scope_pooling_reason'] = (
                '' if row['disease_scope_pooling_selected'] else
                'Another eligible endpoint/contrast is selected for this study population, or this row does not meet the existing pooling criteria.')


def project(rows, exposure, reviews, row_id_function, case_reviews=None,
            endpoint_exclusions=None):
    """Return parent rows, child rows and per-action audit; never pool both endpoints in parent."""
    originals = source_rows(rows)
    annotated, audit = [], []
    grouped = defaultdict(list)
    for original in originals:
        pmid = str(original.get('PMID') or '')
        review = reviews.get(pmid, {})
        rid = row_id_function(original)
        endpoint_record = incidence_endpoint_policy.matching_record(
            original, endpoint_exclusions, source_row_id=rid)
        if endpoint_record is not None:
            action = incidence_endpoint_policy.audit_action(
                endpoint_record, original, source_row_id=rid,
                origin='saved_estimate')
            action['excluded_source_row'] = copy.deepcopy(original)
            audit.append(action)
            continue
        decisions = {d['row_id']: d for d in review.get('estimate_classifications', [])}
        decision = decisions.get(rid, {})
        endpoint = decision.get('endpoint', 'uncertain')
        existing_support = (decision.get('endpoint_support_source') == 'existing_evidence'
                            and bool(decision.get('endpoint_excerpt')))
        if endpoint == 'aggressive':
            existing_support = existing_support and review.get('article_classification') in {'both', 'aggressive_only'} and bool(review.get('aggressive_definition_excerpt'))
        if not (decision.get('supported_by_abstract') is True or existing_support):
            endpoint = 'uncertain'
        row = annotate(original, review, endpoint, decision, rid)
        if endpoint == 'aggressive' and case_reviews is not None:
            counts = case_reviews.get(rid, {})
            row['Cases'] = positive_integer(counts.get('endpoint_cases')) if counts.get('status') == 'confirmed' else None
            row['Estimated Cases'] = None
            row['disease_scope_review']['endpoint_case_count'] = {
                'original_cases': original.get('Cases'),
                'endpoint_cases': row['Cases'],
                'excerpt': counts.get('excerpt'), 'source': counts.get('source'),
                'reason': counts.get('reason'), 'model': 'gpt-5.6-sol',
            }
        grouped[(pmid, key(original.get('Direct Exposure') or exposure))].append(row)
        annotated.append(row)

    for (pmid, direct), templates in grouped.items():
        review = reviews.get(pmid, {})
        # Prefer a poolable saved context as the metadata template, preserving its gates.
        template = min(templates, key=lambda r: (r.get('display_only') is True,
                                                not usable_effect(r), str(r.get('Effect ID'))))
        for estimate in review.get('additional_estimates', []):
            if key(estimate.get('exposure_key')) != direct or estimate.get('endpoint') not in {'general', 'aggressive'} or not estimate.get('effect_excerpt'):
                continue
            derived = _derived_row(template, estimate, review, template['disease_scope_review']['source_row_id'])
            endpoint_record = incidence_endpoint_policy.matching_record(
                derived, endpoint_exclusions,
                source_row_id=template['disease_scope_review']['source_row_id'])
            if endpoint_record is not None:
                audit.append(incidence_endpoint_policy.audit_action(
                    endpoint_record, derived,
                    source_row_id=template['disease_scope_review']['source_row_id'],
                    origin='abstract_extraction'))
                continue
            if any(estimate_signature(r) == estimate_signature(derived) for r in templates):
                continue
            templates.append(derived)
            annotated.append(derived)
            audit.append({'PMID': pmid, 'exposure': direct, 'action': 'abstract_endpoint_added', 'endpoint': derived['Disease Endpoint'], 'Effect ID': derived['Effect ID']})

        general = [r for r in templates if r['Disease Endpoint'] == 'general']
        aggressive = [r for r in templates if r['Disease Endpoint'] == 'aggressive']
        if general and aggressive and review.get('prospective_cohort') is True:
            n = positive_integer(review.get('cohort_total_n')) if review.get('cohort_total_n_excerpt') else None
            if n is None:
                # The saved overall endpoint provides the cohort denominator, not subtype cases.
                ns = {positive_integer(r.get('Sample Size')) for r in general}
                ns.discard(None)
                n = next(iter(ns)) if len(ns) == 1 else None
            if n is not None:
                for row in general + aggressive:
                    prior = row.get('Sample Size')
                    row['Sample Size'] = n
                    row['disease_scope_review']['cohort_sample_size'] = {
                        'original': prior, 'shared_total_n': n,
                        'source': 'abstract' if review.get('cohort_total_n_excerpt') else 'saved_overall_endpoint',
                        'excerpt': review.get('cohort_total_n_excerpt'),
                    }
                audit.append({'PMID': pmid, 'exposure': direct, 'action': 'shared_prospective_cohort_n', 'sample_size': n})
            else:
                for row in general + aggressive:
                    row['Sample Size'] = None
                    row.update(display_only=True, policy_poolable=False, manual_poolable=False,
                               display_only_reason='Shared prospective cohort denominator needs review.')
                audit.append({'PMID': pmid, 'exposure': direct, 'action': 'shared_cohort_n_unresolved'})

        # Evaluate new numeric evidence after any supported shared cohort N is
        # resolved. A nonprospective endpoint cannot borrow the overall sample.
        for row in templates:
            if (row.get('disease_scope_review') or {}).get('origin') == 'abstract_extraction' and (not usable_effect(row) or not positive_integer(row.get('Sample Size'))):
                row.update(display_only=True, policy_poolable=False, manual_poolable=False,
                           pool_in_parent_meta_analysis=False, **{'Primary Estimate': False},
                           display_only_reason='This endpoint is reported, but the abstract does not supply a complete poolable estimate and sample size.')

    parent, child = [], []
    for row in annotated:
        pmid = str(row.get('PMID') or '')
        group = grouped[(pmid, key(row.get('Direct Exposure') or exposure))]
        endpoint = row['Disease Endpoint']
        if endpoint == 'aggressive':
            row['Estimated Cases'] = None
            child_row = copy.deepcopy(row)
            child_row['Disease Scope'] = AGGRESSIVE
            child_row['Estimated Cases'] = None
            child.append(child_row)
            # A separately supplied overall endpoint replaces subtype inheritance.
            if any(r['Disease Endpoint'] == 'general' for r in group):
                audit.append({'PMID': pmid, 'exposure': exposure, 'action': 'separate_general_endpoint_prevents_duplicate_inheritance', 'source_row_id': row['disease_scope_review']['source_row_id']})
                continue
            row['disease_scope_review']['inherited_from'] = AGGRESSIVE
        row['Disease Scope'] = GENERAL
        parent.append(row)
    select_scope_rows(parent, exposure)
    select_scope_rows(child, exposure)
    return parent, child, audit
