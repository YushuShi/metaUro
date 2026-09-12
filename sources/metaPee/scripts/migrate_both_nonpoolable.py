"""Stage atomic single-source display retention for saved ENP/ENP contexts."""
from __future__ import annotations
import argparse,copy,csv,hashlib,io,json,sys,warnings
from collections import Counter,defaultdict
from contextlib import redirect_stdout
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import key_comparison_policy as policy
from scripts import reconcile_screening_tracks as reconcile
from scripts import nonpoolable_display_selection as selection
from scripts import subexposure_policy as sub
from scripts.migrate_nonpoolable_status_rule import read,write,sha,selected

VERSION=selection.VERSION
KIND='both_eligible_not_poolable'
REASON='Both pipelines classify this study as eligible, not poolable. The more informative single-pipeline record is retained for display only; no manual comparison or pooling is required.'
SNAPSHOTS={'disease_scope_original_row','disease_hierarchy_original_row','subexposure_original_row','disease_hierarchy_original_study','source_gate_snapshot'}
ROUTING={'Displayed Exposure','Displayed Disease','Disease Evidence Source','include_subexposures',
 'subexposure_source','subexposure_inherited','disease_hierarchy_inherited','disease_hierarchy_source_key',
 'disease_hierarchy_source_path','disease_hierarchy_reference_basis','disease_hierarchy_source_context_excluded',
 'disease_hierarchy_parent_context_excluded'}
SELECTION_FIELDS={'subexposure_pooling_selected','subexposure_pooling_reason','disease_hierarchy_pooling_selected','disease_hierarchy_pooling_reason'}

def held(row,resolution,pending=False):
 result=copy.deepcopy(row)
 result.update(final_status='ELIGIBLE_NOT_POOLABLE',scientific_pooling_eligible=False,policy_poolable=False,
  **{'Primary Estimate':False,'subexposure_pooling_selected':False,'display_only':True,
     'requires_key_review':pending,'manual_review_pending':pending,
     'nonpoolable_status_resolution':copy.deepcopy(resolution),'display_only_reason':REASON})
 if 'disease_hierarchy_pooling_selected' in result:result['disease_hierarchy_pooling_selected']=False
 if 'smoking_pooling_selected' in result:result['smoking_pooling_selected']=False
 return result

def display_rows(fresh,old_rows,resolution,pending):
 """Carry scope routing only; every scientific/display fact comes from one source."""
 template=old_rows[0];output=[]
 for raw in fresh:
  base=held(raw,resolution,pending);row=copy.deepcopy(base)
  row.update({key:copy.deepcopy(value) for key,value in template.items() if key in ROUTING})
  if 'subexposure_projection_id' in template:
   row['subexposure_projection_id']=sub.row_identity(row,row.get('subexposure_source') or row.get('Direct Exposure'))
  if 'disease_hierarchy_projection_id' in template:
   import disease_hierarchy
   row['disease_hierarchy_projection_id']=disease_hierarchy._identity(row,row.get('disease_hierarchy_source_key'))
   row['disease_hierarchy_pooling_selected']=False
   row['disease_hierarchy_pooling_reason']=REASON
  for key in SNAPSHOTS:
   if isinstance(template.get(key),dict):row[key]=copy.deepcopy(base)
  output.append(row)
 return output

def reelect(rows,packet,hierarchy):
 """Use the established election, copying only its selection flags/reasons."""
 target=packet.get('exposure');sources=defaultdict(list)
 for row in rows:sources[row.get('subexposure_source') or sub.row_direct_exposure(row,target)].append(row)
 mode=packet.get('include_subexposures') or 'no'
 elected=sub.project_rows(sources,target,hierarchy,include=mode)
 choices={sub.row_identity(row,row.get('subexposure_source')):row for row in elected}
 revised=copy.deepcopy(rows)
 for row in revised:
  choice=choices.get(sub.row_identity(row,row.get('subexposure_source') or sub.row_direct_exposure(row,target)))
  if choice is not None and row.get('subexposure_pooling_selected')!=choice.get('subexposure_pooling_selected'):
   row['subexposure_pooling_selected']=choice['subexposure_pooling_selected']
   if 'subexposure_pooling_reason' in choice:row['subexposure_pooling_reason']=choice['subexposure_pooling_reason']
   else:row.pop('subexposure_pooling_reason',None)
 if any(row.get('disease_hierarchy_projection_id') for row in revised):
  import disease_hierarchy
  for row,choice in zip(revised,disease_hierarchy.select_rows(revised)):
   if row.get('disease_hierarchy_pooling_selected')!=choice.get('disease_hierarchy_pooling_selected'):
    for key in ['disease_hierarchy_pooling_selected','disease_hierarchy_pooling_reason']:
     if key in choice:row[key]=choice[key]
 return revised

def refresh_metadata(source,output,report):
 changed=[]
 def deltas(prefix,base_only=False):
  paths=[r for r in report['cache_changes'] if r['cache'].startswith(prefix) and r['cache'].endswith('_true_all.json')]
  if base_only:paths=[r for r in paths if len(Path(r['cache']).parts)==len(Path(prefix).parts)+2]
  else:paths=[r for r in paths if not any(part.startswith('_') for part in Path(r['cache']).parts)]
  return sum(r['row_count_delta'] for r in paths),sum(r['pooling_delta'] for r in paths)
 for view_root in ['combined','combined/provisional_ui']:
  for mode in ['yes','no']:
   prefix=f'{view_root}/subexposure_views/{mode}';visible,pooling=deltas(prefix+'/Cached_results/')
   for suffix in ['manifest.json','Cached_results/_subexposure_manifest.json']:
    relative=f'{prefix}/{suffix}'
    if not (source/relative).exists():continue
    packet=read(source/relative)
    if isinstance(packet.get('counts'),dict):
     for key,delta in [('visible_rows',visible),('pooling_rows',pooling)]:
      if key in packet['counts']:packet['counts'][key]+=delta
    packet.update(both_nonpoolable_display_policy=VERSION,both_nonpoolable_visible_delta=visible,both_nonpoolable_pooling_delta=pooling)
    write(output/relative,packet);changed.append(relative)
 relative='combined/provisional_ui/manifest.json';packet=read(source/relative)
 for path,digest in packet['protected_source_hashes'].items():
  if (output/path).exists():packet['protected_source_hashes'][path]=sha(output/path)
 packet['pending_rows']=report['after_rows']
 packet['frozen_pending_rows']=report['after_rows']-int(packet.get('supplemental_pending_rows') or 0)
 packet['pending_manifest_sha256']=sha(output/'combined/provisional_ui/pending_manifest.json')
 packet['both_nonpoolable_display_policy']=VERSION
 packet['both_nonpoolable_review_rows_resolved']=len(report['removed_review_ids'])
 audit=read(output/'combined/provisional_ui/review_audit.json')
 if isinstance(audit,list):packet['reason_counts']=dict(Counter(row.get('reason') or '' for row in audit))
 for mode in ['yes','no']:
  visible,pooling=deltas(f'combined/provisional_ui/subexposure_views/{mode}/Cached_results/')
  if mode in packet.get('subexposure_modes',{}):
   packet['subexposure_modes'][mode]['visible_rows']+=visible;packet['subexposure_modes'][mode]['pooling_rows']+=pooling
  path=output/f'combined/provisional_ui/subexposure_views/{mode}/Plot/summary_manifest.json'
  if path.exists():
   scopes=read(path)['scopes'];pdfs={name for scope in scopes.values() for plot in scope['plots'].values() for name in plot.get('locales',{}).values()}
   packet.setdefault('summary_modes',{})[mode]={'scopes':len(scopes),'pdfs':len(pdfs)}
 visible,pooling=deltas('combined/provisional_ui/',base_only=True)
 if 'materialization' in packet:
  packet['materialization']['displayed_rows']+=visible;packet['materialization']['pooling_rows']+=pooling
 write(output/relative,packet);changed.append(relative)
 # Disease unions retain checksums of their direct source snapshots. Refresh
 # only checksums and counts whose source files changed in this migration.
 changes={row['cache']:row for row in report['cache_changes']}
 for view_root in ['combined','combined/provisional_ui']:
  for mode in ['yes','no']:
   prefix=f'{view_root}/subexposure_views/{mode}/Cached_results';root=source/prefix;staged=output/prefix
   def update_hashes(metadata):
    hashes=metadata.get('source_hashes');updated=False
    if not isinstance(hashes,dict):return False
    for name,digest in list(hashes.items()):
     path=staged/name
     if path.exists():
      current=sha(path)
      if current!=digest:hashes[name]=current;updated=True
    if updated:
     encoded=(json.dumps(hashes,sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2)+'\n').encode()
     metadata['source_fingerprint']=hashlib.sha256(encoded).hexdigest()
    return updated
   for path in root.glob('*/*_true_*.json'):
    relative=str(path.relative_to(source));target=output/relative;packet=read(target if target.exists() else path)
    metadata=packet.get('disease_hierarchy_materialization')
    if isinstance(metadata,dict) and update_hashes(metadata):
     write(target,packet);changed.append(relative)
   path=root/'_disease_scope_manifest.json'
   if not path.exists():continue
   packet=read(path);touched=update_hashes(packet)
   for parent in packet.get('parents',{}).values():
    rows=[changes[f'{prefix}/{name}'] for name in parent.get('files',[]) if name.endswith('_true_all.json') and f'{prefix}/{name}' in changes]
    if rows:
     parent['visible_rows']+=sum(r['row_count_delta'] for r in rows)
     parent['selected_rows']+=sum(r['pooling_delta'] for r in rows);touched=True
   if touched:
    packet['visible_rows']=sum(p['visible_rows'] for p in packet['parents'].values())
    packet['selected_rows']=sum(p['selected_rows'] for p in packet['parents'].values())
    packet['both_nonpoolable_display_policy']=VERSION
    relative=str(path.relative_to(source));write(output/relative,packet);changed.append(relative)
 report['updated_metadata_files']=changed

def main(source_root,output_root,recalculate=False):
 source=Path(source_root).resolve();output=Path(output_root).resolve()
 if source==output or source in output.parents:raise ValueError('Use separate staging.')
 alias=source.name;source_dir=source/reconcile.SOURCE_DIR.relative_to(reconcile.ROOT)
 files={track:source_dir/(track+'_effective_results.json') for track in ['chatgpt','claude_gemini']}
 hashes={str(path.relative_to(source)):sha(path) for path in files.values()}
 tracks={track:{r['context_id']:r for r in read(path)['results']} for track,path in files.items()}
 canonical_paths=[source/'combined/effective_results.json',source/'combined/provisional_ui/effective_results.json'];current={}
 for path in canonical_paths:
  hashes[str(path.relative_to(source))]=sha(path)
  for row in read(path)['results']:current.setdefault(row['context_id'],[]).append(row)
 status=read(source/'combined/human_review/review_status.json');csv_source=source/'combined/human_review'/status['file']
 with csv_source.open(encoding='utf-8-sig',newline='') as stream:
  reader=csv.DictReader(stream);columns=reader.fieldnames;reviews=list(reader)
 hashes[str(csv_source.relative_to(source))]=sha(csv_source)
 human_contexts={r['Context ID'] for r in reviews if r['Manual review results'].strip()};resolutions={};protected=[]
 for cid in sorted(set(tracks['chatgpt'])&set(tracks['claude_gemini'])):
  pair=[tracks[t][cid] for t in tracks]
  if any(str(row.get('final_status') or row.get('full_text_status') or '').upper()!='ELIGIBLE_NOT_POOLABLE' for row in pair):continue
  if cid in human_contexts or any(reconcile.manual(row) for row in pair+current.get(cid,[])):protected.append(cid);continue
  resolution=policy.nonpoolable_status_resolution(*pair)
  assert resolution and resolution.get('resolution_kind')==KIND
  resolutions[cid]=resolution
 central=source.parent/'screening_status_reports/provisional_human_review_2026-09-07/delivery_manifest.json'
 supplemental=set(read(central).get('supplemental_review_ids',[])) if central.exists() else set()
 removed=[r for r in reviews if r['Context ID'] in resolutions and r['Review ID'] not in supplemental and not r['Manual review results'].strip()]
 removed_ids={r['Review ID'] for r in removed};remaining=[r for r in reviews if r['Review ID'] not in removed_ids]
 independent={r['Context ID'] for r in remaining if r['Context ID'] in resolutions}
 report={'version':VERSION,'alias':alias,'folder':alias,'source_files_sha256':hashes,'resolved_contexts':sorted(resolutions),
  'resolutions':resolutions,'protected_human_contexts':protected,'removed_review_ids':sorted(removed_ids),
  'before_rows':len(reviews),'after_rows':len(remaining),'before_articles':len({r['PMID'] for r in reviews}),
  'after_articles':len({r['PMID'] for r in remaining}),'staged_csv_relative_path':f'combined/human_review/{alias}_human_manual_review_2026-09-08_both_nonpoolable.csv',
  'retained_track_counts':dict(Counter(r['retained_track'] for r in resolutions.values())),
  'affected_pooled_scopes':[],'cache_changes':[],'changed_cache_files':[],'selection_flag_changes':[],'model_calls':0}
 csv_target=output/report['staged_csv_relative_path'];csv_target.parent.mkdir(parents=True,exist_ok=True)
 with csv_target.open('w',encoding='utf-8-sig',newline='') as stream:
  writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(remaining)
 status.update(file=csv_target.name,csv_sha256=sha(csv_target),articles=report['after_articles'],review_rows=len(remaining),
  temporary_pipeline_counts=dict(Counter(r.get('Temporary UI pipeline','') for r in remaining)),
  temporary_pooling_rows=sum(r.get('Temporary pooling eligible')=='Yes' for r in remaining),both_nonpoolable_display_policy=VERSION)
 write(output/'combined/human_review/review_status.json',status)
 records={};fresh={}
 for cid,resolution in resolutions.items():
  record,candidates,issues=reconcile.reconcile_context(tracks['chatgpt'][cid],tracks['claude_gemini'][cid]);assert not issues
  records[cid]=held(record,resolution,cid in independent)
  fresh[cid]=[held(reconcile.app_row(item),resolution,cid in independent) for item in candidates]
  assert fresh[cid],cid
 for path in canonical_paths:
  packet=read(path)
  for i,row in enumerate(packet['results']):
   cid=row.get('context_id')
   if cid not in resolutions:continue
   revised=copy.deepcopy(records[cid])
   previous=row.get('prior_automated_adjudication') if row.get('nonpoolable_status_resolution')==resolutions[cid] else row
   if previous is not None:revised['prior_automated_adjudication']=copy.deepcopy(previous)
   revised['provisional_ui_review_ids']=[value for value in row.get('provisional_ui_review_ids',[]) if value not in removed_ids]
   packet['results'][i]=revised
  write(output/path.relative_to(source),packet)
 for filename in ['pending_manifest.json','review_audit.json']:
  path=source/'combined/provisional_ui'/filename;packet=read(path)
  if isinstance(packet,list):packet=[r for r in packet if r.get('id') not in removed_ids]
  elif isinstance(packet.get('rows'),list):packet['rows']=[r for r in packet['rows'] if r.get('id') not in removed_ids]
  write(output/path.relative_to(source),packet)
 cache_paths=set()
 for root in [source/'combined',source/'combined/provisional_ui']:
  cache_paths.update(root.glob('*/*_true_*.json'))
  for mode in ['yes','no']:cache_paths.update((root/'subexposure_views'/mode/'Cached_results').rglob('*_true_*.json'))
 hierarchy=read(source/'data/exposure_hierarchy.json');calculations={}
 for path in sorted(cache_paths):
  packet=read(path);rows=packet.get('studies')
  if not isinstance(rows,list):continue
  affected={row['context_id'] for row in rows if row.get('context_id') in resolutions}
  if not affected:continue
  previous={cid:[r for r in rows if r.get('context_id')==cid] for cid in affected}
  revised=[];seen=set()
  for row in rows:
   cid=row.get('context_id')
   if cid not in affected:revised.append(copy.deepcopy(row))
   elif cid not in seen:
    seen.add(cid);revised.extend(display_rows(fresh[cid],previous[cid],resolutions[cid],cid in independent))
  pooled_before=[row for row in rows if row.get('context_id') in affected and selected(row)]
  if pooled_before:
   revised=reelect(revised,packet,hierarchy)
   prior_by_id={r.get('Record ID'):r for r in rows if r.get('context_id') not in affected}
   for row in revised:
    if row.get('context_id') in affected:continue
    old=prior_by_id[row.get('Record ID')]
    changes={key:{'before':old.get(key),'after':row.get(key)} for key in SELECTION_FIELDS if old.get(key)!=row.get(key)}
    if changes:report['selection_flag_changes'].append({'cache':str(path.relative_to(source)),'context_id':row.get('context_id'),'record_id':row.get('Record ID'),'changes':changes})
  updated=copy.deepcopy(packet);updated['studies']=revised;updated['both_nonpoolable_display_policy']=VERSION
  updated['requires_key_review']=any(r.get('requires_key_review') for r in revised)
  old_count=sum(selected(row) for row in rows);new_count=sum(selected(row) for row in revised)
  if isinstance(updated.get('subexposure_projection'),dict):updated['subexposure_projection'].update(table_rows=len(revised),pooling_rows=new_count)
  change={'cache':str(path.relative_to(source)),'row_count_delta':len(revised)-len(rows),'pooling_delta':new_count-old_count,'target_contexts':sorted(affected)}
  report['cache_changes'].append(change)
  if pooled_before:
   scope={'cache':str(path.relative_to(source)),'disease':packet.get('disease') or packet.get('cancer'),'exposure':packet.get('exposure'),
    'removed_pmids':[r.get('PMID') for r in pooled_before],'removed_selected_rows':len(pooled_before),'old_selected_rows':old_count,'new_selected_rows':new_count,'old_headline':packet.get('headline')}
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
    if isinstance(updated.get('forest_plot'),dict):updated['forest_plot']['url']=None
    scope['new_headline']=updated.get('headline')
   report['affected_pooled_scopes'].append(scope)
  assert all(not selected(r) and r['scientific_pooling_eligible'] is False for r in revised if r.get('context_id') in affected)
  write(output/path.relative_to(source),updated);report['changed_cache_files'].append(str(path.relative_to(source)))
 refresh_metadata(source,output,report)
 assert all(sha(source/relative)==digest for relative,digest in hashes.items())
 report['source_hashes_verified_unchanged']=True;write(output/'resolution_report.json',report)
 print(json.dumps({k:report[k] for k in ['alias','before_rows','after_rows','before_articles','after_articles','retained_track_counts']},indent=2))
 print('resolved contexts',len(resolutions),'cache files',len(report['changed_cache_files']),'pooled files',len(report['affected_pooled_scopes']),'alternate selections',len(report['selection_flag_changes']),flush=True)
 return report

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--source-root',type=Path,required=True);parser.add_argument('--output-root',type=Path,required=True);parser.add_argument('--recalculate',action='store_true');args=parser.parse_args()
 main(args.source_root,args.output_root,args.recalculate)
