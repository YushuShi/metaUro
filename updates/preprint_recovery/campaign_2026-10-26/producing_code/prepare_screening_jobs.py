"""Join independently guarded native R3 contexts to verified, versioned bodies."""
import json,sys,copy,hashlib,collections
from pathlib import Path
BASE=Path(__file__).resolve().parents[1];ROOT=BASE.parent
sys.path.insert(0,str(BASE/'implementation'))
import native_protocol
import preprint_screening

def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def main():
 gate_path=BASE/'article_availability_gate/authorized_linked_article_gate.json'
 gate_report=read(gate_path)
 gate_records={r['pmid']:r for r in gate_report['records']}
 def bind_gate(job):
  pmid=job['source']['published_pmid'];record=gate_records[pmid]
  job['pmc_availability_gate']={'path':str(gate_path.resolve()),'sha256':sha(gate_path),'pmid':pmid,'record_sha256':preprint_screening.digest(record)}
  return job
 matched=read(BASE/'audit_inventory/matched_preprint_native_contexts.json')['contexts']
 handoff=read(BASE/'europepmc_research/versioned_body_handoff.json')['records']
 available={r['published_pmid']:r for r in handoff if r.get('selected_source') and gate_records.get(r['published_pmid'],{}).get('model_dispatch_authorized') is True}
 # Heart candidates are bound to the two source groups selected by its active
 # coverage ledger. Preserve explicit manual holds and curator decisions.
 heart=read(BASE/'heart_inventory/heart_source_blocked_contexts_provisional.json')
 hr=ROOT/'metaHeart';pointer=read(hr/'data/local_screening_active.json');active=hr/'data/local_screening_views'/pointer['view_id']
 holds=read(active/'interim_adjudications.json').get('manual_hold_context_ids',[])
 curator_text=(active/'curator_dedup_receipt.json').read_text()
 for candidate in heart:
  if candidate['pmid'] not in available:continue
  assert candidate['context_id'] not in holds
  # A publication-wide curator decision needs separate explicit review rather
  # than inference from the source's old R2 gate.
  assert candidate['pmid'] not in curator_text
  candidate=copy.deepcopy(candidate);candidate['native_dispatch_eligible']=True;candidate['human_protected']=False
  candidate['r2_records']=[{'track':e['track'],'source_path':e['path'],'source_sha256':e['sha256'],'saved_record':e['saved_record']} for e in candidate['evidence']]
  first=candidate['r2_records'][0]['saved_record'];candidate['published_metadata']={k:first.get(k) for k in ['pmid','pmcid','published_date','title','authors','journal','pubmed_url']}
  candidate.update(native_protocol.protocol('metaHeart',candidate['outcome'],candidate['disease']))
  extra=hr/'scripts/outcome_scope_policy.py'
  rule=native_protocol.literal(extra,'OUTCOME_SCOPE_INSTRUCTIONS')
  if rule:candidate['native_criteria']+='\n\n'+rule;candidate['native_criteria_sources'].append({'path':str(extra.relative_to(ROOT)),'sha256':sha(extra),'constant':'OUTCOME_SCOPE_INSTRUCTIONS'})
  candidate['native_criteria']+='\nFor this source substitution, return an independent complete preprint estimate bundle. Do not return null for source-supported values just because they were also in R2, and do not merge quantitative fields from the journal abstract. Applicable JBI questions must be complete; questions beyond that checklist may be NA.'
  matched.append(candidate)
 groups=collections.defaultdict(list);unavailable=[]
 for context in matched:
  if context['pmid'] not in available:unavailable.append({'repository':context['repository'],'context_id':context['context_id'],'pmid':context['pmid'],'reason':'No verified publicly retrievable complete preprint body'});continue
  assert context['native_dispatch_eligible'] and not context['human_protected']
  criteria_key=hashlib.sha256(context['native_criteria'].encode()).hexdigest()
  groups[(context['repository'],context['pmid'],criteria_key)].append(context)
 folder=BASE/'screening_jobs_guarded';folder.mkdir(exist_ok=True);jobs=[]
 for (repo,pmid,criteria_key),contexts in sorted(groups.items()):
  if pmid=='38321187' and repo=='metaGut':
   # Preserve exact pilot inputs and paid-response cache identities.
   pilot=bind_gate(read(BASE/'screening_jobs/metaGut_38321187_1.json'))
   path=folder/'metaGut_38321187_1.json';preprint_screening.save(path,pilot)
   preprint_screening.validate_source(pilot)
   jobs.append(str(path.relative_to(BASE)));continue
  link=available[pmid];body=link['selected_source'];research=BASE/'europepmc_research'
  assert link['reciprocal_link']
  assert body['method_heading_found'] and body['results_heading_found']
  assert body.get('expected_preprint_doi_found_in_text') or body.get('expected_preprint_doi_found_in_source_xml') or link.get('server_published_doi_matches_med')
  assert sha(research/body['raw_path'])==body['raw_sha256']
  source={k:link.get(k) for k in ['published_pmid','published_title','published_doi','published_authors','preprint_id','preprint_doi','preprint_title','preprint_authors','relation','reciprocal_link','license']}
  source.update(preprint_version=body['version'],preprint_posted_date=body['version_date'],text_path=str(research/body['text_path']),text_sha256=body['text_sha256'],raw_source_sha256=body['raw_sha256'],retrieval_url=body['url'],full_text_verified=True,source_receipt=str(research/body['receipt']),raw_path=str(research/body['raw_path']))
  for offset in range(0,len(contexts),8):
   subset=copy.deepcopy(contexts[offset:offset+8])
   for item in subset:item['dispatch_authorized']=True
   first=subset[0];job={'repository':repo,'source':source,'contexts':subset,'native_criteria':first['native_criteria'],'native_criteria_sources':first['native_criteria_sources'],'native_extraction_fields':first['native_extraction_fields'],'native_extraction_schema':first['native_extraction_schema']}
   job=bind_gate(job)
   path=folder/(repo.replace(' ','_')+'_'+pmid+'_'+criteria_key[:10]+'_'+str(offset//8+1)+'.json');path.write_text(json.dumps(job,ensure_ascii=False,indent=2)+'\n');preprint_screening.validate_source(job);jobs.append(str(path.relative_to(BASE)))
 summary={'jobs':jobs,'job_count':len(jobs),'native_contexts_with_fulltext':sum(len(read(BASE/p)['contexts']) for p in jobs),'native_matched_without_accessible_body':unavailable,'body_count':len(available),'model_dispatches_in_this_step':0,'native_inputs_unchanged':True}
 (BASE/'screening_jobs_guarded_manifest.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps({k:v for k,v in summary.items() if k not in ['jobs','native_matched_without_accessible_body']},indent=2))
if __name__=='__main__':main()
