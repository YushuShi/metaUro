"""Keep unsupported trend-derived uncertainty out of effective analysis frames."""
from copy import deepcopy
from scripts import p_value_guard
from scripts.trend_ci_projection import retract_projection

AUDIT_FIELDS = ('p_value_ci_ineligible_reason', 'p_value_ci_guard_version', 'p_value_ci_retraction', 'p_value_context')


def guard_row(original):
    row = deepcopy(original)
    changed = retract_projection(row)
    if changed or isinstance(row.get('p_value_ci_retraction'), dict):
        row.update(display_only=True, ui_display_only=True, policy_poolable=False,
                   display_only_reason=p_value_guard.REASON,
                   ui_display_reason=p_value_guard.REASON)
        return row
    return deepcopy(original)


def guard_frame(frame):
    if frame is None or frame.empty:
        return frame
    import pandas as pd
    return pd.DataFrame([guard_row(row) for row in frame.to_dict(orient='records')], index=frame.index)
