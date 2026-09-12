"""Durable, combined-only application of validated title/abstract Astra reviews.

This module never reads article full text or calls a model. The two original
screening tracks and all human/manual decisions remain immutable.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
from scripts import key_comparison_policy as policy

VERSION = 'astra-title-abstract-combined-overlay-2026-09-07-v1'
CHECKPOINT_NAME = 'astra_title_abstract_checkpoint.json'
MANUAL_FIELDS = ('human_adjudication','manual_review','manual_full_text_adjudication',
                 'combined_manual_authority','manual_adjudication')
GATE_FIELDS = ('manual_poolable','policy_poolable','poolable_after_policy','full_text_pooling_block',
               'manual_review_hold','exposure_classification_ambiguous','is_mendelian_randomization',
               'included_in_parent_meta_analysis','pool_in_parent_meta_analysis')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def packet_entries(packet):
    if isinstance(packet,list):return packet
    for key in ('entries','decisions','results','records'):
        if isinstance(packet.get(key),list):return packet[key]
    raise ValueError('Astra checkpoint requires an entries/decisions/results/records array.')


def entry_context(entry):
    row=entry.get('source_row') or entry.get('original_row') or entry.get('csv_row') or {}
    return str(row.get('Context ID') or entry.get('context_id') or ''),row


def is_human(record):
    if not record:return False
    if any(record.get(key) for key in MANUAL_FIELDS):return True
    adjudication=record.get('combined_adjudication') or {}
    return any(word in str(adjudication.get('resolution') or '').upper() for word in ('HUMAN','MANUAL')) and 'PENDING' not in str(adjudication.get('resolution') or '').upper()


def eligible_decision(entry):
    decision=entry.get('adjudication') or {}
    confidence=decision.get('confidence')
    high=str(confidence).casefold()=='high' or (isinstance(confidence,(int,float)) and not isinstance(confidence,bool) and .9<=confidence<=1)
    if not high:return False,'Astra confidence is below high.'
    if decision.get('source_quote_validated') is not True or not str(decision.get('evidence_quote') or '').strip():
        return False,'Title/abstract evidence quote was not independently validated.'
    if str(decision.get('eligibility') or '').upper() not in {'INCLUDE','EXCLUDE'}:
        return False,'Article/exposure eligibility remains unresolved.'
    if str(decision.get('verdict') or '').upper() not in {'A','B','BOTH','NEITHER'}:
        return False,'Astra did not resolve the discrepancy.'
    return True,''


def effect_type(value):
    raw=str(value or '').strip().upper()
    if raw.startswith('ADJUSTED '):raw=raw[9:]
    return {'ARR':'RR','AOR':'OR','AHR':'HR'}.get(raw,raw)


def coherent(extraction):
    if not isinstance(extraction,dict):return False
    n,point,low,high=[policy.number(extraction.get(key)) for key in policy.KEY_FIELDS]
    if n is None or n<=0 or n!=int(n) or any(v is None for v in (point,low,high)):return False
    if not low<high or not low<=point<=high:return False
    scale=policy.wald_scale(extraction)
    return scale is not None and (scale!='log_ratio' or low>0)


def invalid_partial(extraction):
    """Known values must be coherent; absent quantitative fields remain reviewable."""
    values={key:policy.number(extraction.get(key)) for key in policy.KEY_FIELDS}
    for key,value in values.items():
        if not policy.missing(extraction.get(key)) and value is None:return True
    n,point,low,high=[values[key] for key in policy.KEY_FIELDS]
    if n is not None and (n<=0 or n!=int(n)):return True
    if low is not None and high is not None and low>=high:return True
    if point is not None and ((low is not None and point<low) or (high is not None and point>high)):return True
    if policy.wald_scale(extraction)=='log_ratio' and any(value is not None and value<=0 for value in (point,low,high)):return True
    return False


def retained_authority(record):
    if is_human(record):return 'human/manual'
    if record.get('astra_fulltext_adjudication'):return 'Astra PMC full text'
    if record.get('astra_adjudication'):return 'Astra title/abstract'
    adjudication=record.get('combined_adjudication') or {}
    text=json.dumps(adjudication,default=str).upper()
    if 'FABLE' in text or record.get('fable_adjudication') or record.get('combined_adjudicated_fields'):return 'Fable'
    comparison=record.get('key_comparison') or {}
    if 'fable' in str(comparison.get('action') or '').casefold():return 'prior Fable combined result'
    if record.get('key_comparison_pending') or comparison.get('action')=='unresolved_new_key_difference':return 'provisional combined result; unresolved model disagreement'
    return adjudication.get('resolution') or 'prior combined result'


def _bundle_matches(values,extraction,allow_missing_source=False):
    for key in policy.KEY_FIELDS:
        value=values.get('sample_size' if key=='total_n' else key)
        if allow_missing_source and policy.missing(extraction.get(key)):continue
        if not policy.missing(value) and not policy.numeric_equal(value,extraction.get(key),'count' if key=='total_n' else 'ratio'):
            return False
    return True


def apply_entry(previous,tracks,entry,*,protected=False):
    """Return a replacement only for one unambiguous, verified context bundle."""
    cid,source_row=entry_context(entry)
    audit={'id':entry.get('id'),'context_id':cid,'applied':False,'follow_up_required':True,
           'context_status':previous.get('final_status') or previous.get('full_text_status'),
           'context_authority':retained_authority(previous)}
    def hold(reason):audit['reason']=reason;return copy.deepcopy(previous),audit
    if protected or is_human(previous) or any(is_human(row) for row in tracks.values()):
        audit['follow_up_required']=False
        audit['context_authority']='human/manual'
        return hold('Existing human/manual authority preserved.')
    valid,reason=eligible_decision(entry)
    if not valid:return hold(reason)
    if not cid or cid!=str(previous.get('context_id') or ''):return hold('Context identity does not match the saved combined record.')
    pair=str(source_row.get('Estimate pair') or '1').strip()
    if pair not in {'1','1.0'} or any(len(policy.extracts(row))>1 for row in tracks.values() if row):
        return hold('Multiple estimate pairs require an explicit per-estimate application.')
    decision=entry['adjudication'];verdict=str(decision['verdict']).upper()
    eligibility=str(decision['eligibility']).upper()
    # A/B are blinded candidate labels. The collector resolves their identity
    # from its source assignment and supplies the actual selected pipeline.
    requested=str(entry.get('selected_pipeline') or '').casefold().replace('+','_').replace('-','_')
    chosen_track=requested if verdict in {'A','B'} else None
    if verdict in {'A','B'} and chosen_track not in {'chatgpt','claude_gemini'}:
        return hold('A/B requires an explicitly mapped selected pipeline.')
    if verdict=='BOTH':
        left=policy.extracts(tracks.get('chatgpt'));right=policy.extracts(tracks.get('claude_gemini'))
        if eligibility!='EXCLUDE' and (len(left)!=1 or len(right)!=1 or policy.key_differences(left[0],right[0])):
            return hold('BOTH does not identify one agreed numeric bundle.')
        chosen_track='claude_gemini' if tracks.get('claude_gemini') else 'chatgpt'
    if verdict=='NEITHER':chosen_track='claude_gemini' if tracks.get('claude_gemini') else 'chatgpt'
    chosen_source=tracks.get(chosen_track)
    if not chosen_source:return hold('The selected saved pipeline context is absent.')
    selected=copy.deepcopy(chosen_source)
    # A numeric adjudication cannot discard an independently saved clinical hold.
    for gate in ('manual_review_hold','full_text_pooling_block','exposure_classification_ambiguous','is_mendelian_randomization'):
        if previous.get(gate):
            selected[gate]=copy.deepcopy(previous[gate])
    selected['context_id']=cid
    if eligibility=='INCLUDE':
        estimates=policy.extracts(selected)
        extraction=copy.deepcopy(estimates[0]) if len(estimates)==1 else {}
        source_index=source_row.get('ChatGPT estimate index' if chosen_track=='chatgpt' else 'Claude+Gemini estimate index')
        if not policy.missing(source_index) and str(source_index) not in {'0','0.0'}:
            return hold('The saved source estimate index does not match the supported bundle.')
        if verdict=='NEITHER':
            values=decision.get('numeric_values') or {}
            replacement={key:values.get('sample_size' if key=='total_n' else key) for key in policy.KEY_FIELDS}
            replacement['effect_type']=decision.get('effect_type')
            if not coherent(replacement):return hold('A custom correction requires complete coherent N, point estimate, CI, and effect type.')
            if extraction.get('needs_inversion') is True or extraction.get('inversion_applied') is True:
                return hold('A custom correction requires direction verification before replacing an inverted estimate.')
            keep=('design','study_design','population','stratum','subgroup','comparison_type','comparison_direction',
                  'unit','scale','adjusted_model','exposure_measurement_type','effect_is_adjusted','exposure_subtype')
            replacement={**{key:copy.deepcopy(extraction[key]) for key in keep if key in extraction},**replacement}
            replacement['astra_replaced_extraction']=extraction
            replacement['evidence']={key:{'excerpt':decision['evidence_quote'],'source':'abstract','url':source_row.get('PubMed URL')}
                                     for key in ('sample_size','effect_size','confidence_interval')}
            extraction=replacement
        else:
            if invalid_partial(extraction):return hold('Known quantities in the selected candidate are internally inconsistent.')
            if not _bundle_matches(decision.get('numeric_values') or {},extraction,allow_missing_source=True):
                return hold('Astra numeric values do not match the selected saved candidate.')
            if decision.get('effect_type') and not policy.missing(extraction.get('effect_type')) and effect_type(decision['effect_type'])!=effect_type(extraction.get('effect_type')):
                return hold('Astra effect type differs from the selected saved candidate.')
            # Verify the candidate still agrees with the exact discrepancy export.
            prefix='ChatGPT' if chosen_track=='chatgpt' else 'Claude+Gemini'
            exported={key:source_row.get(prefix+' '+label) for key,label in [('sample_size','sample size'),('effect_size','effect size'),('ci_lower','CI lower'),('ci_upper','CI upper')]}
            if not _bundle_matches(exported,extraction):return hold('The saved source changed after the discrepancy CSV was produced.')
        donor=tracks.get('claude_gemini' if chosen_track=='chatgpt' else 'chatgpt') or {}
        donor_estimates=policy.extracts(donor)
        if len(donor_estimates)==1 and verdict!='NEITHER':
            extraction,fills=policy.merge_missing_fields(extraction,donor_estimates[0],protected_fields=('jbi','JBI','jbi_grade','needs_inversion','inversion_applied','p_value_method','ci_method','evidence','wald_derivations','policy_derivations'))
        quantitative_gap=not coherent(extraction)
        selected['astra_quantitative_follow_up_required']=quantitative_gap
        selected['extraction']=extraction
        selected['eligibility']='INCLUDE'
        selected['final_status']=selected['full_text_status']='ELIGIBLE_NOT_POOLABLE'
        # Retain all source scientific/JBI/manual gates; technical poolability
        # is recalculated by the existing repository builder after this overlay.
        source_hold=bool(selected.get('manual_review_hold') or selected.get('full_text_pooling_block') or selected.get('exposure_classification_ambiguous') or selected.get('is_mendelian_randomization') or selected.get('manual_poolable') is False)
        if str(chosen_source.get('final_status') or chosen_source.get('full_text_status')) in {'MR_SEPARATE','ELIGIBLE_SUBTYPE_REVIEW'}:source_hold=True
        selected['astra_preserved_pooling_hold']=source_hold or quantitative_gap
    else:
        selected['eligibility']='EXCLUDE'
        selected['final_status']=selected['full_text_status']='EXCLUDE'
    selected['astra_previous_combined']={'sha256':digest(previous),'final_status':previous.get('final_status'),'combined_adjudication':copy.deepcopy(previous.get('combined_adjudication')),'extraction':copy.deepcopy(previous.get('extraction')),'jbi':copy.deepcopy(previous.get('jbi'))}
    selected['astra_adjudication']={'version':VERSION,'id':entry.get('id'),'selected_pipeline':chosen_track,'source_scope':'TITLE_ABSTRACT_ONLY','decision':copy.deepcopy(decision),'source_row':copy.deepcopy(source_row),'source_record_sha256':digest(chosen_source)}
    selected['combined_adjudication']={'resolution':'ASTRA_TITLE_ABSTRACT_RESOLVED','selected_pipeline':chosen_track.upper(),'explanation':decision.get('reason'),'astra_review_id':entry.get('id')}
    selected['screen_layer']='combined'
    selected['key_comparison_pending']=False
    selected['requires_key_review']=False
    selected['key_comparison']={**copy.deepcopy(previous.get('key_comparison') or {}),'action':'astra_title_abstract_resolved','prior_decision_reused':False,'existing_scientific_hold_preserved':False}
    audit.update(applied=True,reason=('Validated source selected; quantitative information remains incomplete and the context is not poolable.' if selected.get('astra_quantitative_follow_up_required') else 'Validated title/abstract decision applied; existing quality and scientific gates retained.'),selected_pipeline=chosen_track,
                 follow_up_required=bool(selected.get('astra_quantitative_follow_up_required') or selected.get('manual_review_hold') or selected.get('full_text_pooling_block') or selected.get('exposure_classification_ambiguous')),
                 context_status=selected['final_status'],context_authority='Astra title/abstract')
    return selected,audit


def apply_entries(records,tracks,entries,protected_contexts=()):
    grouped={};audit=[];result=copy.deepcopy(records)
    for entry in entries:
        cid,_=entry_context(entry);grouped.setdefault(cid,[]).append(entry)
    for cid,items in grouped.items():
        if cid not in result:
            audit.extend({'id':x.get('id'),'context_id':cid,'applied':False,'follow_up_required':True,'reason':'Context is absent from the saved combined universe.'} for x in items);continue
        if len(items)!=1:
            audit.extend({'id':x.get('id'),'context_id':cid,'applied':False,'follow_up_required':True,'context_status':result[cid].get('final_status'),'context_authority':retained_authority(result[cid]),'reason':'Multiple decisions for one context require explicit estimate-level review.'} for x in items);continue
        row,event=apply_entry(result[cid],{track:values.get(cid) for track,values in tracks.items()},items[0],protected=cid in protected_contexts)
        result[cid]=row;audit.append(event)
    return result,audit


def review_dispositions(audit,records):
    result=[]
    for event in audit:
        final=records.get(event.get('context_id')) or {}
        disposition=copy.deepcopy(event)
        disposition['context_status']=final.get('final_status') or final.get('full_text_status')
        disposition['context_authority']=retained_authority(final)
        disposition['follow_up_required']=bool(event.get('follow_up_required',not event.get('applied')) or final.get('astra_quantitative_follow_up_required'))
        result.append(disposition)
    return result


def apply_checkpoint(records,tracks,combined_root,protected_contexts=()):
    path=Path(combined_root)/CHECKPOINT_NAME
    if not path.exists():return records,[]
    packet=json.loads(path.read_text())
    if packet.get('version')!=VERSION:raise ValueError('Unsupported Astra combined checkpoint version.')
    return apply_entries(records,tracks,packet_entries(packet),set(protected_contexts))
