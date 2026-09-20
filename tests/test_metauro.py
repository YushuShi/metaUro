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
    response = client.get(f'/settings?source={source}&scope={scope}&include_subexposures=no')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.select_one('#disease option[selected]')['data-scope-key'] == scope
    assert soup.select_one('#include-subexposures option[selected]')['value'] == 'no'
    assert soup.select_one('#include-subexposures').find_parent('section')['id'] == 'exposure-settings'
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


@pytest.mark.parametrize('source', ['urinary','prostate'])
def test_source_page_assets_and_summary(client, source):
    response = client.get(f'/evidence/{source}/')
    assert response.status_code == 200, response.data[:1000]
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.title.string.startswith('metaUro')
    assert len(soup.select('#disease option')) == 14
    assert not soup.select_one('#include-subexposures')
    assert 'Saved evidence' not in soup.header.get_text()
    selection_link = soup.select_one('.header-actions [data-uro-nav="selection"]')
    assert selection_link and selection_link['href'].startswith('/study-selection?')
    assert not soup.select_one('#analysis-settings-dialog')
    assert soup.select_one('link[href="/static/theme.css"]')
    assert not any(name in soup.get_text().lower() for name in ('metapee', 'metavir'))
    assert not any(name in element.get('href', '').lower() for element in soup.select('a[href]') for name in ('metapee', 'metavir'))
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


@pytest.mark.parametrize('scope', ['urinary', 'prostate'])
def test_runtime_exposure_alias_is_presentation_only(client, scope):
    response = client.get(f'/evidence/{scope}/static/script.js')
    assert response.status_code == 200
    script = response.get_data(as_text=True)
    assert "'Olive oil intake': 'Olive oil'" in script
    assert 'const canonicalVal = val.trim();' in script
    assert 'const primaryVal = uiText(canonicalVal);' in script
    assert 'presentationText(term).toLocaleLowerCase()' in script
    assert 'elements.exposure.dataset.canonicalExposure = canonical;' in script
    assert 'const exposure = selectedExposureValue();' in script
    assert "'Quality Score', 'VERIFY', 'Exclusion Flags'" in script
    assert "'Quality Score', 'Verified', 'Exclusion Flags'" not in script


@pytest.mark.parametrize('scope', ['urinary', 'prostate'])
def test_requested_labels_cover_templates_runtime_exports_and_localization(client, scope):
    response = client.get(f'/evidence/{scope}/')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.select_one('#download-baujat-png').get_text(strip=True) == 'Download'
    headers = [heading.get_text(' ', strip=True) for heading in soup.select('#studies-table thead th')]
    assert 'VERIFY' in headers
    assert 'Verified' not in headers

    translations = client.get(f'/evidence/{scope}/static/i18n-translations.json').json
    assert translations['Download'] == {
        'zh-CN': '下载', 'zh-TW': '下載', 'nl': 'Downloaden', 'ko': '다운로드',
    }
    assert translations['VERIFY'] == {
        'zh-CN': '核实', 'zh-TW': '驗證', 'nl': 'Verifiëren', 'ko': '검증',
    }


def test_chatgpt_track_remains_visible_and_settings_aware(client):
    response = client.get('/evidence/MetaVir/chatgpt')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.body['data-evidence-track'] == 'chatgpt'
    assert 'ChatGPT view' in soup.select_one('.evidence-track-label').get_text()
    settings = client.get('/settings?source=MetaVir&track=chatgpt')
    assert b'"track": "chatgpt"' in settings.data


@pytest.mark.parametrize('path', ['/about', '/settings?scope=bladder_cancer', '/settings?scope=aggressive_prostate_cancer'])
def test_product_pages_use_single_brand_and_infer_settings_scope(client, path):
    response = client.get(path)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.select_one('link[href="/static/theme.css"]')
    assert not any(name in soup.get_text().lower() for name in ('metapee', 'metavir', 'two evidence collections', 'source-specific engine'))
    assert not any(name in link.get('href', '').lower() for link in soup.select('a[href]') for name in ('metapee', 'metavir'))
    if 'aggressive_prostate' in path:
        assert soup.select_one('#disease option[selected]')['data-scope-key'] == 'aggressive_prostate_cancer'
        assert '/evidence/prostate/' in soup.select_one('[data-uro-nav="evidence"]')['href']


def test_about_page_matches_the_metafemina_presentation_structure(client):
    response = client.get('/about')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    container = soup.select_one('main.about-container')
    assert container
    assert len(soup.select('.background-globes .globe')) == 3
    assert container.select_one('.about-toolbar .back-link').get_text(strip=True) == '← Back to Tool'
    assert container.select_one('.disclaimer-box .disclaimer-title').get_text(' ', strip=True) == '⚠️ CAUTION: AI-Generated Results'
    assert [heading.get_text(' ', strip=True) for heading in container.select('h2')] == [
        'How It Works', 'Analysis Settings', 'Evidence Views', 'Study Selection',
    ]
    style = soup.style.get_text()
    assert 'max-width: 800px' in style
    assert 'background: rgba(255, 255, 255, 0.85)' in style
    assert 'background-color: #ffabc0' in style


@pytest.mark.parametrize('scope', ['bladder_cancer', 'aggressive_prostate_cancer'])
def test_selection_page_uses_review_scope_and_preserves_context(client, scope):
    response = client.get(f'/study-selection?scope={scope}&include_subexposures=no&track=chatgpt')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.select_one('.header-actions [aria-current="page"]').text == 'Study selection'
    assert soup.select_one('.prisma-figure')
    assert len(soup.select('.flow-stage')) == 4
    assert 'Not recorded' in soup.get_text()
    assert 'full-text assessment when needed' in soup.get_text()
    assert soup.select_one('a[href="https://doi.org/10.1371/journal.pmed.1003583"]')
    assert not any(name in soup.get_text().lower() for name in ('metapee', 'metavir'))
    assert not soup.select_one('#include-subexposures')
    ids = [node['id'] for node in soup.select('[id]')]
    assert len(ids) == len(set(ids))
    for link in soup.select('.selection-reviews a'):
        assert client.get(link['href']).status_code == 200
    for link in soup.select('link[rel="stylesheet"]'):
        if link['href'].startswith('/'):
            assert client.get(link['href']).status_code == 200
    assert client.get('/study-selection?group=unknown').status_code == 404
