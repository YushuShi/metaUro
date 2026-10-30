"""Repair only proven synthetic-zero duplicate receipts; do not redraw science."""
from pathlib import Path
import hashlib,importlib.util,json
WORK=Path(__file__).resolve().parents[1];ROOT=WORK.parent
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
reports=[]
for repo in ['metaGut','MetaVir']:
 base=ROOT/repo;manifest_path=base/'data/preprint_recovery/active_manifest.json';manifest=json.loads(manifest_path.read_text())
 namespaces=set()
 for context in manifest['contexts']:
  for path in context['source_cache_paths']:
   cache=Path(path).parent.parent
   plot=cache/'Plot' if repo=='MetaVir' and cache.name in {'yes','no'} else cache.parent/'Plot'
   namespaces.add(base/plot/'preprints_included')
 for namespace in namespaces:
  if not namespace.is_dir():continue
  before={str(p.relative_to(namespace)):sha(p) for p in namespace.rglob('*') if p.is_file()}
  receipt_changes=[]
  for path in namespace.rglob('preprint_build_receipt.json'):
   receipt=json.loads(path.read_text());cells=receipt.get('native_pooling_cells',[])
   witness=[c for c in cells if c.get('selection_policy')!='native_generator_produced_no_estimate_for_recovered_source_context']
   def same_cell(a,b):
    return all(a.get(key)==b.get(key) for key in ['source_repo','include_subexposures','source_variant','dataset']) and bool(set(a.get('source_cache_paths') or []).intersection(b.get('source_cache_paths') or []))
   removed=[c for c in cells if c.get('selection_policy')=='native_generator_produced_no_estimate_for_recovered_source_context' and any(same_cell(c,w) for w in witness)]
   retained=[c for c in cells if c not in removed]
   scientific_before={json.dumps(c.get('source_cache_paths'),sort_keys=True)+str(c.get('dataset')):c.get('selected_rows_sha256') for c in witness}
   for cell in retained:
    sources=cell.get('source_cache_paths') or [];ids={Path(s).parent.name for s in sources}
    labels={c['selector']['exposure'] for c in manifest['contexts'] if set(c['source_cache_paths']).intersection(sources)}
    if len(ids)==1:cell['native_exposure_id']=next(iter(ids))
    if len(labels)==1:cell['exposure_label']=next(iter(labels))
   receipt['native_pooling_cells']=retained
   receipt['receipt_canonicalization']={'version':1,'source':'exact canonical native cache aliases and dataset','removed_synthetic_zero_duplicates':len(removed),'native_scientific_rows_unchanged':True}
   path.write_text(json.dumps(receipt,indent=2)+'\n')
   assert scientific_before=={json.dumps(c.get('source_cache_paths'),sort_keys=True)+str(c.get('dataset')):c.get('selected_rows_sha256') for c in retained if c in witness}
   receipt_changes.append(dict(path=str(path.relative_to(base)),removed_synthetic_zero_duplicates=len(removed),retained_cells=len(retained),selected_row_hashes=scientific_before))
  spec=importlib.util.spec_from_file_location('_seal_'+repo,base/'preprint_ui.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  module.write_preprint_summary_seal(namespace,base,{'scripts/build_preprint_summaries.py':sha(base/'scripts/build_preprint_summaries.py')})
  binary_unchanged=all(sha(namespace/relative)==digest for relative,digest in before.items() if Path(relative).suffix in {'.pdf','.png','.svg','.xlsx','.csv'})
  assert binary_unchanged
  reports.append(dict(repository=repo,namespace=str(namespace.relative_to(base)),receipt_changes=receipt_changes,all_pdf_png_svg_xlsx_csv_bytes_unchanged=True))
(WORK/'ui_architecture/native_receipt_canonicalization.json').write_text(json.dumps(reports,indent=2)+'\n')
print(json.dumps(reports,indent=2))
