"""Optional Uro summaries never auto-build/fall back after an applied recovery."""
import json,hashlib
from unittest.mock import Mock
import pytest
import summary_outputs as summaries


def released(tmp_path,contexts):
    root=tmp_path/'metaUro';p=root/'updates/preprint_recovery/active_manifest.json';p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'schema_version':1,'status':'complete','contexts':contexts}));return root


def test_renamed_uro_deployment_root_serves_recovery_status(tmp_path):
    from flask import Flask
    from preprint_ui import install_status_endpoint, preprint_manifest_path

    root = tmp_path / 'app'
    root.mkdir()
    (root / 'source_worker.py').write_text('# Uro worker')
    (root / 'evidence-manifest.json').write_text('{}')
    path = root / 'updates/preprint_recovery/active_manifest.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'schema_version': 1, 'status': 'complete', 'contexts': []}))
    assert preprint_manifest_path(root) == path
    app = Flask(__name__)
    install_status_endpoint(app, root)
    response = app.test_client().get('/api/preprint-recovery-status')
    assert response.status_code == 200
    assert response.get_json()['available'] is True
    assert response.get_json()['manifest_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_uro_layout_requires_both_markers_and_keeps_other_repository_paths(tmp_path):
    from preprint_ui import preprint_manifest_path

    (tmp_path / 'source_worker.py').write_text('# A worker alone is not the Uro layout')
    assert preprint_manifest_path(tmp_path) == tmp_path / 'data/preprint_recovery/active_manifest.json'
    assert preprint_manifest_path(tmp_path / 'metaUro') == tmp_path / 'metaUro/updates/preprint_recovery/active_manifest.json'


def test_host_binding_repair_accepts_only_the_reviewed_reader_transition(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / 'scripts/refresh_preprint_host_bindings_20261027.py'
    spec = importlib.util.spec_from_file_location('host_binding_repair', path)
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)
    before = b'def preprint_manifest_path(repo_root):\n' + repair.OLD_RESOLVER
    monkeypatch.setattr(repair, 'old_bytes', lambda *args: before)
    current = tmp_path / 'preprint_ui.py'
    current.write_bytes(before.replace(repair.OLD_RESOLVER, repair.NEW_RESOLVER))
    assert repair.reviewed_reader_transition(tmp_path) == (
        repair.checks.digest(before), repair.checks.digest(current.read_bytes()))
    current.write_bytes(current.read_bytes() + b'# unreviewed reader change\n')
    with pytest.raises(ValueError, match='beyond the reviewed'):
        repair.reviewed_reader_transition(tmp_path)


def test_applied_preprint_summary_requires_offline_sealed_assets(tmp_path,monkeypatch):
    selector={'source_repo':'MetaVir','disease':'Prostate cancer','exposure':'Coffee','outcome':'Incidence','include_subexposures':'yes','source_variant':'combined'}
    context={'context_id':'x','selector':selector,'review':{'status':'settled','decision':'include'},'publication_anchor':{'pmid':'987654'}}
    root=released(tmp_path,[context])
    baseline=root/'baseline.json';baseline.write_text('{}')
    context['baseline_files']={'baseline.json':hashlib.sha256(baseline.read_bytes()).hexdigest()}
    record={'pmid':'987654','availability':'pmcid_missing','preprint_fetch_allowed':True,'model_dispatch_authorized':True,'saved_any_track_checked':True,'saved_any_track_full_text_available':False,'any_track_gate_record_sha256':'a'*64}
    proof=root/'article_gate.json';proof.write_text(json.dumps({'schema_version':1,'records':[record]}))
    digest=hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    context['article_availability_gate']={'path':'article_gate.json','sha256':hashlib.sha256(proof.read_bytes()).hexdigest(),'record_sha256':digest,'pmid':'987654'}
    (root/'updates/preprint_recovery/active_manifest.json').write_text(json.dumps({'schema_version':1,'status':'complete','contexts':[context]}))
    build=Mock();monkeypatch.setattr(summaries.subprocess,'run',build);monkeypatch.setattr(summaries,'input_fingerprint',lambda *a:'fixed-source')
    with pytest.raises(ValueError,match='offline release'):summaries.ensure_current(root,'MetaVir','yes',include_preprints=True)
    build.assert_not_called()
    assert summaries.summary_dir(root,'MetaVir','yes',True,True)==root/'summary/yes/preprints_included/cohort_latest'


def test_complete_empty_release_explicitly_reuses_current_assets(tmp_path,monkeypatch):
    root=released(tmp_path,[]);monkeypatch.setattr(summaries,'input_fingerprint',lambda *a:'fixed-source');monkeypatch.setattr(summaries,'_current',lambda *a:True)
    build=Mock();monkeypatch.setattr(summaries.subprocess,'run',build)
    assert summaries.ensure_current(root,'metaPee','no',include_preprints=True)==root/'summary/no'
    assert summaries.summary_view_dir(root,'metaPee','no',False,True)==root/'summary/no'
    build.assert_not_called()
