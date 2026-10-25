"""Build and verify fresh optional Neuro/Uro summaries from saved evidence."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(os.environ.get('COHORT_WORKSPACE_ROOT', Path(__file__).resolve().parents[1])).resolve()
WORK = Path(os.environ.get('COHORT_WORK_DIR', Path(__file__).resolve().parent)).resolve()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(target):
    repo = ROOT / target
    inputs = {str(path.relative_to(repo)): sha(path) for path in
              [repo / 'cohort_identity.py', repo / 'data/cohort_registry.json',
               repo / 'data/cohort_record_metadata.json']}
    metadata = json.loads((repo / 'data/cohort_record_metadata.json').read_text())
    sources = metadata['source_files']
    before = {name: sha(repo / name) for name in sources}
    if target == 'metaNeuro':
        folders = [repo / 'static/summary_plots' / mode / 'cohort_latest' for mode in ('yes', 'no')]
        commands = [('all', [sys.executable, 'scripts/build_summary_plots.py', '--keep-latest-cohort'])]
    else:
        folders = [repo / 'summary' / mode / 'cohort_latest' for mode in ('yes', 'no')]
        commands = [(source + '_' + mode, [sys.executable, 'scripts/build_cohort_summaries.py',
                     '--source', source, '--mode', mode, '--languages', 'en'])
                    for mode in ('yes', 'no') for source in ('metaPee', 'MetaVir')]
    scratch = Path(tempfile.mkdtemp(prefix=target + '-cohort-preliminary-'))
    for folder in folders:
        tracked = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '--',
                                          str(folder.relative_to(repo))], text=True)
        if tracked.strip():
            raise RuntimeError('Refusing to move tracked authoritative outputs: ' + str(folder))
        if folder.exists():
            destination = scratch / folder.parent.name
            shutil.move(str(folder), destination)
    env = dict(os.environ, MPLBACKEND='Agg', MPLCONFIGDIR='/private/tmp/cohort-mplcache',
               XDG_CACHE_HOME='/private/tmp/cohort-cache', METANEURO_DISABLE_API='1',
               OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
    logs = []
    for label, command in commands:
        log = WORK / ('final_summary_build_' + target + '_' + label + '.log')
        with log.open('w') as output:
            subprocess.run(command, cwd=repo, env=env, check=True, stdout=output, stderr=subprocess.STDOUT)
        logs.append(log.name)
        print(target, label, 'complete', flush=True)
    after = {name: sha(repo / name) for name in sources}
    if before != after or inputs != {name: sha(repo / name) for name in inputs}:
        raise RuntimeError('Scientific evidence or cohort metadata changed during summary generation')
    artifacts = {str(path.relative_to(repo)): {'sha256': sha(path), 'bytes': path.stat().st_size}
                 for folder in folders for path in folder.rglob('*') if path.is_file() and path.name != '.build.lock'}
    receipt = {'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'repository': target, 'include_all_is_default': True, 'keep_latest_cohort': True,
               'source_cache_hashes_unchanged': True, 'source_cache_sha256': before,
               'cohort_inputs_sha256': inputs, 'artifacts': artifacts, 'logs': logs,
               'preliminary_output_archive': str(scratch), 'no_model_calls': True}
    path = repo / 'data/cohort_registry_audit/summary_build_receipt.json'
    path.write_text(json.dumps(receipt, indent=2) + '\n')
    shutil.copy2(__file__, repo / 'data/cohort_registry_audit/workspace_summary_runner.py')
    print(target, 'verified', len(artifacts), 'artifacts', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=('metaNeuro', 'metaUro'))
    main(parser.parse_args().target)
