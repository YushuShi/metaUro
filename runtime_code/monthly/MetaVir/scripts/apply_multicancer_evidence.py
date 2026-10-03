"""Rebuild only source-reviewed cancer/exposure cache scopes, offline."""
from __future__ import annotations
import contextlib,copy,gzip,hashlib,io,json,math,sys
from pathlib import Path
from collections import defaultdict,Counter
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import multicancer_evidence as review
from scripts import subexposure_policy as policy
OUT=review.DIRECTORY
VERSION=review.VERSION

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def clean(v):
 if isinstance(v,dict):return {k:clean(x) for k,x in v.items()}
 if isinstance(v,(tuple,list)):return [clean(x) for x in v]
 if hasattr(v,'item'):return clean(v.item())
 if isinstance(v,float) and not math.isfinite(v):return None
 return v
def write(path,value):
 path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(clean(value),ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def read(path):return json.loads(path.read_text())
def preserve(path):
 if path.exists():
  dest=OUT/'preimages'/(hashlib.sha256(str(path.relative_to(ROOT)).encode()).hexdigest()+'.json.gz')
  if not dest.exists():dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(gzip.compress(path.read_bytes(),mtime=0))
  return sha(path)
 return None

def plan():
 hierarchy=read(ROOT/'data/exposure_hierarchy.json')
 profiles=[('combined','no')]
 if ROOT.name=='MetaVir':profiles += [(f'Subexposure_results/{m}',m) for m in ['no','yes']]
 else:profiles += [('combined/provisional_ui','no')]+[(f'combined/provisional_ui/subexposure_views/{m}/Cached_results',m) for m in ['no','yes']]
 delta=defaultdict(list)
 for entry in review.registry()['entries']:delta[(entry['record']['exposure'],entry['record']['disease'])].append(entry['row'])
 if ROOT.name=='metaPee':delta.setdefault(('Processed meat intake','Renal cell carcinoma'),[])
 plans={}
 for folder,mode in profiles:
  root=ROOT/folder
  for (exposure,disease),new in delta.items():
   targets={exposure}
   if mode=='yes':
    for parent in {p for parents in hierarchy.values() for p in parents}:
     if policy.key(exposure) in {policy.key(c) for c in policy.descendants(parent,hierarchy)}:targets.add(parent)
   for target in targets:
    stem=policy.key(disease)+'_incidence_true_'
    for suffix in ['all','core']:
     path=root/policy.key(target)/(stem+suffix+'.json')
     existing=read(path) if path.exists() else {}
     if existing.get('disease_hierarchy_prebuilt'):
      path=root/'_disease_scope_sources'/path.relative_to(root);existing=read(path) if path.exists() else {}
     if not existing and suffix=='all':
      paired=path.with_name(path.name.replace('_all.json','_core.json'))
      if paired.exists():existing=read(paired)
     if not existing:
      existing={'success':True,'canonical_track':'combined','exposure':target,'cancer':disease,'disease':disease,'endpoint':'Incidence','outcome':'Incidence','studies':[]}
      if ROOT.name=='MetaVir':existing['combined_projection']={'build_id':read(ROOT/'combined/_merge_manifest.json')['build_id']}
     prior=plans.get(path,{}).get('payload',existing)
     rows=review.apply_rows(prior.get('studies',[]),target,disease,add=(target==exposure))
     if target!=exposure:
      ids={r.get('Effect ID') for r in rows}
      for row in new:
       if row['Effect ID'] not in ids:
        row=copy.deepcopy(row);row['Displayed Exposure']=target;row['Hierarchy Inherited']=True;rows.append(row)
     if rows!=existing.get('studies',[]):
      payload=copy.deepcopy(prior);payload['studies']=rows;payload['evidence_revision']=VERSION
      if '/yes' in folder or '/no' in folder:payload['include_subexposures']=mode
      plans[path]={'payload':payload,'mode':mode,'before_sha256':sha(path) if path.exists() else None}
 return plans

def calculate(path,item,memo):
 import pandas as pd
 import meta_analysis as backend
 payload=item['payload'];rows=payload['studies'];exposure=payload['exposure'];disease=payload.get('disease') or payload['cancer']
 sources=defaultdict(list)
 for row in rows:sources[row.get('Direct Exposure') or exposure].append(row)
 selected=policy.project_rows(sources,exposure,read(ROOT/'data/exposure_hierarchy.json'),include=item['mode'])
 select_ids={r.get('Effect ID') for r in policy.pooling_rows(selected)}
 for row in rows:row['subexposure_pooling_selected']=row.get('Effect ID') in select_ids
 pool=policy.pooling_rows(rows)
 frame=pd.DataFrame(pool)
 if not frame.empty:frame=backend.filter_curated_meta_analysis_exclusions(frame,disease,exposure,'Incidence')
 if not frame.empty and hasattr(backend,'human_pooling_rows'):frame=backend.human_pooling_rows(frame)
 key=hashlib.sha256(json.dumps([disease,exposure,item['mode'],rows],sort_keys=True,default=str).encode()).hexdigest()[:20]
 if key not in memo:
  result={'success':True,'headline':None,'summary_html':'<p>Eligible reports remain visible; no estimates meet the current pooling criteria.</p>','plot_url':None,'funnel_plot_url':None,'baujat_plot_url':None}
  if not frame.empty:
   with contextlib.redirect_stdout(io.StringIO()):
    if ROOT.name=='MetaVir':
     result=backend.perform_meta_analysis(frame,disease,exposure,outcome='Incidence',exclude_meta=True,df_all=pd.DataFrame(rows),generate_plots=True,plot_namespace='subexposures/'+item['mode']+'/interactive/'+key[:16])
    else:
     # Use the established baseline/conversion pipeline with an isolated plot folder.
     from scripts import refresh_trend_ci_results as native
     native.refresh(path,payload)
     result={k:payload.get(k) for k in ['success','headline','summary_html','plot_url','funnel_plot_url','baujat_plot_url','pooled_study_count']}
   if result.get('error'):raise RuntimeError(str(path)+': '+str(result['error']))
  memo[key]=clean(result)
 for field in ['headline','summary_html','plot_url','funnel_plot_url','baujat_plot_url']:
  payload[field]=copy.deepcopy(memo[key].get(field))
 if isinstance(payload.get('forest_plot'),dict):payload['forest_plot']['url']=payload.get('plot_url')
 if isinstance(payload.get('screening_stats'),dict):payload['screening_stats']['extracted']=len(rows)
 payload['subexposure_projection']={'mode':item['mode'],'table_rows':len(rows),'pooling_rows':len(frame),'policy':'transitive-child-closure-distinct-populations-v1'}
 payload['evidence_revision_source_sha256']=sha(OUT/'registry.json')
 return {'pooling_rows':len(frame),'rows':len(rows),'calculation_id':key}

def apply():
 if (OUT/'application_report.json').exists():raise RuntimeError('Already applied; validate receipt before another release')
 import requests
 requests.sessions.Session.request=lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('Offline rebuild forbids transport'))
 plans=plan();write(OUT/'plan.json',{'version':VERSION,'files':{str(p.relative_to(ROOT)):{'before_sha256':v['before_sha256'],'row_count':len(v['payload']['studies'])} for p,v in plans.items()}})
 changes=[];memo={}
 for path,item in plans.items():
  old=preserve(path);report=calculate(path,item,memo);write(path,item['payload']);changes.append({'path':str(path.relative_to(ROOT)),'before_sha256':old,'after_sha256':sha(path),**report});print(str(path.relative_to(ROOT)),report['pooling_rows'],flush=True)
 masters=['combined/final_screening.json'] if ROOT.name=='MetaVir' else ['combined/effective_results.json','combined/provisional_ui/effective_results.json']
 for relative in masters:
  p=ROOT/relative;obj=read(p);before=preserve(p);obj['results']=review.effective_records(obj['results']);obj['evidence_revision']=VERSION
  counts=dict(Counter(r.get('final_status') for r in obj['results']))
  for key in ['counts','status_counts']:
   if key in obj:obj[key]=counts
  write(p,obj);changes.append({'path':relative,'before_sha256':before,'after_sha256':sha(p),'screening_contexts':len(obj['results'])})
 if ROOT.name=='metaPee':
  from scripts import provisional_disease_scopes as parent
  affected={e['record']['exposure'] for e in review.registry()['entries']}
  for mode in ['no','yes']:
   root=ROOT/f'combined/provisional_ui/subexposure_views/{mode}/Cached_results'
   parent.materialize(root,affected_exposures=affected)
   # Parent analyses are saved by the native composer; render their matching plots.
   for p in sorted(root.glob('*/*_true_core.json')):
    value=read(p)
    if value.get('disease_hierarchy_prebuilt') and value.get('exposure') in affected:
     from scripts import refresh_trend_ci_results as native
     before=preserve(p)
     with contextlib.redirect_stdout(io.StringIO()):native.refresh(p,value)
     value['evidence_revision']=VERSION
     for suffix in ['core','all']:
      dest=p.with_name(p.name.replace('_core.json','_'+suffix+'.json'));write(dest,value)
      changes.append({'path':str(dest.relative_to(ROOT)),'before_sha256':before,'after_sha256':sha(dest),'parent_refreshed':True})
 write(OUT/'application_report.json',{'version':VERSION,'candidate_rows':len(review.registry()['entries']),'added_rows':len(review.registry()['entries']),'poolable_additions':sum(e['row']['policy_poolable'] for e in review.registry()['entries']),'network_calls':0,'model_calls':0,'changes':changes,'unique_calculations':len(memo),'status':'cache_refresh_complete_summary_pending'})
if __name__=='__main__':apply()
