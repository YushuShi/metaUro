"""Explicit Fair-quality sensitivity selections, confined to trusted request rows.

The ordinary Moderate+ analysis and saved evidence are unchanged.  Callers must
bind submitted identities to saved rows before using this module; browser flags
are not evidence that a scientific or manual restriction may be released.
"""
from __future__ import annotations
import copy
import math
import re

QUALITY_REASONS = frozenset({
    'eligible study; jbi fair is below the default pooling threshold.',
    'eligible; jbi fair — shown for review, not pooled',
    'fair jbi: displayed for review; not included in pooled estimates.',
})
QUALITY_RELEASES = {
    'eligible study; jbi fair is below the default pooling threshold.': {
        'display_only', 'policy_poolable', 'manual_poolable', 'Primary Estimate', 'pool_in_parent_meta_analysis'},
    'eligible; jbi fair — shown for review, not pooled': {
        'ui_display_only', 'included_in_meta_analysis', 'Pool in Parent Meta-Analysis', 'pool_in_parent_meta_analysis'},
    'fair jbi: displayed for review; not included in pooled estimates.': {'display_only'},
}
ELECTION_REASONS = frozenset({
    'another exposure estimate from this study is selected to avoid counting the same population twice.',
    'another eligible endpoint/contrast is selected for this study population, or this row does not meet the existing pooling criteria.',
})
TRUE_HOLDS = (
    'manual_review_pending', 'key_comparison_pending', 'requires_key_review',
    'provisional_ui_display_only', 'full_text_pooling_block',
    'exposure_classification_ambiguous', 'astra_quantitative_follow_up_required',
    'astra_fulltext_display_only', 'disease_scope_unquantified',
    'direction_review_required', 'clinical_exclusion', 'scientific_hold',
)
FALSE_HOLDS = ('manual_pooling_eligible', 'scientific_pooling_eligible',
               'included_in_parent_meta_analysis', 'dietary_pooling_eligible', 'poolable',
               'pool_in_exposure_parent_meta_analysis', 'pool_in_disease_parent_meta_analysis')


def _normalized(value):
    """Copy JSON-like records and remove pandas' nonfinite missing sentinels."""
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return copy.deepcopy(value)


def _without_request_flags(row):
    row = _normalized(row)
    # Saved rows must be rebound before preparation; a round-tripped browser
    # decision can never authorize the next request.
    for field in ('fair_quality_opted_in', 'fair_quality_opt_in_reason',
                  'fair_quality_selection_overrides'):
        row.pop(field, None)
    return row


def _text(value):
    value = ' '.join(str(value or '').split()).casefold()
    return '' if value in {'-', 'na', 'n/a', 'none', 'nan', 'null'} else value


def _key(value):
    return re.sub(r'[^a-z0-9]+', '_', _text(value)).strip('_')


def _get(row, *fields):
    for field in fields:
        if row.get(field) not in (None, ''):
            return row[field]
    return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(',', ''))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _grade(row):
    return _text(_get(row, 'Quality Score', 'quality_rating', 'jbi_grade', 'quality_grade')
                 or (row.get('jbi') or {}).get('grade'))


def _complete(row):
    kind = _key(_get(row, 'Effect Type', 'effect_type', 'reported_effect_type', 'effect_measure'))
    ratio = kind in {'rr', 'irr', 'or', 'hr', 'risk_ratio', 'relative_risk',
                     'incidence_rate_ratio', 'odds_ratio', 'hazard_ratio'}
    if not ratio and kind not in {'beta', 'mean_difference'}:
        return False
    values = [_number(_get(row, *fields)) for fields in (
        ('Sample Size', 'sample_size', 'total_n'),
        ('Effect Size', 'effect_size', 'rr'),
        ('Lower CI', 'ci_lower', 'ci_low', 'lower_ci'),
        ('Upper CI', 'ci_upper', 'ci_high', 'ci_upp', 'upper_ci'))]
    if any(value is None for value in values):
        return False
    n, effect, low, high = values
    return n > 0 and n == int(n) and low <= effect <= high and low < high and (not ratio or low > 0)


def is_fair_opt_in(data):
    """Both an explicit flag and a Fair-inclusive request mode are required."""
    return (isinstance(data, dict) and data.get('allow_fair_quality') is True
            and data.get('quality_filter') in {'Fair+', 'All', 'Fair'})


def _quality_releases(row):
    reasons = [_text(row[field]) for field in ('display_only_reason', 'ui_display_reason', 'pooling_exclusion_reason')
               if _text(row.get(field))]
    if not reasons or any(reason not in QUALITY_REASONS for reason in reasons):
        return set()
    return set().union(*(QUALITY_RELEASES[reason] for reason in reasons))


def eligible_for_user_selection(row, target_exposure=None):
    """Recognize only complete Fair rows with a known quality-only restriction."""
    row = _normalized(row)
    if not isinstance(row, dict) or _grade(row) != 'fair' or not _complete(row):
        return False
    releases = _quality_releases(row)
    if not releases:
        return False
    for field in ('display_only', 'ui_display_only'):
        if row.get(field) is True and field not in releases:
            return False
    for field in ('policy_poolable', 'manual_poolable', 'Primary Estimate',
                  'pool_in_parent_meta_analysis', 'Pool in Parent Meta-Analysis', 'included_in_meta_analysis'):
        if row.get(field) is False and field not in releases:
            return False
    if any(row.get(field) is True for field in TRUE_HOLDS):
        return False
    if any(row.get(field) is False for field in FALSE_HOLDS):
        return False
    if any(row.get(field) for field in ('manual_review', 'human_adjudication',
                                       'manual_adjudication', 'missing_quantities', 'missing_required_fields')):
        return False
    for field in ('final_status', 'full_text_status', 'screening_status', 'meta_analysis_status'):
        status = _text(row.get(field))
        if status and status not in {'include_meta', 'eligible_not_poolable', 'include', 'included'}:
            return False
    for reason in row.get('third_round_reasons') or []:
        if _key(reason) not in {'fair_jbi', 'fair_quality', 'jbi_fair'}:
            return False
    provisional = row.get('provisional_ui_selection') or {}
    if isinstance(provisional, dict) and any(provisional.get(field) for field in (
            'independent_source_hold', 'independent_hold_preserved', 'normalization_error',
            'separate_scientific_scope')):
        return False
    if any(row.get(field) is True for field in (
            'Hierarchy Inherited', 'hierarchy_inherited', 'subexposure_inherited',
            'broader_outcome_projection', 'disease_hierarchy_inherited')):
        return False
    direct = _get(row, 'Direct Exposure', 'direct_exposure', 'subexposure_source', 'exposure')
    displayed = target_exposure or _get(row, 'Displayed Exposure', 'displayed_exposure')
    if direct and displayed and _key(direct) != _key(displayed):
        return False
    # A default projection also deselects Fair rows. Only its known eligibility
    # election may be rerun; a separate, unexplained scope restriction stays held.
    for prefix in ('subexposure', 'disease_scope'):
        if row.get(prefix + '_pooling_selected') is False:
            reason = _text(row.get(prefix + '_pooling_reason'))
            if (reason and reason not in ELECTION_REASONS) or (prefix == 'disease_scope' and not reason):
                return False
    return True


def annotate_rows(rows, target_exposure=None):
    """Expose trusted checkbox eligibility without releasing any restriction."""
    result = [_without_request_flags(row) for row in rows]
    for row in result:
        row['fair_quality_opt_in_available'] = eligible_for_user_selection(row, target_exposure)
        row['fair_quality_selection_overrides'] = {}
        if row['fair_quality_opt_in_available']:
            preview = prepare_quality_opt_in_rows([row], 'Fair+', allow_fair_quality=True,
                                                  target_exposure=target_exposure)[0]
            fields = _quality_releases(row) | {'subexposure_pooling_selected', 'fair_quality_opted_in'}
            if 'disease_scope_pooling_selected' in row:
                fields.add('disease_scope_pooling_selected')
            row['fair_quality_selection_overrides'] = {field: preview[field] for field in fields}
    return result


def _population(row, index):
    identifier = _get(row, 'PMID', 'pmid')
    if identifier:
        return ('pmid', str(identifier).removesuffix('.0'))
    return ('record', str(_get(row, 'Record ID', 'Effect ID', 'record_id') or index))


def _selected_nonfair(row):
    return (_grade(row) in {'good', 'moderate'} and _complete(row)
            and not any(row.get(field) is True for field in ('display_only', 'ui_display_only'))
            and not any(row.get(field) is False for field in (
                'subexposure_pooling_selected', 'disease_scope_pooling_selected',
                'pool_in_parent_meta_analysis', 'Pool in Parent Meta-Analysis',
                'policy_poolable', 'manual_poolable', 'manual_pooling_eligible', 'Primary Estimate')))


def _set(row, field, value):
    if field in row and row[field] != value:
        row.setdefault('Original ' + field, copy.deepcopy(row[field]))
    row[field] = value


def prepare_quality_opt_in_rows(trusted_rows, quality_filter='Moderate+', *,
                                allow_fair_quality=False, target_exposure=None):
    """Lift quality defaults only for explicitly submitted, safe Fair estimates.

    Existing eligible Good/Moderate selections retain precedence. At most one new
    Fair estimate per PMID is added; smoking, sex, and intensity labels cannot
    establish independent participants. Original grade/status and numeric data
    are preserved, and no source cache is mutated.
    """
    result = [_without_request_flags(row) for row in trusted_rows]
    if not is_fair_opt_in({'quality_filter': quality_filter, 'allow_fair_quality': allow_fair_quality}):
        return result
    occupied = {_population(row, index) for index, row in enumerate(result) if _selected_nonfair(row)}
    candidates = [(index, row) for index, row in enumerate(result)
                  if eligible_for_user_selection(row, target_exposure)]
    candidates.sort(key=lambda item: (
        not any(item[1].get(field) is True for field in ('effect_is_adjusted', 'effect_adjusted', 'Effect Is Adjusted')),
        item[0]))
    for index, row in candidates:
        population = _population(row, index)
        if population in occupied:
            row['fair_quality_opt_in_reason'] = 'Another selected estimate already represents this study population.'
            continue
        occupied.add(population)
        row['fair_quality_opted_in'] = True
        row['fair_quality_opt_in_available'] = True
        row['fair_quality_opt_in_reason'] = 'Included by explicit Fair-quality sensitivity selection; saved quality remains Fair.'
        for field in _quality_releases(row):
            _set(row, field, field not in {'display_only', 'ui_display_only'})
        _set(row, 'subexposure_pooling_selected', True)
        for field in ('disease_scope_pooling_selected',):
            if field in row:
                _set(row, field, True)
    return result


IDENTITY_FIELDS = ('Record ID', 'record_id', 'Effect ID', 'effect_id', 'CacheRowID', 'cache_row_id')
EDITABLE_FIELDS = ('Effect Size', 'Lower CI', 'Upper CI', 'Sample Size', 'Participants', 'Cases')


def _stable_ids(row):
    return {str(row[field]).strip() for field in IDENTITY_FIELDS
            if row.get(field) not in (None, '')}


def _fallback_identity(row):
    """Legacy rows without stable IDs must retain their exact numeric bundle."""
    return (_text(_get(row, 'PMID', 'pmid')).removesuffix('.0'),
            _key(_get(row, 'Direct Exposure', 'direct_exposure', 'subexposure_source', 'exposure')),
            _text(_get(row, 'context_id', 'Context ID')),
            _key(_get(row, 'Effect Type', 'effect_type', 'reported_effect_type', 'effect_measure')),
            *(_number(_get(row, *fields)) for fields in (
                ('Effect Size', 'effect_size', 'rr'), ('Lower CI', 'ci_lower', 'ci_low'),
                ('Upper CI', 'ci_upper', 'ci_high', 'ci_upp'), ('Sample Size', 'sample_size', 'total_n'))))


def bind_fair_rows(submitted, trusted, target_exposure=None):
    """Bind Fair selections to unique saved identities, restoring every hold.

    Only the existing UI's editable effect, CI, N and case fields are copied
    from submitted Fair rows. Stable IDs allow those numeric edits; legacy rows
    without IDs require an unchanged numeric bundle. Non-Fair rows retain the
    existing route contract. An identity that resolves to Fair remains Fair
    even if a browser changes its grade. Ambiguous or unavailable Fair evidence
    raises ValueError rather than inheriting a browser-authored permission.
    """
    sources = [_without_request_flags(row) for row in trusted]
    result = []
    seen = set()
    for incoming in submitted:
        row = _without_request_flags(incoming)
        identifiers = _stable_ids(row)
        matches = [(index, source) for index, source in enumerate(sources)
                   if (bool(identifiers & _stable_ids(source)) if identifiers
                       else _fallback_identity(row) == _fallback_identity(source))]
        fair = _grade(row) == 'fair' or any(_grade(source) == 'fair' for _, source in matches)
        if not fair:
            result.append(row)
            continue
        if len(matches) != 1:
            raise ValueError('Fair-quality selection does not match one saved evidence row. Reload the evidence and select it again.')
        index, source = matches[0]
        if identifiers and not identifiers.issubset(_stable_ids(source)):
            raise ValueError('Fair-quality selection contains inconsistent saved row identifiers.')
        for fields in (('PMID', 'pmid'), ('Direct Exposure', 'direct_exposure', 'subexposure_source', 'exposure'),
                       ('context_id', 'Context ID')):
            received, saved = _get(row, *fields), _get(source, *fields)
            if received is not None and saved is not None and _key(received) != _key(saved):
                raise ValueError('Fair-quality selection does not match the saved evidence context.')
        if index in seen:
            raise ValueError('The same Fair-quality evidence row was submitted more than once.')
        if not eligible_for_user_selection(source, target_exposure):
            raise ValueError('This saved Fair-quality row has a restriction beyond the default quality threshold.')
        seen.add(index)
        bound = _normalized(source)
        for field in EDITABLE_FIELDS:
            if field in row:
                bound[field] = _normalized(row[field])
        result.append(bound)
    return result
