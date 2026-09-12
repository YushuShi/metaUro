"""Choose an intact display source by populated scientific facts, never pooling it.

Each distinct populated study/effect field contributes one point. Aliases and
identical repeated extraction bundles count once; prose length, pipeline audit
fields, and JBI grade values do not increase a score. Equal scores use ChatGPT.
"""
from __future__ import annotations
import json
import math
import re

VERSION = 'both-nonpoolable-display-2026-09-08-v1'
MISSING = {'', '-', 'na', 'n/a', 'none', 'null', 'nan', 'unknown', 'unclear',
           'not reported', 'not available', 'not specified', 'unspecified',
           'not applicable', 'unavailable', 'not assessed', 'not provided'}
NUMERIC = {
    'effect_size': ('effect_size', 'Effect Size', 'point_estimate'),
    'ci_lower': ('ci_lower', 'Lower CI', 'lower_ci'),
    'ci_upper': ('ci_upper', 'Upper CI', 'upper_ci'),
    'total_n': ('total_n', 'sample_size', 'Sample Size', 'Participants'),
    'cases': ('cases', 'Cases', 'case_count', 'events'),
    'controls': ('controls', 'Controls', 'control_count'),
    'p_value': ('p_value', 'P Value', 'p'),
    'standard_error': ('standard_error', 'se', 'SE'),
    'person_years': ('person_years', 'Person Years'),
    'mean_age': ('mean_age', 'Mean Age'),
    'ci_confidence_level': ('ci_confidence_level', 'confidence_level', 'confidence_interval_level'),
}
TEXT = {
    'effect_type': ('effect_type', 'Effect Type', 'measure_type'),
    'design': ('design', 'study_design', 'study_type', 'Study Design', 'Study Type'),
    'comparison_type': ('comparison_type', 'comparison', 'Comparison', 'Contrast'),
    'timing': ('timing', 'Timing', 'follow_up', 'followup', 'Follow-up'),
    'continent': ('continent', 'Continent'),
    'country': ('country', 'Country'),
    'location': ('location', 'Location', 'study_location'),
    'population': ('population', 'Population'),
    'ethnicity': ('ethnicity', 'Ethnicity'),
    'gender': ('gender', 'sex', 'Gender', 'Sex'),
    'age_range': ('age_range', 'Age Range'),
    'exposure_subtype': ('exposure_subtype', 'Exposure Subtype'),
    'exposure_measurement_type': ('exposure_measurement_type', 'Exposure Measurement Type'),
    'exposure_measurement': ('exposure_measurement', 'exposure_assessment', 'measurement_method'),
    'exposure_unit': ('exposure_unit', 'unit', 'units'),
    'dose': ('dose', 'exposure_dose', 'Dose'),
    'exposure_quantification': ('exposure_quantification', 'dose_description'),
    'reference_category': ('reference_category', 'reference_group', 'Reference Group'),
    'exposure_category': ('exposure_category', 'comparison_group', 'Exposure Category'),
    'stratum': ('stratum', 'subgroup', 'Stratum', 'Subgroup'),
    'smoking_status': ('smoking_status', 'Smoking Status'),
    'adjustment': ('adjustment', 'adjustment_variables', 'adjusted_for', 'covariates', 'adjusted_model'),
}
BIBLIOGRAPHIC = {
    'title': ('title', 'Title'),
    'authors': ('authors', 'author', 'Authors', 'Author'),
    'year': ('year', 'publication_year', 'Year', 'published_date'),
    'journal': ('journal', 'Journal'),
}


def meaningful(value):
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        return bool(text) and text not in MISSING and not re.fullmatch(
            r'(?:not (?:reported|available|specified|provided|assessed)|unknown|unclear)(?: in (?:the )?(?:abstract|full text|article|paper|study))?[. ]*', text)
    if isinstance(value, (list, tuple)):
        return any(meaningful(item) for item in value)
    return False


def numeric(value, *, p_value=False):
    if not meaningful(value) or isinstance(value, (list, tuple)):
        return False
    text = str(value).strip().replace(',', '').replace('\u2212', '-')
    if p_value:
        text = re.sub(r'^(?:[pP]\s*)?[<>=≤≥]+\s*', '', text)
    try:
        return math.isfinite(float(text))
    except (ValueError, TypeError, OverflowError):
        return False


def value_for(row, aliases):
    return next((row[name] for name in aliases if meaningful(row.get(name))), None)


def extraction_facts(row):
    row = dict(row)
    ci = row.get('confidence_interval') or row.get('ci')
    if isinstance(ci, (tuple, list)) and len(ci) >= 2:
        if not meaningful(row.get('ci_lower')): row['ci_lower'] = ci[0]
        if not meaningful(row.get('ci_upper')): row['ci_upper'] = ci[1]
    elif isinstance(ci, dict):
        if not meaningful(row.get('ci_lower')): row['ci_lower'] = ci.get('lower', ci.get('low'))
        if not meaningful(row.get('ci_upper')): row['ci_upper'] = ci.get('upper', ci.get('high'))
    facts = {}
    for field, aliases in NUMERIC.items():
        value = value_for(row, aliases)
        if numeric(value, p_value=field == 'p_value'):
            facts[field] = str(value).strip().casefold()
    for field, aliases in TEXT.items():
        value = value_for(row, aliases)
        if meaningful(value):
            facts[field] = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).casefold()
    # A country repeated verbatim in Location does not add another study fact.
    geographic_values = set()
    for field in ('country', 'location', 'continent'):
        if field in facts:
            if facts[field] in geographic_values:
                del facts[field]
            else:
                geographic_values.add(facts[field])
    return facts


def information_profile(record):
    """Return an auditable count of populated facts in this source alone."""
    record = record if isinstance(record, dict) else {}
    primary = record.get('extraction')
    if not isinstance(primary, dict):
        primary = record
    candidates = [primary]
    for parent in (record, primary):
        for name in ('additional_extractions', 'additional_records'):
            candidates.extend(row for row in parent.get(name, []) or [] if isinstance(row, dict))
    bundles = []
    seen = set()
    for candidate in candidates:
        facts = extraction_facts(candidate)
        signature = json.dumps(facts, sort_keys=True, ensure_ascii=False)
        if facts and signature not in seen:
            seen.add(signature)
            bundles.append(sorted(facts))
    bibliographic = [field for field, aliases in BIBLIOGRAPHIC.items()
                     if meaningful(value_for(record, aliases))]
    return {'score': sum(map(len, bundles)) + len(bibliographic),
            'extraction_fields': bundles, 'study_fields': bibliographic}


def select_display_source(chatgpt, claude_gemini):
    profiles = {'chatgpt': information_profile(chatgpt),
                'claude_gemini': information_profile(claude_gemini)}
    track = ('claude_gemini' if profiles['claude_gemini']['score'] > profiles['chatgpt']['score']
             else 'chatgpt')
    return {'retained_track': track, 'final_status': 'ELIGIBLE_NOT_POOLABLE',
            'resolution_kind': 'both_eligible_not_poolable',
            'display_selection_version': VERSION, 'information_profiles': profiles,
            'equal_information': profiles['chatgpt']['score'] == profiles['claude_gemini']['score']}
