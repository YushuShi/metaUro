"""Build opt-in assets from clean published HEAD plus task code/frozen local data."""
from pathlib import Path
import argparse,concurrent.futures,datetime,hashlib,json,os,shutil,subprocess,sys,tarfile,tempfile
WORK=Path(__file__).resolve().parents[1];ROOT=WORK.parent;AUDIT=WORK/'ui_architecture'
COMMON=['MetaVir','metaThyroid','metaGut','metaFemina','metaBreath','metaPee','MetaCutis_et_ Capillus']
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def run(repo):
    working=ROOT/repo;manifest_path=working/'data/preprint_recovery/active_manifest.json';manifest=json.loads(manifest_path.read_text());manifest_sha=sha(manifest_path)
    assert manifest.get('status')=='complete'
    head=subprocess.run(['git','rev-parse','HEAD'],cwd=working,capture_output=True,text=True,check=True).stdout.strip()
    if not manifest['contexts']:
        receipt=dict(repository=repo,status='reused_base',reason='Complete frozen dataset has no recovered contexts',manifest_sha256=manifest_sha,published_baseline_commit=head,outputs=[])
        (AUDIT/(repo.replace(' ','_')+'_optional_release.json')).write_text(json.dumps(receipt,indent=2)+'\n');return receipt
    owned=json.loads((AUDIT/'owned_change_paths.json').read_text())[repo];dependencies={}
    for context in manifest['contexts']:
        for relative,digest in context['baseline_files'].items():
            source=working/relative;assert sha(source)==digest,(repo,relative,'working binding drift')
            tracked=subprocess.run(['git','cat-file','-e',head+':'+relative],cwd=working,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
            assert tracked or relative in owned or relative.startswith('data/preprint_recovery/'),(repo,relative,'unpublished runtime dependency')
            dependencies[relative]=dict(sha256=digest,head_tracked=tracked,task_owned=relative in owned)
    reuse=json.loads(os.environ.get('PREPRINT_CLEAN_CLONE_MAP','{}')).get(repo)
    scratch=Path(reuse).parent if reuse else Path(tempfile.mkdtemp(prefix='preprint-published-'+repo.replace(' ','_')+'-',dir='/private/tmp'));clone=scratch/repo
    if not reuse:clone.mkdir()
    print(json.dumps(dict(repository=repo,status='preparing_clean_head',head=head,clone=str(clone))),flush=True)
    if not reuse:
        process=subprocess.Popen(['git','archive',head],cwd=working,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdout,mode='r|') as archive:archive.extractall(clone,filter='data')
        finally:
            process.stdout.close()
        errors=process.stderr.read().decode();assert process.wait()==0,errors
    code={}
    for relative in owned:
        source=working/relative
        if source.is_file():
            target=clone/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target);code[relative]=sha(source)
    target_data=clone/'data/preprint_recovery';shutil.copytree(working/'data/preprint_recovery',target_data,dirs_exist_ok=True)
    for relative,item in dependencies.items():assert sha(clone/relative)==item['sha256'],(repo,relative,'clean publication binding differs')
    plans={}
    for context in manifest['contexts']:
        for relative in context['source_cache_paths']:
            path=Path(relative);cache=path.parent.parent
            assert cache.name=='Cached_results' or (repo=='MetaVir' and cache.name in {'yes','no'}),(repo,relative)
            dataset_root=cache.parent if repo=='metaFemina' else cache
            plot_root=cache/'Plot' if repo=='MetaVir' and cache.name in {'yes','no'} else cache.parent/'Plot'
            key=(str(dataset_root),context['selector']['include_subexposures'],context['selector'].get('source_variant','combined'))
            plans[key]=dict(cache_root=str(dataset_root),plot_root=str(plot_root),mode=key[1],source_variant=key[2])
    receipts=[];env={**os.environ,'MPLCONFIGDIR':'/private/tmp/cohort-mplcache','XDG_CACHE_HOME':'/private/tmp/cohort-cache','PYTHONUNBUFFERED':'1'}
    # Onco consumes the all-records native-final source witnesses first.
    for latest in [False,True]:
        for key,item in sorted(plans.items()):
            assert sha(manifest_path)==manifest_sha,'Frozen source data changed during build'
            command=[sys.executable,'-u','scripts/build_preprint_summaries.py','--cache-root',item['cache_root'],'--plot-root',item['plot_root'],'--include-subexposures',item['mode'],'--source-variant',item['source_variant'],'--languages','en']
            if latest:command.append('--keep-latest-cohort')
            log=AUDIT/(repo.replace(' ','_')+'_'+item['mode']+('_latest' if latest else '_all')+'_native_optional_build.log')
            print(json.dumps(dict(repository=repo,status='building',mode=item['mode'],latest=latest,log=str(log))),flush=True)
            with log.open('w') as stream:result=subprocess.run(command,cwd=clone,env=env,stdout=stream,stderr=subprocess.STDOUT)
            assert result.returncode==0,(repo,item,latest,'native build failed',str(log))
            namespace=clone/item['plot_root']/'preprints_included';destination=working/item['plot_root']/'preprints_included'
            staging=destination.parent/('.preprint-release-promote-'+str(os.getpid()));shutil.copytree(namespace,staging)
            backup=destination.parent/('.preprint-release-previous-'+str(os.getpid()))
            if destination.exists():destination.replace(backup)
            try:staging.replace(destination)
            except BaseException:
                if backup.exists():backup.replace(destination)
                raise
            if backup.exists():shutil.move(str(backup),str(scratch/(item['mode']+('_latest' if latest else '_all')+'_previous_optional')))
            output=destination/('cohort_latest' if latest else '');receipt_path=output/'preprint_build_receipt.json'
            receipt=json.loads(receipt_path.read_text());receipt.update(published_baseline_commit=head,clean_build_tree=str(clone),repository=repo)
            # Keep the native sealed bytes intact; publication baseline is bound
            # in this separate durable orchestration receipt.
            receipts.append(dict(plan=item,keep_latest_cohort=latest,receipt=str(receipt_path.relative_to(working)),receipt_sha256=sha(receipt_path)))
            checkpoint=dict(repository=repo,status='running',published_baseline_commit=head,manifest_sha256=manifest_sha,clean_tree=str(clone),code_sha256=code,runtime_dependencies=dependencies,builds=receipts)
            (AUDIT/(repo.replace(' ','_')+'_optional_release.json')).write_text(json.dumps(checkpoint,indent=2)+'\n')
            print(json.dumps(dict(repository=repo,status='completed_view',mode=item['mode'],latest=latest,native_receipt=str(receipt_path))),flush=True)
    artifacts={str(path.relative_to(working)):sha(path) for item in plans.values() for path in (working/item['plot_root']/'preprints_included').rglob('*') if path.is_file()}
    checkpoint.update(status='complete',artifact_sha256=artifacts)
    (AUDIT/(repo.replace(' ','_')+'_optional_release.json')).write_text(json.dumps(checkpoint,indent=2)+'\n');return checkpoint
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--repos',nargs='+',choices=COMMON,default=COMMON);parser.add_argument('--jobs',type=int,default=2);args=parser.parse_args()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:results=list(pool.map(run,args.repos))
    (AUDIT/'common_optional_release_build.json').write_text(json.dumps(dict(generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),repositories=results),indent=2)+'\n')
