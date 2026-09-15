from pathlib import Path
import sys
import json
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from automation.aggregate_common import validate_release, sha


def fixture(root):
    p = root / 'data/monthly_updates/2026-08-30_2026-09-27/monthly_run_manifest.json'
    p.parent.mkdir(parents=True)
    for name in ('candidates.json', 'final_screening.json', 'monthly_application_gate.json'):
        (p.parent / name).write_text('{}')
    csv = p.parent / 'review.csv'; csv.write_text('context_id,human_decision\n')
    output = root / 'combined/cache.json'; output.parent.mkdir(); output.write_text('{}')
    p.write_text(json.dumps({
        'status': 'completed', 'repository': 'metaPee', 'run_id': p.parent.name, 'end_date': '2026-09-27',
        'raw_packet_sha256': {name: sha(p.parent / name) for name in ('candidates.json', 'final_screening.json')},
        'review_artifacts_sha256': {str(csv.relative_to(root)): sha(csv)},
        'application_gate_sha256': sha(p.parent / 'monthly_application_gate.json'),
        'ui_refresh': {'status': 'refreshed', 'output_sha256': {'combined/cache.json': sha(output)}}}))
    state = root / 'data/monthly_automation/state.json'; state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'status': 'completed', 'repository': 'metaPee', 'run_id': p.parent.name,
        'last_successful_publication_date': '2026-09-27', 'manifest_path': str(p.relative_to(root)),
        'manifest_sha256': sha(p)}))
    return p, state


def test_missing_or_wrong_window_source_does_not_refresh(tmp_path):
    from datetime import date
    assert validate_release(tmp_path) is None
    fixture(tmp_path)
    assert validate_release(tmp_path, date(2026, 10, 27)) is None
    assert validate_release(tmp_path, date(2026, 8, 27)) is None
    assert validate_release(tmp_path, date(2026, 9, 27))['repository'] == 'metaPee'


def test_tampered_manifest_is_rejected(tmp_path):
    p, _ = fixture(tmp_path); p.write_text('{}')
    with pytest.raises(ValueError):
        validate_release(tmp_path)


@pytest.mark.parametrize('filename', ['candidates.json', 'final_screening.json', 'monthly_application_gate.json', 'review.csv', 'output'])
def test_changed_release_artifact_blocks_aggregation(tmp_path, filename):
    manifest, _ = fixture(tmp_path)
    target = tmp_path / 'combined/cache.json' if filename == 'output' else manifest.parent / filename
    target.write_text('changed after publication')
    with pytest.raises(ValueError, match='checksum'):
        validate_release(tmp_path)


def test_missing_ui_receipts_are_not_a_valid_release(tmp_path):
    path, state_path = fixture(tmp_path)
    manifest = json.loads(path.read_text()); del manifest['ui_refresh']
    path.write_text(json.dumps(manifest))
    state = json.loads(state_path.read_text()); state['manifest_sha256'] = sha(path)
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='UI refresh'):
        validate_release(tmp_path)


def test_retry_after_midnight_uses_same_monthly_cutoff():
    from datetime import date
    from automation.aggregate_common import monthly_cutoff
    assert monthly_cutoff(date(2026, 9, 28)) == date(2026, 9, 27)
    assert monthly_cutoff(date(2026, 9, 29)) == date(2026, 9, 27)
    assert monthly_cutoff(date(2026, 10, 1)) == date(2026, 9, 27)
    assert monthly_cutoff(date(2027, 1, 1)) == date(2026, 12, 27)
