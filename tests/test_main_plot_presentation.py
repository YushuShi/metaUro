"""Main plot formatting keeps native observations, uncertainty and source bytes intact."""
from copy import deepcopy
import importlib.util
import math
from pathlib import Path

import pytest


@pytest.mark.parametrize('source', ['metaPee', 'MetaVir'])
def test_live_forests_use_three_decimals_and_funnels_label_native_natural_logs(source, monkeypatch):
    from matplotlib.figure import Figure
    root=Path.cwd()
    path=root/'runtime_code'/source/'analysis_settings_backend.py'
    spec=importlib.util.spec_from_file_location('plot_presentation_'+source,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    from main_plot_presentation import install
    install(module)
    snapshots=[]
    original_save=Figure.savefig
    def save(figure,*args,**kwargs):
        snapshots.append(dict(labels=[axis.get_xlabel() for axis in figure.axes],
            ylabels=[axis.get_ylabel() for axis in figure.axes],
            texts=[text.get_text() for axis in figure.axes for text in axis.texts],
            ticks=[axis.xaxis.get_major_formatter()(.8,0) for axis in figure.axes],
            offsets=[collection.get_offsets().tolist() for axis in figure.axes for collection in axis.collections
                     if hasattr(collection,'get_offsets')]))
        return original_save(figure,*args,**kwargs)
    monkeypatch.setattr(Figure,'savefig',save)
    studies=[dict(Study='Example AB (2020) [PMID: 1]',
        **{'Displayed Effect Size':1.23456,'Displayed Lower CI':1.11111,'Displayed Upper CI':1.35678})]
    canonical=dict(pooled_es=.812345,ci_low=.701234,ci_upp=.923456)
    context=dict(settings=dict(effect_measure='RR',forest_scale='both'),baseline=dict(p0=.1),
                 disease='Example disease',exposure='Example exposure',pooled_plot_rows=[
                     dict(Study='A',log_ES=math.log(.8),log_SE=.1),
                     dict(Study='B',log_ES=math.log(1.2),log_SE=.2)])
    before=deepcopy((studies,canonical,context))
    forest=module._forest_plots(studies,canonical,context)
    assert forest['log'].startswith('data:image/png;base64,')
    assert '1.235 (1.111, 1.357)' in snapshots[0]['texts']
    assert '0.812 (0.701, 0.923)' in snapshots[0]['texts']
    assert snapshots[0]['ticks'][1]=='0.800'
    assert 'ln(RR) (95% CI)' in snapshots[0]['texts']
    diagnostics=module._diagnostic_plots(context,canonical)
    assert diagnostics['funnel'].startswith('data:image/png;base64,')
    funnel=snapshots[1]
    assert funnel['labels']==['ln(RR)']
    assert funnel['ylabels']==['Standard error of ln(RR)']
    assert funnel['offsets'][0]==[[math.log(.8),.1],[math.log(1.2),.2]]
    assert (studies,canonical,context)==before
