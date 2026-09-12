"""Endpoint isolation across saved reads, review reports and summary downloads."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import app as web


@pytest.fixture
def client():
    web.app.config['TESTING'] = True
    return web.app.test_client()


def cache(root, disease, pmid):
    path = Path(web.get_cache_path(disease, 'Coffee', 'Incidence', True, cache_dir=str(root)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'canonical_track': 'chatgpt', 'studies': [{'PMID': pmid}]}))
    return path


@pytest.mark.parametrize('disease,subcategory', [
    ('Prostate cancer', 'aggressive_prostate_cancer'),
    ('Aggressive prostate cancer', None),
    ('aggressive_prostate_cancer', None),
])
def test_aggressive_saved_evidence_is_an_exact_endpoint(client, tmp_path, disease, subcategory):
    cache(tmp_path, 'Prostate cancer', '101')
    subtype = cache(tmp_path, 'Aggressive prostate cancer', '202')
    with patch('app.CHATGPT_CACHE_DIR', str(tmp_path)):
        response = client.post('/analyze', json={'disease': disease, 'subcategory': subcategory,
                                               'exposure': 'Coffee', 'exclude_meta': True, 'evidence_track': 'chatgpt'})
    assert response.status_code == 200
    payload = response.get_json()
    assert [row['PMID'] for row in payload['studies']] == ['202']
    assert payload['disease'] == 'Aggressive prostate cancer'
    assert subtype.name == 'aggressive_prostate_cancer_incidence_true_core.json'


def test_missing_subtype_never_uses_general_cache(client, tmp_path):
    cache(tmp_path, 'Prostate cancer', '101')
    with patch('app.CHATGPT_CACHE_DIR', str(tmp_path)):
        response = client.post('/analyze', json={'disease': 'Prostate cancer',
            'subcategory': 'aggressive_prostate_cancer', 'exposure': 'Coffee', 'exclude_meta': True, 'evidence_track': 'chatgpt'})
    assert response.status_code == 400
    assert 'No saved evidence' in response.get_json()['error']


def test_missing_aggressive_risk_is_explicit_and_user_risk_enables_planning(client, tmp_path):
    path = cache(tmp_path, 'Aggressive prostate cancer', '202')
    payload = json.loads(path.read_text())
    payload['studies'][0].update({'Study': 'Fixture et al. (2020)', 'Sample Size': 1000,
        'Cases': None, 'Effect Type': 'RR', 'Effect Size': 1.5, 'Lower CI': 1.1,
        'Upper CI': 2.0, 'Quality Score': 'Good', 'Disease Endpoint': 'aggressive'})
    path.write_text(json.dumps(payload))
    body = {'disease': 'Prostate cancer', 'subcategory': 'aggressive_prostate_cancer',
            'exposure': 'Coffee', 'exclude_meta': True, 'evidence_track': 'chatgpt', 'include_subexposures': 'no'}
    with patch('app.CHATGPT_CACHE_DIR', str(tmp_path)):
        missing = client.post('/analyze', json=body).get_json()
        manual = client.post('/analyze', json={**body, 'analysis_settings': {
            'baseline_source': 'manual', 'manual_risk_percent': 0.5}}).get_json()
    assert missing['baseline_risk']['available'] is False
    assert missing['baseline_risk']['manual_required'] is True
    assert 'Enter the aggressive prostate cancer incidence risk' in missing['baseline_risk']['reason']
    assert missing['sample_size']['available'] is False
    assert manual['baseline_risk']['p0'] == pytest.approx(0.005)
    assert manual['sample_size']['available'] is True


def test_reanalysis_resolves_aggressive_label(client):
    with patch('app.meta_analysis.perform_meta_analysis', return_value={'headline': {}}) as perform:
        response = client.post('/reanalyze', json={'disease': 'Prostate cancer',
            'subcategory': 'aggressive_prostate_cancer', 'studies': [
                {'PMID': '1', 'Quality Score': 'Good', 'Effect Type': 'RR', 'Effect Size': 1.1}]})
    assert response.status_code == 200
    assert perform.call_args.args[1] == 'Aggressive prostate cancer'


def test_review_target_and_context_are_subtype_specific(client, tmp_path):
    cache(tmp_path, 'Prostate cancer', '101')
    cache(tmp_path, 'Aggressive prostate cancer', '202')
    store = tmp_path / 'reviews.json'
    store.write_text('{}')
    client.get('/')
    with patch('app.CHATGPT_CACHE_DIR', str(tmp_path)), patch('app.VERIFICATIONS_FILE', str(store)):
        body = {'disease': 'Prostate cancer', 'subcategory': 'aggressive_prostate_cancer', 'exposure': 'Coffee', 'evidence_track': 'chatgpt'}
        wrong = client.post('/exclude', json={**body, 'pmid': '101'})
        right = client.post('/exclude', json={**body, 'pmid': '202'})
    assert wrong.status_code == 404
    assert right.status_code == 200
    assert right.get_json()['context_key'] == 'aggressive_prostate_cancer_coffee_incidence_chatgpt'


def test_main_and_summary_select_and_indent_aggressive_scope(client):
    for route in ['/', '/summary']:
        response = client.get(route + '?scope=aggressive_prostate_cancer&include_subexposures=no')
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'Disease Scope' in html
        assert 'data-subcategory="aggressive_prostate_cancer"' in html
        assert '&nbsp;&nbsp;&nbsp;&nbsp;Aggressive prostate cancer' in html
        assert 'scope=aggressive_prostate_cancer' in html
        assert 'include_subexposures=no' in html
    assert client.get('/summary?scope=legacy_subtype').status_code == 400


def test_summary_does_not_fall_back_and_scopes_downloads(client, tmp_path):
    def scope(prefix):
        return {'plots': {name: {'available': name == 'forest-protective',
                                'filename': prefix + '.pdf'} for name in web.SUMMARY_PLOT_NAMES},
                'workbooks': {name: {'available': True, 'filename': prefix + '.xlsx',
                                     'exposure_count': 1} for name in ['combined', 'dietary']}}
    general, aggressive = scope('general'), scope('aggressive')
    aggressive['baseline_note'] = 'No published subtype baseline: HR ≈ RR ≈ OR approximation.'
    manifest = {'scopes': {'prostate_cancer': general}}
    with patch('app.load_summary_manifest', return_value=manifest), patch('app.PLOT_DIR', str(tmp_path)):
        missing = client.get('/summary?scope=aggressive_prostate_cancer').get_data(as_text=True)
        assert 'No saved summary is available for Aggressive prostate cancer' in missing
        assert 'summary/plots/prostate_cancer/' not in missing
        manifest['scopes']['aggressive_prostate_cancer'] = aggressive
        (tmp_path / 'aggressive.pdf').write_bytes(b'%PDF-aggressive-fixture')
        (tmp_path / 'aggressive.xlsx').write_bytes(b'aggressive-workbook-fixture')
        available = client.get('/summary?scope=aggressive_prostate_cancer').get_data(as_text=True)
        assert '/summary/plots/aggressive_prostate_cancer/' in available
        assert 'Saved summaries use the reference-risk assumptions below.' in available
        assert aggressive['baseline_note'] in available
        assert 'Personal risks entered in Settings apply to the main analysis page.' in available
        plot = client.get('/summary/plots/aggressive_prostate_cancer/forest-protective')
        workbook = client.get('/summary/workbooks/aggressive_prostate_cancer/combined')
        assert plot.data == b'%PDF-aggressive-fixture'
        assert workbook.data == b'aggressive-workbook-fixture'
        assert 'aggressive.xlsx' in workbook.headers['Content-Disposition']
        plot.close()
        workbook.close()
