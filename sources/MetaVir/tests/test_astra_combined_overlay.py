import copy
import unittest
from scripts import astra_combined_overlay as overlay

CID='123|Milk|Breast cancer'
def source(point=1.2,quality='Fair'):
 return {'context_id':CID,'pmid':'123','exposure':'Milk','eligibility':'INCLUDE','final_status':'ELIGIBLE_NOT_POOLABLE',
         'extraction':{'total_n':1000,'effect_type':'RR','effect_size':point,'ci_lower':.9,'ci_upper':1.5},'jbi':{'grade':quality}}
def entry(verdict='A',eligibility='INCLUDE'):
 return {'id':'review-1','source_row':{'Context ID':CID,'Estimate pair':'1','ChatGPT estimate index':'0','Claude+Gemini estimate index':'0',
     'ChatGPT sample size':'1000','ChatGPT effect size':'1.2','ChatGPT CI lower':'.9','ChatGPT CI upper':'1.5'},
     'selected_pipeline':'chatgpt' if verdict=='A' else None,
     'adjudication':{'verdict':verdict,'eligibility':eligibility,'confidence':'high','source_quote_validated':True,
     'evidence_quote':'Among 1000 participants the RR was1.2 (95% CI .9–1.5).','numeric_values':{'sample_size':1000,'effect_size':1.2,'ci_lower':.9,'ci_upper':1.5},'effect_type':'RR','reason':'The abstract provides the same estimate.'}}

class AstraOverlayTests(unittest.TestCase):
 def setUp(self):
  self.a=source();self.b=source(1.3,'Good');self.previous=source(1.3,'Good');self.tracks={'chatgpt':self.a,'claude_gemini':self.b}
 def apply(self,item=None,**kwargs):return overlay.apply_entry(self.previous,self.tracks,item or entry(),**kwargs)
 def test_verified_source_bundle_and_quality_are_selected_without_mutating_inputs(self):
  old=copy.deepcopy((self.previous,self.tracks));result,audit=self.apply()
  self.assertTrue(audit['applied']);self.assertEqual(result['extraction']['effect_size'],1.2)
  self.assertEqual(result['jbi'],{'grade':'Fair'});self.assertEqual((self.previous,self.tracks),old)
  self.assertEqual(result['final_status'],'ELIGIBLE_NOT_POOLABLE')
 def test_low_confidence_or_unvalidated_quote_cannot_change_combined(self):
  for change in [{'confidence':'medium'},{'source_quote_validated':False},{'evidence_quote':''}]:
   item=entry();item['adjudication'].update(change);result,audit=self.apply(item)
   self.assertFalse(audit['applied']);self.assertEqual(result,self.previous)
 def test_human_authority_has_priority(self):
  for field in ['human_adjudication','manual_review','manual_full_text_adjudication']:
   self.previous[field]={'reviewed':True};result,audit=self.apply();self.assertFalse(audit['applied']);self.assertEqual(result,self.previous);del self.previous[field]
  self.assertFalse(self.apply(protected=True)[1]['applied'])
 def test_numeric_disagreement_with_selected_source_is_held(self):
  item=entry();item['adjudication']['numeric_values']['sample_size']=1200;self.assertFalse(self.apply(item)[1]['applied'])
  item=entry();item['source_row']['ChatGPT effect size']='1.1';self.assertFalse(self.apply(item)[1]['applied'])
 def test_exclusion_does_not_need_a_quantitative_bundle(self):
  self.a['extraction']=None;item=entry(eligibility='EXCLUDE');result,audit=self.apply(item)
  self.assertTrue(audit['applied']);self.assertEqual(result['final_status'],'EXCLUDE')
 def test_multiple_estimates_and_conflicting_verdict_are_held(self):
  item=entry();item['source_row']['Estimate pair']='2';self.assertFalse(self.apply(item)[1]['applied'])
  item=entry();item['selected_pipeline']='claude_gemini';self.assertFalse(self.apply(item)[1]['applied'])
  self.a['additional_extractions']=[{**self.a['extraction'],'effect_size':1.4}];self.assertFalse(self.apply()[1]['applied'])
 def test_blinded_letters_use_the_collector_pipeline_mapping(self):
  item=entry('B');item['selected_pipeline']='chatgpt';result,audit=self.apply(item)
  self.assertTrue(audit['applied']);self.assertEqual(result['extraction']['effect_size'],1.2)
 def test_both_requires_agreeing_saved_keys(self):
  self.assertFalse(self.apply(entry('BOTH'))[1]['applied'])
  self.b['extraction']['effect_size']=1.2;self.assertTrue(self.apply(entry('BOTH'))[1]['applied'])
 def test_custom_numbers_preserve_old_bundle_and_gate(self):
  item=entry('NEITHER');item['adjudication']['numeric_values']['effect_size']=1.4
  self.b['manual_poolable']=False
  result,audit=self.apply(item);self.assertTrue(audit['applied']);self.assertEqual(result['extraction']['effect_size'],1.4)
  self.assertEqual(result['extraction']['astra_replaced_extraction']['effect_size'],1.3);self.assertTrue(result['astra_preserved_pooling_hold'])
  self.b['extraction']['needs_inversion']=True;self.assertFalse(self.apply(item)[1]['applied'])
 def test_nonkey_fill_does_not_mix_pvalue_or_quality(self):
  self.b['extraction'].update(design='cohort',p_value=.04)
  result,audit=self.apply();self.assertEqual(result['extraction']['design'],'cohort');self.assertIsNone(result['extraction'].get('p_value'));self.assertEqual(result['jbi']['grade'],'Fair')
 def test_clear_partial_source_is_retained_unpooled_with_follow_up(self):
  self.a['extraction']['total_n']=None
  item=entry();item['adjudication']['numeric_values']['sample_size']=None;item['source_row']['ChatGPT sample size']=''
  result,audit=self.apply(item)
  self.assertTrue(audit['applied']);self.assertTrue(audit['follow_up_required'])
  self.assertEqual(result['final_status'],'ELIGIBLE_NOT_POOLABLE');self.assertIsNone(result['extraction']['total_n'])
  self.assertTrue(result['astra_preserved_pooling_hold'])
  self.assertIsNone(result['extraction']['total_n']) # Never fill a key quantity from the other model.
  item['adjudication']['numeric_values']['sample_size']=1000
  result,audit=self.apply(item);self.assertTrue(audit['applied']);self.assertIsNone(result['extraction']['total_n'])
 def test_invalid_partial_source_is_held(self):
  self.a['extraction'].update(total_n=None,ci_lower=1.5,ci_upper=.9)
  self.assertFalse(self.apply()[1]['applied'])
 def test_all_dispositions_have_follow_up_and_retained_authority(self):
  item=entry();item['adjudication']['confidence']='medium'
  result,audit=self.apply(item);self.assertTrue(audit['follow_up_required']);self.assertIn('context_authority',audit)
  result,audit=self.apply(protected=True);self.assertFalse(audit['follow_up_required']);self.assertEqual(audit['context_authority'],'human/manual')
 def test_duplicate_context_reviews_fail_closed(self):
  results,audit=overlay.apply_entries({CID:self.previous},{track:{CID:row} for track,row in self.tracks.items()},[entry(),entry()])
  self.assertEqual(results[CID],self.previous);self.assertEqual(sum(x['applied'] for x in audit),0)

if __name__=='__main__':unittest.main()
