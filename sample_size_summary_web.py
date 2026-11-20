"""Request-only planning over the same evidence and models as the sealed summaries."""
from functools import lru_cache
import inspect
import json
import math
from pathlib import Path

from runtime_code.sample_size_summary_native import (
    _baseline, _distinct_count, _exposures, _frame, _native_rows, _point_style,
    _read, _scopes, _settings, _source,
)
from sample_size_planning import DEFAULTS, planning, total_sample_size
from sample_size_trial_direction import trial_direction


def _pool(namespace, repo, scope, exposure, rows, baseline, settings, dietary):
    frame = _frame(rows, dietary)
    if len(frame) < 2:
        return None, 0, False, trial_direction([], 1)
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
        return None, 0, False, trial_direction([], 1)
    return (float(rr), _distinct_count(pooled), any(row.get('preprint_derived') is True for row in pooled),
            trial_direction(pooled, float(rr)))


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
                    rr, count, preprint_derived, trials = pooled_source(
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
                                                    **trials,
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
