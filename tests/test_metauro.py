import hashlib
import json
from pathlib import Path
import sys

from bs4 import BeautifulSoup
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import (
    EGGERS_FEWER_THAN_TEN_MESSAGE,
    EGGERS_UNAVAILABLE_MESSAGE,
    UI_LABEL_TRANSLATIONS,
    app,
    current_egger_interpretation,
    prefix_json,
    prefix_text,
    scope_catalog,
)
from source_worker import close_workers


ROOT_SCOPE_LABELS = [
    'Bladder cancer', 'Kidney and renal pelvis cancer', 'Prostate cancer',
]
SUMMARY_SCOPE_IDS = {
    'bladder_cancer', 'urothelial_carcinoma_of_the_bladder',
    'kidney_and_renal_pelvis_cancer', 'renal_cell_carcinoma',
    'clear_cell_renal_cell_carcinoma', 'papillary_renal_cell_carcinoma',
    'prostate_cancer', 'aggressive_prostate_cancer', 'prostate_cancer_excluding_aggressive',
}


def assert_unified_scope_hierarchy(selector, expected_count):
    assert selector
    assert not selector.select('optgroup')
    options = selector.select('option')
    assert len(options) == expected_count
    roots = [
        option.get_text(' ', strip=True)
        for option in options
        if option['data-hierarchy-depth'] == '0'
    ]
    assert roots == ROOT_SCOPE_LABELS
    aggressive = selector.select_one('option[data-scope-key="aggressive_prostate_cancer"]')
    assert aggressive['data-parent-key'] == 'prostate_cancer'
    assert aggressive['data-hierarchy-depth'] == '1'


@pytest.fixture(scope='module')
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client
    close_workers()


def test_catalog_keeps_all_sources_and_exact_endpoints(client):
    scopes = client.get('/api/scopes').json
    assert len(scopes) == 15
    assert len([s for s in scopes if s['source'] == 'metaPee']) == 12
    assert {s['id'] for s in scopes if s['source'] == 'MetaVir'} == {'prostate_cancer', 'aggressive_prostate_cancer', 'prostate_cancer_excluding_aggressive'}
    by_id = {scope['id']: scope for scope in scopes}
    assert [by_id[scope]['hierarchy_depth'] for scope in (
        'bladder_cancer', 'kidney_and_renal_pelvis_cancer', 'prostate_cancer')] == [0, 0, 0]
    assert by_id['aggressive_prostate_cancer']['parent_id'] == 'prostate_cancer'
    assert by_id['aggressive_prostate_cancer']['hierarchy_depth'] == 1


@pytest.mark.parametrize('source,scope', [('metaPee', 'renal_cell_carcinoma'), ('MetaVir', 'aggressive_prostate_cancer')])
def test_settings_markup_is_complete_and_scope_aware(client, source, scope):
    response = client.get(f'/settings?source={source}&scope={scope}&include_subexposures=no')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert_unified_scope_hierarchy(soup.select_one('#disease'), 15)
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
    assert soup.title.string.startswith('MetaUro')
    assert_unified_scope_hierarchy(soup.select_one('#disease'), 15)
    assert not soup.select_one('#include-subexposures')
    assert 'Saved evidence' not in soup.header.get_text()
    selection_link = soup.select_one('.header-actions [data-uro-nav="selection"]')
    assert selection_link and selection_link['href'].startswith('/study-selection?')
    assert not soup.select_one('#analysis-settings-dialog')
    assert not soup.select_one('#forest-scale-label')
    assert not soup.select_one('#analysis-baseline-notice')
    assert not soup.select_one('.poolability-help')
    assert 'Advanced Meta-Analysis & Evidence Synthesis' not in soup.get_text()
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
    summary_selector = soup.select_one('#summary-scope, #summary-disease')
    assert_unified_scope_hierarchy(summary_selector, len(SUMMARY_SCOPE_IDS))
    assert {option['data-scope-key'] for option in summary_selector.select('option')} == SUMMARY_SCOPE_IDS
    text = soup.get_text(' ', strip=True)
    assert 'These are saved summary plots and workbooks.' not in text
    assert 'Urinary tract summaries' not in text
    assert 'Prostate summaries' not in text
    assert 'Summary eligibility:' not in text
    downloads = soup.select('a.summary-download')
    assert len(downloads) == 1
    assert downloads[0].get_text(' ', strip=True) == 'Download summary workbook'
    assert downloads[0]['href'].split('?', 1)[0].endswith('/combined')
    result = client.get(downloads[0]['href'])
    assert result.status_code == 200
    assert result.headers['Content-Type'].startswith(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def test_summary_workbook_link_tracks_selected_disease(client):
    response = client.get('/evidence/urinary/summary?scope=renal_cell_carcinoma&include_subexposures=no')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.select_one('#summary-scope option[selected]')['data-scope-key'] == 'renal_cell_carcinoma'
    link = soup.select_one('a.summary-download')
    assert link and '/summary/workbooks/renal_cell_carcinoma/combined' in link['href']


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
    assert data['headline']['funnel_interpretation'] == current_egger_interpretation(data, body)
    # Scientific rows stay backed by saved identities, even when display changes.
    if disease == 'Aggressive prostate cancer':
        assert 'aggressive' in json.dumps(data.get('baseline',data.get('analysis_settings',{}))).lower() or 'aggressive' in str(data.get('disease','')).lower()


def test_current_egger_interpretation_has_all_exact_branches():
    request_data = {'exposure': 'Omega-3 Fatty Acid', 'disease': 'Prostate Cancer'}

    def result(count, p_value):
        return {'studies': [{}] * count, 'headline': {'eggers_p': p_value}}

    assert current_egger_interpretation(result(9, 0.001), request_data) == EGGERS_FEWER_THAN_TEN_MESSAGE
    assert current_egger_interpretation(result(10, None), request_data) == EGGERS_UNAVAILABLE_MESSAGE
    assert current_egger_interpretation(result(10, 'not available'), request_data) == EGGERS_UNAVAILABLE_MESSAGE
    assert current_egger_interpretation(result(10, float('nan')), request_data) == EGGERS_UNAVAILABLE_MESSAGE
    assert current_egger_interpretation(result(10, float('inf')), request_data) == EGGERS_UNAVAILABLE_MESSAGE
    assert current_egger_interpretation(result(10, 0.0499), request_data) == (
        'Egger’s test indicates significant funnel plot asymmetry (p=0.0499) for the '
        'meta-analysis of Omega-3 Fatty Acid and Prostate Cancer. This finding suggests '
        'the presence of small-study effects, which may arise from publication bias or '
        'other sources of heterogeneity related to study size. The pooled estimate should '
        'therefore be interpreted with appropriate caution.'
    )
    assert current_egger_interpretation(result(10, 0.05), request_data) == (
        'Egger’s test does not indicate significant funnel plot asymmetry (p=0.0500) for '
        'the meta-analysis of Omega-3 Fatty Acid and Prostate Cancer. There is no statistical '
        'evidence of small-study effects based on this test, although the absence of '
        'significance does not rule out publication bias.'
    )


@pytest.mark.parametrize('scope', ['urinary', 'prostate'])
def test_runtime_egger_renderer_and_translation_catalog_are_current(client, scope):
    script = client.get(f'/evidence/{scope}/static/script.js').get_data(as_text=True)
    assert 'Egger’s test was not performed because fewer than {count} studies were available.' in script
    assert 'Egger’s test could not be reliably estimated for this meta-analysis.' in script
    assert 'Egger’s test indicates significant funnel plot asymmetry (p={p})' in script
    assert 'Egger’s test does not indicate significant funnel plot asymmetry (p={p})' in script
    assert 'indicates substantial publication bias' not in script
    assert 'appears symmetric, suggesting a lower risk of publication bias' not in script

    catalog = client.get(f'/evidence/{scope}/static/i18n-translations.json').json
    for key in UI_LABEL_TRANSLATIONS:
        assert key in catalog


def test_processed_meat_heterogeneity_and_leave_one_out_labels(client):
    response = client.post(
        '/evidence/urinary/analyze?include_subexposures=no',
        json={
            'disease': 'Bladder cancer', 'exposure': 'Processed meat intake',
            'outcome': 'Incidence', 'exclude_meta': True,
            'include_subexposures': 'no',
        },
    )
    assert response.status_code == 200
    headline = response.json['headline']
    from automation.sync_sources import assembled_source
    import json
    saved = json.loads((assembled_source(ROOT, 'metaPee')/'combined/provisional_ui/subexposure_views/no/Cached_results/processed_meat_intake/bladder_cancer_incidence_true_all.json').read_text())
    assert headline['i2'] == pytest.approx(saved['headline']['i2'])
    assert headline['loo_results']
    for row in headline['loo_results']:
        assert 'Men and women' not in row['omitted']
        assert 'Mixed' not in row['omitted']
        assert 'estimate' not in row['omitted'].lower()
        assert row['source_omitted']


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
    assert 'class="review-count"' in script
    assert 'onclick="window.verifyStudy' not in script
    assert 'onclick="window.excludeStudy' not in script
    assert "'Quality Score', 'VERIFY', 'Exclusion Flags'" in script
    assert "'Quality Score', 'Verified', 'Exclusion Flags'" not in script
    if scope == 'prostate':
        assert "endpointBadge.className = 'disease-endpoint-badge'" not in script


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


def test_chatgpt_track_remains_settings_aware_without_a_redundant_banner(client):
    response = client.get('/evidence/MetaVir/chatgpt')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.body['data-evidence-track'] == 'chatgpt'
    assert not soup.select_one('.evidence-track-label')
    settings = client.get('/settings?source=MetaVir&track=chatgpt')
    assert b'"track": "chatgpt"' in settings.data


@pytest.mark.parametrize('path', ['/about', '/settings?scope=bladder_cancer', '/settings?scope=aggressive_prostate_cancer'])
def test_product_pages_use_single_brand_and_infer_settings_scope(client, path):
    response = client.get(path)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.select_one('link[href="/static/theme.css"]')
    assert 'Advanced Meta-Analysis & Evidence Synthesis' not in soup.get_text()
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
    assert soup.select_one('header.site-header a.logo')
    assert not container.select_one('.about-toolbar')
    assert container.select_one('.disclaimer-box .disclaimer-title').get_text(' ', strip=True) == '⚠️ CAUTION: AI-Generated Results'
    assert [heading.get_text(' ', strip=True) for heading in container.select('h2')] == [
        'How It Works',
        'Understanding Meta-Analysis in Urologic Cancers',
        'JBI Quality Appraisal',
        'Bug Reporting & Feedback',
    ]
    assert {'Interpreting Heterogeneity (I²)', 'Exposure Quantification'} <= {
        heading.get_text(' ', strip=True) for heading in container.select('h3')
    }
    assert len(container.select('.jbi-tab')) == 4
    assert len(container.select('.jbi-panel')) == 4
    page_text = container.get_text(' ', strip=True)
    assert 'urologic cancers' in page_text.lower()
    assert "women's cancer" not in page_text.lower()
    assert 'breast, ovarian, or uterine cancer' not in page_text.lower()
    assert 'margauxdelporte@gmail.com' in page_text
    assert 'shiyushu2006@gmail.com' in page_text
    assert 'ziyu.e.liu.gr@dartmouth.edu' in page_text
    style = soup.style.get_text()
    assert 'max-width: 800px' in style
    assert 'background: rgba(255, 255, 255, 0.85)' in style
    assert 'background-color: #ffabc0' in style


def test_selection_page_is_exposure_specific_and_shows_criteria(client):
    response = client.get('/study-selection?scope=bladder_cancer&exposure=Vitamin%20D&include_subexposures=no&track=chatgpt')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert soup.select_one('.header-actions [aria-current="page"]').text == 'Study selection'
    assert soup.select_one('.prisma-figure')
    assert [section['aria-labelledby'] for section in soup.select('.flow-stage')] == [
        'phase-screening', 'phase-eligibility', 'phase-included', 'phase-pooled']
    assert soup.select_one('#selection-scope option[selected]')['value'] == 'bladder_cancer'
    assert soup.select_one('#selection-exposure option[selected]')['value'] == 'Vitamin D'
    text = soup.get_text(' ', strip=True)
    assert 'Bladder cancer · Vitamin D' in text
    assert 'Animal, laboratory, or preclinical study: 1' in text
    assert 'Publication is a review or meta-analysis: 14' in text
    assert 'without an eligible primary estimate' not in text
    assert 'Not recorded' not in text
    assert 'Identification' not in text
    assert 'How to read this diagram' not in text
    assert not soup.select_one('details.flow-reasons')
    assert soup.select_one('a[href="https://doi.org/10.1371/journal.pmed.1003583"]')
    assert not any(name in soup.get_text().lower() for name in ('metapee', 'metavir'))
    assert not soup.select_one('#include-subexposures')
    ids = [node['id'] for node in soup.select('[id]')]
    assert len(ids) == len(set(ids))
    for link in soup.select('link[rel="stylesheet"]'):
        if link['href'].startswith('/'):
            assert client.get(link['href']).status_code == 200
    assert client.get('/study-selection?group=unknown').status_code == 404


def test_selection_page_with_incomplete_scope_does_not_show_a_flow(client):
    response = client.get('/study-selection?scope=aggressive_prostate_cancer')
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    assert not soup.select_one('.prisma-figure')
    assert 'No complete selection flow is available for Aggressive prostate cancer' in soup.get_text()
    listed = {option['value'] for option in soup.select('#selection-scope option:not([disabled])')}
    assert {'bladder_cancer', 'prostate_cancer'} <= listed
