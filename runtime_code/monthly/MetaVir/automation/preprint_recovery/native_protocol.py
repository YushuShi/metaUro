"""Read native protocol literals without importing runners or credentials."""
from pathlib import Path
import ast, hashlib, json, re, os
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

def protocol(repo,outcome=None,disease=None,*,repo_root=None):
 if repo_root is None:raise ValueError('An explicit local native repository root is required')
 root=Path(repo_root).resolve();bind=[];parts=[]
 def add(p,name):
  if not p.exists():return
  v=literal(p,name)
  if isinstance(v,str) and v.strip():parts.append(v.strip());bind.append({'path':str(p.relative_to(root.parent)),'sha256':digest_file(p),'constant':name})
 if repo=='metaNeuro':
  p=root/'scripts/screen_additional_articles.py';src=p.read_text();tree=ast.parse(src)
  q=root/'scripts/outcome_scope_guard.py';v=literal(q,'PROMPT') if q.exists() else '';ns={'outcome_scope_guard':type('Prompt',(),{'PROMPT':v or ''})(),'normalized':lambda x:re.sub(r'\s+',' ',str(x or '')).strip().lower()}
  for n in tree.body:
   if isinstance(n,ast.FunctionDef) and n.name in {'outcome_scope','exclusion_definitions','jbi_instructions'}:
    exec(compile(ast.Module(body=[n],type_ignores=[]),str(p),'exec'),ns)
  parts.extend([ns['outcome_scope'](disease or outcome,outcome or ''),ns['exclusion_definitions'](),ns['jbi_instructions']()])
  parts.append('Native Neuro supports OR/RR/HR and additive BETA/MEAN_DIFFERENCE. PSQI and sleep-efficiency outcomes are continuous outcomes, not incident disease. Extract study.sample_size (common total_n alias), standard_error and its provenance, group statistics if given, outcome_definition and exact score direction. Negative and zero additive effects/CIs are valid. Genetic-focus/gene-environment-only estimates without a separable population-wide exposure effect are excluded. Use target-specific adjusted results and preserve each independent population and stratum separately. Supporting_text requires exact source excerpts for effect and sample size. Never derive statistics in model output.')
  bind.append({'path':str(p.relative_to(root.parent)),'sha256':digest_file(p),'functions':['outcome_scope','exclusion_definitions','jbi_instructions','full_text_prompt','screening_prompt']})
  if q.exists():bind.append({'path':str(q.relative_to(root.parent)),'sha256':digest_file(q),'constant':'PROMPT'})
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
   bind.extend([{'path':str(scope_path.relative_to(root.parent)),'sha256':digest_file(scope_path),'function':'scoped_prompt'},{'path':str(config.relative_to(root.parent)),'sha256':digest_file(config),'scope':disease}])
  fields=STANDARD_FIELDS;schema={'container':'extraction','fields':fields,'effect_types':['RR','IRR','OR','HR'],'sample_size_field':'total_n','additional_extractions':'Preserve independent populations, sex/measurement strata and target-specific estimates separately.','supporting_text':'Native concise source support plus exact evidence_quotes.'}
 parts.insert(0,'SOURCE-ONLY AUTHORIZED ADAPTATION: The complete, user-authorized metadata-discovered candidate preprint replaces unavailable PMC text for this optional recovery lineage only. All native scientific exclusions/outcome criteria still apply. Mentions of PMC below mean supplied verified preprint for this run; do not claim journal/PMC full text was reviewed. Preserve original numeric contrast and flag needs_inversion; do not numerically invert or derive statistics in model output. Default published analyses remain unchanged.')
 if repo=='MetaCutis_et_ Capillus' and outcome and outcome!='Incidence':schema['effect_types']=['RR','IRR','OR','HR','BETA','MEAN_DIFFERENCE','SMD'];fields=list(dict.fromkeys(fields+['group_statistics']))
 return {'native_criteria':'\n\n'.join(parts),'native_criteria_sources':bind,'native_extraction_fields':fields,'native_extraction_schema':schema}
