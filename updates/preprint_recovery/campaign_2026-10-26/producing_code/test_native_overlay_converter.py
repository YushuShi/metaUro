import ast,copy,importlib.util,json,tempfile,unittest,types,math
from pathlib import Path
P=Path(__file__).with_name('native_overlay_converter.py');S=importlib.util.spec_from_file_location('native_overlay_converter',P);m=importlib.util.module_from_spec(S);S.loader.exec_module(m)
class NativeOverlayConverterTests(unittest.TestCase):
 def context(self,repo='metaGut',outcome='Incidence'):
  return {'repository':repo,'context_id':'99999999|Exposure|Disease','pmid':'99999999','pmcid':'','exposure':'Exposure','disease':'Disease','outcome':outcome,'published_metadata':{'published_date':'2024-05-01','authors':['Author A'],'journal':'Journal'}}
 def review(self,**kwargs):
  e={'effect_type':'OR','effect_size':.5,'ci_lower':.25,'ci_upper':.75,'ci_level':95,'p_value':None,'total_n':500,'cases':100,'comparison_direction':'lower_vs_higher','comparison_group':'Lower','reference_group':'Higher','needs_inversion':True,'design':'case_control','exposure_measurement_type':'dietary_intake'};e.update(kwargs)
  return {'status':'INCLUDE_META','extraction':e,'jbi':{'checklist_type':'case_control','answers':{'q'+str(i):'Yes' if i<=8 else 'Unclear' for i in range(1,11)}},'cohort_names':['NHS II'],'evidence_quotes':[{'quote':'supported source text'}]}
 def source(self,version='1'):return {'preprint_id':'PPR1234','preprint_version':version,'text_sha256':'a'*64,'raw_source_sha256':'b'*64,'retrieval_url':'https://example.test/v'+version,'preprint_posted_date':'2023-01-01'}
 def anchor(self):return {'pmid':'99999999','journal_publication_date':'2024-05','journal_date_precision':'month'}
 def med(self):return {'title':'Journal title','authorString':'Author A, Other B.','journalInfo':{'dateOfPublication':'2024 May','yearOfPublication':2024,'monthOfPublication':5,'journal':{'title':'Journal'}}}
 def row(self,r=None,ctx=None,source=None):
  r=r or self.review();return m.native_row(ctx or self.context(),r,source or self.source(),self.anchor(),self.med(),m.combine_bundle(r,(ctx or self.context())['repository']))[0]
 def test_explicit_reverse_ratio_once_preserves_reported_bundle(self):
  r=self.review();before=copy.deepcopy(r);row=self.row(r)
  self.assertEqual(row['Effect Size'],2);self.assertAlmostEqual(row['Lower CI'],1/.75);self.assertEqual(row['Upper CI'],4)
  self.assertEqual(row['Original Effect Size'],.5);self.assertEqual(row['Original Lower CI'],.25);self.assertTrue(row['inversion_applied']);self.assertFalse(row['needs_inversion']);self.assertEqual(r,before)
 def test_negative_and_zero_additive_ci_are_valid_neuro(self):
  ctx=self.context('metaNeuro','psqi');r=self.review(effect_type='BETA',effect_size=-1,ci_lower=-2,ci_upper=0,total_n=200,comparison_direction='per_unit_increase',needs_inversion=False)
  row=self.row(r,ctx);self.assertEqual((row['Effect Size'],row['Lower CI'],row['Upper CI']),(-1,-2,0));self.assertTrue(row['policy_poolable']);self.assertEqual(row['Outcome Key'],'psqi')
 def test_explicit_additive_reverse_negates_reorders_ci(self):
  ctx=self.context('metaNeuro','sleep_efficiency');r=self.review(effect_type='MEAN_DIFFERENCE',effect_size=2,ci_lower=1,ci_upper=3,total_n=200)
  row=self.row(r,ctx);self.assertEqual((row['Effect Size'],row['Lower CI'],row['Upper CI']),(-2,-3,-1));self.assertTrue(row['policy_poolable'])
 def test_fair_never_enters_default_pool(self):
  r=self.review();r['jbi']['answers']={k:('Yes' if i<4 else 'Unclear') for i,k in enumerate(r['jbi']['answers'])};row=self.row(r);self.assertEqual(row['Quality Score'],'Fair');self.assertFalse(row['policy_poolable']);self.assertEqual(row['meta_analysis_status'],'ELIGIBLE_NOT_POOLABLE')
 def test_settled_nonpoolable_stays_visible_nonpoolable(self):
  r=self.review();r['status']='ELIGIBLE_NOT_POOLABLE';row=self.row(r);self.assertEqual(row['final_status'],'ELIGIBLE_NOT_POOLABLE');self.assertEqual(row['PMID'],'99999999');self.assertTrue(row['preprint_derived'])
 def test_model_internal_inversion_claim_cannot_bypass_code(self):
  r=self.review(inversion_applied=True,reported_effect_size=99,estimate_bundle_digest='untrusted')
  row=self.row(r);self.assertEqual(row['Effect Size'],2);self.assertTrue(row['inversion_applied']);self.assertEqual(row['preprint_native_raw_extraction']['reported_effect_size'],99);self.assertEqual(row['preprint_native_effective_extraction']['reported_effect_size'],.5)
 def test_missing_N_stays_missing_not_promoted(self):
  row=self.row(self.review(total_n=None));self.assertIsNone(row['Sample Size']);self.assertFalse(row['scientific_pooling_eligible'])
 def test_bounded_trend_P_cannot_create_missing_CI(self):
  r=self.review(effect_size=1.4,ci_lower=None,ci_upper=None,p_value=.01,p_value_relation='<',p_value_provenance={'exact':False,'same_estimate':False,'kind':'bounded trend'},needs_inversion=False,comparison_direction='higher_vs_lower');row=self.row(r);self.assertIsNone(row['Lower CI']);self.assertIsNone(row['Upper CI']);self.assertFalse(row['policy_poolable'])
 def test_common_native_numeric_disagreement_holds(self):
  r=self.review();r['native_extraction']={'total_n':499}
  with self.assertRaises(ValueError):m.combine_bundle(r,'metaGut')
 def test_official_conversion_cannot_scan_history_without_index(self):
  with self.assertRaises(ValueError):m.active_result_files('/unused',None,False)
 def test_active_index_requires_exact_hashes_and_context_authority(self):
  with tempfile.TemporaryDirectory() as root:
   root=Path(root);p=root/'job/result.json';job=p.with_name('inputs.json');m.save(job,{'repository':'metaGut','contexts':[{'repository':'metaGut','context_id':'paper|Exposure|Disease'}]});m.save(p,{'job_id':'active1'});item={'job_id':'active1','repository':'metaGut','result_path':str(p),'job_file':str(job),'result_sha256':m.sha(p),'inputs_sha256':m.sha(job),'context_ids':['paper|Exposure|Disease']};index=root/'index.json';doc={'schema_version':1,'jobs':[item],'authoritative_context_jobs':{'metaGut|paper|Exposure|Disease':'active1'}};m.save(index,doc)
   files,receipt=m.active_result_files(root,index,False);self.assertEqual(files,[(p,{'paper|Exposure|Disease'})]);self.assertEqual(receipt['contexts'],1)
   doc['authoritative_context_jobs']['metaGut|paper|Exposure|Disease']='historical';m.save(index,doc)
   with self.assertRaises(ValueError):m.active_result_files(root,index,False)
   doc['authoritative_context_jobs']['metaGut|paper|Exposure|Disease']='active1';m.save(index,doc);p.write_text('{"job_id":"changed"}')
   with self.assertRaises(ValueError):m.active_result_files(root,index,False)
 def test_same_literal_bounded_P_native_common_is_preserved(self):
  r=self.review(p_value=.001,p_value_original='<0.001',p_value_relation='trend',p_value_provenance={'exact':False,'same_estimate':False,'kind':'bounded trend'})
  r['native_extraction']={'p_value':'<0.001'};before=copy.deepcopy(r);e=m.combine_bundle(r,'metaGut')
  self.assertEqual(e['p_value'],.001);self.assertEqual(e['p_value_original'],'<0.001');self.assertEqual(r,before)
  row=self.row(r);self.assertEqual(row['p_value_original'],'<0.001');self.assertFalse(row['p_value_provenance']['exact'])
 def test_bound_disagreement_cannot_be_silently_normalized(self):
  r=self.review(p_value=.001,p_value_original='<0.001');r['native_extraction']={'p_value':'>0.001'}
  with self.assertRaises(ValueError):m.combine_bundle(r,'metaGut')
 def test_journal_issue_precision_and_preprint_date_separate(self):
  a=m.publication_anchor(self.context(),{**self.med(),'firstPublicationDate':'2024-02-06','electronicPublicationDate':'2024-02-06'});self.assertEqual((a['journal_publication_date'],a['journal_date_precision']),('2024-05','month'));self.assertEqual(a['original_saved_journal_date'],'2024-05-01');self.assertEqual(a['MED_first_publication_date'],'2024-02-06')
 def test_verified_registry_precision_truncates_padded_day(self):
  prior=m.ROOT
  with tempfile.TemporaryDirectory() as root:
   m.ROOT=Path(root)
   try:
    m.save(m.ROOT/'metaGut/data/cohort_registry.json',{'records':{'99999999':{'publication_date':'2024-05-01','publication_date_precision':'month'}}})
    a=m.publication_anchor(self.context(),self.med());self.assertEqual((a['journal_publication_date'],a['journal_date_precision']),('2024-05','month'));self.assertEqual(a['date_provenance']['raw_value'],'2024-05-01')
   finally:m.ROOT=prior
 def test_unverified_padded_native_day_is_conservative_year(self):
  a=m.publication_anchor(self.context(),{});self.assertEqual((a['journal_publication_date'],a['journal_date_precision']),('2024','year'))
 def test_versions_distinct_and_numbered_cohorts_preserved(self):
  a=self.row(source=self.source('1'));b=self.row(source=self.source('2'));self.assertNotEqual(a['Effect ID'],b['Effect ID']);self.assertIn(':v1:',a['Effect ID']);self.assertEqual(a['cohort_names'],['NHS II']);self.assertNotIn('NHS III',a['cohort_names'])
 def test_exact_pilot_cache_mapping_and_hash_preservation(self):
  f=m.WORK/'audit_inventory/first_38321187_native_context.json';ctx=json.loads(f.read_text())[0];cells=m.cache_cells(ctx);self.assertEqual({x['selector']['include_subexposures'] for x in cells},{'yes','no'});self.assertEqual(len(cells),2)
  paths=[p for c in cells for p in c['paths']];before={str(p):m.sha(p) for p in paths};ids=[m.matching_originals(ctx,c) for c in cells];self.assertTrue(all(d for d in ids));self.assertTrue(all(k.startswith('id:combined:') for d in ids for k in d));self.assertEqual(before,{str(p):m.sha(p) for p in paths})
 def test_heart_resolves_native_active_source_and_canonical_labels(self):
  doc=json.loads((m.WORK/'converter_inputs/heart_cache_resolution.json').read_text());wanted=next(r for r in doc['contexts'] if r['context_id']=='36100703|Vitamin B6|Stroke');ctx={'repository':'metaHeart','context_id':wanted['context_id'],'pmid':'36100703','exposure':'Vitamin B6','disease':'Stroke','outcome':'Incidence'};cells=m.cache_cells(ctx)
  self.assertEqual(m.heart_canonical_exposure('Coffee'),'coffee');self.assertEqual({str(p.relative_to(m.ROOT/'metaHeart')) for c in cells if not c.get('disease_inherited') for p in c['paths']},{c['path'] for c in wanted['cells']});self.assertTrue(all(m.matching_originals(ctx,c) for c in cells));self.assertEqual({c['selector']['exposure'] for c in cells},{'Vitamin B6'});self.assertEqual({c['selector']['disease'] for c in cells if c.get('disease_inherited')},{'Cardiovascular disease'})
 def test_heart_parent_projection_only_existing_exact_child_and_never_poolable(self):
  directory=m.WORK/'screening_run/jobs/54132024bc6851224dd10c60160a3f709820799808561c58d00a4379c0701daf';job=json.loads((directory/'inputs.json').read_text());result=json.loads((directory/'result.json').read_text());ctx=job['contexts'][0];review=result['reviews']['gemini']['results'][0];cells=m.make_manifest_contexts(ctx,review,job['source'],self.med(),directory/'result.json')
  primary=[c for c in cells if c['selector']['disease']=='Stroke'];parents=[c for c in cells if c['selector']['disease']=='Cardiovascular disease'];self.assertEqual(len(primary),2);self.assertEqual(len(parents),2)
  for cell in cells:
   self.assertTrue(cell['audit_input_bindings']);self.assertFalse(set(cell['audit_input_bindings'])&set(cell['baseline_files']));self.assertTrue(all(not p.endswith('_second_screen.json') for p in cell['baseline_files']))
  for cell in primary:self.assertTrue(cell['native_rows'][0]['Primary Estimate']);self.assertTrue(cell['native_rows'][0]['policy_poolable'])
  for cell in cells:
   for row in cell['native_rows'][1:]:
    self.assertEqual(row['cohort_names'],['UK Biobank']);self.assertIn('inherited',row['cohort_metadata']['provenance']);self.assertFalse(row['preprint_native_raw_extraction'].get('cohort_names'));self.assertIn('preprint_secondary_extraction',row)
  for cell in parents:
   self.assertEqual(cell['operation'],'replace');self.assertTrue(cell['original_record_ids'])
   for row in cell['native_rows']:
    self.assertEqual(row['Direct Disease'],'Stroke');self.assertEqual(row['Displayed Disease'],'Cardiovascular disease');self.assertTrue(row['Disease Hierarchy Inherited'])
    for field in ['Primary Estimate','policy_poolable','scientific_pooling_eligible','pool_in_disease_parent_meta_analysis']:self.assertIs(row[field],False)
  import pandas as pd,numpy as np
  source=m.ROOT/'metaHeart/meta_analysis.py';tree=ast.parse(source.read_text());ns={'pd':pd,'np':np}
  for node in tree.body:
   if isinstance(node,ast.FunctionDef) and node.name in {'is_parent_pooling_eligible','poolable_meta_analysis_rows'}:exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
  with m.native_scripts('metaHeart'):
   self.assertEqual(len(ns['poolable_meta_analysis_rows'](pd.DataFrame(primary[0]['native_rows']))),1);self.assertEqual(len(ns['poolable_meta_analysis_rows'](pd.DataFrame(parents[0]['native_rows']))),0)
  dot_path=m.ROOT/'metaHeart/cvd_summary.py';tree=ast.parse(dot_path.read_text());dot={'math':math,'meta_analysis':types.SimpleNamespace(is_parent_pooling_eligible=ns['is_parent_pooling_eligible']),'normalize_effect_type':lambda v:m.canonical_type(v) if m.canonical_type(v) in m.RATIOS else None}
  for node in tree.body:
   if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in {'_POOL_FIELDS','_POOLING_GATES'} for t in node.targets):exec(compile(ast.Module(body=[node],type_ignores=[]),str(dot_path),'exec'),dot)
   elif isinstance(node,ast.FunctionDef) and node.name in {'number','_number_from_row','_selected_records'}:exec(compile(ast.Module(body=[node],type_ignores=[]),str(dot_path),'exec'),dot)
  self.assertEqual(len(dot['_selected_records'](primary[0]['native_rows'])),1);self.assertEqual(len(dot['_selected_records'](parents[0]['native_rows'])),0)
 def portable(self):
  import sys
  prior=sys.dont_write_bytecode;sys.dont_write_bytecode=True
  sys.path.insert(0,str(m.ROOT/'metaGut'))
  try:
   spec=importlib.util.spec_from_file_location('test_portable_preprint_ui',m.ROOT/'metaGut/preprint_ui.py');ui=importlib.util.module_from_spec(spec);spec.loader.exec_module(ui)
  finally:sys.path.pop(0);sys.dont_write_bytecode=prior
  # Resolve frozen identity helper without importing any app/analysis runner.
  ui.record_identity=m.identity_function('metaGut');return ui
 def manifest(self,root,operation='replace'):
  root=Path(root);p=root/'saved.json';p.write_text('{"studies":[]}')
  row=self.row();row['preprint_original_record_ids']=['id:old1','id:old2'] if operation=='replace' else []
  selector={'source_repo':'metaGut','disease':'Disease','exposure':'Exposure','outcome':'Incidence','include_subexposures':'no','source_variant':'combined'}
  c={'context_id':'recovered-cell','selector':selector,'operation':operation,'original_record_ids':row['preprint_original_record_ids'],'review':{'status':'adjudicated','decision':'include'},'baseline_files':{'saved.json':m.sha(p)},'publication_anchor':self.anchor(),'preprint_source':{'id':'PPR1234','source_sha256':'a'*64},'native_rows':[row]}
  gate={'pmid':'99999999','availability':'pmcid_missing','preprint_fetch_allowed':True,'model_dispatch_authorized':True,'saved_any_track_checked':True,'saved_any_track_full_text_available':False,'any_track_gate_record_sha256':'c'*64,'pmcids':[]}
  proof=root/'data/preprint_recovery/article_availability_gate.json';m.save(proof,{'schema_version':1,'records':[gate]})
  c['article_availability_gate']={'path':str(proof.relative_to(root)),'sha256':m.sha(proof),'pmid':'99999999','record_sha256':m.digest(gate)}
  m.save(root/'data/preprint_recovery/active_manifest.json',{'schema_version':1,'status':'complete','contexts':[c]});return selector,row
 def test_off_exact_identity_even_missing_manifest(self):
  ui=self.portable();rows=[{'Effect ID':'old1','Effect Size':999}]
  with tempfile.TemporaryDirectory() as root:
   actual,audit=ui.compose_preprint_view(rows,root,False);self.assertIs(actual,rows);self.assertFalse(audit['enabled'])
 def test_complete_context_replacement_no_double_count_and_idempotent(self):
  ui=self.portable()
  with tempfile.TemporaryDirectory() as root:
   selector,new=self.manifest(root);old=[{'Effect ID':'old1'},{'Effect ID':'old2'}];result,_=ui.compose_preprint_view(old,root,True,selector,allow_additions=True);self.assertEqual(len(result),1);self.assertEqual(result[0]['Effect Size'],2);self.assertEqual(old,[{'Effect ID':'old1'},{'Effect ID':'old2'}]);result2,_=ui.compose_preprint_view(result,root,True,selector,allow_additions=False);self.assertEqual([r['Effect ID'] for r in result2],[new['Effect ID']])
 def test_partial_original_selection_cannot_expand_other_strata(self):
  ui=self.portable()
  with tempfile.TemporaryDirectory() as root:
   selector,_=self.manifest(root);old=[{'Effect ID':'old1'}];result,_=ui.compose_preprint_view(old,root,True,selector,allow_additions=False);self.assertEqual(result,old)
 def test_stale_opt_in_binding_fails_closed_default_off_unchanged(self):
  ui=self.portable()
  with tempfile.TemporaryDirectory() as root:
   selector,_=self.manifest(root);Path(root,'saved.json').write_text('changed');old=[{'Effect ID':'old1'}]
   with self.assertRaises(ui.PreprintViewError):ui.compose_preprint_view(old,root,True,selector,allow_additions=True)
   result,_=ui.compose_preprint_view(old,root,False);self.assertIs(result,old)
if __name__=='__main__':unittest.main()
