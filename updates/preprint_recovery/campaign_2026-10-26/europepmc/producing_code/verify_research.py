"""Offline integrity/coverage/version regression checks; no network or models."""
from pathlib import Path
import hashlib,json,collections,datetime
from research_probe import ROOT
sha=lambda b:hashlib.sha256(b).hexdigest()
errors=[];receipts=[p for p in ROOT.rglob('*.receipt.json') if ROOT/'public_export' not in p.parents];attempts=0;totalbytes=0;status=collections.Counter()
for p in receipts:
 r=json.loads(p.read_text());body=p.with_name(p.name.removesuffix('.receipt.json')+'.body');attempts+=len(r.get('attempts',[{}]));totalbytes+=r.get('bytes',len(body.read_bytes()) if body.exists() else 0);status[str(r.get('status'))]+=1
 if not body.exists() and r.get('body_file'):body=p.parent/r['body_file']
 if not body.exists() and p.name=='initial_linked_search.receipt.json':body=p.with_name('initial_linked_search.json')
 if not body.exists() or sha(body.read_bytes())!=r['sha256']:errors.append({'receipt':str(p.relative_to(ROOT)),'error':'raw SHA mismatch'})
index=json.loads((ROOT/'catalogue/index.json').read_text());summary=json.loads((ROOT/'catalogue/latest_summary.json').read_text());wanted=set()
for x in summary['inputs']:
 raw=(ROOT/x['snapshot']).read_bytes()
 if sha(raw)!=x['sha256']:errors.append({'input':x['path'],'error':'snapshot SHA mismatch'})
 current=Path(x['path']).read_bytes()
 if sha(current)!=x['sha256']:errors.append({'input':x['path'],'error':'upstream input changed since catalogue run'})
 wanted.update(map(str,json.loads(raw)))
for kind in ('MED','PPR'):
 for identity,item in index[kind].items():
  if item.get('dispatch_authorized') is not False:errors.append({'id':identity,'error':'dispatch flag not false'})
  if item.get('record_path'):
   raw=(ROOT/item['record_path']).read_bytes();r=json.loads(raw)
   if sha(raw)!=item['record_sha256'] or r.get('id')!=identity or r.get('source')!=kind:errors.append({'id':identity,'error':'record hash/identity mismatch'})
   body=json.loads((ROOT/item['response_receipt'].replace('.receipt.json','.body')).read_text());row=body['resultList']['result'][item['response_record_index']]
   if row!=r:errors.append({'id':identity,'error':'extracted record differs from raw response'})
for identity in wanted:
 if identity not in index['MED']:errors.append({'id':identity,'error':'missing catalogue coverage'})
 if index['MED'][identity].get('lookup_status')=='med_not_found':
  receipt=json.loads((ROOT/index['MED'][identity]['response_receipt']).read_text())
  if receipt['status']!=200:errors.append({'id':identity,'error':'failure wrongly classified as absent'})
handoff=json.loads((ROOT/'versioned_body_handoff.json').read_text())
for x in handoff['records']:
 if x['dispatch_authorized'] is not False:errors.append({'id':x['published_pmid'],'error':'handoff dispatch enabled'})
 s=x['selected_source']
 if s:
  if not s['method_heading_found'] or not s['results_heading_found']:errors.append({'id':x['published_pmid'],'error':'missing required full-body sections'})
  if sha((ROOT/s['raw_path']).read_bytes())!=s['raw_sha256'] or sha((ROOT/s['text_path']).read_bytes())!=s['text_sha256']:errors.append({'id':x['published_pmid'],'error':'chosen source hash mismatch'})
  if s['version']!=x['target_version']:errors.append({'id':x['published_pmid'],'error':'older body selected over newer version'})
  if not(s['expected_preprint_doi_found_in_text'] or s['expected_preprint_doi_found_in_source_xml']):errors.append({'id':x['published_pmid'],'error':'body DOI not verified'})
# Actual regression: source API says v4; Europe's unversioned record has only datedv1PDF.
v4=next(x for x in handoff['records'] if x['preprint_id']=='PPR1059020')
assert v4['target_version']=='4' and v4['selected_source']['version']=='4'
assert '/2026/06/25/' in v4['selected_source']['url']
assert any(s['version']=='1' for s in v4['all_full_body_sources'])
# Known positive and true no-indexed-link blocked-paper control.
pos=json.loads((ROOT/'positive_preprint.body').read_text())['resultList']['result'][0]
assert pos['id']=='PPR840839'
assert any(x.get('id')=='39325770' for x in pos['commentCorrectionList']['commentCorrection'])
neg=json.loads((ROOT/'historical_negative.body').read_text())['resultList']['result'][0]
assert neg['id']=='7977277' and not neg.get('commentCorrectionList')
eligibility=json.loads((ROOT/'catalogue/source_eligibility_overlay.json').read_text()) if (ROOT/'catalogue/source_eligibility_overlay.json').is_file() else {'records':[]}
allowed={x['published_pmid'] for x in eligibility['records'] if x['source_availability_gate']['preprint_fetch_allowed']}
result={'verified_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'errors':errors,'status':'PASS' if not errors else 'FAIL','receipt_count':len(receipts),'http_attempt_count':attempts,'retrieved_response_bytes':totalbytes,'final_response_statuses':dict(status),'requested_pmids':len(wanted),'lookup_states':dict(collections.Counter(index['MED'][i]['lookup_status'] for i in wanted)),'candidate_pairs':len(handoff['records']),'access_states':dict(collections.Counter(x['access_state'] for x in handoff['records'])),'chosen_source_total_bytes':sum(x['selected_source']['bytes'] for x in handoff['records'] if x['selected_source']),'model_calls':0,'model_cost_usd':0,'dispatch_authorized':False,'upstream_input_hashes_unchanged':not any('input' in x for x in errors),'application_result_mutations':0,'component_scope':'Public retrieval/research only; parent ledger owns campaign model costs','current_article_source_eligible':len(allowed),'current_source_eligible_with_selected_body':sum(x['published_pmid'] in allowed and bool(x.get('selected_source')) for x in handoff['records']),'historical_out_of_current_scope_catalogue_records':sorted(set(index['MED'])-wanted)}
history=ROOT/'verification_history';history.mkdir(exist_ok=True)
old=ROOT/'verification_report.json'
if old.is_file():(history/(sha(old.read_bytes())+'.json')).write_bytes(old.read_bytes())
(ROOT/'verification_report.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2));assert not errors
if __name__=='__main__':pass
