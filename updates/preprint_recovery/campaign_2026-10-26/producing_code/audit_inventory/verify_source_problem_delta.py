#!/usr/bin/env python3
from pathlib import Path
import json,hashlib,re,collections
ROOT=Path(__file__).resolve().parents[2];OUT=Path(__file__).resolve().parent
refs=json.loads((OUT/'additional_explicit_source_problem_contexts.json').read_text());base=json.loads((OUT/'native_source_blocked_contexts_guarded.json').read_text());old=json.loads((OUT/'native_source_blocked_context_inventory.json').read_text());oldkeys={(r['repository'],r['context_id']) for r in old}
p='metaGut/data/poolability_policy/2026-09-05/claude_gemini_effective_results.json';d=json.loads((ROOT/p).read_text());effective={r['context_id']:r for r in d['results']};bindings={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest()};rows=[]
for cid in sorted({r['context_id'] for r in refs if (r['repository'],r['context_id']) not in oldkeys}):
 es=[r for r in refs if r['context_id']==cid];x=effective.get(cid);reason=None
 if x:
  if str(x.get('eligibility')).upper()=='EXCLUDE':reason='later_saved_scientific_exclusion'
  elif x.get('r3_not_required_after_policy') is True:reason='explicit_native_policy_R3_not_required'
  elif x.get('third_round_status')=='COMPLETED' and x.get('pmc_source_status')=='verified_full_text':reason='later_saved_verified_R3_completed'
 for e in es:bindings[e['source_path']]=hashlib.sha256((ROOT/e['source_path']).read_bytes()).hexdigest()
 bits=cid.split('|');pprint=re.search(r'PMC\d+',es[0]['record'].get('error',''))
 rows.append({'repository':'metaGut','context_id':cid,'pmid':bits[0],'pmcid':pprint.group(0) if pprint else None,'exposure':bits[1],'disease':bits[2],'outcome':'Incidence','outstanding_tracks':[] if reason else ['Claude+Gemini'],'gate_evidence':'explicit_native_R3_source_verification_failure','source_failure_class':'unusable_pmc','evidence':es,'current_effective_guard':{'path':p,'source_sha256':bindings[p],'saved_fields':{k:x.get(k) for k in ['context_id','eligibility','needs_full_text','third_round_status','pmc_source_status','r3_not_required_after_policy','effective_r3_required','final_status','policy_source']} if x else None},'recovery_status':'already_settled_or_R3_not_required_saved_lineage' if reason else 'native_R3_source_blocked','excluded_reason':reason,'native_dispatch_eligible':False,'dispatch_authorized':False,'dispatch_note':'Fresh source/exact native R2 guard required before models; this delta only authorizes public source lookup.'})
active=[r for r in rows if not r['excluded_reason']];pmids=sorted({r['pmid'] for r in base+active},key=int);new=sorted({r['pmid'] for r in active}-{r['pmid'] for r in base},key=int)
(OUT/'native_source_problem_delta_inventory.json').write_text(json.dumps({'contexts':rows,'bindings':bindings,'non_source_problem_units_excluded':47,'model_calls':0},indent=2)+'\n');(OUT/'native_source_blocked_pmids_complete.json').write_text(json.dumps(pmids,indent=2)+'\n');(OUT/'native_source_problem_extra_pmids.json').write_text(json.dumps(new,indent=2)+'\n')
summary={'source_failure_context_references':len(refs),'new_contexts_reviewed':len(rows),'new_remaining_source_blocked_contexts':len(active),'new_settled_or_not_required_contexts':len(rows)-len(active),'extra_global_pmids':new,'complete_native_lookup_union_pmids':len(pmids),'base_guarded_contexts':len(base),'complete_native_source_blocked_contexts':len(base)+len(active),'model_calls':0,'cost_usd':0,'note':'Primary guarded file is frozen for first model handoff; this append-only delta preserves that fingerprint and adds remaining source-failure records separately.'};(OUT/'source_problem_delta_report.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,indent=2))
if any(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=sha for p,sha in bindings.items()):raise RuntimeError('Source changed during delta audit')
