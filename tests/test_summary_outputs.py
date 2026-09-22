import json
from pathlib import Path
import subprocess

import pytest

import summary_outputs as outputs

ROOT = Path(__file__).resolve().parents[1]


def test_shared_summary_directory_and_legacy_output_redirection():
    assert outputs.summary_dir(ROOT, 'metaPee', 'yes') == outputs.summary_dir(ROOT, 'MetaVir', 'yes')
    for source in outputs.SOURCES:
        source_root = ROOT / 'sources' / source
        for mode in outputs.MODES:
            cache, folder, selected = outputs.resolve_build_paths(
                source_root, source_root / 'Cached_results', source_root / 'Plot/subexposures' / mode)
            assert selected == mode
            assert folder == ROOT / 'summary' / mode
            assert cache == outputs.cache_dir(ROOT, source, mode)
            legacy = source_root / 'Plot/subexposures' / mode / 'locales/nl/forest_harmful_test.pdf'
            assert outputs.canonical_asset_path(source_root, legacy) == folder / 'locales/nl/forest_harmful_test.pdf'


def test_stale_or_modified_summary_is_rebuilt_before_return(tmp_path, monkeypatch):
    folder = outputs.summary_dir(tmp_path, 'metaPee', 'yes')
    folder.mkdir(parents=True)
    asset = folder / 'exposures_meta_analysis_test_combined.xlsx'
    fingerprint = ['first']
    builds = []

    def build(*args, **kwargs):
        builds.append(fingerprint[0])
        asset.write_bytes(fingerprint[0].encode())
        manifest = {'scopes': {'test': {'workbooks': {'combined': {'filename': asset.name}}}},
                    'sources': {'metaPee': {'input_sha256': fingerprint[0],
                                             'scopes': ['test'], 'artifacts': [asset.name]}},
                    'artifacts': {asset.name: {'sha256': outputs.sha(asset)}}}
        (folder / 'summary_manifest.json').write_text(json.dumps(manifest))

    monkeypatch.setattr(outputs, 'input_fingerprint', lambda *args: fingerprint[0])
    monkeypatch.setattr(outputs, 'cache_dir', lambda *args: tmp_path / 'cache')
    monkeypatch.setattr(outputs.subprocess, 'run', build)
    outputs.ensure_current(tmp_path, 'metaPee')
    outputs.ensure_current(tmp_path, 'metaPee')
    assert builds == ['first']
    fingerprint[0] = 'changed evidence or code'
    outputs.ensure_current(tmp_path, 'metaPee')
    assert asset.read_bytes() == fingerprint[0].encode()
    asset.write_bytes(b'corrupt')
    outputs.ensure_current(tmp_path, 'metaPee')
    assert len(builds) == 3

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, 'builder')

    monkeypatch.setattr(outputs.subprocess, 'run', fail)
    fingerprint[0] = 'new change'
    with pytest.raises(subprocess.CalledProcessError):
        outputs.ensure_current(tmp_path, 'metaPee')


def test_failed_build_keeps_published_files_and_success_recovers_bad_manifest(tmp_path, monkeypatch):
    (tmp_path / 'source_worker.py').write_text('')
    (tmp_path / 'summary_outputs.py').write_text('')
    source = tmp_path / 'sources/metaPee'
    source.mkdir(parents=True)
    folder = tmp_path / 'summary/no'
    folder.mkdir(parents=True)
    filename = 'exposures_meta_analysis_test_combined.xlsx'
    (folder / filename).write_bytes(b'published')
    (folder / 'summary_manifest.json').write_text('{truncated')
    monkeypatch.setattr(outputs, 'cache_dir', lambda *args: tmp_path / 'cache')
    monkeypatch.setattr(outputs, 'input_fingerprint', lambda *args: 'fixed')

    def broken(cache, stage, languages):
        (stage / filename).write_bytes(b'incomplete')
        raise RuntimeError('Simulated renderer failure')

    with pytest.raises(RuntimeError, match='renderer failure'):
        outputs.run_summary_build(source, tmp_path / 'cache', folder, None, broken)
    assert (folder / filename).read_bytes() == b'published'
    assert (folder / 'summary_manifest.json').read_text() == '{truncated'
    assert not list((tmp_path / 'summary').glob('.building-*'))

    def complete(cache, stage, languages):
        (stage / filename).write_bytes(b'complete')
        manifest = {'scopes': {'test': {'workbooks': {'combined': {'filename': filename}}}}}
        manifest = outputs.finalize_build(source, stage, manifest)
        (stage / 'summary_manifest.json').write_text(json.dumps(manifest))
        return manifest

    outputs.run_summary_build(source, tmp_path / 'cache', folder, None, complete)
    assert (folder / filename).read_bytes() == b'complete'
    assert outputs._current(folder, 'fixed', 'metaPee')
    assert not list((tmp_path / 'summary').glob('.previous-*'))


def test_all_summary_artifacts_have_one_canonical_location():
    paths = subprocess.check_output(['rg', '--files', '-uuu'], cwd=ROOT, text=True).splitlines()
    misplaced = [name for name in paths if outputs.is_summary_artifact(name)
                 and not name.startswith(('summary/', '.git/', '.sources/'))]
    assert misplaced == []
    for mode in outputs.MODES:
        folder = ROOT / 'summary' / mode
        manifest = json.loads((folder / 'summary_manifest.json').read_text())
        assert set(manifest['sources']) == set(outputs.SOURCES)
        assert len(manifest['scopes']) == 8
        assert set(manifest['artifacts']) == outputs.declared_artifacts(manifest)
        assert all(outputs.sha(folder / name) == value['sha256']
                   for name, value in manifest['artifacts'].items())


@pytest.mark.parametrize('public,scope', [
    ('urinary', 'bladder_cancer'), ('urinary', 'kidney_and_renal_pelvis_cancer'),
    ('urinary', 'renal_cell_carcinoma'), ('prostate', 'aggressive_prostate_cancer'),
    ('prostate', 'prostate_cancer'),
])
@pytest.mark.parametrize('mode', outputs.MODES)
def test_downloads_serve_the_canonical_file(public, scope, mode):
    from app import app
    client = app.test_client()
    response = client.get(f'/evidence/{public}/summary/workbooks/{scope}/combined?include_subexposures={mode}')
    expected = ROOT / 'summary' / mode / f'exposures_meta_analysis_{scope}_combined.xlsx'
    assert response.status_code == 200
    assert response.data == expected.read_bytes()
    response = client.get(f'/evidence/{public}/summary/plots/{scope}/effect-heterogeneity?include_subexposures={mode}&lang=nl')
    expected = ROOT / 'summary' / mode / 'locales/nl' / f'plot_es_vs_heterogeneity_{scope}.pdf'
    assert response.status_code == 200
    assert response.data == expected.read_bytes()
