"""Exercise real vendored report/finalization/validation without APIs or pools."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys

repo, scope, temporary = map(Path, sys.argv[1:])
sys.path.insert(0, str(repo/'runtime_code/monthly'/str(scope)))

def no_network(*args, **kwargs):
    raise AssertionError('Native report proof must never access the network')

socket.create_connection = no_network
socket.socket.connect = no_network

from scripts import monthly_evidence_update as native
from scripts import validate_monthly_artifacts as validator
from automation import monthly_pipeline as pipeline

spec = importlib.util.spec_from_file_location('uro_scope_probe', repo/'automation/autonomous_scope.py')
worker = importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
worker.configure_stage_order(pipeline)
worker.configure_native_runtime()
assert pipeline.STAGES.index('report') < pipeline.STAGES.index('full-text') < pipeline.STAGES.index('finalize')

from automation import monthly_parallel, monthly_review_csv
assert monthly_review_csv.csv.DictWriter is not __import__('csv').DictWriter
driver = monthly_parallel.load_driver(repo/'runtime_code/monthly'/str(scope))
assert sys.modules[driver.__name__] is driver
independent = temporary/'independent'
independent.mkdir()
driver.OUTPUT_DIR = independent
driver.COST_PATH = independent/'ClaudeGeminiCost.txt'
(independent/'r1_pass_candidates.json').write_text(json.dumps({'articles':[], 'candidate_contexts':0}))
(independent/'r1_pass_packet.json').write_text(json.dumps({'results':[], 'input_digest':'empty-fixture'}))
with contextlib.redirect_stdout(io.StringIO()):
    driver.run_r2(independent)
assert json.loads((independent/'claude_r2_results.json').read_text())['results'] == []

start, end = '2026-07-01', '2026-07-31'
updates, reports = temporary/'updates', temporary/'reports'
output, report = updates/(start+'_'+end), reports/(start+'_'+end)
output.mkdir(parents=True)

def save(name, data):
    (output/name).write_text(json.dumps(data))

save('candidates.json', {'schema_version':1,'publication_date_start':start,
     'publication_date_end':end,'candidate_pmids':0,'candidate_contexts':0,'articles':[]})
save('out_of_window_records.json', {'count':0,'records':[]})
save('applied_changes.json', {'changes':[]})
save('exposure_subtype_review.json', {'records':[]})

with contextlib.redirect_stdout(io.StringIO()):
    for stage in pipeline.STAGES[1:]:
        if stage == 'first-screen': native.first_screen(output)
        elif stage == 'second-screen': native.second_screen(output)
        elif stage == 'report': native.write_reports(output, report)
        elif stage == 'full-text': native.full_text_review(output)
        elif stage == 'finalize': native.finalize_evidence(output, report)
    native.cost_accounting.write_cost_report(output, native.REPO_ROOT,
                                             report_path=report/'cost.txt')

summary = validator.validate_run(updates, reports, start, end)
ledger = json.loads((output/'exclusions.json').read_text())
assert ledger['canonical_track'] == 'chatgpt'
assert ledger['count'] == len(ledger['records']) == 0
packet_hashes = {name:hashlib.sha256((output/name).read_bytes()).hexdigest()
                 for name in ('chatgpt_first_screen.json','chatgpt_second_screen.json',
                              'chatgpt_full_text_review.json','chatgpt_final_screening.json','final_screening.json')}

# Demonstrate the old order fails the unchanged real validator while leaving
# the source/provider decisions unchanged, then restore only derived reports.
with contextlib.redirect_stdout(io.StringIO()):
    native.write_reports(output, report)
try:
    validator.validate_run(updates, reports, start, end)
except validator.ArtifactValidationError as error:
    assert 'exclusions must be ChatGPT-canonical' in str(error), str(error)
else:
    raise AssertionError('Legacy late report unexpectedly passed the validator')
assert all(hashlib.sha256((output/name).read_bytes()).hexdigest()==value for name,value in packet_hashes.items())
with contextlib.redirect_stdout(io.StringIO()):
    native.finalize_evidence(output, report)
validator.validate_run(updates, reports, start, end)
print(json.dumps({'scope':str(scope),'status':'verified','candidate_contexts':summary['candidate_contexts'],
                  'native_report_finalizer_validator':True,'old_order_rejected':True,
                  'registered_native_driver_r2':True,
                  'model_and_network_calls':0,'stage_order':list(pipeline.STAGES)}))
