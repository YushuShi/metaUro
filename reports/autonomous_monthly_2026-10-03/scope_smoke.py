from pathlib import Path
import hashlib,json,os,shutil
import sys
sys.path.insert(0,str(Path.cwd()))
root=Path.cwd();uro=Path(os.environ['METAURO_REPO_ROOT'])
assert root != uro and not root.is_relative_to(uro/'sources')
assert (root/'autonomous_materialization.json').is_file()
for name in ('autonomous_scope.py','autonomous_projection.py'):
 shutil.copyfile(uro/'automation'/name,root/'automation'/name)
from automation import monthly_pipeline,monthly_parallel,monthly_ui as ui
from automation.autonomous_projection import cache_profiles,refresh_ui
from automation.autonomous_scope import export_review_queue
from scripts import monthly_evidence_update
scope=root.name
monthly_parallel.load_driver(root)
profiles=cache_profiles(root,scope)
roots=[root/p['cache'] for p in profiles]+[root/'static/subexposures',root/'combined/provisional_ui/static']
materialized=ui._read(root/'autonomous_materialization.json')
materialized['ui_baseline_sha256']=ui._hashes(root,roots)
# Read-only pinned source assembled before smoke; source view is under this Uro checkout.
index=ui._read(uro/'updates/manifest.json')
from subprocess import check_output
source=check_output([os.sys.executable,'-c',"from automation.sync_sources import assembled_source;from pathlib import Path;print(assembled_source(Path.cwd(),'"+scope+"'))"],cwd=uro,text=True).strip()
materialized['source_root']=source
ui._write(root/'autonomous_materialization.json',materialized)
run=root/'data/monthly_updates/2099-01-01_2099-01-02'
for name in ('first_screen.json','second_screen.json','full_text_review.json','final_screening.json'):
 ui._write(run/name,{'results':[]})
ui._write(run/'candidates.json',{'articles':[],'publication_date_start':'2099-01-01','publication_date_end':'2099-01-02'})
parallel=monthly_parallel.run_parallel(root,run)
assert parallel['complete'] and parallel['expected_r2']==0
base=root/profiles[0]['cache']
name='prostate_cancer_incidence_true_all.json' if scope=='MetaVir' else 'renal_cell_carcinoma_incidence_true_all.json'
payload=ui._read(base/'calcium'/name)
original=payload['studies']
row=dict(original[0]);row.update(PMID='999999991',**{'Effect ID':'offline-smoke','Record ID':'offline-smoke','Direct Exposure':'Calcium','display_only':True,'policy_poolable':False,'manual_poolable':False,'subexposure_pooling_selected':False})
disease=payload.get('disease') or payload['cancer'];cid='999999991|Calcium|'+disease
ui._write(root/'Cached_results/calcium'/name,{'studies':[row],'disease':disease,'cancer':disease,'exposure':'Calcium'})
ui._write(run/'final_screening.json',{'results':[{'context_id':cid,'pmid':'999999991','cancer':disease,'exposure':'Calcium','final_status':'ELIGIBLE_NOT_POOLABLE'}]})
ui._write(run/'applied_changes.json',{'changes':[{'exposure':'Calcium','cancer':disease,'added_pmids':['999999991']}]})
first=refresh_ui(root,run,[])
second=refresh_ui(root,run,[])
assert first['output_sha256']==second['output_sha256'] and first['output_sha256']
assert ui._read(base/'calcium'/name)['studies'][:len(original)]==original
assert not any(str(key).startswith('sources/') for key in first['output_sha256'])
print(json.dumps({'scope':scope,'driver_import':'passed','zero_candidate_independent_review':'complete','projection_replay':'exact_hash_inventory','preserved_historical_rows':len(original),'output_files':len(first['output_sha256'])}))
