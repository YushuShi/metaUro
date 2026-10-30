import copy,json,tempfile,unittest
from pathlib import Path
import native_overlay_converter as native
import onco_overlay_projection as projection

class OncoProjectionTests(unittest.TestCase):
 def fixture(self,root,existing=False):
  root=Path(root);onco=root/'metaOnco';directory=root/'native';snapshot=onco/'data/metaonco.json';identity=native.identity_function('metaMeta/metaOnco')
  selector={'source_repo':'metaGut','disease':'Colorectal cancer','exposure':'Legumes','outcome':'Incidence','include_subexposures':'yes','source_variant':'combined'};target='99999999|Legumes|Colorectal cancer';cache='Cached_results/legumes/colorectal_true_all.json'
  row={'Effect ID':'preprint:metaGut:PPR1:v1:new','PMID':'99999999','Study':'Recovered journal anchor','Effect Type':'OR','Effect Size':.7,'Lower CI':.58,'Upper CI':.83,'preprint_derived':True,'policy_poolable':True,'Primary Estimate':True,'cohort_names':['NHS II'],'publication_date':'2024','publication_date_precision':'year'}
  old={'Effect ID':'peer-reviewed1','PMID':'11111111','Study':'Ordinary peer-reviewed report','Effect Type':'RR','Effect Size':1.2,'Lower CI':1.05,'Upper CI':1.4};old_target={'pmid':'99999999','record_id':'old-target','study':'Journal old source','effect_type':'OR','effect':1.1,'ci_low':.9,'ci_high':1.3}
  metric={'evidence':[old_target]} if existing else None;results=[{'exposure_id':'legumes','cancer_id':'colorectal_cancer','combined':metric}] if existing else [];native.save(snapshot,{'results':results})
  gate={'pmid':'99999999','availability':'pmcid_missing','pmcids':[],'preprint_fetch_allowed':True,'model_dispatch_authorized':True,'saved_any_track_checked':True,'saved_any_track_full_text_available':False,'any_track_gate_record_sha256':'c'*64};gate_path=directory/'article_availability_gate.json';native.save(gate_path,{'schema_version':1,'records':[gate]})
  context={'context_id':'native1','review_context_id':target,'selector':selector,'review':{'decision':'include','status':'adjudicated'},'operation':'replace','original_record_ids':['id:native-old'],'native_rows':[row],'publication_anchor':{'pmid':'99999999','journal_publication_date':'2024','journal_date_precision':'year'},'source_cache_paths':[cache],'baseline_files':{cache:'a'*64},'article_availability_gate':{'path':'data/preprint_recovery/article_availability_gate.json','sha256':native.sha(gate_path),'pmid':'99999999','record_sha256':native.digest(gate)},'preprint_source':{'id':'PPR1','version':'1'}}
  manifest=directory/'active_manifest.json';native.save(manifest,{'schema_version':1,'status':'complete','qa_only':True,'contexts':[context]})
  selected=[old,row];cell={**selector,'dataset':'combined','source_cache_paths':[cache],'selected_rows':selected,'selected_record_ids':[identity(r) for r in selected],'selected_rows_sha256':native.digest(selected),'pooled_n':2,'headline':{'RR':.9},'preprint_manifest_sha256':native.sha(manifest)};receipt=root/'native_receipt.json';native.save(receipt,{'keep_latest_cohort':False,'native_pooling_cells':[cell]})
  mapping={'context_id':target,'pmid':'99999999','native_selector':selector,'projection':{'exposure_id':'legumes','cancer_id':'colorectal_cancer','dataset':'combined'},'required_projection_selector_fields':{'projection_exposure_id':'legumes','projection_cancer_id':'colorectal_cancer','projection_dataset':'combined'},'snapshot_path':'data/metaonco.json','snapshot_sha256':native.sha(snapshot),'native_source_cache_path':cache,'native_source_cache_sha256':'a'*64,'native_and_snapshot_source_cache_identical':existing,'existing_metric':existing,'original_evidence_for_pmid':[old_target] if existing else []};mapping_path=root/'mapping.json';native.save(mapping_path,{'schema_version':1,'mapping_rows':[mapping]});return onco,manifest,mapping_path,receipt,row,old
 def test_missing_metric_retains_ordinary_native_selected_evidence(self):
  with tempfile.TemporaryDirectory() as root:
   onco,manifest,mapping,receipt,new,old=self.fixture(root);before={str(p):native.sha(p) for p in onco.rglob('*') if p.is_file()};out=Path(root)/'out';result=projection.project_manifests([manifest],mapping,[receipt],out,onco);d=json.loads((out/'active_manifest.json').read_text());self.assertEqual(result['contexts'],1);self.assertEqual(result['ordinary_supplemental_rows'],1);self.assertEqual(len(d['contexts'][0]['native_rows']),1);self.assertTrue(d['contexts'][0]['native_rows'][0]['preprint_derived']);self.assertEqual(d['contexts'][0]['native_rows'][0]['cohort_names'],['NHS II']);ordinary=d['projection_baseline_cells'][0]['evidence'][0];self.assertFalse(ordinary['preprint_derived']);self.assertEqual(ordinary['pmid'],'11111111');self.assertEqual(before,{str(p):native.sha(p) for p in onco.rglob('*') if p.is_file()})
 def test_existing_metric_replaces_only_exact_projected_target_ids(self):
  with tempfile.TemporaryDirectory() as root:
   onco,manifest,mapping,receipt,_,_=self.fixture(root,True);out=Path(root)/'out';result=projection.project_manifests([manifest],mapping,[receipt],out,onco);context=json.loads((out/'active_manifest.json').read_text())['contexts'][0];self.assertEqual(result['ordinary_supplemental_rows'],0);self.assertEqual(context['operation'],'replace');self.assertEqual(context['original_record_ids'],['id:old-target']);self.assertEqual(context['native_rows'][0]['preprint_original_record_ids'],['id:old-target'])
 def test_native_selection_hash_cannot_change(self):
  with tempfile.TemporaryDirectory() as root:
   onco,manifest,mapping,receipt,_,_=self.fixture(root);d=json.loads(receipt.read_text());d['native_pooling_cells'][0]['selected_rows'][0]['Effect Size']=9;native.save(receipt,d);result=projection.project_manifests([manifest],mapping,[receipt],Path(root)/'out',onco);self.assertEqual(result['contexts'],0);self.assertIn('hash changed',result['contexts_audit'][0]['hold'])
 def test_different_published_source_generation_is_a_lineage_hold(self):
  with tempfile.TemporaryDirectory() as root:
   onco,manifest,mapping,receipt,_,_=self.fixture(root,True);d=json.loads(mapping.read_text());d['mapping_rows'][0]['native_and_snapshot_source_cache_identical']=False;native.save(mapping,d);result=projection.project_manifests([manifest],mapping,[receipt],Path(root)/'out',onco);self.assertEqual(result['contexts'],0);self.assertIn('lineage review',result['contexts_audit'][0]['hold'])
 def test_settled_nonpoolable_is_not_a_source_lineage_error(self):
  with tempfile.TemporaryDirectory() as root:
   onco,manifest,mapping,receipt,_,_=self.fixture(root,True);d=json.loads(manifest.read_text());d['contexts'][0]['native_rows'][0].update(policy_poolable=False,**{'Primary Estimate':False});native.save(manifest,d);blueprint=json.loads(mapping.read_text());blueprint['mapping_rows'][0]['native_and_snapshot_source_cache_identical']=False;native.save(mapping,blueprint);result=projection.project_manifests([manifest],mapping,[receipt],Path(root)/'out',onco);self.assertEqual(result['contexts_audit'][0]['disposition'],'no_scientific_projection');self.assertNotIn('hold',result['contexts_audit'][0])
 def test_missing_native_estimate_cannot_be_projected(self):
  with tempfile.TemporaryDirectory() as root:
   onco,manifest,mapping,receipt,_,_=self.fixture(root);d=json.loads(receipt.read_text());d['native_pooling_cells'][0]['headline']={};native.save(receipt,d);result=projection.project_manifests([manifest],mapping,[receipt],Path(root)/'out',onco);self.assertEqual(result['contexts'],0);self.assertEqual(result['contexts_audit'][0]['disposition'],'no_scientific_projection');self.assertNotIn('hold',result['contexts_audit'][0])
 def test_latest_filtered_receipt_cannot_remove_dynamic_choice(self):
  with tempfile.TemporaryDirectory() as root:
   onco,manifest,mapping,receipt,_,_=self.fixture(root);d=json.loads(receipt.read_text());d['keep_latest_cohort']=True;native.save(receipt,d);self.assertEqual(projection.load_receipts([receipt]),[])
 def test_narrow_append_exception_cannot_replace_published_evidence(self):
  with self.assertRaisesRegex(ValueError,'cannot replace'):projection.append_lineage_authorization({'original_evidence_for_pmid':[{'pmid':'32814947','effect':1.2}]},{},Path('/unused'),[],{})
 def test_narrow_append_exception_cannot_apply_to_another_source_anchor(self):
  context={'review_context_id':'99999999|Red meat|Prostate cancer','selector':{'source_repo':'MetaVir'}}
  with self.assertRaisesRegex(ValueError,'restricted'):projection.append_lineage_authorization({'original_evidence_for_pmid':[]},context,Path('/unused'),[],{})
 def test_explicit_native_folder_alias_prefers_actual_gate_over_synthetic_zero(self):
  with tempfile.TemporaryDirectory() as root:
   onco,manifest,mapping,receipt,_,_=self.fixture(root);d=json.loads(receipt.read_text());actual=d['native_pooling_cells'][0];actual['exposure']='legumes';actual['selection_policy']='native_final_pooling_gate';zero=copy.deepcopy(actual);zero.update(exposure='Legumes',selected_rows=[],selected_record_ids=[],selected_rows_sha256=native.digest([]),pooled_n=0,headline={},selection_policy='native_generator_produced_no_estimate_for_recovered_source_context');d['native_pooling_cells'].append(zero);native.save(receipt,d);result=projection.project_manifests([manifest],mapping,[receipt],Path(root)/'out',onco);self.assertEqual(result['contexts'],1);self.assertTrue(result['contexts_audit'][0]['activated'])

if __name__=='__main__':unittest.main()
