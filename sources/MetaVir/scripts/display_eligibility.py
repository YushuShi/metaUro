"""Eligible Fair-quality evidence is visible without becoming poolable."""
from scripts import dietary_only_policy as dietary_policy
import copy
import hashlib
import json
import math
import re


def eligible_fair(item):
    status = item.get('final_status') or item.get('full_text_status') or ''
    return (str((item.get('jbi') or {}).get('grade') or '').casefold() == 'fair'
            and status in {'ELIGIBLE_NOT_POOLABLE', 'ELIGIBLE_SUBTYPE_REVIEW', 'INCLUDE_META'}
            and item.get('eligibility') != 'EXCLUDE')


def eligible_fulltext_display(item):
    return (bool(item.get('astra_fulltext_adjudication') or item.get('nonpoolable_status_retained') or item.get('human_adjudication'))
            and item.get('eligibility') == 'INCLUDE'
            and (item.get('final_status') or item.get('full_text_status')) == 'ELIGIBLE_NOT_POOLABLE')


def _source_identity(value):
    """Keep a record ID stable when only the project classification changes."""
    if isinstance(value, list):
        return [_source_identity(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _source_identity(item) for key, item in value.items() if key not in dietary_policy.AUDIT_FIELDS}
    if isinstance(value.get('exposure_measurement_original_fields'), dict):
        for key in dietary_policy.ALIASES:
            result.pop(key, None)
        result.update(copy.deepcopy(value['exposure_measurement_original_fields']))
    return result


def display_row(item):
    """Project existing evidence faithfully; missing numeric fields stay null."""
    e = copy.deepcopy(item.get('extraction') or {})
    jbi = copy.deepcopy(item.get('jbi') or {})
    def number(value):
        try:
            n = float(str(value).replace(',', ''))
            return n if math.isfinite(n) else None
        except (ValueError, TypeError):
            return None
    effect, lo, hi = (number(e.get(k)) for k in ['effect_size', 'ci_lower', 'ci_upper'])
    if e.get('needs_inversion') and all(v is not None and v > 0 for v in [effect, lo, hi]):
        effect, lo, hi = 1 / effect, 1 / hi, 1 / lo
    pmid = str(item.get('pmid') or '')
    authors = item.get('authors') or ['Unknown']
    year = re.search(r'\b(19|20)\d{2}\b', str(item.get('published_date') or ''))
    direct = item.get('direct_exposure') or item.get('exposure')
    identity = [pmid, direct, item.get('cancer') or item.get('disease'), item.get('endpoint'), _source_identity(e)]
    uid = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:16]
    row = {'Study': f"{authors[0]} et al. ({year.group() if year else 'Unknown'})", 'PMID': pmid,
           'Record ID': f'{pmid}:{uid}', 'Effect ID': f'{pmid}:{uid}', 'Title': item.get('title'),
           'Reference': item.get('title') or '', 'Authors': authors, 'Year': year.group() if year else 'Unknown',
           'Journal': item.get('journal') or 'NA', 'Link': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
           'Effect Type': e.get('effect_type') or 'NA', 'Outcome Type': e.get('reported_effect_type') or e.get('effect_type') or 'NA',
           'Effect Size': effect, 'Lower CI': lo, 'Upper CI': hi, 'P Value': number(e.get('p_value')),
           'SE': ((math.log(hi)-math.log(lo))/3.919927969080108 if lo and hi and lo > 0 and hi > lo else None),
           'Sample Size': number(e.get('total_n')), 'Cases': number(e.get('cases')), 'Estimated Cases': None,
           'Quality Score': jbi.get('grade') or 'Fair', 'Quality %': jbi.get('score_percent'), 'JBI': jbi,
           'Design': e.get('study_design') or item.get('study_design') or 'NA',
           'Timing': e.get('timing') or 'NA', 'Stage': e.get('stage') or 'NA',
           'Gender': e.get('gender') or 'NA', 'Ethnicity': e.get('ethnicity') or 'NA',
           'Continent': e.get('continent') or 'NA', 'Population': e.get('population') or 'NA',
           'comparison_type': e.get('comparison_type') or 'NA',
           'exposure_measurement_type': e.get('exposure_measurement_type') or 'unclear',
           'extraction_supporting_text': {k: v.get('excerpt', '') for k,v in (e.get('evidence') or {}).items() if isinstance(v, dict)},
           'Direct Exposure': direct, 'Displayed Exposure': item.get('exposure'),
           'Hierarchy Inherited': bool(item.get('hierarchy_inherited')),
           'pool_in_parent_meta_analysis': False, 'Primary Estimate': False,
           'display_only': True, 'policy_poolable': False, 'manual_poolable': False,
           'display_only_reason': 'Eligible study; JBI Fair is below the default pooling threshold.',
           'final_status': item.get('final_status') or item.get('full_text_status'),
           'context_id': item.get('context_id')}
    for key in ['direction_imputed','direction_imputation_reason','effect_size_imputed','ci_level_assumed','effect_is_adjusted','manual_review','decision_origin','shared_outcome_estimate_id']:
        if key in e: row[key] = e[key]
        elif key in item: row[key] = item[key]
    for field in dietary_policy.AUDIT_FIELDS:
        if field in e:
            row[field] = e[field]
    if e.get('astra_fulltext_provenance'):
        source = e['astra_fulltext_provenance'].get('source') or {}
        row.update(PMCID=source.get('pmcid'), extraction_evidence=e.get('evidence') or {},
                   evidence_excerpt_source=source.get('url'), evidence_source_scope='PMC_FULL_TEXT_TEXT_ONLY')
    if eligible_fulltext_display(item) and not eligible_fair(item):
        row['display_only_reason'] = ('Eligible; quantitative information remains incomplete. Shown for review and not pooled.'
                                      if item.get('astra_quantitative_follow_up_required') else
                                      'Eligible; a saved pooling restriction applies. Shown for review and not pooled.')
        row['astra_fulltext_display_only'] = True
    if item.get('nonpoolable_status_retained'):
        from scripts.nonpoolable_status_retention import decorate_study
        row['nonpoolable_status_resolution'] = copy.deepcopy(item.get('nonpoolable_status_resolution') or {})
        if row['nonpoolable_status_resolution'].get('resolution_kind') == 'both_eligible_not_poolable':
            row.update(Design=e.get('design') or e.get('study_design') or 'NA',
                       Country=e.get('country') or 'NA', Location=e.get('location') or e.get('country') or 'NA',
                       PMCID=item.get('pmcid') or '', extraction_evidence=copy.deepcopy(e.get('evidence') or {}),
                       exposure_quantification=e.get('exposure_quantification') or 'NA')
        row = decorate_study(row)
    if item.get('manual_review_pending'):
        from scripts.provisional_ui_overlay import decorate_row
        row = decorate_row(row,item)
    from scripts.human_manual_review import decorate_study
    row = decorate_study(row, item)
    return dietary_policy.apply_row(row, item.get('exposure'))
