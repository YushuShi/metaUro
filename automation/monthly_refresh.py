"""Refresh metaUro after both source repositories publish the monthly window."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from automation.aggregate_common import monthly_cutoff, validate_release
from automation.sync_sources import SOURCES, sync_sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repos-root', type=Path, required=True)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    cutoff = monthly_cutoff(datetime.now(timezone.utc).date())
    for source in SOURCES:
        release = validate_release(args.repos_root / source, cutoff)
        if release is None:
            print(f'Waiting for {source} to publish a completed release through {cutoff}; current results are retained.')
            return
        if release['repository'] != source:
            raise ValueError('Unexpected source repository identity')
    if args.preflight:
        print('Both source repositories have completed monthly releases.'); return
    root = Path(__file__).resolve().parents[1]
    sync_sources(root, args.repos_root, require_through=str(cutoff))
    subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-q'], cwd=root, check=True)
    print('Updated metaUro results and human-review tables; original evidence is unchanged.')


if __name__ == '__main__':
    main()
