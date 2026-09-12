"""Pair-specific, source-fingerprinted PMC full-text corrections for combined data."""
from __future__ import annotations
import copy
import json
from collections import Counter
from pathlib import Path
from functools import lru_cache
from scripts import astra_fulltext_common as common
from scripts import apply_astra_adjudication as abstract
from scripts import reconcile_screening_tracks as reconcile
from scripts import key_comparison_policy as policy

VERSION=common.VERSION


def candidates_for_record(record,tracks):
    return abstract.candidates_for_record(record,tracks)


def _gate_source(prior,a,b,entry):
    decision=entry['adjudication'];verdict=decision['verdict']
    source=copy.deepcopy(a if verdict=='CHATGPT' else b if verdict=='CLAUDE_GEMINI' else b or a or prior)
    # Quality is not newly assessed by the discrepancy adjudicator. Retain the
    # least permissive complete saved rating, never construct a mixed JBI.
    available=[row['jbi'] for row in (prior,a,b) if row and isinstance(row.get('jbi'),dict) and row['jbi'].get('grade')]
    rank={'poor':0,'fair':1,'moderate':2,'good':3}
    if available:source['jbi']=copy.deepcopy(min(available,key=lambda grade:rank.get(str(grade.get('grade')).casefold(),0)))
    for key in ('manual_poolable','included_in_parent_meta_analysis'):
        if any(row and row.get(key) is False for row in (a,b)):source[key]=False
    for key in ('full_text_pooling_block','exposure_classification_ambiguous','hierarchy_inherited','broader_outcome_projection'):
        if any(row and row.get(key) for row in (prior,a,b)):source[key]=True
    return source


def apply_context(prior,tracks,entries):
    cid=prior['context_id'];a=tracks['chatgpt'].get(cid);b=tracks['claude_gemini'].get(cid)
    items=candidates_for_record(prior,tracks)
    if any(common.is_human(row) for row in (prior,a,b)):
        return copy.deepcopy(prior),items,[{'id':entry['id'],'applied':False,'follow_up_required':False,'reason':'Existing human/manual authority preserved.'} for entry in entries]
    if policy.nonpoolable_status_resolution(a,b):
        resolved, candidates, _ = reconcile.reconcile_context(a,b)
        independent_ids = reconcile.preserve_independent_review(resolved,candidates,entries)
        existing_resolution = prior.get('nonpoolable_status_resolution') or {}
        resolution = resolved['nonpoolable_status_resolution']
        unchanged_retention = (resolution.get('resolution_kind') == 'both_eligible_not_poolable' and existing_resolution == resolution)
        previous = prior.get('prior_automated_adjudication') if unchanged_retention else prior
        if previous is not None:resolved['prior_automated_adjudication'] = copy.deepcopy(previous)
        return resolved, candidates, [{'id': entry['id'], 'applied': False, 'follow_up_required': entry['id'] in independent_ids,
            'reason': ('Independent scientific/manual adjudication remains pending.' if entry['id'] in independent_ids else resolved['poolability_reason'])} for entry in entries]
    accepted=[item for item in items if not item['requires_key_review']]
    pending=[item for item in items if item['requires_key_review']]
    pair_counts=Counter(common.pair_index(entry) for entry in entries)
    comparison=policy.compare_context(a,b)
    resolved_before={row.get('pair_index') for row in prior.get('astra_adjudication_history',[]) if row.get('applied')}
    resolved_before|={row.get('pair_index') for row in prior.get('astra_fulltext_history',[]) if row.get('applied')}
    remaining={index for index,pair in enumerate(comparison['pairs']) if pair.get('differences')}-resolved_before
    remaining|={item['pair_index'] for item in pending}
    audit=[];changed=set();excluded=set()
    historical={event['id']:event for event in prior.get('astra_fulltext_history',[]) if event.get('applied')}
    for entry in entries:
        index=common.pair_index(entry);extraction=None
        previous=historical.get(entry['id'])
        if previous and prior.get('astra_fulltext_source_digest')==common.source_digest(a,b) and previous.get('adjudication')==entry.get('adjudication'):
            audit.append({**copy.deepcopy(previous),'replayed_existing_decision':True});continue
        if pair_counts[index]>1:reason='Duplicate full-text decisions for one estimate pair require review.'
        else:extraction,reason=common.validate_entry(entry,a,b)
        if not reason:
            source=_gate_source(prior,a,b,entry)
            pair=comparison['pairs'][index]
            if extraction is not None:
                originals=[pair.get('chatgpt'),pair.get('claude_gemini')]+[item['extraction'] for item in accepted+pending if item['pair_index']==index]
                extraction=common.preserve_scientific_gates(extraction,originals)
                for key in ('full_text_pooling_block','exposure_classification_ambiguous','hierarchy_inherited','broader_outcome_projection'):
                    if extraction.get(key):source[key]=True
            # A pair with a previous accepted partial bundle is replaceable;
            # all unrelated accepted and disputed pairs remain byte-for-byte.
            accepted=[item for item in accepted if item['pair_index']!=index]
            pending=[item for item in pending if item['pair_index']!=index]
            changed.add(index);remaining.discard(index)
            if entry['adjudication']['eligibility']=='EXCLUDE':
                excluded.add(index)
                audit.append({'id':entry['id'],'pair_index':index,'applied':True,'follow_up_required':False,'reason':'PMC full text supports exclusion of this estimate pair.','adjudication':copy.deepcopy(entry['adjudication'])})
                continue
            pair=comparison['pairs'][index]
            source_index=pair.get('claude_gemini_index')
            if entry['adjudication']['verdict']=='CHATGPT':source_index=pair.get('chatgpt_index')
            if source_index is None:source_index=pair.get('chatgpt_index')
            source_index=source_index if source_index is not None else index
            item=reconcile.candidate(source,extraction,'astra_fulltext',index,source_index=source_index)
            item=abstract.checked_status(item)
            item['astra_fulltext_adjudication']=copy.deepcopy(entry['adjudication'])
            item['astra_fulltext_review_id']=entry['id']
            item['astra_follow_up_required']=not common.coherent(item['extraction'])
            accepted.append(item)
            follow=item['astra_follow_up_required']
            audit.append({'id':entry['id'],'pair_index':index,'applied':True,'follow_up_required':follow,'reason':
                'Supported PMC full-text values applied; quantitative information remains incomplete.' if follow else
                'Supported PMC full-text numeric bundle applied to this estimate pair.','adjudication':copy.deepcopy(entry['adjudication'])})
        else:audit.append({'id':entry['id'],'pair_index':index,'applied':False,'follow_up_required':True,'reason':reason,'adjudication':copy.deepcopy(entry['adjudication'])})
    if not changed:return copy.deepcopy(prior),items,audit
    accepted.sort(key=lambda item:(item['pair_index'],item['source_estimate_index'],item['source']))
    result=copy.deepcopy(prior)
    result.update(extraction=copy.deepcopy(accepted[0]['extraction']) if accepted else None,
        additional_extractions=[copy.deepcopy(item['extraction']) for item in accepted[1:]],
        combined_accepted_estimate_sources=[abstract.candidate_receipt(item) for item in accepted],
        key_review_candidates=[{k:copy.deepcopy(v) for k,v in item.items() if k!='source_record'} for item in pending],
        requires_key_review=bool(remaining),combined_remaining_key_review_pairs=sorted(remaining),
        combined_numeric_resolved=bool(accepted) and not remaining,
        astra_fulltext_version=VERSION,astra_fulltext_source_digest=common.source_digest(a,b),
        astra_fulltext_history=[*prior.get('astra_fulltext_history',[]),*audit])
    result['needs_full_text']=bool(remaining or any(item.get('astra_follow_up_required') for item in accepted))
    result['astra_fulltext_follow_up_required']=result['needs_full_text']
    result['combined_decision_authority']='astra_pmc_fulltext_partial' if result['needs_full_text'] else 'astra_pmc_fulltext'
    if not accepted and not remaining and excluded:
        result.update(eligibility='EXCLUDE',final_status='EXCLUDE',full_text_status='EXCLUDE',poolability_reason='PMC full-text adjudication excludes all remaining estimate pairs.')
    else:
        poolable=any(reconcile.app_row(item)['policy_poolable'] for item in accepted)
        result.update(eligibility='INCLUDE',final_status='INCLUDE_META' if poolable else 'ELIGIBLE_NOT_POOLABLE',
                      full_text_status='INCLUDE_META' if poolable else 'ELIGIBLE_NOT_POOLABLE')
    return result,accepted+pending,audit


@lru_cache(maxsize=8)
def _checkpoint(path,mtime_ns,size):return json.loads(Path(path).read_text())


def replay_record(record,a,b,combined_root):
    """Restore finalized snapshot only while both source records are unchanged."""
    if record.get('nonpoolable_status_resolution') and not any(common.is_human(row) for row in (record,a,b)):
        resolution=policy.nonpoolable_status_resolution(a,b)
        if resolution and resolution.get('resolution_kind')=='both_eligible_not_poolable' and record.get('nonpoolable_status_resolution')!=resolution:
            return reconcile.reconcile_context(a,b)[0]
        return record
    path=Path(combined_root)/common.CHECKPOINT_NAME
    if not path.exists() or any(common.is_human(row) for row in (record,a,b)):return record
    stat=path.stat();packet=_checkpoint(str(path),stat.st_mtime_ns,stat.st_size)
    saved=(packet.get('record_snapshots') or {}).get(record['context_id'])
    if saved and saved.get('source_fingerprint')==common.source_digest(a,b) and saved.get('record_sha256')==common.digest(saved.get('record')):
        return copy.deepcopy(saved['record'])
    return record
