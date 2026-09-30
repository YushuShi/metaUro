"""Append source-reviewed estimates over immutable baseline snapshots.

This is a reviewed scientific revision, not a new monthly search. Preserve
historical metaUro rows and rebuild affected central summary outputs.
"""
from __future__ import annotations
import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from automation.sync_sources import (allowed, assembled_source, legacy_summary_artifact,
    load_overlay, read, sha, write)
import summary_outputs as summaries

VERSION = 'multicancer-2026-09-29'
AUDIT = ROOT / 'evidence-revisions' / VERSION
SOURCES = ('MetaVir', 'metaPee')
REVIEW = 'data/multicancer_review_2026-09-29'
SCOPES = {'MetaVir': {'prostate_cancer'}, 'metaPee': {
    'bladder_cancer', 'renal_cell_carcinoma', 'kidney_and_renal_pelvis_cancer'}}


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def immutable_hashes():
    manifest = read(ROOT / 'evidence-manifest.json')
    paths = [ROOT / entry['path'] for entry in manifest['files']]
    return {str(p.relative_to(ROOT)): sha(p) for p in paths if p.is_file()} | {
        'evidence-manifest.json': sha(ROOT / 'evidence-manifest.json')}


def prepare():
    if (AUDIT / 'source_release.json').exists():
        raise ValueError('Review already prepared; use publish or summaries next.')
    write(AUDIT/'previous_overlay_index.json', read(ROOT/'updates/manifest.json'))
    protected = immutable_hashes()
    releases = {}
    conservation = []
    pending = []
    for source in SOURCES:
        repo = ROOT.parent / source
        if git(repo, 'status', '--porcelain'):
            raise ValueError('Commit the complete source release first: ' + source)
        commit = git(repo, 'rev-parse', 'HEAD')
        release_path = repo / REVIEW / 'release_manifest.json'
        release = read(release_path)
        registry = read(repo / REVIEW / 'registry.json')
        registered = {entry['id']: entry for entry in registry['entries']}
        assert release['status'] == 'completed' and release['version'] == VERSION
        for relative, expected in release['artifact_sha256'].items():
            assert sha(repo / relative) == expected, (source, relative)
        previous = assembled_source(ROOT, source)
        files = {}
        for relative, expected in release['artifact_sha256'].items():
            if not allowed(relative) or legacy_summary_artifact(relative) or not relative.endswith('.json'):
                continue
            incoming = repo / relative
            before = previous / relative
            old, new = read(before, {}), read(incoming)
            if not isinstance(new, dict):
                continue
            if isinstance(new.get('studies'), list):
                additions = [copy.deepcopy(r) for r in new['studies'] if r.get('source_review_id') in registered]
                if not additions:
                    continue
                result = copy.deepcopy(old or new)
                result['studies'] = copy.deepcopy(old.get('studies', []))
                ids = {r.get('Effect ID') for r in result['studies']}
                for row in additions:
                    if row['Effect ID'] not in ids:
                        result['studies'].append(row)
                        ids.add(row['Effect ID'])
                if result['studies'] == old.get('studies'):
                    continue
                conservation.append({'source': source, 'path': relative,
                    'historical_rows_exactly_preserved': len(old.get('studies', [])),
                    'added_reviewed_rows': len(result['studies'])-len(old.get('studies', [])),
                    'before_sha256': sha(before) if before.exists() else None})
                assert result['studies'][:len(old.get('studies', []))] == old.get('studies', [])
            elif isinstance(new.get('results'), list):
                result = copy.deepcopy(old or new)
                result['results'] = copy.deepcopy(old.get('results', []))
                ids = {r.get('context_id') for r in result['results']}
                result['results'].extend(copy.deepcopy(e['record']) for e in registered.values() if e['id'] not in ids)
                for field in ('counts', 'status_counts'):
                    if field in result:
                        result[field] = dict(Counter(r.get('final_status') for r in result['results']))
                assert result['results'][:len(old.get('results', []))] == old.get('results', [])
            else:
                continue
            result['evidence_revision'] = VERSION
            result['evidence_revision_registry_sha256'] = sha(repo / REVIEW / 'registry.json')
            result['source_review_application'] = 'Append reviewed contexts to the existing metaUro evidence; historical source differences are not imported.'
            if source == 'MetaVir' and isinstance(result.get('studies'), list):
                result.setdefault('combined_projection', {})['build_id'] = read(previous/'combined/_merge_manifest.json')['build_id']
            stored = 'versions/' + VERSION + '/files/' + relative
            baseline = ROOT / 'sources' / source / relative
            files[relative] = {'sha256': None, 'original_sha256': sha(baseline) if baseline.is_file() else None,
                'stored_path': stored, 'source_commit': commit, 'run_id': VERSION}
            pending.append((result, ROOT / 'updates' / source / stored))
        releases[source] = {'commit': commit, 'release_manifest_sha256': sha(release_path),
            'source_release': release, 'overlay_files': files}
    # Validate all scientific rows before creating any overlay artifact.
    for result, target in pending:
        write(target, result)
    for source, release in releases.items():
        for entry in release['overlay_files'].values():
            entry['sha256'] = sha(ROOT / 'updates' / source / entry['stored_path'])
    for source in SOURCES:
        folder = AUDIT / source
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('registry.json', 'release_manifest.json', 'conservation.json',
                     'validation.json', 'api_verification.json', 'workbook_preservation.json',
                     'workbook_verification.json', 'pmid29463332_jbi.json', 'README.md'):
            shutil.copyfile(ROOT.parent / source / REVIEW / name, folder / name)
    write(AUDIT / 'immutable_before.json', protected)
    write(AUDIT / 'conservation.json', conservation)
    write(AUDIT / 'source_release.json', releases)
    print('Prepared', len(pending), 'overlay files;', len(conservation), 'historical cache checks', flush=True)


def publish():
    protected = read(AUDIT / 'immutable_before.json')
    assert all(sha(ROOT / name) == digest for name, digest in protected.items())
    index = read(ROOT / 'updates/manifest.json', {'schema_version': 1, 'sources': {}})
    for source, release in read(AUDIT / 'source_release.json').items():
        old = load_overlay(ROOT, source) or {}
        files = copy.deepcopy(old.get('files', {}))
        for relative, entry in release['overlay_files'].items():
            assert sha(ROOT / 'updates' / source / entry['stored_path']) == entry['sha256']
            files[relative] = entry
        manifest = {'schema_version': 1, 'kind': 'source_review_enrichment', 'source': source,
            'run_id': VERSION, 'source_commit': release['commit'], 'files': files,
            'review_files': old.get('review_files', []), 'new_search_performed': False,
            'new_screening_performed': False, 'search_through': old.get('search_through'),
            'human_review_pending': old.get('human_review_pending', 0),
            'audit': str((AUDIT / 'source_release.json').relative_to(ROOT)),
            'audit_sha256': sha(AUDIT / 'source_release.json')}
        path = ROOT / 'updates' / source / 'versions' / VERSION / 'manifest.json'
        write(path, manifest)
        index['sources'][source] = {'manifest': str(path.relative_to(ROOT / 'updates')), 'sha256': sha(path)}
    write(ROOT / 'updates/manifest.json', index)
    print('Published audited evidence overlay', flush=True)


def publish_summaries():
    for source in SOURCES:
        subprocess.run([sys.executable, str(ROOT/'automation/refresh_multicancer_summaries.py'), '--source', source], check=True, cwd=ROOT)
    protected=read(AUDIT/'immutable_before.json')
    assert all(sha(ROOT/name)==digest for name,digest in protected.items())
    write(AUDIT/'immutable_after.json', {'unchanged_files':len(protected), 'before_manifest_sha256':sha(AUDIT/'immutable_before.json'), 'all_hashes_unchanged':True})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'publish', 'summaries'))
    action = parser.parse_args().action
    {'prepare': prepare, 'publish': publish, 'summaries': publish_summaries}[action]()
