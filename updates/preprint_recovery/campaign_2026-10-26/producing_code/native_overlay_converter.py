"""Convert settled linked-preprint reviews into additive native source views.

Only writes an explicitly supplied staging directory. Original caches, cohort
registries and historical screening/model receipts are always read-only.
"""
from __future__ import annotations
import argparse,ast,copy,hashlib,importlib,importlib.util,json,math,os,re,sys,types
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from statistics import NormalDist
ROOT=Path(os.environ.get('PREPRINT_WORKSPACE_ROOT',Path(__file__).resolve().parents[2]))
WORK=Path(os.environ.get('PREPRINT_WORK_DIR',ROOT/'preprint_recovery_2026-10-26'))
VERSION='native-preprint-overlay-2026-10-26-v1'
RATIOS={'OR','RR','HR','IRR'}
NEURO_CONFIG={'alzheimer_disease_incidence':('Alzheimer disease','alzheimer_disease','Incidence of Alzheimer disease'), 'cognitive_decline':('Cognitive decline','cognitive_decline','Cognitive decline'),'parkinson_disease_incidence':('Parkinson disease','parkinson_disease','Incidence of Parkinson disease'),'psqi':('Sleep disorder','sleep_disorder_psqi','PSQI'),'sleep_efficiency':('Sleep disorder','sleep_disorder_sleep_efficiency','sleep efficiency')}
def key(v):return re.sub(r'[^a-z0-9]+','_',str(v or '').lower()).strip('_')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()
def number(v):
 if isinstance(v,bool) or v is None:return None
 try:x=float(v)
 except (ValueError,TypeError):return None
 return x if math.isfinite(x) else None
def clean(v):
 if isinstance(v,dict):return {str(k):clean(x) for k,x in v.items()}
 if isinstance(v,list):return [clean(x) for x in v]
 if isinstance(v,float) and not math.isfinite(v):return None
 return v
def save(p,v):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(clean(v),indent=2,ensure_ascii=False,allow_nan=False)+'\n')
def canonical_type(v):
 s=re.sub(r'[^a-z]','',str(v or '').lower())
 return {'or':'OR','aor':'OR','adjustedoddsratio':'OR','rr':'RR','arr':'RR','irr':'IRR','hr':'HR','ahr':'HR','rrr':'RRR','beta':'BETA','md':'MEAN_DIFFERENCE','meandifference':'MEAN_DIFFERENCE'}.get(s,str(v or '').upper())
@contextmanager
def native_scripts(repo):
 """Load only path-free native numeric policy modules, without app imports."""
 prior={k:v for k,v in sys.modules.items() if k=='scripts' or k.startswith('scripts.')};dont=sys.dont_write_bytecode;sys.dont_write_bytecode=True
 for k in prior:sys.modules.pop(k,None)
 package=types.ModuleType('scripts');package.__path__=[str(ROOT/repo/'scripts')];sys.modules['scripts']=package
 try:yield
 finally:
  for k in list(sys.modules):
   if k=='scripts' or k.startswith('scripts.'):sys.modules.pop(k,None)
  sys.modules.update(prior);sys.dont_write_bytecode=dont

def native_quantitative(repo,e):
 e=copy.deepcopy(e);bindings={}
 # A reviewer cannot certify internal normalization/hash history. Numerical input is always source-reported.
 for field in list(e):
  if field in {'inversion_applied','estimate_identity_digest','estimate_bundle_digest','policy_original_values','policy_derivations','imputation_notes','ci_level_assumed','ci_level_imputed','effect_size_imputed','direction_imputed','comparison_direction_imputed','ci_level_converted','ci_scale_converted','combined_source_inversion_applied'}:e.pop(field,None)
 for field in ['effect_type','effect_size','ci_lower','ci_upper','p_value','standard_error']:e['reported_'+field]=copy.deepcopy(e.get(field))
 if e.get('comparison_direction')=='lower_vs_higher':e['needs_inversion']=True
 elif e.get('comparison_direction') in {'higher_vs_lower','per_unit_increase','category_vs_reference'}:e['needs_inversion']=False
 with native_scripts(repo):
  if repo=='metaNeuro':
   if 'sample_size' not in e:e['sample_size']=e.get('total_n')
   module=importlib.import_module('scripts.quantitative_policy');out=module.apply(e)
  elif (ROOT/repo/'scripts/poolability_policy.py').exists():
   module=importlib.import_module('scripts.poolability_policy');out=module.normalize(e)
  elif (ROOT/repo/'scripts/relaxed_poolability.py').exists():
   module=importlib.import_module('scripts.relaxed_poolability');out=module.complete(e)
  else:raise ValueError('No audited native numerical policy for '+repo)
  for name,m in list(sys.modules.items()):
   p=getattr(m,'__file__',None)
   if name.startswith('scripts.') and p and Path(p).is_relative_to(ROOT/repo):bindings[str(Path(p).relative_to(ROOT/repo))]=sha(p)
 return clean(out),bindings

def combine_bundle(review,repo):
 common=copy.deepcopy(review.get('extraction') or {});native=copy.deepcopy(review.get('native_extraction') or common.pop('native_extraction',{}) or {})
 if isinstance(native.get('study'),dict):native={**native,**native.pop('study')}
 if isinstance(native.get('extraction'),dict):native={**native,**native.pop('extraction')}
 if not isinstance(native,dict):raise ValueError('Native extraction must be an object')
 aliases={'total_n':'sample_size'} if repo=='metaNeuro' else {}
 for field,v in common.items():
  dest=aliases.get(field,field)
  if dest in native and native[dest] is not None and v is not None and field in {'effect_size','ci_lower','ci_upper','standard_error','p_value','total_n','cases'}:
   if number(native[dest])!=number(v):
    # The driver parses literal bounded P strings in the common bundle, preserving
    # the original text. Accept that same literal in native extraction; this does
    # not turn a bound/trend test into an exact coefficient P value.
    bounded=re.fullmatch(r'\s*([<>≤≥]=?)\s*(0?\.\d+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*',str(native[dest])) if field=='p_value' else None
    original=re.fullmatch(r'\s*([<>≤≥]=?)\s*(0?\.\d+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*',str(common.get('p_value_original',''))) if bounded else None
    if bounded and original and bounded.group(1)==original.group(1) and number(bounded.group(2))==number(original.group(2))==number(v):
     native.setdefault('p_value_original',copy.deepcopy(native[dest]));native[dest]=copy.deepcopy(v)
    else:raise ValueError('Common/native selected bundle disagrees: '+field)
  if field=='effect_type' and native.get(dest) and v and canonical_type(native[dest])!=canonical_type(v):raise ValueError('Common/native effect types disagree')
  if native.get(dest) is None:native[dest]=copy.deepcopy(v)
 if repo=='metaNeuro' and 'total_n' in native and native.get('sample_size') is None:native['sample_size']=native['total_n']
 return native

def jbi_quality(jbi):
 kind=jbi.get('checklist_type');total={'cohort':11,'case_control':10,'cross_sectional':8,'rct':13}.get(kind)
 answers=jbi.get('answers') or {}
 if not total or any(answers.get('q'+str(i)) not in {'Yes','No','Unclear','NA'} for i in range(1,total+1)):raise ValueError('Incomplete JBI checklist')
 score=100*sum(answers['q'+str(i)]=='Yes' for i in range(1,total+1))/total
 return ('Good' if score>80 else 'Moderate' if score>=51 else 'Fair'),score

def date_value(value):
 s=str(value or '').strip();months={m.lower():i for i,m in enumerate(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],1)}
 m=re.fullmatch(r'(\d{4})(?:[- /](\d{1,2}|[A-Za-z]+))?(?:[- /](\d{1,2}))?',s)
 if not m:return None
 year,month,day=m.groups()
 if not month:return year,'year'
 mm=int(month) if month.isdigit() else months.get(month[:3].lower())
 if not mm or not 1<=mm<=12:return year,'year'
 if not day:return f'{year}-{mm:02d}','month'
 try:datetime(int(year),mm,int(day))
 except ValueError:return None
 return f'{year}-{mm:02d}-{int(day):02d}','day'

def publication_anchor(ctx,med):
 repo=ctx['repository'];pmid=str(ctx['pmid']);path=ROOT/repo/'data/cohort_registry.json';registry=json.loads(path.read_text()).get('records',{}) if path.exists() else {};record=registry.get(pmid) or {}
 original=(ctx.get('published_metadata') or {}).get('published_date')
 chosen=None;proof=None
 if record.get('publication_date') and record.get('publication_date_precision'):
  precision=record['publication_date_precision'];raw_date=record['publication_date'];date_cut={'year':4,'month':7,'day':10}.get(precision)
  if date_cut:chosen=(str(raw_date)[:date_cut],precision);proof={'kind':'existing_verified_cohort_registry','path':str(path.relative_to(ROOT/repo)),'sha256':sha(path),'raw_value':raw_date}
 info=med.get('journalInfo') or {}
 if chosen is None:
  raw=info.get('dateOfPublication');parsed=date_value(raw)
  if parsed:chosen=parsed;proof={'kind':'europepmc_MED_journal_issue_date','raw_value':raw,'field':'journalInfo.dateOfPublication'}
 if chosen is None:
  y=info.get('yearOfPublication') or med.get('pubYear');m=info.get('monthOfPublication')
  if str(y).isdigit():chosen=(f'{y}-{int(m):02d}','month') if m and 1<=int(m)<=12 else (str(y),'year');proof={'kind':'europepmc_MED_explicit_issue_components','year':y,'month':m}
 if chosen is None:
  # Saved January/first-of-month dates may be padded. Year is a conservative fallback.
  parsed=date_value(original)
  if parsed:chosen=(parsed[0][:4],'year');proof={'kind':'native_saved_date_conservative_year','raw_value':original,'reason':'Original date precision not verified'}
 if chosen is None:raise ValueError('No journal publication date anchor')
 if med.get('_preprint_recovery_source'):proof['source_record']=copy.deepcopy(med['_preprint_recovery_source'])
 originals=[{'source_path':r.get('source_path'),'source_sha256':r.get('source_sha256'),'track':r.get('track'),'published_date':(r.get('saved_record') or {}).get('published_date')} for r in ctx.get('r2_records',[]) if (r.get('saved_record') or {}).get('published_date')]
 return {'original_saved_journal_dates':originals,'pmid':pmid,'journal_publication_date':chosen[0],'journal_date_precision':chosen[1],'original_saved_journal_date':original,'date_provenance':proof,'MED_electronic_publication_date':med.get('electronicPublicationDate'),'MED_first_publication_date':med.get('firstPublicationDate')}

def original_names(ctx):
 return []

def native_row(ctx,review,source,anchor,med,bundle,index=0):
 repo=ctx['repository'];raw=copy.deepcopy(bundle);effective,bindings=native_quantitative(repo,bundle);quality,score=jbi_quality(review['jbi']);typ=canonical_type(effective.get('effect_type'))
 if repo=='metaNeuro' and typ=='IRR':typ='RR'
 point,lo,hi=(number(effective.get(k)) for k in ['effect_size','ci_lower','ci_upper']);n=number(effective.get('sample_size') if repo=='metaNeuro' else effective.get('total_n'));cases=number(effective.get('cases'));se=number(effective.get('standard_error'))
 explicit_reverse=effective.get('needs_inversion') is True and effective.get('inversion_applied') is not True
 inverse=False
 if explicit_reverse and point is not None:
  if typ in RATIOS and point>0 and lo is not None and hi is not None and 0<lo<hi:point,lo,hi=1/point,1/hi,1/lo;inverse=True
  elif typ in {'BETA','MEAN_DIFFERENCE'}:point=-point;lo,hi=(-hi if hi is not None else None),(-lo if lo is not None else None);inverse=True
 if not se and lo is not None and hi is not None and lo<hi:
  se=(math.log(hi)-math.log(lo))/(2*NormalDist().inv_cdf(.975)) if typ in RATIOS and lo>0 else (hi-lo)/(2*NormalDist().inv_cdf(.975)) if typ in {'BETA','MEAN_DIFFERENCE'} else None
 complete=bool(n and n>0 and n.is_integer() and point is not None and ((lo is not None and hi is not None and lo<hi and lo<=point<=hi) or (repo=='metaNeuro' and se and se>0)))
 ratio=typ in RATIOS
 compatible=(typ in {'BETA','MEAN_DIFFERENCE'} if repo=='metaNeuro' and ctx['outcome'] in {'psqi','sleep_efficiency'} else ratio)
 if ratio and any(v is not None and v<=0 for v in [point,lo,hi]):complete=False
 poolable=bool(review['status']=='INCLUDE_META' and complete and compatible and quality in {'Good','Moderate'} and not effective.get('exposure_classification_ambiguous'))
 authors=med.get('authorString') or ', '.join((ctx.get('published_metadata') or {}).get('authors') or []);author=authors.split(',')[0].strip().rstrip('.') or 'Unknown';year=anchor['journal_publication_date'][:4]
 identifier='preprint:'+repo+':'+source['preprint_id']+':v'+str(source.get('preprint_version') or source.get('version') or source.get('target_version') or 'unknown')+':'+digest([ctx['context_id'],raw,index])[:24]
 names=raw.get('cohort_names') or ([raw['cohort_name']] if raw.get('cohort_name') else review.get('cohort_names') if index==0 else []) or []
 if isinstance(names,str):names=[names]
 inherited_cohort=False
 if repo=='metaHeart' and ctx['context_id']=='36100703|Vitamin B6|Stroke' and index>0 and not names and review.get('cohort_names')==['UK Biobank'] and not raw.get('independent_population_id'):
  names=['UK Biobank'];inherited_cohort=True
 reported_support=raw.get('supporting_text') or review.get('extraction',{}).get('supporting_text') or ''
 row={'Effect ID':identifier,'Record ID':identifier,'effect_record_id':identifier,'context_id':identifier,'preprint_native_context_id':ctx['context_id'],'PMID':str(ctx['pmid']),'PMCID':ctx.get('pmcid') or '', 'Study':f'{author} et al. ({year}) [PMID: {ctx["pmid"]}] — preprint-derived estimate {index+1}','Reference':med.get('title') or ctx.get('title'),'Title':med.get('title') or ctx.get('title'),'Authors':authors,'Journal':(med.get('journalInfo',{}).get('journal') or {}).get('title') or (ctx.get('published_metadata') or {}).get('journal'),'Year':year,'Link':'https://pubmed.ncbi.nlm.nih.gov/'+str(ctx['pmid'])+'/', 'Direct Exposure':ctx['exposure'],'direct_exposure':ctx['exposure'],'Displayed Exposure':ctx['exposure'],'Direct Disease':ctx['disease'],'Displayed Disease':ctx['disease'],'Disease Hierarchy Inherited':False,'pool_in_disease_parent_meta_analysis':True,'Effect Type':typ,'Effect Size':point,'Lower CI':lo,'Upper CI':hi,'Original Effect Type':raw.get('effect_type'),'Original Effect Size':raw.get('effect_size'),'Original Lower CI':raw.get('ci_lower'),'Original Upper CI':raw.get('ci_upper'),'P Value':number(effective.get('p_value')),'Sample Size':int(n) if n and n.is_integer() else n,'Cases':cases,'SE':se,'Quality Score':quality,'Quality %':score,'JBI':copy.deepcopy(review['jbi']['answers']),'Design':effective.get('design'),'Timing':effective.get('timing'),'Population':effective.get('population') or effective.get('country_or_region'),'Continent':effective.get('continent') or effective.get('country_or_region'),'Gender':effective.get('gender'),'Ethnicity':effective.get('ethnicity'),'Smoking Status':effective.get('smoking_status'),'Stage':ctx['outcome'],'exposure_measurement_type':effective.get('exposure_measurement_type'),'final_status':'INCLUDE_META' if poolable else 'ELIGIBLE_NOT_POOLABLE','meta_analysis_status':'INCLUDE_META' if poolable else 'ELIGIBLE_NOT_POOLABLE','policy_poolable':poolable,'scientific_pooling_eligible':poolable,'Primary Estimate':poolable,'canonical_track':'combined','combined_source':'preprint_recovery','requires_key_review':False,'effect_inverted':inverse,'inversion_applied':inverse,'needs_inversion':False if inverse else effective.get('needs_inversion'),'preprint_derived':True,'preprint_peer_reviewed':False,'preprint_native_raw_extraction':raw,'preprint_native_effective_extraction':effective,'combined_source_extraction':copy.deepcopy(effective),'preprint_evidence_quotes':copy.deepcopy(review.get('evidence_quotes') or []),'extraction_supporting_text':reported_support,'exposure_measurement_supporting_text':reported_support.get('exposure') if isinstance(reported_support,dict) else reported_support,'cohort_names':copy.deepcopy(names),'Cohort Name':'; '.join(str(x) for x in names),'publication_date':anchor['journal_publication_date'],'publication_date_precision':anchor['journal_date_precision'],'cohort_metadata':{'cohort_names':copy.deepcopy(names),'status':'reported_preprint' if names else 'unresolved_not_identified','publication_date':anchor['journal_publication_date'],'publication_date_precision':anchor['journal_date_precision'],'provenance':'Astra-selected complete preprint reviewer bundle','evidence_quotes':copy.deepcopy(review.get('evidence_quotes') or [])}}
 for k in ['p_value_original','p_value_lexical_parse','p_value_relation','comparison_type','comparison_direction','comparison_group','reference_group','unit','scale','adjusted_model','adjusted_covariates','subgroup','stratum','independent_population_id','ci_level','ci_confidence_level','ci_method','p_value_method','standard_error_method','p_value_provenance','ci_provenance','standard_error_provenance','policy_derivations','quantitative_policy_version','poolability_policy_version','imputation_notes','policy_original_values','direction_imputed','ci_level_assumed','effect_size_imputed','comparison_direction_imputed']:
  if k in effective:row[k]=copy.deepcopy(effective[k])
 if inherited_cohort:
  row['cohort_metadata'].update(provenance='Recorded UK Biobank name inherited from the Astra-selected single-cohort Vitamin B6/Stroke contrast set; no distinct population or cohort is identified for these supplemental dose contrasts.',inherited_from_context=ctx['context_id'],original_secondary_cohort_names=[])
 if inverse:row['preprint_native_inversion']={'method':'native explicit reverse contrast standardized once','reported_bundle':{k:raw.get(k) for k in ['effect_type','effect_size','ci_lower','ci_upper','comparison_direction','comparison_group','reference_group']}}
 if repo=='metaNeuro':
  cfg=NEURO_CONFIG[ctx['outcome']];row.update({'Outcome Key':ctx['outcome'],'Outcome Class':ctx['outcome'],'Outcome Label':cfg[2],'assigned_exposure_category':ctx['exposure'],'original_exposure_category':ctx['exposure'],'screening_context_id':identifier,'source_evidence_level':'PMID-linked preprint recovery','full_text_status':'preprint_full_text_reviewed','missing_quantities':copy.deepcopy(review.get('unresolved_fields') or []),'additional_effects':[]})
 row.update({'subexposure_pooling_selected':poolable,'pool_in_parent_meta_analysis':poolable,'pool_in_exposure_parent_meta_analysis':poolable,'pool_in_disease_parent_meta_analysis':poolable,'display_only':not poolable,'ui_display_only':not poolable})
 return clean(row),bindings

def display_only(row,reason):
 row.update({'Primary Estimate':False,'policy_poolable':False,'scientific_pooling_eligible':False,'subexposure_pooling_selected':False,'pool_in_parent_meta_analysis':False,'pool_in_exposure_parent_meta_analysis':False,'pool_in_disease_parent_meta_analysis':False,'display_only':True,'ui_display_only':True,'final_status':'ELIGIBLE_NOT_POOLABLE','meta_analysis_status':'ELIGIBLE_NOT_POOLABLE','display_only_reason':reason})

def identity_function(repo):
 p=ROOT/repo/'cohort_identity.py';spec=importlib.util.spec_from_file_location('_preprint_identity_'+key(repo),p);m=importlib.util.module_from_spec(spec);dont=sys.dont_write_bytecode;sys.dont_write_bytecode=True
 try:spec.loader.exec_module(m)
 finally:sys.dont_write_bytecode=dont
 return m.record_metadata_key

def hierarchy_ancestors(repo,exposure):
 p=ROOT/repo/'data/exposure_hierarchy.json'
 if not p.exists():return []
 h=json.loads(p.read_text());found=[];todo=[exposure];seen={key(exposure)}
 while todo:
  child=todo.pop()
  for label,parents in h.items():
   if key(label)!=key(child):continue
   for parent in parents if isinstance(parents,list) else []:
    if key(parent) not in seen:seen.add(key(parent));found.append(parent);todo.append(parent)
 return found

def native_plans():
 p=ROOT/'cohort_latest_2026-10-25/latest_summary_plan.log';plans={}
 if p.exists():
  for line in p.read_text().splitlines():
   try:r=json.loads(line)
   except ValueError:continue
   plans[r['repository']]=r['plan']
 return plans

def audit_input_bindings(ctx):
 result={};repo=ctx['repository']
 for r in ctx.get('r2_records') or []:
  path=r.get('source_path')
  if path and (ROOT/path).is_file() and Path(path).is_relative_to(repo):
   actual=sha(ROOT/path)
   if r.get('source_sha256') and actual!=r['source_sha256']:raise ValueError('Native R2/source cache changed since model handoff: '+path)
   result[str(Path(path).relative_to(repo))]=actual
 return result

def metadata_bindings(ctx):
 # Private R2 packets may contain abstracts and are not website dependencies.
 # Verify them now, retain only path/hash audit descriptors in the manifest.
 audit_input_bindings(ctx)
 result={};repo=ctx['repository']
 for r in ctx.get('native_criteria_sources') or []:
  path=Path(r['path'])
  if path.is_relative_to(repo) and (ROOT/path).is_file():
   actual=sha(ROOT/path)
   if r.get('sha256') and actual!=r['sha256']:raise ValueError('Native criteria code changed since model handoff: '+str(path))
   result[str(path.relative_to(repo))]=actual
 for rel in ['data/exposure_hierarchy.json','data/cohort_registry.json','data/cohort_record_metadata.json','cohort_identity.py','meta_analysis.py','scripts/subexposure_views.py','scripts/screening_contract.py','data/full_ncbi_rescreen_v3/generation_manifest.json']:
  p=ROOT/repo/rel
  if p.exists():result[rel]=sha(p)
 if repo=='metaHeart':
  for rel in ['data/local_screening_active.json','scripts/local_screening_view.py','data/synonyms_cache.json','data/additionalExposure.json','data/additionalExposureV2.json']:
   p=ROOT/repo/rel
   if p.exists():result[rel]=sha(p)
 return result

def heart_source_root():
 root=ROOT/'metaHeart';active=root/'data/local_screening_active.json'
 if not active.is_file():return root/'Cached_results',{}
 selected=json.loads(active.read_text());view_id=selected.get('view_id')
 if selected.get('schema_version')!=1 or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*',str(view_id or '')):raise ValueError('Invalid Heart active source pointer')
 directory=root/'data/local_screening_views'/view_id;coverage=directory/'screening_coverage.json'
 if sha(coverage)!=selected.get('coverage_sha256'):raise ValueError('Heart active source coverage changed')
 doc=json.loads(coverage.read_text());assets=directory/'output_assets.json'
 if doc.get('view_id')!=view_id or doc.get('full_campaign_complete') is not False or doc.get('status')!='screening_incomplete' or sha(assets)!=doc.get('output_assets_sha256'):raise ValueError('Heart active source generation is unverified')
 target=(root/doc['cache_root']).resolve()
 if target!=(directory/'Cached_results').resolve() or not target.is_dir():raise ValueError('Heart source cache escapes selected native generation')
 return target,{str(coverage.relative_to(root)):sha(coverage),str(assets.relative_to(root)):sha(assets)}

def heart_canonical_exposure(value):
 root=ROOT/'metaHeart';path=root/'meta_analysis.py';tree=ast.parse(path.read_text());function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_canonical_name');ns={'json':json,'EXPOSURE_CATALOG_FILES':[root/'data'/name for name in ['synonyms_cache.json','additionalExposure.json','additionalExposureV2.json']]}
 exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),ns)
 return ns['get_canonical_name'](value)

def cache_cells(ctx):
 """Return exact native request cells and their original saved source files."""
 repo=ctx['repository'];root=ROOT/repo;cells=[]
 if repo=='metaHeart':
  base,source_bindings=heart_source_root();exposure=heart_canonical_exposure(ctx['exposure'])
  for mode in ['yes','no']:
   for requested in [exposure,*(hierarchy_ancestors(repo,exposure) if mode=='yes' else [])]:
    requested=heart_canonical_exposure(requested);folder=base.parent/'Subexposure_views'/mode/base.name/key(requested);paths=[folder/(key(ctx['disease'])+'_incidence_true_'+kind+'.json') for kind in ['all','core']];existing=[p for p in paths if p.is_file()]
    payload=json.loads(existing[0].read_text()) if existing else {'exposure':requested,'disease':ctx['disease'],'outcome':'Incidence','endpoint':'Incidence','canonical_track':'combined','include_subexposures':mode,'studies':[],'screening_stats':{},'preprint_native_payload_template':True}
    direct={'selector':{'source_repo':repo,'disease':ctx['disease'],'exposure':requested,'outcome':'Incidence','include_subexposures':mode,'source_variant':'combined'},'paths':paths,'payload':payload,'inherited':key(requested)!=key(exposure),'source_bindings':source_bindings};cells.append(direct)
    # Preserve an existing child estimate in a displayed parent, without
    # introducing a child into a parent cell that did not already contain it.
    for parent_path in sorted(folder.glob('*_incidence_true_all.json')):
     parent_payload=json.loads(parent_path.read_text());parent=parent_payload.get('disease') or parent_payload.get('cancer')
     if not parent or key(parent)==key(ctx['disease']):continue
     parent_paths=[parent_path,parent_path.with_name(parent_path.name.replace('_true_all.json','_true_core.json'))]
     cell={'selector':{**direct['selector'],'disease':parent},'paths':parent_paths,'payload':parent_payload,'inherited':direct['inherited'],'disease_inherited':True,'source_bindings':source_bindings}
     originals=matching_originals(ctx,cell)
     if originals and all(key(r.get('Direct Disease'))==key(ctx['disease']) and r.get('pool_in_disease_parent_meta_analysis') is False and r.get('Disease Hierarchy Inherited') is True for r in originals.values()):cells.append(cell)
  return cells
 if repo=='metaNeuro':
  cfg=NEURO_CONFIG[ctx['outcome']];resolver=WORK/'converter_inputs/neuro_cache_resolution.json';resolved=None
  if resolver.exists():
   doc=json.loads(resolver.read_text());rs=doc.get('contexts',doc.get('records',[]))
   for z in rs:
    if z.get('context_id')==ctx['context_id']:resolved=z;break
  sourcepath=None
  if resolved:sourcepath=resolved.get('cache_path') or resolved.get('source_cache_path')
  if sourcepath:
   p=Path(sourcepath);p=p if p.is_absolute() else root/p
   if resolved.get('source_sha256') and (not p.exists() or sha(p)!=resolved['source_sha256']):raise ValueError('Resolved native cache changed since exact source mapping')
   if resolved.get('source_exists') is False and p.exists():raise ValueError('A missing native source cell now exists; refresh the exact source guard')
  else:
   candidates=[]
   for e in ctx.get('evidence') or []:
    s=(e.get('saved_record') or {}).get('Source','').split('#')[0]
    if s and (root/s).is_file() and 'Cached_results' in s or s and (root/s).is_file() and '/metaneuro/' in s:candidates.append(root/s)
   base=root/'Cached_results' if ctx['outcome']=='alzheimer_disease_incidence' else root/'data/metaneuro'/cfg[1]
   candidates+=list((base/key(ctx['exposure'])).glob('*_true_all_*.json')) if (base/key(ctx['exposure'])).exists() else []
   candidates=list(dict.fromkeys(candidates));p=sorted(candidates,key=lambda z:('openai.gpt-5.4' not in z.name,str(z)))[0] if candidates else base/key(ctx['exposure'])/(cfg[1]+'_true_all_openai.gpt-5.4.json')
  payload=json.loads(p.read_text()) if p.exists() else {'success':True,'studies':[],'outcome_key':ctx['outcome'],'outcome_label':cfg[2],'batch_exposure':key(ctx['exposure']),'batch_outcome_key':ctx['outcome'],'article_audit':[],'outcome_type':'continuous' if ctx['outcome'] in {'psqi','sleep_efficiency'} else 'ratio','higher_is_worse':ctx['outcome']!='sleep_efficiency','display_scale':'beta' if ctx['outcome'] in {'psqi','sleep_efficiency'} else 'rr_both','preprint_native_payload_template':True}
  for mode in ['yes','no']:
   cells.append({'selector':{'source_repo':repo,'disease':cfg[0],'exposure':key(ctx['exposure']),'outcome':ctx['outcome'],'include_subexposures':mode,'source_variant':'combined'},'paths':[p],'payload':payload,'inherited':False})
  return cells
 plans=native_plans().get(repo,[])
 if not plans:raise ValueError('No verified active source plan for '+repo)
 for plan in plans:
  base=Path(plan['cache_root']);mode=plan['mode'];variant='chatgpt' if repo=='metaFemina' and '/chatgpt/' in str(base) else 'combined'
  for expose in [ctx['exposure'],*(hierarchy_ancestors(repo,ctx['exposure']) if mode=='yes' else [])]:
   folder=base/key(expose);paths=[];payload=None
   if folder.exists():
    for p in sorted(folder.glob('*.json')):
     if not any(t in p.name for t in ['_true_all','_true_core']):continue
     d=json.loads(p.read_text());disease=d.get('disease') or d.get('cancer');outcome=d.get('endpoint') or d.get('outcome') or 'Incidence'
     if key(disease)!=key(ctx['disease']) or key(outcome)!=key(ctx['outcome']):continue
     if d.get('exposure') and key(d['exposure'])!=key(expose):continue
     paths.append(p)
     if payload is None or '_true_all' in p.name:payload=d
   if not paths:
    # Existing disease source roots must agree; never create a cancer in a nonmalignant branch or vice versa.
    is_nonmal=any(t in str(base) for t in ['nonmalignant','ibd_extension'])
    target_nonmal=any(t in str(ctx['disease']).lower() for t in ['masld','nafld','metabolic dysfunction','crohn','ulcerative','inflammatory bowel','cholelithiasis','diverticul','pancreatitis'])
    if repo in {'metaGut','metaBreath'} and is_nonmal!=target_nonmal:continue
    p=folder/(key(ctx['disease'])+'_'+key(ctx['outcome'])+'_true_all.json');paths=[p]
    payload={'exposure':expose,'disease':ctx['disease'],'cancer':ctx['disease'],'endpoint':ctx['outcome'],'outcome':ctx['outcome'],'canonical_track':variant,'include_subexposures':mode,'studies':[],'screening_stats':{},'preprint_native_payload_template':True}
   selector={'source_repo':repo,'disease':payload.get('disease') or payload.get('cancer') or ctx['disease'],'exposure':payload.get('exposure') or expose,'outcome':payload.get('endpoint') or payload.get('outcome') or ctx['outcome'],'include_subexposures':mode,'source_variant':variant}
   cells.append({'selector':selector,'paths':paths,'payload':payload,'inherited':key(expose)!=key(ctx['exposure'])})
 return cells

def matching_originals(ctx,cell):
 found=[]
 for p in cell['paths']:
  if not p.exists():continue
  packet=json.loads(p.read_text())
  for row in packet.get('studies') or []:
   if str(row.get('PMID') or row.get('pmid') or '')!=str(ctx['pmid']):continue
   direct=row.get('Direct Exposure') or row.get('direct_exposure') or row.get('assigned_exposure_category') or row.get('original_exposure_category')
   if direct and key(direct)!=key(ctx['exposure']):continue
   native_id=row.get('context_id')
   if ctx['repository']!='metaNeuro' and native_id and '|' in str(native_id) and native_id!=ctx['context_id']:continue
   found.append(row)
 ident=identity_function(ctx['repository']);unique={}
 science=['PMID','Effect Type','Effect Size','Lower CI','Upper CI','Sample Size','Cases','Gender','Population','comparison_type','subgroup','stratum','independent_population_id']
 def signature(row):return [number(row.get(k)) if k in {'Effect Size','Lower CI','Upper CI','Sample Size','Cases'} else re.sub(r'\s+',' ',str(row.get(k) or '')).strip() for k in science]
 for row in found:
  rid=ident(row)
  if rid in unique and signature(unique[rid])!=signature(row):raise ValueError('Conflicting saved scientific rows share an original record ID: '+rid)
  unique[rid]=row
 return unique

def make_manifest_contexts(ctx,review,source,med,result_path):
 if not (source.get('preprint_version') or source.get('version')):raise ValueError('An exact preprint version is required for automatic activation')
 anchor=publication_anchor(ctx,med);raw=combine_bundle(review,ctx['repository']);bundles=[raw]
 for field in ['additional_extractions','additional_studies']:
  extra=raw.get(field)
  if isinstance(extra,list):bundles.extend(copy.deepcopy(x) for x in extra if isinstance(x,dict))
 records=[];policies={}
 for i,bundle in enumerate(bundles):
  row,bindings=native_row(ctx,review,source,anchor,med,bundle,i)
  if i>0:
   row['preprint_secondary_extraction']=True;display_only(row,'Additional selected-review extraction preserved; automatic pooling is limited to the primary Astra-selected estimate. Dose contrasts and overlapping populations are not independent studies.')
  records.append(row);policies.update(bindings)
 result=[]
 for cell in cache_cells(ctx):
  originals=matching_originals(ctx,cell)
  for original in originals.values():
   if original.get('manual_review') or original.get('manual_adjudication') or original.get('human_adjudication') or str(original.get('decision_origin','')).lower() in {'human','manual','human_manual_review'}:raise ValueError('Existing native row is human-protected; preserve it for manual review')
  ids=sorted(originals);rows=copy.deepcopy(records)
  for row in rows:
   # Reanalysis must select the complete original context to expand an unmapped multi-stratum replacement.
   row['preprint_original_record_ids']=ids;row['Displayed Exposure']=cell['selector']['exposure'];row['include_subexposures']=cell['selector']['include_subexposures']
   if cell['inherited']:
    row.update(hierarchy_inherited=True,hierarchy_source_poolable=row['policy_poolable'],hierarchy_requested_exposure=cell['selector']['exposure'],hierarchy_pooling_allowed=True,inherited_exposure_memberships=[cell['selector']['exposure']],hierarchy_source_exposures=[ctx['exposure']])
   row['Displayed Disease']=cell['selector']['disease']
   if cell.get('disease_inherited'):
    row.update({'Disease Hierarchy Inherited':True,'disease_hierarchy_source_poolable':row['policy_poolable']});display_only(row,'Existing native child-disease record replaced in its parent display only; parent pooling remains prohibited.')
  baselines={**metadata_bindings(ctx),**policies,**cell.get('source_bindings',{})}
  for p in cell['paths']:
   if p.exists():baselines[str(p.relative_to(ROOT/ctx['repository']))]=sha(p)
  if not baselines:raise ValueError('No immutable source/code baseline for native cell')
  source_meta={'source':'EuropePMC PMID-linked preprint','id':source['preprint_id'],'doi':source.get('preprint_doi'),'version':source.get('preprint_version') or source.get('version'),'posted_date':source.get('preprint_posted_date'),'source_sha256':source['text_sha256'],'raw_source_sha256':source.get('raw_source_sha256'),'url':source['retrieval_url'],'published_pmid':str(ctx['pmid']),'peer_reviewed':False}
  result.append({'context_id':'preprint-cell:'+digest([ctx['repository'],ctx['context_id'],source_meta['id'],source_meta['version'],cell['selector']])[:32],'review_context_id':ctx['context_id'],'selector':cell['selector'],'source_cache_paths':[str(p.relative_to(ROOT/ctx['repository'])) for p in cell['paths']],'operation':'replace' if ids else 'add','original_record_ids':ids,'review':{'status':'adjudicated','decision':'include','agreement':'Astra high-confidence selected complete reviewer bundle','review_receipt_sha256':sha(result_path)},'publication_anchor':anchor,'preprint_source':source_meta,'baseline_files':baselines,'audit_input_bindings':audit_input_bindings(ctx),'native_candidate_poolable_record_ids':[identity_function(ctx['repository'])(r) for r in rows if r.get('policy_poolable')], 'native_candidate_dietary_record_ids':[identity_function(ctx['repository'])(r) for r in rows if r.get('policy_poolable') and r.get('exposure_measurement_type')=='dietary_intake'],'native_poolable_scope_note':'Candidate metadata only; actual native final engine selection remains authoritative.','native_rows':rows,'native_payload':copy.deepcopy(cell['payload']) if not any(p.exists() for p in cell['paths']) else None})
 return result

def authorized_article_gate(job):
 ref=job.get('pmc_availability_gate')
 if not isinstance(ref,dict):raise ValueError('Missing bound article-level PMC availability gate')
 path=Path(ref.get('path') or '')
 if not path.is_absolute():path=ROOT/path
 if not path.is_file() or sha(path)!=ref.get('sha256'):raise ValueError('Article-level PMC gate file changed')
 doc=json.loads(path.read_text());rows=doc.get('records')
 if not isinstance(rows,list):raise ValueError('Article-level PMC gate records must be a list')
 matches=[r for r in rows if str(r.get('pmid'))==str(job['source']['published_pmid'])]
 if len(matches)!=1:raise ValueError('No unique exact-PMID article-level gate')
 record=matches[0]
 if str(ref.get('pmid'))!=str(record['pmid']) or digest(record)!=ref.get('record_sha256'):raise ValueError('Article-level gate record binding changed')
 if record.get('availability') not in {'pmcid_missing','full_text_unavailable'} or record.get('preprint_fetch_allowed') is not True or record.get('model_dispatch_authorized') is not True or record.get('saved_any_track_checked') is not True or record.get('saved_any_track_full_text_available') is not False or not record.get('any_track_gate_record_sha256'):raise ValueError('Full PMC available, unknown or unauthorized article; preprint overlay forbidden')
 if record['availability']=='pmcid_missing' and (record.get('pmcids') or record.get('fresh_epmc_pmcid') or record.get('fresh_pubmed_pmcids')):raise ValueError('Conflicting current PMCID metadata')
 if record['availability']!='pmcid_missing' and not record.get('proofs'):raise ValueError('Actual unavailable-PMC proof missing')
 return copy.deepcopy(record)


def active_result_files(results,index_path=None,qa_preview=False):
 if index_path is None:
  if not qa_preview:raise ValueError('Official conversion requires an explicit hash-bound active result index')
  return [(p,None) for p in sorted(Path(results).glob('jobs/*/result.json'))],None
 index_path=Path(index_path);index=json.loads(index_path.read_text());jobs=index.get('jobs');authority=index.get('authoritative_context_jobs')
 if index.get('schema_version')!=1 or not isinstance(jobs,list) or not isinstance(authority,dict):raise ValueError('Unsupported active result index')
 selected=[];seen={}
 for item in jobs:
  path=Path(item.get('result_path') or item.get('result_file') or '')
  if not path.is_absolute():path=WORK/path
  job_path=Path(item.get('job_file') or path.with_name('inputs.json'))
  if not job_path.is_absolute():job_path=WORK/job_path
  if path.with_name('inputs.json').resolve()!=job_path.resolve():raise ValueError('Active result index does not bind adjacent original job input')
  if not path.is_file() or sha(path)!=item.get('result_sha256') or not job_path.is_file() or sha(job_path)!=item.get('inputs_sha256'):raise ValueError('Active result/input hash changed')
  job=json.loads(job_path.read_text());result=json.loads(path.read_text());jid=item.get('job_id');repo=item.get('repository') or job.get('repository');wanted=item.get('context_ids')
  if not jid or result.get('job_id')!=jid or not isinstance(wanted,list) or not wanted or len(wanted)!=len(set(wanted)):raise ValueError('Active result job identity/contexts invalid')
  actual={r['context_id'] for r in job.get('contexts',[])}
  if not set(wanted)<=actual:raise ValueError('Active index context missing from bound job')
  if any((r.get('repository') or job.get('repository'))!=repo for r in job.get('contexts',[]) if r['context_id'] in wanted):raise ValueError('Active index repository differs from native context')
  for cid in wanted:
   identity=str(repo)+'|'+cid
   if identity in seen or authority.get(identity)!=jid:raise ValueError('Duplicate or nonauthoritative active context: '+identity)
   seen[identity]=jid
  selected.append((path,set(wanted)))
 if seen!=authority:raise ValueError('Active result index authority map has missing/unlisted contexts')
 return selected,{'path':str(index_path),'sha256':sha(index_path),'jobs':len(jobs),'contexts':len(seen)}

def compile_manifests(results,output,only_contexts=None,qa_preview=False,active_result_index=None):
 output=Path(output);output.mkdir(parents=True,exist_ok=True);manifests={repo:{'schema_version':1,'repository':repo,'status':'complete','generation_id':'preprint-recovery-2026-10-26-v1','generated_at':datetime.now(timezone.utc).isoformat(),'qa_only':bool(qa_preview),'campaign_status':'in_progress_QA_only' if qa_preview else 'awaiting_release_freeze_receipt','contexts':[]} for repo in ['MetaVir','metaThyroid','metaGut','metaFemina','metaHeart','metaBreath','metaPee','metaNeuro','MetaCutis_et_ Capillus']};audits=[];protected={};gate_records={repo:{} for repo in manifests}
 matches=json.loads((WORK/'europepmc_research/catalogue/linked_candidates.json').read_text());medpaths={m['published_pmid']:WORK/'europepmc_research'/m['med_record'] for m in matches}
 spec=importlib.util.spec_from_file_location('_preprint_screening_validate',Path(__file__).with_name('preprint_screening.py'));driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)
 active_files,index_receipt=active_result_files(results,active_result_index,qa_preview)
 for result_path,active_context_ids in active_files:
  job_path=result_path.with_name('inputs.json');result=json.loads(result_path.read_text());job=json.loads(job_path.read_text());contexts={r['context_id']:r for r in job['contexts']};source=job['source'];entry={'job_id':result.get('job_id'),'result_path':str(result_path),'result_sha256':sha(result_path),'status':result.get('status'),'context_results':[]}
  if only_contexts and not any((r.get('repository') or job.get('repository'),str(r.get('pmid'))) in only_contexts for r in job['contexts']):continue
  if result.get('status')!='reviewed':audits.append(entry);continue
  try:gate_record=authorized_article_gate(job)
  except ValueError as exc:entry['hold']=str(exc);audits.append(entry);continue
  text=driver.validate_source(job)
  if source.get('raw_path') and sha(source['raw_path'])!=source.get('raw_source_sha256'):raise ValueError('Raw preprint source hash changed')
  for role,review in result.get('reviews',{}).items():driver.validate_review(review,job,text)
  driver.validate_adjudication(result['adjudication'],job,text,result['reviews'])
  for final in result['adjudication']['results']:
   if active_context_ids is not None and final['context_id'] not in active_context_ids:continue
   ctx=contexts[final['context_id']]
   if only_contexts and (ctx.get('repository') or job.get('repository'),str(ctx.get('pmid'))) not in only_contexts:continue
   audit={'repository':ctx.get('repository') or job.get('repository'),'pmid':str(ctx.get('pmid')),'context_id':ctx['context_id'],'exposure':ctx.get('exposure'),'disease':ctx.get('disease'),'outcome':ctx.get('outcome'),'decision':final['decision'],'confidence':final['confidence'],'selected_reviewer':final.get('selected_reviewer'),'reason':final['reason'],'activated':False}
   if audit['repository'] not in manifests:audit['hold']='Native converter owner is another agent';entry['context_results'].append(audit);continue
   if final['decision'] not in {'INCLUDE_META','ELIGIBLE_NOT_POOLABLE'} or final['confidence']!='high' or not isinstance(final.get('unresolved_fields'),list) or final['unresolved_fields'] or final.get('selected_reviewer') not in {'luna','gemini'}:
    audit['hold']='Unresolved, exclusion, non-high-confidence or incomplete decision remains audit-only';entry['context_results'].append(audit);continue
   selected=result['reviews'][final['selected_reviewer']]
   if selected.get('identity_valid') is not True:audit['hold']='Selected reviewer did not verify linked version identity';entry['context_results'].append(audit);continue
   review=next(r for r in selected['results'] if r['context_id']==ctx['context_id'])
   if review.get('status')!=final['decision']:audit['hold']='Astra selected disposition differs from complete reviewer bundle';entry['context_results'].append(audit);continue
   medpath=medpaths[str(ctx['pmid'])];med=json.loads(medpath.read_text());med['_preprint_recovery_source']={'path':str(medpath.relative_to(WORK)),'sha256':sha(medpath),'type':'saved EuropePMC MED core record'}
   try:
    additions=make_manifest_contexts(ctx,review,source,med,result_path);manifests[audit['repository']]['contexts'].extend(additions);gate_records[audit['repository']][str(ctx['pmid'])]=gate_record;audit.update(activated=True,native_cells=len(additions),native_rows=sum(len(r['native_rows']) for r in additions))
    for cell in additions:
     for rel,d in cell['baseline_files'].items():protected[str(ROOT/audit['repository']/rel)]=d
     for rel,d in cell['audit_input_bindings'].items():protected[str(ROOT/audit['repository']/rel)]=d
   except (ValueError,KeyError,TypeError) as exc:audit['hold']='Native conversion hold: '+str(exc)
   entry['context_results'].append(audit)
  audits.append(entry)
 for repo,manifest in manifests.items():
  local_proof={'schema_version':1,'records':[gate_records[repo][pid] for pid in sorted(gate_records[repo],key=int)]}
  proof_path=output/repo/'article_availability_gate.json';save(proof_path,local_proof)
  for context in manifest['contexts']:
   pid=str(context['publication_anchor']['pmid']);context['article_availability_gate']={'path':'data/preprint_recovery/article_availability_gate.json','sha256':sha(proof_path),'pmid':pid,'record_sha256':digest(gate_records[repo][pid])}
  ids=[r['context_id'] for r in manifest['contexts']]
  if len(ids)!=len(set(ids)):raise ValueError('Duplicate native source cells require explicit version arbitration')
  save(output/repo/'active_manifest.json',manifest)
 changes=[p for p,d in protected.items() if not Path(p).exists() or sha(p)!=d]
 if changes:raise ValueError('Original source changed during conversion: '+', '.join(changes))
 holds={repo:[] for repo in manifests}
 for job_audit in audits:
  for item in job_audit.get('context_results',[]):
   if str(item.get('hold','')).startswith('Native conversion hold: '):
    holds[item['repository']].append({**{k:item.get(k) for k in ('repository','pmid','context_id','exposure','disease','outcome')},'reason':item['hold'],'action':'No activation; inspect exact native source/cache/ID/protection evidence. Original journal records remain unchanged.'})
 save(output/'conversion_holds_by_repository.json',holds)
 receipt={'converter_version':VERSION,'converter_sha256':sha(__file__),'generated_at':datetime.now(timezone.utc).isoformat(),'model_calls':0,'new_cost_usd':0,'qa_only':bool(qa_preview),'full_campaign_complete':False,'active_result_index':index_receipt,'source_artifacts_checked':len(protected),'source_artifacts_changed':changes,'repositories':{repo:{'contexts':len(m['contexts']),'rows':sum(len(x['native_rows']) for x in m['contexts']),'manifest_sha256':sha(output/repo/'active_manifest.json')} for repo,m in manifests.items()},'jobs':audits};save(output/'conversion_receipt.json',receipt);return receipt

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--results',type=Path,default=WORK/'screening_run');parser.add_argument('--output',type=Path,default=WORK/'overlay_staging');parser.add_argument('--active-result-index',type=Path);parser.add_argument('--qa-preview',action='store_true');parser.add_argument('--only',action='append',default=[],help='Repository:PMID');args=parser.parse_args();only={tuple(x.split(':',1)) for x in args.only} or None;r=compile_manifests(args.results,args.output,only_contexts=only,qa_preview=args.qa_preview,active_result_index=args.active_result_index);print(json.dumps({k:v for k,v in r.items() if k!='jobs'},indent=2))
