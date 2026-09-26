"""Presentation-only Chinese terminology; canonical research data are untouched."""
import re
from functools import wraps


def chinese_display(value, locale):
    if locale not in {'zh-CN', 'zh-TW'} or not isinstance(value, str):
        return value
    term = '营养因子' if locale == 'zh-CN' else '營養因子'
    # Consume existing nutrition prefixes and factor suffixes as one phrase.
    return re.sub(r'(?:营养|營養)?暴露(?:因素|因子)?', term, value)


def normalize_catalog(value):
    if isinstance(value, dict):
        return {key: chinese_display(item, key) if key in {'zh-CN', 'zh-TW'}
                else normalize_catalog(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_catalog(item) for item in value]
    return value


def install_plot_localization(module):
    """Overlay source renderers without editing immutable source snapshots."""
    if getattr(module, '_nutrition_display_installed', False):
        return
    for key in ('PLOT_STRINGS', 'CATEGORY_LABELS', 'GUT_EXPOSURE_LABELS', 'STYLE_STRINGS'):
        if hasattr(module, key):
            setattr(module, key, normalize_catalog(getattr(module, key)))
    if hasattr(module, '_site_catalog'):
        original = module._site_catalog
        @wraps(original)
        def display_catalog(*args, **kwargs):
            return normalize_catalog(original(*args, **kwargs))
        module._site_catalog = display_catalog
    module._nutrition_display_installed = True
