"""Refresh metaUro after both source repositories publish the monthly window."""
from __future__ import annotations
import argparse
from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
import subprocess
import sys
from automation.aggregate_common import monthly_cutoff, validate_release
from automation.sync_sources import SOURCES, sync_sources, verify_release

ROOT = Path(__file__).resolve().parents[1]


def file_sha(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def tree_sha(path):
    """Hash a generated tree so summary-only repairs are not treated as no-ops."""
    path = Path(path)
    if not path.is_dir():
        return None
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob('*')
                       if candidate.is_file()
                       and not any(part.startswith('.') for part in candidate.relative_to(path).parts)):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(b'\0')
        digest.update(hashlib.sha256(item.read_bytes()).digest())
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repos-root', type=Path, default=ROOT / '.sources',
                        help='Directory containing metaPee/ and MetaVir/ checkouts (default: .sources)')
    parser.add_argument('--cutoff', type=date.fromisoformat,
                        help='Require this exact YYYY-MM-DD publication cutoff; defaults to the scheduled window')
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args(argv)
    cutoff = args.cutoff or monthly_cutoff(datetime.now(timezone.utc).date())
    for source in SOURCES:
        release = validate_release(args.repos_root / source, cutoff)
        if release is None:
            print(f'Waiting for {source} to publish a completed release through {cutoff}; current results are retained.')
            return
        if release['repository'] != source:
            raise ValueError('Unexpected source repository identity')
    if args.preflight:
        for source in SOURCES:
            verify_release(args.repos_root / source, source)
        print('Both source repositories have completed monthly releases.')
        return
    index_path = ROOT / 'updates/manifest.json'
    before = file_sha(index_path)
    summary_before = tree_sha(ROOT / 'summary')
    sync_sources(ROOT, args.repos_root, require_through=str(cutoff))
    # Import lazily so preflight remains a read-only source-release check and
    # does not load the plotting stack.
    from summary_outputs import refresh_all
    refresh_all(ROOT)
    if before == file_sha(index_path) and summary_before == tree_sha(ROOT / 'summary'):
        print('metaUro already contains this verified monthly release; no files changed.')
        return
    subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-q'], cwd=ROOT, check=True)
    print('Updated metaUro results, summaries, and human-review tables.')


if __name__ == '__main__':
    main()
