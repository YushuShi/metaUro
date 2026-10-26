"""Exact EuropePMC-linked DOI lookup via the documented public openRxiv API."""
import json,hashlib
from urllib.parse import quote
from research_probe import ROOT,capture
from preprint_retrieval_gate import authorize_preprint_retrieval,PreprintRetrievalDenied
rows=json.loads((ROOT/'catalogue/linked_candidates.json').read_text());out=[]
receipt,raw=capture('docs_rxiv_api','https://api.biorxiv.org/',accept='text/html')
for r in rows:
 record=json.loads((ROOT/r['ppr_record']).read_text());publisher=(record.get('bookOrReportDetails') or {}).get('publisher','').lower()
 if publisher not in ('biorxiv','medrxiv'):continue
 try:authorization=authorize_preprint_retrieval(r['published_pmid'])
 except PreprintRetrievalDenied as error:
  print(json.dumps({'published_pmid':r['published_pmid'],'preprint_fetch_denied':str(error)}),flush=True);continue
 doi=r['preprint_doi'];url='https://api.biorxiv.org/details/'+publisher+'/'+quote(doi,safe='/')+'/na/json';name='rxiv_metadata_'+r['preprint_id']
 receipt,raw=capture(name,url);item={'published_pmid':r['published_pmid'],'preprint_id':r['preprint_id'],'doi':doi,'status':receipt['status'],'receipt':name+'.receipt.json','raw_sha256':receipt['sha256'],'dispatch_authorized':False}
 if receipt['status']==200:
  data=json.loads(raw);valid=[x for x in data.get('collection',[]) if str(x.get('doi','')).lower()==doi.lower()]
  if len(valid)!=len(data.get('collection',[])):raise ValueError('Unexpected DOI returned by server metadata API')
  item['messages']=data.get('messages');item['versions']=valid
 out.append(item);(ROOT/'rxiv_metadata_manifest.json').write_text(json.dumps(out,indent=2,ensure_ascii=False)+'\n')
 print(json.dumps({'preprint_id':r['preprint_id'],'status':receipt['status'],'versions':[{k:x.get(k) for k in ('doi','version','date','license','jatsxml','published')} for x in item.get('versions',[])]},ensure_ascii=False),flush=True)
