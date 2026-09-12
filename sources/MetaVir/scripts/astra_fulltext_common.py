"""Validation shared by offline PMC-text Astra combined-result overlays.

This module makes no network or model calls. Numeric replacements are atomic,
and every decision is bound to both immutable screening records and PMC text.
"""
from __future__ import annotations
import copy
import ast
import hashlib
import json
import math
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from statistics import NormalDist
from scripts import key_comparison_policy as policy
from scripts import relaxed_poolability as relaxed

VERSION = 'astra-pmc-fulltext-2026-09-07-v1'
SOURCE_SCOPE = 'PMC_FULL_TEXT_TEXT_ONLY'
MODEL = 'gpt-6-astra'
CHECKPOINT_NAME = 'astra_fulltext/current_decisions.json'
NUMERIC_FIELDS = ('total_n', 'effect_size', 'ci_lower', 'ci_upper', 'p_value')
EXTRACTION_FIELDS = set(NUMERIC_FIELDS) | {
    'effect_type','reported_effect_type','ci_level','ci_confidence_level','ci_scale','effect_scale','cases',
    'p_value_relation','p_value_is_exact','p_value_targets_selected_estimate','ci_targets_selected_estimate',
    'wald_inputs_same_estimate','comparison_type','comparison_direction','comparison_group','reference_group',
    'population','stratum','subgroup','gender','effect_is_adjusted','adjusted_model','adjustment_model',
    'exposure_measurement_type','exposure_subtype','unit','scale','timing','follow_up','country','continent',
    'design','study_design','total_n_basis','direction_imputed','ci_level_assumed','derivations',
    'independent_population_id','p_value_method','p_value_imputed','ci_imputed','effect_size_imputed',
    'needs_inversion','inversion_applied','inversion_provenance','reported_comparison_direction',
    'ci_original_reported_bounds','ci_original_confidence_level','ci_level_conversion',
    'effect_size_imputation_method','ci_method','standard_error','ci_scale_conversion','effect_scale_conversion',
    'ci_original_log_bounds','effect_original_log_value','direction_imputation_reason',
    'comparison_labels_imputed','comparison_direction_imputed','reported_extraction',
}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def source_digest(a,b): return digest({'chatgpt':a,'claude_gemini':b})


def source_value_equal(claimed,actual,kind='ratio'):
    """Retain exact identity of malformed source arrays until they are repaired."""
    if policy.numeric_equal(claimed,actual,kind):return True
    if isinstance(actual,(list,dict)):
        value=claimed
        if isinstance(value,str):
            try:value=json.loads(value)
            except (ValueError,TypeError):
                try:value=ast.literal_eval(value)
                except (ValueError,SyntaxError):return False
        return digest(value)==digest(actual)
    return False


def is_human(record):
    if not record:return False
    authority=str(record.get('decision_authority') or '').casefold()
    if authority in {'human','manual','human_manual_review'}:return True
    if any(record.get(key) for key in ('human_adjudication','manual_review','manual_full_text_adjudication',
        'combined_manual_authority','manual_adjudication','manual_full_text_review')):return True
    value=str((record.get('combined_adjudication') or {}).get('resolution') or '').upper()
    return 'PENDING' not in value and any(word in value for word in ('HUMAN','MANUAL'))


def pair_index(entry):
    value=(entry.get('source_row') or {}).get('Estimate pair')
    try:
        number=float(value)
        return int(number)-1 if number.is_integer() and number>=1 else -1
    except (TypeError,ValueError):return -1


def valid_partial(extraction):
    values={key:policy.number(extraction.get(key)) for key in NUMERIC_FIELDS}
    if any(not policy.missing(extraction.get(key)) and value is None for key,value in values.items()):return False
    n,e,lo,hi,p=[values[key] for key in NUMERIC_FIELDS]
    if n is not None and (n<=0 or n!=int(n)):return False
    if lo is not None and hi is not None and lo>=hi:return False
    if e is not None and ((lo is not None and lo>e) or (hi is not None and hi<e)):return False
    if p is not None and not 0<=p<=1:return False
    level=policy.number(extraction.get('ci_confidence_level',extraction.get('ci_level')))
    if level is not None and not 0<level<100:return False
    if policy.wald_scale(extraction)=='log_ratio' and any(v is not None and v<=0 for v in (e,lo,hi)):return False
    return True


def coherent(extraction):
    return bool(extraction and valid_partial(extraction) and all(policy.number(extraction.get(key)) is not None for key in policy.KEY_FIELDS)
                and policy.wald_scale(extraction) is not None)


def preserve_scientific_gates(extraction,originals):
    """Keep independent same-pair restrictions, never stale numeric-gap holds."""
    result=copy.deepcopy(extraction)
    originals=[row for row in originals if isinstance(row,dict)]
    for key in ('manual_poolable','included_in_parent_meta_analysis','pool_in_parent_meta_analysis'):
        if any(row.get(key) is False for row in originals):result[key]=False
    if result.get('pool_in_parent_meta_analysis') is False:result['included_in_parent_meta_analysis']=False
    for key in ('full_text_pooling_block','exposure_classification_ambiguous','hierarchy_inherited','broader_outcome_projection'):
        if any(row.get(key) for row in originals):result[key]=True
    for key in ('manual_overlap_role','overlap_group','manual_exclusion_reason','manual_pooling_restriction'):
        values=[row[key] for row in originals if not policy.missing(row.get(key))]
        if values:result[key]=copy.deepcopy(values[0])
    return result


def normalize_extraction(extraction):
    result={key:copy.deepcopy(value) for key,value in (extraction or {}).items() if key in EXTRACTION_FIELDS}
    for key in NUMERIC_FIELDS:
        value=policy.number(result.get(key))
        result[key]=float(value) if value is not None else None
    if result.get('total_n') is not None and result['total_n'].is_integer():result['total_n']=int(result['total_n'])
    if result.get('ci_confidence_level') is None and result.get('ci_level') is not None:
        result['ci_confidence_level']=result['ci_level']
    # Repository policy versions differ. Perform the scale/CI-level conversion
    # uniformly here before invoking each repository's receipt/direction rules.
    ratio=relaxed.canonical_ratio(result.get('effect_type'))
    if ratio and not result.get('inversion_applied'):
        lo,hi=result.get('ci_lower'),result.get('ci_upper');effect=result.get('effect_size')
        if lo is not None and hi is not None and lo<hi and result.get('ci_scale') in {'log','ln','log_ratio','log scale','log-ratio'}:
            result['ci_original_log_bounds']=[lo,hi]
            try:lo,hi=math.exp(lo),math.exp(hi)
            except OverflowError:lo=hi=None
            result.update(ci_lower=lo,ci_upper=hi,ci_scale='ratio',ci_scale_conversion='Exponentiated reported log-ratio CI bounds')
        if effect is not None and result.get('effect_scale') in {'log','ln','log_ratio','log scale','log-ratio'}:
            result['effect_original_log_value']=effect
            try:effect=math.exp(effect)
            except OverflowError:effect=None
            result.update(effect_size=effect,effect_scale='ratio',effect_scale_conversion='Exponentiated explicitly log-scale effect')
        if lo is not None and hi is not None and 0<lo<hi:
            if effect is None:
                effect=math.exp((math.log(lo)+math.log(hi))/2)
                result.update(effect_size=effect,effect_size_imputed=True,effect_size_imputation_method='exp((ln(CI lower) + ln(CI upper))/2), assuming log symmetry')
            level=policy.number(result.get('ci_confidence_level'))
            level=float(level) if level is not None else 95.0
            if 0<level<1:level*=100
            if result.get('ci_confidence_level') is None:result['ci_level_assumed']=True
            result['ci_confidence_level']=level
            if 0<level<100 and level!=95 and lo<=effect<=hi:
                se=(math.log(hi)-math.log(lo))/(2*NormalDist().inv_cdf((1+level/100)/2))
                z=NormalDist().inv_cdf(.975)
                result.update(ci_original_reported_bounds=[lo,hi],ci_original_confidence_level=level,
                    ci_lower=math.exp(math.log(effect)-z*se),ci_upper=math.exp(math.log(effect)+z*se),
                    ci_confidence_level=95.0,ci_level=95.0,
                    ci_level_conversion='Two-sided Wald conversion of reported confidence level to 95% on log scale')
    direction=result.get('comparison_direction')
    if direction in {'no_vs_yes','per_unit_decrease','yes_vs_no','per_unit_increase'}:
        result.setdefault('reported_comparison_direction',direction)
        result['comparison_direction']='lower_vs_higher' if direction in {'no_vs_yes','per_unit_decrease'} else 'higher_vs_lower'
    if result.get('comparison_direction')=='unclear':result['comparison_direction']=None
    if not result.get('comparison_direction'):
        result.pop('direction_imputed',None)
    if re.search(r'\b(?:trend|interaction|heterogeneity|global|nonlinearity|one[- ]sided|likelihood|log[- ]rank)\b',str(result.get('p_value_method') or ''),re.I):
        result['p_value_targets_selected_estimate']=False
    # The ratio policy supplies only the explicitly authorized deterministic
    # imputations. A p bound is never silently treated as an exact p value.
    relaxed.complete(result)
    if result.get('comparison_direction')=='lower_vs_higher' and not result.get('inversion_applied'):
        result['needs_inversion']=True
    result['astra_fulltext_quantitative_complete']=coherent(result)
    return result


def canonical_numeric_snapshot(extraction):
    """Compare numeric keys in the same greater/yes direction used by the UI."""
    normalized=normalize_extraction(extraction)
    values={key:normalized.get(key) for key in policy.KEY_FIELDS}
    original=extraction or {}
    needs=original.get('needs_inversion') is True or normalized.get('needs_inversion') is True
    already=original.get('inversion_applied') is True or original.get('combined_source_inversion_applied') is True
    if relaxed.canonical_ratio(normalized.get('effect_type')) and needs and not already:
        numbers=[policy.number(values.get(key)) for key in ('effect_size','ci_lower','ci_upper')]
        if all(value is not None and value>0 for value in numbers):
            effect,lo,hi=map(float,numbers)
            values.update(effect_size=1/effect,ci_lower=1/hi,ci_upper=1/lo)
    return values


def _metadata_key(value):
    if isinstance(value,(dict,list)):return digest(value)
    return re.sub(r'[^a-z0-9]+','',str(value).casefold())


def fill_compatible_metadata(extraction,pair,decision,records=None):
    """Fill optional metadata without changing any validated estimand or number."""
    result=copy.deepcopy(extraction);fills=[]
    invalidated=set(decision.get('invalidated_metadata_fields') or [])
    intrinsic=('design','study_design','country','continent')
    tied=('unit','scale','exposure_measurement_type','exposure_subtype','timing','follow_up','adjusted_model','adjustment_model')
    identity=('comparison_type','comparison_direction','comparison_group','reference_group','population','gender','stratum','subgroup','timing','follow_up','country','exposure_measurement_type','exposure_subtype')
    donors={track:copy.deepcopy(pair.get(track) or {}) for track in ('chatgpt','claude_gemini')}
    for track,donor in donors.items():
        for key in intrinsic:
            if policy.missing(donor.get(key)) and not policy.missing(((records or {}).get(track) or {}).get(key)):
                donor[key]=copy.deepcopy(records[track][key])
    compatible={}
    for track,donor in donors.items():
        match=bool(pair.get(track) is not None and coherent(result) and coherent(normalize_extraction(donor)) and
                   not policy.key_differences(canonical_numeric_snapshot(result),canonical_numeric_snapshot(donor)))
        actual_type=relaxed.canonical_ratio(donor.get('effect_type')) or _metadata_key(donor.get('effect_type'))
        current_type=relaxed.canonical_ratio(result.get('effect_type')) or _metadata_key(result.get('effect_type'))
        match=match and actual_type==current_type
        if any(not policy.missing(result.get(key)) and not policy.missing(donor.get(key)) and
               _metadata_key(result[key])!=_metadata_key(donor[key]) for key in identity):match=False
        compatible[track]=match
    for key in (*intrinsic,*tied):
        if key in invalidated or not policy.missing(result.get(key)):continue
        if key in {'design','study_design'} and any(not policy.missing(result.get(alias)) for alias in {'design','study_design'}-{key}):continue
        if key in {'adjusted_model','adjustment_model'} and any(not policy.missing(result.get(alias)) for alias in {'adjusted_model','adjustment_model'}-{key}):continue
        choices=[]
        for track,donor in donors.items():
            value=donor.get(key)
            if policy.missing(value) or (key in tied and not compatible[track]):continue
            if key in intrinsic and any(not policy.missing(result.get(field)) and not policy.missing(donor.get(field)) and _metadata_key(result[field])!=_metadata_key(donor[field]) for field in ('population','gender','stratum','subgroup')):continue
            if key=='continent' and not policy.missing(result.get('country')) and (policy.missing(donor.get('country')) or _metadata_key(result['country'])!=_metadata_key(donor['country'])):continue
            if key in {'adjusted_model','adjustment_model'} and result.get('effect_is_adjusted') is not True:continue
            choices.append((track,value))
        if not choices or len({_metadata_key(value) for _,value in choices})!=1:continue
        value=copy.deepcopy(choices[0][1]);result[key]=value
        fills.append({'field':key,'value':copy.deepcopy(value),'sources':[track for track,_ in choices],
                      'rule':'Agreed intrinsic study metadata' if key in intrinsic else 'Same-pair canonical numeric agreement and compatible recorded estimand'})
        evidence=(donors[choices[0][0]].get('evidence') or {}).get(key)
        if evidence and key not in (result.get('evidence') or {}):
            copied=copy.deepcopy(evidence)
            if isinstance(copied,dict):copied['copied_from_pipeline']=choices[0][0]
            result.setdefault('evidence',{})[key]=copied
    if fills:result['astra_fulltext_metadata_fills']=fills
    return result


@lru_cache(maxsize=256)
def _verified_text(path,checksum,mtime_ns,size):
    data=Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest()!=checksum:raise ValueError('PMC text SHA256 differs from the adjudicated source.')
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',data.decode('utf-8'))).strip()


def validate_entry(entry,a,b):
    """Return (atomic normalized extraction, error); (None,None) is exclusion."""
    decision=entry.get('adjudication') or {};row=entry.get('source_row') or {}
    if entry.get('id')!=decision.get('id'):return None,'Decision and original review IDs differ.'
    if entry.get('source_fingerprint')!=source_digest(a,b):return None,'Screening source fingerprint changed; full-text decision needs review.'
    context=a or b or {}
    expected={'Context ID':context.get('context_id'),'PMID':context.get('pmid'),'Exposure':context.get('exposure'),
              'Disease':context.get('disease') or context.get('cancer'),
              'Endpoint':context.get('endpoint') or context.get('outcome') or 'Incidence'}
    for field,value in expected.items():
        if field in row and not policy.missing(row[field]) and str(row[field])!=str(value):
            return None,'Original review '+field+' does not match the screening context.'
    if decision.get('confidence')!='high':return None,'Full-text confidence is below high.'
    if decision.get('source_quote_validated') is not True:return None,'PMC full-text evidence quotes were not validated.'
    if decision.get('verdict') not in {'CORRECTED','CHATGPT','CLAUDE_GEMINI','BOTH'}:return None,'Full text did not uniquely settle this estimate.'
    eligibility=decision.get('eligibility')
    if eligibility not in {'INCLUDE','EXCLUDE'}:return None,'Full-text eligibility remains unresolved.'
    source=decision.get('source') or entry.get('source') or {}
    if str(source.get('pmid'))!=str(row.get('PMID')) or not re.fullmatch(r'PMC\d+',str(source.get('pmcid') or ''),re.I):
        return None,'PMC source identity does not match the review article.'
    if not re.fullmatch(r'[0-9a-f]{64}',str(source.get('text_sha256') or '')):return None,'PMC text checksum is missing.'
    path=Path(source.get('text_path') or '')
    if path.suffix.casefold() not in {'.txt','.text'} or not path.is_file():return None,'A verified PMC text file is required; PDF files are not accepted.'
    try:
        stat=path.stat();text=_verified_text(str(path.resolve()),source['text_sha256'],stat.st_mtime_ns,stat.st_size)
    except (OSError,UnicodeError,ValueError) as exc:return None,str(exc)
    evidence=decision.get('evidence') or []
    if not evidence:return None,'No supporting PMC full-text evidence was supplied.'
    covered=set()
    for item in evidence:
        quote=re.sub(r'\s+',' ',unicodedata.normalize('NFKC',str(item.get('quote') or ''))).strip()
        if not quote or quote not in text:return None,'An evidence quote is absent from the verified PMC text.'
        covered.update(item.get('fields') or [])
    # Check identity and source indices against current saved candidates. Both
    # tracks, including a source rejected by the model, stay immutable.
    comparison=policy.compare_context(a,b);index=pair_index(entry)
    if not 0<=index<len(comparison['pairs']):return None,'Original estimate pair is absent from current source comparison.'
    pair=comparison['pairs'][index]
    for track,prefix in [('chatgpt','ChatGPT'),('claude_gemini','Claude+Gemini')]:
        ex=pair.get(track);reported=row.get(prefix+' estimate index')
        if not policy.missing(reported):
            actual=pair.get(track+'_index')
            if not policy.numeric_equal(reported,actual,'count'):return None,'Source estimate index differs from the original discrepancy row.'
        for key,suffix in [('total_n','sample size'),('effect_size','effect size'),('ci_lower','CI lower'),('ci_upper','CI upper')]:
            column=prefix+' '+suffix
            if column in row and not source_value_equal(row[column],(ex or {}).get(key),'count' if key=='total_n' else 'ratio'):
                return None,'A source numeric bundle changed since the original discrepancy row.'
    if eligibility=='EXCLUDE':return None,None
    proposed=decision.get('extraction')
    if not isinstance(proposed,dict):return None,'The full-text decision lacks an atomic extraction bundle.'
    reported=decision.get('reported_extraction') or proposed
    supplied_level=reported.get('ci_confidence_level',reported.get('ci_level'))
    if not policy.missing(supplied_level):
        level=policy.number(supplied_level)
        if level is None or not 0<level<100:return None,'Explicit confidence level is invalid.'
        percent=float(level)*100 if level<1 else float(level)
        if percent!=95:
            supported=False
            for item in evidence:
                fields=set(item.get('fields') or [])
                if not fields.intersection({'ci_lower','ci_upper','ci_level','ci_confidence_level','confidence_interval'}):continue
                quote=unicodedata.normalize('NFKC',str(item.get('quote') or ''))
                label=r'(?:CIs?\b|confidence\s+(?:intervals?|limits?|bounds?|level|coefficient))'
                percentages=re.findall(r'(?<![\d.])(\d+(?:\.\d+)?)\s*(?:%|percent)\s*[()\s-]*'+label,quote,re.I)
                percentages+=re.findall(label+r'\s*(?:of|at)?\s*[:=(]*\s*(\d+(?:\.\d+)?)\s*(?:%|percent)',quote,re.I)
                supported|=any(policy.numeric_equal(value,percent,'count') for value in percentages)
                fractions=re.findall(r'confidence\s+(?:level|coefficient)\s*(?:of|at)?\s*[:=(]*\s*(0?\.\d+)(?!\d)',quote,re.I)
                supported|=any(policy.numeric_equal(value,percent/100,'count') for value in fractions)
            if not supported:return None,'Explicit non-95% CI level is not supported by the quoted CI evidence.'
    if reported.get('ci_targets_selected_estimate') is False and any(not policy.missing(reported.get(key)) for key in ('ci_lower','ci_upper')):
        return None,'Reported confidence interval does not target the selected estimate.'
    for key in NUMERIC_FIELDS:
        if not policy.missing(reported.get(key)) and key not in covered:
            return None,'PMC evidence does not identify the reported '+key+' input.'
    result=normalize_extraction(proposed)
    if not valid_partial(result):return None,'The full-text numeric bundle contains incoherent values.'
    if not any(policy.number(result.get(key)) is not None for key in policy.KEY_FIELDS):return None,'No supported quantitative value was recovered.'
    # Inputs reported separately by the validator permit deterministic
    # CI/point/Wald completion, but never an unexplained numerical rewrite.
    if decision.get('reported_extraction'):
        normalized=normalize_extraction(reported)
        for key in policy.KEY_FIELDS:
            if not policy.numeric_equal(normalized.get(key),result.get(key),'count' if key=='total_n' else 'ratio'):
                return None,'Canonical numeric bundle is not reproduced from the reported inputs.'
    result['evidence']={field:{'source':'pmc_full_text','excerpt':item['quote'],'url':source.get('url'),
                             'section':item.get('section'),'pmcid':source['pmcid'],'source_sha256':source['text_sha256']}
                        for item in evidence for field in item.get('fields',[])}
    if result['evidence'].get('total_n'):
        result['evidence']['sample_size']=copy.deepcopy(result['evidence']['total_n'])
    ci_evidence=[result['evidence'].get(key) for key in ('ci_lower','ci_upper') if result['evidence'].get(key)]
    if ci_evidence:
        result['evidence']['confidence_interval']={**copy.deepcopy(ci_evidence[0]),
            'excerpt':' '.join(dict.fromkeys(item['excerpt'] for item in ci_evidence))}
    result['astra_fulltext_provenance']={'id':entry['id'],'model':MODEL,'source_scope':SOURCE_SCOPE,
        'source':copy.deepcopy(source),'source_fingerprint':entry['source_fingerprint'],
        'reason':decision.get('reason'),'reported_extraction':copy.deepcopy(reported)}
    result['astra_fulltext_numeric_matches_sources']={track:bool(pair.get(track) is not None and
        not policy.key_differences(canonical_numeric_snapshot(result),canonical_numeric_snapshot(pair[track])))
        for track in ('chatgpt','claude_gemini')}
    result=fill_compatible_metadata(result,pair,decision,{'chatgpt':a,'claude_gemini':b})
    return result,None


def packet_entries(packet):
    if packet.get('model')!=MODEL or packet.get('source_scope')!=SOURCE_SCOPE or packet.get('complete') is not True:
        raise ValueError('A complete exact GPT-6 Astra PMC text-only packet is required.')
    entries=packet.get('results')
    if not isinstance(entries,list):raise ValueError('Full-text packet results must be an array.')
    ids=[entry.get('id') for entry in entries]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate review IDs in full-text packet.')
    return entries
