"""Build an unapproved, version-aware handoff from immutable public receipts."""
from pathlib import Path
from urllib.parse import urlparse
from collections import Counter
import json,hashlib,re,datetime
from research_probe import ROOT
from preprint_retrieval_gate import authorize_preprint_retrieval,PreprintRetrievalDenied

def sha(b):return hashlib.sha256(b).hexdigest()
def read(p):return json.loads((ROOT/p).read_text())
def compact(value):return re.sub(r'\s+','',value.lower())

def main():
 c=read('catalogue/linked_candidates.json');f=read('fulltext_manifest.json');rx={r['preprint_id']:r for r in read('rxiv_metadata_manifest.json')};status=read('status_all_linked.body')
 status_by={(x['src'],x['ext_ID']):x for x in status['articlesWithStatusUpdate']};out=[]
 for row in c:
  ppr=read(row['ppr_record']);med=read(row['med_record']);r=rx.get(row['preprint_id']);versions=r.get('versions',[]) if r else []
  target=max(versions,key=lambda v:int(v['version'])) if versions else None
  bodies=[]
  for x in f:
   if x['preprint_id']!=row['preprint_id'] or not x.get('body_candidate'):continue
   text=(ROOT/x['text_path']).read_text();origin=x['origin'];version=origin.get('version',{}).get('version');version_date=origin.get('version',{}).get('date')
   match='official_server_api_version_url' if version else None
   if not version and versions:
    for v in versions:
     if '/early/'+v['date'].replace('-','/')+'/' in x['url']:version=v['version'];version_date=v['date'];match='europepmc_url_date_matches_official_server_version';break
   if not version and row.get('version_number'):
    n=row['version_number']
    if re.search(r'/v'+str(n)+r'\.pdf(?:[?]|$)',x['url']) or compact(row['preprint_doi']) in compact(text[:5000]):
     version=str(n);version_date=ppr.get('firstPublicationDate');match='explicit_ppr_version_and_body_doi'
   doi_found=compact(row['preprint_doi']) in compact(text)
   doi_found_xml_metadata=x['format']=='jats_xml' and compact(row['preprint_doi']) in compact((ROOT/x['raw_path']).read_text())
   license_lines=[line.strip() for line in text.splitlines() if re.search(r'(?:All rights reserved|No reuse allowed|CC[- ]BY|Creative Commons.*licen[sc]e|licen[sc]e.*Creative Commons)',line,re.I)]
   bodies.append({k:x.get(k) for k in ('raw_path','raw_sha256','receipt','url','final_url','format','bytes','text_path','text_sha256','text_characters','method_heading_found','results_heading_found')}|{'version':version,'version_date':version_date,'version_evidence':match,'expected_preprint_doi_found_in_text':doi_found,'expected_preprint_doi_found_in_source_xml':doi_found_xml_metadata,'body_license_evidence':list(dict.fromkeys(license_lines))[:4]})
  latest_version=str(target['version']) if target else str(row['version_number']) if row.get('version_number') else None
  eligible=[x for x in bodies if latest_version is not None and x['version']==latest_version and (x['expected_preprint_doi_found_in_text'] or x['expected_preprint_doi_found_in_source_xml'])]
  eligible.sort(key=lambda x:(x['format']!='jats_xml','latest.pdf' in x['url']))
  selected=eligible[0] if eligible else None
  try:source_gate=authorize_preprint_retrieval(row['published_pmid'])
  except PreprintRetrievalDenied as error:
   source_gate={'pmid':row['published_pmid'],'preprint_fetch_allowed':False,'reason':str(error)}
   selected=None
  server_journal_dois=sorted({str(v.get('published','')).lower() for v in versions if v.get('published') and v.get('published')!='NA'})
  out.append({**row,'published_doi':med.get('doi'),'published_title':med.get('title'),'published_authors':med.get('authorString'),'preprint_authors':ppr.get('authorString'),'preprint_first_publication_date':ppr.get('firstPublicationDate'),'preprint_first_index_date':ppr.get('firstIndexDate'),'epmc_versions':(ppr.get('versionList') or {}).get('version',[]),'official_server_versions':versions,'official_server_metadata_receipt':r.get('receipt') if r else None,'server_published_doi_matches_med':med.get('doi','').lower() in server_journal_dois if server_journal_dois else None,'status_update':status_by.get(('PPR',row['preprint_id'])),'target_version':latest_version,'selected_source':selected,'all_full_body_sources':bodies,'source_availability_gate':source_gate,'access_state':'blocked_published_PMC_available_or_unverified' if not source_gate['preprint_fetch_allowed'] else 'latest_version_body_recovered' if selected else 'body_recovered_version_unverified' if bodies else 'no_body_recovered','dispatch_authorized':False,'identity_gate':'Requires exact native blocked context/authority, sample/outcome/effect identity checks and adjudication; catalogue linkage is candidate evidence only.'})
 (ROOT/'versioned_body_handoff.json').write_text(json.dumps({'schema_version':1,'created_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'cutoff_utc_date':'2026-10-26','model_calls':0,'model_cost_usd':0,'dispatch_authorized':False,'records':out},indent=2,ensure_ascii=False)+'\n')
 print(json.dumps({'candidates':len(out),'states':dict(Counter(x['access_state'] for x in out)),'chosen':[{'pmid':x['published_pmid'],'ppr':x['preprint_id'],'version':x['target_version'],'source':x['selected_source']['raw_path'] if x['selected_source'] else None} for x in out]},indent=2))
if __name__=='__main__':main()
