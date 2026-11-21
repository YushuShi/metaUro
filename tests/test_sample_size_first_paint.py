"""The first document includes the default SVGs even before JavaScript runs."""
import json

from werkzeug.datastructures import MultiDict

import sample_size_defaults as defaults


def test_default_navigation_separates_scopes_and_custom_settings():
    catalog = {'diseases': [
        {'value': 'first', 'label': 'First scope'},
        {'value': 'second', 'label': 'Second scope', 'aliases': ['b::scope']},
    ], 'defaults': {'power': .8}}
    query = MultiDict({'disease':'First scope', 'outcome':'Second scope', 'arms':'2', 'power':'.80'})
    assert defaults._choose_disease(query, catalog, 'neuro')['value'] == 'second'
    assert defaults._choose_disease(MultiDict({'scope':'b_scope'}), catalog, 'standard')['value'] == 'second'
    assert defaults._defaults_match(query, catalog, 'neuro', {})
    assert not defaults._defaults_match(MultiDict({'arms':'1'}), catalog, 'standard', {})


def test_packaged_snapshot_requires_deployment_build(tmp_path, monkeypatch):
    path = tmp_path / defaults.FILE
    path.parent.mkdir()
    doc = {'version':defaults.VERSION, 'source_revision':'validated-tree'}
    path.write_text(json.dumps(doc))
    assert defaults._load(tmp_path, None) is None
    import hashlib
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_MESSAGE', 'Release\n\nSample-Size-SHA256: ' + digest)
    assert defaults._load(tmp_path, None) == doc
    path.write_text(json.dumps({**doc, 'different_bytes': True}))
    assert defaults._load(tmp_path, None) is None
    doc['built_for_deployment'] = True
    path.write_text(json.dumps(doc))
    assert defaults._load(tmp_path, None) == doc
    assert defaults._load(tmp_path, 'different-tree') is None
