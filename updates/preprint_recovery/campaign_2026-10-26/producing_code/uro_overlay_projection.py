"""Project accepted native source overlays onto identical durable Uro evidence.

Writes only the supplied staging folder. Immutable source snapshots, installed
monthly overlays and source/global scientific metadata remain read-only.
"""
from __future__ import annotations
import argparse,copy,importlib.util,json,sys
from pathlib import Path
import native_overlay_converter as native

def resolver_module():
 path=native.ROOT/'metaUro/automation/sync_sources.py'
 spec=importlib.util.spec_from_file_location('_preprint_uro_source_resolver',path)
 module=importlib.util.module_from_spec(spec);previous=sys.dont_write_bytecode;sys.dont_write_bytecode=True
 try:spec.loader.exec_module(module)
 finally:sys.dont_write_bytecode=previous
 return module,path

def bound_gate(context,manifest_path):
 ref=context.get('article_availability_gate') or {};path=manifest_path.parent/'article_availability_gate.json'
 if not path.is_file() or native.sha(path)!=ref.get('sha256'):raise ValueError('Native article availability proof changed')
 rows=json.loads(path.read_text()).get('records');pid=str((context.get('publication_anchor') or {}).get('pmid'))
 matches=[r for r in rows or [] if str(r.get('pmid'))==pid]
 if len(matches)!=1 or str(ref.get('pmid'))!=pid or native.digest(matches[0])!=ref.get('record_sha256'):raise ValueError('Native article availability record changed')
 r=matches[0]
 if r.get('availability') not in {'pmcid_missing','full_text_unavailable'} or r.get('preprint_fetch_allowed') is not True or r.get('model_dispatch_authorized') is not True or r.get('saved_any_track_checked') is not True or r.get('saved_any_track_full_text_available') is not False or not r.get('any_track_gate_record_sha256'):raise ValueError('Article not eligible for preprint projection')
 if r['availability']=='pmcid_missing' and (r.get('pmcids') or r.get('fresh_epmc_pmcid') or r.get('fresh_pubmed_pmcids')):raise ValueError('Conflicting current PMCID metadata')
 if r['availability']!='pmcid_missing' and not r.get('proofs'):raise ValueError('Actual unavailable-PMC proof missing')
 return copy.deepcopy(r)

def human_protected(row):
 return bool(row.get('manual_review') or row.get('manual_adjudication') or row.get('human_adjudication') or str(row.get('decision_origin','')).lower() in {'human','manual','human_manual_review'})

def row_lineage_proof(context,manifest_path,uro_root,resolver):
 """Prove only the complete original target rows, never an entire old cache."""
 source=context['selector']['source_repo'];ids=set(context.get('original_record_ids') or []);pid=str(context['publication_anchor']['pmid']);cid=context.get('review_context_id');identity=native.identity_function('metaUro')
 if source not in {'MetaVir','metaPee'} or context.get('operation')!='replace' or not ids or not cid:raise ValueError('Row lineage exception requires explicit original IDs and exact source context')
 previous=resolver.load_overlay(uro_root,source);files=[]
 for relative in context.get('source_cache_paths') or []:
  expected=context.get('baseline_files',{}).get(relative)
  if not expected:continue
  source_path=native.ROOT/source/relative;effective=resolver._effective_file(uro_root,source,relative,previous)
  if not source_path.is_file() or native.sha(source_path)!=expected or effective is None:raise ValueError('Lineage source cache unavailable or changed')
  source_rows=json.loads(source_path.read_text()).get('studies',[]);uro_rows=json.loads(effective.read_text()).get('studies',[]);pairs=[]
  for rid in sorted(ids):
   left=[r for r in source_rows if identity(r)==rid];right=[r for r in uro_rows if identity(r)==rid]
   if len(left)!=1 or len(right)!=1:raise ValueError('Original target record ID missing or conflicting in source lineage')
   a,b=left[0],right[0]
   if str(a.get('PMID') or a.get('pmid'))!=pid or str(b.get('PMID') or b.get('pmid'))!=pid or a.get('context_id')!=cid or b.get('context_id')!=cid:raise ValueError('Original target PMID/native context identity differs')
   if human_protected(a) or human_protected(b):raise ValueError('Human-protected source cannot receive a lineage exception')
   if any(str(r.get(k) or '').strip().upper() in {'EXCLUDE','EXCLUDED','PENDING_MANUAL_REVIEW','HUMAN_REVIEW'} for r in (a,b) for k in ('final_status','meta_analysis_status','screening_status')):raise ValueError('Excluded or held source cannot receive a row lineage exception')
   if native.digest(a)!=native.digest(b):raise ValueError('Entire original target row differs between native source and Uro')
   pairs.append({'record_id':rid,'pmid':pid,'native_context_id':cid,'entire_original_row_sha256':native.digest(a),'human_protected':False})
  files.append({'native_cache_path':relative,'native_cache_sha256':expected,'durable_uro_path':str(effective.relative_to(uro_root)),'durable_uro_sha256':native.sha(effective),'exact_target_records':pairs})
 if not files:raise ValueError('No exact source files for row lineage proof')
 gate=bound_gate(context,manifest_path)
 return {'schema_version':1,'policy':'Exact entire saved target-row equivalence; exception only to unrelated full-cache generation differences. Native Uro scientific gates remain authoritative.','native_manifest_sha256':native.sha(manifest_path),'native_context_id':context['context_id'],'review_context_id':cid,'selector':context['selector'],'published_pmid':pid,'original_record_ids':sorted(ids),'source_files':files,'native_criteria_bindings':{p:d for p,d in context.get('baseline_files',{}).items() if p.endswith('.py')},'audit_input_bindings':context.get('audit_input_bindings',{}),'review_receipt_sha256':context['review'].get('review_receipt_sha256'),'preprint_source':context['preprint_source'],'article_gate_record_sha256':native.digest(gate),'native_runtime_admission':'Requires separate actual native Uro eligibility and disposition QA before official release.'}

def create_lineage_proofs(manifest_paths,output,uro_root=None):
 uro_root=Path(uro_root or native.ROOT/'metaUro').resolve();resolver,_=resolver_module();records=[];holds=[]
 for path in map(Path,manifest_paths):
  for context in json.loads(path.read_text()).get('contexts',[]):
   try:records.append(row_lineage_proof(context,path,uro_root,resolver))
   except (ValueError,KeyError,TypeError) as exc:holds.append({'native_context_id':context.get('context_id'),'reason':str(exc)})
 document={'schema_version':1,'records':records,'holds':holds,'model_calls':0};native.save(output,document);return document

def project_context(context,manifest_path,uro_root,resolver,resolver_path,lineage=None):
 context=copy.deepcopy(context);native_context_id=context.get('context_id');selector=context.get('selector') or {};source=selector.get('source_repo')
 if source not in {'MetaVir','metaPee'}:raise ValueError('Uro projection only accepts native MetaVir/metaPee')
 if context.get('review',{}).get('decision')!='include' or context.get('review',{}).get('status') not in {'adjudicated','settled'}:raise ValueError('Unsettled/excluded native context cannot be projected')
 paths=context.get('source_cache_paths')
 if not isinstance(paths,list) or not paths:raise ValueError('No exact native cache bindings')
 previous=resolver.load_overlay(uro_root,source);mapping={};baselines={};original_union=set();ident=native.identity_function('metaUro')
 for relative in paths:
  # Only existing hash-bound native cache aliases can resolve durable Uro data.
  # A missing-file add requires a separate installed source generation first.
  expected=context.get('baseline_files',{}).get(relative)
  if not expected:continue
  effective=resolver._effective_file(uro_root,source,relative,previous)
  if effective is None:raise ValueError('Native source cache is absent from installed Uro generation: '+relative)
  effective=Path(effective).resolve()
  actual=native.sha(effective)
  if not effective.is_relative_to(uro_root.resolve()):raise ValueError('Installed Uro source cache escapes durable root')
  if actual!=expected:
   if lineage is None or native.digest(row_lineage_proof(context,manifest_path,uro_root,resolver))!=native.digest(lineage):raise ValueError('Installed Uro source cache differs from accepted native baseline: '+relative)
  durable=str(effective.relative_to(uro_root.resolve()));mapping[relative]=durable;baselines[durable]=actual
  packet=json.loads(effective.read_text());rows=packet.get('studies',[]) if isinstance(packet,dict) else []
  original_union.update(ident(r) for r in rows if isinstance(r,dict))
 if not mapping:raise ValueError('No installed, byte-identical durable Uro cache; addition remains audit-only')
 originals=set(context.get('original_record_ids') or [])
 if context.get('operation')=='replace' and not originals.issubset(original_union):raise ValueError('Original source record IDs differ in installed Uro cache')
 if context.get('operation')=='add' and originals:raise ValueError('Add context unexpectedly targets original rows')
 for rel in ['updates/manifest.json','cohort_identity.py','automation/sync_sources.py','source_worker.py','preprint_ui.py']:
  p=uro_root/rel
  if p.is_file():baselines[rel]=native.sha(p)
 context['context_id']='preprint-uro:'+native.digest([source,context['context_id'],mapping])[:32]
 context['source_cache_paths']=sorted(set(mapping.values()));context['baseline_files']=baselines
 context['native_source_projection']={'source_repository':source,'manifest_sha256':native.sha(manifest_path),'native_context_id':native_context_id,'source_cache_path_map':mapping,'resolver_path':'automation/sync_sources.py','resolver_sha256':native.sha(resolver_path),'policy':'Installed durable source bytes and exact original record IDs must match accepted native source; native engine remains authoritative.'}
 if lineage:context['native_source_projection'].update(policy='Explicit exact entire-target-row lineage equivalence over separately bound native/Uro cache generations; actual native Uro eligibility QA required.',row_lineage_proof_sha256=native.digest(lineage))
 context['native_rows']=copy.deepcopy(context.get('native_rows') or [])
 for row in context['native_rows']:
  row['preprint_derivative_source_repo']=source
  row['preprint_derivative_source_manifest_sha256']=native.sha(manifest_path)
 return context,bound_gate(context,manifest_path)

def source_diagnostics(context,uro_root,resolver):
 source=(context.get('selector') or {}).get('source_repo');pmid=str((context.get('publication_anchor') or {}).get('pmid'));result={'anchor_pmid':pmid,'native_original_record_ids':context.get('original_record_ids',[]),'source_cache_bindings':[],'native_code_bindings':{p:d for p,d in context.get('baseline_files',{}).items() if p.endswith('.py')}}
 if source not in {'MetaVir','metaPee'}:return result
 previous=resolver.load_overlay(uro_root,source);identity=native.identity_function('metaUro')
 for relative in context.get('source_cache_paths') or []:
  item={'native_cache_path':relative,'accepted_native_sha256':context.get('baseline_files',{}).get(relative)}
  try:
   effective=resolver._effective_file(uro_root,source,relative,previous)
   item.update(durable_uro_path=str(effective.relative_to(uro_root)) if effective else None,durable_uro_sha256=native.sha(effective) if effective else None)
   if effective:
    packet=json.loads(effective.read_text());rows=packet.get('studies',[]) if isinstance(packet,dict) else []
    item['same_anchor_saved_records']=[{'record_id':identity(r),'row':copy.deepcopy(r),'human_protected':bool(r.get('manual_review') or r.get('manual_adjudication') or r.get('human_adjudication') or str(r.get('decision_origin','')).lower() in {'human','manual','human_manual_review'})} for r in rows if isinstance(r,dict) and str(r.get('PMID') or r.get('pmid') or '')==pmid]
  except (ValueError,TypeError,KeyError) as exc:item['error']=str(exc)
  result['source_cache_bindings'].append(item)
 return result

def selector_resolution(contexts,path,output,uro_root):
 records=[json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()];aliases={};proofs=[]
 for record in records:
  actual=record.get('selector') or {}
  for context in contexts:
   expected=context['selector']
   if any(actual.get(k)!=expected.get(k) for k in ('source_repo','disease','outcome','include_subexposures','source_variant')):continue
   if actual.get('_cache_path') not in context['source_cache_paths']:continue
   a,e=actual.get('exposure'),expected.get('exposure')
   if not isinstance(a,str) or not isinstance(e,str) or a==e:continue
   if a.casefold()!=e.casefold():raise ValueError('Only explicitly witnessed case-only canonical selector aliases are supported')
   if a in aliases and aliases[a]!=e:raise ValueError('Conflicting exact canonical selector aliases')
   aliases[a]=e;proof={'actual_selector':actual,'native_selector':expected,'projected_context_id':context['context_id']}
   if proof not in proofs:proofs.append(proof)
 if not aliases:return {}
 document={'schema_version':1,'policy':'Literal canonical case-only aliases witnessed by actual native broker requests with exact durable cache paths; no fuzzy exposure matching.','private_runtime_receipt_sha256':native.sha(path),'resolutions':proofs}
 rel='selector_resolution_proof.json';native.save(output/rel,document)
 for context in contexts:
  context['baseline_files']['updates/preprint_recovery/'+rel]=native.sha(output/rel)
 return {'exposure':aliases}

def bind_native_admission(output,policy_path,qa_manifest_path,bundle_verification=None):
 """Bind actual native broker QA, allowing only manifest-provenance differences."""
 output=Path(output);manifest_path=output/'active_manifest.json';manifest=json.loads(manifest_path.read_text());policy_path=Path(policy_path);policy=json.loads(policy_path.read_text());qa_manifest_path=Path(qa_manifest_path)
 if policy.get('status')!='PASS' or native.sha(qa_manifest_path)!=policy.get('native_gate_manifest_sha256'):raise ValueError('Actual native admission policy/QA manifest is unverified')
 receipt_path=Path(policy['native_gate_receipt']);receipt_path=receipt_path if receipt_path.is_absolute() else native.ROOT/receipt_path;receipt=json.loads(receipt_path.read_text())
 if native.sha(receipt_path)!=policy.get('native_gate_receipt_sha256') or receipt.get('status')!='PASS' or receipt.get('source_mutations') or receipt.get('models')!=0:raise ValueError('Actual native admission receipt changed or source data mutated')
 qa=json.loads(qa_manifest_path.read_text())
 if manifest.get('selector_aliases')!=qa.get('selector_aliases'):raise ValueError('Final Uro selector aliases differ from actual native-tested resolution')
 before={c['context_id']:c for c in qa['contexts']};equivalence=[]
 for context in manifest['contexts']:
  prior=before.get(context['context_id'])
  if prior is None or any(prior.get(k)!=context.get(k) for k in ('selector','original_record_ids','source_cache_paths','preprint_source')):raise ValueError('Final Uro context differs from actual native-tested scope')
  def rows(values):return [{k:v for k,v in row.items() if k!='preprint_derivative_source_manifest_sha256'} for row in values]
  if rows(prior['native_rows'])!=rows(context['native_rows']):raise ValueError('Final Uro scientific rows differ from actual native-tested bundle')
  equivalence.append({'context_id':context['context_id'],'scientific_rows_sha256':native.digest(rows(context['native_rows'])),'allowed_metadata_difference':'Source manifest SHA changed from QA preview to frozen release; all other recovered fields identical.'})
 proof={'schema_version':1,'status':'PASS','native_policy_review':policy,'actual_QA_receipt_sha256':native.sha(receipt_path),'actual_QA_manifest_sha256':native.sha(qa_manifest_path),'frozen_bundle_equivalence':equivalence,'actual_QA_runs':[{k:r.get(k) for k in ('exposure','mode','keep_latest_cohort','include_preprints','http_status','display_rows','recovered_display_rows','recovered_pooled_rows','selected_rows')} for r in receipt.get('runs',[])],'default_source_mutations':receipt.get('source_mutations')}
 if bundle_verification:
  bundle=json.loads(Path(bundle_verification).read_text())
  if bundle.get('status')!='PASS' or {c.get('context_id') for c in bundle.get('checks',[])}!=set(before) or any(c.get('differences') or not any(r.get('Effect ID')==c.get('source_effect_id') for r in before[c['context_id']]['native_rows']) for c in bundle['checks']):raise ValueError('Approved quantitative bundle native verification does not match tested contexts')
  proof['approved_quantitative_bundle_verification']=bundle;proof['approved_quantitative_bundle_verification_sha256']=native.sha(bundle_verification)
 relative='source_lineage/native_admission_verification.json';native.save(output/relative,proof)
 for context in manifest['contexts']:
  context['baseline_files']['updates/preprint_recovery/'+relative]=native.sha(output/relative);context['native_source_projection']['native_admission_verification']={'path':'updates/preprint_recovery/'+relative,'sha256':native.sha(output/relative)}
 native.save(manifest_path,manifest)
 receipt_path=output/'projection_receipt.json'
 if receipt_path.is_file():
  receipt=json.loads(receipt_path.read_text());receipt['before_native_admission_manifest_sha256']=receipt.get('manifest_sha256');receipt['manifest_sha256']=native.sha(manifest_path);receipt['native_admission_verification_sha256']=native.sha(output/relative);receipt['actual_native_QA_receipt_sha256']=policy['native_gate_receipt_sha256'];native.save(receipt_path,receipt)
 return proof

def project_manifests(manifest_paths,output,uro_root=None,reviewed_lineage_proof=None,selector_resolution_proof=None):
 output=Path(output);uro_root=Path(uro_root or native.ROOT/'metaUro').resolve();resolver,resolver_path=resolver_module();contexts=[];audits=[];gates={};protected={}
 lineage_records=[]
 if reviewed_lineage_proof:
  document=json.loads(Path(reviewed_lineage_proof).read_text())
  if document.get('schema_version')!=1 or not isinstance(document.get('records'),list):raise ValueError('Unsupported explicit row lineage proof')
  lineage_records=document['records']
 for path in map(Path,manifest_paths):
  manifest=json.loads(path.read_text())
  if manifest.get('schema_version')!=1 or manifest.get('status')!='complete':raise ValueError('Native manifest is not a complete supported view')
  for context in manifest.get('contexts',[]):
   audit={'native_manifest':str(path),'native_manifest_sha256':native.sha(path),'native_context_id':context.get('context_id'),'selector':context.get('selector'),'activated':False,'source_diagnostics':source_diagnostics(context,uro_root,resolver)}
   try:
    matches=[r for r in lineage_records if r.get('native_context_id')==context.get('context_id') and r.get('native_manifest_sha256')==native.sha(path)]
    if len(matches)>1:raise ValueError('Duplicate explicit row lineage proof')
    lineage=matches[0] if matches else None
    projected,gate=project_context(context,path,uro_root,resolver,resolver_path,lineage)
    if lineage:
     relative='source_lineage/'+native.digest(lineage)+'.json';native.save(output/relative,lineage);projected['baseline_files']['updates/preprint_recovery/'+relative]=native.sha(output/relative);projected['native_source_projection']['row_lineage_proof']={'path':'updates/preprint_recovery/'+relative,'sha256':native.sha(output/relative)}
    contexts.append(projected);gates[str(gate['pmid'])]=gate;audit.update(activated=True,source_cache_paths=projected['source_cache_paths'],explicit_row_lineage=bool(lineage))
    protected.update({str(uro_root/p):d for p,d in projected['baseline_files'].items() if not p.startswith('updates/preprint_recovery/')})
   except (ValueError,KeyError,TypeError) as exc:audit['hold']=str(exc)
   audits.append(audit)
 proof=output/'article_availability_gate.json';native.save(proof,{'schema_version':1,'records':[gates[p] for p in sorted(gates,key=int)]})
 for context in contexts:
  pid=str(context['publication_anchor']['pmid']);context['article_availability_gate']={'path':'updates/preprint_recovery/article_availability_gate.json','sha256':native.sha(proof),'pmid':pid,'record_sha256':native.digest(gates[pid])}
 ids=[c['context_id'] for c in contexts]
 if len(ids)!=len(set(ids)):raise ValueError('Duplicate native source/version cells in Uro projection')
 manifest={'schema_version':1,'repository':'metaUro','status':'complete','generation_id':'preprint-recovery-2026-10-26-v1','qa_only':any(json.loads(Path(p).read_text()).get('qa_only') for p in manifest_paths),'contexts':contexts}
 if selector_resolution_proof:manifest['selector_aliases']=selector_resolution(contexts,selector_resolution_proof,output,uro_root)
 native.save(output/'active_manifest.json',manifest)
 changed=[p for p,d in protected.items() if not Path(p).is_file() or native.sha(p)!=d]
 if changed:raise ValueError('Durable Uro evidence changed during read-only projection')
 receipt={'schema_version':1,'producer_sha256':native.sha(__file__),'model_calls':0,'new_cost_usd':0,'contexts':len(contexts),'rows':sum(len(c['native_rows']) for c in contexts),'source_artifacts_checked':len(protected),'source_artifacts_changed':changed,'manifest_sha256':native.sha(output/'active_manifest.json'),'contexts_audit':audits}
 native.save(output/'projection_receipt.json',receipt);return receipt

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--native-manifest',action='append',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--reviewed-lineage-proof',type=Path);parser.add_argument('--selector-resolution-proof',type=Path);parser.add_argument('--native-admission-policy',type=Path);parser.add_argument('--native-tested-manifest',type=Path);parser.add_argument('--native-bundle-verification',type=Path);parser.add_argument('--create-lineage-proof',action='store_true');args=parser.parse_args()
 if args.create_lineage_proof:
  result=create_lineage_proofs(args.native_manifest,args.output);print(json.dumps({'records':len(result['records']),'holds':result['holds']}))
 else:
  receipt=project_manifests(args.native_manifest,args.output,reviewed_lineage_proof=args.reviewed_lineage_proof,selector_resolution_proof=args.selector_resolution_proof)
  if bool(args.native_admission_policy)!=bool(args.native_tested_manifest):parser.error('Both native-admission-policy and native-tested-manifest are required together')
  if args.native_admission_policy:bind_native_admission(args.output,args.native_admission_policy,args.native_tested_manifest,args.native_bundle_verification);receipt=json.loads((args.output/'projection_receipt.json').read_text())
  print(json.dumps({k:v for k,v in receipt.items() if k!='contexts_audit'},indent=2))
