"""Scope-only R2/cache minimization; original saved/private artifacts stay intact."""
from pathlib import Path
import json,copy,re,hashlib,os
BASE=Path(os.environ.get('PREPRINT_WORK_DIR',Path(__file__).resolve().parents[1]))
def key(v):return re.sub(r'[^a-z0-9]+','_',str(v or '').lower()).strip('_')
def exact_target_rows(packet,ctx,reference):
 requested=(reference.split('#screening_context_id=',1)[1] if '#screening_context_id=' in reference else None)
 result=[]
 def matches(row):
  if str(row.get('pmid') or row.get('PMID') or '')!=str(ctx['pmid']):return False
  rid=row.get('screening_context_id') or row.get('Screening Context ID') or row.get('context_id')
  if requested and rid and str(rid)!=requested:return False
  exposure=row.get('exposure') or row.get('direct_exposure') or row.get('assigned_exposure_category') or row.get('Direct Exposure')
  outcome=row.get('outcome') or row.get('Outcome Key') or row.get('Outcome Class')
  if exposure and key(exposure)!=key(ctx['exposure']):return False
  if isinstance(outcome,str) and outcome and key(outcome)!=key(ctx['outcome']):return False
  return True
 for container in ['results','records','contexts','studies','article_audit']:
  values=packet.get(container)
  if isinstance(values,dict):values=[dict(r,context_id=r.get('context_id',cid)) for cid,r in values.items() if isinstance(r,dict)]
  if not isinstance(values,list):continue
  for index,row in enumerate(values):
   if isinstance(row,dict) and matches(row):result.append({'container':container,'row_index':index,'record':copy.deepcopy(row)})
 return result

def minimize_context(ctx):
 out=copy.deepcopy(ctx);stats=[]
 for r in out.get('r2_records',[]):
  if 'saved_cache_packet' not in r:continue
  old=r.pop('saved_cache_packet');selected=exact_target_rows(old,ctx,r.get('source_row_reference',''))
  if not selected:raise ValueError('No exact target record in saved source: '+ctx['context_id'])
  r['saved_target_records']=selected
  r['source_scope_selection']={'method':'Exact PMID plus native exposure/outcome and original screening_context_id when supplied','native_context_id':ctx['context_id'],'source_row_reference':r.get('source_row_reference'),'packet_metadata_keys':sorted(k for k,v in old.items() if not isinstance(v,(dict,list))),'target_record_count':len(selected),'unrelated_rows_removed_from_prompt_only':True,'original_saved_source_unchanged':True}
  stats.append({'source_path':r['source_path'],'source_sha256':r['source_sha256'],'original_packet_json_bytes':len(json.dumps(old)),'selected_target_json_bytes':len(json.dumps(selected)),'selected_target_records':len(selected)})
 out['prompt_minimization_receipt']=stats;return out

def main():
 source=BASE/'audit_inventory/matched_preprint_native_contexts.json';raw=source.read_bytes();d=json.loads(raw);wanted={('42349803','cognitive_decline'),('39544014','cognitive_decline'),('40762630','sleep_efficiency')};rows=[minimize_context(r) for r in d['contexts'] if (r['pmid'],r['outcome']) in wanted];out=BASE/'converter_inputs/neuro_minimized_context_overrides.json'
 result={'schema_version':1,'original_private_export_path':str(source),'original_private_export_sha256':hashlib.sha256(raw).hexdigest(),'scope':'Only exact target source records are provided to models. No source/R2/extraction values changed, no model calls.','contexts':rows,'overrides_by_context_id':{r['context_id']:{'r2_records':r['r2_records'],'prompt_minimization_receipt':r['prompt_minimization_receipt']} for r in rows},'model_calls':0,'new_cost_usd':0};out.parent.mkdir(exist_ok=True);out.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
 out.with_name('neuro_minimized_r2_overrides.json').write_bytes(out.read_bytes())
 if hashlib.sha256(source.read_bytes()).hexdigest()!=result['original_private_export_sha256']:raise RuntimeError('Private original export changed')
 print(json.dumps({'path':str(out),'contexts':[(r['context_id'],r['prompt_minimization_receipt']) for r in rows]},indent=2))
if __name__=='__main__':main()
