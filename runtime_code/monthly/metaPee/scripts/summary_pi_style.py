"""Visible prediction intervals for saved ratio-summary forest rows."""
from __future__ import annotations
import math


def valid_bounds(low, high):
    try:
        low, high = float(low), float(high)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and v > 0 for v in (low, high)) or high < low:
        return None
    return low, high


def draw_prediction_interval(axis, low, high, y, x_min, x_max, *, offset=.20, cap_half_height=.055):
    """Draw saved finite positive bounds below their CI; arrows denote clipping."""
    bounds = valid_bounds(low, high)
    if bounds is None:
        return False
    low, high = bounds
    y = y + offset
    color = '#37474F'
    left, right = max(x_min, min(low, x_max)), max(x_min, min(high, x_max))
    axis.plot([left, right], [y, y], color=color, linewidth=1.15,
              linestyle=(0, (4, 2)), alpha=.95, zorder=3,
              gid='summary-prediction-interval')
    for value in (low, high):
        if x_min <= value <= x_max:
            axis.plot([value, value], [y-cap_half_height, y+cap_half_height],
                      color=color, linewidth=.85, alpha=.95, zorder=3,
                      gid='summary-prediction-interval-cap')
    if low < x_min:
        axis.scatter(x_min, y, marker='<', s=16, color=color, linewidths=0, clip_on=False, zorder=3)
    if high > x_max:
        axis.scatter(x_max, y, marker='>', s=16, color=color, linewidths=0, clip_on=False, zorder=3)
    return low < x_min or high > x_max
