from pathlib import Path
import json,hashlib,sys,collections,copy
root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root));from scripts import trend_ci_projection as projection
out=root/'data/quantitative_audits/trend_p_ci_2026-09-27';audit=json.loads((out/'audit_expanded_before.json').read_text())
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
# Validate the frozen audit before any mutation.
for f in audit['affected_files']:
 p=root/f['file'];assert sha(p)==f['source_sha256'],p
report=[]
for f in audit['affected_files']:
 p=root/f['file'];obj=json.loads(p.read_text());changes=projection.correct_tree(obj)
 if not changes:continue
 if isinstance(obj.get('results'),list):
  for r in obj['results']:
   ext=r.get('extraction') or {}
   if ext.get('p_value_ci_retraction'):
    ext['missing_quantitative_fields']=sorted(set(ext.get('missing_quantitative_fields') or [])|{'ci_lower','ci_upper'})
    r['missing_quantitative_fields']=ext['missing_quantitative_fields']
    if root.name=='metaFemina':
     from scripts.build_combined_results import bind_final_row_digests
     bind_final_row_digests(r)
  counts=dict(collections.Counter(r.get('final_status') for r in obj['results']))
  for k in ['counts','status_counts']:
   if k in obj:obj[k]=counts
 obj['trend_ci_correction_version']=projection.guard.VERSION
 p.write_text(json.dumps(obj,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
 report.append({'file':f['file'],'before_sha256':f['source_sha256'],'after_sha256':sha(p),'changes':changes})
(out/'application_report.json').write_text(json.dumps({'status':'intervals_retracted_refresh_pending','guard_version':projection.guard.VERSION,'changed_files':len(report),'changes':report,'source_excerpt_audit':'audit_expanded_before.json','network_calls':0,'model_calls':0},indent=2,ensure_ascii=False)+'\n')
print(root.name,len(report),sum(len(f['changes']) for f in report),'interval objects retracted')
