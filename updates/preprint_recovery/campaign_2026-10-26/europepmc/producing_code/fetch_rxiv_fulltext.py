"""Fetch API-returned version-specific JATS URLs; exact DOI only, no title search."""
from fetch_linked_fulltext import ROOT,fetch
from preprint_retrieval_gate import authorize_preprint_retrieval,PreprintRetrievalDenied
import json
p=ROOT/'fulltext_manifest.json';rows=json.loads(p.read_text());seen={(r['preprint_id'],r['url']) for r in rows}
metadata=json.loads((ROOT/'rxiv_metadata_manifest.json').read_text())
for r in metadata:
 try:authorize_preprint_retrieval(r['published_pmid'])
 except PreprintRetrievalDenied as error:
  print(json.dumps({'published_pmid':r['published_pmid'],'preprint_fetch_denied':str(error)}),flush=True);continue
 versions=r.get('versions',[])
 if not versions:continue
 # Preserve every listed version; latest-first enables authoritative current-version body choice.
 for version in sorted(versions,key=lambda v:int(v['version']),reverse=True):
  url=version.get('jatsxml')
  if not url or (r['preprint_id'],url) in seen:continue
  item=fetch(r['published_pmid'],r['preprint_id'],url,{'type':'official_rxiv_exact_doi_version_api','metadata_receipt':r['receipt'],'metadata_sha256':r['raw_sha256'],'version':version});rows.append(item);seen.add((r['preprint_id'],url))
  p.write_text(json.dumps(rows,indent=2,ensure_ascii=False)+'\n')
  print(json.dumps({k:item.get(k) for k in ['published_pmid','preprint_id','status','format','bytes','text_characters','body_candidate','url']},ensure_ascii=False),flush=True)
