import copy,json,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
import native_overlay_converter as native
import uro_overlay_projection as projection

class UroProjectionTests(unittest.TestCase):
 def fixture(self,root):
  root=Path(root);uro=root/'metaUro';source=uro/'sources/MetaVir/Cached_results/saved.json';native.save(source,{'studies':[{'Effect ID':'original1','PMID':'99999999'}]});native.save(uro/'updates/manifest.json',{'schema_version':1,'sources':{}})
  directory=root/'native_manifest';gate={'pmid':'99999999','availability':'pmcid_missing','pmcids':[],'preprint_fetch_allowed':True,'model_dispatch_authorized':True,'saved_any_track_checked':True,'saved_any_track_full_text_available':False,'any_track_gate_record_sha256':'a'*64}
  proof=directory/'article_availability_gate.json';native.save(proof,{'schema_version':1,'records':[gate]})
  context={'context_id':'native-cell','review_context_id':'99999999|Exposure|Disease','selector':{'source_repo':'MetaVir','disease':'Disease','exposure':'Exposure','outcome':'Incidence','include_subexposures':'yes','source_variant':'combined'},'operation':'replace','review':{'status':'adjudicated','decision':'include'},'publication_anchor':{'pmid':'99999999','journal_publication_date':'2024','journal_date_precision':'year'},'article_availability_gate':{'path':'data/preprint_recovery/article_availability_gate.json','sha256':native.sha(proof),'record_sha256':native.digest(gate),'pmid':'99999999'},'source_cache_paths':['Cached_results/saved.json'],'baseline_files':{'Cached_results/saved.json':native.sha(source)},'original_record_ids':['id:original1'],'native_rows':[{'Effect ID':'preprint:MetaVir:new1','PMID':'99999999','preprint_derived':True,'policy_poolable':False,'preprint_original_record_ids':['id:original1']}],'preprint_source':{'id':'PPR1','version':'1','source_sha256':'b'*64}}
  manifest=directory/'active_manifest.json';native.save(manifest,{'schema_version':1,'status':'complete','qa_only':True,'contexts':[context]});return uro,source,manifest,context
 def test_exact_durable_source_projection_preserves_all_source_bytes(self):
  with tempfile.TemporaryDirectory() as root:
   uro,source,manifest,original=self.fixture(root);before={str(p):native.sha(p) for p in uro.rglob('*') if p.is_file()};output=Path(root)/'projected';receipt=projection.project_manifests([manifest],output,uro)
   context=json.loads((output/'active_manifest.json').read_text())['contexts'][0]
   self.assertEqual(receipt['contexts'],1);self.assertEqual(context['source_cache_paths'],['sources/MetaVir/Cached_results/saved.json']);self.assertIn('updates/manifest.json',context['baseline_files']);self.assertEqual(context['original_record_ids'],original['original_record_ids']);self.assertFalse(context['native_rows'][0]['policy_poolable']);self.assertEqual(context['article_availability_gate']['path'],'updates/preprint_recovery/article_availability_gate.json');self.assertEqual(before,{str(p):native.sha(p) for p in uro.rglob('*') if p.is_file()})
 def test_different_snapshot_is_audit_only(self):
  with tempfile.TemporaryDirectory() as root:
   uro,source,manifest,_=self.fixture(root);source.write_text('{"studies":[]}');receipt=projection.project_manifests([manifest],Path(root)/'out',uro);self.assertEqual(receipt['contexts'],0);self.assertIn('differs',receipt['contexts_audit'][0]['hold'])
 def test_original_id_mismatch_is_audit_only(self):
  with tempfile.TemporaryDirectory() as root:
   uro,source,manifest,context=self.fixture(root);context['original_record_ids']=['id:absent'];native.save(manifest,{'schema_version':1,'status':'complete','contexts':[context]});receipt=projection.project_manifests([manifest],Path(root)/'out',uro);self.assertEqual(receipt['contexts'],0);self.assertIn('record IDs',receipt['contexts_audit'][0]['hold'])
 def test_full_PMC_available_cannot_project_even_with_review(self):
  with tempfile.TemporaryDirectory() as root:
   uro,source,manifest,context=self.fixture(root);proof=manifest.with_name('article_availability_gate.json');gate=json.loads(proof.read_text())['records'][0];gate['saved_any_track_full_text_available']=True;native.save(proof,{'schema_version':1,'records':[gate]});context['article_availability_gate'].update(sha256=native.sha(proof),record_sha256=native.digest(gate));native.save(manifest,{'schema_version':1,'status':'complete','contexts':[context]});receipt=projection.project_manifests([manifest],Path(root)/'out',uro);self.assertEqual(receipt['contexts'],0);self.assertIn('not eligible',receipt['contexts_audit'][0]['hold'])
 def test_explicit_whole_target_row_lineage_cannot_cover_changed_target(self):
  with tempfile.TemporaryDirectory() as root:
   root=Path(root);uro,source,manifest,context=self.fixture(root);row={'Effect ID':'original1','PMID':'99999999','context_id':context['review_context_id'],'Sample Size':500};native_source=root/'workspace/MetaVir/Cached_results/saved.json';native.save(native_source,{'studies':[row]});native.save(source,{'studies':[copy.deepcopy(row),{'Effect ID':'unrelated','PMID':'11111111'}]});context['baseline_files']['Cached_results/saved.json']=native.sha(native_source);native.save(manifest,{'schema_version':1,'status':'complete','qa_only':True,'contexts':[context]});resolver,path=projection.resolver_module();identity=native.identity_function('metaUro')
   with patch.object(native,'ROOT',root/'workspace'),patch.object(native,'identity_function',lambda repo:identity),patch.object(projection,'resolver_module',lambda:(resolver,path)):
    proof=root/'lineage.json';audit=projection.create_lineage_proofs([manifest],proof,uro);self.assertEqual(len(audit['records']),1);self.assertEqual(audit['holds'],[])
    strict=projection.project_manifests([manifest],root/'strict',uro);self.assertEqual(strict['contexts'],0)
    approved=projection.project_manifests([manifest],root/'approved',uro,proof);self.assertEqual(approved['contexts'],1);self.assertEqual(approved['source_artifacts_changed'],[])
    changed=json.loads(source.read_text());changed['studies'][0]['Sample Size']=501;native.save(source,changed);rejected=projection.project_manifests([manifest],root/'rejected',uro,proof);self.assertEqual(rejected['contexts'],0);self.assertIn('target row differs',rejected['contexts_audit'][0]['hold'])
 def test_selector_alias_requires_exact_cache_and_literal_case_only(self):
  with tempfile.TemporaryDirectory() as root:
   uro,source,manifest,context=self.fixture(root);out=Path(root)/'out';runtime=Path(root)/'requests.jsonl';selector={**context['selector'],'exposure':'exposure','_cache_path':'sources/MetaVir/Cached_results/saved.json'};runtime.write_text(json.dumps({'selector':selector})+'\n');projection.project_manifests([manifest],out,uro,selector_resolution_proof=runtime);view=json.loads((out/'active_manifest.json').read_text());self.assertEqual(view['selector_aliases'],{'exposure':{'exposure':'Exposure'}});self.assertIn('updates/preprint_recovery/selector_resolution_proof.json',view['contexts'][0]['baseline_files']);selector['exposure']='Different chemical';runtime.write_text(json.dumps({'selector':selector})+'\n')
   with self.assertRaises(ValueError):projection.project_manifests([manifest],Path(root)/'invalid',uro,selector_resolution_proof=runtime)

if __name__=='__main__':unittest.main()
