"""Read-only hierarchy projection. Screening/measurement decisions remain unchanged."""
import copy
import hashlib
import json
import math
import re
from collections import defaultdict


def key(value):
    return re.sub(r'[^a-z0-9]+','_',str(value or '').casefold()).strip('_')


def include_mode(value=None):
    if value is None:return True
    if isinstance(value,bool):return value
    if str(value).strip().casefold() in {'yes','true','1'}:return True
    if str(value).strip().casefold() in {'no','false','0'}:return False
    raise ValueError('Include subexposures must be Yes or No.')


def descendants(target,hierarchy):
    """Transitive child closure; deduplicate diamonds and reject cycles."""
    children=defaultdict(set); labels={key(target):str(target)}
    for child,parents in hierarchy.items():
        labels[key(child)]=child
        for parent in parents:
            labels[key(parent)]=parent;children[key(parent)].add(key(child))
    seen=set();active=set()
    def visit(node):
        if node in active:raise ValueError(f'Exposure hierarchy cycle at {labels.get(node,node)}')
        if node in seen:return
        active.add(node)
        for child in sorted(children[node]):visit(child)
        active.remove(node);seen.add(node)
    visit(key(target));seen.discard(key(target))
    return [labels[node] for node in sorted(seen)]


def row_direct_exposure(row,source):
    return row.get('Direct Exposure') or row.get('direct_exposure') or source


def direct_rows(rows,source):
    """Strip previously projected child rows from each source cache."""
    return [copy.deepcopy(r) for r in rows if key(row_direct_exposure(r,source))==key(source)]


def row_identity(row,source):
    identity=[str(row.get('PMID') or ''),key(row_direct_exposure(row,source)),
              row.get('Record ID') or row.get('Effect ID') or row.get('record_id'),
              row.get('Effect Type'),row.get('Effect Size'),row.get('Lower CI'),row.get('Upper CI'),
              row.get('comparison_type'),row.get('Gender'),row.get('Sample Size'),row.get('Cases')]
    return hashlib.sha256(json.dumps(identity,sort_keys=True,default=str).encode()).hexdigest()


def eligible_for_selection(row):
    grade = str(row.get('Quality Score') or '').strip().casefold()
    fair_opt_in = grade == 'fair' and row.get('fair_quality_opted_in') is True
    if grade not in {'good','moderate'} and not fair_opt_in:return False
    if row.get('display_only') is True:return False
    try:
        n,e,lo,hi=(float(str(row.get(k)).replace(',','')) for k in ['Sample Size','Effect Size','Lower CI','Upper CI'])
        if not all(math.isfinite(x) for x in [n,e,lo,hi]) or n<=0 or n!=int(n) or not lo<=e<=hi or lo>=hi:return False
        if str(row.get('Effect Type') or '').upper() in {'RR','IRR','OR','HR'} and lo<=0:return False
    except (TypeError,ValueError):return False
    if any(row.get(k) is False for k in ['policy_poolable','manual_poolable','manual_pooling_eligible','Primary Estimate']):return False
    if row.get('final_status') in {'EXCLUDE','ELIGIBLE_NOT_POOLABLE','ELIGIBLE_SUBTYPE_REVIEW'} and not (fair_opt_in and row.get('final_status') == 'ELIGIBLE_NOT_POOLABLE'):return False
    return True


def project_rows(sources,target,hierarchy,include=True,eligible=eligible_for_selection):
    """All direct child evidence is visible; correlated sibling exposures pool once.

    Prefer an eligible parent-direct source for each PMID. Otherwise choose one
    eligible child source deterministically. Preserve within-source independent
    strata and existing primary/manual flags; do not rewrite scientific gates.
    """
    include=include_mode(include)
    wanted={key(target),*(key(x) for x in descendants(target,hierarchy))} if include else {key(target)}
    rows=[];seen=set()
    for source,source_rows in sorted(sources.items(),key=lambda x:(key(x[0])!=key(target),key(x[0]))):
        if key(source) not in wanted:continue
        for raw in direct_rows(source_rows,source):
            identity=row_identity(raw,source)
            if identity in seen:continue
            seen.add(identity);row=copy.deepcopy(raw)
            row['Direct Exposure']=row_direct_exposure(row,source)
            row['Displayed Exposure']=target
            row['subexposure_source']=source
            row['subexposure_inherited']=key(source)!=key(target)
            row['include_subexposures']='yes' if include else 'no'
            row['subexposure_projection_id']=identity
            rows.append(row)
    winners={}
    for row in rows:
        # If PMID is absent, do not conflate unrelated unidentified studies.
        population=str(row.get('PMID') or row['subexposure_projection_id'])
        if eligible(row):winners.setdefault(population,key(row['subexposure_source']))
    selected_ids=set()
    groups=defaultdict(list)
    for row in rows:
        population=str(row.get('PMID') or row['subexposure_projection_id'])
        if winners.get(population)==key(row['subexposure_source']) and eligible(row):groups[population].append(row)
    for candidates in groups.values():
        # Preserve only explicitly disjoint populations, never infer independence from N.
        sexes={str(r.get('Gender') or '').strip().casefold() for r in candidates}
        disjoint_sexes=bool(sexes) and sexes.issubset({'male','female','men','women'})
        all_populations_explicit = all(r.get('independent_population_id') or r.get('Independent Population ID') for r in candidates)
        strata={}
        for row in sorted(candidates,key=lambda r:(not (r.get('effect_is_adjusted') is True or r.get('effect_adjusted') is True or r.get('Effect Is Adjusted') is True),r.get('Primary Estimate') is not True)):
            population_id=(row.get('independent_population_id') or row.get('Independent Population ID')) if all_populations_explicit else None
            sex=str(row.get('Gender') or '').strip().casefold()
            stratum=('population',str(population_id)) if population_id else ('sex', {'men':'male','women':'female'}.get(sex,sex)) if disjoint_sexes else ('study','all')
            strata.setdefault(stratum,row)
        selected_ids.update(r['subexposure_projection_id'] for r in strata.values())
    for row in rows:
        population=str(row.get('PMID') or row['subexposure_projection_id'])
        selected=row['subexposure_projection_id'] in selected_ids
        row['subexposure_pooling_selected']=selected
        if not selected and eligible(row):
            row['subexposure_pooling_reason']='Another exposure estimate from this study is selected to avoid counting the same population twice.'
    return rows


def pooling_rows(rows):
    """Filter the mode's candidates, preserving all prior clinical/manual gates."""
    return [r for r in rows if r.get('subexposure_pooling_selected') is not False and eligible_for_selection(r)]
