from copy import deepcopy
from types import SimpleNamespace
import pandas as pd
from runtime_code.trend_ci_projection import correct_tree
from runtime_code.trend_ci_runtime import install_core


def fixture():
    e={'effect_size':1.63,'ci_lower':.8659971914159837,'ci_upper':3.068023806931438,
       'p_value':.13,'ci_method':'95% Wald CI from effect and two-sided p value on the log scale',
       'evidence':{'p_value':{'excerpt':'P for trend = .13'}}}
    return {'PMID':'11159145','Effect Size':e['effect_size'],'Lower CI':e['ci_lower'],'Upper CI':e['ci_upper'],
            'P Value':.13,'SE':.56,'combined_source_extraction':e,'subexposure_pooling_selected':True}


def test_runtime_blocks_borrowed_ci_without_mutating_source_and_handles_positional_df_all():
    saved=fixture();frame=pd.DataFrame([saved]);received=[]
    def original(frame,disease,exposure,outcome='Incidence',exclude_meta=True,df_all=None):
        received.append((frame,df_all));return frame
    module=SimpleNamespace(perform_meta_analysis=original)
    install_core(module);first=module.perform_meta_analysis;install_core(module)
    assert module.perform_meta_analysis is first
    module.perform_meta_analysis(frame,'Bladder cancer','Alcohol','Incidence',True,frame)
    for projected in received[0]:
        assert projected.iloc[0]['Lower CI'] is None
        assert projected.iloc[0]['Upper CI'] is None
        assert projected.iloc[0]['SE'] is None
    assert frame.iloc[0]['Lower CI']==saved['Lower CI']
    assert saved['combined_source_extraction']['ci_lower'] is not None


def test_historical_input_receipts_remain_unchanged_and_retraction_idempotent():
    row=fixture();raw=deepcopy(row['combined_source_extraction']);row['policy_original_values']=deepcopy(raw)
    assert correct_tree(row)
    assert row['policy_original_values']==raw
    after=deepcopy(row);assert correct_tree(row)==[];assert row==after


def test_all_generation_cached_selection_layers_invalidate(monkeypatch):
    import study_selection as selection
    revision=['a'];calls=[]
    monkeypatch.setattr(selection,'generation',lambda *_:revision[0])
    @selection.generation_cache(2)
    def derived(source,disease=None):calls.append((source,disease));return len(calls)
    assert derived('urinary',disease='Bladder cancer')==1
    assert derived('urinary',disease='Bladder cancer')==1
    revision[0]='b';assert derived('urinary',disease='Bladder cancer')==2


def test_real_monthly_sync_after_no_search_scientific_overlay(tmp_path):
    from test_source_updates import prepare
    from automation.sync_sources import write,sha,sync_sources,load_overlay
    root,siblings,_=prepare(tmp_path)
    manifest=root/'updates/metaPee/versions/correction/manifest.json'
    write(manifest,{'schema_version':1,'kind':'scientific_correction','source':'metaPee',
                    'run_id':'correction','search_through':None,'files':{},'review_files':[]})
    write(root/'updates/manifest.json',{'schema_version':1,'sources':{'metaPee':{
        'manifest':str(manifest.relative_to(root/'updates')),'sha256':sha(manifest)}}})
    sync_sources(root,siblings)
    assert load_overlay(root,'metaPee')['search_through']=='2026-09-28'


def test_localized_catalog_keeps_saved_summary_translation_overrides():
    from display_localization import install_plot_localization
    values={'fiber':{'zh-CN':'暴露'}}
    module=SimpleNamespace(_site_catalog=lambda *_:values)
    install_plot_localization(module)
    first=module._site_catalog('same-root')
    first['soy_isoflavones']={'zh-CN':'大豆异黄酮'}
    assert module._site_catalog('same-root')['soy_isoflavones']['zh-CN']=='大豆异黄酮'
    assert module._site_catalog('same-root')['fiber']['zh-CN']=='营养因子'
