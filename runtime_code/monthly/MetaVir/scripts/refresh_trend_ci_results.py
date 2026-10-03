"""Refresh only proven trend-derived CI copies from saved data, without model calls.

Use an evidence-first JSON manifest from the saved-excerpt audit. Raw screening
receipts and nested source extractions are preserved; effective study rows carry
retraction receipts. No replacement contrast is automatically selected.
"""
from __future__ import annotations
import argparse,copy,gzip,hashlib,io,json,math,sys,warnings
from collections import defaultdict
from contextlib import redirect_stdout
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.trend_ci_policy import guard_row
from scripts.subexposure_policy import pooling_rows
AUDIT=ROOT/'data/trend_ci_audit/2026-09-27'
FIELDS=('headline','summary_html','plot_url','funnel_plot_url','baujat_plot_url')
def digest(raw):return hashlib.sha256(raw).hexdigest()
def write(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 path.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
def compatible(value):
 if isinstance(value,dict):return {str(k):compatible(v) for k,v in value.items()}
 if isinstance(value,(list,tuple)):return [compatible(v) for v in value]
 if isinstance(value,float) and not math.isfinite(value):return None
 if hasattr(value,'item'):return compatible(value.item())
 return value

def refresh(manifest,apply=False):
 previous=AUDIT/'refresh.json'
 if apply and previous.exists():
  report=json.loads(previous.read_text())
  if report.get('applied') and report.get('changes') and all(digest((ROOT/c['path']).read_bytes())==c['after_sha256'] for c in report['changes']):
   print({'reused_verified_refresh':True,'changed_files':report['changed_files']},flush=True)
   return report
 import pandas as pd
 import meta_analysis
 # Computational analysis is offline. Fail closed if future code adds transport.
 import requests
 def network_disabled(*args,**kwargs):raise RuntimeError('Network is disabled for the saved trend-CI refresh')
 requests.sessions.Session.request=network_disabled
 AUDIT.mkdir(parents=True,exist_ok=True)
 source=copy.deepcopy(manifest)
 source.update(policy='Only p-derived CIs with matching saved trend-test evidence are retracted; directly reported uncertainty and all source receipts are retained.',network_calls=0)
 write(AUDIT/'source_evidence.json',source)
 by_path=defaultdict(list)
 for rec in manifest['records']:by_path[rec['path']].append(rec)
 changes=[];calculations={}
 for relative,recs in sorted(by_path.items()):
  path=ROOT/relative;raw=path.read_bytes();old=json.loads(raw)
  if digest(raw)!=manifest['file_sha256'][relative]:
   # A completed refresh is repeatable without overwriting its preimages.
   if old.get('trend_ci_guard_version')=='trend-p-ci-2026-09-27-v1':continue
   raise RuntimeError('Input changed after source audit: '+relative)
  rows=[guard_row(row) for row in old.get('studies',[])]
  changed=[i for i,(a,b) in enumerate(zip(old.get('studies',[]),rows)) if a!=b]
  expected={r['json_path'][1] for r in recs}
  if set(changed)!=expected:raise RuntimeError('Source-audited rows disagree with runtime guard: '+relative)
  before_pool=pooling_rows(old['studies']);after_pool=pooling_rows(rows)
  disease=old.get('cancer') or old.get('disease');exposure=old.get('exposure') or path.parent.name
  endpoint=old.get('outcome') or old.get('endpoint') or 'Incidence'
  if not disease:raise RuntimeError('No disease scope: '+relative)
  before_ids=[old['studies'].index(r) for r in before_pool];after_ids=[rows.index(r) for r in after_pool]
  record={'path':relative,'before_sha256':digest(raw),'changed_study_indices':changed,
          'removed_from_current_pool_indices':sorted(set(before_ids)-set(after_ids)),
          'pool_rows_before':len(before_pool),'pool_rows_after':len(after_pool),
          'old_headline':old.get('headline'),'disease':disease,'exposure':exposure}
  output=copy.deepcopy(old)
  if before_ids!=after_ids:
   # Reuse genuinely identical statistical inputs across core/all and views.
   calc_id=digest(json.dumps([disease,exposure,endpoint,after_pool],sort_keys=True,default=str).encode())[:20]
   if calc_id not in calculations:
    namespace='trend_ci_2026_09_27/'+calc_id
    kwargs=dict(outcome=endpoint,exclude_meta=True,df_all=pd.DataFrame(rows),
                screening_stats=old.get('screening_stats'),generate_plots=apply)
    if ROOT.name=='metaBreath':kwargs['plot_root']=ROOT/'static'/namespace
    elif ROOT.name=='metaGut':kwargs.update(plot_output_root=str(ROOT/'static'/namespace),plot_url_prefix='static/'+namespace)
    else:kwargs['plot_namespace']=namespace
    with warnings.catch_warnings(),redirect_stdout(io.StringIO()):
     warnings.simplefilter('ignore')
     result=meta_analysis.perform_meta_analysis(pd.DataFrame(after_pool,columns=list(pd.DataFrame(rows).columns)),disease,exposure,**kwargs)
    if result.get('error'):raise RuntimeError(relative+': '+result['error'])
    if ROOT.name=='metaBreath' and apply:
     for key in FIELDS:
      if key.endswith('_url') and isinstance(result.get(key),str):result[key]=result[key].replace('static/','static/'+namespace+'/',1)
    if isinstance(result.get('headline'),dict):
     result['headline']['n_studies']=len(after_pool)
     result['headline']['total_n']=int(pd.to_numeric(pd.DataFrame(after_pool)['Sample Size'],errors='coerce').fillna(0).sum())
    calculations[calc_id]=compatible(result)
   for key in FIELDS:output[key]=calculations[calc_id].get(key)
   record['calculation_id']=calc_id
  output['studies']=rows
  output['trend_ci_guard_version']='trend-p-ci-2026-09-27-v1'
  output['trend_ci_audit']='data/trend_ci_audit/2026-09-27/source_evidence.json'
  record['new_headline']=output.get('headline')
  record['receipts']=[{'study_index':i,'pmid':rows[i].get('PMID'),**copy.deepcopy(rows[i]['p_value_ci_retraction'])} for i in changed]
  if apply:
   target=AUDIT/'preimages'/(digest(relative.encode())+'.json.gz');target.parent.mkdir(parents=True,exist_ok=True)
   if not target.exists():target.write_bytes(gzip.compress(raw,mtime=0))
   record['preimage']=str(target.relative_to(ROOT))
   write(path,compatible(output));record['after_sha256']=digest(path.read_bytes())
  changes.append(record)
 report={'applied':apply,'network_calls':0,'changed_files':len(changes),'changed_rows':sum(len(x['changed_study_indices']) for x in changes),
         'files_with_pool_change':sum(bool(x['removed_from_current_pool_indices']) for x in changes),
         'unique_calculations':len(calculations),'changes':changes}
 write(AUDIT/('refresh.json' if apply else 'refresh_plan.json'),report)
 print({k:v for k,v in report.items() if k!='changes'},flush=True)
 return report
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--audit-input',required=True,type=Path);parser.add_argument('--apply',action='store_true');a=parser.parse_args()
 refresh(json.loads(a.audit_input.read_text()),a.apply)
