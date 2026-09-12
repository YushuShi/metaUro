"""Disease ancestry and overlap-aware views of saved combined or ChatGPT evidence."""
import copy
import hashlib
import json
import re
from collections import defaultdict
from scripts.subexposure_policy import eligible_for_selection

URINARY_PARENTS = {
    'urothelial_carcinoma_of_the_bladder': 'bladder_cancer',
    'squamous_cell_carcinoma_of_the_bladder': 'bladder_cancer',
    'adenocarcinoma_of_the_bladder': 'bladder_cancer',
    'small_cell_carcinoma_of_the_bladder': 'bladder_cancer',
    'renal_cell_carcinoma': 'kidney_and_renal_pelvis_cancer',
    'clear_cell_renal_cell_carcinoma': 'renal_cell_carcinoma',
    'papillary_renal_cell_carcinoma': 'renal_cell_carcinoma',
    'chromophobe_renal_cell_carcinoma': 'renal_cell_carcinoma',
    'urothelial_carcinoma_of_the_renal_pelvis': 'kidney_and_renal_pelvis_cancer',
    'renal_sarcoma': 'kidney_and_renal_pelvis_cancer',
}
SKIN_PARENTS = {name: 'skin_cancer' for name in (
    'cutaneous_melanoma', 'basal_cell_carcinoma', 'cutaneous_squamous_cell_carcinoma')}
COMMON_NAMES = {
    'acne_vulgaris': 'acne',
    'hidradenitis_suppurativa': 'acne inversa', 'rosacea': 'acne rosacea',
    'atopic_dermatitis': 'eczema', 'alopecia_areata': 'patchy hair loss',
    'androgenetic_alopecia': 'pattern hair loss',
    'seborrheic_dermatitis': 'dandruff / seborrheic eczema',
}
SOURCES = [
    'https://training.seer.cancer.gov/kidney/abstract-code-stage/morphology.html',
    'https://www.cancer.gov/publications/dictionaries/cancer-terms/def/skin-cancer',
]
AUDIT_FIELDS = ['Disease Evidence Source', 'Displayed Disease', 'disease_hierarchy_inherited',
    'disease_hierarchy_source_key', 'disease_hierarchy_source_path', 'disease_hierarchy_projection_id',
    'disease_hierarchy_pooling_selected', 'disease_hierarchy_pooling_reason', 'disease_hierarchy_source_context_excluded', 'disease_hierarchy_parent_context_excluded', 'disease_hierarchy_reference_basis']

def decorate_registry(registry, kind):
    scopes = copy.deepcopy(registry)
    parents = URINARY_PARENTS if kind == 'urinary' else SKIN_PARENTS
    if kind == 'skin':
        ordered = {}
        for key, scope in scopes.items():
            if key in SKIN_PARENTS and 'skin_cancer' not in ordered:
                ordered['skin_cancer'] = {'key':'skin_cancer','site_id':'skin_cancer','label':'Skin cancer',
                    'disease':'Skin cancer','aliases':['skin malignancy','skin cancers'],
                    'incidence_rate':None,'incidence_rate_percent':None,'lifetime_risk_percent':None,
                    'endpoints':['Incidence'],'subcategories':[],
                    'baseline_note':'No exact published all-skin-cancer baseline; do not substitute melanoma risk.'}
            if key in COMMON_NAMES:
                scope['label'] = scope['disease'] + ' (' + COMMON_NAMES[key] + ')'
            ordered[key] = scope
        scopes = ordered
    for key, scope in scopes.items():
        parent = parents.get(key)
        scope['parent_key'] = parent if parent in scopes else None
        scope['children'] = [k for k, p in parents.items() if p == key and k in scopes]
        depth = 0; ancestor = scope['parent_key']; seen = {key}
        while ancestor:
            if ancestor in seen: raise ValueError('Disease hierarchy cycle')
            seen.add(ancestor);depth += 1;ancestor = parents.get(ancestor)
        scope['hierarchy_depth'] = depth
        scope['scope_kind'] = 'parent' if scope['children'] else 'subtype' if depth else 'disease'
    return scopes

def descendant_keys(scopes, parent):
    output = []
    def visit(key):
        for child in scopes[key].get('children', []):
            if child in output: continue
            output.append(child);visit(child)
    visit(parent)
    return output

def _identity(row, source):
    fields = ['PMID','Study','Record ID','Screening Record ID','Effect Type','Effect Size','Lower CI','Upper CI',
              'Original Effect Type','Original Effect Size','Original Lower CI','Original Upper CI',
              'Sample Size','Gender','independent_population_id','Independent Population ID','comparison_type',
              'direct_exposure','subexposure_projection_id']
    return hashlib.sha256(json.dumps([source,*[row.get(f) for f in fields]],sort_keys=True,default=str).encode()).hexdigest()

def eligible(row):
    import effect_measure
    kind = effect_measure.normalize_effect_type(row.get('Effect Type'))
    if kind not in {'RR','IRR','OR','HR'}: return False
    validator = getattr(effect_measure, 'is_acceptable_incidence_effect_type', None)
    if validator and not validator(row.get('Effect Type'), row.get('Outcome Type')): return False
    return row.get('disease_hierarchy_source_context_excluded') is not True and row.get('disease_hierarchy_parent_context_excluded') is not True and eligible_for_selection(row) and row.get('subexposure_pooling_selected') is not False and row.get('pool_in_parent_meta_analysis') is not False

def is_projection(row):
    """Ignore placeholder audit columns emitted by legacy table serializers."""
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(row.get("disease_hierarchy_projection_id") or "")))

def select_rows(rows):
    """Prefer a parent-direct estimate; otherwise one eligible child per overlap group.

    Only explicit independent population IDs or exclusively male/female strata
    establish disjoint populations. Different sample sizes or cancer subtypes do
    not establish independence. Saved quantitative, quality and manual gates stay.
    """
    result = copy.deepcopy(rows)
    groups = defaultdict(list)
    for row in result:
        if not is_projection(row): continue
        study = str(row.get('PMID') or row.get('Study') or row['disease_hierarchy_projection_id'])
        if eligible(row): groups[study].append(row)
    winners = set()
    for candidates in groups.values():
        explicit = all(r.get('independent_population_id') or r.get('Independent Population ID') for r in candidates)
        genders = {str(r.get('Gender') or '').strip().casefold() for r in candidates}
        split_gender = bool(genders) and genders.issubset({'male','female','men','women'})
        strata = {}
        ranked = sorted(candidates, key=lambda r:(
            r.get('disease_hierarchy_inherited') is True,
            not (r.get('effect_is_adjusted') is True or r.get('effect_adjusted') is True or r.get('Effect Is Adjusted') is True),
            r.get('Primary Estimate') is not True,
            str(r.get('disease_hierarchy_source_key') or ''),r['disease_hierarchy_projection_id']))
        for row in ranked:
            population = row.get('independent_population_id') or row.get('Independent Population ID')
            gender = str(row.get('Gender') or '').strip().casefold()
            stratum = ('population',str(population)) if explicit else ('gender',{'men':'male','women':'female'}.get(gender,gender)) if split_gender else ('study','overlapping_or_unspecified')
            strata.setdefault(stratum,row)
        winners.update(r['disease_hierarchy_projection_id'] for r in strata.values())
    for row in result:
        if not is_projection(row): continue
        selected = row['disease_hierarchy_projection_id'] in winners
        row['disease_hierarchy_pooling_selected'] = selected
        row['disease_hierarchy_pooling_reason'] = (
            'Selected eligible estimate for this study/population; parent-direct preference, then adjusted/primary estimate.' if selected else
            'Another overlapping disease estimate is selected for this study/population.' if eligible(row) else
            'Saved quantitative, quality, clinical, exposure or manual pooling gates are not met.')
    return result

def compose_result(base, sources, parent_key, scopes, exposure, endpoint, source_filter=None, include_direct_only=False):
    """Append child evidence in memory; never write or rescreen source caches."""
    track = (base or {}).get('canonical_track') or next((payload.get('canonical_track') for _, payload, _ in sources if payload.get('canonical_track') in {'combined', 'chatgpt'}), 'combined')
    if track not in {'combined', 'chatgpt'}: return base
    valid = [(key,payload,path) for key,payload,path in sources if payload.get('canonical_track') == track]
    if not valid and (not include_direct_only or not base): return None
    parent = scopes[parent_key]['disease']
    result = copy.deepcopy(base or {'success':True,'canonical_track':track,'studies':[],'headline':None})
    rows = [];seen = set()
    all_sources = [(parent_key,base,'direct parent cache')] if base else []
    all_sources += valid
    for key,payload,path in all_sources:
        source_rows = payload.get('studies') or []
        allowed = set(range(len(source_rows)))
        if source_filter is not None and source_rows:
            import pandas as pd
            allowed = set(source_filter(pd.DataFrame(source_rows), scopes[key]['disease'], exposure, endpoint).index)
        for index, raw in enumerate(source_rows):
            if not isinstance(raw,dict): continue
            identity = _identity(raw,key)
            if identity in seen: continue
            seen.add(identity);row = copy.deepcopy(raw)
            row.update({'Disease Evidence Source':scopes[key]['disease'],'Displayed Disease':parent,
                'disease_hierarchy_inherited':key!=parent_key,'disease_hierarchy_source_key':key,
                'disease_hierarchy_source_path':str(path),'disease_hierarchy_projection_id':identity,
                'disease_hierarchy_source_context_excluded':index not in allowed,
                'disease_hierarchy_reference_basis':'Selected parent reference risk is an analysis proxy for inherited subtype estimates; it is not a published subtype risk.' if key!=parent_key else 'Exact selected disease reference, when available.'})
            rows.append(row)
    if source_filter is not None and rows:
        import pandas as pd
        parent_allowed = set(source_filter(pd.DataFrame(rows), parent, exposure, endpoint).index)
        for index, row in enumerate(rows):
            row['disease_hierarchy_parent_context_excluded'] = index not in parent_allowed
    rows = select_rows(rows)
    result.update({'disease':parent,'cancer':parent,'exposure':exposure,'endpoint':endpoint,'outcome':endpoint,
        'studies':rows,'disease_hierarchy_applied':True,'disease_hierarchy_parent_key':parent_key,
        'disease_hierarchy_sources':[scopes[key]['disease'] for key,_,_ in valid],
        'disease_hierarchy_inherited_count':sum(r['disease_hierarchy_inherited'] for r in rows),
        'disease_hierarchy_pooling_count':sum(r['disease_hierarchy_pooling_selected'] for r in rows),
        'disease_hierarchy_method':'Eligible child evidence belongs to the selected general category. Parent-direct estimates are preferred within each overlapping study/population; otherwise one adjusted/primary child estimate is selected. Disjoint populations require explicit documentation.',
        'plot_url':None,'funnel_plot_url':None,'baujat_plot_url':None})
    if not base:result['screening_stats']={'total_fetched':f'Saved {track} subtype evidence','extracted':len(rows),'llm_screened_in':len(rows)}
    return result

def select_frames(frame, all_frame=None):
    """Reapply overlap selection to the current user-selected candidate set."""
    records = frame.to_dict(orient='records')
    if not any(is_projection(row) for row in records): return frame, all_frame
    selected = select_rows(records)
    decisions = {row['disease_hierarchy_projection_id']: row for row in selected if is_projection(row)}
    frame = frame.copy()
    for index, row in frame.iterrows():
        if not is_projection(row): continue
        decision = decisions[row['disease_hierarchy_projection_id']]
        for field in ('disease_hierarchy_pooling_selected','disease_hierarchy_pooling_reason'):
            frame.at[index, field] = decision[field]
    keep = [not is_projection(row) or decisions[row['disease_hierarchy_projection_id']]['disease_hierarchy_pooling_selected'] for row in records]
    frame = frame.loc[keep].copy()
    if all_frame is not None:
        all_frame = all_frame.copy()
        for index, row in all_frame.iterrows():
            if not is_projection(row): continue
            decision = decisions.get(row['disease_hierarchy_projection_id'])
            all_frame.at[index, 'disease_hierarchy_pooling_selected'] = bool(decision and decision['disease_hierarchy_pooling_selected'])
            all_frame.at[index, 'disease_hierarchy_pooling_reason'] = (decision['disease_hierarchy_pooling_reason'] if decision else 'Not in the current eligible or user-selected candidate set.')
    return frame, all_frame
