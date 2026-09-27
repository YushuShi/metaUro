"""Protect preserved source engines without modifying their evidence snapshots."""
from functools import wraps
from runtime_code.trend_ci_projection import sanitized_frame
from runtime_code import p_value_guard


def install_core(module):
    original=module.perform_meta_analysis
    if getattr(original,'_metauro_trend_ci_guard',False):
        return
    @wraps(original)
    def perform(frame,*args,**kwargs):
        frame=sanitized_frame(frame)
        if len(args) >= 5:
            args = (*args[:4], sanitized_frame(args[4]), *args[5:])
        if kwargs.get('df_all') is not None:
            kwargs['df_all']=sanitized_frame(kwargs['df_all'])
        return original(frame,*args,**kwargs)
    perform._metauro_trend_ci_guard=True
    module.perform_meta_analysis=perform


def install_completion():
    """Block source-engine completion even when excerpts use older schemas."""
    try:
        from scripts import relaxed_poolability
    except ImportError:
        return
    original=relaxed_poolability.complete
    if getattr(original,'_metauro_trend_ci_guard',False):return
    @wraps(original)
    def complete(extraction,*args,**kwargs):
        if isinstance(extraction,dict):
            p_value_guard.retract_trend_ci(extraction)
            if p_value_guard.trend_evidence(extraction):
                extraction['p_value_targets_selected_estimate']=False
        return original(extraction,*args,**kwargs)
    complete._metauro_trend_ci_guard=True
    relaxed_poolability.complete=complete
