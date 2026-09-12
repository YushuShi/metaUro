"""Live plot layout and bibliographic identity regression coverage."""
import copy
import math

import pytest

import analysis_settings_backend as backend


def _context(repository='MetaVir'):
    return {'settings': {'effect_measure': 'RR', 'forest_scale': 'both'},
            'baseline': {'p0': 0.03}, 'repository': repository,
            'disease': 'Lung and bronchus cancer', 'exposure': 'Vitamin A'}


def _studies():
    return [dict(Study='Sigurdardottir LG et al. (2015) [PMID: 25107635]', Authors='Sigurdardottir LG, Markt SC',
                 Year='2015', PMID='25107635', **{'Displayed Effect Size': 1.234567,
                 'Displayed Lower CI': 0.9123456, 'Displayed Upper CI': 2.345678}),
            dict(Study='van der Kamp JJ et al. (2001) — never smokers', Authors=['van der Kamp JJ', 'Smith A'],
                 Year=2001, PMID=11440962, **{'Displayed Effect Size': 0.0001234567,
                 'Displayed Lower CI': 0.00009123456, 'Displayed Upper CI': 0.0002345678})]


@pytest.mark.parametrize('labels', ['original', 'log', 'both'])
def test_forest_widens_graph_and_preserves_compact_numeric_columns(labels, monkeypatch):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    saved = []
    original_save = Figure.savefig
    def inspect(figure, *args, **kwargs):
        FigureCanvasAgg(figure).draw()
        renderer = figure.canvas.get_renderer()
        saved.append({'axes': figure.axes.copy(),
                      'positions': [axis.get_position().width for axis in figure.axes],
                      'labels': [[text.get_text() for text in axis.texts] for axis in figure.axes],
                      'label_boxes': [[text.get_window_extent(renderer) for text in axis.texts] for axis in figure.axes],
                      'axis_boxes': [axis.get_window_extent(renderer) for axis in figure.axes]})
        return original_save(figure, *args, **kwargs)
    monkeypatch.setattr(Figure, 'savefig', inspect)
    context = _context()
    context['settings']['forest_scale'] = labels
    plots = backend._forest_plots(_studies(), {'pooled_es': 1.25, 'ci_low': 0.75, 'ci_upp': 1.9}, context)
    assert set(plots) == {'log'}
    image = saved[-1]
    assert image['positions'][1] / image['positions'][0] == pytest.approx(5.1 / 4.2)
    assert image['positions'][2] / image['positions'][1] == pytest.approx(1.8 / 5.1)
    assert image['labels'][0][1] == 'Sigurdardottir LG et al. (2015) [PMID: 25107635]'
    if labels != 'log':
        assert '1.235 (0.9123, 2.346)' in image['labels'][2]
    for column_index in range(2, len(image['axis_boxes'])):
        for box in image['label_boxes'][column_index]:
            assert box.x1 <= image['axis_boxes'][column_index].x1 + 1


def test_breath_short_labels_preserve_source_identity_and_disambiguate_multiple_estimates():
    rows = _studies()
    rows.append({**rows[0], 'Study': rows[0]['Study'] + ' — women'})
    untouched = copy.deepcopy(rows)
    output = backend.forest_annotation_rows(rows, {}, _context('metaBreath'))
    assert output[0]['study'] == 'Sigurdardottir (2015) [PMID: 25107635] [estimate 1]'
    assert output[1]['study'] == 'van der Kamp (2001) [PMID: 11440962]'
    assert output[2]['study'].endswith('[estimate 2]')
    assert output[2]['source_study'].endswith('— women')
    assert rows == untouched


def test_funnel_pooled_reference_title_and_baujat_labels_are_next_to_every_point(monkeypatch):
    from matplotlib.figure import Figure
    from matplotlib.patches import ConnectionPatch
    saved = []
    original_save = Figure.savefig
    def inspect(figure, *args, **kwargs):
        saved.append({'titles': [axis.get_title() for axis in figure.axes],
                      'texts': [text.get_text() for axis in figure.axes for text in axis.texts],
                      'lines': [(line.get_color(), line.get_linestyle(), list(line.get_xdata()))
                                for axis in figure.axes for line in axis.lines],
                      'connections': sum(isinstance(item, ConnectionPatch) for item in figure.artists),
                      'annotations': [text for axis in figure.axes for text in axis.texts if hasattr(text, 'xy')]})
        return original_save(figure, *args, **kwargs)
    monkeypatch.setattr(Figure, 'savefig', inspect)
    context = _context()
    context['pooled_plot_rows'] = [
        dict(Study='Smith A et al. (2020)', Authors='Smith A, Jones B', Year=2020, PMID='12345678', log_ES=math.log(1.2), log_SE=.1),
        dict(Study='Jones B et al. (2021)', Authors=['Jones B', 'Liu C'], Year=2021, PMID='23456789', log_ES=math.log(1.3), log_SE=.2),
        dict(Study='Liu C et al. (2022)', Authors='Liu C, Smith A', Year=2022, PMID='34567890', log_ES=math.log(.9), log_SE=.15)]
    plots = backend._diagnostic_plots(context, {'pooled_es': 1.12})
    assert set(plots) == {'funnel', 'baujat'}
    assert saved[0]['titles'] == ['Funnel Plot: Lung and bronchus cancer vs Vitamin A']
    red = [line for line in saved[0]['lines'] if line[0] == '#dc2626']
    assert len(red) == 1 and red[0][1] == '--'
    assert red[0][2] == pytest.approx([math.log(1.12)] * 2)
    assert set(saved[1]['texts']) == {'Smith (2020) [PMID: 12345678]', 'Jones (2021) [PMID: 23456789]', 'Liu (2022) [PMID: 34567890]'}
    assert saved[1]['connections'] == 0
    assert len(saved[1]['annotations']) == 3
    assert all(text.arrow_patch is None and text.get_position() == (4, 4) for text in saved[1]['annotations'])
