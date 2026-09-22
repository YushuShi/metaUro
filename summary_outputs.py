"""One published location for current summary workbooks, PDFs and manifests."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

SOURCES = ('metaPee', 'MetaVir')
MODES = ('yes', 'no')
_ACTIVE_BUILD = ContextVar('metauro_summary_build', default=None)
ASSET_NAME = re.compile(
    r'(?:exposures_meta_analysis_.+\.(?:xlsx|csv)|'
    r'forest_(?:protective|harmful)_.+\.(?:pdf|png)|'
    r'plot_(?:es|eggers)_vs_heterogeneity_.+\.(?:pdf|png))$')


def summary_mode(value):
    if value is None or str(value).lower().strip() in {'', 'yes', 'true', '1'}:
        return 'yes'
    if str(value).lower().strip() in {'no', 'false', '0'}:
        return 'no'
    raise ValueError('include_subexposures must be yes or no')


def summary_dir(repo_root, source, mode):
    if source not in SOURCES:
        raise ValueError('Unknown summary source')
    return Path(repo_root).resolve() / 'summary' / summary_mode(mode)


def metauro_root(source_root):
    source_root = Path(source_root).resolve()
    for parent in (source_root, *source_root.parents):
        if (parent / 'source_worker.py').is_file() and (parent / 'summary_outputs.py').is_file():
            return parent
    return None


def _mode_from_paths(*paths):
    for path in paths:
        parts = Path(path).parts
        for index in range(len(parts) - 1, -1, -1):
            if parts[index] in MODES:
                return parts[index]
    return 'yes'


def cache_dir(repo_root, source, mode):
    from automation.sync_sources import assembled_source
    root = assembled_source(repo_root, source)
    if source == 'metaPee':
        return root / 'combined/provisional_ui/subexposure_views' / mode / 'Cached_results'
    return root / 'Subexposure_results' / mode


def resolve_build_paths(source_root, cache_root, requested_plot_root):
    """Redirect legacy entry points, including explicit old --plot-root values."""
    repo = metauro_root(source_root)
    if repo is None:
        return Path(cache_root), Path(requested_plot_root), None
    active = _ACTIVE_BUILD.get()
    if active is not None:
        return active['cache'], active['stage'], active['mode']
    source = Path(source_root).name
    mode = _mode_from_paths(requested_plot_root, cache_root)
    incoming = Path(cache_root).resolve()
    source_path = Path(source_root).resolve()
    # A caller may be building a new evidence view in a staging directory.
    # Keep that explicit new input; redirect only the old default input aliases.
    aliases = {source_path / 'Cached_results', source_path / 'combined',
               source_path / 'Subexposure_results' / mode,
               source_path / 'combined/subexposure_views' / mode / 'Cached_results'}
    current_cache = cache_dir(repo, source, mode)
    if incoming in aliases:
        incoming = current_cache
    return incoming, summary_dir(repo, source, mode), mode


def canonical_asset_path(source_root, path):
    """Route low-level writers used by older rebuilding scripts as well."""
    repo = metauro_root(source_root)
    path = Path(path)
    if repo is None:
        return path
    source = Path(source_root).name
    active = _ACTIVE_BUILD.get()
    if active is not None:
        if path.resolve().is_relative_to(active['stage']):
            return path
        relative = Path(path.name)
        if 'locales' in path.parts:
            relative = Path(*path.parts[path.parts.index('locales'):])
        return active['stage'] / relative
    if path.resolve().is_relative_to(repo / 'summary'):
        return path
    mode = _mode_from_paths(path)
    relative = Path(path.name)
    if 'locales' in path.parts:
        index = path.parts.index('locales')
        relative = Path(*path.parts[index:])
    return summary_dir(repo, source, mode) / relative


def is_summary_artifact(path):
    path = Path(path)
    return bool(ASSET_NAME.fullmatch(path.name)) or (
        'summary-corrections' in path.parts and path.suffix in {'.xlsx', '.pdf', '.png'})


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def input_fingerprint(repo_root, source, mode):
    """Hash saved evidence and code, so edits invalidate published summaries."""
    repo = Path(repo_root).resolve()
    from automation.sync_sources import assembled_source
    source_root = assembled_source(repo, source)
    cache = cache_dir(repo, source, mode)
    inputs = set(cache.rglob('*.json'))
    inputs.update(source_root.glob('*.py'))
    inputs.update(source_root.glob('*.csv'))
    inputs.update((source_root / 'data').glob('*.json'))
    inputs.update((source_root / 'scripts').glob('*.py'))
    inputs.update((source_root / 'static').rglob('*.json'))
    # Track the code actually used by these offline generators. Browser-only
    # changes and interactive request settings do not alter default summaries.
    inputs.add(repo / 'summary_outputs.py')
    for relative in ('data/exposure_hierarchy.json',
                     'combined/astra_adjudication/plot_translation_overrides.json'):
        path = source_root / relative
        if path.exists():
            inputs.add(path)
    if (repo / 'updates/manifest.json').exists():
        inputs.add(repo / 'updates/manifest.json')
    if not cache.is_dir() or not any(cache.glob('*/*_incidence_true_all.json')):
        raise FileNotFoundError(f'No current summary evidence in {cache}')
    digest = hashlib.sha256()
    for path in sorted(inputs):
        digest.update(str(path.relative_to(repo)).encode() + b'\0')
        digest.update(bytes.fromhex(sha(path)))
    return digest.hexdigest()


def declared_artifacts(manifest):
    files = set()
    for scope in manifest.get('scopes', {}).values():
        for workbook in scope.get('workbooks', {}).values():
            # Empty workbooks are still explicitly generated outputs.
            if workbook.get('filename'):
                files.add(workbook['filename'])
        for plot in scope.get('plots', {}).values():
            if plot.get('available'):
                files.add(plot['filename'])
                files.update(plot.get('locales', {}).values())
    for relative in files:
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('Invalid summary manifest path')
    return files


def finalize_build(source_root, plot_root, manifest):
    """Publish checksums and remove outputs no longer declared by this build."""
    repo = metauro_root(source_root)
    if repo is None:
        return manifest
    folder = Path(plot_root).resolve()
    source = Path(source_root).name
    active = _ACTIVE_BUILD.get()
    mode = active['mode'] if active else _mode_from_paths(folder)
    expected_folder = active['stage'] if active else summary_dir(repo, source, mode)
    if folder != expected_folder:
        raise ValueError('Summary output must use its canonical directory')
    declared = declared_artifacts(manifest)
    new_artifacts = {name: {'sha256': sha(folder / name), 'bytes': (folder / name).stat().st_size}
                     for name in sorted(declared)}
    own_metadata = {key: value for key, value in manifest.items()
                    if key not in {'scopes', 'sources', 'artifacts'}}
    previous_path = folder / 'summary_manifest.json'
    previous = read_manifest(previous_path)
    previous_source = previous.get('sources', {}).get(source, {})
    new_scopes = set(manifest['scopes'])
    own_scopes = set(previous_source.get('scopes', [])) | new_scopes
    other_scopes = {key: value for key, value in previous.get('scopes', {}).items()
                    if key not in own_scopes}
    manifest['scopes'].update(other_scopes)
    complete_declared = declared | declared_artifacts({'scopes': other_scopes})
    for path in folder.rglob('*'):
        if (path.is_file() and is_summary_artifact(path)
                and str(path.relative_to(folder)) not in complete_declared):
            path.unlink()
    # Diagnostic PNGs are transient companions, not published summary files.
    for path in folder.rglob('*.png'):
        if is_summary_artifact(path):
            path.unlink()
    manifest['sources'] = previous.get('sources', {})
    manifest['sources'][source] = {
        'input_sha256': active['fingerprint'] if active else input_fingerprint(repo, source, mode),
        'scopes': sorted(new_scopes),
        'artifacts': sorted(declared),
        'metadata': own_metadata,
    }
    manifest['include_subexposures'] = mode
    for key in ('canonical_track', 'combined_build_id', 'combined_manifest_sha256'):
        if key not in manifest and key in previous:
            manifest[key] = previous[key]
    manifest['artifacts'] = {name: entry for name, entry in previous.get('artifacts', {}).items()
                             if name in declared_artifacts({'scopes': other_scopes})}
    manifest['artifacts'].update(new_artifacts)
    return manifest


def read_manifest(path):
    try:
        value = json.loads(Path(path).read_text())
        if (not isinstance(value, dict) or not isinstance(value.get('scopes'), dict)
                or not isinstance(value.get('sources'), dict)
                or not isinstance(value.get('artifacts'), dict)):
            return {}
        if any(not isinstance(item, dict) for item in value['scopes'].values()):
            return {}
        for item in value['sources'].values():
            if (not isinstance(item, dict) or not isinstance(item.get('scopes'), list)
                    or not isinstance(item.get('artifacts'), list)):
                return {}
            if any(not isinstance(name, str) for name in item['scopes'] + item['artifacts']):
                return {}
        declared_artifacts(value)
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def _current(folder, fingerprint, source):
    try:
        manifest = json.loads((folder / 'summary_manifest.json').read_text())
        provenance = manifest.get('sources', {}).get(source, {})
        own_scopes = {key: manifest['scopes'][key] for key in provenance.get('scopes', [])}
        expected = declared_artifacts({'scopes': own_scopes})
        return (provenance.get('input_sha256') == fingerprint
                and 'scopes' in provenance and 'artifacts' in provenance
                and set(provenance.get('artifacts', [])) == expected
                and all((folder / path).is_file()
                        and sha(folder / path) == manifest['artifacts'][path]['sha256']
                        for path in expected))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


@contextmanager
def _build_lock(folder):
    if os.environ.get('METAURO_SUMMARY_LOCK_HELD') == str(folder):
        yield
        return
    locks = folder.parent / '.locks'
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / (folder.name + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_summary_build(source_root, cache_root, plot_root, languages, builder):
    """Build inside summary/ and replace a complete mode under a stable lock."""
    repo = metauro_root(source_root)
    if repo is None:
        return builder(cache_root, plot_root, languages)
    cache, folder, mode = resolve_build_paths(source_root, cache_root, plot_root)
    source = Path(source_root).name
    with _build_lock(folder):
        fingerprint = input_fingerprint(repo, source, mode)
        stage = Path(tempfile.mkdtemp(prefix=f'.building-{mode}-', dir=repo / 'summary')).resolve()
        previous = repo / 'summary' / (f'.previous-{mode}-' + uuid.uuid4().hex)
        token = _ACTIVE_BUILD.set({'cache': cache, 'stage': stage, 'mode': mode,
                                   'fingerprint': fingerprint})
        try:
            if folder.exists():
                shutil.copytree(folder, stage, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('.build.lock'))
            manifest = builder(cache, stage, languages)
            if (input_fingerprint(repo, source, mode) != fingerprint
                    or not _current(stage, fingerprint, source)):
                raise RuntimeError('Summary inputs changed during generation or outputs failed verification')
            if folder.exists():
                folder.rename(previous)
            try:
                stage.rename(folder)
            except BaseException:
                if previous.exists():
                    previous.rename(folder)
                raise
            if previous.exists():
                shutil.rmtree(previous)
            return manifest
        finally:
            _ACTIVE_BUILD.reset(token)
            if stage.exists():
                shutil.rmtree(stage)


def ensure_current(repo_root, source, mode='yes', *, force=False):
    from automation.sync_sources import assembled_source
    repo = Path(repo_root).resolve()
    mode = summary_mode(mode)
    folder = summary_dir(repo, source, mode)
    with _build_lock(folder):
        fingerprint = input_fingerprint(repo, source, mode)
        if not force and _current(folder, fingerprint, source):
            return folder
        command = [sys.executable, '-B', str(repo / 'sources' / source / 'scripts/build_summary_plots.py'),
                   '--cache-root', str(cache_dir(repo, source, mode)), '--plot-root', str(folder)]
        environment = dict(os.environ, MPLBACKEND='Agg', PYTHONDONTWRITEBYTECODE='1',
                           METAURO_SUMMARY_LOCK_HELD=str(folder),
                           METAURO_SUMMARY_SOURCE_ROOT=str(assembled_source(repo, source)))
        subprocess.run(command, cwd=repo, env=environment, check=True)
        if not _current(folder, fingerprint, source):
            raise RuntimeError('Summary inputs changed during generation or outputs failed verification')
    return folder


def refresh_all(repo_root, *, force=False):
    return [str(ensure_current(repo_root, source, mode, force=force))
            for source in SOURCES for mode in MODES]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--source', choices=SOURCES)
    parser.add_argument('--mode', choices=MODES)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    for source in (args.source,) if args.source else SOURCES:
        for mode in (args.mode,) if args.mode else MODES:
            print(ensure_current(args.repo_root, source, mode, force=args.force), flush=True)


if __name__ == '__main__':
    main()
