"""Receipt-backed public MED/PPR catalogue lookup; never authorizes model dispatch."""
from pathlib import Path
from urllib.parse import urlencode
import argparse,hashlib,json,time
from research_probe import capture,BASE,ROOT
from preprint_retrieval_gate import authorize_preprint_retrieval,PreprintRetrievalDenied

CAT=ROOT/'catalogue'

def digest(raw):return hashlib.sha256(raw).hexdigest()
def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.pending');temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');temporary.replace(path)
def records(data):return data.get('resultList',{}).get('result',[]) or []
def listify(value):return value if isinstance(value,list) else [] if value is None else [value]
def load_index():return json.loads((CAT/'index.json').read_text()) if (CAT/'index.json').exists() else {'schema_version':1,'MED':{},'PPR':{}}
def persist_record(source,row,receipt_name,position):
    raw=(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode();identity=digest(raw)
    path=CAT/'records'/source/str(row['id'])/(identity+'.json');path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():assert path.read_bytes()==raw
    else:path.write_bytes(raw)
    return {'id':str(row['id']),'source':source,'record_path':str(path.relative_to(ROOT)),'record_sha256':identity,
            'response_receipt':receipt_name+'.receipt.json','response_record_index':position}
def core_batch(source,ids,published_pmids=None):
    if source=="PPR":
        if not published_pmids:raise PreprintRetrievalDenied("An exact journal-PMID source gate is required before PPR lookup.")
        for pmid in published_pmids:authorize_preprint_retrieval(pmid)
    query=f'SRC:{source} AND EXT_ID:('+ ' OR '.join(ids)+')'
    assert len(query)<1450, len(query)
    request_identity=digest(json.dumps({'source':source,'ids':ids},sort_keys=True).encode())
    name='catalogue_'+source+'_'+request_identity
    url=BASE+'/search?'+urlencode({'query':query,'resultType':'core','format':'json','pageSize':100,'synonym':'false'})
    # A failed response remains auditable but must never become permanent cached absence.
    attempt=0; base_name=name
    while True:
        existing=ROOT/(name+'.receipt.json')
        if not existing.exists():break
        cached=json.loads(existing.read_text())
        if cached.get('status')==200:
            try:
                prior=json.loads((ROOT/(name+'.body')).read_text())
                if not prior.get('errCode') and 'resultList' in prior:break
            except (OSError,ValueError):pass
        attempt+=1;name=base_name+'_retry_'+str(attempt)
    receipt,raw=capture(name,url)
    if receipt['status']!=200:return name,receipt,None
    try:data=json.loads(raw)
    except ValueError:return name,receipt,None
    if data.get('errCode') or 'resultList' not in data:return name,receipt,None
    if data.get('hitCount',0)>100:raise RuntimeError('Identifier batch unexpectedly requires pagination')
    return name,receipt,data

def run(paths):
    CAT.mkdir(parents=True,exist_ok=True);index=load_index();wanted=set();snapshots=[]
    for path in paths:
        path=Path(path);raw=path.read_bytes();value=json.loads(raw);pmids=value if isinstance(value,list) else value['pmids']
        if not all(str(x).isdigit() for x in pmids):raise ValueError('Only exact public PMIDs are accepted')
        wanted.update(map(str,pmids));identity=digest(raw);snapshot=CAT/'inputs'/(identity+'.json');snapshot.parent.mkdir(parents=True,exist_ok=True)
        if snapshot.exists():assert snapshot.read_bytes()==raw
        else:snapshot.write_bytes(raw)
        snapshots.append({'path':str(path),'sha256':identity,'snapshot':str(snapshot.relative_to(ROOT)),'pmids':len(pmids)})
    run_id=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'_'+str(time.time_ns()%1000000000).zfill(9);new=sorted(wanted-set(index['MED']),key=int,reverse=True)
    summary={'run_id':run_id,'inputs':snapshots,'wanted_pmids':len(wanted),'new_pmids':len(new),'dispatch_authorized':False,'model_calls':0,'errors':[]}
    for offset in range(0,len(new),100):
        ids=new[offset:offset+100];name,receipt,data=core_batch('MED',ids)
        if data is None:
            summary['errors'].append({'batch':name,'status':receipt['status'],'pmids':ids});continue
        found={}
        for position,row in enumerate(records(data)):
            identity=str(row.get('id',''))
            if row.get('source')!='MED' or identity not in ids:raise RuntimeError('MED query returned an unexpected identity')
            item=persist_record('MED',row,name,position)
            links=[x for x in listify((row.get('commentCorrectionList') or {}).get('commentCorrection')) if x.get('source')=='PPR' and x.get('type','').strip().lower()=='preprint in']
            item.update(linked_preprints=links,dispatch_authorized=False,lookup_status='linked_preprint' if links else 'no_indexed_preprint_link')
            index['MED'][identity]=item;found[identity]=item
        for identity in set(ids)-set(found):index['MED'][identity]={'id':identity,'source':'MED','lookup_status':'med_not_found','response_receipt':name+'.receipt.json','dispatch_authorized':False}
        atomic(CAT/'index.json',index)
        linked=sum(bool(x.get('linked_preprints')) for x in index['MED'].values())
        print(json.dumps({'stage':'MED','processed':offset+len(ids),'new_total':len(new),'catalogued_med':len(index['MED']),'linked_pmids':linked}),flush=True)
    ppr_ids=sorted({str(link['id']) for identity in wanted for link in index['MED'].get(identity,{}).get('linked_preprints',[]) if link.get('id')})
    missing=[];summary['preprint_lookup_denied']=[]
    for ppr in ppr_ids:
        anchors=[identity for identity in wanted if any(str(link['id'])==ppr for link in index['MED'].get(identity,{}).get('linked_preprints',[]))]
        try:
            for identity in anchors:authorize_preprint_retrieval(identity)
        except PreprintRetrievalDenied as error:
            summary['preprint_lookup_denied'].append({'ppr_id':ppr,'published_pmids':anchors,'reason':str(error)});continue
        if ppr not in index['PPR']:missing.append(ppr)
    for offset in range(0,len(missing),75):
        ids=missing[offset:offset+75];anchors=[identity for identity in wanted if any(str(link['id']) in ids for link in index['MED'].get(identity,{}).get('linked_preprints',[]))];name,receipt,data=core_batch('PPR',ids,published_pmids=anchors)
        if data is None:
            summary['errors'].append({'batch':name,'status':receipt['status'],'ppr_ids':ids});continue
        found=set()
        for position,row in enumerate(records(data)):
            identity=str(row.get('id',''))
            if row.get('source')!='PPR' or identity not in ids:raise RuntimeError('PPR query returned unexpected identity')
            item=persist_record('PPR',row,name,position)
            item.update(lookup_status='resolved',dispatch_authorized=False)
            index['PPR'][identity]=item;found.add(identity)
        for identity in set(ids)-found:index['PPR'][identity]={'id':identity,'source':'PPR','lookup_status':'ppr_not_found_in_current_search','response_receipt':name+'.receipt.json','dispatch_authorized':False}
        atomic(CAT/'index.json',index)
        print(json.dumps({'stage':'PPR','processed':offset+len(ids),'new_total':len(missing)}),flush=True)
    candidates=[]
    for identity in sorted(wanted,key=int):
        med=index['MED'].get(identity,{})
        for link in med.get('linked_preprints',[]):
            ppr=index['PPR'].get(str(link['id']),{});row=json.loads((ROOT/ppr['record_path']).read_text()) if ppr.get('record_path') else {}
            reverse=[x for x in listify((row.get('commentCorrectionList') or {}).get('commentCorrection')) if x.get('source')=='MED' and str(x.get('id'))==identity and x.get('type','').lower()=='preprint of']
            note=link.get('note','')
            try:source_gate=authorize_preprint_retrieval(identity)
            except PreprintRetrievalDenied as error:source_gate={'pmid':identity,'preprint_fetch_allowed':False,'reason':str(error)}
            candidates.append({'published_pmid':identity,'preprint_id':str(link['id']),'relation':link,'link_origin':'crossref_loader' if 'crossref' in note.lower() else 'title_author_match' if 'match' in note.lower() else 'unspecified',
                'med_record':med.get('record_path'),'ppr_record':ppr.get('record_path'),'ppr_status':ppr.get('lookup_status'),'reciprocal_link':bool(reverse),
                'preprint_doi':row.get('doi'),'preprint_title':row.get('title'),'version_number':row.get('versionNumber'),'license':row.get('license'),
                'full_text_urls':(row.get('fullTextUrlList') or {}).get('fullTextUrl',[]),'isOpenAccess':row.get('isOpenAccess'),'inEPMC':row.get('inEPMC'),'hasPDF':row.get('hasPDF'),
                'source_availability_gate':source_gate,'dispatch_authorized':False,'identity_gate':'Native blocked-context and version/fulltext identity validation still required.'})
    atomic(CAT/'linked_candidates.json',candidates)
    summary.update(catalogued_input_pmids=sum(x in index['MED'] for x in wanted),linked_pmids=len({x['published_pmid'] for x in candidates}),linked_preprint_pairs=len(candidates),ppr_records=len(ppr_ids))
    atomic(CAT/'runs'/(run_id+'.json'),summary);atomic(CAT/'latest_summary.json',summary)
    print(json.dumps(summary),flush=True)
    return summary

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--pmids',type=Path,action='append',required=True);args=parser.parse_args();run(args.pmids)
