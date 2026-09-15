"""Public API regressions for the curator's publication-level retention choices.

The fixture captures retained scientific values from the release before this
deduplication. Live source folders are never needed by the tests.
"""
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import app
from source_worker import close_workers


BASELINE = json.loads((Path(__file__).parent /
                       'fixtures/dataset_deduplication_baseline.json').read_text())
CASES = BASELINE['cases']


@pytest.fixture(scope='module')
def client():
    close_workers()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client
    close_workers()


def _bundles(rows, pmid):
    return [{key: row.get(key) for key in BASELINE['fields']}
            for row in rows if str(row.get('PMID')) == pmid]


def _sorted_bundles(rows):
    # Sort by contrast/exposure while comparing numeric values without string
    # coercion, so e.g. 404 and 404.0 have the same scientific value.
    return sorted(rows, key=lambda row: (
        str(row.get('Direct Exposure')), str(row.get('comparison_type')),
        str(row.get('Effect Type')), str(row.get('Effect Size'))))


@pytest.mark.parametrize('mode', ['no', 'yes'])
@pytest.mark.parametrize('case', CASES, ids=[
    'urinary-epic', 'prostate-lycopene', 'prostate-omega3',
    'prostate-omega6', 'prostate-inherited-pufa', 'prostate-selenium',
    'prostate-vitamin-e', 'prostate-unrelated-tea',
])
def test_retained_articles_keep_their_estimates_and_rejected_articles_leave_the_view(
        client, case, mode):
    response = client.post(f'/evidence/{case["public_source"]}/analyze', json={
        'disease': case['disease'], 'exposure': case['exposure'],
        'outcome': 'Incidence', 'exclude_meta': True,
        'include_subexposures': mode,
    })
    assert response.status_code == 200, response.data[:1000]
    payload = response.get_json()
    assert not payload.get('error'), payload.get('error')
    assert isinstance(payload.get('studies'), list)
    assert response.headers['X-metaUro-Source'] == case['source']
    assert response.headers['Cache-Control'] == 'no-store'
    rows = payload['studies']
    if case['rejected_pmid']:
        assert not any(str(row.get('PMID')) == case['rejected_pmid'] for row in rows), (
            case['exposure'], case['disease'], mode, case['rejected_pmid'])
    actual = _bundles(rows, case['retained_pmid'])
    expected = case['modes'][mode]['retained_rows']
    assert _sorted_bundles(actual) == _sorted_bundles(expected)


def test_rejecting_lycopene_does_not_change_the_same_articles_tea_record():
    expected = BASELINE['unrelated_canonical']
    packet = json.loads((ROOT / expected['path']).read_text())
    records = [row for row in packet['results'] if row['context_id'] == expected['context_id']]
    assert len(records) == 1
    actual = hashlib.sha256(json.dumps(records[0], sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()
    assert actual == expected['sha256']
