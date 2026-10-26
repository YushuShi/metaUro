"""Read native protocol literals without importing runners or credentials."""
from pathlib import Path
import ast, hashlib, json, re, os
from minimize_native_contexts import exact_target_rows
ROOT=Path(os.environ.get('PREPRINT_WORKSPACE_ROOT',Path(__file__).resolve().parents[2]))
BASE=Path(os.environ.get('PREPRINT_WORK_DIR',ROOT/'preprint_recovery_2026-10-26'))
STANDARD_FIELDS=['effect_size','effect_type','ci_lower','ci_upper','ci_level','p_value','p_value_relation','standard_error','total_n','cases','comparison_type','comparison_direction','comparison_group','reference_group','unit','scale','adjusted_model','adjusted_covariates','subgroup','stratum','design','timing','continent','country','population','gender','exposure_measurement_type','needs_inversion','p_value_provenance','ci_provenance','standard_error_provenance','supporting_text','additional_extractions']
NEURO_FIELDS=['design','timing','country_or_region','cohort_name','sample_size','cases','exposure_definition','comparison_type','comparison_direction','comparison_group','reference_group','unit','scale','adjusted_model','adjusted_covariates','adjusted','subgroup','stratum','outcome_definition','effect_type','effect_size','ci_lower','ci_upper','ci_level','standard_error','p_value','p_value_relation','p_value_provenance','ci_provenance','standard_error_provenance','needs_inversion','exposure_measurement_type','independent_population_id','group_statistics','additional_studies']

def digest_file(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def literal(p,name):
 tree=ast.parse(Path(p).read_text())
 for n in tree.body:
  if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets):
   try:return ast.literal_eval(n.value)
   except (ValueError,TypeError):return None
 return None

def protocol(repo,outcome=None,disease=None):
 root=ROOT/repo;bind=[];parts=[]
 def add(p,name):
  if not p.exists():return
  v=literal(p,name)
  if isinstance(v,str) and v.strip():parts.append(v.strip());bind.append({'path':str(p.relative_to(ROOT)),'sha256':digest_file(p),'constant':name})
 if repo=='metaNeuro':
  p=root/'scripts/screen_additional_articles.py';src=p.read_text();tree=ast.parse(src)
  q=root/'scripts/outcome_scope_guard.py';v=literal(q,'PROMPT') if q.exists() else '';ns={'outcome_scope_guard':type('Prompt',(),{'PROMPT':v or ''})(),'normalized':lambda x:re.sub(r'\s+',' ',str(x or '')).strip().lower()}
  for n in tree.body:
   if isinstance(n,ast.FunctionDef) and n.name in {'outcome_scope','exclusion_definitions','jbi_instructions'}:
    exec(compile(ast.Module(body=[n],type_ignores=[]),str(p),'exec'),ns)
  parts.extend([ns['outcome_scope'](disease or outcome,outcome or ''),ns['exclusion_definitions'](),ns['jbi_instructions']()])
  parts.append('Native Neuro supports OR/RR/HR and additive BETA/MEAN_DIFFERENCE. PSQI and sleep-efficiency outcomes are continuous outcomes, not incident disease. Extract study.sample_size (common total_n alias), standard_error and its provenance, group statistics if given, outcome_definition and exact score direction. Negative and zero additive effects/CIs are valid. Genetic-focus/gene-environment-only estimates without a separable population-wide exposure effect are excluded. Use target-specific adjusted results and preserve each independent population and stratum separately. Supporting_text requires exact source excerpts for effect and sample size. Never derive statistics in model output.')
  bind.append({'path':str(p.relative_to(ROOT)),'sha256':digest_file(p),'functions':['outcome_scope','exclusion_definitions','jbi_instructions','full_text_prompt','screening_prompt']})
  if q.exists():bind.append({'path':str(q.relative_to(ROOT)),'sha256':digest_file(q),'constant':'PROMPT'})
  add(root/'scripts/quantitative_policy.py','PROMPT');fields=NEURO_FIELDS
  schema={'container':'study','fields':fields,'effect_types':['OR','RR','HR','BETA','MEAN_DIFFERENCE'],'supporting_text':{'exposure':None,'outcome':None,'sample_size':None,'effect':None,'quality':None},'group_statistics':'Preserve native continuous-outcome group N/mean/SD/SE fields when explicitly reported; do not derive.','sample_size_alias':'common extraction.total_n maps to native study.sample_size'}
 else:
  p=root/'scripts/monthly_evidence_update.py'
  if literal(p,'SECOND_SCREEN_INSTRUCTIONS') is None:
   p=root/'scripts/dual_screening.py';add(p,'SECOND_INSTRUCTIONS');add(p,'FULL_TEXT_INSTRUCTIONS');add(p,'POLICY_INSTRUCTIONS')
  else:add(p,'SECOND_SCREEN_INSTRUCTIONS');add(p,'FULL_TEXT_INSTRUCTIONS')
  for rel,name in [('scripts/poolability_policy.py','PROMPT'),('scripts/relaxed_poolability.py','POLICY_INSTRUCTIONS'),('scripts/dietary_only_policy.py','PROMPT'),('scripts/incidence_endpoint.py','PROMPT_RULE'),('scripts/incidence_endpoint_policy.py','PROMPT_RULE'),('scripts/population_scope.py','PROMPT_RULE'),('scripts/measurement_estimates.py','PROMPT')]:add(root/rel,name)
  if repo=='metaGut' and disease and any(t in disease.lower() for t in ['masld','nafld','nonalcoholic','metabolic dysfunction','crohn','ulcerative','inflammatory bowel']):
   scope_path=root/'scripts/run_nonmalignant_extension.py';tree=ast.parse(scope_path.read_text());ns={'re':re}
   for n in tree.body:
    if isinstance(n,ast.FunctionDef) and n.name=='scoped_prompt':exec(compile(ast.Module(body=[n],type_ignores=[]),str(scope_path),'exec'),ns)
   config=root/'nonmalignant_disease_config.py';scopes=literal(config,'DISEASES') or {};note=(scopes.get(disease) or {}).get('scope_note','')
   note=disease+': '+note+'\nOnly incidence/risk/development of the requested disease is in scope. Prevalence, treatment, prognosis, recurrence and other excluded endpoints retain the existing rules. All exposure, human-study, genetic-focus, JBI, extraction, Wald and pooling requirements remain in force.'
   parts=[ns['scoped_prompt'](text,note) for text in parts]
   bind.extend([{'path':str(scope_path.relative_to(ROOT)),'sha256':digest_file(scope_path),'function':'scoped_prompt'},{'path':str(config.relative_to(ROOT)),'sha256':digest_file(config),'scope':disease}])
  fields=STANDARD_FIELDS;schema={'container':'extraction','fields':fields,'effect_types':['RR','IRR','OR','HR'],'sample_size_field':'total_n','additional_extractions':'Preserve independent populations, sex/measurement strata and target-specific estimates separately.','supporting_text':'Native concise source support plus exact evidence_quotes.'}
 parts.insert(0,'SOURCE-ONLY AUTHORIZED ADAPTATION: The complete, explicitly EuropePMC PMID-linked preprint replaces unavailable PMC text for this optional recovery lineage only. All native scientific exclusions/outcome criteria still apply. Mentions of PMC below mean supplied verified preprint for this run; do not claim journal/PMC full text was reviewed. Preserve original numeric contrast and flag needs_inversion; do not numerically invert or derive statistics in model output. Default published analyses remain unchanged.')
 return {'native_criteria':'\n\n'.join(parts),'native_criteria_sources':bind,'native_extraction_fields':fields,'native_extraction_schema':schema}

def source_rows(d):
 if isinstance(d,list):yield from (r for r in d if isinstance(r,dict));return
 for k in ['results','contexts','records']:
  z=d.get(k)
  if isinstance(z,list):yield from (r for r in z if isinstance(r,dict));return
  if isinstance(z,dict):
   for cid,r in z.items():
    if isinstance(r,dict):yield dict(r,context_id=r.get('context_id',cid))
   return

def export_matched():
 matches=json.loads((BASE/'europepmc_research/catalogue/linked_candidates.json').read_text());pmids={m['published_pmid'] for m in matches}
 inventory=json.loads((BASE/'audit_inventory/native_source_blocked_contexts_guarded.json').read_text());contexts=[r for r in inventory if r['pmid'] in pmids]
 bykey={(r['repository'],r['context_id']):r for r in contexts};raw={k:[] for k in bykey}
 paths=set()
 for r in contexts:
  paths.update(e['path'] for e in r['evidence'] if Path(e['path']).name in {'second_screen.json','chatgpt_second_screen.json'})
  p=f"{r['repository']}/data/claude_gemini_screening/all_history_through_2026-08-29/claude_r2_results.json"
  if (ROOT/p).exists():paths.add(p)
 for p in sorted(paths):
  d=json.loads((ROOT/p).read_text());sha=digest_file(ROOT/p);track='Claude+Gemini' if Path(p).name=='claude_r2_results.json' else 'ChatGPT'
  for x in source_rows(d):
   key=(p.split('/')[0],x.get('context_id'))
   if key not in bykey:continue
   raw[key].append({'track':track,'source_path':p,'source_sha256':sha,'packet_model':d.get('model'),'packet_input_digest':d.get('input_digest'),'saved_record':x})
  del d
 # Neuro current canonical IDs differ only in case from raw current Claude IDs.
 for key,r in bykey.items():
  if r['repository']!='metaNeuro':continue
  # Preserve canonical source references and parse only the selected saved cache file when needed.
  for e in r['evidence']:
   saved=e['saved_record'];p=saved.get('Source','').split('#')[0]
   if saved.get('Pipeline')=='ChatGPT' and (ROOT/r['repository']/p).is_file():
    source=ROOT/r['repository']/p;d=json.loads(source.read_text());selected=exact_target_rows(d,r,saved.get('Source',''))
    if not selected:raise ValueError('No exact target row for native source export: '+r['context_id'])
    raw[key].append({'track':'ChatGPT','source_path':str(source.relative_to(ROOT)),'source_sha256':digest_file(source),'source_row_reference':saved.get('Source'),'saved_target_records':selected,'source_scope_selection':{'method':'Exact PMID plus native exposure/outcome and original screening_context_id when supplied','native_context_id':r['context_id'],'target_record_count':len(selected),'unrelated_rows_removed_from_prompt_only':True,'original_saved_source_unchanged':True}})
 for r in contexts:
  key=(r['repository'],r['context_id']);r['r2_records']=raw[key]
  cite=next((x['saved_record'] for x in raw[key] if x.get('saved_record')),None)
  if cite:
   r['title']=cite.get('title') or r.get('title');r['disease']=cite.get('disease') or cite.get('cancer') or r.get('disease');r['outcome']=cite.get('outcome') or cite.get('endpoint') or r.get('outcome')
   r['published_metadata']={k:cite.get(k) for k in ['pmid','pmcid','published_date','title','authors','journal','pubmed_url']}
  if not r.get('disease'):
   bits=r['context_id'].split('|');r['disease']=bits[2] if len(bits)>2 else None
  if not r.get('outcome'):
   bits=r['context_id'].split('|');r['outcome']=bits[3] if len(bits)>3 else 'Incidence'
  r.update(protocol(r['repository'],r['outcome'],r['disease']))
  observed=set()
  for record in r['r2_records']:
   row=record.get('saved_record',{});native=row.get('study') or row.get('extraction') or {}
   if isinstance(native,dict):observed.update(native)
  r['native_extraction_fields']=list(dict.fromkeys(r['native_extraction_fields']+sorted(observed)))
  r['native_extraction_schema']['fields']=r['native_extraction_fields']
  if r['repository']=='MetaCutis_et_ Capillus' and r['outcome']!='Incidence':r['native_extraction_schema']['effect_types']='Endpoint-appropriate reported effect labels, including continuous score changes; keep reported type and native conversion separate.'
  r['native_dispatch_eligible']=True;r['dispatch_authorized']=False;r['human_protected']=False
 output={'schema_version':1,'native_scope_guard_sha256':digest_file(BASE/'audit_inventory/native_source_blocked_contexts_guarded.json'),'matched_pmid_count':len(pmids),'native_matched_context_count':len(contexts),'contexts':contexts,'note':'Native dispatch eligibility only; root must separately verify preprint fulltext identity/source hashes then set dispatch_authorized. No model calls.'}
 dest=BASE/'audit_inventory/matched_preprint_native_contexts.json';dest.write_text(json.dumps(output,indent=2,ensure_ascii=False)+'\n')
 first=[r for r in contexts if r['pmid']=='38321187'];(BASE/'audit_inventory/first_38321187_native_context.json').write_text(json.dumps(first,indent=2,ensure_ascii=False)+'\n')
 print(json.dumps({'contexts':len(contexts),'first_38321187_contexts':len(first),'source_rows':sum(len(r['r2_records']) for r in contexts),'path':str(dest)},indent=2))
if __name__=='__main__':export_matched()
