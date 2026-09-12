#!/usr/bin/env python3
"""Rebuild effective evidence and app projections, offline, from immutable sources."""
from __future__ import annotations

import copy,hashlib,json,sys,re,math
from collections import Counter,defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import dietary_only_policy as dietary_policy
from scripts import dual_screening as ds,monthly_evidence_update as monthly,relaxed_poolability as policy
import meta_analysis
import pandas as pd
AUDIT=ROOT/'outputs/poolability_policy_2026-09-05'
CG=ROOT/'data/claude_gemini_screening/all_history_through_2026-08-29'
SOURCES={}

def read(p):
 if not p.exists():return {}
 b=p.read_bytes();SOURCES[str(p.relative_to(ROOT))]=hashlib.sha256(b).hexdigest();return json.loads(b)
def write(p,d):
 p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(monthly.json_compatible(d),ensure_ascii=False,allow_nan=False,indent=2)+'\n')
def records(d):
 rows=d.get('results_by_context') or d.get('results',[])
 return list(rows.values()) if isinstance(rows,dict) else rows

def source_rows(folder):
 rows={}
 for name in ['second_screen.json','chatgpt_second_screen.json','chatgpt_full_text_review_partial_with_unresolved.json','chatgpt_full_text_review.json','chatgpt_final_screening.json']:
  for row in records(read(folder/name)):
   cid=row.get('context_id')
   if cid:rows[cid]={**rows.get(cid,{}),**copy.deepcopy(row)}
 for row in records(read(folder/'human_effective_results.json')):
  if row.get('manual_review'):rows[row['context_id']]=copy.deepcopy(row)
 return rows

def chatgpt():
 selected=records(read(CG/'r1_selected_decisions.json'))
 if not selected:
  prepared=read(CG/'prepared_candidates.json')
  aliases={s['source']:str(Path(s['first_screen_path']).parent) for s in prepared['source_first_screen_packets']}
  selected=[dict(r,selected_source=aliases[r['selected_first_screen_source']],decision='PASS') for r in prepared['contexts']]
 folders={r['selected_source'] for r in selected}
 runs={name:source_rows(ROOT/name if '/' in name else ROOT/'data/monthly_updates'/name) for name in folders}
 rows={}
 for r in selected:
  if r.get('decision')=='PASS' and r['context_id'] in runs[r['selected_source']]:
   rows[r['context_id']]=runs[r['selected_source']][r['context_id']]
 for folder in (ROOT/'data/monthly_updates').iterdir():
  for r in records(read(folder/'human_effective_results.json')):
   if r.get('manual_review'):rows[r['context_id']]=copy.deepcopy(r)
 return rows

def cg_rows():
 rows={}
 for name in ['claude_r2_results.json','gemini_r3_results.json']:
  for row in records(read(CG/name)):
   cid=row.get('context_id')
   if cid: rows[cid]={**rows.get(cid,{}),**copy.deepcopy(row)}
 for row in records(read(CG/'human_effective_results.json')):
  if row.get('manual_review'):rows[row['context_id']]=copy.deepcopy(row)
 return rows

def extraction_list(item):
 e=item.get('extraction')
 if not isinstance(e,dict):return []
 return [e]+[x for x in (e.get('additional_records') or item.get('additional_extractions') or []) if isinstance(x,dict)]

def effective(item):
 result=copy.deepcopy(item)
 result['poolability_original_status']=result.get('final_status') or result.get('full_text_status') or ('EXCLUDE' if result.get('eligibility')=='EXCLUDE' else 'R2_INCLUDE')
 result['poolability_policy_version']=policy.VERSION
 for e in extraction_list(result):
  ds.add_wald_derivations(e)
  if ROOT.name=='metaBreath':ds.apply_poolability_policy(e)
  else:policy.complete(e)
 e=result.get('extraction') or {}
 if result.get('eligibility')=='EXCLUDE' or result['poolability_original_status']=='EXCLUDE':
  result['final_status']='EXCLUDE';return dietary_policy.apply_record(result)
 # All clinical and explicit manual exclusions survive quantitative completion.
 blocked=bool(result.get('full_text_pooling_block'))
 if result.get('manual_review') and result.get('manual_poolable') is False and 'Existing JBI grade or required fields' not in str(result.get('poolability_reason') or ''):
  blocked=True
 if result['poolability_original_status']=='ELIGIBLE_SUBTYPE_REVIEW':blocked=True
 try:
  if ROOT.name.startswith('MetaCutis'):
   poolable=ds.is_poolable(e,result.get('jbi'),result.get('endpoint','Incidence'))
   issues=ds.missing_key_fields(e,result.get('endpoint','Incidence'))
  elif ROOT.name=='metaPee':
   poolable=ds.item_has_poolable_extraction(result,result.get('jbi'))
   issues=sorted(set(ds.missing_key_fields_for_item(result)+ds.invalid_key_fields_for_item(result)))
  else:
   poolable=ds.is_poolable(e,result.get('jbi'));issues=ds.required_quantitative_issues(e)
 except (ValueError,TypeError,OverflowError) as exc:
  poolable=False;issues=['invalid_saved_quantitative_bundle: '+str(exc)]
 result['final_status']='INCLUDE_META' if poolable and not blocked else 'ELIGIBLE_NOT_POOLABLE'
 result['missing_key_fields']=issues
 result['needs_full_text']=bool(issues or (result.get('jbi') or {}).get('grade')=='Fair')
 result['full_text_eligible']=bool(result['needs_full_text'] and result.get('pmcid'))
 if result.get('manual_review') and not blocked: result['manual_poolable']=bool(poolable)
 if hasattr(ds,'apply_whole_food_subtype_disposition'):
  result=ds.apply_whole_food_subtype_disposition(result)
 return dietary_policy.apply_record(result)

def cache_paths(exposure,disease,endpoint):
 if hasattr(monthly,'cache_paths_for_context'):return monthly.cache_paths_for_context(exposure,disease,endpoint)
 if hasattr(monthly,'_canonical_cache_paths'):return monthly._canonical_cache_paths(exposure,disease)
 slug=lambda x:re.sub('[^a-zA-Z0-9]+','_',x.lower()).strip('_')
 return [ROOT/'Cached_results'/slug(exposure)/f'{slug(disease)}_{slug(endpoint)}_true_{s}.json' for s in ['all','core']]

def materialize(rows,track):
 from scripts.display_eligibility import eligible_fair
 groups=defaultdict(list)
 for r in rows.values():
  key=(r.get('exposure'),r.get('cancer') or r.get('disease'),r.get('endpoint') or r.get('outcome') or 'Incidence')
  if all(key):groups[key].append(r)
 counters=Counter();errors=[]
 for (exposure,disease,endpoint),items in groups.items():
  paths=cache_paths(exposure,disease,endpoint)
  cache=read(paths[0]) if track=='chatgpt' else {}
  affected={str(r.get('pmid')) for r in items}
  retained=[r for r in cache.get('studies',[]) if str(r.get('PMID')) not in affected]
  added=[]
  for item in items:
   if item.get('final_status')=='EXCLUDE':continue
   poolable=item.get('final_status')=='INCLUDE_META'
   # Retain previously visible human records even while quality still prevents pooling.
   if not poolable and not item.get('manual_review') and not eligible_fair(item):continue
   raw_records=extraction_list(item) or ([{}] if eligible_fair(item) else [])
   chosen=policy.prefer_adjusted(copy.deepcopy(raw_records))
   primary_assigned = False
   for index,e in enumerate(chosen):
    e=copy.deepcopy(e);e.pop('additional_records',None)
    if not policy.number(e.get('effect_size')) and not eligible_fair(item):continue
    if ROOT.name.startswith('MetaCutis'):
     record_poolable=ds.is_poolable(e,item.get('jbi'),endpoint)
    else:
     record_poolable=ds.is_poolable(e,item.get('jbi'))
    if poolable and not record_poolable and not eligible_fair(item):continue
    sub={**item,'extraction':e,'is_primary_estimate':not primary_assigned}
    primary_assigned = True
    if ROOT.name=='metaBreath' and item.get('manual_review') and item.get('pmid')=='19369370':sub['is_primary_estimate']=True
    try:row=monthly.study_row(sub)
    except (RuntimeError,ValueError,TypeError,KeyError,ZeroDivisionError) as exc:
     errors.append({'track':track,'context_id':item['context_id'],'error':str(exc)});continue
    for k in ['manual_review','manual_poolable','shared_outcome_estimate_id','broader_outcome_projection']:
     if k in item:row[k]=item[k]
    row['policy_poolable']=bool(poolable and record_poolable);row['decision_origin']=item.get('decision_origin') or ('human' if item.get('manual_review') else track)
    row['reported_outcome']=e.get('reported_outcome',disease);row['sample_size_scope']=e.get('sample_size_scope')
    row['context_id']=item['context_id'];row['policy_track']=track
    added.append(row)
  if not added and not cache.get('studies'):continue
  studies=retained+added
  # Avoid duplicate direct populations already projected in a context.
  seen=set();dedup=[]
  for r in studies:
   key=(str(r.get('PMID')),str(r.get('Record ID') or ''),str(r.get('Gender') or ''),str(r.get('Effect Size')),str(r.get('comparison_type')))
   if key not in seen:dedup.append(r);seen.add(key)
  frame=pd.DataFrame(dedup)
  pool=frame.copy()
  if not pool.empty:
   for column in ['policy_poolable','manual_poolable','Primary Estimate']:
    if column in pool:pool=pool[pool[column].fillna(True).astype(bool)]
   if 'Quality Score' in pool:pool=pool[pool['Quality Score'].astype(str).isin(['Moderate','Good'])]
  analysis_result = None
  for path in paths:
   if track!='chatgpt':path=ROOT/'ClaudeGemini_results'/path.relative_to(ROOT/'Cached_results')
   else:
    backup=AUDIT/'before'/path.relative_to(ROOT)
    if path.exists() and not backup.exists():backup.parent.mkdir(parents=True,exist_ok=True);backup.write_bytes(path.read_bytes())
   output=copy.deepcopy(cache)
   result=analysis_result or {}
   if not pool.empty and analysis_result is None:
    try:
     result=meta_analysis.perform_meta_analysis(pool,disease,exposure,outcome=endpoint,exclude_meta=True,df_all=frame,generate_plots=track=='chatgpt')
     analysis_result=result
    except (ValueError,TypeError,KeyError) as exc: errors.append({'track':track,'scope':[exposure,disease,endpoint],'error':str(exc)})
   for k in ['summary_html','headline','screening_stats']:
    output[k]=result.get(k)
   # Clear old figures until current summary rebuild; no stale effect plot may be shown.
   output.update(studies=dedup,exposure=exposure,cancer=disease,disease=disease,endpoint=endpoint,outcome=endpoint,canonical_track=track,plot_url=result.get('plot_url'),funnel_plot_url=result.get('funnel_plot_url'),baujat_plot_url=result.get('baujat_plot_url'),poolability_projection={'policy_version':policy.VERSION,'raw_model_packets_unchanged':True,'new_gateway_calls':0})
   if not result.get('success'):output['summary_html']='<p>No poolable estimates in this scope under the unchanged JBI and clinical eligibility rules.</p>'
   write(path,output);counters['cache_files']+=1
  counters['displayed_rows']+=len(dedup);counters['pooling_rows']+=len(pool)
 return dict(counters),errors

def main():
 out={};all_errors=[]
 for track,base in [('chatgpt',chatgpt()),('claude_gemini',cg_rows())]:
  rows={cid:effective(r) for cid,r in base.items()}
  counts=Counter(r['final_status'] for r in rows.values());trans=Counter(r['poolability_original_status']+' -> '+r['final_status'] for r in rows.values())
  packet={'policy_version':policy.VERSION,'raw_model_packets_unchanged':True,'new_gateway_calls':0,'statuses':dict(counts),'transitions':dict(trans),'results':list(rows.values())}
  write(AUDIT/f'{track}_effective_results.json',packet)
  if track=='claude_gemini':write(CG/'poolability_effective_results.json',packet)
  caches,errors=materialize(rows,track);all_errors.extend(errors)
  out[track]={'statuses':dict(counts),'transitions':dict(trans),**caches,'projection_errors':len(errors)}
  print(json.dumps({track:out[track]}),flush=True)
 # Record raw hashes for exact verification; generated caches are omitted.
 raw={k:v for k,v in SOURCES.items() if k.startswith('data/')}
 changed=[k for k,v in raw.items() if hashlib.sha256((ROOT/k).read_bytes()).hexdigest()!=v]
 if changed:raise RuntimeError('Original source changed: '+str(changed))
 summary={'policy_version':policy.VERSION,'new_gateway_calls':0,'raw_sources_sha256':raw,'source_packets_verified_unchanged':True,'tracks':out,'projection_errors':all_errors}
 from scripts import build_summary_plots
 summary['summary_scopes']=len(build_summary_plots.build(ROOT/'Cached_results',ROOT/'Plot')['scopes'])
 summary['summary_plots_status']='complete'
 write(AUDIT/'application_summary.json',summary)
 print(json.dumps({'complete':True,'projection_errors':all_errors}),flush=True)
if __name__=='__main__':main()
