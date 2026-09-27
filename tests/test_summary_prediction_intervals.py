import importlib
import math
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt

from runtime_code.summary_pi_style import draw_prediction_interval, valid_bounds


def test_prediction_interval_is_below_ci_with_saved_endpoints_and_caps():
    fig, ax = plt.subplots();ax.set_ylim(3,0)
    assert not draw_prediction_interval(ax,.7,1.5,1,.1,2)
    line = next(line for line in ax.lines if line.get_gid()=='summary-prediction-interval')
    assert list(line.get_xdata())==[.7,1.5]
    assert list(line.get_ydata())==[1.2,1.2]
    assert line.is_dashed()
    assert len([line for line in ax.lines if line.get_gid()=='summary-prediction-interval-cap'])==2
    plt.close(fig)


def test_missing_nonpositive_nonfinite_and_reversed_intervals_are_absent():
    fig, ax = plt.subplots()
    for low,high in [(None,2),(.4,None),(0,2),(-1,2),(.4,float('inf')),(float('nan'),2),(2,1)]:
        assert valid_bounds(low,high) is None
        assert not draw_prediction_interval(ax,low,high,1,.1,2)
    assert not ax.lines and not ax.collections
    plt.close(fig)


def test_clipped_interval_has_boundary_arrows_and_does_not_expand_ci_axis():
    fig, ax=plt.subplots();ax.set_xlim(.1,2)
    assert draw_prediction_interval(ax,.01,10,1,.1,2)
    assert ax.get_xlim()==(.1,2)
    assert len(ax.collections)==2
    assert list(ax.lines[0].get_xdata())==[.1,2]
    plt.close(fig)


def test_runtime_installer_changes_only_pi_and_is_idempotent():
    from types import SimpleNamespace
    from runtime_code.summary_pi_style import install
    calls=[]
    module=SimpleNamespace(_draw_bounded_interval=lambda *a,**k:calls.append((a,k)),plot_text=lambda key,locale='en',**k:key)
    install(module);wrapped=module._draw_bounded_interval;install(module);assert module._draw_bounded_interval is wrapped
    fig,ax=plt.subplots()
    module._draw_bounded_interval(ax,.7,1.3,1,.1,2,color='red',linewidth=.65,linestyle='--')
    assert list(ax.lines[0].get_ydata())==[1.2,1.2] and not calls
    module._draw_bounded_interval(ax,.8,1.2,1,.1,2,color='red',linewidth=1.5)
    assert len(calls)==1
    assert '(where available)' in module.plot_text('prediction_interval_caption','en')
    assert '置信区间下方' in module.plot_text('prediction_interval_caption','zh-CN')
    plt.close(fig)


def test_summary_build_installs_pi_before_builder_and_binds_signature(tmp_path, monkeypatch):
    import json
    import sys
    from types import ModuleType
    import summary_outputs as outputs

    (tmp_path / 'source_worker.py').write_text('')
    (tmp_path / 'summary_outputs.py').write_text('')
    source = tmp_path / 'sources/metaPee'
    source.mkdir(parents=True)
    folder = tmp_path / 'summary/no'
    folder.mkdir(parents=True)
    monkeypatch.setattr(outputs, 'cache_dir', lambda *args: tmp_path / 'cache')
    monkeypatch.setattr(outputs, 'input_fingerprint', lambda *args: 'pi-renderer-signature')
    renderer = ModuleType('summary_pi_test_renderer')
    renderer._draw_bounded_interval = lambda *args, **kwargs: None
    renderer.plot_text = lambda key, locale='en', **kwargs: key
    monkeypatch.setitem(sys.modules, renderer.__name__, renderer)

    def builder(cache, stage, languages):
        assert renderer._separated_summary_pi_installed
        assert 'below CI' in renderer.plot_text('prediction_interval_caption')
        filename = 'forest_protective_test.pdf'
        (stage / filename).write_bytes(b'%PDF-fixture')
        manifest = {'scopes': {'test': {'plots': {'forest-protective': {
            'filename': filename, 'available': True}}}}}
        manifest = outputs.finalize_build(source, stage, manifest)
        (stage / 'summary_manifest.json').write_text(json.dumps(manifest))
        return manifest

    builder.__module__ = renderer.__name__
    outputs.run_summary_build(source, tmp_path / 'cache', folder, None, builder)
    assert outputs._current(folder, 'pi-renderer-signature', 'metaPee')
    assert not outputs._current(folder, 'stale-renderer-signature', 'metaPee')
