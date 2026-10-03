"""Seal scoped source-review artifacts after conserving the pre-release evidence."""
from __future__ import annotations
import collections,hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import multicancer_evidence as review
from scripts import subexposure_policy as policy
OUT=review.DIRECTORY
VERSION=review.VERSION

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
def read(p):return json.loads(p.read_text())
def old_bytes(rel):
 r=subprocess.run(['git','show','HEAD:'+rel],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
 return r.stdout if r.returncode==0 else None

def scientific(row):
 fields=('PMID','Effect ID','Record ID','Direct Exposure','Effect Type','Effect Size','Lower CI','Upper CI','Sample Size','Cases','Gender','Quality Score')
 # JSON numeric 1 and 1.0 are scientifically identical.
 return tuple(row.get(k) for k in fields)

def conservation():
 changed=subprocess.check_output(['git','diff','--name-only'],cwd=ROOT,text=True).splitlines()
 report=[];oldmap={};cache_count=0
 for relative in changed:
  p=ROOT/relative;raw=old_bytes(relative)
  if raw is not None:oldmap[hashlib.sha256(raw).hexdigest()]=sha(p)
  if not relative.endswith('.json') or not relative.startswith(('combined/','Subexposure_results/')) or raw is None:continue
  before=json.loads(raw);after=read(p)
  if isinstance(before.get('results'),list):
   old={r['context_id']:r for r in before['results']};new={r['context_id']:r for r in after['results']}
   assert old.keys() <= new.keys(),relative
   assert all(new[cid]==r for cid,r in old.items()),('Historical screening record changed',relative)
   report.append({'path':relative,'historical_records_exactly_unchanged':len(old),'added_records':len(new)-len(old)})
  if isinstance(before.get('studies'),list):
   expected=collections.Counter(scientific(review.correct_row(r)) for r in before['studies'])
   actual=collections.Counter(scientific(r) for r in after['studies'])
   assert not expected-actual,('Historical study lost/changed',relative,list((expected-actual).elements())[:2])
   oldids={r.get('Effect ID') for r in before['studies']}
   added=[r for r in after['studies'] if r.get('Effect ID') not in oldids]
   assert all(r.get('evidence_revision')==VERSION for r in added),('Unreviewed addition',relative)
   report.append({'path':relative,'historical_rows_preserved':len(before['studies']),'rows_after':len(after['studies']),'added_rows':len(added),'bacon_corrected':sum(bool(r.get('bacon_source_correction')) for r in after['studies'])});cache_count+=1
 return {'files':report,'checked_cache_files':cache_count,'historical_records_preserved':True,'historical_scientific_rows_preserved_except_exact_bacon_correction':True},oldmap

def update_hashes(v,mapping):
 if isinstance(v,str):return mapping.get(v,v)
 if isinstance(v,list):return [update_hashes(x,mapping) for x in v]
 if isinstance(v,dict):return {k:update_hashes(x,mapping) for k,x in v.items()}
 return v

def main():
 # Revision references follow the final registry, including corrected locators.
 roots=[ROOT/'combined']
 if ROOT.name=='MetaVir':roots += [ROOT/'Subexposure_results']
 for root in roots:
  for p in root.rglob('*.json'):
   if p.name.endswith(('_core.json','_all.json')) and not any(x in p.parts for x in ['_history','astra_fulltext','astra_adjudication']):
    obj=read(p)
    if obj.get('evidence_revision')==VERSION and obj.get('evidence_revision_source_sha256')!=sha(OUT/'registry.json'):
     obj['evidence_revision_source_sha256']=sha(OUT/'registry.json');write(p,obj)
 conservation_report,mapping=conservation();write(OUT/'conservation.json',conservation_report)
 # Bind only active manifests. Historical release/audit hashes remain immutable.
 manifest_paths=[]
 if ROOT.name=='MetaVir':
  from scripts import build_combined_results as combined_builder
  main=ROOT/'combined/_merge_manifest.json';obj=read(main)
  records=read(ROOT/'combined/final_screening.json')['results']
  caches=list((ROOT/'combined').glob('*/*_true_core.json'))
  counts=dict(obj['counts'])
  counts.update(current_contexts=len(records),combined_statuses=dict(collections.Counter(r['final_status'] for r in records)),cache_exposures=len({p.parent for p in caches}),cache_files=2*len(caches),displayed_ui_studies=sum(len(read(p)['studies']) for p in caches),direct_ui_studies=len({r.get('Effect ID') or r.get('Record ID') for p in caches for r in read(p)['studies']}))
  counts['merge_actions']=dict(collections.Counter(r['merge_action'] for r in read(ROOT/'combined/_adjudication_ledger.json')['records']))
  obj['evidence_revision']={'version':VERSION,'registry':'data/multicancer_review_2026-09-29/registry.json','registry_sha256':sha(OUT/'registry.json'),'preserves_base_build_id':True,'additional_contexts':len(review.registry()['entries']),'current_counts':counts,'producer_digests':{k:sha(p) for k,p in combined_builder.GENERATOR_FILES.items()},'legacy_validation_baseline_sha256':sha(OUT/'legacy_validation_baseline.json'),'base_metadata_scope':'Base adjudication build; current source review counts and producing code are recorded in this evidence_revision.'};write(main,obj)
  for mode in ['no','yes']:
   (ROOT/f'Subexposure_results/{mode}/_merge_manifest.json').write_bytes(main.read_bytes())
   p=ROOT/f'Subexposure_results/{mode}/_subexposure_manifest.json';obj=read(p);obj['source_cache_sha256']={str(c.relative_to(ROOT)):sha(c) for c in sorted((ROOT/'combined').glob('*/*_true_all.json'))};obj['evidence_revision']=VERSION;write(p,obj)
   p=ROOT/f'Plot/subexposures/{mode}/summary_manifest.json';obj=read(p);obj['combined_manifest_sha256']=sha(main);write(p,obj)
 else:
  p=ROOT/'combined/provisional_ui/manifest.json';obj=read(p)
  for relative in obj['protected_source_hashes']:obj['protected_source_hashes'][relative]=sha(ROOT/relative)
  obj['evidence_revision']={'version':VERSION,'registry_sha256':sha(OUT/'registry.json'),'additional_contexts':len(review.registry()['entries']),'bacon_estimate_held':True};write(p,obj)
  for mode in ['no','yes']:
   p=ROOT/f'combined/provisional_ui/subexposure_views/{mode}/Cached_results/_subexposure_manifest.json';obj=read(p);obj['evidence_revision']=VERSION;write(p,obj)
 # Refresh source hashes already declared by the affected active manifests.
 for root in roots:
  for p in root.rglob('*manifest*.json'):
   if not any(x in p.parts for x in ['astra_fulltext','astra_adjudication','_history','chatgpt','outputs']):manifest_paths.append(p)
 for _ in range(2):
  for p in manifest_paths:
   obj=read(p);updated=update_hashes(obj,mapping)
   if obj!=updated:
    before=sha(p);write(p,updated);mapping[before]=sha(p)
 # Correct final receipt hashes after JSON formatting and manifest sealing.
 p=OUT/'application_report.json';obj=read(p)
 for entry in obj['changes']:entry['after_sha256']=sha(ROOT/entry['path'])
 obj['status']='completed';obj['final_registry_sha256']=sha(OUT/'registry.json');write(p,obj)
 registered={r['Effect ID']:r for r in review.rows()};profiles=[ROOT/f'Subexposure_results/{m}' for m in ['no','yes']] if ROOT.name=='MetaVir' else [ROOT/f'combined/provisional_ui/subexposure_views/{m}/Cached_results' for m in ['no','yes']]
 selected_by_mode={}
 for cache in profiles:
  found=set();selected=set()
  for p in cache.glob('*/*_true_core.json'):
   payload=read(p)
   for r in payload.get('studies',[]):
    rid=r.get('Effect ID')
    if rid in registered:
     expected=registered[rid]
     for k in ['PMID','Effect Size','Lower CI','Upper CI','Sample Size','Cases','Gender','Quality Score']:assert r.get(k)==expected.get(k),(p,k)
     found.add(rid)
     if policy.eligible_for_selection(r) and r.get('subexposure_pooling_selected') is not False and r.get('disease_hierarchy_pooling_selected') is not False:selected.add(rid)
    if r.get('bacon_source_correction'):assert r.get('policy_poolable') is False and r.get('Gender')=='Female' and r.get('Source Exposure')=='Bacon'
   for field in ['plot_url','funnel_plot_url','baujat_plot_url']:
    url=payload.get(field)
    if not url or not payload.get('evidence_revision'):continue
    local=url.split('?',1)[0]
    if local.startswith('provisional-assets/'):
     _,mode,*parts=local.split('/');target=ROOT/'combined/provisional_ui/static'/mode/Path(*parts)
    else:target=ROOT/local
    assert target.is_file(),(p,field,target)
  assert found==set(registered),(str(cache),set(registered)-found)
  selected_by_mode[str(cache.relative_to(ROOT))]={'new_visible_estimates':len(found),'new_selected_in_at_least_one_exact_scope':len(selected)}
 # Scratch import diagnostics are not release artifacts.
 for p in ROOT.rglob('*.xlsx.inspect.ndjson'):
  if str(p).startswith(str(ROOT/'Plot')) or 'combined/provisional_ui/' in str(p):p.unlink()
 files=set(subprocess.check_output(['git','diff','--name-only'],cwd=ROOT,text=True).splitlines())
 files.update(subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=ROOT,text=True).splitlines())
 files={f for f in files if (ROOT/f).is_file() and not any(k in Path(f).parts for k in ['__pycache__','previews','previews_before'])}
 manifest={'version':VERSION,'status':'completed','repository':ROOT.name,'candidate_direct_rows':len(registered),'added_direct_rows':len(registered),'new_poolable_direct_rows':sum(r['policy_poolable'] for r in registered.values()),'held_direct_rows':sum(not r['policy_poolable'] for r in registered.values()),'source_provenance':'User-supplied PMIDs; primary source table review. Original model packets unchanged.','validation':selected_by_mode,'conservation_sha256':sha(OUT/'conservation.json'),'artifact_sha256':{f:sha(ROOT/f) for f in sorted(files) if f!='data/multicancer_review_2026-09-29/release_manifest.json'},'network_calls_during_rebuild':0,'model_calls':0}
 write(OUT/'release_manifest.json',manifest)
 print(ROOT.name,'sealed',len(files),'artifacts',selected_by_mode,flush=True)
if __name__=='__main__':main()
