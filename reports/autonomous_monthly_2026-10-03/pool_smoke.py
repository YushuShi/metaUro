from pathlib import Path
import os,sys,json,math
sys.path.insert(0,str(Path.cwd()))
from automation import monthly_ui as ui
from automation.autonomous_projection import cache_profiles,refresh_ui
from scripts import subexposure_policy
root=Path.cwd();uro=Path(os.environ['METAURO_REPO_ROOT'])
assert root != uro and not root.is_relative_to(uro/'sources')
assert (root/'autonomous_materialization.json').is_file()
scope=root.name;profiles=cache_profiles(root,scope)
base=root/profiles[0]['cache'];name='prostate_cancer_incidence_true_all.json' if scope=='MetaVir' else 'renal_cell_carcinoma_incidence_true_all.json'
payload=ui._read(base/'calcium'/name);old=payload['studies']
def okay(r):
 try:return r.get('display_only') is not True and r.get('subexposure_pooling_selected') is not False and all(float(r.get(k) or 0)>0 for k in ('Effect Size','Lower CI','Upper CI'))
 except (ValueError,TypeError):return False
row=dict(next(r for r in old if okay(r)))
row.update(PMID='999999993',**{'Effect ID':'offline-pooled-smoke-2','Record ID':'offline-pooled-smoke-2','Direct Exposure':'Calcium','display_only':False,'policy_poolable':True,'manual_poolable':True,'subexposure_pooling_selected':True,'disease_hierarchy_pooling_selected':True})
disease=payload.get('disease') or payload['cancer'];cid='999999993|Calcium|'+disease
run=root/'data/monthly_updates/2099-01-01_2099-01-02'
ui._write(root/'Cached_results/calcium'/name,{'studies':[row],'disease':disease,'cancer':disease,'exposure':'Calcium'})
ui._write(run/'final_screening.json',{'results':[{'context_id':cid,'pmid':'999999993','cancer':disease,'exposure':'Calcium','final_status':'INCLUDE_META'}]})
ui._write(run/'applied_changes.json',{'changes':[{'exposure':'Calcium','cancer':disease,'added_pmids':['999999993']}]})
result=refresh_ui(root,run,[])
after=ui._read(base/'calcium'/name)
assert after['studies'][:len(old)]==old
assert after['headline'] and math.isfinite(after['headline']['pooled_es'])
assert after['headline']['baseline_risk']==payload['headline']['baseline_risk']
assert after['plot_url'] and any(p.endswith('.png') for p in result['output_sha256'])
print(json.dumps({'scope':scope,'calculation':'passed','baseline_preserved':True,'historical_rows_preserved':len(old),'new_studies':len(after['studies'])-len(old),'changed_files':len(result['output_sha256']),'pooled_rr':after['headline']['pooled_es']}))
