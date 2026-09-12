import json
from pathlib import Path
import subprocess

import pytest

from automation.sync_sources import assembled_source, generation, load_overlay, sha, sync_sources, write


def study(pmid):
    return {'PMID': str(pmid), 'Effect ID': str(pmid), 'Direct Exposure': 'Fiber',
            'Effect Type': 'RR', 'Effect Size': 0.8, 'Lower CI': 0.7, 'Upper CI': 0.9,
            'Sample Size': 1000, 'Cases': 100}


def git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def prepare(tmp_path):
    metauro, siblings = tmp_path / 'metaUro', tmp_path / 'siblings'
    cache = 'Subexposure_results/yes/fiber/cancer_incidence_true_all.json'
    for source in ('metaPee', 'MetaVir'):
        original = metauro / 'sources' / source
        write(original / cache, {'studies': [study(1)]})
        (original / 'app.py').write_text('# Original engine, never replaced\n')
        root = siblings / source
        run = root / 'data/monthly_updates/2026-09-01_2026-09-28'
        write(root / cache, {'studies': [study(1), study(2)]})
        for name in ('candidates.json', 'final_screening.json'):
            write(run / name, {'results': []})
        write(run / 'monthly_application_gate.json', {'blocked_context_ids': ['3|Fiber|Cancer']})
        review = run / 'discrepancies.csv'
        review.write_text('context_id,human_decision\n3|Fiber|Cancer,\n')
        release = {'status': 'completed', 'run_id': run.name, 'end_date': '2026-09-28',
                   'raw_packet_sha256': {name: sha(run / name) for name in ('candidates.json', 'final_screening.json')},
                   'review_artifacts_sha256': {str(review.relative_to(root)): sha(review)},
                   'application_gate_sha256': sha(run / 'monthly_application_gate.json'),
                   'ui_refresh': {'status': 'refreshed', 'output_sha256': {cache: sha(root / cache)}}}
        manifest = run / 'monthly_run.json'
        write(manifest, release)
        write(root / 'data/monthly_automation/state.json', {
            'status': 'completed', 'run_id': run.name, 'manifest_path': str(manifest.relative_to(root)),
            'manifest_sha256': sha(manifest), 'last_successful_publication_date': '2026-09-28',
            'completed_at': '2026-09-28T10:00:00+00:00', 'human_review_pending': 1})
        git(root, 'init', '-q')
        git(root, 'config', 'user.email', 'fixture@example.com')
        git(root, 'config', 'user.name', 'Fixture')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'Completed monthly fixture')
    return metauro, siblings, cache


def test_sync_updates_runtime_and_keeps_original_bytes(tmp_path):
    root, siblings, cache = prepare(tmp_path)
    original = root / 'sources/metaPee' / cache
    before = original.read_bytes()
    sync_sources(root, siblings)
    overlay = load_overlay(root, 'metaPee')
    assert overlay['human_review_pending'] == 1
    assert overlay['review_files'][0]['filename'] == 'discrepancies.csv'
    runtime = assembled_source(root, 'metaPee')
    assert runtime.name == 'metaPee'
    assert [row['PMID'] for row in json.loads((runtime / cache).read_text())['studies']] == ['1', '2']
    assert original.read_bytes() == before
    assert original.stat().st_ino != (runtime / cache).stat().st_ino
    assert (runtime / 'app.py').read_bytes() == (root / 'sources/metaPee/app.py').read_bytes()
    assert assembled_source(root, 'metaPee') == runtime


def test_both_sources_must_validate_before_install(tmp_path):
    root, siblings, _ = prepare(tmp_path)
    state = siblings / 'MetaVir/data/monthly_automation/state.json'
    write(state, {'status': 'failed'})
    with pytest.raises(ValueError, match='completed'):
        sync_sources(root, siblings)
    assert not (root / 'updates/manifest.json').exists()


def test_modified_output_fails_hash_validation(tmp_path):
    root, siblings, cache = prepare(tmp_path)
    (siblings / 'MetaVir' / cache).write_text('{}')
    with pytest.raises(ValueError, match='checksum'):
        sync_sources(root, siblings)
    assert not (root / 'updates/manifest.json').exists()


def test_changed_historical_estimate_is_not_imported(tmp_path):
    root, siblings, cache = prepare(tmp_path)
    write(root / 'sources/MetaVir' / cache, {'studies': [dict(study(1), **{'Effect Size': 0.95})]})
    with pytest.raises(ValueError, match='historical evidence'):
        sync_sources(root, siblings)
    assert not (root / 'updates/manifest.json').exists()


def test_overlay_tampering_is_detected_before_runtime_assembly(tmp_path):
    root, siblings, _ = prepare(tmp_path)
    sync_sources(root, siblings)
    overlay = load_overlay(root, 'metaPee')
    entry = next(iter(overlay['files'].values()))
    (root / 'updates/metaPee' / entry['stored_path']).write_text('{}')
    with pytest.raises(ValueError, match='checksum'):
        assembled_source(root, 'metaPee')


def test_idempotent_source_sync_keeps_same_generation(tmp_path):
    root, siblings, _ = prepare(tmp_path)
    sync_sources(root, siblings)
    previous = generation(root, 'metaPee')
    sync_sources(root, siblings)
    assert generation(root, 'metaPee') == previous


def test_monthly_page_serves_only_verified_csv(tmp_path, monkeypatch):
    root, siblings, _ = prepare(tmp_path)
    sync_sources(root, siblings)
    import app
    monkeypatch.setattr(app, 'ROOT', root)
    # The UI context uses immutable clinical catalogs; keep this test bounded to
    # the monthly page and its manifest-bound download behavior.
    monkeypatch.setattr(app, 'context', lambda *args: {
        'current': {'id': 'urinary'}, 'prefix': '/evidence/urinary', 'page': 'selection',
        'source': 'metaPee', 'track': 'combined', 'include_subexposures': 'yes'})
    client = app.app.test_client()
    response = client.get('/monthly-review/urinary')
    assert response.status_code == 200
    assert b'2026-09-28' in response.data and b'1 exposure' in response.data
    assert b'Download discrepancy review' in response.data
    download = client.get('/monthly-review/urinary/download/discrepancies.csv')
    assert download.status_code == 200 and b'3|Fiber|Cancer' in download.data
    assert client.get('/monthly-review/urinary/download/unlisted.csv').status_code == 404


def test_wrong_monthly_cutoff_is_not_published(tmp_path):
    root, siblings, _ = prepare(tmp_path)
    with pytest.raises(ValueError, match='publication cutoff'):
        sync_sources(root, siblings, require_through='2026-10-27')
    assert not (root / 'updates/manifest.json').exists()


def test_monthly_counts_stay_separate_from_historical_prisma(tmp_path, monkeypatch):
    root, siblings, _ = prepare(tmp_path)
    sync_sources(root, siblings)
    import study_selection
    monkeypatch.setattr(study_selection, 'ROOT', root)
    monkeypatch.setattr(study_selection, '_build_selection_flow', lambda source: {
        'screened': 12, 'included_reports': 7, 'cohort_description': 'Current review through August.', 'notes': []})
    flow = study_selection.build_selection_flow('urinary')
    assert flow['screened'] == 12 and flow['included_reports'] == 7
    assert flow['cohort_description'].startswith('Historical')
    assert flow['monthly']['human_review_pending'] == 1
    assert '2026-09-28' in flow['notes'][-1]


def test_missed_month_delta_is_retained_through_verified_lineage(tmp_path):
    from automation.sync_sources import _cumulative_outputs
    previous_path = tmp_path / 'data/monthly_updates/2026-08-01_2026-08-27/monthly_run_manifest.json'
    previous = {'run_id': previous_path.parent.name, 'status': 'completed',
                'ui_refresh': {'status': 'refreshed', 'output_sha256': {
                    'static/monthly/retained.png': 'first', 'static/monthly/replaced.png': 'older'}}}
    write(previous_path, previous)
    latest_path = tmp_path / 'data/monthly_updates/2026-08-28_2026-09-27/monthly_run_manifest.json'
    latest = {'run_id': latest_path.parent.name, 'status': 'completed',
              'watermark_source': {'kind': 'successful_release', 'run_id': previous_path.parent.name,
                                   'manifest_sha256': sha(previous_path)},
              'ui_refresh': {'status': 'refreshed', 'output_sha256': {
                  'static/monthly/replaced.png': 'newer'}}}
    result = _cumulative_outputs(tmp_path, latest, latest_path)
    assert result == {'static/monthly/retained.png': 'first', 'static/monthly/replaced.png': 'newer'}
    previous_path.write_text('{}')
    with pytest.raises(ValueError, match='Previous monthly release checksum'):
        _cumulative_outputs(tmp_path, latest, latest_path)
