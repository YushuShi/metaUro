#!/usr/bin/env python3
"""Record conservative saved-result guards; never mutate an application artifact."""
from pathlib import Path
import json,hashlib,collections,datetime,re
ROOT=Path(__file__).resolve().parents[2];OUT=Path(__file__).resolve().parent
rr=json.loads((OUT/'native_source_blocked_context_inventory.json').read_text());active={(r['repository'],r['context_id']):r for r in rr if r['recovery_status']=='native_R3_source_blocked'}
baseline=json.loads((OUT/'screening_artifact_preservation_baseline.json').read_text());meta={z['path']:z.get('status_fields',{}) for z in baseline['files']}
artifacts=[];guards=collections.defaultdict(list)
def stamp(d):
 vals=[]
 for k in ['updated_at','recorded_at','created_at','generated_at']:
  try:vals.append(datetime.datetime.fromisoformat(str(d.get(k,'')).replace('Z','+00:00')).timestamp())
  except (ValueError,TypeError):pass
 return max(vals,default=0)
def bind(p):
 b=(ROOT/p).read_bytes();artifacts.append({'path':p,'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b),'mtime_ns':(ROOT/p).stat().st_mtime_ns})
def rows(d):
 if isinstance(d,list):yield from (x for x in d if isinstance(x,dict));return
 for k in ['results','records','contexts','entries']:
  z=d.get(k)
  if isinstance(z,list):yield from (x for x in z if isinstance(x,dict));return
  if isinstance(z,dict):
   for cid,x in z.items():
    if isinstance(x,dict):yield dict(x,context_id=x.get('context_id',cid))
   return
def pipeline(p,d):
 t=str(d.get('track') or '').lower()
 return 'Claude+Gemini' if ('claude' in t or 'gemini' in t or 'claude_gemini' in p or '/monthly_parallel/' in p or Path(p).name=='claude_r2_results.json') else 'ChatGPT'
paths=(OUT/'final_gate_paths.txt').read_text().splitlines()
for p in paths:
 if any(x in p for x in ['/saved_before/','/source_effective_results','/before','/originals/','/lineage_recovery/','/generation_repair_snapshots/','/scope_repair_snapshots/','superseded']):continue
 name=Path(p).name
 if name not in {'claude_r2_results.json','chatgpt_effective_results.json','claude_gemini_effective_results.json','poolability_effective_results.json','claude_r2_effective_results.json','human_effective_results.json','final_screening.json','chatgpt_final_screening.json'}:continue
 bind(p);d=json.loads((ROOT/p).read_text());track=pipeline(p,d);layer=3 if name=='human_effective_results.json' else 2 if 'effective_results' in name else 1;time=stamp(d)
 for x in rows(d):
  cid=x.get('context_id');key=(p.split('/')[0],cid)
  if key not in active:continue
  human=(x.get('third_round_status')=='SUPERSEDED_BY_HUMAN_REVIEW' or x.get('human_review_status')=='applied' or x.get('manual_adjudication') is True or (isinstance(x.get('human_adjudication'),dict) and x['human_adjudication'].get('decision_source')=='human_manual_review'))
  reason=None
  if human:reason='exact_saved_human_adjudication_preserved'
  elif x.get('third_round_status')=='COMPLETED' and x.get('pmc_source_status') in {'full_text','verified_full_text'} and x.get('pmc_source_sha256'):reason='later_saved_verified_R3_completed'
  elif x.get('r3_not_required_after_policy') is True:reason='explicit_native_policy_R3_not_required'
  elif str(x.get('eligibility') or x.get('decision') or '').upper()=='EXCLUDE':reason='later_saved_scientific_exclusion'
  elif x.get('needs_full_text') is False:reason='later_saved_native_R3_gate_false'
  if reason:
   guards[key].append({'path':p,'track':track,'layer':layer,'timestamp':time,'human_context_override':human,'reason':reason,'saved_fields':{k:x.get(k) for k in ['context_id','eligibility','decision','needs_full_text','third_round_status','r3_not_required_after_policy','final_status','full_text_status','human_review_status','manual_adjudication','human_adjudication','pmc_source_status','pmc_source_sha256','third_round_model','recovery_receipt'] if k in x}})
 del d
# Applied current adjudication dispositions can settle a source-blocked context.
for repo in sorted({r['repository'] for r in active.values()}):
 for typ in ['astra_adjudication','astra_fulltext']:
  p=f'{repo}/combined/{typ}/review_dispositions.json'
  if not (ROOT/p).exists():continue
  bind(p);d=json.loads((ROOT/p).read_text());group=collections.defaultdict(list)
  for x in rows(d):
   cid=x.get('context_id');key=(repo,cid)
   if key in active:group[key].append(x)
  for key,z in group.items():
   scientific_exclude=any(x.get('applied') is True and str(x.get('context_status','')).upper()=='EXCLUDE' for x in z)
   all_settled=typ=='astra_fulltext' and all(x.get('applied') is True and x.get('follow_up_required') is False for x in z)
   if scientific_exclude or all_settled:
    guards[key].append({'path':p,'track':'both','layer':4,'timestamp':0,'human_context_override':True,'reason':'current_applied_scientific_exclusion' if scientific_exclude else 'current_applied_adjudication_no_followup','saved_fields':z})
# Later publication/exposure-specific curator decisions override old Neuro closeout rows.
for p in ['metaNeuro/data/curator_publication_dedup/decisions.json','metaNeuro/data/study_review_adjudications.json','metaNeuro/data/manual_exposure_adjudications.json']:
 bind(p);d=json.loads((ROOT/p).read_text())
 for x in d.get('decisions',d.get('adjudications',[])):
  if x.get('action')!='exclude_duplicate_publication' and x.get('decision') not in {'exclude','remove','eligible_not_poolable'}:continue
  pmid=str(x.get('excluded_pmid') or x.get('pmid') or '')
  outs=x.get('outcome_keys') or [x.get('outcome_key')]
  for key,r in active.items():
   if r['repository']!='metaNeuro' or r['pmid']!=pmid or r.get('outcome') not in outs:continue
   if x.get('exposure') and re.sub(r'[^a-z0-9]','',str(r.get('exposure','')).lower())!=re.sub(r'[^a-z0-9]','',x['exposure'].lower()):continue
   guards[key].append({'path':p,'track':'both','layer':5,'timestamp':stamp(d),'human_context_override':True,'reason':'later_explicit_curator_scope_decision','saved_fields':x})
excluded=[];pending=[]
for key,r in active.items():
 # Neuro canonical closeout is later than raw pipeline source/gate layers. Keep explicit canonical state.
 if r['repository']=='metaNeuro' and not any(g['layer']==5 for g in guards[key]):r['saved_lineage_guards']=[];r['lineage_guard_status']='canonical_current_neuro_R3_deferral';pending.append(r);continue
 selected=[];remain=[]
 for track in r['outstanding_tracks']:
  evidence_dates=[stamp(meta.get(e['path'],{})) for e in r['evidence'] if ('Claude+Gemini' if ('claude_gemini' in e['path'] or '/validation/' in e['path'] or '/monthly_parallel/' in e['path'] or Path(e['path']).name in {'r3_source_deferrals.json','r3_terminal_source_deferrals.json','r3_deferrals.json'}) else 'ChatGPT')==track]
  source_date=max(evidence_dates,default=0)
  eligible=[g for g in guards[key] if (g['track'] in {track,'both'} or g['human_context_override']) and (g['layer']>=2 or g['timestamp']>=source_date)]
  if eligible:selected.append(max(eligible,key=lambda g:(g['layer'],g['timestamp'])))
  else:remain.append(track)
 r['saved_lineage_guards']=selected;r['outstanding_tracks_before_lineage_guard']=r['outstanding_tracks'];r['outstanding_tracks']=remain
 if not remain:r['recovery_status']='already_settled_or_R3_not_required_saved_lineage';r['lineage_guard_status']='exclude_from_new_R3_dispatch';excluded.append(r)
 else:r['lineage_guard_status']='source_blocked_after_saved_lineage_guard';pending.append(r)
# Retain all historical rows and guarded rows in one audit inventory.
(OUT/'native_source_blocked_context_inventory_guarded.json').write_text(json.dumps(rr,indent=2,ensure_ascii=False)+'\n')
(OUT/'native_source_blocked_contexts_guarded.json').write_text(json.dumps(pending,indent=2,ensure_ascii=False)+'\n')
(OUT/'native_resolved_or_not_required_contexts.json').write_text(json.dumps(excluded,indent=2,ensure_ascii=False)+'\n')
pmids=sorted({r['pmid'] for r in pending},key=int);(OUT/'native_source_blocked_pmids_guarded.json').write_text(json.dumps(pmids,indent=2)+'\n')
summary={'generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_blocked_contexts_after_saved_lineage_guard':len(pending),'unique_pmids_after_guard':len(pmids),'settled_or_not_required_contexts_excluded':len(excluded),'guard_reasons':dict(collections.Counter(g['reason'] for r in excluded for g in r['saved_lineage_guards'])),'per_repository':[],'model_calls':0,'network_requests':0,'cost_usd':0,'dispatch_authorized':False,'scope_note':'Read-only native scientific gate inventory, independent of singleton/nonpoolability. Public recovery lookup allowed. Fresh policy/source/PMID-link and exact stage-input guard required before models.'}
for repo in sorted({r['repository'] for r in active.values()}):
 a=[r for r in pending if r['repository']==repo];summary['per_repository'].append({'repository':repo,'contexts':len(a),'pmids':len({r['pmid'] for r in a}),'missing_pmcid':sum(not r['pmcid'] for r in a),'unusable_pmc':sum(bool(r['pmcid']) for r in a),'settled_or_not_required_excluded':sum(r['repository']==repo for r in excluded)})
(OUT/'native_lineage_guard_report.json').write_text(json.dumps(summary,indent=2)+'\n');(OUT/'lineage_guard_artifact_baseline.json').write_text(json.dumps({'files':artifacts},indent=2)+'\n');print(json.dumps(summary,indent=2))
