#!/usr/bin/env python3
"""Article-level PMC gate over fresh MED metadata and every saved source track.
No retrieval, model calls, native result edits or historical evidence deletion.
"""
from pathlib import Path
import json,hashlib,re,collections,datetime,os
ROOT=Path(os.environ.get('PREPRINT_WORKSPACE_ROOT',Path(__file__).resolve().parents[2]));WORK=Path(os.environ.get('PREPRINT_WORK_DIR',ROOT/'preprint_recovery_2026-10-26'));OUT=WORK/'article_availability_gate';EPMC=WORK/'europepmc_research'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def pmcid(v):
 s=str(v or '').strip();m=re.fullmatch(r'(PMC\d+)(?:\.\d+)?',s,re.I);return m.group(1).upper() if m else ''
def pmid(v):
 s=str(v or '').strip();return s if s.isdigit() else ''
def num(v):
 try:return float(v)
 except (ValueError,TypeError):return 0
index_path=EPMC/'catalogue/index.json';index_sha=sha(index_path);index=json.loads(index_path.read_text());summary=json.loads((EPMC/'catalogue/latest_summary.json').read_text());wanted=set()
for x in summary.get('inputs',[]):
 p=EPMC/x['snapshot'];wanted.update(str(z) for z in json.loads(p.read_text()))
med={};mapping=collections.defaultdict(set);errors=[]
for identifier in sorted(wanted,key=int):
 info=index.get('MED',{}).get(identifier)
 if not info or not info.get('record_path'):errors.append({'pmid':identifier,'error':'Fresh MED lookup missing or article not found','lookup_status':(info or {}).get('lookup_status')});continue
 p=EPMC/info['record_path'];actual=sha(p)
 if actual!=info['record_sha256']:errors.append({'pmid':identifier,'error':'Fresh MED checksum mismatch'});continue
 d=json.loads(p.read_text());rpath=EPMC/info['response_receipt'];receipt=json.loads(rpath.read_text());current=pmcid(d.get('pmcid'))
 if current:mapping[current].add(identifier)
 med[identifier]={'pmid':identifier,'current_pmcid':current or None,'title':d.get('title'),'current_metadata':{'source':'EuropePMC MED core','record_path':str(p.relative_to(WORK)),'record_sha256':actual,'response_receipt':str(rpath.relative_to(WORK)),'response_receipt_sha256':sha(rpath),'retrieved_at_utc':receipt.get('retrieved_at_utc'),'response_status':receipt.get('status'),'inPMC':d.get('inPMC'),'inEPMC':d.get('inEPMC'),'hasPDF':d.get('hasPDF'),'isOpenAccess':d.get('isOpenAccess')}}
positive=collections.defaultdict(list);file_audit=[];parse_errors=[]
SKIP={'extraction','additional_extractions','study','studies','supporting_text','raw_output','prompt','abstract','Abstract','title','Title','source_snapshot','record_snapshots','snapshots','record_snapshot','citation_metadata','metadata','body','full_text','text','content','headers','response_headers','request_headers','input_content','messages'}
def walk(node,pointer='',ipmc='',ipmid=''):
 if isinstance(node,dict):
  c=pmcid(node.get('pmcid') or node.get('PMCID') or node.get('source_identifier')) or ipmc
  p=pmid(node.get('pmid') or node.get('PMID')) or ipmid
  status=str(node.get('pmc_source_status') or node.get('status') or '').casefold()
  chars=num(node.get('full_text_characters_reviewed') or node.get('full_text_characters') or node.get('characters') or node.get('non_abstract_body_characters') or node.get('body_characters'))
  identity=any(node.get(k) is True for k in ['identity_verified','article_element_verified','direct_article_body_verified','body_element_verified','well_formed_jats','matched_article_identity_verified'])
  fmt=str(node.get('pmc_source_format') or node.get('source_format') or '').lower()
  source_hash=node.get('pmc_source_sha256') or node.get('text_sha256') or node.get('sha256') or node.get('pmc_source_text_sha256')
  verified=(status=='verified_full_text' and bool(source_hash)) or (status=='full_text' and bool(source_hash) and (identity or chars>=5000)) or (status=='verified' and bool(node.get('pmc_source_receipt_sha256')) and bool(node.get('pmc_source_text_sha256')) and chars>0)
  if fmt and not any(z in fmt for z in ['pmc','jats','xml','html','pdf']):verified=False
  # Actual complete short communications remain available even below an automated safety floor.
  if node.get('deferral_code')=='R3-PMC-GENUINE-SHORT-BELOW-SAFETY-FLOOR':verified=True
  if c and verified:
   inferred=False
   if not p and len(mapping.get(c,set()))==1:p=next(iter(mapping[c]));inferred=True
   if not p and node.get('context_ids'):
    ids={str(z).split('|')[0] for z in node['context_ids'] if re.match(r'^\d+\|',str(z))}
    if len(ids)==1:p=next(iter(ids));inferred=True
   if p in wanted:yield p,{'pmcid':c,'json_pointer':pointer,'source_status':status,'source_format':fmt or None,'text_characters':chars or None,'source_text_sha256':source_hash,'source_receipt_digest':node.get('pmc_source_receipt_digest') or node.get('receipt_digest') or node.get('pmc_source_receipt_sha256'),'identity_verified_flag':identity,'pmid_inferred_from_current_MED_or_exact_contexts':inferred,'complete_short_source':node.get('deferral_code')=='R3-PMC-GENUINE-SHORT-BELOW-SAFETY-FLOOR'}
  if c and status=='excluded_pdf_only_user_policy' and p in wanted:
   yield p,{'pmcid':c,'json_pointer':pointer,'source_status':status,'source_format':'official_PMC_article_owned_scanned_PDF','text_characters':None,'source_text_sha256':source_hash,'identity_verified_flag':True,'availability_note':'Original source-format policy identifies an exact-identity scanned complete article with its article-owned PDF. An extraction/source-format policy failure is not absence of article full text.'}
  for k,v in node.items():
   if k in SKIP:continue
   if isinstance(v,(dict,list)):
    child_c=pmcid(k) or c;child_p=pmid(k) or p
    yield from walk(v,pointer+'/'+str(k),child_c,child_p)
 elif isinstance(node,list):
  for i,v in enumerate(node):
   if isinstance(v,(dict,list)):yield from walk(v,pointer+'/'+str(i),ipmc,ipmid)
paths=(OUT/'pmc_receipt_corpus_paths.txt').read_text().splitlines()
for i,s in enumerate(paths):
 p=ROOT/s
 try:
  raw=p.read_bytes();d=json.loads(raw);digest=hashlib.sha256(raw).hexdigest();hits=list(walk(d))
 except Exception as e:parse_errors.append({'path':s,'error_type':type(e).__name__,'error':str(e)});continue
 file_audit.append({'path':s,'sha256':digest,'bytes':len(raw),'positive_witnesses':len(hits)})
 for identifier,w in hits:positive[identifier].append({'repository':s.split('/')[0],'path':s,'artifact_sha256':digest,**w})
 if (i+1)%1000==0:print(json.dumps({'files_scanned':i+1,'positive_articles':len(positive)}),flush=True)
# Deduplicate duplicate copied receipts without deleting their origin references.
for identifier,ws in list(positive.items()):
 merged={}
 for w in ws:
  k=(w['pmcid'],w.get('source_text_sha256'),w.get('source_status'),w.get('source_receipt_digest'))
  if k not in merged:merged[k]={k:v for k,v in w.items() if k not in {'path','artifact_sha256','json_pointer','repository'}};merged[k]['origins']=[]
  merged[k]['origins'].append({k:w.get(k) for k in ['repository','path','artifact_sha256','json_pointer']})
 positive[identifier]=list(merged.values())
rows={}
for identifier in sorted(wanted,key=int):
 r=med.get(identifier,{'pmid':identifier,'current_pmcid':None,'current_metadata':None});proofs=positive.get(identifier,[])
 if proofs:state='blocked_verified_full_PMC_available_any_track';eligible=False
 elif not r.get('current_metadata'):state='hold_fresh_MED_metadata_unverified';eligible=False
 elif r.get('current_pmcid'):state='hold_current_PMCID_requires_actual_source_availability_check';eligible=False
 else:state='eligible_current_PMCID_absent_no_saved_full_PMC_receipt';eligible=True
 rows[identifier]={**r,'article_availability_gate_version':'article-level-PMC-first-2026-10-26-v1','gate_state':state,'preprint_source_eligible':eligible,'verified_full_pmc_available_any_track':bool(proofs),'verified_full_pmc_receipts':proofs,'native_scientific_R3_gate_still_required':True,'dispatch_authorized':False,'preprint_fetch_authorized_by_this_report':False}
linked={x['published_pmid'] for x in json.loads((EPMC/'catalogue/linked_candidates.json').read_text())};linked_rows={p:rows[p] for p in sorted(linked,key=int)}
report={'schema_version':1,'generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'policy':'Fetch preprints only if article PMCID is absent or actual PMC full article text is unavailable. Any saved verified full PMC source in any relevant repository/track blocks the whole article; a model/schema/source-format failure does not prove text unavailable.','scope_pmids':len(wanted),'fresh_MED_records_verified':len(med),'source_artifacts_scanned':len(file_audit),'source_artifact_parse_errors':parse_errors,'MED_errors':errors,'gate_counts':dict(collections.Counter(x['gate_state'] for x in rows.values())),'linked18_gate_counts':dict(collections.Counter(x['gate_state'] for x in linked_rows.values())),'no_new_model_calls':True,'no_new_preprint_fetches':True,'metadata_catalogue_index_sha256':index_sha,'records':rows}
OUT.mkdir(exist_ok=True);(OUT/'article_preprint_source_gate.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n');(OUT/'linked18_article_preprint_source_gate.json').write_text(json.dumps({'schema_version':1,'records':linked_rows,'gate_counts':report['linked18_gate_counts']},indent=2,ensure_ascii=False)+'\n');(OUT/'PMC_source_receipt_corpus_audit.json').write_text(json.dumps({'files':file_audit,'parse_errors':parse_errors,'model_calls':0},indent=2)+'\n')
if sha(index_path)!=index_sha:raise RuntimeError('MED catalogue index changed during article-level gate audit')
print(json.dumps({k:v for k,v in report.items() if k not in {'records','source_artifact_parse_errors'}},indent=2))
