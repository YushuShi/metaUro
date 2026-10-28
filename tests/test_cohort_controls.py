from bs4 import BeautifulSoup
import pytest

from app import app, decorate_html


@pytest.mark.parametrize('source', ['metaPee', 'MetaVir'])
def test_evidence_cohort_control_is_inline_and_defaults_to_latest(source):
    html = '<html><head><title>Evidence</title></head><body><main><section class="card"><h2>Extracted Studies</h2><div class="filters-container"></div><table id="studies-table"></table></section></main></body></html>'
    with app.test_request_context('/'):
        soup = BeautifulSoup(decorate_html(html, source, ''), 'html.parser')
    control = soup.select_one('.filters-container #evidence-keep-latest-cohort')
    assert control is not None
    assert control.find_previous_sibling('label')['for'] == control['id']
    assert control.select_one('option[selected]')['value'] == 'true'
    assert [option['value'] for option in control.select('option')] == ['false', 'true']


@pytest.mark.parametrize('source', ['metaPee', 'MetaVir'])
def test_summary_cohort_selector_has_its_own_group(source):
    html = '<html><head><title>Summary</title></head><body><main><form class="summary-scope-form"><div class="input-group"><label for="summary-scope">Disease scope</label><select id="summary-scope"></select></div></form></main></body></html>'
    with app.test_request_context('/summary?latest_cohort=yes'):
        soup = BeautifulSoup(decorate_html(html, source, 'summary'), 'html.parser')
    group = soup.select_one('form .summary-cohort-control')
    assert group.select_one('label')['for'] == 'summary-cohort-mode'
    assert group.select_one('select')['name'] == 'latest_cohort'
    assert group.select_one('option[selected]')['value'] == 'yes'
    assert soup.select_one('#summary-scope').find_parent(class_='input-group') is not group


@pytest.mark.parametrize('source', ['metaPee', 'MetaVir'])
@pytest.mark.parametrize('choice', [None, False, True])
def test_analysis_forwards_latest_default_and_preserves_explicit_choice(monkeypatch, source, choice):
    import json
    import types
    import app as web
    captured = {}
    def request(**message):
        captured.update(json.loads(message['data']))
        return 200, [('Content-Type', 'application/json')], b'{"success":true,"studies":[]}'
    monkeypatch.setitem(web.workers, source, types.SimpleNamespace(request=request))
    settings = {} if choice is None else {'keep_latest_cohort': choice}
    payload = {'analysis_settings': settings} if choice is not None else {}
    response = web.app.test_client().post(f'/evidence/{source}/analyze', json=payload)
    assert response.status_code == 200
    assert captured['analysis_settings']['keep_latest_cohort'] is (True if choice is None else choice)
