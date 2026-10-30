"""Fetch only Europe PMC catalogue-linked preprint sources; never authorize models.
Raw public responses and receipts are immutable. No fuzzy article discovery.
"""
from pathlib import Path
from urllib.parse import urlparse,urljoin
from html.parser import HTMLParser
import json,hashlib,re,subprocess,sys,time
from xml.etree import ElementTree as ET
from research_probe import ROOT,capture
from preprint_retrieval_gate import authorize_preprint_retrieval,PreprintRetrievalDenied

class Anchors(HTMLParser):
 def __init__(self):super().__init__();self.urls=[];self.meta={}
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='a' and a.get('href'):self.urls.append(a['href'])
  if tag=='meta' and a.get('content'):
   self.meta.setdefault(a.get('name',a.get('property','')).lower(),[]).append(a['content'])

def digest(b):return hashlib.sha256(b).hexdigest()
def fetch(pmid,ppr,url,origin):
 authorization=authorize_preprint_retrieval(pmid)
 key=digest(url.encode());name=f'fulltext/{ppr}/{key}'
 (ROOT/'fulltext'/ppr).mkdir(parents=True,exist_ok=True)
 receipt,raw=capture(name,url,accept='application/pdf,text/html,application/xml;q=0.9,*/*;q=0.5')
 item={'published_pmid':pmid,'preprint_id':ppr,'origin':origin,'url':url,'final_url':receipt['final_url'],'status':receipt['status'],'receipt':name+'.receipt.json','raw_path':name+'.body','raw_sha256':digest(raw),'bytes':len(raw),'content_type':next((v for k,v in receipt['response_headers'].items() if k.lower()=='content-type'),None),'dispatch_authorized':False,'source_availability_gate':authorization}
 if receipt['status']!=200:return item
 if raw.startswith(b'%PDF-'):
  out=ROOT/(name+'.txt')
  if not out.exists():
   result=subprocess.run(['pdftotext','-layout',str(ROOT/(name+'.body')),str(out)],capture_output=True)
   if result.returncode:item['extract_error']=result.stderr.decode(errors='replace')[:500]
  if out.exists():
   text=out.read_text(errors='replace');item.update({'format':'pdf','text_path':str(out.relative_to(ROOT)),'text_sha256':digest(out.read_bytes()),'text_characters':len(text),'method_heading_found':bool(re.search(r'(?im)^\s*(?:[0-9. ]+)?(?:materials? (?:and|&) )?methods?\b',text)),'results_heading_found':bool(re.search(r'(?im)^\s*(?:[0-9. ]+)?results?\b',text))})
   item['body_candidate']=len(text)>8000 and item['method_heading_found'] and item['results_heading_found']
 elif (b'<article' in raw[:1000] or b'<article ' in raw[:3000]) and b'<body' in raw:
  try:
   root=ET.fromstring(raw)
   text='\n'.join(' '.join(' '.join(el.itertext()).split()) for el in root.iter() if el.tag.split('}')[-1] in ('article-title','title','p','table-wrap','fig','label'))
   out=ROOT/(name+'.txt');out.write_text(text+'\n')
   titles=[' '.join(el.itertext()) for el in root.iter() if el.tag.split('}')[-1]=='title']
   item.update({'format':'jats_xml','text_path':str(out.relative_to(ROOT)),'text_sha256':digest(out.read_bytes()),'text_characters':len(text),'method_heading_found':any(re.search(r'method',t,re.I) for t in titles),'results_heading_found':any(re.search(r'results?',t,re.I) for t in titles)})
   item['body_candidate']=len(text)>8000 and item['method_heading_found'] and item['results_heading_found']
   item['explicit_pdf_anchors']=[el.attrib.get('{http://www.w3.org/1999/xlink}href') for el in root.iter() if el.tag.split('}')[-1]=='self-uri' and el.attrib.get('content-type')=='pdf' and el.attrib.get('{http://www.w3.org/1999/xlink}href')]
  except ET.ParseError as error:item['parse_error']=str(error)
 else:
  html=raw.decode('utf-8',errors='replace');p=Anchors();p.feed(html);item['format']='html_or_other';item['html_title']=re.sub('<.*?>','',x.group(1)) if (x:=re.search(r'<title[^>]*>(.*?)</title>',html,re.S|re.I)) else None
  item['citation_metadata']={k:v for k,v in p.meta.items() if k.startswith(('citation_','dc.','dc:')) or 'license' in k}
  item['explicit_pdf_anchors']=list(dict.fromkeys(urljoin(receipt['final_url'],u) for u in p.urls if re.search(r'(?:\.pdf(?:[?#]|$)|/pdf(?:[/?#]|$)|download[^#]*pdf)',u,re.I)))
  item['license_anchors']=list(dict.fromkeys(urljoin(receipt['final_url'],u) for u in p.urls if 'creativecommons.org/' in u or '/license' in u))
 return item

def main():
 rows=json.loads((ROOT/'catalogue/linked_candidates.json').read_text());selected=sys.argv[1:]
 rows=[r for r in rows if not selected or r['preprint_id'] in selected or r['published_pmid'] in selected]
 manifest=ROOT/'fulltext_manifest.json';previous=json.loads(manifest.read_text()) if manifest.exists() else []
 seen={(r['preprint_id'],r['url']):r for r in previous}
 for row in sorted(rows,key=lambda r:(not any(u.get('site')=='Europe_PMC' and u.get('documentStyle')=='pdf' for u in r['full_text_urls']),r['preprint_id'])):
  try:authorize_preprint_retrieval(row['published_pmid'])
  except PreprintRetrievalDenied as error:
   print(json.dumps({'published_pmid':row['published_pmid'],'preprint_fetch_denied':str(error)}),flush=True);continue
  urls=sorted(row['full_text_urls'],key=lambda u:(u.get('documentStyle')!='pdf',u.get('site')!='Europe_PMC'))
  for link in urls:
   url=link['url'];key=(row['preprint_id'],url)
   if key in seen:continue
   item=fetch(row['published_pmid'],row['preprint_id'],url,{'type':'europe_pmc_core_fullTextUrlList','record':row['ppr_record'],'entry':link});seen[key]=item
   manifest.write_text(json.dumps(list(seen.values()),indent=2,ensure_ascii=False)+'\n')
   print(json.dumps({k:item.get(k) for k in ['published_pmid','preprint_id','status','format','bytes','text_characters','body_candidate','url','final_url','explicit_pdf_anchors']},ensure_ascii=False),flush=True)
 print(json.dumps({'fetched_urls':len(seen),'pdf_bodies':sum(r.get('body_candidate',False) for r in seen.values()),'dispatch_authorized':False,'model_calls':0}),flush=True)
if __name__=='__main__':main()
