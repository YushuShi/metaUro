"""Code-only binding repair rejects scientific data and output changes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/refresh_summary_reader_bindings_20261027.py'
spec = importlib.util.spec_from_file_location('reader_binding_repair', SCRIPT)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def test_metadata_hash_replacement_preserves_existing_text_encoding():
    old, new = 'a' * 64, 'b' * 64
    raw = (json.dumps({'note':'≈','input_sha256':old}, ensure_ascii=True, indent=2) + '\n').encode()
    expected = json.loads(raw)
    expected['input_sha256'] = new
    revised = repair.replace_hashes(raw, {old: new}, expected)
    assert b'\\u2248' in revised
    assert revised == raw.replace(old.encode(), new.encode())


def test_asset_validation_detects_changed_outputs_without_rewriting(tmp_path):
    path = tmp_path / 'published.xlsx'
    path.write_bytes(b'published scientific workbook')
    assets = {path.name: {'sha256': repair.digest(path.read_bytes()), 'bytes': path.stat().st_size}}
    repair.verify_assets(tmp_path, assets)
    path.write_bytes(b'changed scientific workbook')
    with pytest.raises(ValueError, match='Published asset changed'):
        repair.verify_assets(tmp_path, assets)
    assert path.read_bytes() == b'changed scientific workbook'


def evidence_fixture(root):
    old_worker, new_worker = b'old reviewed reader', b'new reviewed reader'
    (root / 'source_worker.py').write_bytes(new_worker)
    (root / 'evidence.json').write_bytes(b'scientific input bytes')
    manifest = root / 'active_manifest.json'
    document = {'contexts': [{'context_id': 'reviewed-context', 'native_rows': [{'effect': .8}],
                             'baseline_files': {'source_worker.py': repair.digest(old_worker),
                                                'evidence.json': repair.digest((root / 'evidence.json').read_bytes())}}]}
    manifest.write_text(json.dumps(document, indent=2))

    def bind(repo, relative, expected):
        path = repo / relative
        if repair.digest(path.read_bytes()) != expected:
            raise ValueError('Scientific binding mismatch')
        return path

    native = SimpleNamespace(preprint_manifest_path=lambda repo: manifest, _bound_file=bind)

    def status(repo):
        for context in json.loads(manifest.read_text())['contexts']:
            for relative, expected in context['baseline_files'].items():
                native._bound_file(repo, relative, expected)
        return {'available': True, 'status': 'complete', 'contexts': 1, 'records': 1}

    native.preprint_status = status
    return manifest, document, native, old_worker, new_worker


def test_evidence_rebinding_only_prepares_reviewed_code_hash_changes(tmp_path):
    path, previous, native, old_worker, new_worker = evidence_fixture(tmp_path)
    before = path.read_bytes()
    with patch.object(repair, 'old_bytes', return_value=old_worker), \
            patch.dict(repair.APPROVED_READERS, {'source_worker.py': repair.digest(new_worker)}):
        _, original, prepared, status, audit = repair.prepare_evidence_manifest(tmp_path, native)
    assert original == before == path.read_bytes()
    revised = json.loads(prepared)
    revised['contexts'][0]['baseline_files']['source_worker.py'] = previous['contexts'][0]['baseline_files']['source_worker.py']
    assert revised == previous
    assert status['available'] and audit['scientific_evidence_payload_unchanged']


def test_evidence_rebinding_rejects_noncode_input_changes(tmp_path):
    path, _, native, old_worker, new_worker = evidence_fixture(tmp_path)
    before = path.read_bytes()
    (tmp_path / 'evidence.json').write_bytes(b'changed scientific input')
    with patch.object(repair, 'old_bytes', return_value=old_worker), \
            patch.dict(repair.APPROVED_READERS, {'source_worker.py': repair.digest(new_worker)}):
        with pytest.raises(ValueError, match='Unreviewed recovery input binding changed'):
            repair.prepare_evidence_manifest(tmp_path, native)
    assert path.read_bytes() == before


def test_legacy_fingerprint_uses_current_scientific_bytes(tmp_path):
    (tmp_path / 'runtime_code').mkdir()
    before = {'summary_outputs.py': b'old offline entrypoint', 'runtime_code/manifest.json': b'old runtime inventory'}
    (tmp_path / 'summary_outputs.py').write_bytes(b'new interactive default')
    (tmp_path / 'runtime_code/manifest.json').write_bytes(b'new API inventory')
    (tmp_path / 'runtime_code/sample_size_summary_native.py').write_bytes(b'interactive helper')
    (tmp_path / 'evidence.json').write_bytes(b'unchanged scientific evidence')
    native = SimpleNamespace(sha=lambda path: repair.digest(path.read_bytes()))

    def fingerprint(root, source, mode):
        paths = [root / 'summary_outputs.py', root / 'runtime_code/manifest.json', root / 'evidence.json']
        paths.extend((root / 'runtime_code').glob('*.py'))
        return repair.digest(''.join(native.sha(path) for path in paths).encode())

    native.input_fingerprint = fingerprint
    with patch.object(repair, 'old_bytes', side_effect=lambda root, path: before[path]):
        original = repair.legacy_fingerprint(tmp_path, 'MetaVir', 'yes', native)
        expected = repair.digest(''.join([repair.digest(before['summary_outputs.py']),
            repair.digest(before['runtime_code/manifest.json']), repair.digest((tmp_path / 'evidence.json').read_bytes())]).encode())
        assert original == expected
        (tmp_path / 'evidence.json').write_bytes(b'changed scientific evidence')
        assert repair.legacy_fingerprint(tmp_path, 'MetaVir', 'yes', native) != original


def test_packaged_release_serves_bound_summaries_without_rebuilding(monkeypatch):
    """Run against the hydrated release, so reader edits cannot silently stale it."""
    import preprint_ui
    import summary_outputs

    root = SCRIPT.parents[1]
    status = preprint_ui.preprint_status(root)
    assert status['available'] and status['status'] == 'complete'
    assert status['contexts'] > 0 and status['records'] > 0
    for mode in ('yes', 'no'):
        namespace = root / 'summary' / mode / 'preprints_included'
        for folder in (namespace, namespace / 'cohort_latest'):
            assert preprint_ui.verify_preprint_summary_asset(folder / 'summary_manifest.json', root), folder

    monkeypatch.delenv('METAURO_KEEP_LATEST_COHORT', raising=False)
    folder = summary_outputs.summary_dir(root, 'MetaVir', 'yes', include_preprints=False)
    assert folder == root / 'summary/yes/cohort_latest'
    # The native freshness check validates the shipped default view directly;
    # failure must be repaired before release, rather than rebuilding in a test.
    fingerprint = summary_outputs.input_fingerprint(root, 'MetaVir', 'yes')
    assert summary_outputs._current(folder, fingerprint, 'MetaVir')
