#!/usr/bin/env python3
"""Read saved screening receipts only; never import runners or call services."""
from pathlib import Path
import json,hashlib,re,collections,datetime
ROOT=Path(__file__).resolve().parents[2];OUT=Path(__file__).resolve().parent
REPOS=['MetaVir','metaThyroid','metaGut','metaFemina','metaBreath','metaPee','metaNeuro','MetaCutis_et_ Capillus']
EXCLUDE=['/before','/originals/','/lineage_recovery/','/generation_repair_snapshots/','/scope_repair_snapshots/','superseded','_archives/','/full_ncbi_rescreen/','_partial_','/manual_reviews/','/cohort_registry_audit/']
def excluded(p):return any(x in p for x in EXCLUDE) or any(x in Path(p).name for x in ['.before.','.pre_','.complete_with_unresolved'])
def load(p):
 try:return json.loads((ROOT/p).read_text())
 except Exception as e:return {'_inventory_parse_error':str(e)}
def rows(obj):
 if isinstance(obj,list):
  for r in obj:
   if isinstance(r,dict):yield r
 elif isinstance(obj,dict):
  for k in ['results','contexts','records']:
   if isinstance(obj.get(k),(list,dict)):
    z=obj[k]
    if isinstance(z,dict):
     for cid,r in z.items():
      if isinstance(r,dict):yield dict(r,context_id=r.get('context_id',cid))
    else:
     for r in z:
      if isinstance(r,dict):yield r
    return
  if obj and all(isinstance(v,dict) for v in obj.values()):
   for cid,r in obj.items():yield dict(r,context_id=r.get('context_id',cid))
def pmid_of(r):
 x=str(r.get('pmid') or r.get('PMID') or '').strip()
 if x.isdigit():return x
 s=str(r.get('context_id') or '')
 return s.split('|')[0] if re.match(r'^\d+\|',s) else ''
def recurse(obj,path=''):
 if isinstance(obj,dict):
  if obj.get('context_ids') and obj.get('pmcid'):
   for cid in obj['context_ids']:
    if isinstance(cid,str):yield path+'/context_ids',dict(obj,context_id=cid,pmid=cid.split('|')[0] if re.match(r'^\d+\|',cid) else obj.get('pmid'))
  elif obj.get('context_id') and (pmid_of(obj) or obj.get('pmcid')):yield path,obj
  else:
   for k,v in obj.items():yield from recurse(v,path+'/'+str(k))
 elif isinstance(obj,list):
  for i,v in enumerate(obj):yield from recurse(v,path+'/'+str(i))
def scientific_complete(r):
 source=str(r.get('pmc_source_status') or r.get('source_status') or '').lower()
 if source in {'no_pmcid','partial_record','retrieval_failed','unavailable','source_unavailable','missing_pmcid'}:return False
 if r.get('third_round_status')=='COMPLETED':return True
 return source in {'full_text','verified','verified_full_text'} and str(r.get('full_text_status','')).upper() in {'COMPLETED','COMPLETE','INCLUDE_META','ELIGIBLE_NOT_POOLABLE','EXCLUDE'}
allpaths=set()
for name in ['receipt_paths.txt','screening_packet_paths.txt','other_schema_paths.txt']:
 if (OUT/name).exists():allpaths.update((OUT/name).read_text().splitlines())
allowed={'r3_deferrals.json','full_text_source_unavailable_deferrals.json','r3_status.json','r3_source_deferrals.json','r3_terminal_source_deferrals.json','luna_r3_source_availability.json','v2_luna_r3_source_availability.json','luna_r3_unresolved_units.json','v2_luna_r3_unresolved_units.json','second_screen.json','chatgpt_second_screen.json','full_text_review.json','chatgpt_full_text_review.json','gemini_r3_results.json','gemini_r3_progress.json','pmc_source_status.json','run_status.json','run_state.json','status.json','validation_status.json','validation-r3.json','r3.json','recovery_status.json','luna_r3_status.json','v2_luna_r3_status.json','luna_r3_baseline.json'}
allpaths={p for p in allpaths if p.split('/')[0] in REPOS and not excluded(p) and Path(p).name in allowed}
files={p:load(p) for p in sorted(allpaths)};receipts=[];candidates=[];completed=collections.defaultdict(list);r2index=collections.defaultdict(list)
for p,d in files.items():
 b=(ROOT/p).read_bytes();receipts.append({'path':p,'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b),'mtime_ns':(ROOT/p).stat().st_mtime_ns,'schema_keys':list(d) if isinstance(d,dict) else None,'status_fields':{k:d[k] for k in ['state','status','stage_status','completion_status','current_stage','updated_at','created_at','recorded_at','stopped_reason'] if isinstance(d,dict) and k in d},'row_count':sum(1 for _ in rows(d))})
 repo=p.split('/')[0];name=Path(p).name
 if name in {'full_text_review.json','chatgpt_full_text_review.json','gemini_r3_results.json'}:
  for r in rows(d):
   cid=str(r.get('context_id') or '')
   if cid and (scientific_complete(r) or (name=='gemini_r3_results.json' and (r.get('eligibility') or r.get('decision')))):
    completed[(repo,cid)].append({'path':p,'pmid':pmid_of(r),'third_round_status':r.get('third_round_status'),'eligibility':r.get('eligibility'),'decision':r.get('decision')})
 if name in {'second_screen.json','chatgpt_second_screen.json'}:
  for r in rows(d):
   cid=str(r.get('context_id') or '')
   if cid:r2index[(repo,cid)].append((p,r))
 if 'source_deferral' in name or name in {'r3_deferrals.json','r3_status.json','full_text_source_unavailable_deferrals.json'}:
  for ptr,r in recurse(d):
   if name=='r3_status.json' and 'source_retrieval_unresolved' not in ptr:continue
   if r.get('stage') and 'r2' in str(r.get('stage')).lower():continue
   pmid=pmid_of(r)
   if not pmid:continue
   candidates.append({'repository':repo,'context_id':str(r.get('context_id')),'pmid':pmid,'pmcid':r.get('pmcid') or '', 'exposure':r.get('exposure'),'disease':r.get('disease') or r.get('cancer') or r.get('scope_id'),'outcome':r.get('outcome') or r.get('endpoint'),'source_failure_class':'missing_pmcid' if not r.get('pmcid') else 'unusable_pmc','gate_evidence':'explicit_native_R3_source_deferral','gate_reasons':r.get('gate_reasons'),'scientific_disposition_created':r.get('scientific_disposition_created'),'evidence':[{'path':p,'json_pointer':ptr,'saved_record':r}]})
 if name in {'full_text_review.json','chatgpt_full_text_review.json'}:
  for r in rows(d):
   source=str(r.get('pmc_source_status') or '').lower()
   if source not in {'partial_record','partial_front_matter','retrieval_failed','source_unavailable','unavailable','no_pmcid'}:continue
   if r.get('needs_full_text') is not True:continue
   pmid=pmid_of(r);cid=str(r.get('context_id') or '')
   if not pmid or not cid:continue
   candidates.append({'repository':repo,'context_id':cid,'pmid':pmid,'pmcid':r.get('pmcid') or '', 'exposure':r.get('exposure'),'disease':r.get('disease') or r.get('cancer') or r.get('scope_id'),'outcome':r.get('outcome') or r.get('endpoint'),'source_failure_class':'missing_pmcid' if not r.get('pmcid') else 'unusable_pmc','gate_evidence':'native_full_text_packet_source_failure_without_model_decision','gate_reasons':r.get('third_round_reasons') or r.get('missing_quantitative_fields') or r.get('missing_key_fields'),'title':r.get('title'),'evidence':[{'path':p,'json_pointer':'/results/context_id='+cid,'saved_record':{k:r.get(k) for k in ['context_id','pmid','pmcid','title','needs_full_text','full_text_eligible','full_text_status','pmc_source_status','reason','pmc_error']}}]})
 if 'r3_source_availability' in name:
  for pmid,r in d.get('records',{}).items():
   if r.get('status')!='unavailable':continue
   for cid in r.get('context_ids',[]):
    candidates.append({'repository':repo,'context_id':cid,'pmid':str(r.get('pmid') or pmid),'pmcid':r.get('pmcid') or '', 'exposure':r.get('exposures'),'disease':r.get('outcomes'),'outcome':r.get('outcomes'),'source_failure_class':'unusable_pmc','gate_evidence':'native_R3_source_preflight_unavailable','gate_reasons':None,'evidence':[{'path':p,'json_pointer':'/records/'+pmid,'saved_record':r}]})
for (repo,cid),items in r2index.items():
 for p,r in items:
  if r.get('needs_full_text') is not True or r.get('pmcid') or str(r.get('eligibility','')).upper()=='EXCLUDE':continue
  pmid=pmid_of(r)
  if not pmid:continue
  candidates.append({'repository':repo,'context_id':cid,'pmid':pmid,'pmcid':'','exposure':r.get('exposure'),'disease':r.get('disease') or r.get('cancer') or r.get('scope_id'),'outcome':r.get('outcome') or r.get('endpoint'),'source_failure_class':'missing_pmcid','gate_evidence':'native_saved_R2_needs_full_text_true','gate_reasons':r.get('third_round_reasons') or r.get('post_full_text_gate_reasons') or r.get('missing_quantitative_fields') or r.get('missing_key_fields'),'title':r.get('title'),'evidence':[{'path':p,'json_pointer':'/results/context_id='+cid,'saved_record':{k:r.get(k) for k in ['context_id','pmid','pmcid','title','exposure','cancer','eligibility','reason','needs_full_text','full_text_eligible','third_round_reasons','missing_quantitative_fields','missing_key_fields']}}]})
merged={}
for r in candidates:
 key=(r['repository'],r['context_id'])
 if key not in merged:merged[key]=r
 else:
  merged[key]['evidence']+=r['evidence']
  if not merged[key].get('title') and r.get('title'):merged[key]['title']=r['title']
for key,r in merged.items():
 r['completed_R3_receipts']=completed.get(key,[])
 r['has_completion_overlap']=bool(r['completed_R3_receipts'])
 r['inventory_status']='provisional_R3_source_blocked'
 r['dispatch_authorized']=False
 r['screening_gate_note']='Native saved R3 gate only; not inferred from nonpoolable or singleton status. Provisional until later/manual lineage and campaign activity checked.'
outrows=sorted(merged.values(),key=lambda r:(r['repository'],r['pmid'],r['context_id']))
(OUT/'native_source_blocked_contexts_provisional.json').write_text(json.dumps(outrows,indent=2,ensure_ascii=False)+'\n')
pmids=sorted({r['pmid'] for r in outrows if r['inventory_status']=='provisional_R3_source_blocked'},key=int);(OUT/'native_source_blocked_pmids_provisional.json').write_text(json.dumps(pmids,indent=2)+'\n')
(OUT/'screening_artifact_preservation_baseline.json').write_text(json.dumps({'generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'files':receipts,'model_calls':0,'network_requests':0},indent=2,ensure_ascii=False)+'\n')
summary=[]
for repo in REPOS:
 a=[r for r in outrows if r['repository']==repo];pending=[r for r in a if r['inventory_status']=='provisional_R3_source_blocked']
 summary.append({'repository':repo,'native_source_blocked_contexts':len(a),'provisional_uncompleted_contexts':len(pending),'provisional_unique_pmids':len({r['pmid'] for r in pending}),'with_saved_R3_completion_overlap':sum(r['has_completion_overlap'] for r in a),'failure_classes':dict(collections.Counter(r['source_failure_class'] for r in pending))})
(OUT/'native_inventory_summary_provisional.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps({'repositories':summary,'unique_pmids_global':len(pmids),'screening_artifacts_hashed':len(receipts)},indent=2))
