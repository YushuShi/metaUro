"""Verify PI presentation releases and unchanged saved scientific artifacts."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
AUDIT=ROOT/'data/summary_pi_audit/2026-09-27'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def verify(baseline_root, api=False):
    refresh=json.loads((AUDIT/'refresh.json').read_text())
    changed={entry['path']:entry for entry in refresh['artifacts']}
    repair_path=AUDIT/'integrity_link_repair.json'
    repair=json.loads(repair_path.read_text()) if repair_path.exists() else None
    if repair:
        entry=repair['artifact'];changed[entry['path']]=entry
    allowed={name for name in changed if name.endswith(('interim_manifest.json','active_interim_view.json','nonmalignant_active_view.json')) or name=='combined/provisional_ui/manifest.json'}
    protected=[]
    for suffix in ['science-before.json','other-plots-before.json']:
        entries=json.loads((baseline_root/(ROOT.name+'-'+suffix)).read_text())
        for entry in entries:
            if entry['path'] in allowed:
                continue
            actual=sha(ROOT/entry['path'])
            if actual!=entry['sha256']:
                raise ValueError(f"Protected artifact changed: {entry['path']}")
            protected.append(entry)
    if repair:
        for entry in repair['verified_source_links']:
            if sha(ROOT/entry['path'])!=entry['current_sha256']:
                raise ValueError('Repaired source link changed')
            if entry['path'] not in {item['path'] for item in protected}:
                protected.append({'path':entry['path'],'sha256':entry['current_sha256']})
    for path,entry in changed.items():
        if sha(ROOT/path)!=entry['after_sha256']:
            raise ValueError(f'Published artifact hash differs: {path}')
    # Retain the verified seal preimages so this audit also replays after commit.
    preimage_path=AUDIT/'integrity_manifest_preimages.json'
    preimages=json.loads(preimage_path.read_text()) if preimage_path.exists() else {}
    for path in allowed:
        if path not in preimages:
            raw=subprocess.check_output(['git','show','HEAD:'+path],cwd=ROOT)
            if hashlib.sha256(raw).hexdigest()!=changed[path]['before_sha256']:
                raise ValueError(f'Integrity preimage differs: {path}')
            preimages[path]={'sha256':changed[path]['before_sha256'],'text':raw.decode()}
        if (hashlib.sha256(preimages[path]['text'].encode()).hexdigest()!=changed[path]['before_sha256']
                or preimages[path]['sha256']!=changed[path]['before_sha256']):
            raise ValueError(f'Saved integrity preimage differs: {path}')
    write(preimage_path,preimages)
    # A seal refresh may alter figure digests and display metadata only.
    for path in allowed:
        before=json.loads(preimages[path]['text'])
        after=json.loads((ROOT/path).read_text())
        if path=='combined/provisional_ui/manifest.json':
            before_assets=before.pop('protected_source_hashes')
            after_assets=after.pop('protected_source_hashes')
            expected=dict(before_assets)
            for entry in repair['verified_source_links']:
                if expected[entry['path']]!=entry['old_manifest_sha256']:
                    raise ValueError('Repair source preimage differs')
                expected[entry['path']]=entry['current_sha256']
            if expected!=after_assets:
                raise ValueError('Unexpected source integrity link changed')
        elif path.endswith('interim_manifest.json'):
            key='files_sha256' if 'files_sha256' in before else 'asset_sha256'
            before_assets=before.pop(key);after_assets=after.pop(key)
            if set(before_assets)!=set(after_assets):
                raise ValueError('Integrity asset membership changed')
            for asset,digest in after_assets.items():
                if digest!=before_assets[asset] and asset not in changed:
                    raise ValueError(f'Non-presentation asset resealed: {asset}')
            after.pop('forest_presentation',None)
        else:
            before.pop('manifest_sha256');after.pop('manifest_sha256')
        if before!=after:
            raise ValueError(f'Scientific bundle metadata changed: {path}')
    write(AUDIT/'protected_artifact_hashes.json',protected)
    results={'protected_artifacts_unchanged':len(protected),
             'workbooks_unchanged':sum(e['path'].endswith('.xlsx') for e in protected),
             'json_files_unchanged':sum(e['path'].endswith('.json') for e in protected),
             'other_figures_unchanged':sum(e['path'].endswith(('.png','.pdf')) for e in protected),
             'forest_pdfs_refreshed':len(refresh['jobs']),
             'manifest_updates':len(changed)-len(refresh['jobs']),
             'languages':sorted({j['locale'] for j in refresh['jobs']}),
             'scientific_recalculation':False}
    if api:
        import app as web
        import meta_analysis
        def forbidden(*args,**kwargs):
            raise RuntimeError('Summary figure requests must not run a new analysis')
        meta_analysis.perform_meta_analysis=forbidden
        client=web.app.test_client()
        checks=[]
        for job in refresh['jobs']:
            url=f"/summary/plots/{job['scope_key']}/{job['name']}"
            response=client.get(url,query_string={'lang':job['locale'],'include_subexposures':job['mode']})
            expected=changed[job['target']]['after_sha256']
            actual=hashlib.sha256(response.data).hexdigest()
            if response.status_code!=200 or actual!=expected:
                raise ValueError(f"API forest mismatch: {url} {job['locale']} {job['mode']} ({response.status_code})")
            checks.append({'url':url,'language':job['locale'],'include_subexposures':job['mode'],
                           'path':job['target'],'sha256':actual})
        write(AUDIT/'api_verification.json',checks)
        results['api_figures_verified']=len(checks)
        for entry in protected:
            if sha(ROOT/entry['path']) != entry['sha256']:
                raise ValueError(f"Summary request changed scientific input: {entry['path']}")
    write(AUDIT/'validation.json',results)
    print(json.dumps(results,indent=2),flush=True)
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root',type=Path,default=Path('/tmp/metaanalysis-pi-audit'))
    parser.add_argument('--api',action='store_true')
    args=parser.parse_args();verify(args.baseline_root,args.api)
