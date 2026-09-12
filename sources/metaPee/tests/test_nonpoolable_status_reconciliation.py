"""Only the excluded/nonpoolable pair resolves without human numeric review."""
import copy,json
from pathlib import Path
from scripts import key_comparison_policy as policy
from scripts import reconcile_screening_tracks as reconcile
from scripts import provisional_review_policy as provisional
from scripts import build_provisional_review_ui as ui
from scripts import apply_astra_adjudication as abstract
from scripts import astra_fulltext_overlay as fulltext
from scripts import astra_fulltext_common as common


def record(status,effect=1.4,**extra):
 row={'context_id':'1|Coffee|Test','pmid':'1','exposure':'Coffee','disease':'Test','cancer':'Test','outcome':'Incidence',
      'final_status':status,'eligibility':'EXCLUDE' if status=='EXCLUDE' else 'INCLUDE','jbi':{'grade':'Good'},
      'extraction':{'total_n':1000,'effect_type':'RR','effect_size':effect,'ci_lower':1.1,'ci_upper':1.8}}
 row.update(extra);return row


def test_both_orders_retain_nonpoolable_bundle_without_agreement_or_pooling():
 for left,right in [(record('EXCLUDE'),record('ELIGIBLE_NOT_POOLABLE')),(record('ELIGIBLE_NOT_POOLABLE'),record('EXCLUDE'))]:
  before=copy.deepcopy((left,right));comparison=policy.compare_context(left,right)
  assert comparison['pairs']==[] and not comparison['has_key_discrepancy'] and not comparison['both_excluded']
  result,candidates,issues=reconcile.reconcile_context(left,right)
  assert result['final_status']=='ELIGIBLE_NOT_POOLABLE' and result['eligibility']=='INCLUDE'
  assert result['scientific_pooling_eligible'] is False and result['combined_numeric_consensus'] is False
  assert not result['requires_key_review'] and not issues and candidates
  assert result['extraction']==policy.normalise_estimate((left if left['final_status']=='ELIGIBLE_NOT_POOLABLE' else right)['extraction'])
  assert all(not reconcile.app_row(item)['policy_poolable'] for item in candidates)
  assert all(reconcile.app_row(item)['scientific_pooling_eligible'] is False for item in candidates)
  assert (left,right)==before


def test_all_human_markers_preserve_explicit_decision():
 markers=[{'manual_review':{'decision':'EXCLUDE'}},{'decision_authority':'human'},
          {'manual_full_text_review':{'decision':'EXCLUDE'}},{'combined_adjudication':{'resolution':'HUMAN_EXCLUDE'}}]
 for marker in markers:
  human=record('EXCLUDE',**marker);other=record('ELIGIBLE_NOT_POOLABLE')
  assert policy.nonpoolable_status_resolution(human,other) is None
  result,items,issues=reconcile.reconcile_context(human,other)
  assert result['final_status']=='EXCLUDE' and result['combined_manual_authority'] is True
  assert not items and not issues


def test_other_status_and_numeric_disagreements_remain_reviewable():
 for left,right in [(record('EXCLUDE'),record('INCLUDE_META')),(record('ELIGIBLE_NOT_POOLABLE'),record('INCLUDE_META',1.5)),(None,record('ELIGIBLE_NOT_POOLABLE'))]:
  comparison=policy.compare_context(left,right)
  assert comparison['has_key_discrepancy'] and 'nonpoolable_status_resolution' not in comparison


def test_stale_provisional_selection_cannot_promote_the_complete_side():
 a,b=record('EXCLUDE'),record('ELIGIBLE_NOT_POOLABLE');entries=[{'id':'review-1','manual_review_required':True}]
 selection=provisional.select_pending_pair(a,b,None,b['extraction'],entries)
 assert not selection['pending'] and selection['selection_poolable_candidate'] is False
 assert selection['higher_jbi'] is None
 tracks={'chatgpt':{a['context_id']:a},'claude_gemini':{b['context_id']:b}}
 prior=record('INCLUDE_META')
 for apply in (ui.apply_context,abstract.apply_context,fulltext.apply_context):
  resolved,candidates,audit=apply(prior,tracks,entries)
  assert resolved['final_status']=='ELIGIBLE_NOT_POOLABLE'
  assert all(not reconcile.app_row(item)['policy_poolable'] for item in candidates)
  assert all(not event.get('follow_up_required') and not event.get('pending') for event in audit)


def test_old_automated_checkpoint_cannot_restore_pooling(tmp_path):
 a,b=record('EXCLUDE'),record('ELIGIBLE_NOT_POOLABLE');resolved,_,_=reconcile.reconcile_context(a,b)
 old=record('INCLUDE_META');packet={'record_snapshots':{a['context_id']:{'source_fingerprint':common.source_digest(a,b),'record':old,'record_sha256':common.digest(old)}}}
 path=tmp_path/common.CHECKPOINT_NAME;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(packet))
 assert fulltext.replay_record(resolved,a,b,tmp_path)['final_status']=='ELIGIBLE_NOT_POOLABLE'
 candidates=abstract.candidates_for_record(resolved,{'chatgpt':{a['context_id']:a},'claude_gemini':{b['context_id']:b}})
 assert all(not reconcile.app_row(item)['policy_poolable'] for item in candidates)


def test_eligibility_only_exclude_is_the_same_screening_status():
 excluded=record('EXCLUDE');excluded.pop('final_status')
 retained=record('ELIGIBLE_NOT_POOLABLE')
 assert policy.nonpoolable_status_resolution(excluded,retained)['retained_track']=='claude_gemini'
 assert not policy.compare_context(retained,excluded)['has_key_discrepancy']
