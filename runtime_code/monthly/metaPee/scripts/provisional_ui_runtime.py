"""Serve one saved provisional combined view and its locally computed plots."""
from __future__ import annotations
import base64
import copy
import shutil
import hashlib
import inspect
import json
from functools import lru_cache
from pathlib import Path


def view_root(repo_root):
    return Path(repo_root) / 'combined/provisional_ui'


@lru_cache(maxsize=12)
def _verified(root, signature):
    root = Path(root)
    manifest = json.loads((view_root(root) / 'manifest.json').read_text())
    from scripts import provisional_review_policy
    if not manifest.get('complete') or manifest.get('version') != provisional_review_policy.VERSION:
        return False
    for relative, expected in manifest['protected_source_hashes'].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            return False
    return True


def ready(repo_root):
    root = Path(repo_root)
    path = view_root(root) / 'manifest.json'
    try:
        manifest = json.loads(path.read_text())
        files = [path, *(root / name for name in manifest['protected_source_hashes'])]
        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
        return _verified(str(root), signature)
    except (OSError, ValueError, KeyError):
        return False


def ensure_plots(result, repo_root, mode, request_data, backend):
    """Render plots from the exact selected saved rows; never perform screening."""
    if result.get('canonical_track') != 'combined':
        return result
    import analysis_settings_backend
    if analysis_settings_backend.settings_active():
        # The response hook already renders forest/Baujat/funnel PNGs from these
        # exact saved rows with the visitor's current analysis settings.
        return result
    from scripts import subexposure_policy
    import pandas as pd
    rows = result.get('studies') or []
    pool = subexposure_policy.pooling_rows(rows)
    if not pool:
        return result
    disease = result.get('disease') or result.get('cancer')
    exposure = result.get('exposure')
    endpoint = result.get('endpoint') or result.get('outcome') or 'Incidence'
    identity = [rows, mode, disease, exposure, endpoint,
                (request_data or {}).get('analysis_settings'),
                (request_data or {}).get('locale') or (request_data or {}).get('language') or (request_data or {}).get('lang') or 'en']
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:20]
    folder = view_root(repo_root) / 'static' / mode / digest
    receipt = folder / 'plot_result.json'
    fields = ('plot_url', 'funnel_plot_url', 'baujat_plot_url')
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if all((view_root(repo_root) / 'static' / str(saved[key]).split('provisional-assets/', 1)[-1].split('?', 1)[0]).is_file()
               for key in fields if saved.get(key)):
            return {**result, **{key: saved.get(key) for key in fields}}
    folder.mkdir(parents=True, exist_ok=True)
    prefix = 'provisional-assets/' + mode + '/' + digest
    options = {'outcome': endpoint, 'exclude_meta': True,
               'df_all': pd.DataFrame(rows), 'generate_plots': True}
    parameters = inspect.signature(backend.perform_meta_analysis).parameters
    if 'plot_output_root' in parameters:
        options.update(plot_output_root=str(folder), plot_url_prefix=prefix)
        if 'locale' in parameters:
            options['locale'] = (request_data or {}).get('locale') or (request_data or {}).get('language') or (request_data or {}).get('lang') or 'en'
    elif 'plot_root' in parameters:
        options['plot_root'] = str(folder)
    elif 'plot_namespace' in parameters:
        options['plot_namespace'] = 'subexposures/' + mode + '/interactive/' + digest[:16]
    else:
        raise ValueError('This plot backend does not support isolated saved-view plots.')
    calculated = backend.perform_meta_analysis(pd.DataFrame(pool), disease, exposure, **options)
    if calculated.get('error') or calculated.get('success') is False:
        raise ValueError('Saved evidence plots could not be calculated: ' + str(calculated.get('error')))
    saved = {}
    for key in fields:
        value = calculated.get(key)
        if value and str(value).startswith('data:image/png;base64,'):
            (folder / (key + '.png')).write_bytes(base64.b64decode(str(value).split(',', 1)[1]))
            value = prefix + '/' + key + '.png'
        elif value and 'plot_namespace' in options:
            original = Path(repo_root) / str(value).split('?', 1)[0]
            if not original.is_file():
                raise ValueError('Saved evidence plot is missing: ' + str(value))
            shutil.copy2(original, folder / (key + '.png'))
            value = prefix + '/' + key + '.png'
        elif value and str(value).startswith('static/'):
            value = prefix + '/' + str(value).split('?', 1)[0][len('static/'):]
        saved[key] = value
    receipt.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + '\n')
    return {**result, **saved}


def generation(repo_root):
    """Bind interactive row selection to the installed saved evidence generation."""
    path = view_root(repo_root) / 'manifest.json'
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'fixture'


def row_id(result, repo_root, mode, index, row):
    context = [generation(repo_root), mode, result.get('disease') or result.get('cancer'),
               result.get('exposure'), result.get('endpoint') or result.get('outcome'),
               index, row.get('Record ID') or row.get('Effect ID')]
    return hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()[:24]


def identify_rows(result, repo_root, mode):
    result = copy.deepcopy(result)
    for index, row in enumerate(result.get('studies') or []):
        row['Cache Row ID'] = row_id(result, repo_root, mode, index, row)
    return result


def bind_submitted_rows(submitted, saved_result, repo_root, mode):
    """Use saved identity, quality and scientific gates for interactive edits."""
    if saved_result.get('canonical_track') != 'combined':
        raise ValueError('Reload the saved evidence before recalculating.')
    saved = identify_rows(saved_result, repo_root, mode)
    allowed = {row['Cache Row ID']: row for row in saved.get('studies') or []}
    edits = {'Effect Type', 'Effect Size', 'Lower CI', 'Upper CI', 'Original Effect Type',
             'Original Effect Size', 'Original Lower CI', 'Original Upper CI', 'Cases',
             'Sample Size', 'Participants', 'SE'}
    selected = []; seen = set()
    for submitted_row in submitted:
        key = submitted_row.get('Cache Row ID')
        if key not in allowed or key in seen:
            raise ValueError('The study selection is stale. Reload the saved evidence before recalculating.')
        row = copy.deepcopy(allowed[key]); seen.add(key)
        row.update({field: copy.deepcopy(value) for field, value in submitted_row.items() if field in edits})
        selected.append(row)
    return selected
