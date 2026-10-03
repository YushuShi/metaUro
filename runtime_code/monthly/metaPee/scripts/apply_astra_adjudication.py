#!/usr/bin/env python3
"""Apply validated title/abstract-only Astra decisions to saved combined evidence.

Dry-run by default. Re-run this overlay after reconstructing combined results
from the immutable model tracks. Original screening bundles are never rewritten.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import reconcile_screening_tracks as reconcile
from scripts import key_comparison_policy as policy

VERSION='astra-title-abstract-adjudication-2026-09-07-v1'
GATES=('jbi','manual_poolable','policy_poolable','included_in_parent_meta_analysis',
       'full_text_pooling_block','exposure_classification_ambiguous','hierarchy_inherited',
       'broader_outcome_projection','poolability_original_status')


def read(path):return json.loads(Path(path).read_text())
def dump(path,value):
    def clean(item):
        if isinstance(item,dict):return {key:clean(child) for key,child in item.items()}
        if isinstance(item,(tuple,list)):return [clean(child) for child in item]
        if hasattr(item,'item'):return clean(item.item())
        if isinstance(item,float) and not math.isfinite(item):return None
        return item
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(clean(value),ensure_ascii=False,indent=2,allow_nan=False,default=str)+'\n')
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()
def integer(value):
    number=policy.number(value)
    return int(number) if number is not None and number==int(number) else None

def high_confidence(decision):
    confidence=decision.get('confidence')
    return str(confidence).casefold()=='high' or (isinstance(confidence,(int,float)) and not isinstance(confidence,bool) and .9<=confidence<=1)

def validated(decision):
    return (high_confidence(decision) and decision.get('source_quote_validated') is True
        and bool(str(decision.get('evidence_quote') or '').strip()))

def coherent(extraction):
    n,e,lo,hi=[policy.number(extraction.get(key)) for key in policy.KEY_FIELDS]
    if n is None or n<=0 or n!=int(n) or any(v is None for v in (e,lo,hi)) or not lo<hi or not lo<=e<=hi:return False
    scale=policy.wald_scale(extraction)
    return scale is not None and (scale!='log_ratio' or lo>0)

def valid_partial(extraction):
    values={key:policy.number(extraction.get(key)) for key in policy.KEY_FIELDS}
    if any(not policy.missing(extraction.get(key)) and value is None for key,value in values.items()):return False
    n,e,lo,hi=(values[key] for key in policy.KEY_FIELDS)
    if n is not None and (n<=0 or n!=int(n)):return False
    if lo is not None and hi is not None and not lo<hi:return False
    if e is not None and ((lo is not None and lo>e) or (hi is not None and hi<e)):return False
    if policy.wald_scale(extraction)=='log_ratio' and any(value is not None and value<=0 for value in (e,lo,hi)):return False
    return True

def human(record):
    return bool(record and (reconcile.manual(record) or record.get('combined_manual_authority') or str(record.get('decision_authority','')).casefold() in {'human','manual'}))

def candidate_receipt(item):
    result={key:copy.deepcopy(value) for key,value in item.items() if key not in {'source_record','extraction'}}
    result['source_gate_snapshot']={key:copy.deepcopy(item['source_record'].get(key)) for key in GATES if key in item['source_record']}
    result['effective_source_status']=item['source_record'].get('final_status')
    return result

def candidates_for_record(record,tracks):
    """Recover each saved estimate's provenance without rebuilding its numbers."""
    cid=record['context_id'];gpt=tracks['chatgpt'].get(cid);cg=tracks['claude_gemini'].get(cid)
    if record.get('nonpoolable_status_resolution') and not human(record):
        resolution = policy.nonpoolable_status_resolution(gpt,cg)
        if resolution and resolution.get('resolution_kind') == 'both_eligible_not_poolable' and record.get('nonpoolable_status_resolution') != resolution:
            return reconcile.reconcile_context(gpt,cg)[1]
        items = [reconcile.candidate(record, extraction,
            record['nonpoolable_status_resolution']['retained_track'], index, source_index=index)
            for index, extraction in enumerate(policy.extracts(record))]
        if record.get('manual_review_pending') and record.get('requires_key_review'):
            for item in items:
                item.update(requires_key_review=True,manual_review_pending=True,
                    provisional_ui_selection={'pending':True,'selected_track':item['source'],
                    'selection_poolable_candidate':False,'reason':'Independent scientific/manual adjudication remains pending.'})
        return items
    _,original,_=reconcile.reconcile_context(gpt,cg)
    baseline=[item for item in original if not item['requires_key_review']]
    receipts=record.get('combined_accepted_estimate_sources') or []
    items=[]
    for index,extraction in enumerate(policy.extracts(record)):
        receipt=receipts[index] if index<len(receipts) else None
        if receipt:
            source=receipt['source'];source_record=copy.deepcopy(tracks.get(source,{}).get(cid) or record)
            source_record.update(copy.deepcopy(receipt.get('source_gate_snapshot') or {}))
            source_record['final_status']=receipt.get('effective_source_status',source_record.get('final_status'))
            item=reconcile.candidate(source_record,extraction,source,receipt['pair_index'],source_index=receipt['source_estimate_index'])
            item.update({key:copy.deepcopy(value) for key,value in receipt.items() if key not in {'source_gate_snapshot','effective_source_status'}})
        else:
            match=next((item for item in baseline if reconcile.numeric_snapshot(item['extraction'])==reconcile.numeric_snapshot(extraction)),None)
            item=copy.deepcopy(match) if match else reconcile.candidate(record,extraction,'combined',index)
            item['extraction']=copy.deepcopy(extraction)
        item['requires_key_review']=False
        items.append(item)
    for stored in record.get('key_review_candidates') or []:
        item=copy.deepcopy(stored)
        item['source_record']=copy.deepcopy(tracks.get(item['source'],{}).get(cid) or record)
        items.append(item)
    return items

def checked_status(item):
    """Re-evaluate quantitative eligibility while preserving source quality/gates."""
    from scripts import rebuild_relaxed_poolability as rebuild
    source=copy.deepcopy(item['source_record'])
    source['extraction']=copy.deepcopy(item['extraction']);source['additional_extractions']=[]
    source['eligibility']='INCLUDE'
    # The title/abstract decision replaces a source eligibility rejection; none
    # of the independent clinical/manual/JBI/exposure gates are removed.
    if source.get('final_status')=='EXCLUDE':source['final_status']='ELIGIBLE_NOT_POOLABLE'
    result=rebuild.effective(source)
    for key in GATES:
        if key in source:result[key]=copy.deepcopy(source[key])
    item=copy.deepcopy(item);item['source_record']=result;item['extraction']=result['extraction']
    item['requires_key_review']=False;item['key_differences']=[]
    return item

def resolve_pair(candidates,entry):
    decision=entry['adjudication'];verdict=str(decision.get('verdict') or '').upper()
    if not validated(decision):return None,'Low confidence or source quote not validated.'
    eligibility=str(decision.get('eligibility') or 'UNRESOLVED').upper()
    if eligibility=='EXCLUDE':return [],'Validated exclusion for the disputed estimate pair.'
    if eligibility!='INCLUDE':return None,'Eligibility remains unresolved.'
    if verdict not in {'A','B','BOTH','NEITHER'}:return None,'Numeric verdict remains unresolved.'
    wanted=entry.get('selected_pipeline') if verdict in {'A','B'} else None
    if verdict in {'A','B'} and wanted not in {'chatgpt','claude_gemini'}:return None,'Blinded A/B verdict needs a validated selected_pipeline mapping.'
    by_source={item['source']:item for item in candidates}
    if verdict=='BOTH':
        a,b=by_source.get('chatgpt'),by_source.get('claude_gemini')
        if not a or not b or policy.key_differences(a['extraction'],b['extraction']):return None,'BOTH does not identify one agreed numeric bundle.'
        wanted='claude_gemini'
    if verdict=='NEITHER':
        values=decision.get('numeric_values') or {}
        extraction={key:values.get('sample_size' if key=='total_n' else key) for key in policy.KEY_FIELDS}
        extraction['effect_type']=decision.get('effect_type')
        if not coherent(extraction):return None,'NEITHER requires an explicit coherent N, effect, and CI bundle.'
        chosen=copy.deepcopy(by_source.get('claude_gemini') or by_source.get('chatgpt'))
        if not chosen:return None,'No existing candidate context can support a replacement.'
        old=copy.deepcopy(chosen['extraction'])
        # Keep the old bundle as provenance. No stale CI/p/inversion receipt may
        # assert validation of the corrected numerical bundle.
        safe_fields=('design','study_design','timing','continent','country','population','gender','stratum','subgroup',
            'comparison_type','comparison_direction','comparison_group','reference_group','unit','scale','adjusted_model',
            'exposure_measurement_type','exposure_subtype','effect_is_adjusted','independent_population_id')
        new={key:copy.deepcopy(old[key]) for key in safe_fields if key in old}
        new.update(extraction)
        new['combined_replaced_extraction']=old
        new['evidence']={field:{'excerpt':decision['evidence_quote'],'source':'abstract','url':entry['source_row'].get('PubMed URL')} for field in ('sample_size','effect_size','confidence_interval')}
        # Corrected numeric fields must not silently inherit reverse direction.
        if old.get('needs_inversion') is True or old.get('inversion_applied') is True:return None,'Replacement direction needs explicit source review.'
        chosen['extraction']=new
        chosen['astra_basis_source']=chosen['source'];chosen['source']='astra'
    else:
        if wanted not in by_source:return None,'Selected source has no candidate in this estimate pair.'
        chosen=copy.deepcopy(by_source[wanted])
        source_index=entry['source_row'].get('ChatGPT estimate index' if wanted=='chatgpt' else 'Claude+Gemini estimate index')
        if not policy.missing(source_index) and integer(source_index)!=chosen['source_estimate_index']:return None,'Source estimate index does not match the saved discrepancy.'
        csv_prefix='ChatGPT' if wanted=='chatgpt' else 'Claude+Gemini'
        for key,suffix in [('total_n','sample size'),('effect_size','effect size'),('ci_lower','CI lower'),('ci_upper','CI upper')]:
            column=csv_prefix+' '+suffix
            if column in entry['source_row'] and not policy.numeric_equal(entry['source_row'][column],chosen['extraction'].get(key),'count' if key=='total_n' else 'ratio'):
                return None,'Saved source numeric bundle changed since the adjudicated discrepancy CSV.'
        values=decision.get('numeric_values') or {}
        for key in policy.KEY_FIELDS:
            supplied=values.get('sample_size' if key=='total_n' else key)
            if not policy.missing(supplied) and not policy.numeric_equal(supplied,chosen['extraction'].get(key),'count' if key=='total_n' else 'ratio'):
                return None,'Adjudicated numeric bundle differs from the selected saved estimate.'
        if decision.get('effect_type'):
            from scripts.relaxed_poolability import canonical_ratio
            adjudicated_type=canonical_ratio(decision['effect_type']) or str(decision['effect_type']).casefold()
            saved_type=canonical_ratio(chosen['extraction'].get('effect_type')) or str(chosen['extraction'].get('effect_type')).casefold()
            if not policy.missing(chosen['extraction'].get('effect_type')) and (canonical_ratio(decision['effect_type']) or canonical_ratio(chosen['extraction'].get('effect_type'))) and adjudicated_type!=saved_type:return None,'Adjudicated effect type differs from the saved estimate.'
        if not valid_partial(chosen['extraction']):return None,'Selected saved estimate contains invalid or contradictory quantitative values.'
    other=next((item for item in candidates if item['source']!=chosen['source']),None)
    if other and verdict!='NEITHER':
        merged,fills=policy.merge_missing_fields(chosen['extraction'],other['extraction'],protected_fields=('jbi','JBI','jbi_answers','jbi_grade','jbi_quality','manual_review','manual_adjudication'))
        if fills:merged=reconcile.preserve_source_receipts(merged,chosen['extraction'])
        chosen['extraction']=merged;chosen['nonkey_fills']=fills
    chosen=checked_status(chosen)
    chosen['astra_adjudication']={key:copy.deepcopy(value) for key,value in decision.items()}
    chosen['astra_adjudication']['review_id']=entry['id']
    chosen['astra_follow_up_required']=not coherent(chosen['extraction'])
    chosen['astra_follow_up_reason']=('The selected saved bundle still has missing quantitative fields or effect type; full text/manual extraction is needed.' if chosen['astra_follow_up_required'] else None)
    return [chosen],(chosen['astra_follow_up_reason'] or 'Validated title/abstract decision applied to this estimate pair.')

def apply_context(record,tracks,entries):
    original=copy.deepcopy(record);items=candidates_for_record(record,tracks);audit=[]
    if human(record) or any(human(tracks[track].get(record['context_id'])) for track in tracks):
        return original,items,[{'id':entry['id'],'applied':False,'reason':'Existing human/manual authority preserved.','follow_up_required':False} for entry in entries]
    a,b=(tracks[track].get(record['context_id']) for track in ('chatgpt','claude_gemini'))
    if policy.nonpoolable_status_resolution(a,b):
        resolved, candidates, _ = reconcile.reconcile_context(a,b)
        independent_ids = reconcile.preserve_independent_review(resolved,candidates,entries)
        existing_resolution = original.get('nonpoolable_status_resolution') or {}
        resolution = resolved['nonpoolable_status_resolution']
        unchanged_retention = (resolution.get('resolution_kind') == 'both_eligible_not_poolable' and existing_resolution == resolution)
        previous = original.get('prior_automated_adjudication') if unchanged_retention else original
        if previous is not None:resolved['prior_automated_adjudication'] = copy.deepcopy(previous)
        return resolved, candidates, [{'id': entry['id'], 'applied': False, 'follow_up_required': entry['id'] in independent_ids,
            'reason': ('Independent scientific/manual adjudication remains pending.' if entry['id'] in independent_ids else resolved['poolability_reason'])} for entry in entries]
    accepted=[item for item in items if not item['requires_key_review']]
    pending=[item for item in items if item['requires_key_review']]
    expected_pairs={item['pair_index'] for item in pending}
    # Some disagreements have no extractable candidates at either source.
    _,_,differences=reconcile.reconcile_context(tracks['chatgpt'].get(record['context_id']),tracks['claude_gemini'].get(record['context_id']))
    expected_pairs|={row['estimate_pair']-1 for row in differences}
    previously_resolved={row['pair_index'] for row in record.get('astra_adjudication_history',[]) if row.get('applied')}
    expected_pairs-=previously_resolved
    historical={row['id']:row for row in record.get('astra_adjudication_history',[]) if row.get('applied')}
    resolved=set();excluded=set();seen=set()
    pair_counts=Counter(integer(entry['source_row'].get('Estimate pair')) for entry in entries)
    for entry in entries:
        pair=integer(entry['source_row'].get('Estimate pair'))
        index=pair-1 if pair is not None else -1
        reason='';chosen=None
        previous=historical.get(entry['id'])
        same_selection=(entry.get('selected_pipeline') is None or any(
            item['pair_index']==index and item['source']==entry.get('selected_pipeline') for item in accepted))
        if pair_counts[pair]==1 and previous and previous.get('adjudication')==entry['adjudication'] and (same_selection or str(entry['adjudication'].get('eligibility')).upper()=='EXCLUDE'):
            audit.append({**copy.deepcopy(previous),'replayed_existing_decision':True})
            continue
        if pair_counts[pair]>1 or index in seen:reason='Duplicate decisions for the same estimate pair require review.'
        elif index not in expected_pairs:reason='Estimate pair is absent from the saved unresolved review list.'
        else:
            seen.add(index)
            chosen,reason=resolve_pair([item for item in pending if item['pair_index']==index],entry)
        applied=chosen is not None
        audit.append({'id':entry['id'],'pair_index':index,'applied':applied,'reason':reason,'follow_up_required':not applied or any(item.get('astra_follow_up_required') for item in (chosen or [])),'adjudication':copy.deepcopy(entry['adjudication'])})
        if applied:
            accepted.extend(chosen);resolved.add(index)
            if not chosen:excluded.add(index)
    pending=[item for item in pending if item['pair_index'] not in resolved]
    record=copy.deepcopy(record)
    if not resolved:return original,items,audit
    accepted.sort(key=lambda item:(item['pair_index'],item['source_estimate_index'],item['source']))
    record['extraction']=copy.deepcopy(accepted[0]['extraction']) if accepted else None
    record['additional_extractions']=[copy.deepcopy(item['extraction']) for item in accepted[1:]]
    record['combined_accepted_estimate_sources']=[candidate_receipt(item) for item in accepted]
    record['key_review_candidates']=[{key:copy.deepcopy(value) for key,value in item.items() if key!='source_record'} for item in pending]
    record['requires_key_review']=bool(expected_pairs-resolved)
    record['combined_remaining_key_review_pairs']=sorted(expected_pairs-resolved)
    record.setdefault('combined_pre_astra_quantitative_gate',{key:copy.deepcopy(original.get(key)) for key in ('missing_key_fields','invalid_key_fields','needs_full_text')})
    record['missing_key_fields']=sorted({key for item in accepted for key in item['source_record'].get('missing_key_fields',[])})
    record['invalid_key_fields']=sorted({key for item in accepted for key in item['source_record'].get('invalid_key_fields',[])})
    record['needs_full_text']=record['requires_key_review'] or any(item['source_record'].get('needs_full_text') for item in accepted)
    record['full_text_eligible']=bool(record['needs_full_text'] and record.get('pmcid'))
    record['combined_numeric_consensus']=False
    record['combined_numeric_resolved']=bool(accepted) and not record['requires_key_review']
    record['astra_adjudication_version']=VERSION
    record['astra_adjudication_history']=[*record.get('astra_adjudication_history',[]),*(row for row in audit if not row.get('replayed_existing_decision'))]
    if not accepted and not record['requires_key_review'] and excluded==expected_pairs and all(row['applied'] and str(row['adjudication'].get('eligibility')).upper()=='EXCLUDE' for row in audit):
        record.update(eligibility='EXCLUDE',final_status='EXCLUDE',poolability_reason='All disputed pairs were excluded by validated title/abstract adjudication.')
    else:
        record['eligibility']='INCLUDE'
        poolable=any(reconcile.app_row(item)['policy_poolable'] for item in accepted)
        record['final_status']='INCLUDE_META' if poolable else 'ELIGIBLE_NOT_POOLABLE'
        record['poolability_reason']=('Resolved estimates meet existing quality and pooling gates.' if poolable else
            'No resolved estimate meets all existing quantitative, JBI, clinical and manual pooling gates.')
    return record,accepted+pending,audit


def build_combined_modes(source_paths,destination):
    """Save combined-only transitive exposure projections; never touch public GPT views."""
    import pandas as pd
    import meta_analysis
    from scripts import subexposure_policy as sub
    groups=defaultdict(dict);templates={};hierarchy=read(ROOT/'data/exposure_hierarchy.json')
    for path in source_paths:
        payload=read(path);exposure=payload.get('exposure');disease=payload.get('disease') or payload.get('cancer');endpoint=payload.get('endpoint') or payload.get('outcome') or 'Incidence'
        if not exposure or not disease:continue
        groups[(disease,endpoint)][exposure]=payload.get('studies',[])
        templates[(disease,endpoint)]=payload
    ancestors={}
    all_parents={parent for values in hierarchy.values() for parent in values}
    for parent in all_parents:
        for child in sub.descendants(parent,hierarchy):ancestors.setdefault(sub.key(child),set()).add(parent)
    counts={}
    for mode in ('yes','no'):
        count=Counter()
        for (disease,endpoint),sources in groups.items():
            targets=set(sources)
            for source in sources:targets.update(ancestors.get(sub.key(source),set()))
            for exposure in sorted(targets):
                rows=sub.project_rows(sources,exposure,hierarchy,include=mode)
                pool=sub.pooling_rows(rows);frame=pd.DataFrame(pool);all_frame=pd.DataFrame(rows)
                if not frame.empty:
                    frame=meta_analysis.filter_curated_meta_analysis_exclusions(frame,disease,exposure,endpoint)
                    if hasattr(meta_analysis,'human_pooling_rows'):frame=meta_analysis.human_pooling_rows(frame)
                result={}
                if not frame.empty:
                    result=meta_analysis.perform_meta_analysis(frame,disease,exposure,outcome=endpoint,exclude_meta=True,df_all=all_frame,generate_plots=False)
                    if result.get('error'):raise RuntimeError(f'Combined subexposure analysis failed: {disease}, {exposure}: {result["error"]}')
                packet={key:result.get(key) for key in ('headline','summary_html','screening_stats')}
                packet.update(exposure=exposure,disease=disease,cancer=disease,endpoint=endpoint,outcome=endpoint,studies=rows,
                    canonical_track='combined',include_subexposures=mode,plot_url=None,funnel_plot_url=None,baujat_plot_url=None,
                    requires_key_review=any(row.get('requires_key_review') for row in rows),astra_adjudication_version=VERSION,
                    subexposure_projection={'mode':mode,'table_rows':len(rows),'pooling_rows':len(frame)})
                from scripts.rebuild_relaxed_poolability import cache_paths
                for original in cache_paths(exposure,disease,endpoint):
                    dump(destination/mode/'Cached_results'/original.relative_to(ROOT/'Cached_results'),packet)
                count['contexts']+=1;count['visible_rows']+=len(rows);count['pooling_rows']+=len(frame)
        counts[mode]=dict(count)
        manifest={'canonical_track':'combined','include_subexposures':mode,'version':VERSION,'counts':dict(count)}
        dump(destination/mode/'manifest.json',manifest)
        dump(destination/mode/'Cached_results/_subexposure_manifest.json',manifest)
    return counts


def run(packet_path,output,apply=False):
    packet=read(packet_path)
    if packet.get('model')!='gpt-6-astra' or packet.get('source_scope')!='TITLE_ABSTRACT_ONLY':raise ValueError('Only exact gpt-6-astra title/abstract-only packets are accepted.')
    if packet.get('complete') is False:raise ValueError('The validated adjudication packet is incomplete.')
    if output.exists() and any(output.iterdir()):raise ValueError('Use a new empty output directory for this run.')
    output.mkdir(parents=True,exist_ok=True)
    source_paths={track:reconcile.SOURCE_DIR/f'{track}_effective_results.json' for track in ('chatgpt','claude_gemini')}
    source_hashes={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths.values()}
    tracks={track:{row['context_id']:row for row in read(path)['results']} for track,path in source_paths.items()}
    combined_path=ROOT/'combined/effective_results.json';combined=read(combined_path)
    grouped=defaultdict(list);audit=[]
    for entry in packet.get('results',[]):
        row=entry.get('source_row') or {};cid=row.get('Context ID')
        if not cid:raise ValueError('Every adjudication requires the original CSV Context ID.')
        if entry.get('id')!=entry.get('adjudication',{}).get('id'):raise ValueError('Decision and packet IDs differ.')
        grouped[cid].append(entry)
    records=[];changed=set();all_items={}
    for original in combined['results']:
        cid=original['context_id']
        if cid not in grouped:records.append(original);continue
        for entry in grouped[cid]:
            row=entry['source_row']
            if str(row.get('PMID'))!=str(original.get('pmid')) or row.get('Exposure')!=original.get('exposure') or row.get('Disease')!=(original.get('disease') or original.get('cancer')):raise ValueError(f'CSV context identity mismatch: {entry["id"]}')
            if not policy.missing(row.get('Endpoint')) and row['Endpoint']!=(original.get('endpoint') or original.get('outcome') or 'Incidence'):raise ValueError(f'CSV endpoint mismatch: {entry["id"]}')
        updated,items,decisions=apply_context(original,tracks,grouped[cid])
        for decision in decisions:decision['context_id']=cid
        audit.extend(decisions);records.append(updated);all_items[cid]=items
        if digest(updated)!=digest(original):changed.add(cid)
    missing=set(grouped)-{row['context_id'] for row in combined['results']}
    if missing:raise ValueError(f'Contexts absent from combined: {sorted(missing)}')
    report={'version':VERSION,'model':'gpt-6-astra','source_scope':'TITLE_ABSTRACT_ONLY','apply_requested':apply,
        'changed_contexts':len(changed),'decision_counts':dict(Counter('applied' if row['applied'] else 'retained_for_review' for row in audit)),
        'combined_status_counts':dict(Counter(row.get('final_status') for row in records)),
        'changed_context_ids':sorted(changed),'source_hashes':source_hashes,'combined_before_sha256':hashlib.sha256(combined_path.read_bytes()).hexdigest()}
    dump(output/'decisions.json',audit);dump(output/'input_packet.json',packet)
    if apply and changed:
        scopes={reconcile.scope(row) for row in records if row['context_id'] in changed}
        groups=defaultdict(list)
        for record in records:
            key=reconcile.scope(record)
            if key in scopes:groups[key].extend(all_items.get(record['context_id']) or candidates_for_record(record,tracks))
        stage=output/'staged_combined';old_combined,old_audit=reconcile.COMBINED,reconcile.AUDIT
        try:
            reconcile.COMBINED=stage;reconcile.AUDIT=output/'builder_audit'
            report['materialization']=reconcile.materialize(groups)
            if report['materialization']['errors']:raise RuntimeError('Combined cache materialization has errors; nothing applied.')
        finally:reconcile.COMBINED=old_combined;reconcile.AUDIT=old_audit
        # Explicitly empty fully excluded scopes so no old candidate survives.
        from scripts.rebuild_relaxed_poolability import cache_paths
        for scope,items in groups.items():
            if items:continue
            exposure,disease,endpoint=scope
            empty={'canonical_track':'combined','exposure':exposure,'disease':disease,'cancer':disease,'endpoint':endpoint,'outcome':endpoint,'studies':[],
                'headline':None,'plot_url':None,'funnel_plot_url':None,'baujat_plot_url':None,'requires_key_review':False,'astra_adjudication_version':VERSION}
            for original in cache_paths(*scope):dump(stage/original.relative_to(ROOT/'Cached_results'),empty)
        source_caches={str(path.relative_to(ROOT/'combined')):path for path in (ROOT/'combined').glob('*/*_true_all.json')}
        source_caches.update({str(path.relative_to(stage)):path for path in stage.glob('*/*_true_all.json')})
        report['subexposure_modes']=build_combined_modes(source_caches.values(),stage/'subexposure_views')
        combined['results']=records;combined['astra_adjudication_version']=VERSION
        dump(stage/'effective_results.json',combined)
        previous_packet_path=ROOT/'combined/astra_adjudication/current_decisions.json'
        previous_packet=read(previous_packet_path) if previous_packet_path.exists() else {}
        cumulative={entry['id']:entry for entry in previous_packet.get('results',[])}
        cumulative.update({entry['id']:entry for entry in packet.get('results',[])})
        stored_packet={**packet,'results':list(cumulative.values())}
        dump(stage/'astra_adjudication/current_decisions.json',stored_packet)
        dump(stage/'astra_adjudication/application_audit.json',audit)
        by_context={row['context_id']:row for row in records}
        dispositions=[{key:row.get(key) for key in ('id','applied','reason','follow_up_required')} | {'context_status':by_context[row['context_id']].get('final_status'),'context_authority':'human' if human(by_context[row['context_id']]) else 'astra_and_saved_sources'} for row in audit]
        dump(stage/'astra_adjudication/review_dispositions.json',dispositions)
        assert all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==checksum for path,checksum in source_hashes.items())
        for path in sorted(stage.rglob('*')):
            if not path.is_file():continue
            relative=path.relative_to(stage);target=ROOT/'combined'/relative
            if target.exists():
                backup=output/'before/combined'/relative;backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,backup)
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
        report['combined_after_sha256']=hashlib.sha256(combined_path.read_bytes()).hexdigest()
    else:
        report['combined_after_sha256']=report['combined_before_sha256']
    if apply and not changed:
        by_context={row['context_id']:row for row in records}
        dump(ROOT/'combined/astra_adjudication/review_dispositions.json',[{key:row.get(key) for key in ('id','applied','reason','follow_up_required')} | {'context_status':by_context[row['context_id']].get('final_status')} for row in audit])
    report['source_packets_verified_unchanged']=all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==checksum for path,checksum in source_hashes.items())
    if not report['source_packets_verified_unchanged']:raise RuntimeError('Original source packet hash changed.')
    dump(output/'application_report.json',report)
    return report

def build_summary_figures(output):
    from scripts import build_summary_plots as summary
    if output.exists() and any(output.iterdir()):raise ValueError('Use a new empty summary audit directory.')
    output.mkdir(parents=True,exist_ok=True);report={}
    from scripts import plot_localization
    override_path=ROOT/'combined/astra_adjudication/plot_translation_overrides.json'
    if override_path.exists():
        catalog=plot_localization._site_catalog(str(ROOT.resolve()))
        for label,translations in read(override_path)['translations'].items():
            for language,text in translations.items():catalog.setdefault(label,{}).setdefault(language,text)
    for mode in ('yes','no'):
        root=ROOT/'combined/subexposure_views'/mode
        manifest=read(root/'manifest.json')
        dump(root/'Cached_results/_subexposure_manifest.json',manifest)
        destination=output/'staged_plots'/mode
        plotted=summary.build(root/'Cached_results',destination)
        plotted.update(canonical_track='combined',astra_adjudication_version=VERSION,include_subexposures=mode)
        dump(destination/'summary_manifest.json',plotted)
        shutil.copytree(destination,root/'Plot',dirs_exist_ok=True)
        report[mode]={'scopes':len(plotted['scopes']),'pdfs':len(list(destination.rglob('*.pdf'))),'workbooks':len(list(destination.glob('*.xlsx')))}
    dump(output/'plot_report.json',report);return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet',type=Path,default=ROOT/'combined/astra_adjudication/current_decisions.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--summary-plots-only',action='store_true',help='Rebuild both combined-only summary figure sets from the saved adjudicated views.')
    args=parser.parse_args();print(json.dumps(build_summary_figures(args.output) if args.summary_plots_only else run(args.packet,args.output,args.apply),indent=2))
