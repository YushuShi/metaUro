"""Read-only sample-size summaries from the native, released evidence views.

The native eligibility, conversion and random-effects engines remain the source
of each pooled RR. No caches, plots, or source records are materialized here.
"""
from __future__ import annotations

from functools import lru_cache
import inspect
import json
import math
from pathlib import Path

from sample_size_planning import DEFAULTS, planning, total_sample_size


def _read(path):
    path = Path(path)
    if not path.is_file():
        return {}
    stat = path.stat()
    return _read_version(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=64)
def _read_version(path, modified, size):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _boolean(value, default=False):
    if value is None:
        return default
    if str(value).lower() not in {'true', 'false', 'yes', 'no', '1', '0'}:
        raise ValueError('Boolean settings must be yes or no.')
    return str(value).lower() in {'true', 'yes', '1'}


def _settings(namespace, values, catalog):
    fields = ('baseline_source', 'age', 'years', 'sex', 'race', 'manual_risk_percent')
    raw = {key: values[key] for key in fields if values.get(key) not in (None, '')}
    raw['keep_latest_cohort'] = _boolean(values.get('keep_latest_cohort', values.get('latest_cohort')), True)
    raw['include_preprints'] = _boolean(values.get('include_preprints'), False)
    if values.get('baseline_percent') not in (None, ''):
        raw.update(baseline_source='manual', manual_risk_percent=float(values['baseline_percent']))
    if namespace.get('resolve_incidence'):
        return namespace['normalize_settings'](raw)
    from analysis_settings_backend import normalize_settings
    return normalize_settings(raw, catalog)


def _baseline(namespace, catalog, disease, settings, subcategory=None):
    if namespace.get('resolve_incidence'):
        return namespace['resolve_incidence'](disease, settings)
    from analysis_settings_backend import resolve_baseline
    return resolve_baseline(catalog, disease, subcategory, settings)


def _scopes(namespace, catalog):
    """Use configured public scopes, including saved disease subcategories."""
    result = []
    def add(item, parent=None, parent_key=None):
        if not isinstance(item, dict):
            return
        disease = item.get('disease') or item.get('canonical_name') or item.get('label')
        label = item.get('label') or disease
        if disease and label:
            subcategory = (item.get('slug') or item.get('key')) if parent and namespace.get('get_subcategory_result_path') else None
            selected_disease = parent.get('disease', disease) if subcategory else disease
            value = f"{parent.get('label') or parent.get('disease')} / {label}" if subcategory else str(label)
            if parent and namespace.get('resolve_disease_scope'):
                selected_disease = namespace['resolve_disease_scope'](label, item.get('key'))
            aliases = ([f'{selected_disease}::{subcategory}', f'{parent_key}::{subcategory}']
                       if subcategory else [str(selected_disease)])
            aliases.extend(str(alias) for alias in item.get('aliases', []) if isinstance(alias, str))
            result.append({'value': value, 'label': value, 'disease': str(selected_disease),
                           'aliases': sorted(set(aliases)),
                           'subcategory': subcategory, 'parent_key': parent_key,
                           'endpoint': item.get('default_endpoint') or item.get('endpoint') or 'Incidence'})
        for key in ('children', 'subcategories'):
            for child in item.get(key, []) or []:
                add(child, item, item.get('key') or parent_key)
    if namespace.get('get_scope_config'):
        for key, item in namespace['get_scope_config']().items():
            add({**item, 'key': item.get('key', key)})
    elif namespace.get('load_disease_scopes'):
        for item in namespace['load_disease_scopes']():
            add(item)
    else:
        for item in catalog.get('diseases', []):
            add(item)
    if not result:
        disease = namespace.get('DEFAULT_DISEASE', '')
        add({'label': disease, 'disease': disease})
    # Additional supported diseases do not borrow a cancer-parent baseline.
    seen = {item['value'].casefold() for item in result}
    for item in catalog.get('diseases', []):
        label = item.get('label')
        if label and label.casefold() not in seen and not namespace.get('get_scope_config') and not namespace.get('load_disease_scopes'):
            add(item)
            seen.add(label.casefold())
    return list({item['value']: item for item in result}.values())


def _exposures(namespace, repo):
    function = namespace.get('get_ui_exposures') or namespace.get('get_exposures')
    if function:
        response = function()
        raw = response.get_json() if hasattr(response, 'get_json') else response
        if isinstance(raw, list):
            return sorted(set(str(item) for item in raw), key=str.casefold)
    raw = _read(repo / 'static/exposures.json')
    result = list(raw) if isinstance(raw, list) else []
    for name in ('additionalExposure.json', 'additionalExposureV2.json'):
        extra = _read(repo / 'data' / name)
        if isinstance(extra, dict):
            result.extend(extra)
    return sorted(set(str(item) for item in result), key=str.casefold)


def _source(namespace, scope, exposure):
    disease, outcome = scope['disease'], scope['endpoint']
    if scope.get('subcategory') and namespace.get('get_subcategory_result_path'):
        path = namespace['get_subcategory_result_path'](scope['parent_key'], scope['subcategory'], exposure)
        return Path(path), _read(path)
    for downstream in (False, True):
        if namespace.get('resolve_incidence'):
            # Heart's ensure_cache creates a missing file; use the read-only path resolver.
            from scripts.subexposure_views import cache_path
            path = cache_path(namespace['CACHE_DIR'], disease, exposure,
                              namespace['current_subexposure_mode'](), use_downstream=downstream)
        else:
            path = namespace['get_cache_path'](disease, exposure, outcome, True, downstream)
        if Path(path).is_file():
            return Path(path), _read(path)
    return None, {}


def _native_rows(namespace, repo, scope, exposure, path, payload, settings):
    from copy import deepcopy
    rows = deepcopy(payload.get('studies') or [])
    if settings.get('include_preprints'):
        from preprint_ui import compose_preprint_view, request_selector, normalize_preprint_frame
        import pandas as pd
        selector = request_selector(namespace, {'disease': scope['disease'], 'exposure': exposure,
                                    'outcome': scope['endpoint'], 'subcategory': scope.get('subcategory')})
        if path:
            selector['_cache_path'] = str(path.resolve().relative_to(repo.resolve()))
        rows, audit = compose_preprint_view(rows, repo, True, selector, allow_additions=True)
        if rows:
            rows = normalize_preprint_frame(pd.DataFrame(rows), audit.get('introduced_metadata_fields', [])).to_dict(orient='records')
    data = {'quality_filter': 'Moderate+', 'analysis_settings': settings}
    if namespace.get('quality_request_rows'):
        rows = namespace['quality_request_rows'](rows, data, exposure)
    elif namespace.get('adapt_trusted_source_rows'):
        rows = namespace['adapt_trusted_source_rows'](rows, str(repo), exposure)
    dietary = namespace.get('dietary')
    if dietary is not None and hasattr(dietary, 'apply_row'):
        rows = [dietary.apply_row(row, exposure) for row in rows]
    return rows


def _frame(rows, dietary):
    import pandas as pd
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    quality = frame.get('Quality Score', pd.Series('', index=frame.index)).astype(str).str.strip().str.lower()
    frame = frame.loc[quality.isin({'moderate', 'good'})].copy()
    if dietary:
        measurement = frame.get('exposure_measurement_type', pd.Series('', index=frame.index)).astype(str).str.strip().str.lower()
        frame = frame.loc[measurement.eq('dietary_intake')].copy()
    if frame.empty:
        return frame
    for field in ('Effect Size', 'Lower CI', 'Upper CI', 'SE'):
        frame[field] = pd.to_numeric(frame.get(field, pd.Series(float('nan'), index=frame.index)), errors='coerce')
    valid = frame['Effect Size'].gt(0) & frame['Lower CI'].gt(0) & frame['Upper CI'].gt(frame['Lower CI'])
    valid &= frame['Effect Size'].ge(frame['Lower CI']) & frame['Effect Size'].le(frame['Upper CI'])
    kinds = frame.get('Effect Type', pd.Series('', index=frame.index)).astype(str).str.strip().str.upper()
    valid &= kinds.isin({'RR','RRR','HR','OR','IRR','RISK RATIO','RELATIVE RISK','HAZARD RATIO','ODDS RATIO','INCIDENCE RATE RATIO'})
    frame = frame.loc[valid].copy()
    if 'Study' not in frame:
        frame['Study'] = frame.get('PMID', frame.index.to_series().astype(str))
    return frame


def _distinct_count(rows):
    keys = set()
    for index, row in enumerate(rows):
        pmid = str(row.get('PMID') or '').strip()
        if pmid.endswith('.0'):
            pmid = pmid[:-2]
        identity = pmid if pmid.lower() not in {'', '-', 'none', 'nan'} else str(row.get('Study') or index)
        keys.add(identity)
    return len(keys)


def _point_style(exposure, repo, preprint_derived=False):
    """Match the immutable source Summary scatter and its preprint labels."""
    import colorsys
    import hashlib
    import re
    # sources/<source>/scripts/diagnostic_style.py::exposure_color is the
    # shared renderer used by summary_outputs and build_cohort_summaries.
    key = re.sub(r'[^a-z0-9]+', '_', str(exposure).lower()).strip('_')
    hue = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], 'big') / 2**32
    rgb = colorsys.hls_to_rgb(hue, 0.43, 0.65)
    color = '#' + ''.join(f'{round(channel * 255):02x}' for channel in rgb)
    # build_cohort_summaries recolors recovered-source text only; dots retain
    # their ordinary per-exposure colors in the published Summary scatter.
    return {'dot_color': color, 'label_color': '#000080' if preprint_derived else color}


def _pool(namespace, repo, scope, exposure, rows, baseline, settings, dietary):
    frame = _frame(rows, dietary)
    if len(frame) < 2:
        return None, 0, False
    analysis = namespace['meta_analysis']
    engine = inspect.unwrap(analysis.perform_meta_analysis)
    if namespace.get('resolve_incidence'):
        from cohort_pooling import latest_cohort_pooling
        with latest_cohort_pooling(settings['keep_latest_cohort']) as context:
            result = engine(frame, scope['disease'], exposure, outcome=scope['endpoint'], exclude_meta=True,
                            df_all=frame.copy(deep=True), generate_plots=False, reference_baseline=baseline,
                            keep_latest_cohort=settings['keep_latest_cohort'])
        pooled = context.get('rows', [])
    else:
        from analysis_settings_backend import _ACTIVE
        context = {'settings': settings, 'baseline': baseline, 'repository': repo.name,
                   'disease': scope['disease'], 'exposure': exposure, 'quality_filter': 'Moderate+',
                   'min_cases': 0, 'pooled_plot_rows': []}
        token = _ACTIVE.set(context)
        try:
            result = engine(frame, scope['disease'], exposure, outcome=scope['endpoint'], exclude_meta=True,
                            df_all=frame.copy(deep=True), generate_plots=False)
        finally:
            _ACTIVE.reset(token)
        pooled = context.get('pooled_plot_rows', [])
    headline = result.get('headline') or {} if isinstance(result, dict) else {}
    canonical = headline.get('canonical_rr') or headline
    rr = canonical.get('pooled_es')
    if rr is None or not math.isfinite(float(rr)) or float(rr) <= 0:
        return None, 0, False
    return float(rr), _distinct_count(pooled), any(row.get('preprint_derived') is True for row in pooled)


def install_app(app, namespace):
    """Register APIs and the page without changing any published evidence."""
    from flask import jsonify, render_template, request
    namespace = namespace if isinstance(namespace, dict) else vars(namespace)
    repo = Path(namespace['__file__']).resolve().parent
    def inputs():
        catalog = _read(repo / 'static/seer-baseline.json')
        settings = _settings(namespace, request.args, catalog)
        return catalog, settings, _scopes(namespace, catalog)

    @lru_cache(maxsize=512)
    def pooled_source(path, modified, size, scope_json, exposure, settings_json, dietary, recovery_stamp):
        scope, settings = json.loads(scope_json), json.loads(settings_json)
        catalog = _read(repo / 'static/seer-baseline.json')
        baseline = _baseline(namespace, catalog, scope['disease'], settings, scope.get('subcategory'))
        payload = _read(path) if path else {}
        rows = _native_rows(namespace, repo, scope, exposure, Path(path) if path else None, payload, settings)
        return _pool(namespace, repo, scope, exposure, rows, baseline, settings, dietary)

    def catalog_view():
        try:
            catalog, settings, scopes = inputs()
            diseases = []
            for scope in scopes:
                baseline = _baseline(namespace, catalog, scope['disease'], settings, scope.get('subcategory'))
                diseases.append({'value': scope['value'], 'label': scope['label'], 'aliases': scope.get('aliases', [scope['disease']]), 'baseline_percent': baseline.get('risk_percent')})
            return jsonify(diseases=diseases, defaults=DEFAULTS, analysis_settings=settings)
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify(error=str(exc)), 400

    def summary_view():
        try:
            catalog, settings, scopes = inputs()
            requested = request.args.get('disease') or namespace.get('DEFAULT_DISEASE') or scopes[0]['value']
            scope = next((row for row in scopes if requested.casefold() in {str(alias).casefold() for alias in [row['value'], *row.get('aliases', [row['disease']])]}), None)
            if scope is None:
                raise ValueError('Select a configured disease.')
            options = planning(request.args)
            baseline = _baseline(namespace, catalog, scope['disease'], settings, scope.get('subcategory'))
            panels = [{'measurement': measurement, 'design': design, 'points': [], 'omitted': []}
                      for measurement in ('all', 'dietary') for design in ('prospective', 'retrospective')]
            for exposure in _exposures(namespace, repo):
                path, payload = _source(namespace, scope, exposure)
                if not payload and not settings['include_preprints']:
                    continue
                stamp = path.stat() if path else None
                registry_stamp = []
                for filename in ('data/cohort_registry.json', 'data/cohort_record_metadata.json'):
                    metadata = repo / filename
                    stat = metadata.stat() if metadata.is_file() else None
                    registry_stamp.append((filename, stat.st_mtime_ns if stat else 0, stat.st_size if stat else 0))
                recovery_stamp = json.dumps(registry_stamp)
                if settings['include_preprints']:
                    from preprint_ui import preprint_manifest_stamp
                    recovery_stamp += str(preprint_manifest_stamp(repo))
                for measurement in ('all', 'dietary'):
                    rr, count, preprint_derived = pooled_source(
                        str(path) if path else '', stamp.st_mtime_ns if stamp else 0,
                        stamp.st_size if stamp else 0, json.dumps(scope, sort_keys=True),
                        exposure, json.dumps(settings, sort_keys=True),
                        measurement == 'dietary', recovery_stamp)
                    if count < 2 or rr is None:
                        continue
                    for panel in (row for row in panels if row['measurement'] == measurement):
                        p0 = baseline.get('p0')
                        total, reason = total_sample_size(rr, p0, panel['design'], options)
                        effect = rr
                        if panel['design'] == 'retrospective' and p0 is not None:
                            effect = rr * (1 - p0) / (1 - p0 * rr) if p0 * rr != 1 else None
                        if total is None or effect is None or not math.isfinite(effect) or effect <= 0:
                            panel['omitted'].append({'exposure': exposure, 'reason': reason or 'No finite effect estimate is available.'})
                        else:
                            panel['points'].append({'exposure': exposure, 'effect_size': effect, 'total_n': total,
                                                    'study_count': count, 'pooled_rr': rr,
                                                    **_point_style(exposure, repo, preprint_derived)})
            return jsonify(disease=scope['value'], baseline_percent=baseline.get('risk_percent'), baseline=baseline,
                           settings=options, analysis_settings=settings, panels=panels,
                           evidence_note='Each measurement group uses the same eligible pooled evidence for prospective and retrospective planning. At least two distinct study reports are required.')
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify(error=str(exc)), 400

    def page():
        return render_template('sample_size_summary.html', sample_size_title=repo.name,
                               sample_size_styles=['/static/style.css'], sample_size_header='_site_header.html')

    app.add_url_rule('/sample-size-summary', 'sample_size_summary', page)
    app.add_url_rule('/api/sample-size-summary/catalog', 'sample_size_summary_catalog', catalog_view)
    app.add_url_rule('/api/sample-size-summary', 'sample_size_summary_data', summary_view)
