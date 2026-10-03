"""Retract proven trend-imputed uncertainty from current effective screening layers.

Raw model responses, sealed stage packets, source snapshots and nested original
receipts remain unchanged. This processes only explicitly named current effective
layers, retaining old values and excerpts in retraction receipts and preimages.
"""
from __future__ import annotations
import argparse,copy,gzip,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.trend_ci_projection import retract_projection
from scripts.refresh_trend_ci_results import AUDIT,write,digest
FILES={
 'metaBreath':['combined/effective_results.json','combined/provisional_ui/effective_results.json','outputs/poolability_policy_2026-09-05/chatgpt_effective_results.json','outputs/poolability_policy_2026-09-05/claude_gemini_effective_results.json'],
 'metaGut':['combined/effective_results.json','combined/provisional_ui/effective_results.json','data/poolability_policy/2026-09-05/chatgpt_effective_results.json','data/poolability_policy/2026-09-05/claude_gemini_effective_results.json'],
 'MetaVir':['combined/final_screening.json','outputs/poolability_policy_2026-09-05/chatgpt_poolability.json','outputs/poolability_policy_2026-09-05/claude_gemini_poolability.json','outputs/fair_display_2026-09-05/chatgpt_poolability.json','outputs/fair_display_2026-09-05/claude_gemini_poolability.json']}

def extraction_nodes(row,path):
 ext=row.get('extraction')
 if isinstance(ext,dict):
  yield path+['extraction'],ext
  for i,child in enumerate(ext.get('additional_records') or []):
   if isinstance(child,dict):yield path+['extraction','additional_records',i],child
 for key in ('additional_extractions','additional_records'):
  for i,child in enumerate(row.get(key) or []):
   if isinstance(child,dict):yield path+[key,i],child
 for key in ('key_review_candidates','provisional_ui_candidates'):
  for i,child in enumerate(row.get(key) or []):
   if isinstance(child,dict):yield from extraction_nodes(child,path+[key,i])

def run(apply=False):
 previous=AUDIT/'effective_refresh.json'
 if apply and previous.exists():
  report=json.loads(previous.read_text())
  if report.get('applied') and all(digest((ROOT/f['path']).read_bytes())==f.get('after_sha256',f['before_sha256']) for f in report['files']):
   print({'reused_verified_refresh':True,'retractions':report['retractions']},flush=True)
   return report
 report={'applied':apply,'files':[],'network_calls':0,'changed_files':0,'retractions':0}
 for relative in FILES[ROOT.name]:
  path=ROOT/relative
  if not path.exists():continue
  raw=path.read_bytes();packet=json.loads(raw);output=copy.deepcopy(packet)
  rows=output if isinstance(output,list) else output.get('results',output.get('records',[]))
  prefix=[] if isinstance(output,list) else ['results' if 'results' in output else 'records']
  changes=[]
  for i,row in enumerate(rows):
   if not isinstance(row,dict):continue
   for location,ext in extraction_nodes(row,prefix+[i]):
    if retract_projection(ext,row):
     if 'estimate_bundle_digest' in ext and (ROOT/'scripts/screening_contract.py').exists():
      from scripts.screening_contract import estimate_bundle_digest
      ext['estimate_bundle_digest']=estimate_bundle_digest(ext)
     changes.append({'json_path':location,'context_id':row.get('context_id'),'pmid':row.get('pmid'),'exposure':row.get('exposure'),'disease':row.get('cancer',row.get('disease')),'receipt':copy.deepcopy(ext['p_value_ci_retraction'])})
   if any(c['json_path'][:len(prefix)+1]==prefix+[i] for c in changes):
    # Never reinterpret exclusion or manual clinical decisions. Only a prior
    # include-for-pooling disposition depends on the now-retracted uncertainty.
    if row.get('final_status')=='INCLUDE_META':
     # Additional independent, valid records can still make this report poolable.
     eligible=[e for loc,e in extraction_nodes(row,[]) if not any(k in loc for k in ('key_review_candidates','provisional_ui_candidates')) and not e.get('p_value_ci_retraction') and isinstance(e.get('ci_lower'),(int,float)) and isinstance(e.get('ci_upper'),(int,float)) and e['ci_lower']>0 and e['ci_upper']>e['ci_lower']]
     if not eligible:
      row['final_status']='ELIGIBLE_NOT_POOLABLE'
      if row.get('full_text_status')=='INCLUDE_META':row['full_text_status']='ELIGIBLE_NOT_POOLABLE'
      row['scientific_pooling_eligible']=False
      for field in ('poolable_after_policy','r3_not_required_after_policy'):
       if field in row:row[field]=False
      row['pooling_reason']='A p value for trend does not supply uncertainty for the selected contrast.'
    if 'policy_effective_result_digest' in row:
     row.pop('policy_effective_result_digest')
     row['policy_effective_result_digest']=hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()
  record={'path':relative,'before_sha256':digest(raw),'records_scanned':len(rows),'changes':changes}
  if changes:
   report['changed_files']+=1;report['retractions']+=len(changes)
   if apply:
    target=AUDIT/'effective_preimages'/(digest(relative.encode())+'.json.gz');target.parent.mkdir(parents=True,exist_ok=True)
    if not target.exists():target.write_bytes(gzip.compress(raw,mtime=0))
    record['preimage']=str(target.relative_to(ROOT));write(path,output);record['after_sha256']=digest(path.read_bytes())
  report['files'].append(record)
 write(AUDIT/('effective_refresh.json' if apply else 'effective_plan.json'),report)
 print({k:v for k,v in report.items() if k!='files'},flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');run(p.parse_args().apply)
