"""Refresh Chinese summary figures from saved rows, without re-pooling evidence.

Pass the published summary manifests with --manifest. Workbooks and source
data are hashed and left unchanged. Rendering is staged before release copies
are replaced; active interim-bundle checksums are updated with the figures.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from openpyxl import load_workbook
from scripts import build_summary_plots as plots


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path):
    book = load_workbook(path, read_only=True, data_only=True)
    iterator = book.active.iter_rows(values_only=True)
    columns = next(iterator)
    rows = []
    for values in iterator:
        row = dict(zip(columns, values))
        if not isinstance(row.get('number studies', row.get('number estimates')), (int, float)):
            continue
        row['category'] = plots.exposure_category(row['Exposure'])
        rows.append(row)
    book.close()
    return rows


def plan(manifests):
    jobs, inputs = [], {}
    for path in manifests:
        manifest = json.loads(path.read_text())
        inputs[path] = digest(path)
        mode = manifest.get('include_subexposures')
        if mode not in ('yes', 'no'):
            mode = next((part for part in path.parts if part in ('yes', 'no')), None)
            if mode is None:
                mode = next((part.rsplit('_', 1)[-1] for part in path.parts if part in ('subexposures_yes', 'subexposures_no')), None)
        for key, scope in manifest['scopes'].items():
            datasets = {}
            saved_rows = path.parent / 'summary_rows.json'
            if saved_rows.exists():
                inputs[saved_rows] = digest(saved_rows)
                datasets = json.loads(saved_rows.read_text())
            else:
                for dataset, entry in scope.get('workbooks', {}).items():
                    source = path.parent / entry['filename']
                    if source.exists():
                        inputs[source] = digest(source)
                        datasets[dataset] = read_rows(source)
            for name, entry in scope.get('plots', {}).items():
                if not entry.get('available'):
                    continue
                for locale in ('zh-CN', 'zh-TW'):
                    filename = entry.get('locales', {}).get(locale)
                    if filename:
                        target = path.parent / filename
                        if not target.resolve().is_relative_to(path.parent.resolve()):
                            raise ValueError(f'Plot path escapes its release: {target}')
                        jobs.append(dict(target=target, name=name, locale=locale,
                                         scope=scope, manifest=manifest, datasets=datasets,
                                         mode=mode, expected=entry['exposure_count']))
    return jobs, inputs


def render(job, target):
    scope, name, locale = job['scope'], job['name'], job['locale']
    disease = scope['label']
    plots.SUBEXPOSURE_MODE = job['mode']
    if name == 'evidence-coverage':
        from scripts.nonmalignant_summary import coverage_plot
        coverage_plot(target, scope['evidence_coverage'], disease, job['mode'], locale)
        return scope['evidence_coverage']['exposure_count']
    if name.startswith('forest-'):
        dietary = name.endswith('-dietary')
        direction = 'protective' if 'protective' in name else 'harmful'
        function = plots.forest_plot
        args = [target, job['datasets']['dietary' if dietary else 'combined'],
                disease, direction, dietary, locale]
    elif name in ('effect-heterogeneity', 'egger-heterogeneity'):
        function = plots.heterogeneity_plot
        args = [target, job['datasets']['combined'], disease,
                'effect' if name == 'effect-heterogeneity' else 'egger', locale]
    else:
        raise ValueError(f'Unsupported summary plot: {name}')
    kwargs = {}
    if 'coverage_note' in inspect.signature(function).parameters:
        kwargs['coverage_note'] = plots.screening_coverage_note(job['manifest'].get('screening_coverage'), locale)
    return function(*args, **kwargs)


def refresh(manifests, check=False):
    jobs, inputs = plan(manifests)
    print(f'{len(jobs)} Chinese summary figures from {len(manifests)} manifests', flush=True)
    if check:
        return
    changed = []
    reused = {}
    with tempfile.TemporaryDirectory(prefix='summary-translations-') as directory:
        staging = Path(directory)
        for index, job in enumerate(jobs):
            identity = json.dumps({k: v for k, v in job.items() if k != 'target'}, sort_keys=True, default=str)
            if identity in reused:
                job['staged'] = reused[identity]
                continue
            target = staging / str(index) / job['target'].name
            target.parent.mkdir(parents=True)
            count = render(job, target)
            if count != job['expected']:
                raise ValueError(f"Saved plot count differs: {job['target']} ({count} != {job['expected']})")
            job['staged'] = target
            reused[identity] = target
            if (index + 1) % 20 == 0:
                print(f'Rendered {index + 1}/{len(jobs)}', flush=True)
        if any(digest(path) != expected for path, expected in inputs.items()):
            raise ValueError('A saved workbook or manifest changed during rendering')
        for job in jobs:
            for suffix in ('.pdf', '.png'):
                source, target = job['staged'].with_suffix(suffix), job['target'].with_suffix(suffix)
                # Refresh existing previews only; do not invent new release assets.
                if not source.exists() or (suffix == '.png' and not target.exists()):
                    continue
                old_mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                target.chmod(old_mode)
                changed.append(target)
    print(f'Refreshed {len(changed)} figures/previews; saved numerical workbooks unchanged.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, action='append', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    refresh([path.resolve() for path in args.manifest], args.check)
