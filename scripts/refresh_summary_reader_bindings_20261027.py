"""Rebind unchanged saved summaries after the reviewed interactive UI releases.

This one-time maintenance operation reconstructs the published fingerprint with
the original producer bytes before accepting any new binding. It never pools
evidence or renders assets, and refuses unreviewed code or changed inputs.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

BASELINE = '95ae46a539737b0bf1e746f4a8985d957065be58'
INTRODUCED_INTERACTIVE = {'sample_size_summary_native.py', 'sample_size_planning.py'}
APPROVED_READERS = {
    'app.py': '29522ad19fbb4b5b5e2e987a249bccac293225ccfe236c6c7e6e7655faf6ef67',
    'source_worker.py': 'b3fd279e04fbb6c78d4109f6eb0b0deafeed84e6f54c9a3338e662d2ccf63e49',
    'summary_outputs.py': '603514b9d4e2a7f09ba0cf3c37ec010964ba30414737f558c724f95ac37ff809',
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def replace_hashes(raw, replacements, expected):
    """Keep original JSON formatting and text escapes; replace approved hashes."""
    previous = json.loads(raw)

    def compare(before, after):
        if before == after:
            return
        if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
            for key in before:
                compare(before[key], after[key])
        elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
            for first, second in zip(before, after):
                compare(first, second)
        elif not isinstance(before, str) or replacements.get(before) != after:
            raise ValueError('Metadata changed beyond the explicitly reviewed hash fields')

    compare(previous, expected)
    for ascii_text in (True, False):
        original = (json.dumps(previous, ensure_ascii=ascii_text, indent=2) + '\n').encode()
        if original == raw:
            return (json.dumps(expected, ensure_ascii=ascii_text, indent=2) + '\n').encode()
    raise ValueError('Published JSON formatting needs a separate reviewed migration')


def old_bytes(root, relative):
    return subprocess.check_output(['git', '-C', str(root), 'show', BASELINE + ':' + relative])


def bound(folder, relative):
    path = (folder / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(folder.resolve()):
        raise ValueError('Asset escapes its published directory: ' + relative)
    return path


def verify_assets(folder, assets):
    """Validate every declared output before changing any release metadata."""
    for relative, entry in assets.items():
        path = bound(folder, relative)
        expected = entry['sha256'] if isinstance(entry, dict) else entry
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError('Published asset changed: ' + str(path))
        if isinstance(entry, dict) and 'bytes' in entry and path.stat().st_size != entry['bytes']:
            raise ValueError('Published asset size changed: ' + str(path))


def legacy_fingerprint(root, source, mode, summary):
    """Replay only reviewed code substitutions; all scientific bytes stay current."""
    replacements = {root / relative: digest(old_bytes(root, relative))
                    for relative in ('summary_outputs.py', 'runtime_code/manifest.json')}
    original_glob, original_sha = Path.glob, summary.sha

    def legacy_sha(path):
        return replacements.get(Path(path)) or original_sha(path)

    def legacy_glob(path, pattern, **kwargs):
        paths = original_glob(path, pattern, **kwargs)
        if path == root / 'runtime_code' and pattern == '*.py':
            return (item for item in paths if item.name not in INTRODUCED_INTERACTIVE)
        return paths

    with patch.object(summary, 'sha', legacy_sha), patch.object(Path, 'glob', legacy_glob):
        return summary.input_fingerprint(root, source, mode)


def validate_code(root):
    for relative, expected in APPROVED_READERS.items():
        if digest((root / relative).read_bytes()) != expected:
            raise ValueError('Reader code needs another review: ' + relative)
    baseline = old_bytes(root, 'summary_outputs.py')
    expected = baseline.replace(b"'METAURO_KEEP_LATEST_COHORT', 'no'",
                                b"'METAURO_KEEP_LATEST_COHORT', 'yes'")
    if (root / 'summary_outputs.py').read_bytes() != expected:
        raise ValueError('Offline summary generator changed beyond the reviewed request default')
    previous = json.loads(old_bytes(root, 'runtime_code/manifest.json'))
    current = json.loads((root / 'runtime_code/manifest.json').read_text())
    for name in INTRODUCED_INTERACTIVE:
        expected_hash = current['shared_files'].pop(name)['runtime_sha256']
        if digest((root / 'runtime_code' / name).read_bytes()) != expected_hash:
            raise ValueError('Interactive runtime manifest mismatch: ' + name)
    current['description'] = previous['description']
    if current != previous:
        raise ValueError('An existing scientific runtime binding changed')


def prepare_evidence_manifest(root, preprints):
    """Rebind only the reviewed worker code; leave recovered evidence intact."""
    path = preprints.preprint_manifest_path(root)
    original = path.read_bytes()
    document = json.loads(original)
    revised = deepcopy(document)
    worker_before = digest(old_bytes(root, 'source_worker.py'))
    worker_after = APPROVED_READERS['source_worker.py']
    changes = []
    for index, context in enumerate(document['contexts']):
        for relative, expected in context.get('baseline_files', {}).items():
            actual = digest((root / relative).read_bytes())
            if actual == expected:
                continue
            if relative != 'source_worker.py' or expected != worker_before or actual != worker_after:
                raise ValueError('Unreviewed recovery input binding changed: ' + relative)
            revised['contexts'][index]['baseline_files'][relative] = actual
            changes.append(context['context_id'])
    old_token = ('"source_worker.py": "' + worker_before + '"').encode()
    new_token = ('"source_worker.py": "' + worker_after + '"').encode()
    if original.count(old_token) != len(changes):
        raise ValueError('Recovery worker bindings need a different reviewed migration')
    prepared = original.replace(old_token, new_token)
    if json.loads(prepared) != revised:
        raise ValueError('Recovery evidence changed beyond worker code bindings')
    original_bound = preprints._bound_file

    def compatible_bound(repo, relative, expected):
        if relative == 'source_worker.py' and expected == worker_before:
            if digest((root / relative).read_bytes()) != worker_after:
                raise ValueError('Reviewed worker changed during validation')
            return root / relative
        return original_bound(repo, relative, expected)

    # Use native article gates, every other scientific source binding, and the
    # native-row-file checks before any file is changed.
    with patch.object(preprints, '_bound_file', compatible_bound):
        status = preprints.preprint_status(root)
    audit = dict(path=str(path.relative_to(root)), sha256_before=digest(original),
                 sha256_after=digest(prepared), source_worker_sha256_before=worker_before,
                 source_worker_sha256_after=worker_after, changed_code_contexts=changes,
                 scientific_evidence_payload_unchanged=True)
    return path, original, prepared, status, audit


def refresh(root, *, apply=False, audit_file=None):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    import summary_outputs as summary
    import preprint_ui as preprints

    validate_code(root)
    evidence_path, evidence_before, evidence_after, status, evidence_audit = prepare_evidence_manifest(root, preprints)
    if not status['available']:
        raise ValueError('Reviewed preprint evidence bindings are unavailable')
    fingerprints = {}
    for source in ('MetaVir', 'metaPee'):
        for mode in ('yes', 'no'):
            fingerprints[(source, mode)] = {
                'published': legacy_fingerprint(root, source, mode, summary),
                'current': summary.input_fingerprint(root, source, mode),
            }

    prepared, original = {evidence_path: evidence_after}, {evidence_path: evidence_before}
    manifests, seals = [], []
    for path in sorted((root / 'summary').glob('**/summary_manifest.json')):
        raw = path.read_bytes()
        document = json.loads(raw)
        verify_assets(path.parent, document['artifacts'])
        mode = path.relative_to(root / 'summary').parts[0]
        revised = deepcopy(document)
        replacements = {}
        if 'preprint_recovery_manifest_sha256' in revised:
            if revised['preprint_recovery_manifest_sha256'] != digest(evidence_before):
                raise ValueError('Published recovery manifest binding changed: ' + str(path))
            revised['preprint_recovery_manifest_sha256'] = digest(evidence_after)
            replacements[digest(evidence_before)] = digest(evidence_after)
        changes = {}
        for source, provenance in document['sources'].items():
            values = fingerprints[(source, mode)]
            if provenance['input_sha256'] != values['published']:
                raise ValueError('Current scientific inputs do not reproduce ' + str(path))
            revised['sources'][source]['input_sha256'] = values['current']
            replacements[values['published']] = values['current']
            changes[source] = values
        original[path] = raw
        prepared[path] = replace_hashes(raw, replacements, revised)
        manifests.append({'path': str(path.relative_to(root)), 'source_fingerprints': changes,
                          'sha256_before': digest(raw), 'sha256_after': digest(prepared[path]),
                          'scientific_artifact_count': len(document['artifacts']),
                          'artifact_map_unchanged': revised['artifacts'] == document['artifacts']})

    for path in sorted((root / 'summary').glob('**/preprint_view_seal.json')):
        raw = path.read_bytes()
        document = json.loads(raw)
        if document['preprint_manifest_sha256'] != preprints.preprint_manifest_stamp(root):
            raise ValueError('Preprint evidence manifest changed')
        verify_assets(path.parent, document['assets'])
        actual = {str(item.relative_to(path.parent)) for item in path.parent.rglob('*')
                  if item.is_file() and item.name != path.name}
        if actual != set(document['assets']):
            raise ValueError('The sealed asset inventory changed: ' + str(path))
        revised = deepcopy(document)
        revised['preprint_manifest_sha256'] = digest(evidence_after)
        replacements = {digest(evidence_before): digest(evidence_after)}
        for relative, expected in document['producing_code'].items():
            if digest(old_bytes(root, relative)) != expected:
                raise ValueError('The original producer is not the reviewed baseline: ' + relative)
            current = digest((root / relative).read_bytes())
            if current != expected and relative not in {*APPROVED_READERS, 'runtime_code/manifest.json'}:
                raise ValueError('Unreviewed producer change: ' + relative)
            revised['producing_code'][relative] = current
            replacements[expected] = current
        metadata = []
        for relative in document['assets']:
            asset = path.parent / relative
            if asset in prepared:
                if asset.name != 'summary_manifest.json':
                    raise ValueError('Only summary metadata hashes may change')
                revised['assets'][relative] = digest(prepared[asset])
                replacements[document['assets'][relative]] = revised['assets'][relative]
                metadata.append(relative)
        original[path] = raw
        prepared[path] = replace_hashes(raw, replacements, revised)
        seals.append({'path': str(path.relative_to(root)),
                      'producing_code_before': document['producing_code'],
                      'producing_code_after': revised['producing_code'],
                      'asset_count': len(document['assets']), 'metadata_digest_changes': metadata,
                      'asset_keys_unchanged': set(revised['assets']) == set(document['assets']),
                      'scientific_asset_digests_unchanged': all(
                          revised['assets'][name] == value for name, value in document['assets'].items()
                          if name not in metadata)})

    report = dict(schema_version=1, baseline_revision=BASELINE,
                  producing_helper=str(Path(__file__).resolve().relative_to(root)),
                  producing_helper_sha256=digest(Path(__file__).read_bytes()),
                  reason='Interactive defaults, sample-size APIs, and plot presentation only; offline pooling unchanged.',
                  checked_at=datetime.now(timezone.utc).isoformat(),
                  preprint_evidence=status, evidence_code_binding_repair=evidence_audit,
                  manifests=manifests, seals=seals,
                  all_published_scientific_fingerprints_reproduced=True,
                  mathematical_results_regenerated=False, applied=bool(apply))
    if not apply:
        return report
    if audit_file is None:
        raise ValueError('An applied binding repair requires a durable audit receipt')
    try:
        for path, raw in original.items():
            if path.read_bytes() != raw:
                raise ValueError('Release metadata changed while validating: ' + str(path))
        for path, raw in prepared.items():
            path.write_bytes(raw)
        after_status = preprints.preprint_status(root)
        if any(after_status[key] != status[key] for key in ('available', 'status', 'contexts', 'records')):
            raise ValueError('Recovered evidence availability or record count changed')
        report['preprint_evidence_after'] = after_status
        for item in manifests:
            path = root / item['path']
            document = json.loads(path.read_text())
            verify_assets(path.parent, document['artifacts'])
            for source, values in item['source_fingerprints'].items():
                mode = path.relative_to(root / 'summary').parts[0]
                if summary.input_fingerprint(root, source, mode) != values['current']:
                    raise ValueError('Scientific inputs changed during rebinding')
                if not summary._current(path.parent, values['current'], source):
                    raise ValueError('Rebound summary does not pass its native validity check')
        checked = 0
        for item in seals:
            path = root / item['path']
            document = json.loads(path.read_text())
            for relative in document['assets']:
                if not preprints.verify_preprint_summary_asset(path.parent / relative, root):
                    raise ValueError('Rebound asset does not pass its native seal check: ' + relative)
                checked += 1
        report['native_summary_checks_passed'] = True
        report['native_preprint_asset_checks_passed'] = checked
        destination = Path(audit_file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + '\n')
    except Exception:
        for path, raw in original.items():
            path.write_bytes(raw)
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--audit-file', type=Path)
    args = parser.parse_args()
    report = refresh(args.repo_root, apply=args.apply, audit_file=args.audit_file)
    print(json.dumps({key: value for key, value in report.items() if key not in {'manifests', 'seals'}}, indent=2))


if __name__ == '__main__':
    main()
