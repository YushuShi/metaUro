"""No-model transport tests: native CSV bytes, module identity and Git staging."""
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import types

import pytest

from automation import autonomous_monthly as monthly
from automation.autonomous_scope import (_CanonicalReviewCSV, canonical_review_safe,
                                         export_review_queue, load_registered_driver)
from automation.sync_sources import sha, write
from native_proof_fixture import add_native_proofs

ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True).stdout


def completed_fixture(root, csv_bytes):
    """A small actual valid two-scope release with hash-bound review evidence."""
    run_id = '2026-09-01_2026-10-02'
    cutoff = '2026-10-02'
    write(root/'sources/metaPee/evidence.json', {'untouched':True})
    write(root/'evidence-manifest.json', {'files':[{'path':'sources/metaPee/evidence.json'}]})
    write(root/'runtime_code/monthly/manifest.json', {'version':monthly.VERSION, 'files':{}})
    review = root/'monthlyHumanReview/native.csv'
    review.parent.mkdir(parents=True)
    review.write_bytes(csv_bytes)
    write(root/'summary/proof.json', {'studies':[]})
    audit = root/'updates/monthly'/run_id
    write(audit/'proof.json', {'models':'unchanged'})
    receipts = {}
    index = {'schema_version':1, 'sources':{}}
    for scope in monthly.SOURCES:
        native_csv = audit/scope/'review/native.csv'
        native_csv.parent.mkdir(parents=True)
        native_csv.write_bytes(csv_bytes)
        screening = audit/scope/'screening'
        write(screening/'astra/adjudications.json', {'results':[]})
        write(screening/'final_screening.json', {'results':[]})
        write(screening/'monthly_application_gate.json', {
            'review_csv':'monthlyHumanReview/native.csv', 'review_csv_sha256':sha(native_csv),
            'adjudication_path':'data/monthly_updates/'+run_id+'/astra/adjudications.json',
            'adjudication_sha256':sha(screening/'astra/adjudications.json'),
            'source_final_screening_sha256':sha(screening/'final_screening.json')})
        receipts[scope] = {'run_id':run_id,
            'application_gate_sha256':sha(screening/'monthly_application_gate.json'),
            'native_review_csv_artifact':{'path':str(native_csv.relative_to(root)), 'sha256':sha(native_csv)}}
        add_native_proofs(screening, scope, receipts[scope])
        relative = scope+'/versions/monthly/manifest.json'
        write(root/'updates'/relative, {'source':scope, 'producer':'metaUro',
              'search_through':cutoff, 'files':{}, 'review_files':[]})
        index['sources'][scope] = {'manifest':relative, 'sha256':sha(root/'updates'/relative)}
    write(root/'updates/manifest.json', index)
    release = {'status':'completed', 'run_id':run_id, 'end_date':cutoff,
        'scopes':receipts, 'prepared_overlay_index':index,
        'audit_artifact_sha256':{str(p.relative_to(root)):sha(p)
            for p in [review, *audit.rglob('*')] if p.is_file()},
        'summary_sha256':{'summary/proof.json':sha(root/'summary/proof.json')}}
    write(audit/'release.json', release)
    write(root/'updates/monthly/state.json', {'repository':'metaUro', 'status':'completed',
        'run_id':run_id, 'search_through':dict.fromkeys(monthly.SOURCES, cutoff),
        'manifest_path':str((audit/'release.json').relative_to(root)), 'manifest_sha256':sha(audit/'release.json')})
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Transport fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    git(root, 'config', 'core.autocrlf', 'input')
    (root/'.gitattributes').write_text('*.csv text\n')
    git(root, 'add', '.')
    return review


def record():
    source = {'context_id':'context-1', 'pmid':'123', 'different_fields':['effect_size'],
        'comparison':'PRIMARY_VALIDATION', 'title':'Quoted "title"\nsecond line',
        'r2':{}, 'r3':{}, 'primary':{'eligibility':'INCLUDE', 'model':'gpt-5.6-sol',
        'extraction':{'effect_size':1.1}}, 'validation':{'eligibility':'INCLUDE',
        'model':'claude-opus-5', 'extraction':{'effect_size':1.2}}}
    return {'request':{'input':source}, 'request_digest':'receipt-1', 'status':'pending'}


@pytest.mark.parametrize('scope', monthly.SOURCES)
def test_native_and_export_csv_git_roundtrip_preserves_claims(scope, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('native_csv_fixture', ROOT/'runtime_code/monthly'/scope/'automation/monthly_review_csv.py')
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    native.csv = _CanonicalReviewCSV()
    native.safe = canonical_review_safe
    review = tmp_path/'native.csv'
    native.write_review(review, [record()], context={'run_id':'fixture', 'source_scope':scope})
    raw = review.read_bytes()
    assert b'\r\n' not in raw
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    assert rows[0]['title'] == record()['request']['input']['title']
    assert json.loads(rows[0]['primary_claims']) == record()['request']['input']['primary']
    assert json.loads(rows[0]['validation_claims']) == record()['request']['input']['validation']
    assert csv.excel.lineterminator == '\r\n'  # No global standard-library mutation.
    # Preserve human annotations and multiline quotations on repeated native export.
    rows[0]['comment'] = 'Keep "quoted" evidence\r\nand the second line\rand third'
    with review.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader(); writer.writerows(rows)
    native.write_review(review, [record()])
    assert b'\r' not in review.read_bytes()
    annotated = list(csv.DictReader(io.StringIO(review.read_text(encoding='utf-8-sig'))))
    assert annotated[0]['comment'] == 'Keep "quoted" evidence\nand the second line\nand third'
    import automation
    monkeypatch.setitem(sys.modules, 'automation.monthly_review_csv', native)
    monkeypatch.setattr(automation, 'monthly_review_csv', native, raising=False)
    run = tmp_path/'data/monthly_updates/fixture'
    write(run/'final_screening.json', {'results':[]})
    (tmp_path/'monthlyHumanReview').mkdir()
    exported = export_review_queue(tmp_path, run, {'review':{'csv_path':str(review)}, 'ui_refresh':{}})
    assert exported.read_bytes() == review.read_bytes()
    release_root = tmp_path/'release'
    published = completed_fixture(release_root, exported.read_bytes())
    before = published.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    assert monthly.verify_git_index(release_root)['status'] == 'verified'
    git(release_root, 'commit', '-qm', 'Fixture release')
    assert git(release_root, 'show', 'HEAD:monthlyHumanReview/native.csv') == before
    published.unlink()
    git(release_root, 'checkout', '--', 'monthlyHumanReview/native.csv')
    assert hashlib.sha256(published.read_bytes()).hexdigest() == digest
    assert monthly.validate_completed_release(release_root)['status'] == 'completed'


def test_default_crlf_csv_is_rejected_after_real_git_normalization(tmp_path):
    review = completed_fixture(tmp_path, b'pmid,reason\r\n123,"one\n two"\r\n')
    assert monthly.validate_completed_release(tmp_path)['status'] == 'completed'
    assert git(tmp_path, 'show', ':monthlyHumanReview/native.csv') != review.read_bytes()
    with pytest.raises(ValueError, match='Git index bytes differ.*native.csv'):
        monthly.verify_git_index(tmp_path)


@pytest.mark.parametrize('relative', ['updates/monthly/2026-09-01_2026-10-02/proof.json',
    'summary/proof.json', 'monthlyHumanReview/native.csv', 'runtime_code/monthly/manifest.json',
    'sources/metaPee/evidence.json', 'evidence-manifest.json'])
def test_required_manifest_proofs_must_be_staged(tmp_path, relative):
    completed_fixture(tmp_path, b'pmid,reason\n')
    git(tmp_path, 'rm', '--cached', '--', relative)
    with pytest.raises(ValueError, match='missing or unresolved'):
        monthly.verify_git_index(tmp_path)


def test_clean_filter_that_changes_validated_bytes_is_rejected(tmp_path):
    completed_fixture(tmp_path, b'pmid,reason\n123,X\n')
    git(tmp_path, 'config', 'filter.fixture.clean', 'tr X Y')
    (tmp_path/'.gitattributes').write_text('*.csv text filter=fixture\n')
    git(tmp_path, 'add', '--renormalize', '.')
    with pytest.raises(ValueError, match='Git index bytes differ.*native.csv'):
        monthly.verify_git_index(tmp_path)


def test_unreferenced_recovery_sidecar_is_scratch_but_hash_bound_sidecar_is_required(tmp_path):
    completed_fixture(tmp_path, b'pmid,reason\n')
    relative = 'updates/monthly/recovery/discovery.sqlite3-wal'
    sidecar = tmp_path/relative
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b'fixture sidecar')
    assert monthly.verify_git_index(tmp_path)['status'] == 'verified'
    state_path = tmp_path/'updates/monthly/state.json'
    state = json.loads(state_path.read_text())
    release_path = tmp_path/state['manifest_path']
    release = json.loads(release_path.read_text())
    release['audit_artifact_sha256'][relative] = sha(sidecar)
    write(release_path, release)
    state['manifest_sha256'] = sha(release_path)
    write(state_path, state)
    git(tmp_path, 'add', '--', str(state_path.relative_to(tmp_path)), str(release_path.relative_to(tmp_path)))
    with pytest.raises(ValueError, match='missing or unresolved.*sqlite3-wal'):
        monthly.verify_git_index(tmp_path)


@pytest.mark.parametrize('mode', ['no', 'yes'])
def test_summary_lock_is_scratch_unless_explicitly_hash_bound(tmp_path, mode):
    completed_fixture(tmp_path, b'pmid,reason\n')
    ignore = tmp_path/'summary/.gitignore'
    ignore.write_text('.locks/\n')
    git(tmp_path, 'add', '--', 'summary/.gitignore')
    relative = f'summary/.locks/{mode}.lock'
    lock = tmp_path/relative
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b'')
    inventory = monthly.summary_inventory(tmp_path)
    assert relative not in inventory
    assert inventory == {'summary/proof.json':sha(tmp_path/'summary/proof.json'),
                         'summary/.gitignore':sha(ignore)}
    assert git(tmp_path, 'check-ignore', relative).decode().strip() == relative
    assert monthly.verify_git_index(tmp_path)['status'] == 'verified'
    # An explicit reference always wins over the scratch-file scan exclusion.
    state_path = tmp_path/'updates/monthly/state.json'
    state = json.loads(state_path.read_text())
    release_path = tmp_path/state['manifest_path']
    release = json.loads(release_path.read_text())
    release['summary_sha256'][relative] = sha(lock)
    write(release_path, release)
    state['manifest_sha256'] = sha(release_path)
    write(state_path, state)
    git(tmp_path, 'add', '--', str(state_path.relative_to(tmp_path)), str(release_path.relative_to(tmp_path)))
    with pytest.raises(ValueError, match=r'missing or unresolved.*summary/\.locks'):
        monthly.verify_git_index(tmp_path)
    git(tmp_path, 'add', '-f', '--', relative)
    assert monthly.verify_git_index(tmp_path)['status'] == 'verified'


def test_summary_inventory_retains_lock_named_files_outside_builder_lock_directory(tmp_path):
    path = tmp_path/'summary/no/study.lock'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'authoritative fixture')
    assert monthly.summary_inventory(tmp_path) == {'summary/no/study.lock':sha(path)}


@pytest.mark.parametrize('artifact', ['native_csv', 'gate', 'adjudication', 'final_screening',
    'candidates', 'native_manifest', 'parallel_source', 'application_filter'])
def test_native_gate_proofs_cannot_be_resealed_with_changed_bytes(tmp_path, artifact):
    completed_fixture(tmp_path, b'review_id,reason\n')
    state_path = tmp_path/'updates/monthly/state.json'
    state = json.loads(state_path.read_text())
    release_path = tmp_path/state['manifest_path']
    release = json.loads(release_path.read_text())
    scope = release_path.parent/'metaPee'
    target = {'native_csv':scope/'review/native.csv',
              'gate':scope/'screening/monthly_application_gate.json',
              'adjudication':scope/'screening/astra/adjudications.json',
              'final_screening':scope/'screening/final_screening.json',
              'candidates':scope/'screening/candidates.json',
              'application_filter':scope/'screening/monthly_application_filter.json',
              'native_manifest':scope/'screening/monthly_run_manifest.json',
              'parallel_source':scope/'screening/monthly_parallel/source_receipt.json'}[artifact]
    target.write_bytes(b'{}\n')
    # Updating the outer inventory cannot hide a broken native evidence chain.
    release['audit_artifact_sha256'][str(target.relative_to(tmp_path))] = sha(target)
    write(release_path, release)
    state['manifest_sha256'] = sha(release_path)
    write(state_path, state)
    with pytest.raises(ValueError, match='checksum mismatch'):
        monthly.validate_completed_release(tmp_path)


def test_driver_registers_during_import_and_restores_previous_on_failure(tmp_path, monkeypatch):
    script = tmp_path/'scripts/run_claude_gemini_parallel_review.py'
    script.parent.mkdir()
    script.write_text('import sys\nself_at_import = sys.modules[__name__]\n')
    monkeypatch.delitem(sys.modules, 'monthly_parallel_native_driver', raising=False)
    driver = load_registered_driver(tmp_path)
    assert driver.self_at_import is driver is sys.modules[driver.__name__]
    script.write_text('raise RuntimeError("fixture import failure")\n')
    with pytest.raises(RuntimeError, match='fixture import failure'):
        load_registered_driver(tmp_path)
    assert sys.modules[driver.__name__] is driver
    sys.modules.pop(driver.__name__)
    with pytest.raises(RuntimeError, match='fixture import failure'):
        load_registered_driver(tmp_path)
    assert 'monthly_parallel_native_driver' not in sys.modules


def test_publication_index_guard_runs_after_staging_before_commit():
    workflow = (ROOT/'.github/workflows/monthly-source-refresh.yml').read_text()
    assert workflow.index('git add -A') < workflow.index('automation.autonomous_monthly verify-index') < workflow.index('git commit')


@pytest.mark.parametrize('text,expected', [('=formula\r\nnext', "'=formula\nnext"),
    ('\r@formula', "'\n@formula"), ('quoted\r\ncell\rsecond', 'quoted\ncell\nsecond'),
    (None, '')])
def test_cell_newlines_are_canonical_before_formula_protection(text, expected):
    assert canonical_review_safe(text) == expected
