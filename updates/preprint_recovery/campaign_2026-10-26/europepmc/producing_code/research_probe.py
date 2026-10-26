"""Read-only public Europe PMC API prototype; no model calls or app writes."""
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode
import hashlib,json,time,email.utils

ROOT=Path(__file__).resolve().parent
BASE='https://www.ebi.ac.uk/europepmc/webservices/rest'
_LAST=0.0

def capture(name,url,body=None,accept='application/json'):
    global _LAST
    destination=ROOT/(name+'.body');receipt=ROOT/(name+'.receipt.json')
    if destination.exists() and receipt.exists():
        saved=json.loads(receipt.read_text());raw=destination.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=saved['sha256']:raise ValueError('Research receipt changed')
        return saved,raw
    attempts=[];final_url=url
    payload=None if body is None else json.dumps(body,separators=(',',':')).encode()
    headers={'User-Agent':'metaGut-preprint-link-research/1.0','Accept':accept}
    if payload is not None:headers['Content-Type']='application/json'
    for number in range(3):
        remaining=1-(time.monotonic()-_LAST)
        if remaining>0:time.sleep(remaining)
        _LAST=time.monotonic();started=time.time();raw=b''
        try:
            with urlopen(Request(url,data=payload,headers=headers,method='GET' if payload is None else 'POST'),timeout=30) as response:
                raw=response.read();status=response.status;response_headers=dict(response.headers);final_url=response.url
        except HTTPError as error:
            raw=error.read();status=error.code;response_headers=dict(error.headers);final_url=error.url
        except (URLError,TimeoutError) as error:
            status=None;response_headers={};final_url=url;attempts.append({'attempt':number+1,'error_type':type(error).__name__})
            if number<2:time.sleep(min(2**number,4));continue
        attempts.append({'attempt':number+1,'status':status,'elapsed_seconds':round(time.time()-started,3),'body_sha256':hashlib.sha256(raw).hexdigest()})
        if status not in (429,500,502,503,504) or number==2:break
        delay=response_headers.get('Retry-After','')
        try:delay=float(delay)
        except ValueError:
            try:delay=max(0,email.utils.parsedate_to_datetime(delay).timestamp()-time.time())
            except (ValueError,TypeError):delay=2**number
        time.sleep(min(max(delay,1),30))
    destination.write_bytes(raw)
    saved={'request_url':url,'final_url':final_url,'method':'GET' if payload is None else 'POST','request_body':body,
           'request_headers':headers,'status':status,'response_headers':response_headers,'attempts':attempts,
           'retrieved_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'body_file':destination.name}
    receipt.write_text(json.dumps(saved,indent=2)+'\n')
    return saved,raw

def search(name,query,page_size=10):
    receipt,raw=capture(name,BASE+'/search?'+urlencode({'query':query,'resultType':'core','format':'json','pageSize':page_size,'synonym':'false'}))
    if receipt['status']!=200:return receipt,{}
    data=json.loads(raw)
    if data.get('errCode') or 'resultList' not in data:raise ValueError('Europe PMC semantic error: '+str(data.get('errCode')))
    return receipt,data


def first_phase():
    docs=[('docs_developers','https://europepmc.org/developers'),('docs_rest','https://europepmc.org/RestfulWebService'),
          ('docs_searchsyntax','https://europepmc.org/searchsyntax'),('docs_copyright','https://europepmc.org/copyright'),
          ('docs_preprints','https://europepmc.org/Preprints'),('docs_preprint_faq','https://plus.europepmc.org/user-guide/preprintfaqs'),
          ('docs_api_spec',BASE+'/api-docs')]
    results=[]
    for name,url in docs:
        receipt,raw=capture(name,url,accept='text/html,application/json;q=0.9');results.append({'name':name,'url':url,'status':receipt['status'],'bytes':len(raw)})
    for name,query in [('positive_pmc_paper','PMCID:PMC11426508'),('historical_negative','EXT_ID:7977277 AND SRC:MED'),
                       ('dated_linked_sample','SRC:MED AND HAS_PREPRINT:Y AND FIRST_PDATE:[2020-01-01 TO 2025-12-31]')]:
        receipt,data=search(name,query,3)
        rows=data.get('resultList',{}).get('result',[])
        results.append({'name':name,'status':receipt['status'],'hitCount':data.get('hitCount'),
                        'records':[{k:r.get(k) for k in ('id','source','doi','title','commentCorrectionList','versionList','fullTextUrlList','isOpenAccess','inEPMC','hasPDF')} for r in rows]})
    (ROOT/'phase1_summary.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__':first_phase()
