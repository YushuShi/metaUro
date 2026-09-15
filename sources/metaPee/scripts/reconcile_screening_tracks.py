#!/usr/bin/env python3
"""Offline numeric reconciliation of immutable current effective screening tracks."""
from __future__ import annotations
import copy, hashlib, json, math, re, sys
from collections import Counter, defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import key_comparison_policy as policy
from scripts import dietary_only_policy as dietary

AUDIT=ROOT/'outputs/track_comparison_2026-09-05'
COMBINED=ROOT/'combined'
SOURCE_DIR=ROOT/'outputs/poolability_policy_2026-09-05'


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def write(path,value):
    if path.is_relative_to(COMBINED) and path.exists():
        backup_relative=path.resolve().relative_to(ROOT) if path.resolve().is_relative_to(ROOT) else Path('external_saved_view')/path.relative_to(COMBINED)
        backup=AUDIT/'before'/backup_relative
        if not backup.exists():
            backup.parent.mkdir(parents=True,exist_ok=True);backup.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True,exist_ok=True)
    def clean(item):
        if isinstance(item,dict):return {key:clean(child) for key,child in item.items()}
        if isinstance(item,(tuple,list)):return [clean(child) for child in item]
        if hasattr(item,'item'):return clean(item.item())
        if isinstance(item,float) and not math.isfinite(item):return None
        return item
    path.write_text(json.dumps(clean(value),ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def protected_sources():
    baseline=read(AUDIT/'source_sha256_before.json')
    failures=[name for name,digest in baseline.items() if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest]
    if failures:raise RuntimeError('Protected source changed: '+', '.join(failures))
    return len(baseline)


def manual(record):
    if not record:return False
    authority = str(record.get('decision_authority') or record.get('decision_origin') or '').casefold()
    resolution = str((record.get('combined_adjudication') or {}).get('resolution') or '').upper()
    return bool(authority in {'human','manual','human_manual_review'} or any(record.get(key) for key in
        ('manual_review','manual_adjudication','human_adjudication','manual_full_text_review',
         'manual_full_text_adjudication','combined_manual_authority')) or
        ('PENDING' not in resolution and any(word in resolution for word in ('HUMAN','MANUAL'))))


def scope(record):
    return (record.get('exposure'),record.get('disease') or record.get('cancer'),record.get('endpoint') or record.get('outcome') or 'Incidence')


def excluded(record):
    return not record or record.get('eligibility')=='EXCLUDE' or record.get('final_status')=='EXCLUDE'


def stripped_extraction(extraction):
    result=copy.deepcopy(extraction)
    for key in ['additional_records','additional_extractions']:
        result.pop(key,None)
    return result


def numeric_snapshot(extraction):
    if extraction is None:return None
    return {key:copy.deepcopy(extraction.get(key)) for key in ['total_n','effect_type','effect_size','ci_lower','ci_upper','p_value','gender','stratum','subgroup','comparison_type','adjusted_model']}


def apply_p_resolution(extraction,resolution):
    """Populate from the shared policy's explicit resolution contract."""
    result=copy.deepcopy(extraction)
    if resolution.get('resolution')=='computed_two_sided_wald':
        result['combined_original_p_value']=copy.deepcopy(result.get('p_value'))
        result['p_value']=resolution['combined_p_value']
        result['p_value_relation']=resolution.get('combined_p_value_relation')
        result['p_value_method']=resolution['method']
        result['p_value_imputed']=True
        result['p_value_provenance']='combined_two_sided_wald'
    result['combined_p_value_resolution']=copy.deepcopy(resolution)
    return result


def preserve_source_receipts(derived, original):
    """Copied model receipts attest to the source, never to a merged extraction."""
    fields=[key for key in derived if key in {'estimate_identity_digest','estimate_bundle_digest','inversion_provenance','inversion_applied','needs_inversion_original'} or key.endswith('_source_estimate_identity_digest')]
    if fields:
        derived['combined_source_estimate_provenance']={key:copy.deepcopy(original.get(key)) for key in fields}
        derived['combined_source_estimate_provenance']['numeric_fields']=numeric_snapshot(original)
        for key in fields:derived.pop(key,None)
        if original.get('inversion_applied') is True:
            derived['needs_inversion']=False
            derived['combined_source_inversion_applied']=True
    derived['combined_derived_estimate']=True
    return derived


def candidate(record,extraction,source,pair_index,review=False,differences=(),fills=(),p_resolution=None,source_index=None):
    return {'source':source,'source_context_id':record.get('context_id'),
        'pair_index':pair_index,'source_estimate_index':source_index if source_index is not None else pair_index,'requires_key_review':bool(review),'key_differences':list(differences),
        'source_record':copy.deepcopy(record),'extraction':stripped_extraction(extraction),
        'nonkey_fills':copy.deepcopy(fills),'p_resolution':copy.deepcopy(p_resolution)}


def reconcile_context(gpt,cg):
    context=cg or gpt
    comparison=policy.compare_context(gpt,cg)
    human=gpt if manual(gpt) else cg if manual(cg) else None
    resolution = comparison.get('nonpoolable_status_resolution')
    retained = {'chatgpt': gpt, 'claude_gemini': cg}.get((resolution or {}).get('retained_track'))
    preferred=human or retained or cg or gpt
    other=cg if preferred is gpt else gpt
    if (resolution or {}).get('resolution_kind') == 'both_eligible_not_poolable':
        record, fills = copy.deepcopy(preferred), []
    else:
        record,fills=policy.merge_missing_fields(preferred,other or {},protected_fields=('jbi','JBI','jbi_answers','jbi_grade','jbi_quality','manual_review','manual_adjudication'))
    accepted=[];pending=[];discrepancies=[]
    if human:
        if not excluded(human):
            for i,extraction in enumerate(policy.extracts(human)):
                accepted.append(candidate(record,extraction,'chatgpt' if human is gpt else 'claude_gemini',i))
        record['combined_manual_authority']=True
    elif resolution:
        record.update(eligibility='INCLUDE', final_status='ELIGIBLE_NOT_POOLABLE',
                      full_text_status='ELIGIBLE_NOT_POOLABLE', scientific_pooling_eligible=False,
                      nonpoolable_status_resolution=copy.deepcopy(resolution))
        record['poolability_reason'] = ('Both pipelines classify this study as eligible, not poolable. The more informative single-pipeline record is retained for display; no manual comparison or pooling is required.' if resolution.get('resolution_kind') == 'both_eligible_not_poolable' else 'EXCLUDE versus ELIGIBLE_NOT_POOLABLE resolves to eligible, not poolable; no manual comparison is required.')
        retained_extractions = policy.extracts(retained)
        if not retained_extractions and resolution.get('resolution_kind') == 'both_eligible_not_poolable':
            retained_extractions = [policy.normalise_estimate({})]
        for index, extraction in enumerate(retained_extractions):
            accepted.append(candidate(record, extraction, resolution['retained_track'], index, source_index=index))
    else:
        for i,pair in enumerate(comparison['pairs']):
            left,right=pair['chatgpt'],pair['claude_gemini']
            if pair['key_agreement'] and left is not None and right is not None:
                merged,extra_fills=policy.merge_missing_fields(right,left,protected_fields=('jbi','JBI','jbi_answers','jbi_grade','jbi_quality','manual_review','manual_adjudication'))
                merged=apply_p_resolution(merged,pair.get('p_resolution') or {})
                if extra_fills or (pair.get('p_resolution') or {}).get('resolution')=='computed_two_sided_wald':
                    merged=preserve_source_receipts(merged,right)
                accepted.append(candidate(record,merged,'claude_gemini',i,fills=extra_fills,p_resolution=pair.get('p_resolution'),source_index=pair.get('claude_gemini_index')))
            else:
                for extraction,source,source_record in [(left,'chatgpt',gpt),(right,'claude_gemini',cg)]:
                    if extraction is not None and source_record and not excluded(source_record):
                        pending.append(candidate(source_record,extraction,source,i,True,pair['differences'],source_index=pair.get(source+'_index')))
                exposure,disease,endpoint=scope(context)
                discrepancies.append({'repo':ROOT.name,'context_id':context.get('context_id'),
                    'pmid':str(context.get('pmid') or ''),'title':context.get('title'),'exposure':exposure,'disease':disease,'endpoint':endpoint,
                    'estimate_pair':i+1,'key_differences':pair['differences'],
                    'chatgpt':numeric_snapshot(left),'claude_gemini':numeric_snapshot(right),
                    'chatgpt_status':(gpt or {}).get('final_status'),'claude_gemini_status':(cg or {}).get('final_status'),
                    'chatgpt_estimate_index':pair.get('chatgpt_index'),'claude_gemini_estimate_index':pair.get('claude_gemini_index'),
                    'p_resolution':pair.get('p_resolution'),'manual_review_results':'',
                    'pubmed_url':context.get('pubmed_url') or 'https://pubmed.ncbi.nlm.nih.gov/'+str(context.get('pmid'))+'/'})
    accepted.sort(key=lambda item:item['source_estimate_index'])
    record['extraction']=copy.deepcopy(accepted[0]['extraction']) if accepted else None
    record['additional_extractions']=[copy.deepcopy(item['extraction']) for item in accepted[1:]]
    record['key_review_candidates']=[{key:copy.deepcopy(value) for key,value in item.items() if key!='source_record'} for item in pending]
    record['requires_key_review']=bool(pending or discrepancies)
    record['combined_sources']={'chatgpt':{'path':str((SOURCE_DIR/'chatgpt_effective_results.json').relative_to(ROOT)),'context_id':(gpt or {}).get('context_id')},
        'claude_gemini':{'path':str((SOURCE_DIR/'claude_gemini_effective_results.json').relative_to(ROOT)),'context_id':(cg or {}).get('context_id')}}
    record['combined_nonkey_fills']=fills
    if not human and record['requires_key_review'] and not accepted:
        record['eligibility']='INCLUDE';record['final_status']='ELIGIBLE_NOT_POOLABLE'
        record['poolability_reason']='Key numeric results disagree between screening tracks; both candidates are retained for review.'
    record['combined_numeric_consensus']=bool(accepted) and not record['requires_key_review'] and not resolution
    record['combined_reconciliation_version']=policy.VERSION
    receipt_fields=[key for key in record if key in {'result_digest','packet_sha256','extraction_sha256','estimate_digest'}]
    if receipt_fields:
        record['combined_source_record_provenance']={key:record.pop(key) for key in receipt_fields}
    return record,accepted+pending,discrepancies



def preserve_independent_review(record,candidates,entries):
    """Keep explicitly separate scientific adjudication pending after status retention."""
    ids=[entry.get('id') for entry in entries if entry.get('independent_scientific_review') is True
         or str(entry.get('remaining_category') or '').casefold().startswith('independent scientific')
         or '-additional-human-' in str(entry.get('id') or '')]
    ids=[value for value in ids if value]
    if not ids:return []
    record.update(requires_key_review=True,manual_review_pending=True,independent_scientific_review_ids=ids)
    for item in candidates:
        item.update(requires_key_review=True,manual_review_pending=True)
        item['source_record'].update(requires_key_review=True,manual_review_pending=True,independent_scientific_review_ids=ids)
        item['provisional_ui_selection']={'pending':True,'review_ids':ids,
            'selected_track':item['source'],'selection_poolable_candidate':False,
            'reason':'Independent scientific/manual adjudication remains pending; this single-source record is display only.'}
    return ids

def number(value):
    try:
        n=float(str(value).replace(',',''))
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):return None


def app_row(item):
    record=item['source_record'];e=item['extraction'];jbi=record.get('jbi') or {}
    exposure,disease,endpoint=scope(record)
    n,effect,lower,upper=(number(e.get(key)) for key in ['total_n','effect_size','ci_lower','ci_upper'])
    original_effect,original_lower,original_upper=effect,lower,upper
    typ=e.get('effect_type');ratio=re.sub('[^a-z]','',str(typ).lower()) in ['rr','hr','or','arr','ahr','aor']
    inverted=bool(ratio and e.get('needs_inversion') is True and e.get('inversion_applied') is not True and e.get('combined_source_inversion_applied') is not True and all(value is not None and value>0 for value in [effect,lower,upper]))
    if inverted:effect,lower,upper=1/effect,1/upper,1/lower
    valid=n is not None and n>0 and n.is_integer() and all(x is not None for x in [effect,lower,upper]) and lower<upper and (not ratio or lower>0)
    poolable=bool(valid and record.get('final_status')=='INCLUDE_META' and str(jbi.get('grade')).casefold() in ['good','moderate'] and not item['requires_key_review'])
    for flag in ['manual_poolable','policy_poolable','included_in_parent_meta_analysis','scientific_pooling_eligible']:
        if record.get(flag) is False or e.get(flag) is False:poolable=False
    if record.get('full_text_pooling_block') or record.get('exposure_classification_ambiguous') or e.get('exposure_classification_ambiguous'):poolable=False
    if record.get('hierarchy_inherited') or record.get('broader_outcome_projection'):poolable=False
    digest=hashlib.sha256(json.dumps([record.get('context_id'),item['pair_index'],item['source'],numeric_snapshot(e)],sort_keys=True).encode()).hexdigest()[:20]
    year=re.search(r'\b(?:19|20)\d{2}\b',str(record.get('published_date') or ''))
    authors=record.get('authors') or ['Unknown'];authors=[authors] if isinstance(authors,str) else authors
    authors=[str(value) for value in authors]
    evidence=e.get('evidence') or {}
    se=None
    if lower is not None and upper is not None and upper>lower:
        if ratio and lower>0:se=(math.log(upper)-math.log(lower))/3.919927969080108
        elif not ratio:se=(upper-lower)/3.919927969080108
    study_label=f"{authors[0]} et al. ({year.group() if year else 'Unknown'}) [PMID: {record.get('pmid')}] — estimate {item['source_estimate_index']+1}"
    if e.get('gender'):study_label+=' · '+str(e['gender'])
    if item['requires_key_review'] and not item.get('manual_review_pending'):study_label+=' · '+('ChatGPT' if item['source']=='chatgpt' else 'Claude+Gemini')+' review candidate'
    row={'Record ID':'combined:'+digest,'Effect ID':'combined:'+digest,'context_id':record.get('context_id'),
        'PMID':str(record.get('pmid')),'PMCID':record.get('pmcid'),'Study':study_label,
        'Reference':record.get('title'),'Title':record.get('title'),'Authors':', '.join(authors),'Journal':record.get('journal'),
        'Year':year.group() if year else None,'Link':record.get('pubmed_url') or 'https://pubmed.ncbi.nlm.nih.gov/'+str(record.get('pmid'))+'/',
        'Direct Exposure':record.get('direct_exposure') or exposure,'direct_exposure':record.get('direct_exposure') or exposure,'Displayed Exposure':exposure,
        'Effect Type':typ,'Effect Size':effect,'Lower CI':lower,'Upper CI':upper,
        'Original Effect Size':original_effect,'Original Lower CI':original_lower,'Original Upper CI':original_upper,'Original Effect Type':typ,'effect_inverted':inverted,
        'combined_direction_harmonization':{'method':'Reciprocal ratio and reversed reciprocal CI bounds, matching the saved source inversion flag.','source_needs_inversion':True} if inverted else None,'P Value':number(e.get('p_value')),'Sample Size':int(n) if n is not None and n.is_integer() else n,'Cases':number(e.get('cases')),'SE':se,
        'Quality Score':jbi.get('grade'),'Quality %':jbi.get('score_percent'),'JBI':copy.deepcopy(jbi.get('answers') or jbi),
        'Design':e.get('design') or e.get('study_design'),'Timing':e.get('timing'),'Continent':e.get('continent'),'Population':e.get('population'),
        'Gender':e.get('gender'),'Ethnicity':e.get('ethnicity'),'Smoking Status':e.get('smoking_status'),'Stage':e.get('stage'),
        'comparison_type':e.get('comparison_type'),'exposure_measurement_type':e.get('exposure_measurement_type'),'exposure_subtype':e.get('exposure_subtype'),
        'extraction_evidence':copy.deepcopy(evidence),'extraction_supporting_text':{key:value.get('excerpt') for key,value in evidence.items() if isinstance(value,dict)},
        'exposure_measurement_supporting_text':(evidence.get('exposure_measurement') or {}).get('excerpt'),
        'policy_poolable':poolable,'Primary Estimate':poolable,'combined_source_estimate_index':item['source_estimate_index'],'canonical_track':'combined','combined_source':item['source'],
        'requires_key_review':item['requires_key_review'],'key_differences':item['key_differences'],'combined_nonkey_fills':item['nonkey_fills'],
        'combined_p_value_resolution':item['p_resolution'],'combined_source_extraction':copy.deepcopy(e),
        'final_status':'INCLUDE_META' if poolable else 'ELIGIBLE_NOT_POOLABLE'}
    for key in [*dietary.AUDIT_FIELDS,'direction_imputed','ci_level_assumed','effect_size_imputed','effect_is_adjusted','comparison_direction','comparison_group','reference_group','unit','scale','adjusted_model','subgroup','stratum','independent_population_id','Independent Population ID']:
        if key in e:row[key]=copy.deepcopy(e[key])
    for key in ['scientific_pooling_eligible','nonpoolable_status_resolution','manual_review','manual_poolable','decision_origin','shared_outcome_estimate_id','broader_outcome_projection','hierarchy_inherited','inherited_exposure_memberships']:
        if key in record:row[key]=copy.deepcopy(record[key])
    if item.get('manual_review_pending'):
        row['manual_review_pending']=True
        row['provisional_ui_selection']=copy.deepcopy(item.get('provisional_ui_selection') or {})
    if row['requires_key_review']:
        row['manual_poolable']=False
        row['display_only_reason']=(item.get('provisional_ui_selection') or {}).get('reason') or 'Key numeric disagreement; both track candidates retained for manual review.'
    return dietary.apply_row(row,exposure)


def materialize(groups):
    from scripts import rebuild_relaxed_poolability as existing
    import pandas as pd
    import meta_analysis
    count=0;errors=[];pooled=0;displayed=0
    for (exposure,disease,endpoint),items in sorted(groups.items()):
        rows=[app_row(item) for item in items]
        if not rows:continue
        for row in rows:
            cases=row.get('Cases')
            if isinstance(cases,float) and cases.is_integer():row['Cases']=int(cases)
            row.update(meta_analysis.add_estimated_cases_to_row(row,disease))
            if meta_analysis.is_curated_meta_analysis_exclusion(row.get('PMID'),disease,exposure,endpoint):
                row['policy_poolable']=False;row['Primary Estimate']=False
                row['display_only_reason']='Existing curated clinical exclusion remains active.'
        from scripts.subexposure_policy import project_rows, pooling_rows
        rows.sort(key=lambda row:row['combined_source_estimate_index'])
        rows=project_rows({exposure:rows},exposure,{},include=False)
        pool=pooling_rows(rows)
        result={}
        if pool:
            try:result=meta_analysis.perform_meta_analysis(pd.DataFrame(pool),disease,exposure,outcome=endpoint,exclude_meta=True,df_all=pd.DataFrame(rows),generate_plots=False)
            except (ValueError,TypeError,KeyError,RuntimeError) as exc:errors.append({'scope':[exposure,disease,endpoint],'error':str(exc)})
            if result.get('error'):errors.append({'scope':[exposure,disease,endpoint],'error':result['error']})
        packet={key:result.get(key) for key in ['headline','summary_html','screening_stats']}
        packet.update(studies=rows,exposure=exposure,disease=disease,cancer=disease,endpoint=endpoint,outcome=endpoint,canonical_track='combined',
            plot_url=None,funnel_plot_url=None,baujat_plot_url=None,requires_key_review=any(row['requires_key_review'] for row in rows),
            combined_reconciliation={'source':'outputs/track_comparison_2026-09-05/discrepancies.json','original_screening_tracks_unchanged':True})
        for path in existing.cache_paths(exposure,disease,endpoint):
            target=COMBINED/path.relative_to(ROOT/'Cached_results')
            if target.exists():
                backup_relative=target.resolve().relative_to(ROOT) if target.resolve().is_relative_to(ROOT) else Path('external_saved_view')/target.relative_to(COMBINED)
                backup=AUDIT/'before'/backup_relative
                if not backup.exists():backup.parent.mkdir(parents=True,exist_ok=True);backup.write_bytes(target.read_bytes())
            write(target,packet);count+=1
        displayed+=len(rows);pooled+=len(pool)
    return {'cache_files':count,'displayed_rows':displayed,'pooling_rows':pooled,'errors':errors}


def p_audits(tracks):
    audits=[]
    for cid in sorted(set(tracks['chatgpt'])|set(tracks['claude_gemini'])):
        gpt=tracks['chatgpt'].get(cid);cg=tracks['claude_gemini'].get(cid);context=cg or gpt
        human=gpt if manual(gpt) else cg if manual(cg) else None
        for i,pair in enumerate(policy.compare_context(gpt,cg)['pairs']):
            resolution=pair.get('p_resolution') or {}
            if not resolution.get('needed'):continue
            left,right=pair['chatgpt'],pair['claude_gemini'];exposure,disease,endpoint=scope(context)
            audit={'repo':ROOT.name,'context_id':cid,'pmid':str(context.get('pmid')),'title':context.get('title'),
                'exposure':exposure,'disease':disease,'endpoint':endpoint,'estimate_pair':i+1,
                'chatgpt':numeric_snapshot(left),'claude_gemini':numeric_snapshot(right),
                'chatgpt_p_value':left.get('p_value'),'chatgpt_p_value_relation':left.get('p_value_relation'),
                'claude_gemini_p_value':right.get('p_value'),'claude_gemini_p_value_relation':right.get('p_value_relation'),
                **copy.deepcopy(resolution),'manual_authoritative':bool(human),
                'applied_to_combined':not human and resolution.get('resolution')=='computed_two_sided_wald',
                'reason_if_unavailable':'A valid point estimate and CI on a supported, consistent Wald effect scale are unavailable.' if resolution.get('resolution')=='wald_unavailable' else None}
            if human:
                audit['combined_p_value']=(left if human is gpt else right).get('p_value')
                audit['combined_p_value_relation']=(left if human is gpt else right).get('p_value_relation')
                audit['manual_resolution_note']='The existing human-adjudicated p value remains authoritative.'
            audits.append(audit)
    return audits


def main():
    protected=protected_sources()
    tracks={track:{row['context_id']:row for row in read(SOURCE_DIR/f'{track}_effective_results.json')['results']} for track in ['chatgpt','claude_gemini']}
    p_records=p_audits(tracks)
    write(AUDIT/'p_resolutions.json',p_records)
    p_counts={'p_resolution_records':len(p_records),'p_resolution_status_counts':dict(Counter(row['resolution'] for row in p_records)),
        'p_matching_source_counts':dict(Counter(str(row.get('matching_source')) for row in p_records))}
    if '--p-audits-only' in sys.argv:
        report=read(AUDIT/'application_report.json');report.update(p_counts)
        assert protected_sources()==protected
        write(AUDIT/'application_report.json',report);print(json.dumps(p_counts,indent=2));return
    records=[];differences=[];groups=defaultdict(list)
    for cid in sorted(set(tracks['chatgpt'])|set(tracks['claude_gemini'])):
        record,entries,issues=reconcile_context(tracks['chatgpt'].get(cid),tracks['claude_gemini'].get(cid))
        from scripts import astra_fulltext_overlay as fulltext
        replayed=fulltext.replay_record(record,tracks['chatgpt'].get(cid),tracks['claude_gemini'].get(cid),COMBINED)
        from scripts import human_review_overlay
        replayed=human_review_overlay.replay_record(replayed,tracks['chatgpt'].get(cid),tracks['claude_gemini'].get(cid),COMBINED)
        from scripts import curator_dedup_overlay
        replayed=curator_dedup_overlay.replay_record(replayed,tracks['chatgpt'].get(cid),tracks['claude_gemini'].get(cid),COMBINED)
        if replayed is not record:
            record=replayed;entries=fulltext.candidates_for_record(record,tracks)
        records.append(record);differences.extend(issues)
        groups[scope(record)].extend(entries)
    write(AUDIT/'discrepancies.json',differences)
    write(COMBINED/'effective_results.json',{'canonical_track':'combined','source_packets_unchanged':True,'results':records})
    report=materialize(groups)
    report.update(**p_counts,contexts=len(records),key_discrepancy_pairs=len(differences),key_discrepancy_contexts=len({row['context_id'] for row in differences}),
        source_files_sha256_verified_unchanged=protected,manual_contexts_preserved=sum(bool(row.get('combined_manual_authority')) for row in records),new_gateway_calls=0)
    assert protected_sources()==protected
    write(AUDIT/'application_report.json',report)
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
