"""Audit a staged optional view's clone-ready runtime inputs, never private R2."""
import argparse,json,re,subprocess
from pathlib import Path
import native_overlay_converter as native

def resolve_binding(repo_root,manifest_path,relative):
 part=Path(relative)
 if part.is_absolute() or '..' in part.parts:raise ValueError('Unsafe runtime dependency path: '+relative)
 prefix='updates/preprint_recovery/' if repo_root.name=='metaUro' else 'data/preprint_recovery/'
 if relative.startswith(prefix):
  candidate=manifest_path.parent/relative[len(prefix):]
  if candidate.is_file():return candidate,True
 return repo_root/relative,False

def audit(manifest_paths,output):
 records=[];errors=[]
 for manifest_path in map(Path,manifest_paths):
  manifest=json.loads(manifest_path.read_text());repo=manifest['repository'];repo_name='metaMeta/metaOnco' if repo in {'metaOnco','metaMeta'} else repo;root=native.ROOT/repo_name;bindings={}
  for context in manifest.get('contexts',[]):
   bindings.update(context.get('baseline_files') or {})
   ref=context.get('article_availability_gate') or {}
   if ref.get('path'):bindings[ref['path']]=ref['sha256']
  for relative,expected in sorted(bindings.items()):
   path,staged=resolve_binding(root,manifest_path,relative);row={'repository':repo_name,'path':relative,'expected_sha256':expected,'exists':path.is_file(),'staged_recovery_artifact':staged,'manifest_sha256':native.sha(manifest_path)}
   if not path.is_file():errors.append({**row,'reason':'Runtime dependency absent from working source and proposed release'});records.append(row);continue
   row['actual_sha256']=native.sha(path)
   if row['actual_sha256']!=expected:errors.append({**row,'reason':'Runtime dependency bytes changed'})
   if staged:row['release_status']='must_publish_staged_recovery_artifact'
   else:
    tracked=subprocess.run(['git','-C',str(root),'ls-files','--error-unmatch','--',relative],capture_output=True).returncode==0;index=subprocess.run(['git','-C',str(root),'show',':'+relative],capture_output=True);row['tracked']=tracked;row['matches_index']=False
    if not index.returncode:
     import hashlib
     row['index_blob_sha256']=hashlib.sha256(index.stdout).hexdigest();lfs=re.search(rb'^oid sha256:([a-f0-9]{64})$',index.stdout,re.M) if index.stdout.startswith(b'version https://git-lfs.github.com/spec/v1\n') else None
     row['matches_index']=row['index_blob_sha256']==expected or bool(lfs and lfs.group(1).decode()==expected)
     if lfs:row['index_lfs_object_sha256']=lfs.group(1).decode()
    row['release_status']='already_index_bound' if row['matches_index'] else 'must_publish_exact_task_code_or_authoritative_cache'
   records.append(row)
 report={'schema_version':1,'producer_sha256':native.sha(__file__),'runtime_dependencies':len(records),'missing_or_changed':len(errors),'errors':errors,'requires_task_publication':[{'repository':r['repository'],'path':r['path'],'sha256':r['expected_sha256'],'status':r['release_status']} for r in records if r.get('release_status')!='already_index_bound'],'records':records,'private_R2_policy':'R2 packets are audit_input_bindings only, not website runtime dependencies; never publish abstract-bearing private packets.'}
 native.save(output,report)
 if errors:raise ValueError('Runtime dependencies absent or changed: '+str(len(errors)))
 return report

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--manifest',action='append',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();result=audit(args.manifest,args.output);print(json.dumps({k:v for k,v in result.items() if k not in {'records','errors','requires_task_publication'}},indent=2))
