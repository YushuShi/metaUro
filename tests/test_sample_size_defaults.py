import json

import pytest
from flask import Flask, jsonify, request

import sample_size_defaults as defaults


@pytest.mark.parametrize('revision', ['release-a', 'release-b'])
def test_only_matching_release_and_settings_skip_live_analysis(tmp_path, monkeypatch, revision):
    catalog = {
        'diseases': [{'value': 'test', 'label': 'Test', 'baseline_percent': 2}],
        'defaults': {'power': .8, 'alpha': .05, 'sidedness': 'two',
                     'control_exposure_percent': 50, 'allocation_ratio': 1},
        'analysis_settings': {'age': 60, 'keep_latest_cohort': True, 'sex': 'female'},
    }
    saved = {'disease': 'test', 'panels': [{'points': [{'total_n': 2000}]}]}
    bundle = dict(version=defaults.VERSION, source_revision='release-a',
                  profile='standard', catalog=catalog, summaries={'test': saved})
    path = tmp_path / defaults.FILE
    path.parent.mkdir()
    path.write_text(json.dumps(bundle))
    monkeypatch.setattr(defaults, '_revision', lambda root: revision)

    app = Flask(__name__)
    defaults.install(app, root=tmp_path)

    @app.get(defaults.SUMMARY)
    @app.get(defaults.CATALOG)
    def live():
        return jsonify(live=True, power=request.args.get('power'))

    client = app.test_client()
    response = client.get(defaults.SUMMARY + '?disease=test&power=.800&age=60&sex=female'
                          '&keep_latest_cohort=yes&max_sample_size=10000')
    if revision == 'release-a':
        assert response.headers['X-Sample-Size-Source'] == 'precomputed'
        assert response.json == saved
        assert client.get(defaults.CATALOG).json == catalog
    else:
        assert response.json['live']

    for custom in ('power=.9', 'baseline_percent=2', 'age=70', 'sex=male',
                   'include_preprints=yes', 'keep_latest_cohort=no',
                   'include_subexposures=no', 'power=.8&power=.9', 'age=60.5'):
        response = client.get(defaults.SUMMARY + '?disease=test&' + custom)
        assert response.json['live']
        assert 'X-Sample-Size-Source' not in response.headers
    assert client.get(defaults.SUMMARY + '?disease=unknown').json['live']


def test_urinary_default_sex_matches_each_saved_disease():
    from werkzeug.datastructures import MultiDict
    catalog = {'defaults': {'power': .8}}
    query = MultiDict({'sex': 'both'})
    assert not defaults._defaults_match(query, catalog, 'uro', None)
    assert not defaults._defaults_match(query, catalog, 'uro', {'analysis_settings': {'sex': 'male'}})
    assert defaults._defaults_match(query, catalog, 'uro', {'analysis_settings': {'sex': 'both'}})
