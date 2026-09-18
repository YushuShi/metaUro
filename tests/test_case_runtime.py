"""Updated case controls run above unchanged, verifiable source snapshots."""
import hashlib
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
    html = '<html><head><title>Source</title></head><body><header></header><main><input id="filter-min-cases" value="50" step="50"></main><script src="/static/script.js"></script></body></html>'
    with web.app.test_request_context('/evidence/urinary/'):
        result = BeautifulSoup(web.decorate_html(html, 'metaPee', ''), 'html.parser')
    control = result.select_one('#filter-min-cases')
    assert control['value'] == '0' and control['step'] == '1'
    assert result.select_one('script[src="/static/case-counts.js?v=20260918"]')
    client = web.app.test_client()
    response = client.get('/evidence/urinary/static/script.js')
    assert response.status_code == 200
    assert b'min_cases: window.CaseCounts.minimum' in response.data
    assert b'window.CaseCounts.refresh' in response.data
