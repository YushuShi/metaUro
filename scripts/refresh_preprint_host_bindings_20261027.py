"""Rebind the reviewed deployment-path fix without regenerating saved results.

The deployed directory can be /app. Only layout detection in preprint_ui.py
changes; all scientific inputs, source fingerprints, and output bytes must
match the previously published release before any metadata can be updated.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

BASELINE = 'd58094ef5d727a22e1f75d4222f5229e176fb825'
OLD_RESOLVER = b"    folder = 'updates' if root.name == 'metaUro' else 'data'\n"
NEW_RESOLVER = (
    b"    uro_layout = (root / 'source_worker.py').is_file() and (root / 'evidence-manifest.json').is_file()\n"
    b"    folder = 'updates' if root.name == 'metaUro' or uro_layout else 'data'\n"
)
spec = importlib.util.spec_from_file_location('reader_binding_checks',
    Path(__file__).with_name('refresh_summary_reader_bindings_20261027.py'))
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def old_bytes(root, relative):
    return subprocess.check_output(['git', '-C', str(root), 'show', BASELINE + ':' + relative])


def reviewed_reader_transition(root):
    before = old_bytes(root, 'preprint_ui.py')
    after = (root / 'preprint_ui.py').read_bytes()
    if before.count(OLD_RESOLVER) != 1 or after != before.replace(OLD_RESOLVER, NEW_RESOLVER):
        raise ValueError('Reader changed beyond the reviewed deployment layout detection')
    return checks.digest(before), checks.digest(after)


def refresh(root, *, apply=False, audit_file=None):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    import preprint_ui as preprints
    import summary_outputs as summaries

    old_code, new_code = reviewed_reader_transition(root)
    evidence_path = preprints.preprint_manifest_path(root)
    evidence_before = evidence_path.read_bytes()
    if evidence_before != old_bytes(root, str(evidence_path.relative_to(root))):
        raise ValueError('Recovery evidence changed since the reviewed release')
    evidence = json.loads(evidence_before)
    revised = deepcopy(evidence)
    changed_contexts = []
    for index, context in enumerate(evidence['contexts']):
        for relative, expected in context.get('baseline_files', {}).items():
            actual = checks.digest((root / relative).read_bytes())
            if relative == 'preprint_ui.py':
                if expected != old_code or actual != new_code:
                    raise ValueError('Unexpected preprint reader code binding')
                revised['contexts'][index]['baseline_files'][relative] = new_code
                changed_contexts.append(context['context_id'])
            elif actual != expected:
                raise ValueError('Unchanged recovery input binding failed: ' + relative)
    if len(changed_contexts) != 4:
        raise ValueError('Unexpected recovery code-binding inventory')
    evidence_after = checks.replace_hashes(evidence_before, {old_code: new_code}, revised)
    old_stamp, new_stamp = checks.digest(evidence_before), checks.digest(evidence_after)
    original_bound = preprints._bound_file

    def compatible_bound(repo, relative, expected):
        if relative == 'preprint_ui.py' and expected == old_code:
            if checks.digest((root / relative).read_bytes()) != new_code:
                raise ValueError('Reviewed reader changed during verification')
            return root / relative
        return original_bound(repo, relative, expected)

    # Validate native article gates, every scientific baseline binding, and all
    # native row-file bindings before accepting the sole code transition.
    with patch.object(preprints, '_bound_file', compatible_bound):
        before_status = preprints.preprint_status(root)
    if not before_status['available']:
        raise ValueError('Reviewed recovery evidence is unavailable')

    prepared = {evidence_path: evidence_after}
    originals = {evidence_path: evidence_before}
    manifests, fingerprints, artifact_count = [], {}, 0
    paths = sorted((root / 'summary').rglob('summary_manifest.json'))
    if len(paths) != 8:
        raise ValueError('Unexpected published summary inventory')
    for path in paths:
        relative = str(path.relative_to(root))
        raw = path.read_bytes()
        if raw != old_bytes(root, relative):
            raise ValueError('Summary metadata changed since reviewed release: ' + relative)
        document = json.loads(raw)
        checks.verify_assets(path.parent, document['artifacts'])
        artifact_count += len(document['artifacts'])
        mode = path.relative_to(root / 'summary').parts[0]
        for source, provenance in document['sources'].items():
            key = source + '/' + mode
            current = fingerprints.setdefault(key, summaries.input_fingerprint(root, source, mode))
            if provenance['input_sha256'] != current or not summaries._current(path.parent, current, source):
                raise ValueError('Scientific source fingerprint or saved assets changed: ' + relative)
        if 'preprints_included' in path.parts:
            revised = deepcopy(document)
            if revised['preprint_recovery_manifest_sha256'] != old_stamp:
                raise ValueError('Unexpected summary recovery-manifest binding')
            revised['preprint_recovery_manifest_sha256'] = new_stamp
            prepared[path] = checks.replace_hashes(raw, {old_stamp: new_stamp}, revised)
            originals[path] = raw
        manifests.append({'path': relative, 'sha256_before': checks.digest(raw),
                          'sha256_after': checks.digest(prepared.get(path, raw)),
                          'artifact_entries_unchanged': len(document['artifacts'])})

    # Prove the root reader is excluded from the scientific fingerprint, rather
    # than assuming this from its pathname. Every saved fingerprint stays exact.
    native_sha = summaries.sha
    with patch.object(summaries, 'sha', lambda path:
                      old_code if Path(path) == root / 'preprint_ui.py' else native_sha(path)):
        for key, expected in fingerprints.items():
            source, mode = key.split('/')
            if summaries.input_fingerprint(root, source, mode) != expected:
                raise ValueError('Deployment path reader affected a scientific fingerprint')

    seals = []
    for mode in ('yes', 'no'):
        path = root / 'summary' / mode / 'preprints_included/preprint_view_seal.json'
        relative = str(path.relative_to(root))
        raw = path.read_bytes()
        if raw != old_bytes(root, relative):
            raise ValueError('Summary seal changed since reviewed release')
        document = json.loads(raw)
        checks.verify_assets(path.parent, document['assets'])
        if document['preprint_manifest_sha256'] != old_stamp:
            raise ValueError('Unexpected seal recovery-manifest binding')
        for code, expected in document['producing_code'].items():
            compatible_bound(root, code, expected)
        if document['producing_code'].get('preprint_ui.py') != old_code:
            raise ValueError('Unexpected seal preprint-reader binding')
        revised = deepcopy(document)
        revised['producing_code']['preprint_ui.py'] = new_code
        revised['preprint_manifest_sha256'] = new_stamp
        replacements = {old_code: new_code, old_stamp: new_stamp}
        changed_assets = []
        for asset, expected in document['assets'].items():
            asset_path = path.parent / asset
            if asset_path in prepared:
                if asset_path.name != 'summary_manifest.json':
                    raise ValueError('Scientific output entered the repair set')
                revised['assets'][asset] = checks.digest(prepared[asset_path])
                replacements[expected] = revised['assets'][asset]
                changed_assets.append(asset)
        if set(changed_assets) != {'summary_manifest.json', 'cohort_latest/summary_manifest.json'}:
            raise ValueError('Unexpected summary metadata asset inventory')
        prepared[path] = checks.replace_hashes(raw, replacements, revised)
        originals[path] = raw
        seals.append({'path': relative, 'sha256_before': checks.digest(raw),
                      'sha256_after': checks.digest(prepared[path]),
                      'asset_keys_unchanged': True, 'metadata_assets_updated': changed_assets,
                      'scientific_asset_digests_unchanged': True})

    report = dict(schema_version=1, baseline_revision=BASELINE, checked_at=datetime.now(timezone.utc).isoformat(),
                  producing_helper=str(Path(__file__).resolve().relative_to(root)),
                  producing_helper_sha256=checks.digest(Path(__file__).read_bytes()),
                  reason='Locate Uro recovery evidence independently of deployment directory basename.',
                  reader_sha256_before=old_code, reader_sha256_after=new_code,
                  recovery_manifest_sha256_before=old_stamp, recovery_manifest_sha256_after=new_stamp,
                  changed_code_contexts=changed_contexts, scientific_evidence_payload_unchanged=True,
                  source_input_fingerprints_unchanged=fingerprints,
                  mathematical_results_regenerated=False, scientific_artifact_entries_verified=artifact_count,
                  preprint_status_before=before_status, manifests=manifests, seals=seals, applied=bool(apply))
    if not apply:
        return report
    if audit_file is None:
        raise ValueError('An applied code-only repair requires a durable audit receipt')
    try:
        for path, raw in originals.items():
            if path.read_bytes() != raw:
                raise ValueError('Release metadata changed during verification')
        for path, raw in prepared.items():
            path.write_bytes(raw)
        after_status = preprints.preprint_status(root)
        for key in ('available', 'status', 'contexts', 'records'):
            if after_status[key] != before_status[key]:
                raise ValueError('Recovered evidence availability or count changed')
        report['preprint_status_after'] = after_status
        for item in manifests:
            path = root / item['path']
            document = json.loads(path.read_text())
            checks.verify_assets(path.parent, document['artifacts'])
            mode = path.relative_to(root / 'summary').parts[0]
            for source in document['sources']:
                expected = fingerprints[source + '/' + mode]
                if summaries.input_fingerprint(root, source, mode) != expected or not summaries._current(path.parent, expected, source):
                    raise ValueError('Scientific source fingerprint changed during repair')
        checked = 0
        for item in seals:
            path = root / item['path']
            for asset in json.loads(path.read_text())['assets']:
                if not preprints.verify_preprint_summary_asset(path.parent / asset, root):
                    raise ValueError('Repaired asset fails native seal verification')
                checked += 1
        report['native_summary_checks_passed'] = True
        report['native_preprint_asset_checks_passed'] = checked
        destination = Path(audit_file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + '\n')
    except Exception:
        for path, raw in originals.items():
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
