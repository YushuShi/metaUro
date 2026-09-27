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

CAPTIONS = {'en': 'dashed line below CI = 95% prediction interval (where available)', 'zh-CN': '置信区间下方虚线 = 95% 预测区间（可估计时）', 'zh-TW': '信賴區間下方虛線 = 95% 預測區間（可估計時）', 'es': 'línea discontinua bajo el IC = intervalo de predicción del 95% (cuando está disponible)', 'fr': 'ligne en tirets sous l’IC = intervalle de prédiction à 95 % (si disponible)', 'nl': 'streepjeslijn onder BI = 95%-voorspellingsinterval (indien beschikbaar)', 'ko': '신뢰구간 아래 점선 = 95% 예측구간 (추정 가능한 경우)'}

def install(module):
    """Adapt the immutable source renderer without rewriting its snapshot."""
    if getattr(module, '_separated_summary_pi_installed', False):
        return
    original = module._draw_bounded_interval
    def draw(axis, low, high, y, x_min, x_max, **kwargs):
        if kwargs.get('linestyle') == '--':
            return draw_prediction_interval(axis, low, high, y, x_min, x_max)
        return original(axis, low, high, y, x_min, x_max, **kwargs)
    original_text = module.plot_text
    def text(key, locale='en', **kwargs):
        if key == 'prediction_interval_caption':
            return CAPTIONS.get(locale, CAPTIONS['en'])
        return original_text(key, locale, **kwargs)
    module._draw_bounded_interval = draw
    module.plot_text = text
    module._separated_summary_pi_installed = True
