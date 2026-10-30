"""Developer-only snapshot creation; monthly jobs never use legacy repositories."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def vendor(legacy_root):
    target = ROOT/'runtime_code/monthly'
    manifest = {'version':'metauro-autonomous-monthly-v1',
                'purpose':'Self-contained urinary screening and extraction runtime; no runtime external checkout.',
                'origins':{},'files':{}}
    for scope in ('metaPee','MetaVir'):
        source = Path(legacy_root)/scope
        manifest['origins'][scope] = {'commit':subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip(),
            'note':'Code and catalog snapshot; historical evidence comes only from metaUro sources plus its updates.'}
        paths = set(source.glob('*.py')) | set((source/'scripts').glob('*.py')) | set((source/'automation').glob('*.py'))
        paths |= set((source/'automation/preprint_recovery').glob('*.py'))
        paths |= set((source/'data').glob('*.json')) | set((source/'static').glob('*.json'))
        paths |= set(source.glob('*.csv')) | {source/'automation/monthly_config.json'}
        # Explicitly scoped scientific adjudications used by the saved code.
        paths |= {source/'data/multicancer_review_2026-09-29/registry.json'}
        paths |= {source/'tests/test_ncbi_throttle.py'}
        changes = set(subprocess.check_output(['git','-C',str(source),'diff','--name-only','HEAD'],text=True).splitlines())
        changes.update(subprocess.check_output(['git','-C',str(source),'ls-files','--others','--exclude-standard'],text=True).splitlines())
        manifest['origins'][scope]['snapshot_worktree_changes'] = sorted(
            str(path.relative_to(source)) for path in paths if str(path.relative_to(source)) in changes)
        if manifest['origins'][scope]['snapshot_worktree_changes']:
            manifest['origins'][scope]['note'] += ' Listed worktree patches are pinned by the per-file runtime hashes.'
        for path in sorted(paths):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            destination = target/scope/relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path,destination)
    # A manifest is generated after local adaptations, by --seal below.
    (target/'origins.json').write_text(json.dumps(manifest['origins'],indent=2)+'\n')
    return seal()


def seal():
    target = ROOT/'runtime_code/monthly'
    manifest = {'version':'metauro-autonomous-monthly-v1',
                'purpose':'Self-contained urinary screening and extraction runtime; no runtime external checkout.',
                'origins':json.loads((target/'origins.json').read_text()),
                'files':{str(p.relative_to(target)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(target.rglob('*')) if p.is_file() and p.name!='manifest.json'
                    and '__pycache__' not in p.parts and not p.name.endswith('.pyc')}}
    (target/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(f'Sealed {len(manifest["files"])} standalone runtime files.')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-root',type=Path)
    parser.add_argument('--seal',action='store_true')
    args=parser.parse_args()
    if args.seal:
        seal()
    elif args.legacy_root:
        vendor(args.legacy_root)
    else:
        parser.error('Supply --legacy-root for explicit developer snapshot creation, or --seal.')
