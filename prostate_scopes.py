"""Derived general-prostate evidence view; saved endpoint decisions are preserved."""
from __future__ import annotations
import copy
from functools import wraps

GENERAL = 'Prostate cancer'
LABEL = 'Prostate cancer\\aggressive'
SCOPE_ID = 'prostate_cancer_excluding_aggressive'
VERSION = 'prostate-general-scope-2026-10-18'
NOTE = ('Overall prostate cancer evidence, excluding estimates solely about aggressive prostate cancer. '
        'General estimates from papers that also report aggressive endpoints remain included. '
        'Uncertain endpoints retain their existing review and pooling restrictions.')
TRANSLATIONS = {'zh-CN': '前列腺癌（排除仅侵袭性癌证据）',
                'zh-TW': '攝護腺癌（排除僅侵襲性癌證據）',
                'nl': 'prostaatkanker (zonder uitsluitend agressieve eindpunten)',
                'ko': '전립선암 (공격성 암에만 해당하는 근거 제외)'}


def is_scope(value):
    return str(value or '').strip().casefold() in {LABEL.casefold(), SCOPE_ID}


def aggressive_only(row):
    """Use reviewed endpoint metadata, never title keywords or a paper-wide guess."""
    review = row.get('disease_scope_review') or {}
    return (str(row.get('Disease Endpoint') or '').casefold() == 'aggressive'
            or str(review.get('endpoint') or '').casefold() == 'aggressive'
            or review.get('inherited_from') == 'Aggressive prostate cancer')


def project_payload(parent):
    child = copy.deepcopy(parent)
    rows = [r for r in child.get('studies', []) if not aggressive_only(r)]
    child.update(studies=rows, cancer=LABEL, disease=LABEL,
                 evidence_scope_projection={'version': VERSION, 'parent': GENERAL,
                     'scope_id': SCOPE_ID, 'definition': NOTE,
                     'excluded_aggressive_rows': len(parent.get('studies', []))-len(rows)})
    if isinstance(child.get('screening_stats'), dict):
        child['screening_stats']['extracted'] = len(rows)
    if isinstance(child.get('subexposure_projection'), dict):
        child['subexposure_projection']['table_rows'] = len(rows)
    # Recalculate all derivatives from the retained estimates before publishing.
    for field in ('headline','summary_html','plot_url','funnel_plot_url','baujat_plot_url',
                  'forest_plots','forest_plot','canonical_rr','analysis_settings'):
        child.pop(field, None)
    return child


def extend_catalog(catalog):
    catalog = copy.deepcopy(catalog)
    if any(d['id'] == SCOPE_ID for d in catalog['diseases']):
        return catalog
    parent = next(d for d in catalog['diseases'] if d['id'] == 'prostate_cancer')
    mapping = copy.deepcopy(parent)
    mapping.update(id=SCOPE_ID, label=LABEL, aliases=[LABEL,SCOPE_ID],
                   parent_id='prostate_cancer', definition=NOTE,
                   baseline_scope='prostate_cancer',
                   baseline_scope_note='Evidence restriction; retained endpoints describe overall prostate cancer incidence.')
    catalog['diseases'].append(mapping)
    # This restricts evidence, not diagnosis risk. Explicitly identify the
    # reused general-prostate reference in every copied reference record.
    catalog['records'].extend({**copy.deepcopy(r), 'disease': SCOPE_ID,
        'baseline_scope': 'prostate_cancer'} for r in list(catalog.get('records', []))
        if r['disease'] == 'prostate_cancer')
    return catalog


def install_calculations(module):
    if getattr(module, '_general_prostate_scope_installed', False):
        return
    old = module.baseline_risk_for_disease
    @wraps(old)
    def baseline(disease, *args, **kwargs):
        return old(GENERAL if is_scope(disease) else disease, *args, **kwargs)
    module.baseline_risk_for_disease = baseline
    old_exclusion = getattr(module, 'is_curated_meta_analysis_exclusion', None)
    if old_exclusion:
        @wraps(old_exclusion)
        def excluded(pmid, disease, *args, **kwargs):
            return old_exclusion(pmid, GENERAL if is_scope(disease) else disease, *args, **kwargs)
        module.is_curated_meta_analysis_exclusion = excluded
    module._general_prostate_scope_installed = True


def install_engine(engine):
    """Extend the preserved engine without mutating its immutable source files."""
    install_calculations(engine.meta_analysis)
    old_resolve = engine.resolve_disease_scope
    @wraps(old_resolve)
    def resolve(disease=GENERAL, subcategory=None):
        if is_scope(disease) or is_scope(subcategory):
            if subcategory and not is_scope(subcategory):
                raise ValueError('Conflicting prostate cancer subcategory.')
            if not (is_scope(disease) or engine.get_major_scope_key(disease)
                    or disease == 'prostate_cancer'):
                raise ValueError('This prostate cancer subcategory has a different parent.')
            return LABEL
        return old_resolve(disease, subcategory)
    engine.resolve_disease_scope = resolve
    old_config = engine.get_scope_config
    @wraps(old_config)
    def config():
        scopes = copy.deepcopy(old_config())
        scopes['prostate']['subcategories'].append({'key':SCOPE_ID,'label':LABEL,
            'disease':GENERAL,'lifetime_risk_percent':13.2,'scope_note':NOTE})
        return scopes
    engine.get_scope_config = config
    old_path = engine.get_cache_path
    @wraps(old_path)
    def cache_path(disease, *args, **kwargs):
        return old_path(SCOPE_ID if is_scope(disease) else disease, *args, **kwargs)
    engine.get_cache_path = cache_path


def install_plotting(renderer, localization):
    install_calculations(renderer.meta_analysis)
    original = renderer.safe_component
    renderer.safe_component = lambda value: SCOPE_ID if is_scope(value) else original(value)
    localization.DISEASE_LABELS[LABEL] = TRANSLATIONS
