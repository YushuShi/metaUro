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
from preprint_ui import preprint_variant_root,preprint_manifest_stamp,preprint_status,preprint_manifest_path,applicable_preprint_contexts,verify_preprint_summary_asset

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


def latest_cohort_mode(value=None):
    value = os.environ.get('METAURO_KEEP_LATEST_COHORT', 'yes') if value is None else value
    text = str(value).strip().lower()
    if text not in {'yes','true','1','no','false','0',''}:
        raise ValueError('latest_cohort must be yes or no')
    return text in {'yes','true','1'}


def summary_dir(repo_root, source, mode, keep_latest_cohort=None,include_preprints=None):
    if source not in SOURCES:
        raise ValueError('Unknown summary source')
    folder = Path(repo_root).resolve() / 'summary' / summary_mode(mode)
    latest = latest_cohort_mode() if keep_latest_cohort is None else bool(keep_latest_cohort)
    folder=folder/'cohort_latest' if latest else folder
    if include_preprints is None:include_preprints=os.environ.get('METAURO_INCLUDE_PREPRINTS','no')=='yes'
    return preprint_variant_root(folder,include_preprints)


def summary_view_dir(repo_root,source,mode,keep_latest_cohort=False,include_preprints=False):
    if include_preprints and preprint_status(repo_root)['available'] and not applicable_preprint_contexts(repo_root,{'source_repo':source,'include_subexposures':summary_mode(mode)}):
        include_preprints=False
    return summary_dir(repo_root,source,mode,keep_latest_cohort,include_preprints)


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
    inputs.add(repo / 'cohort_identity.py')
    inputs.add(repo / 'cohort_summary_support.py')
    if (repo / 'data/cohort_record_metadata.json').exists(): inputs.add(repo / 'data/cohort_record_metadata.json')
    if (repo / 'data/cohort_registry.json').exists(): inputs.add(repo / 'data/cohort_registry.json')
    inputs.add(repo / 'scripts/build_cohort_summaries.py')
    inputs.add(repo / 'display_localization.py')
    if source == 'MetaVir':
        inputs.add(repo / 'prostate_scopes.py')
    inputs.update((repo / 'runtime_code').glob('*.py'))
    inputs.add(repo / 'runtime_code/manifest.json')
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


def _cohort_audit_groups(manifest):
    """Recover source ownership without discarding legacy audit records."""
    groups = {source: list(audits) for source, audits in
              manifest.get('cohort_selection_audits_by_source', {}).items() if isinstance(audits, list)}
    history = list(manifest.get('cohort_selection_audit_history', []))
    for source, info in manifest.get('sources', {}).items():
        audits = info.get('metadata', {}).get('cohort_selection_audits')
        if isinstance(audits, list):
            if source not in groups:
                groups[source] = list(audits)
            elif groups[source] != audits:
                entry = {'source': source, 'provenance': 'legacy_source_metadata',
                         'reason': 'Superseded by explicit source audit mapping', 'audits': audits}
                if entry not in history:
                    history.append(entry)
    established_sources = set(groups)
    covered = [audit for audits in groups.values() for audit in audits]
    unassigned = []
    for audit in manifest.get('cohort_selection_audits', []):
        if audit in covered:
            covered.remove(audit)
            continue
        disease = str(audit.get('disease', ''))
        owners = []
        for source, info in manifest.get('sources', {}).items():
            labels = {str(manifest.get('scopes', {}).get(scope, {}).get('label', scope))
                      for scope in info.get('scopes', [])}
            if disease and disease in labels:
                owners.append(source)
        if len(owners) == 1 and owners[0] not in established_sources:
            groups.setdefault(owners[0], []).append(audit)
        else:
            unassigned.append(audit)
    if unassigned:
        entry = {'provenance': 'legacy_flat_cohort_selection_audits',
                 'reason': 'Source ownership or current source-audit generation could not be established uniquely',
                 'audits': unassigned}
        if entry not in history:
            history.append(entry)
    return groups, history


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
    audit_groups, audit_history = _cohort_audit_groups(previous)
    incoming_audits = manifest.get('cohort_selection_audits_by_source', {}).get(source, manifest.get('cohort_selection_audits'))
    if isinstance(incoming_audits, list):
        if source in audit_groups and audit_groups[source] != incoming_audits:
            entry = {'source': source, 'provenance': 'previous_source_audit',
                     'reason': 'Superseded by this source refresh', 'audits': audit_groups[source]}
            if entry not in audit_history:
                audit_history.append(entry)
        audit_groups[source] = list(incoming_audits)
    if audit_groups:
        manifest['cohort_selection_audits_by_source'] = audit_groups
        manifest['cohort_selection_audits'] = [audit for owner in sorted(audit_groups) for audit in audit_groups[owner]]
    if audit_history:
        manifest['cohort_selection_audit_history'] = audit_history
    new_scopes = set(manifest['scopes'])
    own_scopes = set(previous_source.get('scopes', [])) | new_scopes
    other_scopes = {key: value for key, value in previous.get('scopes', {}).items()
                    if key not in own_scopes}
    manifest['scopes'].update(other_scopes)
    complete_declared = declared | declared_artifacts({'scopes': other_scopes})
    for path in folder.rglob('*'):
        # The optional cohort view is a complete publication of its own. A
        # base-mode rebuild must not clean its nested assets against base names.
        if path.relative_to(folder).parts[0] in {'cohort_latest','preprints_included'}:
            continue
        if (path.is_file() and is_summary_artifact(path)
                and str(path.relative_to(folder)) not in complete_declared):
            path.unlink()
    # Diagnostic PNGs are transient companions, not published summary files.
    for path in folder.rglob('*.png'):
        if path.relative_to(folder).parts[0] in {'cohort_latest','preprints_included'}:
            continue
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
    # Base and latest publications share a mode tree; serializing them also
    # prevents a base stage from replacing a concurrently refreshed variant.
    scope = folder.parent if folder.name == 'cohort_latest' else folder
    if os.environ.get('METAURO_SUMMARY_LOCK_HELD') in {str(folder), str(scope)}:
        yield
        return
    locks = scope.parent / '.locks'
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / (scope.name + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_summary_build(source_root, cache_root, plot_root, languages, builder):
    """Build inside summary/ and replace a complete mode under a stable lock."""
    repo = metauro_root(source_root)
    if repo is not None:
        from runtime_code.trend_ci_runtime import install_core, install_completion
        if 'meta_analysis' in sys.modules:
            install_core(sys.modules['meta_analysis'])
        install_completion()
    if repo is None:
        return builder(cache_root, plot_root, languages)
    from display_localization import install_plot_localization
    plot_localization = sys.modules.get("scripts.plot_localization")
    if plot_localization is not None:
        install_plot_localization(plot_localization)
    if source_root.name == 'MetaVir' and plot_localization is not None:
        from prostate_scopes import install_plotting
        renderer = sys.modules.get(builder.__module__)
        if renderer is not None:
            install_plotting(renderer, plot_localization)
    from runtime_code.summary_pi_style import install as install_summary_pi
    renderer = sys.modules.get(builder.__module__)
    if renderer is not None and hasattr(renderer, '_draw_bounded_interval'):
        install_summary_pi(renderer)
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


def ensure_current(repo_root, source, mode='yes', *, force=False, keep_latest_cohort=False,include_preprints=False):
    from automation.sync_sources import assembled_source
    repo = Path(repo_root).resolve()
    mode = summary_mode(mode)
    folder = summary_dir(repo, source, mode, keep_latest_cohort,include_preprints)
    if include_preprints:
        status=preprint_status(repo)
        if not status['available']:raise ValueError('Reviewed-preprint summary release is unavailable.')
        recovery=json.loads(preprint_manifest_path(repo).read_text())
        relevant=applicable_preprint_contexts(repo,{'source_repo':source,'include_subexposures':mode})
        if not relevant:
            return ensure_current(repo,source,mode,force=force,keep_latest_cohort=keep_latest_cohort)
        manifest=read_manifest(folder/'summary_manifest.json')
        if not verify_preprint_summary_asset(folder/'summary_manifest.json',repo) or not _current(folder,input_fingerprint(repo,source,mode),source):
            raise ValueError('Reviewed-preprint summary assets need an offline release for this source and mode.')
        return folder
    with _build_lock(folder):
        fingerprint = input_fingerprint(repo, source, mode)
        if not force and _current(folder, fingerprint, source):
            return folder
        command = [sys.executable, '-B', str(repo / 'sources' / source / 'scripts/build_summary_plots.py'),
                   '--cache-root', str(cache_dir(repo, source, mode)), '--plot-root', str(folder)]
        if keep_latest_cohort:
            command = [sys.executable, '-B', str(repo / 'scripts/build_cohort_summaries.py'),
                       '--source', source, '--mode', mode]
        environment = dict(os.environ,METAURO_INCLUDE_PREPRINTS='no', METAURO_KEEP_LATEST_COHORT='yes' if keep_latest_cohort else 'no', MPLBACKEND='Agg' , PYTHONDONTWRITEBYTECODE='1',
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
