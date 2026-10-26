import json,hashlib,gc,collections
from pathlib import Path
root=Path(__file__).resolve().parents[2];repo=root/'metaHeart';out=Path(__file__).parent
coverage=json.loads((repo/'data/local_screening_views/diagnosis_endpoint_2026-09-19_02ca02cccd6c/screening_coverage.json').read_text());ledger=json.loads(Path(coverage['coverage_ledger']).read_text());found={};baseline={};stats=[]
def read(p):
 baseline[str(p.relative_to(root))]=hashlib.sha256(p.read_bytes()).hexdigest();return json.loads(p.read_text())
for subset in ledger['source_subsets']:
 base=Path(subset['directory'])
 for track in ['chatgpt','claude_gemini']:
  r3p=base/(track+'_full_text_review.json');r3=read(r3p);completed={r['context_id'] for r in r3['results'] if r.get('pmc_source_status')=='full_text' and r.get('pmc_source_sha256') and r.get('full_text_characters_reviewed',0)>0}
  r2p=base/(track+'_second_screen.json');r2=read(r2p);counts=collections.Counter()
  for row in r2['results']:
   if not row.get('needs_full_text'):continue
   cid=row['context_id'];counts['needs_R3']+=1
   if cid in completed:counts['R3_complete']+=1;continue
   if row.get('pmcid'):counts['pmcid_present_not_proven_source_failed']+=1;continue
   counts['missing_pmcid']+=1
   entry=found.setdefault(cid,{'repository':'metaHeart','context_id':cid,'pmid':str(row['pmid']),'pmcid':'','exposure':row['exposure'],'disease':row['cancer'],'outcome':row.get('outcome','Incidence'),'title':row['title'],'source_failure_class':'missing_pmcid','gate_evidence':'active_native_saved_R2_needs_full_text_true','gate_reasons':row.get('missing_key_fields') or [],'evidence':[],'dispatch_authorized':False,'inventory_status':'source_blocked_active_native_R2','screening_gate_note':'Not inferred from standalone/nonpoolable status; native R2 requests R3 and no PMCID is saved.'})
   entry['evidence'].append({'path':str(r2p.relative_to(root)),'track':track,'sha256':baseline[str(r2p.relative_to(root))],'saved_record':row})
  stats.append({'source_subset':str(base.relative_to(repo)),'track':track,**dict(counts)})
  del r2,r3;gc.collect()
rows=[found[k] for k in sorted(found)];pmids=sorted({r['pmid'] for r in rows},key=int)
(out/'heart_source_blocked_contexts_provisional.json').write_text(json.dumps(rows,indent=2)+'\n');(out/'heart_source_blocked_pmids_provisional.json').write_text(json.dumps(pmids,indent=2)+'\n');(out/'baseline.json').write_text(json.dumps(baseline,indent=2)+'\n');report={'contexts':len(rows),'unique_pmids':len(pmids),'track_scope':stats,'published_source_coverage_ledger':coverage['coverage_ledger'],'source_policy':'source-unavailable only; R1/R2/schema/model errors excluded','remaining_audit':'PMCID-present pending groups require affirmative source-unavailable evidence; do not assume missing model result means unavailable PMC.'};(out/'heart_inventory_summary.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
