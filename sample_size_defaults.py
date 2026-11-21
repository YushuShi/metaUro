"""Generate and serve the default sample-size plots from the deployed evidence."""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

CATALOG = '/api/sample-size-summary/catalog'
SUMMARY = '/api/sample-size-summary'
FILE = 'data/sample_size_defaults.json'
VERSION = 1


def _revision(root, ref='HEAD'):
    """Identify the release tree without including its derived snapshot."""
    root = root.resolve()
    try:
        top = Path(subprocess.check_output(
            ['git', 'rev-parse', '--show-toplevel'], cwd=root, stderr=subprocess.DEVNULL
        ).decode().strip()).resolve()
        path = (root / FILE).relative_to(top).as_posix().encode()
        entries = subprocess.check_output(
            ['git', 'ls-tree', '-rz', '--full-tree', ref],
            cwd=root, stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None
    digest = hashlib.sha256()
    for entry in entries.split(b'\0'):
        if entry and entry.partition(b'\t')[2] != path:
            digest.update(entry + b'\0')
    return digest.hexdigest()


@lru_cache(maxsize=8)
def _read(path, modified, size):
    doc = json.loads(path.read_text())
    return doc if doc['version'] == VERSION else None


def _load(root, revision):
    path = root / FILE
    try:
        stat = path.stat()
        bundle = _read(path, stat.st_mtime_ns, stat.st_size)
        if bundle and bundle['source_revision'] == revision:
            return bundle
    except (OSError, KeyError, ValueError, TypeError):
        pass
    return None


def _equal(raw, default):
    if isinstance(default, bool):
        return raw.lower() in ({'1', 'yes', 'true'} if default else {'0', 'no', 'false'})
    if isinstance(default, (int, float)):
        try:
            return math.isfinite(float(raw)) and float(raw) == default
        except ValueError:
            return False
    return isinstance(default, str) and raw == default


def _defaults_match(args, catalog, profile, summary):
    if any(len(values) != 1 for _, values in args.lists()):
        return False
    defaults = {
        'keep_latest_cohort': True, 'include_preprints': False,
        'include_subexposures': 'yes',
    }
    current = (summary or {}).get('analysis_settings', catalog.get('analysis_settings', {}))
    for name, value in current.items():
        if name in ('baseline_source', 'age', 'years', 'sex', 'race'):
            defaults[name] = value
    if profile == 'onco':
        if not current:
            defaults.update(baseline_source='seer', age=60, years=10, sex='site_default')
    elif profile == 'uro':
        if summary is None:
            # The prostate and bladder workers have different default sexes.
            defaults.update(baseline_source='seer', age=60, years=10,
                            race='all_races_ethnicities')
    elif profile == 'neuro':
        defaults.update(incidenceStartAge=65, incidenceYears=10, incidenceSex='both')
    defaults['latest_cohort'] = defaults['keep_latest_cohort']
    defaults.update(catalog['defaults'])
    for key, value in args.items():
        if key in {'lang', 'disease', 'scope', 'outcome', 'exposure', 'max_sample_size',
                   'effect_measure', 'forest_scale', 'study_design', 'studyDesign',
                   'exposure_rate', 'controls_per_case', 'controlExposurePercent',
                   'controlsPerCase'}:
            continue
        if summary is None and key in catalog['defaults']:
            continue
        if key in ('age', 'years', 'incidenceStartAge', 'incidenceYears') and not value.strip().isdecimal():
            return False
        if key == 'sidedness' and value.lower() == '2':
            value = 'two'
        if not _equal(value, defaults.get(key)):
            return False
    return True


def install(app, profile='standard', root=None):
    """Short-circuit only matching requests, before pooling or starting workers."""
    from flask import jsonify, request

    root = Path(root or app.root_path).resolve()

    @lru_cache(maxsize=1)
    def release():
        return _revision(root)

    @app.before_request
    def default_sample_size():
        if (app.config.get('SAMPLE_SIZE_DEFAULTS_DISABLED')
                or request.method not in ('GET', 'HEAD')
                or request.path not in (CATALOG, SUMMARY)):
            return None
        bundle = _load(root, release())
        if not bundle or bundle['profile'] != profile:
            return None
        catalog = bundle['catalog']
        # The page always submits a catalog ID. Leave aliases, errors and
        # implicit endpoint selection to the existing live handler.
        summary = None
        if request.path == SUMMARY:
            summary = bundle['summaries'].get(request.args.get('disease'))
            if summary is None:
                return None
        if not _defaults_match(request.args, catalog, profile, summary):
            return None
        data = catalog if summary is None else summary
        response = jsonify(data)
        response.headers['X-Sample-Size-Source'] = 'precomputed'
        return response


def generate(app, root, profile, force=False):
    revision = _revision(root)
    existing = None if force else _load(root, revision)
    if (existing and existing['profile'] == profile and revision is not None
            and set(existing['summaries']) == {d['value'] for d in existing['catalog']['diseases']}):
        print(f"Default sample size plots already prepared for {len(existing['summaries'])} outcomes.", flush=True)
        return
    previous = {key: app.config.get(key) for key in ('SAMPLE_SIZE_DEFAULTS_DISABLED', 'SAMPLE_SIZE_BUILD')}
    app.config['SAMPLE_SIZE_DEFAULTS_DISABLED'] = True
    app.config['SAMPLE_SIZE_BUILD'] = {}
    client = app.test_client()

    def fetch(path, **params):
        response = client.get(path, query_string=params)
        if response.status_code != 200:
            raise RuntimeError(f'{path} {params}: HTTP {response.status_code}: {response.get_json(silent=True)}')
        return response.get_json()

    try:
        catalog = fetch(CATALOG)
        if not catalog['diseases']:
            raise ValueError('The sample size catalog has no outcomes.')
        summaries = {}
        for disease in catalog['diseases']:
            label = disease['value']
            summary = fetch(SUMMARY, disease=label)
            if summary['disease'] != label or len(summary['panels']) != 4:
                raise ValueError(f'Invalid sample size results for {label}')
            summaries[label] = summary
            print(f'{len(summaries)}/{len(catalog["diseases"])} {label}', flush=True)
        bundle = dict(version=VERSION, source_revision=revision, profile=profile,
                      catalog=catalog, summaries=summaries)
        path = root / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False, encoding='utf-8') as stream:
            temporary = Path(stream.name)
            try:
                json.dump(bundle, stream, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':'), sort_keys=True)
                stream.write('\n')
                stream.flush()
                temporary.chmod(0o644)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        app.config.update(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('standard', 'neuro', 'onco', 'uro'), default='standard')
    parser.add_argument('--force', action='store_true')
    options = parser.parse_args()
    from app import app

    generate(app, Path(__file__).resolve().parent, options.profile, options.force)


if __name__ == '__main__':
    main()
