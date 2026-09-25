"""Install verified monthly UI data as additive overlays over immutable evidence."""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

SOURCES = ('metaPee', 'MetaVir')
ROOT = Path(__file__).resolve().parents[1]
ASSEMBLY_SCHEMA = 'central-summary-v1'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else deepcopy(default)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n'
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(raw)
    temporary.replace(path)


def bound(root, relative):
    part = Path(relative)
    path = (Path(root) / part).resolve()
    if part.is_absolute() or '..' in part.parts or not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Invalid monthly artifact path')
    return path


def allowed(relative):
    part = Path(relative)
    if any(token.startswith('.') or token in {'_history', 'sources'} for token in part.parts):
        return False
    if part.suffix.lower() not in {'.json', '.png', '.pdf', '.xlsx', '.csv', '.jpg', '.jpeg'}:
        return False
    return (part.parts[0] in {'Cached_results', 'Subexposure_results', 'Subexposure_views', 'Plot', 'static'}
            or relative.startswith('combined/provisional_ui/')
            or (part.parts[0] == 'combined' and len(part.parts) == 3 and '_true_' in part.name))


def legacy_summary_artifact(relative):
    """Return whether a source output belongs in metaUro's central summary tree.

    Source repositories still publish their historical ``Plot`` layouts.  We
    verify those bytes as part of the source release, but metaUro regenerates
    the public summary collection from the installed caches and must not retain
    a second workbook/plot copy in ``updates`` or an assembled runtime tree.
    """
    part = Path(relative)
    in_plot_tree = any(token.casefold() == 'plot' for token in part.parts[:-1])
    return in_plot_tree and (
        part.suffix.casefold() in {'.xlsx', '.pdf', '.png'}
        or part.name == 'summary_manifest.json'
    )


def _ignore_legacy_summary_artifacts(base):
    base = Path(base).resolve()

    def ignore(directory, names):
        root = Path(directory).resolve()
        return {
            name for name in names
            if legacy_summary_artifact((root / name).relative_to(base))
        }

    return ignore


def _check_rows(previous, incoming):
    """A UI overlay may extend a cache; it must retain previous study estimates."""
    if not isinstance(previous, dict) or not previous.get('studies'):
        return
    if not isinstance(incoming, dict) or not isinstance(incoming.get('studies'), list):
        raise ValueError('A monthly overlay would remove saved studies')
    def identity(row):
        return tuple(str(row.get(key)) for key in ('PMID', 'Record ID', 'Effect ID', 'Direct Exposure',
                     'Effect Type', 'Effect Size', 'Lower CI', 'Upper CI', 'Sample Size', 'Cases'))
    after = {identity(row) for row in incoming['studies']}
    if any(identity(row) not in after for row in previous['studies']):
        raise ValueError('A source update changes historical evidence; review it before importing')


def load_overlay(repo, source):
    index = read(Path(repo) / 'updates/manifest.json', {})
    entry = index.get('sources', {}).get(source)
    if not entry:
        return None
    path = bound(Path(repo) / 'updates', entry['manifest'])
    if sha(path) != entry['sha256']:
        raise ValueError('Monthly overlay manifest checksum mismatch')
    manifest = read(path)
    if manifest.get('source') != source:
        raise ValueError('Monthly overlay source mismatch')
    return manifest


def generation(repo, source):
    index = read(Path(repo) / 'updates/manifest.json', {})
    return index.get('sources', {}).get(source, {}).get('sha256', 'original')


def _effective_file(repo, source, relative, previous):
    entry = (previous or {}).get('files', {}).get(relative)
    if entry:
        if entry.get('deleted'):
            return None
        path = bound(Path(repo) / 'updates' / source, entry['stored_path'])
        if sha(path) != entry['sha256']:
            raise ValueError('Previously installed monthly artifact checksum mismatch')
        return path
    path = bound(Path(repo) / 'sources' / source, relative)
    return path if path.exists() else None


def _reuse_with_current_provenance(entry, previous, source_commit, run_id):
    """Reuse stored bytes while recording both their origin and latest release."""
    reused = deepcopy(entry)
    if reused.get('source_commit') == source_commit and reused.get('run_id') == run_id:
        return reused
    stored_commit = reused.get('stored_source_commit', reused.get('source_commit'))
    stored_run = reused.get('stored_run_id', reused.get('run_id'))
    if stored_commit is None:
        stored_commit = (previous or {}).get('source_commit')
    if stored_run is None:
        stored_run = (previous or {}).get('run_id')
    if stored_commit is not None:
        reused['stored_source_commit'] = stored_commit
    if stored_run is not None:
        reused['stored_run_id'] = stored_run
    reused.update(source_commit=source_commit, run_id=run_id)
    return reused


def _cumulative_outputs(source_root, manifest, manifest_path, seen=None):
    seen = set() if seen is None else seen
    run_id = manifest.get('run_id')
    if run_id in seen:
        raise ValueError('Monthly source lineage contains a cycle')
    seen.add(run_id)
    outputs = {}
    prior = manifest.get('watermark_source', {})
    if prior.get('kind') == 'successful_release':
        prior_path = bound(source_root, 'data/monthly_updates/' + prior['run_id'] + '/' + manifest_path.name)
        if sha(prior_path) != prior.get('manifest_sha256'):
            raise ValueError('Previous monthly release checksum mismatch')
        previous = read(prior_path)
        if previous.get('status') != 'completed' or previous.get('run_id') != prior['run_id']:
            raise ValueError('Previous monthly release is not completed')
        outputs.update(_cumulative_outputs(source_root, previous, prior_path, seen))
    ui = manifest.get('ui_refresh', {})
    if ui.get('status') not in {'refreshed', 'already_current', 'no_new_accepted_rows'}:
        raise ValueError('UI refresh is incomplete')
    if not isinstance(ui.get('output_sha256'), dict):
        raise ValueError('UI artifact checksums are absent')
    outputs.update(ui['output_sha256'])
    return outputs


def verify_release(source_root, source):
    state = read(source_root / 'data/monthly_automation/state.json', {})
    if state.get('status') != 'completed':
        raise ValueError(f'{source}: a completed monthly source release is required')
    if state.get('repository') != source:
        raise ValueError(f'{source}: source state repository identity mismatch')
    manifest_path = bound(source_root, state['manifest_path'])
    if sha(manifest_path) != state.get('manifest_sha256'):
        raise ValueError(f'{source}: release manifest checksum mismatch')
    manifest = read(manifest_path)
    if manifest.get('status') != 'completed' or manifest.get('run_id') != state.get('run_id'):
        raise ValueError(f'{source}: incomplete or mismatched release')
    if manifest.get('repository') != source:
        raise ValueError(f'{source}: release repository identity mismatch')
    if manifest.get('end_date') != state.get('last_successful_publication_date'):
        raise ValueError(f'{source}: search cutoff differs from the release')
    run = manifest_path.parent
    raw = manifest.get('raw_packet_sha256')
    required_raw = {'candidates.json', 'final_screening.json'}
    if not isinstance(raw, dict) or not required_raw.issubset(raw):
        raise ValueError(f'{source}: release lacks required screening packet receipts')
    for relative, digest in raw.items():
        if sha(bound(run, relative)) != digest:
            raise ValueError(f'{source}: screening packet checksum mismatch')
    reviews = manifest.get('review_artifacts_sha256', {})
    if not reviews:
        raise ValueError(f'{source}: no verified human-review artifacts')
    for relative, digest in reviews.items():
        if sha(bound(source_root, relative)) != digest:
            raise ValueError(f'{source}: human-review checksum mismatch')
    gate = run / 'monthly_application_gate.json'
    if sha(gate) != manifest.get('application_gate_sha256'):
        raise ValueError(f'{source}: application gate checksum mismatch')
    outputs = _cumulative_outputs(source_root, manifest, manifest_path)
    for relative, digest in outputs.items():
        path = bound(source_root, relative)
        if (digest is None and path.exists()) or (digest is not None and sha(path) != digest):
            raise ValueError(f'{source}: UI artifact checksum mismatch')
    commit = subprocess.check_output(['git', '-C', str(source_root), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(source_root), 'status', '--porcelain'], text=True).strip()
    if dirty:
        raise ValueError(f'{source}: source checkout must match its committed release')
    return state, manifest, outputs, reviews, commit


def sync_sources(repo_root, repos_root, require_through=None):
    repo, siblings = Path(repo_root).resolve(), Path(repos_root).resolve()
    # Validate both cohorts completely before publishing either pointer.
    releases = {source: verify_release(siblings / source, source) for source in SOURCES}
    cutoffs = {entry[0]['last_successful_publication_date'] for entry in releases.values()}
    if len(cutoffs) != 1 or (require_through is not None and cutoffs != {require_through}):
        raise ValueError('Both source releases must complete the same required publication cutoff')
    index = read(repo / 'updates/manifest.json', {'schema_version': 1, 'sources': {}})
    prepared = {}
    for source, (state, release, outputs, reviews, commit) in releases.items():
        previous = load_overlay(repo, source)
        if previous and state['last_successful_publication_date'] < previous['search_through']:
            raise ValueError('A monthly source release would rewind the search cutoff')
        files = {
            relative: deepcopy(entry)
            for relative, entry in (previous or {}).get('files', {}).items()
            if not legacy_summary_artifact(relative)
        }
        run_id = state['run_id']
        version_id = run_id + '_' + state['manifest_sha256'][:16]
        source_root = siblings / source
        target = repo / 'updates' / source
        for relative, digest in outputs.items():
            if legacy_summary_artifact(relative):
                continue
            if not allowed(relative):
                raise ValueError(f'Unapproved source output type: {relative}')
            old = _effective_file(repo, source, relative, previous)
            incoming = bound(source_root, relative)
            if digest is None:
                if old and old.suffix == '.json' and read(old, {}).get('studies'):
                    raise ValueError('A monthly overlay cannot delete saved study evidence')
                files[relative] = {'deleted': True, 'previous_sha256': sha(old) if old else None,
                                   'source_commit': commit, 'run_id': run_id}
                continue
            if old and relative.endswith('.json'):
                _check_rows(read(old), read(incoming))
            original = bound(repo / 'sources' / source, relative)
            original_sha = sha(original) if original.exists() else None
            if digest == original_sha:
                files.pop(relative, None)
                continue
            previous_entry = (previous or {}).get('files', {}).get(relative)
            if (previous_entry and not previous_entry.get('deleted')
                    and previous_entry.get('sha256') == digest):
                reused = _reuse_with_current_provenance(previous_entry, previous, commit, run_id)
                reused['original_sha256'] = original_sha
                files[relative] = reused
                continue
            stored = f'versions/{version_id}/files/{relative}'
            destination = bound(target, stored)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(incoming, destination)
            files[relative] = {'sha256': digest, 'original_sha256': original_sha,
                               'stored_path': stored, 'source_commit': commit, 'run_id': run_id}
        review_files = []
        previous_reviews = {
            (entry.get('filename'), entry.get('sha256')): entry
            for entry in (previous or {}).get('review_files', [])
            if isinstance(entry, dict)
        }
        for relative, digest in reviews.items():
            if Path(relative).suffix != '.csv':
                continue
            name = Path(relative).name
            previous_entry = previous_reviews.get((name, digest))
            if previous_entry:
                previous_path = bound(target, previous_entry['stored_path'])
                if not previous_path.is_file() or sha(previous_path) != digest:
                    raise ValueError('Previously installed human-review artifact checksum mismatch')
                review_files.append(_reuse_with_current_provenance(
                    previous_entry, previous, commit, run_id))
                continue
            stored = f'versions/{version_id}/review/{name}'
            path = bound(target, stored)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bound(source_root, relative), path)
            review_files.append({'filename': name, 'stored_path': stored, 'sha256': digest,
                                 'source_commit': commit, 'run_id': run_id})
        manifest = {'schema_version': 1, 'source': source, 'run_id': run_id, 'source_commit': commit,
                    'source_manifest_sha256': state['manifest_sha256'],
                    'search_through': state['last_successful_publication_date'],
                    'completed_at': state.get('completed_at'),
                    'human_review_pending': state.get('human_review_pending', 0),
                    'files': files, 'review_files': review_files,
                    'prisma_scope': 'Historical flow counts remain unchanged; monthly updates are reported separately.'}
        # Version the manifest too: switching the shared pointer is the sole
        # publication step and cannot expose one half of a two-source refresh.
        manifest_name = f'{source}/versions/{version_id}/manifest.json'
        manifest_path = bound(repo / 'updates', manifest_name)
        write(manifest_path, manifest)
        prepared[source] = {'manifest': manifest_name, 'sha256': sha(manifest_path)}
    index['sources'].update(prepared)
    write(repo / 'updates/manifest.json', index)
    return index


def assembled_source(repo_root, source):
    """Make a read-only runtime view without ever modifying a base hardlink."""
    repo = Path(repo_root).resolve()
    if source not in SOURCES:
        raise ValueError('Unknown source')
    manifest = load_overlay(repo, source)
    base = repo / 'sources' / source
    if not manifest:
        return base
    folder = repo / 'runtime' / 'assembled' / generation(repo, source) / source
    code_inputs = sorted(set(base.glob('*.py')) | set((base / 'scripts').glob('*.py'))
                         | set((base / 'static').rglob('*.json'))
                         | set((base / 'data').glob('*.json')) | set(base.glob('*.csv')))
    code_digest = hashlib.sha256(''.join(sha(path) for path in code_inputs).encode()).hexdigest()
    completion = generation(repo, source) + '\n' + ASSEMBLY_SCHEMA + '\n' + code_digest + '\n'
    if (folder / '.complete').is_file() and (folder / '.complete').read_text() == completion:
        return folder
    if folder.exists():
        shutil.rmtree(folder)
    folder.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='assembling-', dir=folder.parent))
    try:
        def link(original, target):
            try:
                return os.link(original, target)
            except OSError:
                return shutil.copyfile(original, target)
        shutil.copytree(
            base,
            temporary,
            dirs_exist_ok=True,
            copy_function=link,
            ignore=_ignore_legacy_summary_artifacts(base),
        )
        for relative, entry in manifest['files'].items():
            if legacy_summary_artifact(relative):
                continue
            if not allowed(relative):
                raise ValueError('Unexpected runtime overlay path')
            target = bound(temporary, relative)
            if target.exists():
                target.unlink()  # Unlink before replacement: the base inode is immutable.
            if entry.get('deleted'):
                continue
            source_file = bound(repo / 'updates' / source, entry['stored_path'])
            if sha(source_file) != entry['sha256']:
                raise ValueError('Runtime overlay artifact checksum mismatch')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target)
        (temporary / '.complete').write_text(completion)
        try:
            temporary.rename(folder)
        except FileExistsError:
            shutil.rmtree(temporary)
        return folder
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=ROOT)
    parser.add_argument('--repos-root', type=Path, required=True)
    parser.add_argument('--require-through', help='Require both sources to finish this YYYY-MM-DD publication cutoff')
    args = parser.parse_args()
    print(json.dumps(sync_sources(args.repo_root, args.repos_root, args.require_through), indent=2))
