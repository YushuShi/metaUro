from pathlib import Path
import json,sys,collections,hashlib,copy
root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root));from scripts import trend_ci_projection as projection
repo=root.name
out=root/'data/quantitative_audits/trend_p_ci_2026-09-27/audit_expanded_before.json'
if out.exists():raise SystemExit('Refusing to overwrite the immutable before-correction audit: '+str(out))
if repo=='metaFemina':
 folders=['combined/Cached_results','combined/subcategory_results','chatgpt/Cached_results','chatgpt/subcategory_results','data/subexposure_views','chatgpt/subexposure_views']
 files=['combined/final_screening.json','chatgpt/final_screening.json','combined/provisional_ui/effective_results.json','data/poolability_policy/2026-09-05/chatgpt_effective_results.json','data/poolability_policy/2026-09-05/claude_gemini_effective_results.json']
else:
 folders=['Cached_results','ClaudeGemini_results','Disease_hierarchy_results','Subexposure_results','combined']
 files=['data/claude_gemini_screening/all_history_through_2026-08-29/poolability_effective_results.json']
paths={root/p for p in files if (root/p).is_file()}
for folder in folders:
 for p in (root/folder).rglob('*.json'):
  if any(x in p.parts for x in ['_history','static','staged_provisional_ui']):continue
  if 'Cached_results' in p.parts or 'subcategory_results' in p.parts or p.name.endswith(('_core.json','_all.json')) or p.name=='effective_results.json':paths.add(p)
found=[];hashes={};counts=collections.Counter()
for p in sorted(paths):
 raw=p.read_bytes();data=json.loads(raw);changes=projection.correct_tree(data)
 hashes[str(p.relative_to(root))]=hashlib.sha256(raw).hexdigest()
 if not changes:continue
 print(p.relative_to(root),len(changes),flush=True)
 found.append({'file':str(p.relative_to(root)),'source_sha256':hashes[str(p.relative_to(root))],'changes':changes})
 counts['objects']+=len(changes)
 for c in changes:counts['copied' if c['receipt'].get('copied_from') else 'direct']+=1
out=root/'data/quantitative_audits/trend_p_ci_2026-09-27/audit_expanded_before.json';out.parent.mkdir(parents=True,exist_ok=True)
out.write_text(json.dumps({'mode':'read_only_before_mutation','summary':dict(counts),'files_scanned':len(paths),'source_sha256':hashes,'affected_files':found},indent=2,ensure_ascii=False)+'\n')
print('TOTAL',len(paths),len(found),dict(counts))
