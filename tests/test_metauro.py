import hashlib
import json
from pathlib import Path
import sys

from bs4 import BeautifulSoup
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import app, prefix_json, prefix_text, scope_catalog
from source_worker import close_workers


@pytest.fixture(scope='module')
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client
    close_workers()


def test_catalog_keeps_all_sources_and_exact_endpoints(client):
    scopes = client.get('/api/scopes').json
    assert len(scopes) == 14
    assert len([s for s in scopes if s['source'] == 'metaPee']) == 12
    assert {s['id'] for s in scopes if s['source'] == 'MetaVir'} == {'prostate_cancer', 'aggressive_prostate_cancer'}


@pytest.mark.parametrize('source,scope', [('metaPee', 'renal_cell_carcinoma'), ('MetaVir', 'aggressive_prostate_cancer')])
def test_settings_markup_is_complete_and_scope_aware(client, source, scope):
    response = client.get(f'/settings?source={source}&scope={scope}')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.select_one('#disease option[selected]')['data-scope-key'] == scope
    ids = [el['id'] for el in soup.select('[id]')]
    assert len(ids) == len(set(ids))
    for key in ['settings-sex','settings-race','settings-age','settings-years','settings-effect-measure','settings-forest-scale','settings-study-design','settings-manual-risk','settings-source-citation','settings-risk-context','reset-settings']:
        assert soup.select_one('#' + key)
    assert soup.select_one('script[src="/static/analysis-settings.js"]')
    assert soup.select_one('a[aria-current="page"]').text == 'Settings'


def test_rebase_limits_to_local_assets():
    prefix = '/evidence/metaPee'
    assert prefix_text("fetch('/analyze'); fetch('/api/config'); const x='/static/foo.png';", prefix) == "fetch('/evidence/metaPee/analyze'); fetch('/evidence/metaPee/api/config'); const x='/evidence/metaPee/static/foo.png';"
    assert prefix_text('https://example.org/static/x', prefix) == 'https://example.org/static/x'
    assert prefix_json({'plot':'static/x.png','citation':'https://example.org/static/x','image':'data:image/png;base64,abc'}, prefix) == {'plot':'/evidence/metaPee/static/x.png','citation':'https://example.org/static/x','image':'data:image/png;base64,abc'}


@pytest.mark.parametrize('source', ['metaPee','MetaVir'])
def test_mutations_and_invalid_manual_baselines_rejected(client, source):
    for route in ('verify', 'exclude'):
        assert client.post(f'/evidence/{source}/{route}',json={'pmid':'test'}).status_code == 403
    for risk in (0, 100, -1, True, 'NaN', None):
        response = client.post(f'/evidence/{source}/analyze',json={'analysis_settings':{'baseline_source':'manual','manual_risk_percent':risk}})
        assert response.status_code == 400
    assert client.post(f'/evidence/{source}/analyze', json=[]).status_code == 400


@pytest.mark.parametrize('source', ['metaPee','MetaVir'])
def test_source_page_assets_and_summary(client, source):
    response = client.get(f'/evidence/{source}/')
    assert response.status_code == 200, response.data[:1000]
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.title.string.startswith('metaUro')
    assert len(soup.select('#disease option')) == 14
    assert soup.select_one('#include-subexposures')
    assert not soup.select_one('#analysis-settings-dialog')
    for element in soup.select('script[src],link[rel="stylesheet"]'):
        url = element.get('src') or element.get('href')
        if url.startswith('/'):
            result = client.get(url)
            assert result.status_code == 200, url
    response = client.get(f'/evidence/{source}/summary')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert 'saved summary' in soup.get_text().lower()
    for link in soup.select('a[href]'):
        url = link['href']
        if '/summary/workbooks/' in url or '/summary/plots/' in url:
            assert client.get(url).status_code == 200, url
            break


@pytest.mark.parametrize('source,disease', [('metaPee','Bladder cancer'),('MetaVir','Prostate cancer'),('MetaVir','Aggressive prostate cancer')])
def test_saved_analysis_and_interactive_settings(client, source, disease):
    body = {'disease':disease,'exposure':'Vitamin D','outcome':'Incidence','exclude_meta':True,'include_subexposures':'yes',
            'analysis_settings':{'age':60,'years':10,'effect_measure':'OR','forest_scale':'both'}}
    response = client.post(f'/evidence/{source}/analyze',json=body)
    assert response.status_code == 200, response.data[:1000]
    data = response.json
    assert not data.get('error'), data.get('error')
    assert data.get('studies')
    assert response.headers['X-metaUro-Source'] == source
    assert response.headers['Cache-Control'] == 'no-store'
    # Scientific rows stay backed by saved identities, even when display changes.
    if disease == 'Aggressive prostate cancer':
        assert 'aggressive' in json.dumps(data.get('baseline',data.get('analysis_settings',{}))).lower() or 'aggressive' in str(data.get('disease','')).lower()


def test_invalid_source_does_not_dispatch(client):
    assert client.get('/evidence/unknown/').status_code == 404
    assert client.get('/settings?source=unknown').status_code == 404


def test_conditional_and_head_json_asset_requests(client):
    url = '/evidence/metaPee/static/seer-baseline.json'
    response = client.get(url)
    assert response.status_code == 200
    assert client.head(url).status_code == 200
    if response.headers.get('Last-Modified'):
        cached = client.get(url, headers={'If-Modified-Since': response.headers['Last-Modified']})
        assert cached.status_code == 304


def test_chatgpt_track_remains_visible_and_settings_aware(client):
    response = client.get('/evidence/MetaVir/chatgpt')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.body['data-evidence-track'] == 'chatgpt'
    assert 'Saved ChatGPT evidence' in soup.select_one('.uro-context').get_text()
    settings = client.get('/settings?source=MetaVir&track=chatgpt')
    assert b'"track": "chatgpt"' in settings.data
