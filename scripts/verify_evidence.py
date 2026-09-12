"""Verify the immutable snapshots, optionally also checking their original folders."""
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
    for entry in manifest['files']:
        targets = [ROOT / entry['path']]
        if original_parent:
            targets.append(Path(original_parent) / Path(entry['path']).relative_to('sources'))
        for path in targets:
            if path.exists() and getattr(path.stat(), 'st_flags', 0) & 0x40000000:
                failures.append(str(path) + ' (download from OneDrive before direct comparison)')
                continue
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                failures.append(str(path))
    if failures:
        print('\n'.join(failures))
        print(f'FAILED: {len(failures)} changed or missing files')
        return 1
    print(f"Verified {len(manifest['files']):,} snapshot files; all SHA-256 checksums match.")
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--original-parent')
    args = parser.parse_args()
    sys.exit(verify(args.original_parent))
