"""Format exact native-final selections as an optional Onco source projection.

Taxonomy identities must come from an explicit mapping. Native final selected
rows authorize pooling; a reviewer's poolability label alone never does.
"""
from __future__ import annotations
import argparse,copy,json
from pathlib import Path
import native_overlay_converter as native
from uro_overlay_projection import bound_gate,row_lineage_proof,resolver_module

EXTRA_FIELDS=('Cohort','cohort','Cohort Name','cohort_name','cohort_names','Study Name','study_name',
 'publication_date','publication_date_precision','Publication Date','Year','year','Sex','sex','Gender','gender',
 'Subgroup','subgroup','Direct Exposure','Direct Disease','Outcome','outcome','Population','population',
 'independent_population_id','Independent Population ID','population_id','Population ID','Stratum','stratum',
 'cohort_metadata','preprint_native_context_id','preprint_source','preprint_context_id','preprint_original_record_ids')

class NoScientificProjection(ValueError):pass

def append_lineage_authorization(mapping,context,manifest_path,lineages,admission):
 if mapping.get('original_evidence_for_pmid'):raise ValueError('Append lineage cannot replace an existing published target estimate')
 if context.get('review_context_id')!='32814947|Red meat|Prostate cancer' or context['selector'].get('source_repo')!='MetaVir':raise ValueError('Append lineage exception is restricted to the explicitly approved blocked red-meat/prostate anchor')
 matches=[r for r in lineages if r.get('native_context_id')==context['context_id'] and r.get('native_manifest_sha256')==native.sha(manifest_path)]
 if len(matches)!=1 or not admission or admission.get('status')!='PASS':raise ValueError('Missing exact original-row lineage or actual native admission proof')
 lineage=matches[0];resolver,_=resolver_module();actual=row_lineage_proof(context,manifest_path,native.ROOT/'metaUro',resolver)
 if native.digest(actual)!=native.digest(lineage):raise ValueError('Exact source/Uro original-record lineage changed')
 witnesses=[f for f in lineage['source_files'] if f['native_cache_path']==mapping['native_source_cache_path']]
 if len(witnesses)!=1:raise ValueError('Published Onco source cache lacks exact target-row lineage witness')
 source=witnesses[0]
 if source['native_cache_sha256']!=mapping['native_source_cache_sha256'] or source['durable_uro_sha256']!=mapping.get('onco_source_cache_sha256'):raise ValueError('Published Onco cache is not the exact pinned old Uro source generation')
 path_map={f['native_cache_path']:f['durable_uro_path'] for f in lineage['source_files']};uro_context='preprint-uro:'+native.digest(['MetaVir',context['context_id'],path_map])[:32];equivalence=next((r for r in admission.get('frozen_bundle_equivalence',[]) if r.get('context_id')==uro_context),None)
 projected=copy.deepcopy(context['native_rows'])
 for row in projected:row['preprint_derivative_source_repo']='MetaVir'
 if not equivalence or equivalence.get('scientific_rows_sha256')!=native.digest(projected):raise ValueError('Actual native Uro disposition proof does not bind the same complete approved preprint bundle')
 return {'policy':'Narrow append only for actual native-final-selected recovery absent from the published cell; every ordinary published peer estimate remains unchanged.','exact_original_record_lineage':lineage,'native_admission_verification':admission,'explicit_projection_mapping_sha256':native.digest(mapping),'published_snapshot_sha256':mapping['snapshot_sha256']}

def evidence_row(row,identity):
 def present(*fields):
  return next((row[k] for k in fields if k in row and row[k] is not None and str(row[k]).strip().casefold() not in {'','nan','none','null','na','n/a'}),None)
 def number(*fields):return next((v for k in fields if (v:=native.number(row.get(k))) is not None),None)
 # Match Onco's native source_selection priority: harmonized fields precede
 # original reported fields, which may represent the reciprocal contrast.
 record_id=present('Record ID','record_id','Effect ID','effect_record_id')
 result={'pmid':str(present('PMID','pmid') or '').removesuffix('.0'),
  'record_id':str(record_id or identity(row)), 'study':str(present('Study','study') or ''),
  'effect_type':native.canonical_type(present('Effect Type','effect_type','Original Effect Type','original_effect_type','Outcome Type','Measure')),
  'effect':number('Effect Size','effect_size','effect','Original Effect Size','original_effect_size'),
  'ci_low':number('Lower CI','lower_ci','ci_low','Original Lower CI','original_lower_ci'),
  'ci_high':number('Upper CI','upper_ci','ci_high','Original Upper CI','original_upper_ci'),
  'preprint_derived':row.get('preprint_derived') is True}
 for field in EXTRA_FIELDS:
  if field in row:result[field]=copy.deepcopy(row[field])
 if result['effect_type'] not in native.RATIOS or any(result[k] is None or result[k]<=0 for k in ('effect','ci_low','ci_high')) or not result['ci_low']<result['ci_high'] or not result['ci_low']<=result['effect']<=result['ci_high']:raise ValueError('Native final selected row is not a valid direction-harmonized ratio')
 return result

def load_receipts(paths):
 cells=[]
 for path in map(Path,paths):
  document=json.loads(path.read_text());rows=document.get('native_pooling_cells')
  if document.get('keep_latest_cohort') is True:continue
  if document.get('keep_latest_cohort') is not False:raise ValueError('Native selection witness must explicitly preserve all publications before dynamic cohort selection')
  if not isinstance(rows,list):raise ValueError('Native final selection receipt lacks cells: '+str(path))
  for index,cell in enumerate(rows):cells.append((cell,path,index))
 return cells

def exact_cell(mapping,context,cells,manifest_sha):
 selector=context['selector'];matches=[];exposure_aliases={selector['exposure'],mapping['native_selector']['exposure'],mapping['projection']['exposure_id'],Path(mapping['native_source_cache_path']).parent.name}
 for cell,path,index in cells:
  if any(cell.get(k)!=selector.get(k) for k in ('source_repo','disease','outcome','include_subexposures','source_variant')) or cell.get('exposure') not in exposure_aliases:continue
  if cell.get('dataset')!=mapping['projection']['dataset'] or mapping['native_source_cache_path'] not in cell.get('source_cache_paths',[]):continue
  if cell.get('preprint_manifest_sha256')!=manifest_sha:raise ValueError('Native selection receipt binds another recovery manifest')
  rows=cell.get('selected_rows') or []
  if cell.get('selected_rows_sha256') and native.digest(rows)!=cell['selected_rows_sha256']:
   # The native receipt serializes ASCII by default; accept its exact serializer
   # as well as the producing converter's Unicode canonical digest.
   import hashlib
   original=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
   if original!=cell['selected_rows_sha256']:raise ValueError('Native final selected row hash changed')
  matches.append((cell,path,index))
 if not matches:raise ValueError('No exact native final engine selection witness')
 actual=[x for x in matches if x[0].get('selection_policy')!='native_generator_produced_no_estimate_for_recovered_source_context']
 if actual:matches=actual
 signatures={native.digest(x[0].get('selected_rows') or []) for x in matches}
 if len(signatures)!=1:raise ValueError('Conflicting all/core native final selected rows require explicit source choice')
 return matches[0]

def format_mapping(mapping,context,manifest_path,cells,onco_root,identity,lineages=None,admission=None):
 if context.get('review',{}).get('decision')!='include' or context.get('review',{}).get('status') not in {'adjudicated','settled'}:raise ValueError('Unsettled native context cannot project into Onco')
 if context.get('native_rows') and not any(r.get('policy_poolable') is True and r.get('Primary Estimate') is True for r in context['native_rows']):
  bound_gate(context,manifest_path)
  raise NoScientificProjection('Settled native source is display-only/nonpoolable; no Onco dot projection is required')
 snapshot=onco_root/mapping['snapshot_path']
 if not snapshot.is_file() or native.sha(snapshot)!=mapping.get('snapshot_sha256'):raise ValueError('Onco snapshot changed since exact taxonomy mapping')
 snapshot_data=json.loads(snapshot.read_text());projection=mapping['projection'];pairs=[r for r in snapshot_data.get('results',[]) if r.get('exposure_id')==projection['exposure_id'] and r.get('cancer_id')==projection['cancer_id']]
 if len(pairs)>1:raise ValueError('Duplicate published Onco result cell')
 metric=pairs[0].get(projection['dataset']) if pairs else None
 if (metric is not None)!=bool(mapping.get('existing_metric')):raise ValueError('Onco metric existence differs from explicit source mapping')
 original=[r for r in (metric or {}).get('evidence',[]) if str(r.get('pmid'))==str(mapping['pmid'])]
 if native.digest(original)!=native.digest(mapping.get('original_evidence_for_pmid') or []):raise ValueError('Onco original target evidence differs from exact source mapping')
 relative=mapping['native_source_cache_path'];expected=context.get('baseline_files',{}).get(relative)
 if not expected or expected!=mapping.get('native_source_cache_sha256'):raise ValueError('Native context/cache binding differs from explicit Onco mapping')
 lineage=None
 if mapping.get('existing_metric') and mapping.get('native_and_snapshot_source_cache_identical') is not True:
  if lineages is None:raise ValueError('Published Onco source generation differs; explicit scientific lineage review required')
  lineage=append_lineage_authorization(mapping,context,manifest_path,lineages,admission)
 cell,path,index=exact_cell(mapping,context,cells,native.sha(manifest_path));selected=cell.get('selected_rows') or []
 if not cell.get('headline') or not selected:raise NoScientificProjection('Native engine produced no poolable estimate for this reviewed source cell')
 if cell.get('pooled_n',0)!=len(selected):raise ValueError('Native selected row count disagrees with its estimate witness')
 if cell.get('selected_record_ids')!=[identity(r) for r in selected]:raise ValueError('Native final selected record IDs do not match witnessed rows')
 recovered_ids={identity(row) for row in context.get('native_rows',[]) if row.get('preprint_derived') is True};selected_recovered=[r for r in selected if identity(r) in recovered_ids]
 if not selected_recovered:raise NoScientificProjection('Reviewed recovered rows did not enter native final pooling selection')
 native_context=copy.deepcopy(context);selector={**context['selector'],**mapping['required_projection_selector_fields']};native_context['selector']=selector
 old=mapping.get('original_evidence_for_pmid') or [];old_ids=sorted({identity(row) for row in old});rows=[]
 for row in selected_recovered:
  evidence=evidence_row(row,identity);evidence['preprint_original_record_ids']=old_ids;rows.append(evidence)
 native_context.update(context_id='preprint-onco:'+native.digest([context['context_id'],mapping['projection']])[:32],operation='replace' if old_ids else 'add',original_record_ids=old_ids,native_rows=rows,source_cache_paths=[],native_payload=None,
  baseline_files={mapping['snapshot_path']:mapping['snapshot_sha256']},native_projection={'source_context_id':context['context_id'],'native_manifest_sha256':native.sha(manifest_path),'native_cache_path':relative,'native_cache_sha256':expected,'exact_taxonomy_mapping_sha256':native.digest(mapping)})
 baseline=[]
 if not mapping.get('existing_metric'):
  for row in selected:
   if identity(row) not in recovered_ids:
    if row.get('preprint_derived'):raise ValueError('Additional recovered context in new cell requires complete combined projection mapping')
    evidence=evidence_row(row,identity);evidence['preprint_derived']=False;baseline.append(evidence)
 proof={'schema_version':1,'native_keep_latest_cohort':False,'native_receipt_sha256':native.sha(path),'native_receipt_cell_index':index,'source_repo':cell['source_repo'],'selector':{k:cell.get(k) for k in ('disease','exposure','outcome','include_subexposures','source_variant','dataset')},'source_cache_paths':cell['source_cache_paths'],'preprint_manifest_sha256':cell['preprint_manifest_sha256'],'selected_record_ids':cell.get('selected_record_ids'),'selected_rows_sha256':cell.get('selected_rows_sha256'),'pooled_n':cell['pooled_n'],'headline':cell['headline'],'projected_recovered_evidence':rows,'ordinary_supplemental_evidence':baseline}
 if lineage:proof['explicit_append_lineage_authorization']=lineage;native_context['native_projection']['explicit_append_lineage_sha256']=native.digest(lineage)
 return native_context,baseline,proof,bound_gate(context,manifest_path)

def project_manifests(manifest_paths,mapping_path,receipt_paths,output,onco_root=None,uro_lineage_proof=None,uro_native_admission=None,qa_preview=False):
 output=Path(output);onco_root=Path(onco_root or native.ROOT/'metaMeta/metaOnco');mapping_path=Path(mapping_path);mapping=json.loads(mapping_path.read_text());cells=load_receipts(receipt_paths);identity=native.identity_function('metaMeta/metaOnco');contexts=[];baselines=[];audits=[];gates={};protected={};display_contexts=[]
 manifests=[(Path(p),json.loads(Path(p).read_text())) for p in manifest_paths]
 lineages=json.loads(Path(uro_lineage_proof).read_text()).get('records') if uro_lineage_proof else None;admission=json.loads(Path(uro_native_admission).read_text()) if uro_native_admission else None
 for item in mapping['mapping_rows']:
  selector=item['native_selector'];matches=[]
  for path,manifest in manifests:
   if manifest.get('schema_version')!=1 or manifest.get('status')!='complete':raise ValueError('Unsupported native source view manifest')
   for context in manifest.get('contexts',[]):
    if context.get('review_context_id')!=item['context_id']:continue
    if any(context.get('selector',{}).get(k)!=selector.get(k) for k in ('source_repo','disease','outcome','include_subexposures','source_variant')):continue
    if item['native_source_cache_path'] not in context.get('source_cache_paths',[]):continue
    matches.append((path,context))
  if not matches:continue
  audit={'repository':selector['source_repo'],'pmid':item['pmid'],'context_id':item['context_id'],'selector':selector,'projection':item['projection'],'activated':False}
  try:
   if len(matches)!=1:raise ValueError('Ambiguous accepted native source context')
   path,context=matches[0];projected,baseline,proof,gate=format_mapping(item,context,path,cells,onco_root,identity,lineages,admission);proof_rel='native_selection_receipts/'+native.digest(proof)+'.json';native.save(output/proof_rel,proof);projected['baseline_files']['data/preprint_recovery/'+proof_rel]=native.sha(output/proof_rel)
   for rel in ['analysis.py','cohort_identity.py','preprint_ui.py','data/exposure_taxonomy.json','scripts/source_selection.py']:
    p=onco_root/rel
    if p.is_file():projected['baseline_files'][rel]=native.sha(p)
   if baseline:baselines.append({**item['projection'],'evidence':baseline,'selection_receipt':{'path':'data/preprint_recovery/'+proof_rel,'sha256':native.sha(output/proof_rel)},'preprint_context_ids':[projected['context_id']]})
   contexts.append(projected);gates[str(gate['pmid'])]=gate;audit.update(activated=True,native_selected_recovered_rows=len(projected['native_rows']),ordinary_supplemental_rows=len(baseline))
   protected.update({str(onco_root/p):d for p,d in projected['baseline_files'].items() if not p.startswith('data/preprint_recovery/')})
  except NoScientificProjection as exc:
   audit.update(disposition='no_scientific_projection',reason=str(exc));display_contexts.append({'native_context_id':context['context_id'],'review_context_id':context['review_context_id'],'selector':context['selector'],'projection':item['projection'],'source_manifest_sha256':native.sha(path),'preprint_source':context['preprint_source'],'publication_anchor':context['publication_anchor'],'disposition':'No poolable native estimate; original reviewed source bundle retained by the native application.','native_rows':[{k:copy.deepcopy(r[k]) for k in ('Effect ID','PMID','Study','Effect Type','Effect Size','Lower CI','Upper CI','Sample Size','Cases','Quality Score','Quality %','JBI','cohort_names','publication_date','publication_date_precision','final_status','meta_analysis_status','policy_poolable','Primary Estimate') if k in r} for r in context['native_rows']]})
  except (ValueError,TypeError,KeyError) as exc:audit['hold']=str(exc)
  audits.append(audit)
 proof_path=output/'article_availability_gate.json';native.save(proof_path,{'schema_version':1,'records':[gates[p] for p in sorted(gates,key=int)]})
 for context in contexts:
  pid=str(context['publication_anchor']['pmid']);context['article_availability_gate']={'path':'data/preprint_recovery/article_availability_gate.json','sha256':native.sha(proof_path),'record_sha256':native.digest(gates[pid]),'pmid':pid}
 ids=[c['context_id'] for c in contexts]
 if len(ids)!=len(set(ids)):raise ValueError('Duplicate Onco projection/version cells')
 manifest={'schema_version':1,'repository':'metaOnco','status':'complete','generation_id':'preprint-recovery-2026-10-26-v1','qa_only':bool(qa_preview) or any(d.get('qa_only') for _,d in manifests),'contexts':contexts,'projection_baseline_cells':baselines,'display_only_native_contexts':display_contexts}
 native.save(output/'active_manifest.json',manifest);changed=[p for p,d in protected.items() if not Path(p).is_file() or native.sha(p)!=d]
 if changed:raise ValueError('Published Onco baseline changed during read-only projection')
 receipt={'schema_version':1,'producer_sha256':native.sha(__file__),'mapping_sha256':native.sha(mapping_path),'model_calls':0,'new_cost_usd':0,'contexts':len(contexts),'rows':sum(len(c['native_rows']) for c in contexts),'ordinary_supplemental_rows':sum(len(c['evidence']) for c in baselines),'source_artifacts_checked':len(protected),'source_artifacts_changed':changed,'contexts_audit':audits,'manifest_sha256':native.sha(output/'active_manifest.json')};native.save(output/'projection_receipt.json',receipt);return receipt

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--native-manifest',action='append',type=Path,required=True);parser.add_argument('--native-receipt',action='append',type=Path,required=True);parser.add_argument('--mapping',type=Path,default=native.WORK/'converter_inputs/onco_projection_mapping.json');parser.add_argument('--uro-lineage-proof',type=Path);parser.add_argument('--uro-native-admission',type=Path);parser.add_argument('--qa-preview',action='store_true');parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();result=project_manifests(args.native_manifest,args.mapping,args.native_receipt,args.output,uro_lineage_proof=args.uro_lineage_proof,uro_native_admission=args.uro_native_admission,qa_preview=args.qa_preview);print(json.dumps({k:v for k,v in result.items() if k!='contexts_audit'},indent=2))
