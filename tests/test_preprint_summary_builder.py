"""Optional publication retains its callable and fails closed on native errors."""
from contextlib import contextmanager
from pathlib import Path
import importlib.util,json,sys,types
import pandas as pd
import pytest


@pytest.fixture
def harness(tmp_path,monkeypatch):
    root=tmp_path/'metaUro';source=root/'sources/MetaVir';source.mkdir(parents=True)
    cache=source/'Subexposure_results/yes';path=cache/'red_meat/prostate_cancer_incidence_true_all.json';path.parent.mkdir(parents=True)
    row={'PMID':'123','Study':'Saved','Effect Size':1.2,'Lower CI':1.1,'Upper CI':1.3,'Sample Size':100,'Cases':4}
    path.write_text(json.dumps({'studies':[row]}))
    active=root/'updates/preprint_recovery/active_manifest.json';active.parent.mkdir(parents=True);active.write_text(json.dumps({'schema_version':1,'status':'complete','contexts':[]}))
    module_path=Path(__file__).resolve().parents[1]/'scripts/build_cohort_summaries.py';spec=importlib.util.spec_from_file_location('fixture_preprint_summary_builder',module_path);builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder);monkeypatch.setattr(builder,'ROOT',root)
    @contextmanager
    def selection(enabled):yield {'rows':[row],'audit':{}}
    monkeypatch.setitem(sys.modules,'meta_analysis',types.ModuleType('meta_analysis'))
    monkeypatch.setitem(sys.modules,'analysis_settings_backend',types.SimpleNamespace(latest_cohort_pooling=selection))
    import automation.sync_sources as sync,summary_outputs,preprint_ui
    monkeypatch.setattr(sync,'assembled_source',lambda *a:source);monkeypatch.setattr(sync,'load_overlay',lambda *a:{})
    monkeypatch.setattr(sync,'_effective_file',lambda *a:source/a[2])
    folder=root/'summary/yes/preprints_included';folder.mkdir(parents=True)
    monkeypatch.setattr(summary_outputs,'cache_dir',lambda *a:cache);monkeypatch.setattr(summary_outputs,'summary_dir',lambda *a:folder)
    monkeypatch.setattr(preprint_ui,'applicable_preprint_contexts',lambda *a:[]);monkeypatch.setattr(preprint_ui,'write_preprint_summary_seal',lambda *a:None)
    for relative in ('app.py','source_worker.py','summary_outputs.py','scripts/build_preprint_summaries.py','scripts/build_cohort_summaries.py','runtime_code/manifest.json','runtime_code/MetaVir/analysis_settings_backend.py','runtime_code/metaPee/analysis_settings_backend.py','prostate_scopes.py','cohort_identity.py'):
        item=root/relative;item.parent.mkdir(parents=True,exist_ok=True);item.write_text('fixture')
    renderer=types.ModuleType('metauro_cohort_summary_builder');state={'calls':0,'finalized':0,'fail':False}
    def native_summary(*a):
        state['calls']+=1
        if state['fail']:raise RuntimeError('Native fixture error')
        return {'Exposure':'Red meat'}
    renderer.summarize_cache=native_summary;renderer.analysis_cases=lambda *a:pd.Series([4]);renderer.exposure_label=lambda root,value,locale:value
    renderer.forest_plot=renderer.heterogeneity_plot=lambda *a:None;renderer.workbook=lambda *a:None
    def finalize(*a):state['finalized']+=1;return a[-1]
    renderer.finalize_build=finalize
    def build(cache,folder,languages):
        result=None
        try:result=renderer.summarize_cache(next(cache.glob('*/*.json')),'Prostate cancer','Red meat',False)
        except Exception:pass  # Native legacy builders log/skip; finalization must catch it.
        return renderer.finalize_build(source,folder,{'result':result})
    renderer.build=build
    class Loader:
        def create_module(self,spec):return renderer
        def exec_module(self,module):pass
    original_spec=builder.importlib.util.spec_from_file_location
    monkeypatch.setattr(builder.importlib.util,'spec_from_file_location',lambda name,path:importlib.util.spec_from_loader(name,Loader()) if name=='metauro_cohort_summary_builder' else original_spec(name,path))
    monkeypatch.setitem(sys.modules,'metauro_cohort_summary_builder',renderer)
    from matplotlib.figure import Figure
    monkeypatch.setattr(Figure,'savefig',Figure.savefig);monkeypatch.setattr(sys,'path',sys.path.copy())
    for name in ('METAURO_KEEP_LATEST_COHORT','METAURO_INCLUDE_PREPRINTS','METAURO_SUMMARY_SOURCE_ROOT','MPLBACKEND'):monkeypatch.setenv(name,'fixture')
    return builder,state


def test_optional_staging_does_not_replace_native_summary_callable(harness):
    builder,state=harness;result=builder.build('MetaVir','yes',('en',),keep_latest_cohort=False,include_preprints=True)
    assert state['calls']==1 and state['finalized']==1
    assert result['result']['number studies']==1 and result['result']['total N']==100


def test_optional_native_failure_prevents_publication(harness):
    builder,state=harness;state['fail']=True
    with pytest.raises(RuntimeError,match='not published'):
        builder.build('MetaVir','yes',('en',),keep_latest_cohort=False,include_preprints=True)
    assert state['calls']==1 and state['finalized']==0
