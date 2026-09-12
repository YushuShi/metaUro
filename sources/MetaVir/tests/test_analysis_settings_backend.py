"""Finite-risk conversion and request isolation; fixture values are synthetic."""
import copy
import json
import math
from pathlib import Path
import tempfile
import types

import pandas as pd
import pytest
from flask import Flask, jsonify

import analysis_settings_backend as backend


@pytest.fixture
def catalog():
    return {'defaults': {'sex': 'female', 'race': 'all_races_ethnicities'},
            'source': {'id': 'official-fixture', 'data_period': '2021–2023'},
            'diseases': [{'id': 'breast', 'label': 'Breast cancer', 'aliases': ['Breast Cancer']},
                         {'id': 'dcis', 'label': 'DCIS', 'aliases': ['in_situ'], 'unsupported_reason': 'No exact subtype risk'}],
            'records': [{'disease': 'breast', 'sex': 'female', 'race': 'all_races_ethnicities',
                         'age': 60, 'years': 10, 'risk_percent': '3.551400', 'source': 'official-fixture', 'source_key': 'raw-key'}]}


def test_exact_lookup_defaults_and_no_subtype_or_sex_fallback(catalog):
    settings = backend.normalize_settings({}, catalog)
    baseline = backend.resolve_baseline(catalog, 'Breast Cancer', None, settings)
    assert baseline['p0'] == pytest.approx(0.035514)
    assert not baseline['identity_approximation']
    assert not backend.resolve_baseline(catalog, 'Breast Cancer', 'in_situ', settings)['available']
    settings['sex'] = 'both'
    assert not backend.resolve_baseline(catalog, 'Breast Cancer', None, settings)['available']


def test_femina_bare_subtype_slug_is_scoped_to_its_parent(catalog):
    catalog['repository'] = 'metaFemina'
    catalog['diseases'] = [
        {'id': 'uterine_cancer', 'label': 'Uterine Cancer', 'aliases': []},
        {'id': 'ovarian_cancer', 'label': 'Ovarian Cancer', 'aliases': []},
        {'id': 'uterus_clear_cell_carcinoma', 'label': 'Uterus clear cell', 'aliases': []},
        {'id': 'ovary_clear_cell_carcinoma', 'label': 'Ovary clear cell', 'aliases': []},
    ]
    settings = backend.normalize_settings({}, catalog)
    uterine = backend.resolve_baseline(catalog, 'Uterine Cancer', 'clear_cell_carcinoma', settings)
    ovarian = backend.resolve_baseline(catalog, 'Ovarian Cancer', 'clear_cell_carcinoma', settings)
    assert uterine['disease'] == 'uterus_clear_cell_carcinoma'
    assert ovarian['disease'] == 'ovary_clear_cell_carcinoma'
    assert not uterine['available'] and not ovarian['available']
    wrong = backend.resolve_baseline(catalog, 'Uterine Cancer', 'ovary_clear_cell_carcinoma', settings)
    assert not wrong['available'] and 'different parent' in wrong['reason']
    settings['sex'], settings['age'] = 'female', 61
    assert not backend.resolve_baseline(catalog, 'Breast Cancer', None, settings)['available']


def test_manual_settings_and_age_cutoff(catalog):
    settings = backend.normalize_settings({'baseline_source': 'manual', 'manual_risk_percent': 7.5,
                                          'age': 75, 'years': 15}, catalog)
    assert backend.resolve_baseline(catalog, 'Unsupported', None, settings)['p0'] == 0.075
    for invalid in [{'age': 73, 'years': 18}, {'age': 60.5}, {'exposure_rate': 0},
                    {'baseline_source': 'manual', 'manual_risk_percent': 100},
                    {'baseline_source': 'manual', 'manual_risk_percent': 'NaN'}]:
        with pytest.raises(ValueError):
            backend.normalize_settings(invalid, catalog)


def test_aggressive_prostate_requires_exact_manual_risk():
    catalog = json.loads((Path(__file__).resolve().parents[1] / 'static' / 'seer-baseline.json').read_text())
    settings = backend.normalize_settings({}, catalog)
    general = backend.resolve_baseline(catalog, 'Prostate cancer', None, settings)
    assert general['available'] and general['p0'] > 0
    for disease, subtype in [('Prostate cancer', 'aggressive_prostate_cancer'),
                             ('Aggressive prostate cancer', None),
                             ('Prostate cancer', 'aggressive_prostate')]:
        aggressive = backend.resolve_baseline(catalog, disease, subtype, settings)
        assert aggressive['disease'] == 'aggressive_prostate_cancer'
        assert aggressive['manual_required'] and not aggressive['available']
        assert aggressive['p0'] is None
        assert 'Enter the aggressive prostate cancer incidence risk' in aggressive['reason']
        assert not backend.sample_size_for_rr(1.5, aggressive['p0'], 'prospective')['available']
    assert not any(row['disease'] == 'aggressive_prostate_cancer' for row in catalog['records'])
    manual = backend.normalize_settings({'baseline_source': 'manual', 'manual_risk_percent': 1.5}, catalog)
    baseline = backend.resolve_baseline(catalog, 'Prostate cancer', 'aggressive_prostate_cancer', manual)
    assert baseline['p0'] == pytest.approx(0.015)
    assert baseline['source'] == 'manual' and not baseline['manual_required']


def test_canonical_rr_roundtrip_and_missing_baseline_identity():
    for original in (0.5, 1.5, 3):
        for kind in ('OR', 'HR', 'RR'):
            rr = backend.ratio_to_rr(original, kind, 0.075)
            assert backend.rr_to_display(rr, kind, 0.075) == pytest.approx(original)
            assert backend.ratio_to_rr(original, kind, None) == original
            assert backend.rr_to_display(original, kind, None) == original
    assert backend.ratio_to_rr(2, 'IRR', 0.1) == pytest.approx(1.9)
    assert backend.rr_to_display(11, 'OR', 0.1) is None
    assert backend.rr_to_display(10, 'HR', 0.1) is None


def test_sample_size_uses_same_baseline_and_retrospective_exposure_odds():
    first = backend.sample_size_for_rr(1.5, 0.05)
    second = backend.sample_size_for_rr(1.5, 0.1)
    assert first['available'] and second['available']
    assert first['required_per_group'] != second['required_per_group']
    retro = backend.sample_size_for_rr(1.5, 0.05, 'retrospective', 0.5)
    expected_or = 1.5 * 0.95 / 0.925
    assert retro['odds_ratio'] == pytest.approx(expected_or)
    assert retro['p1_for_power'] == pytest.approx(expected_or / (1 + expected_or))
    assert retro['total'] == 2 * retro['required_per_group']
    assert not backend.sample_size_for_rr(1.5, None)['available']
    assert not backend.sample_size_for_rr(1, 0.05)['available']


def test_saved_effects_are_preserved_and_estimated_cases_refreshed():
    source = {'Effect Type': 'OR', 'Effect Size': 2, 'Lower CI': 1.2, 'Upper CI': 3,
              'Sample Size': '1,000', 'Cases': '-', 'Estimated Cases': 777,
              'Original Effect Size': 1.9}
    untouched = copy.deepcopy(source)
    converted = backend._study_conversion(source, 0.1, 'HR')
    assert source == untouched
    assert converted['Effect Size'] == 2 and converted['Original Effect Size'] == 1.9
    assert converted['Canonical RR'] == pytest.approx(2 / 1.1)
    assert converted['Estimated Cases'] == 100
    assert backend._study_conversion(source, None, 'RR')['Estimated Cases'] is None


def test_cached_projection_recalculates_and_request_context_does_not_leak(catalog, tmp_path, monkeypatch):
    (tmp_path / 'static').mkdir()
    (tmp_path / 'static' / 'seer-baseline.json').write_text(json.dumps(catalog))
    seen = []
    namespace = {'baseline_risk_for_disease': lambda disease: 0.99,
                 'convert_ratio_to_rr': lambda value, kind, p0: value / (1 - p0 + p0 * value),
                 'normalize_effect_type': lambda value: value}

    def analyze(frame, disease, exposure, **kwargs):
        p0 = namespace['baseline_risk_for_disease'](disease)
        seen.append(p0)
        row = frame.iloc[0]
        converted = namespace['convert_ratio_to_rr'](row['Effect Size'], row['Effect Type'], p0)
        backend.capture_pooling_rows(frame)
        return {'studies': kwargs['df_all'].to_dict('records'),
                'headline': {'canonical_rr': {'pooled_es': converted, 'ci_low': 1.1, 'ci_upp': 2.2}}}

    namespace['perform_meta_analysis'] = analyze
    backend.configure_meta_analysis(namespace)
    module = types.SimpleNamespace(**namespace)
    application = Flask(__name__)
    original_cache = {'headline': {'pooled_es': 99}, 'studies': [
        {'Study': 'Saved study', 'PMID': '123', 'Effect Type': 'OR', 'Effect Size': 2,
         'Lower CI': 1.2, 'Upper CI': 3, 'Sample Size': 1000, 'Cases': '-', 'Quality Score': 'Good'}]}

    @application.post('/analyze')
    def cached():
        return jsonify(copy.deepcopy(original_cache))

    monkeypatch.setattr(backend, '_forest_plots', lambda *args: {'log': 'data:image/png;base64,fixture'})
    backend.install_app(application, module, str(tmp_path / 'app.py'))
    client = application.test_client()
    first = client.post('/analyze', json={'disease': 'Breast Cancer', 'exposure': 'Coffee'}).get_json()
    second = client.post('/analyze', json={'disease': 'Breast Cancer', 'exposure': 'Coffee',
        'analysis_settings': {'baseline_source': 'manual', 'manual_risk_percent': 20}}).get_json()
    assert first['canonical_rr']['pooled_es'] != second['canonical_rr']['pooled_es']
    assert seen == pytest.approx([0.035514, 0.2])
    assert original_cache['headline']['pooled_es'] == 99
    assert namespace['baseline_risk_for_disease']('Breast Cancer') == 0.99
    assert not backend.settings_active()
    assert first['studies'][0]['Effect Size'] == 2


@pytest.mark.parametrize('label_scale', ['original', 'log', 'both'])
def test_forest_keeps_log_axis_with_selected_numerical_ci_columns(catalog, label_scale, monkeypatch):
    settings = backend.normalize_settings({'effect_measure': 'OR', 'forest_scale': label_scale}, catalog)
    from matplotlib.figure import Figure
    figures = []
    class CapturedFigure(Figure):
        def clear(self, *args, **kwargs):
            if getattr(self, 'axes', []):
                figures.append({'scales': [axis.get_xscale() for axis in self.axes],
                                'texts': [text.get_text() for axis in self.axes for text in axis.texts]})
            return super().clear(*args, **kwargs)
    monkeypatch.setattr('matplotlib.figure.Figure', CapturedFigure)
    context = {'settings': settings, 'baseline': {'p0': 0.05}, 'disease': 'Breast cancer'}
    study = backend._study_conversion({'Study': 'Study', 'Effect Type': 'RR', 'Effect Size': 1.5,
                                      'Lower CI': 1.1, 'Upper CI': 2}, 0.05, 'OR')
    plots = backend._forest_plots([study], {'pooled_es': 1.5, 'ci_low': 1.1, 'ci_upp': 2}, context)
    assert set(plots) == {'log'}
    assert all(value.startswith('data:image/png;base64,iVBOR') for value in plots.values())
    assert figures[-1]['scales'][1] == 'log'
    labels = figures[-1]['texts']
    assert ('OR (95% CI)' in labels) == (label_scale in {'original', 'both'})
    assert ('ln(OR) (95% CI)' in labels) == (label_scale in {'log', 'both'})
    rows = backend.forest_annotation_rows([study], {'pooled_es': 1.5, 'ci_low': 1.1, 'ci_upp': 2}, context)
    for row in rows:
        assert row['log_estimate'] == pytest.approx(math.log(row['estimate']))
        assert row['log_ci_low'] == pytest.approx(math.log(row['ci_low']))
        assert row['log_ci_upp'] == pytest.approx(math.log(row['ci_upp']))


def test_empty_pool_keeps_null_headline_and_numerical_errors_visible(catalog, monkeypatch):
    monkeypatch.setattr(backend, '_forest_plots', lambda *args: {})
    monkeypatch.setattr(backend, '_diagnostic_plots', lambda *args: {})
    settings = backend.normalize_settings({}, catalog)
    context = {'settings': settings, 'baseline': backend.resolve_baseline(catalog, 'Breast Cancer', None, settings),
               'disease': 'Breast Cancer', 'quality_filter': 'Moderate+'}
    result = backend._project({'studies': [], 'headline': None}, context)
    assert result['headline'] is None
    assert not result['sample_size']['available']
    namespace = {'perform_meta_analysis': lambda *a, **k: {'error': 'Numerical pooling failed'},
                 'baseline_risk_for_disease': lambda disease: 0.1}
    backend.configure_meta_analysis(namespace)
    token = backend._ACTIVE.set(context)
    try:
        result = namespace['perform_meta_analysis'](pd.DataFrame(), 'Breast Cancer', 'Coffee')
        assert result['error'] == 'Numerical pooling failed'
        assert result['headline'] is None
    finally:
        backend._ACTIVE.reset(token)


def test_femina_reanalysis_preserves_already_embedded_plot_urls(monkeypatch):
    if Path(__file__).resolve().parents[1].name != 'metaFemina':
        pytest.skip('Regression for the Femina controller image-embedding loop.')
    import app as app_module
    # A minimal embedded PNG prefix is sufficient: this test checks that the
    # controller preserves an already-rendered image, never treats it as a path.
    image = 'data:image/png;base64,iVBORw0KGgo='
    projected = {'success': True, 'studies': [], 'headline': None,
                 '_analysis_settings_applied': True, 'forest_plots': {'log': image},
                 'plot_url': image, 'funnel_plot_url': image, 'baujat_plot_url': image}
    monkeypatch.setattr(app_module.meta_analysis, 'perform_meta_analysis', lambda *a, **k: copy.deepcopy(projected))
    response = app_module.app.test_client().post('/reanalyze', json={
        'disease': 'Breast Cancer', 'exposure': 'Coffee', 'outcome': 'Incidence',
        'studies': [{'Study': 'Study', 'PMID': '123', 'Effect Type': 'OR', 'Effect Size': 2,
                     'Lower CI': 1.2, 'Upper CI': 3, 'Sample Size': 1000, 'Cases': 50,
                     'Quality Score': 'Good'}]})
    result = response.get_json()
    assert response.status_code == 200, result
    assert result['plot_url'] == result['forest_plots']['log'] == image
    assert result['funnel_plot_url'] == result['baujat_plot_url'] == image


@pytest.mark.parametrize('odds', [0.5, 1.5, 2, 3])
@pytest.mark.parametrize('ratio', [0.5, 1, 2, 4])
@pytest.mark.parametrize('q0', [0.1, 0.5, 0.8])
def test_case_control_or_matches_independent_statsmodels(odds, ratio, q0):
    from statsmodels.stats.proportion import samplesize_proportions_2indep_onetail
    result = backend.sample_size_for_or(odds, q0, ratio)
    q1 = odds * q0 / (1 - q0 + odds * q0)
    expected = samplesize_proportions_2indep_onetail(q1-q0, q0, 0.8, ratio=ratio, alpha=0.05)
    assert result['available']
    assert result['required_cases'] == math.ceil(expected)
    assert result['required_controls'] == math.ceil(math.ceil(expected) * ratio)
    assert result['total'] == result['required_cases'] + result['required_controls']
    assert result['measure'] == 'OR'


def test_case_control_without_disease_incidence_and_invalid_allocation(catalog):
    result = backend.sample_size_for_rr(2, None, 'retrospective', 0.5, 2)
    assert result['available'] and result['identity_approximation']
    assert result['odds_ratio'] == 2
    assert result['required_controls'] == 2 * result['required_cases']
    assert not backend.sample_size_for_rr(2, None)['available']
    for ratio in [0, -1, 'NaN', True]:
        with pytest.raises(ValueError):
            backend.normalize_settings({'controls_per_case': ratio}, catalog)
    for odds, q0, ratio in [(1, .5, 1), (0, .5, 1), (2, 0, 1), (2, 1, 1), (2, .5, 0)]:
        assert not backend.sample_size_for_or(odds, q0, ratio)['available']


def test_fair_quality_is_visible_but_changes_pool_only_after_explicit_opt_in(monkeypatch):
    import meta_analysis
    from scripts.fair_quality_selection import annotate_rows
    good = {'PMID': '900001', 'Study': 'Synthetic Good', 'Quality Score': 'Good',
            'Effect Type': 'RR', 'Effect Size': 0.8, 'Lower CI': 0.6, 'Upper CI': 1.0,
            'Sample Size': 1000, 'Cases': 100, 'SE': 0.13,
            'display_only': False, 'pool_in_parent_meta_analysis': True}
    fair = dict(good, PMID='900002', Study='Synthetic Fair', **{
        'Quality Score': 'Fair', 'Effect Size': 2.0, 'Lower CI': 1.5, 'Upper CI': 2.7,
        'display_only': True, 'policy_poolable': False, 'manual_poolable': False,
        'Primary Estimate': False, 'pool_in_parent_meta_analysis': False,
        'display_only_reason': 'Eligible study; JBI Fair is below the default pooling threshold.'})
    held = dict(fair, PMID='900003', Study='Synthetic independent hold', manual_review_pending=True)
    source = [good, fair, held]
    before = copy.deepcopy(source)
    monkeypatch.setattr(backend, '_project', lambda result, context: result)
    def calculate(mode, opt_in):
        context = {'settings': backend.normalize_settings({}, {}), 'baseline': {'p0': 0.01},
                   'quality_filter': mode, 'allow_fair_quality': opt_in}
        token = backend._ACTIVE.set(context)
        try:
            result = meta_analysis.perform_meta_analysis(pd.DataFrame(source), 'Test disease', 'Vitamin A',
                                                         generate_plots=False)
            assert result.get('success'), result
            return result, len(context['pooled_study_keys'])
        finally:
            backend._ACTIVE.reset(token)
    default, default_count = calculate('Moderate+', False)
    unconfirmed, unconfirmed_count = calculate('All', False)
    opted, opted_count = calculate('All', True)
    reset, reset_count = calculate('Moderate+', False)
    assert default_count == unconfirmed_count == reset_count == 1
    assert opted_count == 2
    assert default['headline']['pooled_es'] == unconfirmed['headline']['pooled_es'] == reset['headline']['pooled_es']
    assert opted['headline']['pooled_es'] != default['headline']['pooled_es']
    assert len(default['studies']) == 3
    visible = annotate_rows(default['studies'], 'Vitamin A')
    assert visible[1]['fair_quality_opt_in_available']
    assert not visible[2]['fair_quality_opt_in_available']
    assert source == before
