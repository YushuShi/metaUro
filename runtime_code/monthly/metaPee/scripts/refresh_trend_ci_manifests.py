from pathlib import Path
import hashlib,json,sys
root=Path(sys.argv[1]).resolve();isuro=root.name=='metaUro';sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
out=root/('evidence-revisions/trend-p-ci-2026-09-27' if isuro else 'data/quantitative_audits/trend_p_ci_2026-09-27')
a=json.loads((out/'application_report.json').read_text());mapping={}
for item in a['changes']:
 p=root/(item['stored_path'] if isuro else item['file']);mapping[item.get('source_sha256',item.get('before_sha256'))]=sha(p)
def replace(value):
 if isinstance(value,str):return mapping.get(value,value)
 if isinstance(value,list):return [replace(v) for v in value]
 if isinstance(value,dict):return {k:replace(v) for k,v in value.items()}
 return value
reports=[]
for attempt in range(2):
 if isuro:
  paths=[]
  for source in ['metaPee','MetaVir']:
   base=root/'sources'/source
   for folder in ['combined','Cached_results','Subexposure_results','Disease_hierarchy_results']:
    paths += [(source,p) for p in (base/folder).rglob('*manifest*.json') if 'Plot' not in p.parts and '_history' not in p.parts and 'static' not in p.parts]
 else:
  folders=['combined','chatgpt','data/subexposure_views'] if root.name=='metaFemina' else ['combined','Cached_results','Subexposure_results','Disease_hierarchy_results']
  paths=[(None,p) for folder in folders for p in (root/folder).rglob('*manifest*.json') if '_history' not in p.parts and 'static' not in p.parts]
 for source,p in sorted(set(paths)):
  original=sha(p)
  destination=root/'updates'/source/'versions/trend-p-ci-2026-09-27/files'/p.relative_to(root/'sources'/source) if isuro else p
  obj=json.loads((destination if destination.exists() else p).read_text());new=replace(obj)
  if obj==new:continue
  destination.parent.mkdir(parents=True,exist_ok=True);destination.write_text(json.dumps(new,indent=2,ensure_ascii=False)+'\n')
  mapping[original]=sha(destination)
  reports.append({'source':str(p.relative_to(root)),'output':str(destination.relative_to(root)),'before_sha256':original,'after_sha256':sha(destination)})
(out/'manifest_refresh_report.json').write_text(json.dumps(reports,indent=2)+'\n')
print(root.name,len(reports),'current manifests refreshed')
