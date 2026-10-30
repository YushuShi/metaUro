"""Export facts and receipts only; never copy whole-paper PDFs/XML/text or abstracts."""
from pathlib import Path
import json,hashlib,shutil
from research_probe import ROOT
from receipt_sanitizer import sanitize_receipt
out=ROOT/'public_export';out.mkdir(exist_ok=True)
sha=lambda b:hashlib.sha256(b).hexdigest()
def put(name,data):
 p=out/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')
idx=json.loads((ROOT/'catalogue/index.json').read_text());h=json.loads((ROOT/'versioned_body_handoff.json').read_text());summary=json.loads((ROOT/'catalogue/latest_summary.json').read_text());lookup={}
for k,v in idx['MED'].items():lookup[k]={f:v.get(f) for f in ('id','lookup_status','record_sha256','response_receipt','response_record_index','linked_preprints','dispatch_authorized')}
put('catalogue_lookup_facts.json',lookup)
eligibility=json.loads((ROOT/'catalogue/source_eligibility_overlay.json').read_text());eligibility_by_pmid={x['published_pmid']:x['source_availability_gate'] for x in eligibility['records']}
public=[]
for row in h['records']:
 keep={k:row.get(k) for k in ('published_pmid','published_doi','published_title','published_authors','preprint_id','preprint_doi','preprint_title','preprint_authors','relation','link_origin','reciprocal_link','preprint_first_publication_date','preprint_first_index_date','epmc_versions','target_version','license','server_published_doi_matches_med','status_update','access_state','source_availability_gate','dispatch_authorized')}
 keep['source_availability_gate']=eligibility_by_pmid.get(row['published_pmid'],{'preprint_fetch_allowed':False})
 s=row['selected_source']
 if s:
  # Exclude article text, paths that imply it is published, and extended quotation.
  keep['source']={k:s.get(k) for k in ('raw_sha256','text_sha256','receipt','url','final_url','format','bytes','version','version_date','version_evidence','expected_preprint_doi_found_in_text','expected_preprint_doi_found_in_source_xml')}
 server=[]
 for v in row.get('official_server_versions',[]):server.append({k:v.get(k) for k in ('doi','date','version','license','jatsxml','published','server','type')})
 keep['historical_source_only']=not keep['source_availability_gate']['preprint_fetch_allowed']
 keep['official_server_versions']=server;public.append(keep)
put('linked_preprint_source_facts.json',public)
for p in list(ROOT.rglob('*.receipt.json')):
 if out in p.parents:continue
 # Receipts contain URLs, HTTP status/headers, timestamps and SHA; no article/prompt body.
 target=out/'receipts'/p.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True);receipt=sanitize_receipt(json.loads(p.read_text()))
 target.write_text(json.dumps(receipt,indent=2)+'\n')
for name in ('research_probe.py','batch_lookup.py','fetch_linked_fulltext.py','fetch_explicit_anchors.py','fetch_rxiv_metadata.py','fetch_rxiv_fulltext.py','build_handoff.py','verify_research.py','export_public_provenance.py','preprint_retrieval_gate.py','test_preprint_retrieval_gate.py','receipt_sanitizer.py','test_receipt_sanitizer.py'):
 (out/'producing_code').mkdir(exist_ok=True);shutil.copyfile(ROOT/name,out/'producing_code'/name)
for name in ('research_contract.json','verification_report.json'):(out/name).write_bytes((ROOT/name).read_bytes())
put('lookup_scope_summary.json',summary)
put('source_eligibility_overlay.json',eligibility)
source_gate_root=ROOT.parent/'article_availability_gate'
for name in ('authorized_linked_article_gate.json','linked18_article_preprint_source_gate.json','article_preprint_source_gate.json','PMC_source_receipt_corpus_audit.json','pmc_receipt_corpus_paths.txt'):
 target=out/'source_gate'/name;target.parent.mkdir(exist_ok=True);target.write_bytes((source_gate_root/name).read_bytes())
(out/'source_gate'/'fresh_pmc_availability_report.json').write_bytes((ROOT.parent/'pmc_availability_gate/latest_report.json').read_bytes())
(out/'producing_code'/'audit_article_pmc_availability.py').write_bytes((source_gate_root/'audit_article_pmc_availability.py').read_bytes())
gate=json.loads((source_gate_root/'authorized_linked_article_gate.json').read_text())
bindings=[]
for binding in gate['source_reports']:
 source=Path(binding['path']);source=source if source.is_absolute() else ROOT.parents[1]/source
 if sha(source.read_bytes())!=binding['sha256']:raise ValueError('Source-availability report binding changed.')
 name='fresh_pmc_availability_report.json' if source.name=='latest_report.json' else source.name
 target=out/'source_gate'/name
 if sha(target.read_bytes())!=binding['sha256']:raise ValueError('Exported source-availability report differs from authoritative binding.')
 bindings.append({'original_path':binding['path'],'export_path':str(target.relative_to(out)),'sha256':binding['sha256']})
put('source_gate/portable_source_report_bindings.json',{'schema_version':1,'policy':'These exact report bytes reproduce the authorized source-report bindings; raw PMC and preprint article bodies are private and not exported. The source corpus inventory retains source paths, byte counts and SHA256 values only.','bindings':bindings})
paths=[p for p in out.rglob('*') if p.is_file() and p.name!='public_export_manifest.json']
put('public_export_manifest.json',{'policy':'facts_identifiers_urls_dates_versions_http_receipts_and_hashes_only_no_whole_papers_abstracts_or_model_prompts','private_source_root':'../fulltext','model_calls':0,'model_cost_usd':0,'files':{str(p.relative_to(out)):{'bytes':p.stat().st_size,'sha256':sha(p.read_bytes())} for p in sorted(paths)}})
print(json.dumps({'public_files':len(paths)+1,'public_bytes':sum(p.stat().st_size for p in paths),'private_fulltext_unchanged':True}))
