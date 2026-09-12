"""Stage the targeted EXCLUDE/ENP migration from immutable saved source tracks."""
from __future__ import annotations
import argparse,copy,csv,hashlib,io,json,os,re,sys,warnings
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import key_comparison_policy as policy
from scripts import reconcile_screening_tracks as reconcile

VERSION='nonpoolable-status-reconciliation-2026-09-08-v1'
REASON='EXCLUDE versus ELIGIBLE_NOT_POOLABLE resolves to eligible, not poolable; no manual comparison is required.'

def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 def clean(v):
  if isinstance(v,dict):return {k:clean(x) for k,x in v.items()}
  if isinstance(v,(list,tuple)):return [clean(x) for x in v]
  if hasattr(v,'item'):return clean(v.item())
  if isinstance(v,float) and not __import__('math').isfinite(v):return None
  return v
 path.write_text(json.dumps(clean(value),ensure_ascii=False,indent=2,allow_nan=False)+'\n')

def selected(row):
 return row.get('policy_poolable') is True and row.get('Primary Estimate') is not False and row.get('subexposure_pooling_selected') is not False and row.get('disease_hierarchy_pooling_selected') is not False

def refresh_metadata(source,output,report):
 """Refresh existing view counts using only this migration's selected-row delta."""
 source=Path(source);output=Path(output);changed=[];deltas={}
 for view_root in ['combined','combined/provisional_ui']:
  for mode in ['yes','no']:
   prefix=f'{view_root}/subexposure_views/{mode}'
   delta=sum(r['removed_selected_rows'] for r in report['affected_pooled_scopes'] if r['cache'].startswith(prefix+'/Cached_results/') and r['cache'].endswith('_true_all.json') and '_disease_hierarchy_sources' not in r['cache'])
   deltas[(view_root,mode)]=delta
   if not delta:continue
   for suffix in ['manifest.json','Cached_results/_subexposure_manifest.json']:
    relative=f'{prefix}/{suffix}'
    if not (source/relative).exists():continue
    packet=read(source/relative)
    if isinstance(packet.get('counts'),dict) and 'pooling_rows' in packet['counts']:
     packet['counts']['pooling_rows']-=delta
    packet.update(nonpoolable_status_policy=VERSION,nonpoolable_removed_selected_rows=delta)
    write(output/relative,packet);changed.append(relative)
 relative='combined/provisional_ui/manifest.json';packet=read(output/relative)
 original=read(source/relative)
 packet['frozen_pending_rows']=report['after_rows']-int(original.get('supplemental_pending_rows') or 0)
 packet['automatic_nonpoolable_review_rows_resolved']=len(report['removed_review_ids'])
 audit=read(output/'combined/provisional_ui/review_audit.json')
 if isinstance(audit,list):packet['reason_counts']=dict(Counter(row.get('reason') or (row.get('decision') or {}).get('reason') or '' for row in audit))
 for mode in ['yes','no']:
  if isinstance(packet.get('subexposure_modes',{}).get(mode),dict):
   packet['subexposure_modes'][mode]['pooling_rows']=original['subexposure_modes'][mode]['pooling_rows']-deltas[('combined/provisional_ui',mode)]
  path=output/f'combined/provisional_ui/subexposure_views/{mode}/Plot/summary_manifest.json'
  if path.exists():
   scopes=read(path)['scopes'];pdfs={name for scope in scopes.values() for plot in scope['plots'].values() for name in plot.get('locales',{}).values()}
   packet.setdefault('summary_modes',{})[mode]={'scopes':len(scopes),'pdfs':len(pdfs)}
 delta=sum(r['removed_selected_rows'] for r in report['affected_pooled_scopes'] if len(Path(r['cache']).parts)==4 and r['cache'].startswith('combined/provisional_ui/') and r['cache'].endswith('_true_all.json'))
 if isinstance(packet.get('materialization'),dict):packet['materialization']['pooling_rows']=original['materialization']['pooling_rows']-delta
 write(output/relative,packet);changed.append(relative)
 report['updated_metadata_files']=changed
 return report

def mark(row,resolution,*,pending=False):
 result=copy.deepcopy(row)
 fields={'final_status':'ELIGIBLE_NOT_POOLABLE','scientific_pooling_eligible':False,'policy_poolable':False,
         'Primary Estimate':False,'subexposure_pooling_selected':False,'display_only':True,
         'requires_key_review':pending,'manual_review_pending':pending}
 if 'disease_hierarchy_pooling_selected' in result:fields['disease_hierarchy_pooling_selected']=False
 if 'smoking_pooling_selected' in result:fields['smoking_pooling_selected']=False
 result.setdefault('nonpoolable_status_previous',{key:copy.deepcopy(result.get(key)) for key in fields})
 result.update(fields,nonpoolable_status_resolution=copy.deepcopy(resolution),display_only_reason=REASON)
 # These snapshots are normative inputs to later projection, rather than history.
 for key in ['disease_scope_original_row','disease_hierarchy_original_row','subexposure_original_row','disease_hierarchy_original_study','source_gate_snapshot']:
  if isinstance(result.get(key),dict):result[key]=mark(result[key],resolution,pending=pending)
 for key in ['provisional_ui_candidates','combined_accepted_estimate_sources']:
  if isinstance(result.get(key),list):
   result[key]=[mark(item,resolution,pending=pending) if isinstance(item,dict) else item for item in result[key]]
 return result

def main(source_root,output_root,recalculate=False):
 source=Path(source_root).resolve();output=Path(output_root).resolve()
 if source==output or source in output.parents:raise ValueError('Use a separate staging directory.')
 alias=source.name
 source_dir=source/reconcile.SOURCE_DIR.relative_to(reconcile.ROOT)
 files={track:source_dir/(track+'_effective_results.json') for track in ['chatgpt','claude_gemini']}
 hashes={str(p.relative_to(source)):sha(p) for p in files.values()}
 tracks={t:{r['context_id']:r for r in read(p)['results']} for t,p in files.items()}
 canonical_paths=[source/'combined/effective_results.json',source/'combined/provisional_ui/effective_results.json']
 current={}
 for path in canonical_paths:
  if path.exists():
   hashes[str(path.relative_to(source))]=sha(path)
   for row in read(path).get('results',[]):current.setdefault(row['context_id'],[]).append(row)
 csv_source=source/f'combined/human_review/{alias}_human_manual_review_2026-09-07.csv'
 with csv_source.open(encoding='utf-8-sig',newline='') as stream:reader=csv.DictReader(stream);columns=reader.fieldnames;reviews=list(reader)
 hashes[str(csv_source.relative_to(source))]=sha(csv_source)
 human_contexts={r.get('Context ID') for r in reviews if r.get('Manual review results','').strip()}
 resolutions={};protected=[]
 for cid in sorted(set(tracks['chatgpt'])&set(tracks['claude_gemini'])):
  source_pair=[tracks[track][cid] for track in ['chatgpt','claude_gemini']]
  def status(row):
   eligibility=row.get('eligibility')
   if isinstance(eligibility,dict):eligibility=eligibility.get('decision')
   fallback='EXCLUDE' if str(eligibility or '').strip().upper()=='EXCLUDE' else ''
   return str(row.get('final_status') or row.get('full_text_status') or fallback).strip().upper()
  if set(map(status,source_pair))=={'EXCLUDE','ELIGIBLE_NOT_POOLABLE'} and any(reconcile.manual(row) for row in source_pair):
   protected.append(cid);continue
  resolution=policy.nonpoolable_status_resolution(*source_pair)
  if not resolution:continue
  if cid in human_contexts or any(reconcile.manual(row) for row in current.get(cid,[])):
   protected.append(cid);continue
  resolutions[cid]=resolution
 supplemental=set()
 central=source.parent/'screening_status_reports/provisional_human_review_2026-09-07/delivery_manifest.json'
 if central.exists():supplemental=set(read(central).get('supplemental_review_ids',[]))
 removed=[r for r in reviews if r.get('Context ID') in resolutions and r['Review ID'] not in supplemental and not r.get('Manual review results','').strip()]
 removed_ids={r['Review ID'] for r in removed};remaining=[r for r in reviews if r['Review ID'] not in removed_ids]
 independent={r.get('Context ID') for r in remaining if r.get('Context ID') in resolutions}
 report={'version':VERSION,'alias':alias,'folder':alias,'source_files_sha256':hashes,
  'resolved_contexts':sorted(resolutions),'resolutions':resolutions,'protected_human_contexts':protected,
  'removed_review_ids':sorted(removed_ids),'before_rows':len(reviews),'after_rows':len(remaining),
  'before_articles':len({r['PMID'] for r in reviews}),'after_articles':len({r['PMID'] for r in remaining}),
  'staged_csv_relative_path':f'combined/human_review/{alias}_human_manual_review_2026-09-08.csv',
  'affected_pooled_scopes':[],'changed_cache_files':[],'model_calls':0}
 # Preserve every cell of retained human-review rows, including entered decisions.
 csv_target=output/report['staged_csv_relative_path'];csv_target.parent.mkdir(parents=True,exist_ok=True)
 with csv_target.open('w',encoding='utf-8-sig',newline='') as stream:writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(remaining)
 status=read(source/'combined/human_review/review_status.json')
 status.update(file=csv_target.name,csv_sha256=sha(csv_target),articles=report['after_articles'],review_rows=len(remaining),
               status='Human review pending' if remaining else 'No pending human comparison review',
               temporary_pipeline_counts=dict(Counter(r.get('Temporary UI pipeline','') for r in remaining)),
               temporary_pooling_rows=sum(r.get('Temporary pooling eligible')=='Yes' for r in remaining),reconciliation_policy_version=VERSION)
 write(output/'combined/human_review/review_status.json',status)
 for path in canonical_paths:
  if not path.exists():continue
  packet=read(path);changed=False
  for i,row in enumerate(packet.get('results',[])):
   cid=row.get('context_id')
   if cid not in resolutions:continue
   resolution=resolutions[cid];retained=tracks[resolution['retained_track']][cid]
   updated=mark(row,resolution,pending=cid in independent)
   updated.update(eligibility='INCLUDE',full_text_status='ELIGIBLE_NOT_POOLABLE',combined_numeric_consensus=False,combined_numeric_resolved=False,poolability_reason=REASON)
   if not row.get('extraction'):
    updated['extraction']=copy.deepcopy(retained.get('extraction'));updated['additional_extractions']=copy.deepcopy(retained.get('additional_extractions') or [])
   updated['key_review_candidates']=[]
   updated['provisional_ui_review_ids']=[x for x in row.get('provisional_ui_review_ids',[]) if x not in removed_ids]
   packet['results'][i]=updated;changed=True
  if changed:write(output/path.relative_to(source),packet)
 # Refresh the local queue metadata while the central package is updated separately.
 ui=source/'combined/provisional_ui'
 for filename in ['pending_manifest.json','review_audit.json']:
  path=ui/filename
  if not path.exists():continue
  packet=read(path)
  if isinstance(packet,list):packet=[r for r in packet if r.get('id') not in removed_ids]
  elif isinstance(packet.get('rows'),list):packet['rows']=[r for r in packet['rows'] if r.get('id') not in removed_ids]
  write(output/path.relative_to(source),packet)
 cache_paths=set()
 for folder in [source/'combined',ui]:cache_paths.update(folder.glob('*/*_true_*.json'))
 for view_root in [source/'combined',ui]:
  for mode in ['yes','no']:
   folder=view_root/'subexposure_views'/mode/'Cached_results'
   cache_paths.update(folder.rglob('*_true_*.json'))
 calculations={}
 for path in sorted(cache_paths):
  packet=read(path);rows=packet.get('studies')
  if not isinstance(rows,list):continue
  affected=[row for row in rows if row.get('context_id') in resolutions]
  if not affected:continue
  pooled_before=[row for row in affected if selected(row)]
  revised=[mark(row,resolutions[row['context_id']],pending=row['context_id'] in independent) if row.get('context_id') in resolutions else row for row in rows]
  updated=copy.deepcopy(packet);updated['studies']=revised;updated['nonpoolable_status_policy']=VERSION
  if isinstance(updated.get('subexposure_projection'),dict):updated['subexposure_projection']['pooling_rows']=sum(selected(row) for row in revised)
  if pooled_before:
   scope={'cache':str(path.relative_to(source)),'disease':packet.get('disease') or packet.get('cancer'),'exposure':packet.get('exposure'),
          'removed_pmids':[r.get('PMID') for r in pooled_before],'removed_selected_rows':len(pooled_before),'old_headline':packet.get('headline')}
   if recalculate:
    import pandas as pd
    import meta_analysis
    pool=[row for row in revised if selected(row)]
    signature=hashlib.sha256(json.dumps([scope['disease'],scope['exposure'],packet.get('endpoint') or packet.get('outcome'),pool,packet.get('disease_hierarchy_prebuilt')],sort_keys=True,default=str).encode()).hexdigest()
    if signature not in calculations:
     if pool:
      with redirect_stdout(io.StringIO()),warnings.catch_warnings():
       warnings.simplefilter('ignore')
       if packet.get('disease_hierarchy_prebuilt'):
        from scripts.provisional_disease_scopes import analyze_parent
        result=analyze_parent(pd.DataFrame(pool),pd.DataFrame(revised),scope['disease'],scope['exposure'],packet.get('endpoint') or packet.get('outcome') or 'Incidence')['result']
       else:result=meta_analysis.perform_meta_analysis(pd.DataFrame(pool),scope['disease'],scope['exposure'],outcome=packet.get('endpoint') or packet.get('outcome') or 'Incidence',exclude_meta=True,df_all=pd.DataFrame(revised),generate_plots=False)
      if result.get('error'):raise ValueError((str(path),result['error']))
     else:result={'headline':None,'summary_html':'<p>No studies meet the default pooling criteria. Eligible evidence remains listed below.</p>'}
     calculations[signature]=result
    result=calculations[signature]
    for key in ['headline','summary_html','screening_stats']:
     if key in result:updated[key]=copy.deepcopy(result[key])
    for key in ['plot_url','funnel_plot_url','baujat_plot_url']:updated[key]=None
    # Main plots are generated on demand from this exact saved view and its new generation hash.
    if isinstance(updated.get('forest_plot'),dict):updated['forest_plot']['url']=None
    scope['new_headline']=updated.get('headline')
   report['affected_pooled_scopes'].append(scope)
  write(output/path.relative_to(source),updated);report['changed_cache_files'].append(str(path.relative_to(source)))
 manifest=read(ui/'manifest.json')
 for relative in manifest.get('protected_source_hashes',{}):
  migrated=output/relative
  if migrated.exists():manifest['protected_source_hashes'][relative]=sha(migrated)
 manifest.update(nonpoolable_status_policy=VERSION,pending_rows=len(remaining),pending_manifest_sha256=sha(output/'combined/provisional_ui/pending_manifest.json'))
 write(output/'combined/provisional_ui/manifest.json',manifest)
 refresh_metadata(source,output,report)
 assert all(sha(source/relative)==digest for relative,digest in hashes.items())
 report['source_hashes_verified_unchanged']=True
 prior_report=output/'resolution_report.json'
 if prior_report.exists():
  prior=read(prior_report)
  for key in ['summary_rebuild','canonical_summary_rebuild','deleted_paths']:
   if key in prior:report[key]=prior[key]
 write(output/'resolution_report.json',report)
 print(json.dumps({k:report[k] for k in ['alias','before_rows','after_rows','before_articles','after_articles']},indent=2))
 print('resolved contexts',len(resolutions),'cache files',len(report['changed_cache_files']),'affected pooled files',len(report['affected_pooled_scopes']),flush=True)
 return report

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--source-root',type=Path,required=True);parser.add_argument('--output-root',type=Path,required=True);parser.add_argument('--recalculate',action='store_true');args=parser.parse_args()
 main(args.source_root,args.output_root,args.recalculate)
