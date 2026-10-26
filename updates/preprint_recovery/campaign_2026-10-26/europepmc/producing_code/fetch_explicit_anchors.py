"""Follow only actual article PDF/meta anchors or HTTPS upgrade of DOI redirect.
No guessed article identifiers; do not bypass authentication/challenges.
"""
import json
from urllib.parse import urlparse
from fetch_linked_fulltext import ROOT,fetch
from preprint_retrieval_gate import authorize_preprint_retrieval,PreprintRetrievalDenied
p=ROOT/'fulltext_manifest.json';rows=json.loads(p.read_text());seen={(r['preprint_id'],r['url']) for r in rows};queue=[]
for parent in list(rows):
 urls=[*parent.get('explicit_pdf_anchors',[]),*parent.get('citation_metadata',{}).get('citation_pdf_url',[])]
 for url in urls:
  host=(urlparse(url).hostname or '').lower()
  # Only servers already explicitly linked from a current candidate record.
  if host not in ('www.researchsquare.com','www.medrxiv.org','medrxiv.org','www.authorea.com','authorea.com','www.biorxiv.org','biorxiv.org','europepmc.org'):continue
  queue.append((parent,url,'explicit_pdf_anchor'))
 if parent['final_url'].startswith('http://medrxiv.org/'):
  queue.append((parent,'https://'+parent['final_url'][7:],'https_upgrade_of_doi_redirect'))
for parent,url,kind in queue:
 try:authorize_preprint_retrieval(parent['published_pmid'])
 except PreprintRetrievalDenied as error:
  print(json.dumps({'published_pmid':parent['published_pmid'],'preprint_fetch_denied':str(error)}),flush=True);continue
 key=(parent['preprint_id'],url)
 if key in seen:continue
 item=fetch(parent['published_pmid'],parent['preprint_id'],url,{'type':kind,'parent_receipt':parent['receipt'],'parent_raw_sha256':parent['raw_sha256']});rows.append(item);seen.add(key)
 p.write_text(json.dumps(rows,indent=2,ensure_ascii=False)+'\n')
 print(json.dumps({k:item.get(k) for k in ['published_pmid','preprint_id','status','format','bytes','text_characters','body_candidate','url','final_url','explicit_pdf_anchors']},ensure_ascii=False),flush=True)
print(json.dumps({'fetched_urls':len(rows),'pdf_bodies':sum(r.get('body_candidate',False) for r in rows),'dispatch_authorized':False,'model_calls':0}),flush=True)
