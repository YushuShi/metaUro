"""Scientific source-view invariants, independent of model/retrieval services."""
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from preprint_ui import (PreprintViewError, compose_preprint_view,
    install_common_runtime, preprint_status, preprint_variant_root, record_identity)


SELECTOR = dict(source_repo='fixture', disease='Disease', exposure='Coffee',
                outcome='Incidence', include_subexposures='yes', source_variant='combined')


@pytest.fixture
def release(tmp_path):
    source = tmp_path / 'cache.json'
    old = {'Effect ID': 'old', 'PMID': '123', 'Effect Size': 1.2, 'Quality Score': 'Good'}
    source.write_text(json.dumps({'studies': [old]}))
    context = dict(context_id='reviewed-context', selector=SELECTOR,
        operation='replace', original_record_ids=['id:old'],
        review=dict(status='settled', decision='include'),
        publication_anchor=dict(pmid='123', journal_publication_date='2020-06-01'),
        preprint_source=dict(id='PPR1', version='2', posted_date='2019-03-01', source_sha256='a'*64),
        baseline_files={'cache.json': sha256(source.read_bytes()).hexdigest()},
        native_rows=[{'Effect ID': 'recovery:1:v2', 'PMID': '123', 'Effect Size': .8,
                      'Quality Score': 'Good', 'preprint_original_record_ids': ['id:old']}])
    manifest = dict(schema_version=1, repository='fixture', status='complete', contexts=[context])
    path = tmp_path / 'data/preprint_recovery/active_manifest.json'
    path.parent.mkdir(parents=True)
    gate_record = dict(pmid='123', availability='pmcid_missing', pmcids=[],
        preprint_fetch_allowed=True, model_dispatch_authorized=True,
        saved_any_track_checked=True, saved_any_track_full_text_available=False,
        any_track_gate_record_sha256='f'*64)
    gate_path = path.parent/'article_availability_gate.json'
    gate_path.write_text(json.dumps(dict(schema_version=1, records=[gate_record])))
    context['article_availability_gate'] = dict(path=str(gate_path.relative_to(tmp_path)), pmid='123',
        sha256=sha256(gate_path.read_bytes()).hexdigest(),
        record_sha256=sha256(json.dumps(gate_record, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest())
    def save():
        path.write_text(json.dumps(manifest))
    save()
    return tmp_path, old, context, manifest, save


def test_disabled_is_same_object_and_never_opens_optional_manifest(release, monkeypatch):
    root, old, *_ = release
    rows = [old]
    monkeypatch.setattr(Path, 'read_text', lambda *args, **kwargs: pytest.fail('OFF read a manifest'))
    selected, audit = compose_preprint_view(rows, root, enabled=False)
    assert selected is rows and not audit['enabled']


def test_replacement_is_single_trusted_source_with_separate_journal_date(release):
    root, old, _, _, _ = release
    before = deepcopy(old)
    selected, audit = compose_preprint_view([old], root, True, SELECTOR, allow_additions=True)
    assert len(selected) == 1 and selected[0]['Effect Size'] == .8
    assert selected[0]['PMID'] == '123'
    assert selected[0]['publication_date'] == '2020-06-01'
    assert selected[0]['preprint_source']['posted_date'] == '2019-03-01'
    assert selected[0]['preprint_derived'] and not selected[0]['preprint_peer_reviewed']
    assert old == before and audit['replaced_record_ids'] == ['id:old']


def test_repeat_update_rebinds_recovered_values_and_keeps_selection_id(release):
    root, old, *_ = release
    selected, _ = compose_preprint_view([old], root, True, SELECTOR, allow_additions=True)
    selected[0]['Effect Size'] = 999
    selected[0]['Cache Row ID'] = 'native-selection'
    selected, _ = compose_preprint_view(selected, root, True, SELECTOR)
    assert len(selected) == 1 and selected[0]['Effect Size'] == .8
    assert selected[0]['Cache Row ID'] == 'native-selection'


def test_deselect_all_does_not_revive_preprints(release):
    root, *_ = release
    rows = []
    assert compose_preprint_view(rows, root, True, SELECTOR)[0] is rows


def test_additions_require_source_analyze_or_explicit_recovery_selection(release):
    root, _, context, _, save = release
    context.update(operation='add', original_record_ids=[])
    context['native_rows'][0]['preprint_original_record_ids'] = []
    save()
    assert compose_preprint_view([], root, True, SELECTOR)[0] == []
    assert len(compose_preprint_view([], root, True, SELECTOR, allow_additions=True)[0]) == 1
    assert len(compose_preprint_view([], root, True, SELECTOR, selected_record_ids=['id:recovery:1:v2'])[0]) == 1


def test_partial_selection_requires_explicit_effect_mapping(release):
    root, old, context, _, save = release
    context['original_record_ids'].append('id:second')
    context['native_rows'].append({'Effect ID': 'recovery:2', 'PMID': '123', 'Effect Size': .9,
        'preprint_original_record_ids': ['id:second']})
    save()
    selected, _ = compose_preprint_view([old], root, True, SELECTOR)
    assert [row['Effect ID'] for row in selected] == ['recovery:1:v2']
    for row in context['native_rows']:
        row.pop('preprint_original_record_ids')
    save()
    assert compose_preprint_view([old], root, True, SELECTOR)[0] == [old]


@pytest.mark.parametrize('review', [dict(status='manualReview', decision='include'),
    dict(status='settled', decision='exclude'), dict(status='pending', decision='include')])
def test_unresolved_or_rejected_preprint_never_removes_journal_evidence(release, review):
    root, old, context, _, save = release
    context['review'] = review
    save()
    rows = [old]
    assert compose_preprint_view(rows, root, True, SELECTOR, allow_additions=True)[0] is rows


@pytest.mark.parametrize('field,value', [('source_repo','other'), ('disease','Other disease'),
    ('outcome','Mortality'), ('include_subexposures','no'), ('source_variant','chatgpt'), ('exposure','coffee')])
def test_no_fuzzy_or_cross_view_substitution(release, field, value):
    root, old, *_ = release
    rows = [old]
    assert compose_preprint_view(rows, root, True, {**SELECTOR,field:value}, allow_additions=True)[0] is rows


def test_enabled_stale_source_fails_closed(release):
    root, old, *_ = release
    (root / 'cache.json').write_text('{}')
    with pytest.raises(PreprintViewError, match='binding changed'):
        compose_preprint_view([old], root, True, SELECTOR, allow_additions=True)


def test_duplicate_versions_cannot_count_same_journal_evidence_twice(release):
    root, old, context, manifest, save = release
    other = deepcopy(context)
    other['context_id'] = 'second-version'
    other['native_rows'][0]['Effect ID'] = 'recovery:1:v3'
    manifest['contexts'].append(other)
    save()
    with pytest.raises(PreprintViewError, match='Multiple preprint versions'):
        compose_preprint_view([old], root, True, SELECTOR, allow_additions=True)


def test_empty_release_and_namespace_preserve_defaults(release):
    root, old, _, manifest, save = release
    manifest['contexts'] = []
    save()
    rows = [old]
    assert preprint_status(root)['contexts'] == 0
    assert compose_preprint_view(rows, root, True, SELECTOR)[0] is rows
    path = Path('Plot/cohort_latest')
    assert preprint_variant_root(path, False) is path
    assert preprint_variant_root(path, True) == Path('Plot/preprints_included/cohort_latest')


def test_native_cache_view_is_overlaid_before_selection_binder(release):
    from flask import Flask, jsonify, request, g
    root, old, *_ = release
    active = ContextVar('test_source_settings', default=None)
    app = Flask(__name__)
    namespace = {'__file__': str(root/'app.py'), 'DEFAULT_DISEASE':'Disease',
        'load_json': lambda path, default: json.loads(Path(path).read_text()),
        'cache_row_id': lambda disease, exposure, index: f'cache:{index}'}
    @app.before_request
    def settings():
        data = request.get_json(silent=True) or {}
        g.token = active.set({'settings': {'include_preprints': data.get('include_preprints', False)}})
    @app.teardown_request
    def teardown(error):
        if hasattr(g, 'token'):
            active.reset(g.token)
            del g.token
    install_common_runtime(app, namespace, root, active.get)
    @app.post('/reanalyze')
    def reanalyze():
        submitted = request.json['studies']
        saved = namespace['load_json'](root/'cache.json', {})
        allowed = {record_identity(row) for row in saved['studies']}
        assert all(record_identity(row) in allowed for row in submitted)
        return jsonify(studies=submitted)
    client = app.test_client()
    request_data = {**SELECTOR, 'include_preprints':True, 'studies':[old]}
    # Runtime uses actual root source identity, supplied explicitly in native
    # deployments through META_PREPRINT_SOURCE_REPO.
    import preprint_ui
    original = preprint_ui.request_selector
    from unittest.mock import patch
    with patch.object(preprint_ui, 'request_selector', side_effect=lambda *args: SELECTOR):
        first = client.post('/reanalyze', json=request_data)
        assert first.status_code == 200
        row = first.json['studies'][0]
        assert row['Effect ID'] == 'recovery:1:v2' and row['Cache Row ID'] == 'cache:0'
        row['Effect Size'] = 500
        second = client.post('/reanalyze', json={**request_data,'studies':[row]})
        assert second.status_code == 200 and second.json['studies'][0]['Effect Size'] == .8
        assert client.post('/reanalyze', json={**request_data,'studies':[]}).json['studies'] == []
        off = client.post('/reanalyze', json={**request_data,'include_preprints':False,'studies':[row]})
        assert off.status_code == 409
