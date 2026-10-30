"""Publish validated monthly outputs; retain only the current run for recovery."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta

from automation.monthly_pipeline import ACTIVE_PATH, MANIFEST_FILENAME, STATE_PATH, digest_json, iso_date, within_repo

ROOT = Path.cwd()
ALLOWED_ROOTS = ('Cached_results', 'ClaudeGemini_results', 'Disease_hierarchy_results', 'static', 'Plot', 'combined', 'Subexposure_results', 'Subexposure_views', 'data/subexposure_views', 'data/metaneuro', 'cost.txt', 'additionalExposureCost.txt', 'ClaudeGeminiCost.txt', 'data/monthly_updates', 'data/monthly_reports', 'data/monthly_automation')

# Optional monthly R3 review queues are durable release evidence.
ALLOWED_ROOTS += ('manualReview/preprint_recovery',)

def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)

def read(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default

def safe_checkpoint_path(path):
    parts=Path(path).parts
    suffix=Path(path).suffix.lower()
    return (not Path(path).is_absolute() and '..' not in parts and '\\' not in str(path)
            and any(str(path).startswith(base+'/') for base in ('data/monthly_updates','data/monthly_reports','data/monthly_automation','manualReview/preprint_recovery','monthlyHumanReview'))
            and (suffix in {'.json','.jsonl','.csv','.txt','.xml','.html','.htm','.pdf','.sqlite3'}
                 or str(path).endswith(('.sqlite3-wal','.sqlite3-shm'))))

def configuration():
    config=read(ROOT/'automation/monthly_config.json')
    if not isinstance(config,dict) or not config.get('repository'):
        raise RuntimeError('Monthly release requires its repository configuration.')
    return config

def active_run(config):
    active=read(within_repo(ROOT,config.get('active_file',ACTIVE_PATH)))
    if not active:return None
    run_id=active.get('run_id')
    if (not isinstance(run_id,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}',run_id)
            or active.get('repository')!=config['repository']
            or run_id!=f"{active.get('start_date')}_{active.get('end_date')}"):
        raise RuntimeError('Monthly active-run pointer is malformed or belongs to another repository.')
    return active

def checkpoint():
    target=ROOT/'.monthly-checkpoint'
    # Preserve private preprint raw sources and unknown-cost call ledgers.
    # They are workflow checkpoint inputs, never public Git release artifacts.
    if target.is_symlink():raise RuntimeError('Checkpoint root cannot be a symlink')
    if target.exists():
        private = target/'preprint_recovery'
        if private.is_symlink():raise RuntimeError('Private recovery checkpoint cannot be a symlink')
        for child in target.iterdir():
            if child.name == 'preprint_recovery':continue
            if child.is_dir() and not child.is_symlink():shutil.rmtree(child)
            else:child.unlink()
    config=configuration()
    active=active_run(config)
    if not active:return
    run=ROOT/'data/monthly_updates'/active['run_id']
    if not (run/MANIFEST_FILENAME).is_file():return
    for source in (run,ROOT/'data/monthly_reports'/run.name,within_repo(ROOT,config.get('active_file',ACTIVE_PATH)).parent):
        if not source.is_dir():continue
        for file in source.rglob('*'):
            if file.is_symlink() or not file.is_file():continue
            rel=file.relative_to(ROOT)
            if not safe_checkpoint_path(rel):continue
            dest=target/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,dest)
    review=ROOT/'monthlyHumanReview'/(run.name+'.csv')
    if review.is_file() and not review.is_symlink():
        dest=target/review.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(review,dest)
    queue=ROOT/'manualReview/preprint_recovery/monthly'
    for file in sorted(queue.glob(run.name+'_*.csv')) if queue.is_dir() else []:
        if file.is_symlink():raise RuntimeError('Preprint review queue must be a regular file')
        dest=target/file.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,dest)
    print(f'Recovery checkpoint prepared for {run.name}.')

def restore_checkpoint(folder,config):
    """Restore only the first unpublished interval; never roll state or old evidence back."""
    folder=Path(folder)
    # With both the checkpoint directory and a root log in upload-artifact's
    # input list, the common ancestor preserves this hidden directory name.
    payload=folder/'.monthly-checkpoint' if (folder/'.monthly-checkpoint').is_dir() else folder
    active_relative=Path(config.get('active_file',ACTIVE_PATH))
    candidate=read(payload/active_relative)
    if not isinstance(candidate,dict):return 0
    run_id=candidate.get('run_id')
    if (not isinstance(run_id,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}',run_id)
            or candidate.get('repository')!=config['repository']):return 0
    start=iso_date(candidate.get('start_date'),'recovery start')
    end=iso_date(candidate.get('end_date'),'recovery end')
    if run_id!=f'{start}_{end}' or end<start:return 0
    state=read(within_repo(ROOT,config.get('state_file',STATE_PATH)))
    if state is not None and (state.get('repository')!=config['repository'] or state.get('status')!='completed'):
        raise RuntimeError('Committed monthly state is invalid; checkpoint was not restored.')
    watermark=iso_date(state['last_successful_publication_date'] if state is not None else config['bootstrap']['last_successful_publication_date'],'committed watermark')
    if end<=watermark:return 0
    if start!=watermark+timedelta(days=1):return 0
    manifest=read(payload/'data/monthly_updates'/run_id/MANIFEST_FILENAME)
    if (not isinstance(manifest,dict) or manifest.get('repository')!=config['repository']
            or manifest.get('run_id')!=run_id or manifest.get('config_sha256')!=digest_json(config)):
        return 0
    current=active_run(config)
    if current is not None:
        current_end=iso_date(current['end_date'],'current active end')
        if current_end>watermark and current['run_id']!=run_id:
            raise RuntimeError('An unpublished monthly window already exists; refusing to replace it.')
    copies=[]
    for prefix in (Path('data/monthly_updates')/run_id,Path('data/monthly_reports')/run_id):
        source=payload/prefix
        if not source.is_dir():continue
        for file in source.rglob('*'):
            if file.is_symlink() or not file.is_file():continue
            relative=file.relative_to(payload)
            if not safe_checkpoint_path(relative):continue
            dest=within_repo(ROOT,relative)
            if dest.exists():
                if dest.read_bytes()!=file.read_bytes():
                    raise RuntimeError('A recovery file differs from an existing monthly artifact; no files were overwritten.')
                continue
            copies.append((file,dest))
    relative=Path('monthlyHumanReview')/(run_id+'.csv')
    review=payload/relative
    if review.is_file() and not review.is_symlink():
        dest=within_repo(ROOT,relative)
        if dest.exists() and dest.read_bytes()!=review.read_bytes():
            raise RuntimeError('A recovery file differs from an existing monthly artifact; no files were overwritten.')
        if not dest.exists():copies.append((review,dest))
    # Restore this validated unpublished interval's entire private subtree,
    # including extensionless raw blobs and unacknowledged dispatch receipts.
    private=payload/'preprint_recovery'/run_id
    if private.is_symlink():raise RuntimeError('Private recovery checkpoint cannot be a symlink')
    if private.is_dir():
        for file in sorted(private.rglob('*')):
            if file.is_symlink():raise RuntimeError('Private recovery checkpoint contains a symlink')
            if not file.is_file():continue
            relative=Path('.monthly-checkpoint/preprint_recovery')/run_id/file.relative_to(private)
            dest=within_repo(ROOT,relative)
            if dest.exists() and dest.read_bytes()!=file.read_bytes():
                raise RuntimeError('Private recovery bytes conflict; no call ledger may be overwritten')
            if not dest.exists():copies.append((file,dest))
    queue=payload/'manualReview/preprint_recovery/monthly'
    for file in sorted(queue.glob(run_id+'_*.csv')) if queue.is_dir() else []:
        if file.is_symlink():raise RuntimeError('Preprint review queue must be a regular file')
        dest=within_repo(ROOT,file.relative_to(payload))
        if dest.exists() and dest.read_bytes()!=file.read_bytes():
            raise RuntimeError('Preprint review checkpoint conflicts; existing review preserved')
        if not dest.exists():copies.append((file,dest))
    for source,dest in copies:
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    # A committed pointer to the previous completed month must be replaced with
    # this interrupted interval. Never restore state.json from the artifact.
    destination=within_repo(ROOT,active_relative)
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(payload/active_relative,destination)
    return len(copies)+1

def restore(repository):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',str(repository or '')):raise ValueError('Invalid repository')
    config=configuration()
    endpoint=f'repos/{repository}/actions/workflows/monthly-evidence-update.yml/runs?per_page=100'
    try:
        runs=json.loads(command(['gh','api',endpoint],capture_output=True).stdout)['workflow_runs']
    except subprocess.CalledProcessError:
        print('No prior monthly workflow checkpoints found.');return
    for run in runs:
        if run.get('status')!='completed' or run.get('conclusion')=='success':continue
        with tempfile.TemporaryDirectory() as folder:
            result=subprocess.run(['gh','run','download',str(run['id']),'--repo',repository,'--name','monthly-evidence-checkpoint','--dir',folder],capture_output=True,text=True)
            if result.returncode:continue
            restored=restore_checkpoint(folder,config)
            if restored:
                print(f'Restored {restored} checkpoint files from run {run["id"]}.');return
    print('No resumable monthly checkpoint is available.')

def publish(repository,branch):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise ValueError('Invalid repository')
    command([os.sys.executable,'-m','automation.monthly_pipeline','--repo-root','.', '--config','automation/monthly_config.json','--finalize-state'])
    config=configuration();active=active_run(config)
    if active is None:raise RuntimeError('Cannot publish without a validated active monthly run.')
    # Force-add this audited interval: inherited ignore rules exclude whole
    # monthly directories in some repos. Never force-add old ignored runs.
    paths=[path for path in ALLOWED_ROOTS if path not in {'data/monthly_updates','data/monthly_reports','monthlyHumanReview'} and (ROOT/path).exists()]
    paths.extend(str(Path(base)/active['run_id']) for base in ('data/monthly_updates','data/monthly_reports') if (ROOT/base/active['run_id']).exists())
    review=Path('monthlyHumanReview')/(active['run_id']+'.csv')
    if (ROOT/review).is_file():paths.append(str(review))
    command(['git','config','user.name','github-actions[bot]'])
    command(['git','config','user.email','41898282+github-actions[bot]@users.noreply.github.com'])
    archived={}
    if config.get('adapter')=='neuro':
        from automation.monthly_archives import pack_run
        archived=pack_run(ROOT,config,active['run_id'])
        if 'data/monthly_automation' not in paths:paths.append('data/monthly_automation')
    command(['git','add','-f','-A','--',*paths])
    # Keep hydrated canonical files locally while Git stores their immutable,
    # checksummed gzip chunks below its per-file size limit.
    if archived:command(['git','rm','--cached','--ignore-unmatch','--',*archived])
    # The watermark must never be published without its exact input packets,
    # application gate, completed manifest, and human-review table.
    run=ROOT/'data/monthly_updates'/active['run_id'];manifest=read(run/MANIFEST_FILENAME)
    required=[str((run/MANIFEST_FILENAME).relative_to(ROOT)),config.get('state_file',STATE_PATH)]
    required.extend(str((run/name).relative_to(ROOT)) for name in manifest.get('raw_packet_sha256',{}))
    required.extend(manifest.get('review_artifacts_sha256',{}))
    required.append(str((run/'monthly_application_gate.json').relative_to(ROOT)))
    for path in required:
        proofs=[path] if path not in archived else ['data/monthly_automation/archives.json',*[chunk['path'] for chunk in archived[path]['chunks']]]
        for proof in proofs:command(['git','cat-file','-e',':'+proof],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if subprocess.run(['git','diff','--cached','--quiet']).returncode==0:
        print('No monthly publication changes.');return
    command(['git','commit','-m','Update monthly evidence, Astra review, and successful search checkpoint'])
    # A concurrent default-branch update must be reconciled explicitly, never forced.
    command(['git','push','origin',f'HEAD:{branch}'])
    print('Published validated monthly evidence and human-review CSV tables.')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['restore','checkpoint','publish'])
    parser.add_argument('--repository');parser.add_argument('--branch',default='main')
    args=parser.parse_args()
    if args.action=='checkpoint':checkpoint()
    elif args.action=='restore':restore(args.repository)
    else:publish(args.repository,args.branch)
if __name__=='__main__':main()
