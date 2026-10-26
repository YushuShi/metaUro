"""Stage only this task's code and authoritative results, preserving local work."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parent
REPOS = ['MetaVir', 'metaThyroid', 'metaGut', 'metaFemina', 'metaHeart',
         'metaBreath', 'metaPee', 'metaNeuro', 'MetaCutis_et_ Capillus', 'metaMeta', 'metaUro']
INSERT = "\n\n# Optional preprint source views and their release audit are authoritative.\nALLOWED_ROOTS += ('data/preprint_recovery', 'manualReview/preprint_recovery')"


def git(repo, *args, data=None):
    return subprocess.check_output(['git', *args], cwd=ROOT/repo, input=data)


def metadata(path):
    return json.loads(path.read_text()) if path.exists() else {}


def safe_file(path):
    return (not any(p in {'__pycache__', '.pytest_cache', '.git'} for p in path.parts)
            and path.suffix not in {'.pyc', '.body', '.env'} and path.name != '.DS_Store'
            and not path.name.endswith('.tmp'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', action='store_true')
    parser.add_argument('--repository', action='append', choices=REPOS)
    args = parser.parse_args()
    before = metadata(BASE/'workspace_baseline.json')
    inventories = [metadata(BASE/'ui_architecture/owned_change_paths.json'),
        metadata(BASE/'special_runtime/owned_paths.json'),
        metadata(BASE/'ui_architecture/owned_artifact_paths.json'),
        metadata(BASE/'special_runtime/owned_artifact_paths.json')]
    target = BASE/('release_staging_report.json' if args.stage else 'release_staging_plan.json')
    report = metadata(target)
    for repo in args.repository or REPOS:
        branch = git(repo, 'branch', '--show-current').decode().strip()
        expected_branch = 'V2' if repo in {'metaFemina', 'metaUro'} else 'main'
        if branch != expected_branch or git(repo, 'rev-parse', 'HEAD').decode().strip() != before[repo]['head']:
            raise ValueError('Unexpected publication branch/base for '+repo)
        paths = set()
        for inventory in inventories:
            owned = inventory.get(repo, [])
            if isinstance(owned, dict):
                owned = owned.get('authoritative_namespaces', [])
            paths.update(owned)
        prefix = 'metaOnco/' if repo == 'metaMeta' else ''
        paths.add(prefix+('updates' if repo == 'metaUro' else 'data')+'/preprint_recovery')
        paths.add('manualReview/preprint_recovery')
        expanded = set()
        for relative in paths:
            path = ROOT/repo/relative
            if path.is_dir():
                expanded.update(str(p.relative_to(ROOT/repo)) for p in path.rglob('*') if p.is_file() and safe_file(p))
            elif path.is_file() and safe_file(path):
                expanded.add(relative)
            else:
                raise ValueError('Owned release path is missing: '+repo+'/'+relative)
        old_dirty = {line[3:] for line in before[repo]['status'].splitlines() if len(line) > 3}
        if old_dirty.intersection(expanded):
            raise ValueError('Owned release overlaps pre-existing dirty file: '+repo)
        existing = set(git(repo, 'diff', '--cached', '--name-only', '-z').decode().split('\0'))-{''}
        if existing-expanded-{'automation/monthly_release.py'}:
            raise ValueError('Unrelated staged work must be preserved: '+repo)
        if args.stage:
            # Ignored results are included only inside the supplied release roots.
            for index in range(0, len(expanded), 150):
                git(repo, 'add', '-f', '--', *sorted(expanded)[index:index+150])
            if repo in {'metaPee', 'metaNeuro', 'MetaCutis_et_ Capillus'}:
                relative = 'automation/monthly_release.py'
                head = git(repo, 'show', 'HEAD:'+relative).decode()
                position = head.index('\n', head.index('ALLOWED_ROOTS ='))
                release = (head[:position]+INSERT+head[position:]).encode()
                blob = git(repo, 'hash-object', '-w', '--stdin', data=release).decode().strip()
                git(repo, 'update-index', '--add', '--cacheinfo', '100644,'+blob+','+relative)
            code_paths = set()
            for inventory in inventories[:2]:
                code_paths.update(inventory.get(repo, []))
            if repo in {'metaPee', 'metaNeuro', 'MetaCutis_et_ Capillus'}:
                code_paths.add('automation/monthly_release.py')
            # Preserve native SVG/CSV bytes and historical source snapshots;
            # whitespace lint applies to the authored application code.
            if code_paths:
                git(repo, '-c', 'core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol',
                    'diff', '--cached', '--check', '--', *sorted(code_paths))
        staged = sorted(set(git(repo, 'diff', '--cached', '--name-only', '-z').decode().split('\0'))-{''})
        objects = {}
        for entry in git(repo, 'ls-files', '--stage', '-z').split(b'\0'):
            if not entry:
                continue
            header, name = entry.split(b'\t', 1)
            mode, oid, stage = header.decode().split()
            if stage == '0':
                objects[name.decode()] = oid
        object_format = git(repo, 'rev-parse', '--show-object-format').decode().strip()
        mismatches = []
        for relative in staged:
            path = ROOT/repo/relative
            if relative == 'automation/monthly_release.py' and repo in {'metaPee', 'metaNeuro', 'MetaCutis_et_ Capillus'}:
                continue  # Deliberately stage only our narrow addition to this dirty file.
            content = path.read_bytes()
            blob_digest = hashlib.new(object_format, b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()
            if objects.get(relative) != blob_digest:
                mismatches.append(relative)
            if path.stat().st_size >= 100_000_000:
                raise ValueError('Release file exceeds GitHub individual-file limit: '+str(path))
        if mismatches:
            raise ValueError('Git filters changed verified artifact bytes: '+repo+' '+str(mismatches[:3]))
        report[repo] = {'branch': branch, 'owned_files': len(expanded), 'staged_files': len(staged),
            'staged_bytes_match_working_results': not mismatches,
            'staged_paths': staged, 'owned_paths': sorted(expanded),
            'unstaged_status': git(repo, 'status', '--short').decode()}
        target.write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps({'repository': repo, 'owned_files': len(expanded), 'staged_files': len(staged)}))
    target.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
