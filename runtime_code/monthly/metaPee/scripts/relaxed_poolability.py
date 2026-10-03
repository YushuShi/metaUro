"""Offline, auditable quantitative completion under the 2026-09-05 protocol.

No source excerpts are invented and no eligibility/JBI decisions are changed.
The saved raw extraction remains the source; these are effective assumptions.
"""
from __future__ import annotations
import copy
import math
import re
from statistics import NormalDist
from scripts import p_value_guard

VERSION = 'relaxed-poolability-2026-09-05-v2'
Z95 = NormalDist().inv_cdf(.975)
POLICY_INSTRUCTIONS = '''
QUANTITATIVE POOLABILITY POLICY (2026-09-05, takes precedence over older field requirements):
Total sample size is required; exact cases are optional and must not be invented.
Exposure unit and scale, adjustment model, subgroup/stratum and direct exposure
measurement may be null/NA. Missing optional fields never alone require R3.
For RR/OR/HR/IRR, a CI OR an exact two-sided p-value for the selected estimate
suffices. Preserve inequalities; never turn a bound, trend, interaction or
one-sided p into an exact coefficient p. Code completes Wald uncertainty.
Assume an unstated CI level is 95%. When only CI bounds are available code uses
the geometric midpoint on the ratio scale. For a CI reported on the log-ratio
scale, return exponentiated bounds (and exponentiated midpoint if no estimate).
Leave absent direction null; code assumes higher versus lower or yes versus no
and marks it imputed in the UI. Preserve explicit direction, including inversion.
aRR/ARR, aOR/AOR and aHR/AHR are adjusted RR, OR and HR. Prefer the adjusted
estimate over the raw estimate only for the same outcome, exposure, comparison,
time and stratum; do not mix numerical fields from different estimates.
R3 runs only when quantitative fields are still inadequate after these
assumptions or the existing JBI quality rule requires full-text review.
All relevance, study design, endpoint and JBI rules remain in force.
'''


def number(value):
    if isinstance(value, bool): return None
    try: v=float(str(value).replace(',','').strip())
    except (TypeError, ValueError): return None
    return v if math.isfinite(v) else None


def canonical_ratio(value):
    label=re.sub(r'[^A-Z]+',' ',str(value or '').upper()).strip()
    if label in {'AOR','ARR','AHR','AIRR'}: return label[1:]
    label=re.sub(r'\b(?:ADJUSTED|MULTIVARIABLE|MULTIVARIATE|FULLY|CRUDE|UNADJUSTED)\b','',label).strip()
    return {'RR':'RR','RISK RATIO':'RR','RELATIVE RISK':'RR','OR':'OR','ODDS RATIO':'OR','HR':'HR','HAZARD RATIO':'HR','IRR':'IRR','INCIDENCE RATE RATIO':'IRR','RATE RATIO':'IRR','RRR':'RR'}.get(label)


def is_adjusted(record):
    label=str(record.get('reported_effect_type') or record.get('effect_type') or '')
    model=str(record.get('adjusted_model') or record.get('adjustment_model') or '')
    return bool(record.get('effect_is_adjusted') or re.fullmatch(r'a(?:OR|RR|HR|IRR)',label,re.I) or re.search(r'\badjusted\b', label,re.I) or (model.strip().lower() not in {'','na','n/a','none','unadjusted','crude'}))


def prefer_adjusted(records):
    """Choose adjusted only within identical available non-model identities."""
    if len(records)<2: return records
    fields=('effect_type','pmid','cancer','endpoint','outcome','reported_outcome','exposure','comparison_type','comparison_direction','comparison_group','reference_group','unit','scale','timing','follow_up','gender','smoking_status','stratum','subgroup','exposure_subtype','exposure_measurement_type','exposure_quantification','total_n')
    buckets={}
    for i,r in enumerate(records):
        key=tuple(str(r.get(k) or '').strip().casefold() for k in fields)
        buckets.setdefault(key,[]).append(i)
    selected=[]
    for indices in buckets.values():
        adjusted=[i for i in indices if is_adjusted(records[i])]
        chosen=adjusted[0] if adjusted else indices[0]
        for i in indices:
            r=records[i]
            if adjusted and not is_adjusted(r):
                r['superseded_by_adjusted']=True
                r['adjusted_preference_reason']='Adjusted estimate is available for the same recorded exposure/outcome/contrast/time/stratum.'
            else:
                selected.append((i,r))
    return [r for _,r in sorted(selected)]


def complete(extraction, *, direction=True):
    if not isinstance(extraction,dict): return extraction
    e=extraction
    p_value_guard.retract_trend_ci(e)
    e['poolability_policy_version']=VERSION
    raw=e.get('effect_type'); typ=canonical_ratio(raw)
    if typ:
        e.setdefault('reported_effect_type',raw)
        if is_adjusted(e): e['effect_is_adjusted']=True
        e['effect_type']=typ
    else:
        return e  # No standardized, beta or continuous effect is relabelled a ratio.
    # Replaying a fully inverted bundle must retain its exact numerical identity.
    frozen=e.get('inversion_applied') is True
    lo,hi=number(e.get('ci_lower')),number(e.get('ci_upper'))
    scale=str(e.get('ci_scale') or e.get('confidence_interval_scale') or '').lower()
    if not frozen and lo is not None and hi is not None and lo<hi and scale in {'log','ln','log_ratio','log scale','log-ratio'}:
        try: newlo,newhi=math.exp(lo),math.exp(hi)
        except OverflowError: newlo=newhi=None
        if newlo and newhi and math.isfinite(newhi):
            e['ci_original_log_bounds']=[lo,hi]
            e['ci_lower'],e['ci_upper']=lo,hi=newlo,newhi
            e['ci_scale']='ratio'
            e['ci_scale_conversion']='Exponentiated reported log-ratio CI bounds'
    valid_ci=lo is not None and hi is not None and 0<lo<hi
    level=number(e.get('ci_confidence_level'))
    if level is None:
        ci_evidence=((e.get('evidence') or {}).get('confidence_interval') or {})
        stated=re.search(r'(?<![\d.])(\d{2}(?:\.\d+)?)\s*%\s*(?:CI|confidence|confidence interval)',str(ci_evidence.get('excerpt') or ''),re.I)
        if stated:
            level=number(stated.group(1));e['ci_confidence_level']=level
            e['ci_confidence_level_source']='Explicit confidence interval evidence'

    if valid_ci and level is None:
        level=95.;e['ci_confidence_level']=95.;e['ci_level_assumed']=True
    if level is not None and 0<level<1: level*=100.;e['ci_confidence_level']=level
    effect=number(e.get('effect_size'))
    if not frozen and effect is not None and str(e.get('effect_scale') or '').lower() in {'log','ln','log_ratio','log scale','log-ratio'}:
        try: ratio=math.exp(effect)
        except OverflowError: ratio=None
        if ratio and math.isfinite(ratio):
            e['effect_original_log_value']=effect;e['effect_size']=effect=ratio
            e['effect_scale']='ratio';e['effect_scale_conversion']='Exponentiated explicitly log-scale effect'

    if not frozen and effect is None and valid_ci:
        effect=math.exp((math.log(lo)+math.log(hi))/2)
        e['effect_size']=effect;e['effect_size_imputed']=True
        e['effect_size_imputation_method']='exp((ln(CI lower) + ln(CI upper))/2), assuming log symmetry'
    if not frozen and valid_ci and level is not None and 0<level<100 and level!=95 and effect is not None and lo<=effect<=hi:
        z=NormalDist().inv_cdf((1+level/100)/2)
        se=(math.log(hi)-math.log(lo))/(2*z)
        try: newlo,newhi=math.exp(math.log(effect)-Z95*se),math.exp(math.log(effect)+Z95*se)
        except OverflowError: newlo=newhi=None
        if newlo and newhi and math.isfinite(newhi):
            e['ci_original_reported_bounds']=[lo,hi];e['ci_original_confidence_level']=level
            e['ci_lower'],e['ci_upper']=lo,hi=newlo,newhi
            e['ci_confidence_level']=level=95.
            e['ci_level_conversion']='Two-sided Wald conversion of reported confidence level to 95% on log scale'
    p=number(e.get('p_value'))
    pe=((e.get('evidence') or {}).get('p_value') or {})
    ptext=' '.join(str(v or '') for v in (e.get('p_value'),e.get('p_value_relation'),e.get('p_value_reported_as'),pe.get('excerpt')))
    exact=bool(not p_value_guard.trend_evidence(e) and p is not None and 0<p<1 and not re.search(r'[<>≤≥≈~]|\b(?:trend|interaction|heterogeneity|homogeneity|global|nonlinearity|curvature|likelihood[- ]ratio|log[- ]rank|joint[- ]test|ANOVA|chi[- ]square|LRT|under|over|approximately|about|not exceeding|did not exceed|does not exceed|not significant|one[- ](?:sided|tailed))\b',ptext,re.I) and str(e.get('p_value_relation') or '').strip().lower() in {'', '=', 'eq', 'equal', 'exact'} and e.get('p_value_is_exact') is not False and e.get('p_value_targets_selected_estimate') is not False and e.get('wald_inputs_same_estimate') is not False)
    coherent=e.get('wald_inputs_same_estimate') is not False and e.get('ci_targets_selected_estimate') is not False
    if not frozen and not valid_ci and e.get('ci_lower') is None and e.get('ci_upper') is None and effect is not None and effect>0 and effect!=1 and exact:
        z=-NormalDist().inv_cdf(max(p/2,5e-324))
        se=abs(math.log(effect))/z
        try: lower,upper=math.exp(math.log(effect)-Z95*se),math.exp(math.log(effect)+Z95*se)
        except OverflowError: lower=upper=None
        if lower and upper and math.isfinite(upper):
            e.update(ci_lower=lower,ci_upper=upper,ci_confidence_level=95.,standard_error=se,ci_method='95% Wald CI from effect and two-sided p value on the log scale',ci_source_p_value=p,ci_source_p_value_role='exact_two_sided_target_effect',ci_imputed=True)
            lo,hi,level,valid_ci=lower,upper,95.,True
    if valid_ci and level and 0<level<100 and effect is not None and lo<=effect<=hi and coherent:
        z=NormalDist().inv_cdf((1+level/100)/2)
        se=(math.log(hi)-math.log(lo))/(2*z)
        if not frozen and e.get('p_value') is None:
            p=math.erfc(abs(math.log(effect))/se/math.sqrt(2))
            # A positive machine number is required by the existing identity contract.
            e['p_value']=max(p,5e-324)
            e['p_value_method']='two-sided Wald test from effect and 95% CI on the log scale' if level==95 else f'two-sided Wald test from effect and {level:g}% CI on the log scale'
            e['p_value_relation']=None;e['p_source_ci_role']='approximately_log_symmetric_95_percent_target_effect' if level==95 else f'{level:g}_percent_target_effect'
            e['p_value_imputed']=True;e['standard_error']=se
    if direction and not frozen and not e.get('direction_imputed'):
        comparison=str(e.get('comparison_type') or '').replace('_', ' ')
        d=str(e.get('comparison_direction') or '')
        explicit=d in {'higher_vs_lower','lower_vs_higher','per_unit_increase','yes_vs_no'} or bool(comparison and not re.search(r'^(?:unclear|unknown|unspecified|not (?:reported|stated|specified)|NA)$|(?:direction|referen[ct]e|comparator) (?:unclear|unknown|unspecified|not (?:reported|stated|specified))|(?:vs\.?|versus) (?:an? )?(?:unknown|unspecified|unstated)',comparison,re.I) and re.search(r'\b(?:versus|vs\.?|per|increase|decrease|increment|compared|relative)\b',comparison,re.I))
        if not explicit:
            binary=bool(re.search(r'\b(?:yes|no|binary|ever|never|users?|nonusers?|positive|negative|exposed|unexposed)\b',comparison,re.I))
            e.setdefault('direction_original_comparison',e.get('comparison_type'))
            e.setdefault('direction_original_value',e.get('comparison_direction'))
            e.update(comparison_type='Yes versus no' if binary else 'High versus low',comparison_direction='higher_vs_lower',needs_inversion=False,direction_imputed=True,direction_imputation_reason='Protocol default yes versus no' if binary else 'Protocol default high versus low')
        else:
            e['direction_imputed']=False
            if d not in {'higher_vs_lower','lower_vs_higher','per_unit_increase'}:
                if e.get('needs_inversion') is True: e['comparison_direction']='lower_vs_higher'
                elif re.search(r'\bper\b|increment|increase',comparison,re.I): e['comparison_direction']='per_unit_increase'
                elif re.search(r'\b(?:low|lowest|lower|no|never|nonusers?)\b.*\b(?:versus|vs|compared)\b.*\b(?:high|highest|higher|yes|users?)\b',comparison,re.I): e['comparison_direction']='lower_vs_higher'
                else: e['comparison_direction']='higher_vs_lower'
        if e.get('comparison_direction') in {'higher_vs_lower','lower_vs_higher'}:
            reverse=e['comparison_direction']=='lower_vs_higher'
            for k,v in [('comparison_group','lower' if reverse else 'higher'),('reference_group','higher' if reverse else 'lower')]:
                if not e.get(k): e[k]=v;e['comparison_labels_imputed']=True
        e.setdefault('needs_inversion',False)
    return e
