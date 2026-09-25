"""Updated case controls run above unchanged, verifiable source snapshots."""
import hashlib
import importlib.util
import json
from pathlib import Path

from bs4 import BeautifulSoup

import app as web

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_modules_match_their_manifest_and_preserve_snapshot_bytes():
    manifest = json.loads((ROOT / 'runtime_code/manifest.json').read_text())
    for source, entry in manifest['sources'].items():
        for relative, hashes in entry['files'].items():
            original = ROOT / 'sources' / source / relative
            runtime = ROOT / 'runtime_code' / source / relative
            assert hashlib.sha256(original.read_bytes()).hexdigest() == hashes['snapshot_sha256']
            assert hashlib.sha256(runtime.read_bytes()).hexdigest() == hashes['runtime_sha256']


def test_wrapper_loads_current_case_logic_and_default_zero():
    html = '<html><head><title>Source</title></head><body><header></header><main><p id="forest-scale-label">verbose scale</p><p id="analysis-baseline-notice">verbose risk</p><details class="poolability-help"><summary>Pooling requirements</summary></details><input id="filter-min-cases" value="50" step="50"></main><script src="/static/script.js"></script></body></html>'
    with web.app.test_request_context('/evidence/urinary/'):
        result = BeautifulSoup(web.decorate_html(html, 'metaPee', ''), 'html.parser')
    control = result.select_one('#filter-min-cases')
    assert control['value'] == '0' and control['step'] == '1'
    assert result.select_one('script[src="/static/case-counts.js?v=20260918"]')
    assert not result.select_one('#forest-scale-label')
    assert not result.select_one('#analysis-baseline-notice')
    assert not result.select_one('.poolability-help')
    assert 'Advanced Meta-Analysis & Evidence Synthesis' not in result.get_text()
    client = web.app.test_client()
    response = client.get('/evidence/urinary/static/script.js')
    assert response.status_code == 200
    assert b'min_cases: window.CaseCounts.minimum' in response.data
    assert b'window.CaseCounts.refresh' in response.data


def test_forest_labels_are_citation_only_but_keep_source_identity():
    study = {
        'Study': 'Smith AB et al. (2020) [PMID: 12345] — estimate 1 · Mixed',
        'Authors': 'Smith AB, Jones C', 'Year': '2020', 'PMID': '12345',
        'Displayed Effect Size': 1.2, 'Displayed Lower CI': 1.0, 'Displayed Upper CI': 1.4,
    }
    context = {'settings': {'effect_measure': 'RR'}, 'baseline': {'p0': .1}, 'repository': 'metaUro'}
    for source in ('metaPee', 'MetaVir'):
        path = ROOT / 'runtime_code' / source / 'analysis_settings_backend.py'
        spec = importlib.util.spec_from_file_location(f'runtime_{source}', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = module.forest_annotation_rows([study, study], {}, context)
        assert [row['study'] for row in rows] == [
            'Smith (2020) [PMID: 12345]', 'Smith (2020) [PMID: 12345]',
        ]
        assert all(row['source_study'] == study['Study'] for row in rows)
        assert all('estimate' not in row['study'].lower() and 'Mixed' not in row['study'] for row in rows)


def test_leave_one_out_labels_are_citation_only_but_keep_source_identity():
    saved = [
        {
            'omitted': 'Dianatinasab M et al. (2021) [PMID: 34036467] — estimate 1 · Men and women',
            'pooled_es': 1.11, 'ci_low': .99, 'ci_upp': 1.25,
        },
        {
            'omitted': 'Rosato V et al. (2018) [PMID: 29570985] — estimate 1 · Mixed [stratum 2]',
            'pooled_es': 1.13, 'ci_low': 1.0, 'ci_upp': 1.28,
        },
    ]
    for source in ('metaPee', 'MetaVir'):
        path = ROOT / 'runtime_code' / source / 'analysis_settings_backend.py'
        spec = importlib.util.spec_from_file_location(f'runtime_loo_{source}', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = module.normalize_leave_one_out_rows(saved)
        assert [row['omitted'] for row in rows] == [
            'Dianatinasab (2021) [PMID: 34036467]',
            'Rosato (2018) [PMID: 29570985]',
        ]
        assert [row['source_omitted'] for row in rows] == [row['omitted'] for row in saved]
        assert [row['pooled_es'] for row in rows] == [1.11, 1.13]
