"""Materialize disease parents inside one isolated combined exposure-mode cache.

The input index is frozen before deriving any parents, so a nested disease
parent never introduces a second copy of its descendants. Original direct
parent caches are retained under _disease_scope_sources for repeatable builds.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

VERSION = 'combined-disease-scopes-2026-09-07-v1'


def _encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       indent=2) + '\n').encode('utf-8')



def analyze_parent(frame, all_frame, disease, exposure, endpoint='Incidence'):
    """Analyze one parent with the UI default reference and no plot rendering."""
    import meta_analysis
    import analysis_settings_backend as settings_backend
    repo = Path(__file__).resolve().parents[1]
    catalog = json.loads((repo / 'static/seer-baseline.json').read_text())
    settings = settings_backend.normalize_settings(None, catalog)
    baseline = settings_backend.resolve_baseline(catalog, disease, None, settings)
    context = {'settings': settings, 'baseline': baseline, 'disease': disease,
        'exposure': exposure, 'repository': repo.name, 'quality_filter': 'Moderate+',
        'pooled_plot_rows': [], 'pooled_study_keys': set()}
    frame, all_frame = frame.copy(deep=True), all_frame.copy(deep=True)
    frame['Estimated Cases'] = None
    all_frame['Estimated Cases'] = None
    token = settings_backend._ACTIVE.set(context)
    try:
        # Retain request-aware conversion helpers, without invoking the response
        # wrapper that renders PNGs. This is the established parent-summary path.
        original = getattr(meta_analysis.perform_meta_analysis, '__wrapped__',
                           meta_analysis.perform_meta_analysis)
        result = original(frame, disease, exposure, outcome=endpoint,
                          exclude_meta=True, df_all=all_frame, generate_plots=False)
    finally:
        settings_backend._ACTIVE.reset(token)
    return {'result': result, 'pooled_rows': context['pooled_plot_rows'],
            'baseline': baseline, 'settings': settings}


def materialize(cache_root):
    """Save identical all/core parent datasets, retaining every source row."""
    import app
    import meta_analysis
    import pandas as pd
    import analysis_settings_backend as settings_backend
    from disease_hierarchy import compose_result, descendant_keys
    from scripts.subexposure_policy import pooling_rows

    root = Path(cache_root).resolve()
    if not root.is_dir():
        raise ValueError(f'Missing saved mode cache: {root}')
    repo = Path(__file__).resolve().parents[1]
    if root in {repo / 'Cached_results', repo / 'combined', repo / 'combined/Cached_results'}:
        raise ValueError('Disease projections require an isolated UI mode cache.')
    scopes = app.get_scope_config()
    parents = [key for key, scope in scopes.items() if scope.get('children')]
    source_root = root / '_disease_scope_sources'
    index = {}
    source_writes = {}
    original_hashes = {}
    # Prefer core inputs; both outputs use that same immutable input bundle.
    for suffix in ('core', 'all'):
        for path in sorted(root.glob(f'*/*_true_{suffix}.json')):
            raw = path.read_bytes()
            payload = json.loads(raw)
            source_path = path
            if payload.get('disease_hierarchy_prebuilt') is True:
                source_path = source_root / path.relative_to(root)
                if not source_path.is_file():
                    continue  # A synthetic parent has no direct source evidence.
                raw = source_path.read_bytes()
                payload = json.loads(raw)
            if payload.get('canonical_track') != 'combined':
                raise ValueError(f'Non-combined disease source: {source_path}')
            key = app.get_major_scope_key(payload.get('disease') or payload.get('cancer'), scopes)
            exposure = payload.get('exposure')
            endpoint = payload.get('endpoint') or payload.get('outcome') or 'Incidence'
            if not key or not exposure:
                continue
            context = (key, exposure, endpoint)
            if context in index:
                continue
            original_hashes[source_path] = hashlib.sha256(raw).hexdigest()
            # Once a direct parent path becomes a union, its untouched input
            # remains available for audit and for another materialize() call.
            if key in parents and source_path == path:
                source_path = source_root / path.relative_to(root)
                source_writes[source_path] = raw
            index[context] = (payload, source_path, hashlib.sha256(raw).hexdigest())

    report = {'version': VERSION, 'canonical_track': 'combined',
              'generated_parent_count': 0, 'generated_file_count': 0,
              'visible_rows': 0, 'selected_rows': 0, 'parents': {}, 'source_hashes': {}}
    writes = {}
    for parent in parents:
        children = descendant_keys(scopes, parent)
        contexts = sorted({(exposure, endpoint) for key, exposure, endpoint in index
                           if key in {parent, *children}
                           and endpoint in scopes[parent].get('endpoints', ['Incidence'])})
        parent_report = {'label': scopes[parent]['disease'], 'children': children,
                         'contexts': 0, 'visible_rows': 0, 'selected_rows': 0, 'files': []}
        for exposure, endpoint in contexts:
            direct = index.get((parent, exposure, endpoint))
            sources = []
            source_checksums = {}
            for key in [parent, *children]:
                entry = index.get((key, exposure, endpoint))
                if not entry:
                    continue
                payload, path, checksum = entry
                relative = str(path.relative_to(root))
                source_checksums[relative] = checksum
                if key != parent:
                    sources.append((key, payload, relative))
            payload = compose_result(direct[0] if direct else None, sources, parent, scopes,
                exposure, endpoint, source_filter=meta_analysis.filter_curated_meta_analysis_exclusions,
                include_direct_only=True)
            if payload is None:
                continue
            if direct:
                for row in payload['studies']:
                    if row.get('disease_hierarchy_source_key') == parent:
                        row['disease_hierarchy_source_path'] = str(direct[1].relative_to(root))
            rows = payload.get('studies') or []
            candidates = [row for row in pooling_rows(rows)
                          if row.get('disease_hierarchy_pooling_selected') is not False]
            frame = pd.DataFrame(candidates)
            if not frame.empty:
                frame = meta_analysis.filter_curated_meta_analysis_exclusions(
                    frame, scopes[parent]['disease'], exposure, endpoint)
            calculated = {}
            pooled_count = 0
            if not frame.empty:
                calculation = analyze_parent(frame, pd.DataFrame(rows),
                    scopes[parent]['disease'], exposure, endpoint)
                calculated = calculation['result']
                pooled_count = len(calculation['pooled_rows'])
                if calculated.get('error'):
                    raise ValueError(f'Parent analysis failed for {parent}/{exposure}: {calculated["error"]}')
            payload.update({field: copy.deepcopy(calculated.get(field))
                            for field in ('headline', 'summary_html')})
            payload.update(success=True, canonical_track='combined', disease_hierarchy_prebuilt=True,
                requires_key_review=any(row.get('requires_key_review') for row in rows),
                disease_hierarchy_materialization={'version': VERSION,
                    'source_root': 'mode Cached_results', 'source_hashes': source_checksums,
                    'source_fingerprint': hashlib.sha256(_encoded(source_checksums)).hexdigest()})
            mode = root.parent.name if root.parent.name in {'yes', 'no'} else next(
                (source[0].get('include_subexposures') for source in index.values()
                 if source[0].get('include_subexposures') in {'yes', 'no'}), None)
            if mode is not None:
                payload['include_subexposures'] = mode
            encoded = _encoded(app.sanitize_data(payload))
            for suffix in ('core', 'all'):
                relative = Path(app.safe_path_component(exposure)) / (
                    app.safe_path_component(f'{scopes[parent]["disease"]}_{endpoint}_true_{suffix}') + '.json')
                writes[root / relative] = encoded
                parent_report['files'].append(str(relative))
            count = pooled_count
            parent_report['contexts'] += 1
            parent_report['visible_rows'] += len(rows)
            parent_report['selected_rows'] += count
            report['source_hashes'].update(source_checksums)
        report['parents'][parent] = parent_report
        report['generated_parent_count'] += parent_report['contexts']
        report['visible_rows'] += parent_report['visible_rows']
        report['selected_rows'] += parent_report['selected_rows']

    # Derive everything before writing, then verify original sources did not
    # change during computation. A failed calculation leaves mode data intact.
    for path, expected in original_hashes.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Disease source changed during build: {path}')
    for path, data in {**source_writes, **writes}.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    report['generated_file_count'] = len(writes)
    report['source_fingerprint'] = hashlib.sha256(_encoded(report['source_hashes'])).hexdigest()
    (root / '_disease_scope_manifest.json').write_bytes(_encoded(report))
    return report
