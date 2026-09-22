"""Regression examples from the human review CSVs applied on 14 September 2026.

Exercise the public workers, including both saved subexposure projections, so
correcting the canonical record without rebuilding served evidence cannot pass.
"""
import hashlib
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import app
from source_worker import close_workers


@pytest.fixture(scope='module')
def client():
    # New workers are necessary after an offline evidence installation.
    close_workers()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client
    close_workers()


def _direct_rows(payload, pmid, exposure):
    return [row for row in payload['studies']
            if str(row.get('PMID')) == pmid
            and (row.get('Direct Exposure') or row.get('direct_exposure')
                 or row.get('subexposure_source') or exposure) == exposure]


def _one_reviewed_row(payload, pmid, exposure, *, estimate, interval, sample_size):
    rows = _direct_rows(payload, pmid, exposure)
    assert len(rows) == 1, f'{pmid}: expected one retained direct estimate, found {len(rows)}'
    row = rows[0]
    assert float(row['Sample Size']) == sample_size
    assert float(row['Effect Size']) == pytest.approx(estimate, rel=1e-7)
    assert (float(row['Lower CI']), float(row['Upper CI'])) == pytest.approx(interval, rel=1e-7)
    assert not row.get('manual_review_pending')
    assert not row.get('requires_key_review')
    return row


def _check_mediterranean(payload, mode):
    # metaPee-0125 replaces a continuous estimate with the human-selected HR.
    _one_reviewed_row(payload, '27149545', 'Mediterranean diet', estimate=.89,
                      interval=(.62, 1.26), sample_size=37442)
    # metaPee-0157 agrees with accepted pair 1; pairs 2/3 must not duplicate it.
    _one_reviewed_row(payload, '30737562', 'Mediterranean diet', estimate=.85,
                      interval=(.77, .93), sample_size=646222)


def _check_alcohol(payload, mode):
    # metaPee-0002 selects ChatGPT; metaPee-0003 deletes its duplicate pair.
    _one_reviewed_row(payload, '11159145', 'Alcohol', estimate=1.63,
                      interval=(.865997191, 3.068023807), sample_size=120852)


def _check_beta_carotene(payload, mode):
    # metaVir-0301 requests inversion of the estimate and reversed CI bounds.
    # Checking the served values also catches a second accidental inversion.
    _one_reviewed_row(payload, '2449278', 'Beta-carotene', estimate=1 / 2.1,
                      interval=(1 / 4.47, 1 / .98), sample_size=300)


def _check_tea(payload, mode):
    # The review removes Tea for this report because its exposure is Green tea.
    # A valid child exposure may still appear when related exposures are enabled.
    assert not _direct_rows(payload, '27975054', 'Tea')
    rows = [row for row in payload['studies'] if str(row.get('PMID')) == '27975054']
    if mode == 'no':
        assert not rows
    else:
        assert all(str(row.get('Direct Exposure') or row.get('direct_exposure')
                       or row.get('subexposure_source')).casefold() == 'green tea' for row in rows)
        assert all(row.get('subexposure_inherited') is True for row in rows)


CASES = [
    pytest.param('metaPee', 'urinary', 'Bladder cancer', 'Mediterranean diet',
                 _check_mediterranean, id='urinary-custom-and-duplicate'),
    pytest.param('metaPee', 'urinary', 'Bladder cancer', 'Alcohol',
                 _check_alcohol, id='urinary-selected-pipeline-and-duplicate'),
    pytest.param('MetaVir', 'prostate', 'Prostate cancer', 'Beta-carotene',
                 _check_beta_carotene, id='prostate-reciprocal'),
    pytest.param('MetaVir', 'prostate', 'Prostate cancer', 'Tea',
                 _check_tea, id='prostate-excluded-direct-exposure'),
]


def _protected_hashes(source, mode):
    root = ROOT / 'sources' / source
    if source == 'metaPee':
        paths = [root / 'combined/effective_results.json',
                 root / 'combined/provisional_ui/manifest.json',
                 ROOT / f'summary/{mode}/summary_manifest.json']
    else:
        paths = [root / 'combined/final_screening.json',
                 root / f'Subexposure_results/{mode}/_merge_manifest.json',
                 ROOT / f'summary/{mode}/summary_manifest.json']
    paths.append(root / 'combined/human_review/current_decisions.json')
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


@pytest.mark.parametrize('mode', ['no', 'yes'])
@pytest.mark.parametrize('source,public_source,disease,exposure,check', CASES)
def test_human_corrections_reach_saved_and_settings_views_without_source_writes(
        client, source, public_source, disease, exposure, check, mode):
    before = _protected_hashes(source, mode)
    body = {'disease': disease, 'exposure': exposure, 'outcome': 'Incidence',
            'exclude_meta': True, 'include_subexposures': mode}
    # Omitted settings and explicit nondefault display settings use the same
    # corrected scientific rows, while each request produces its own analysis.
    for settings in (None, {'age': 60, 'years': 10, 'effect_measure': 'OR',
                            'forest_scale': 'both'}):
        payload = dict(body)
        if settings is not None:
            payload['analysis_settings'] = settings
        response = client.post(f'/evidence/{public_source}/analyze', json=payload)
        assert response.status_code == 200, response.data[:1000]
        data = response.get_json()
        assert not data.get('error'), data.get('error')
        assert data.get('studies'), (source, exposure, mode)
        assert response.headers['X-metaUro-Source'] == source
        assert response.headers['Cache-Control'] == 'no-store'
        check(data, mode)
        if source == 'MetaVir' and exposure == 'Tea':
            # The saved hierarchy has no Green tea -> Tea edge. Confirm the
            # accepted specific exposure remains served in its own analysis.
            green = client.post(f'/evidence/{public_source}/analyze',
                                json={**payload, 'exposure': 'Green tea'})
            assert green.status_code == 200, green.data[:1000]
            green_data = green.get_json()
            assert not green_data.get('error'), green_data.get('error')
            _one_reviewed_row(green_data, '27975054', 'Green tea', estimate=.4,
                              interval=(.08, 1.92), sample_size=280)
    assert _protected_hashes(source, mode) == before
