import copy
from scripts import key_comparison_policy as policy
from scripts import reconcile_screening_tracks as reconcile
from scripts import nonpoolable_display_selection as selection
from scripts import build_provisional_review_ui as ui
from scripts import astra_fulltext_overlay as fulltext
from scripts import apply_astra_adjudication as abstract
from scripts import migrate_both_nonpoolable as migration

def record(extraction,grade='Good',**extra):
 result={'context_id':'1|Tea|Test','pmid':'1','exposure':'Tea','cancer':'Test','disease':'Test','outcome':'Incidence',
         'title':'A study','final_status':'ELIGIBLE_NOT_POOLABLE','eligibility':'INCLUDE','jbi':{'grade':grade},'extraction':extraction}
 result.update(extra);return result

def test_selects_atomic_richer_record_and_never_pools_complete_numbers():
 gpt=record({'effect_size':1.4,'effect_type':'RR','ci_lower':1.1,'ci_upper':1.8,'total_n':1000,'cases':10},grade='Fair')
 cg=record({'effect_size':.8,'effect_type':'RR','country':'USA'})
 before=copy.deepcopy((gpt,cg));resolution=policy.nonpoolable_status_resolution(gpt,cg)
 assert resolution['retained_track']=='chatgpt' and resolution['resolution_kind']=='both_eligible_not_poolable'
 result,candidates,issues=reconcile.reconcile_context(gpt,cg)
 assert result['jbi']==gpt['jbi'] and 'country' not in result['extraction'] and not result['combined_nonkey_fills']
 assert result['extraction']['effect_size']==1.4 and result['extraction']['total_n']==1000
 assert not issues and result['combined_numeric_consensus'] is False and result['scientific_pooling_eligible'] is False
 assert all(reconcile.app_row(item)['policy_poolable'] is False for item in candidates)
 assert (gpt,cg)==before

def test_equal_information_uses_chatgpt_without_jbi_or_prose_preference():
 gpt=record({'effect_size':1.4,'effect_type':'RR'},grade='Fair')
 cg=record({'effect_size':.8,'effect_type':'RR'},reason='long audit '*1000)
 resolution=policy.nonpoolable_status_resolution(gpt,cg)
 assert resolution['equal_information'] and resolution['retained_track']=='chatgpt'
 cg['extraction']['total_n']=100
 assert policy.nonpoolable_status_resolution(gpt,cg)['retained_track']=='claude_gemini'

def test_aliases_placeholders_duplicate_extracts_do_not_inflate_information():
 row=record({'effect_size':1.2,'Effect Size':1.2,'total_n':'not reported','country':'USA','location':'USA'})
 base=selection.information_profile(row)['score']
 row['additional_extractions']=[copy.deepcopy(row['extraction'])]*3
 assert selection.information_profile(row)['score']==base
 row['jbi']['grade']='Moderate';row['extraction']['cases']=float('nan')
 assert selection.information_profile(row)['score']==base

def test_all_replay_entrypoints_keep_single_source_and_are_idempotent():
 a,b=record({'effect_size':1.4,'total_n':1000,'cases':10}),record({'effect_size':.8})
 tracks={'chatgpt':{a['context_id']:a},'claude_gemini':{b['context_id']:b}};entries=[{'id':'old-review'}]
 prior=record({'effect_size':.2},final_status='INCLUDE_META')
 for apply in [ui.apply_context,fulltext.apply_context,abstract.apply_context]:
  first,items,_=apply(prior,tracks,entries);second,_,_=apply(first,tracks,entries)
  assert first==second
  assert first['extraction']['effect_size']==1.4 and first['jbi']==a['jbi']
  assert all(reconcile.app_row(item)['scientific_pooling_eligible'] is False for item in items)

def test_human_and_prior_exclude_nonpoolable_rules_remain_unchanged():
 a=record({'effect_size':1.4},manual_review={'decision':'keep display'})
 b=record({'effect_size':.8,'total_n':1000})
 assert policy.nonpoolable_status_resolution(a,b) is None
 a.pop('manual_review');a['final_status']='EXCLUDE';a['eligibility']='EXCLUDE'
 assert policy.nonpoolable_status_resolution(a,b)=={'retained_track':'claude_gemini','final_status':'ELIGIBLE_NOT_POOLABLE'}

def test_normal_duplicate_election_selects_eligible_alternate_only():
 baseline={'PMID':'1','Direct Exposure':'Tea','Displayed Exposure':'Tea','Quality Score':'Good','Effect Type':'RR','Effect Size':1.4,'Lower CI':1.1,'Upper CI':1.8,'Sample Size':1000,'Primary Estimate':True,'policy_poolable':True,'final_status':'INCLUDE_META'}
 target={**baseline,'Record ID':'target','context_id':'target','subexposure_pooling_selected':True}
 alternate={**baseline,'Record ID':'alternate','context_id':'alternate','subexposure_pooling_selected':False}
 held=migration.held(target,{'retained_track':'chatgpt'})
 result=migration.reelect([held,alternate],{'exposure':'Tea','include_subexposures':'no'},{})
 assert result[0]['subexposure_pooling_selected'] is False and result[1]['subexposure_pooling_selected'] is True
 assert {k:v for k,v in result[1].items() if k not in migration.SELECTION_FIELDS}=={k:v for k,v in alternate.items() if k not in migration.SELECTION_FIELDS}


def test_saved_retention_version_and_independent_pending_are_respected(tmp_path):
 a,b=record({'effect_size':1.4,'total_n':1000}),record({'effect_size':.8})
 tracks={'chatgpt':{a['context_id']:a},'claude_gemini':{b['context_id']:b}}
 saved,_,_=reconcile.reconcile_context(a,b)
 saved['nonpoolable_status_resolution']['display_selection_version']='old-version'
 saved['extraction']['effect_size']=.2
 assert abstract.candidates_for_record(saved,tracks)[0]['extraction']['effect_size']==1.4
 assert fulltext.replay_record(saved,a,b,tmp_path)['extraction']['effect_size']==1.4
 saved,_,_=reconcile.reconcile_context(a,b)
 saved.update(manual_review_pending=True,requires_key_review=True)
 recovered=abstract.candidates_for_record(saved,tracks)
 assert all(reconcile.app_row(item)['manual_review_pending'] and not reconcile.app_row(item)['policy_poolable'] for item in recovered)


def test_both_without_quantitative_extractions_still_display_one_source():
 a,b=record(None,grade='Fair'),record(None)
 result,candidates,issues=reconcile.reconcile_context(a,b)
 assert result['jbi']==a['jbi'] and len(candidates)==1 and not issues
 row=reconcile.app_row(candidates[0])
 assert row['Sample Size'] is None and row['Effect Size'] is None and row['policy_poolable'] is False
