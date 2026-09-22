"""Verify the active evidence release and its preserved import provenance."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def verify(original_parent=None):
    manifest = json.loads((ROOT / 'evidence-manifest.json').read_text())
    failures = []
    if manifest.get('pending_cloud_files'):
        print(f"Incomplete snapshot: {len(manifest['pending_cloud_files'])} cloud files are still pending.")
        return 1
    collections = [(manifest['files'] + manifest.get('supporting_files', []), ROOT, False)]
    if original_parent:
        baseline_path = manifest.get('baseline_snapshot_manifest')
        baseline = json.loads((ROOT / baseline_path).read_text()) if baseline_path else manifest
        collections.append((baseline['files'], Path(original_parent), True))
    for entries, parent, original in collections:
        for entry in entries:
            relative = Path(entry['path']).relative_to('sources') if original else Path(entry['path'])
            path = parent / relative
            if path.exists() and getattr(path.stat(), 'st_flags', 0) & 0x40000000:
                failures.append(str(path) + ' (download from OneDrive before direct comparison)')
                continue
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                failures.append(str(path))
    summary_count = 0
    for summary_manifest in sorted((ROOT / 'summary').glob('*/summary_manifest.json')):
        summary = json.loads(summary_manifest.read_text())
        for relative, entry in summary.get('artifacts', {}).items():
            path = (summary_manifest.parent / relative).resolve()
            if (not path.is_relative_to((ROOT / 'summary').resolve()) or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']):
                failures.append(str(path))
            summary_count += 1
    if failures:
        print('\n'.join(failures))
        print(f'FAILED: {len(failures)} changed or missing files')
        return 1
    print(f"Verified {len(manifest['files']):,} evidence files and "
          f"{len(manifest.get('supporting_files', []))} provenance files and "
          f"{summary_count} current summary artifacts; all SHA-256 checksums match.")
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--original-parent')
    args = parser.parse_args()
    sys.exit(verify(args.original_parent))
