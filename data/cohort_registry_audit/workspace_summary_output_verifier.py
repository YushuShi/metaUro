"""Read-only final verification of the separate Neuro/Uro cohort summaries."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys

ROOT = Path(os.environ.get('COHORT_WORKSPACE_ROOT', Path(__file__).resolve().parents[1])).resolve()
WORK = Path(os.environ.get('COHORT_WORK_DIR', Path(__file__).resolve().parent)).resolve()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def urls(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from urls(item)
    elif isinstance(value, str) and value.startswith('/static/summary_plots/'):
        yield value


def main(target):
    repo = ROOT / target
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    os.environ.update(METANEURO_DISABLE_API='1', MPLBACKEND='Agg',
                      MPLCONFIGDIR='/private/tmp/cohort-mplcache', XDG_CACHE_HOME='/private/tmp/cohort-cache')
    import app as web
    from cohort_summary_support import cohort_manifest_current
    receipt = json.loads((repo / 'data/cohort_registry_audit/summary_build_receipt.json').read_text())
    for key in ('source_cache_sha256', 'cohort_inputs_sha256'):
        for relative, expected in receipt[key].items():
            assert sha(repo / relative) == expected, relative
    for relative, entry in receipt['artifacts'].items():
        assert sha(repo / relative) == entry['sha256'], relative
    client = web.app.test_client()
    tested = set()
    scopes = {}
    for mode in ('yes', 'no'):
        base = repo / ('static/summary_plots' if target == 'metaNeuro' else 'summary') / mode / 'cohort_latest'
        manifest = json.loads((base / ('manifest.json' if target == 'metaNeuro' else 'summary_manifest.json')).read_text())
        assert cohort_manifest_current(manifest, repo), mode
        assert manifest['keep_latest_cohort'] is True
        if target == 'metaNeuro':
            response = client.get('/api/summary-plots', query_string={'latest_cohort':'yes', 'include_subexposures':mode})
            assert response.status_code == 200, response.data[:300]
            payload = response.get_json()
            assert payload['keep_latest_cohort'] is True
            scopes[mode] = len(payload['all_outcomes'])
            for url in set(urls(payload)):
                result = client.get(url)
                assert result.status_code == 200, url
                assert result.data == (repo / url.lstrip('/')).read_bytes(), url
                tested.add(url)
        else:
            scopes[mode] = len(manifest['scopes'])
            for source, data in manifest['sources'].items():
                for scope in data['scopes']:
                    for kind, endpoint in [('plots', 'plots'), ('workbooks', 'workbooks')]:
                        for key, entry in manifest['scopes'][scope].get(kind, {}).items():
                            if not entry.get('available'):
                                continue
                            url = f'/evidence/{source}/summary/{endpoint}/{scope}/{key}'
                            result = client.get(url, query_string={'latest_cohort':'yes', 'include_subexposures':mode})
                            assert result.status_code == 200, (url, result.data[:300])
                            assert result.data == (base / entry['filename']).read_bytes(), url
                            tested.add(mode + ':' + url)
    pdfs = []
    for relative in receipt['artifacts']:
        path = repo / relative
        if path.suffix != '.pdf':
            continue
        info = subprocess.run(['pdfinfo', str(path)], capture_output=True, text=True, check=True)
        assert 'Pages:' in info.stdout, relative
        pdfs.append(relative)
    result = {'repository':target, 'status':'passed', 'source_and_metadata_hashes_verified':True,
              'artifact_hashes_verified':len(receipt['artifacts']), 'http_asset_bytes_verified':len(tested),
              'scopes_by_mode':scopes, 'pdf_structure_verified':len(pdfs), 'no_model_calls':True}
    (WORK / ('final_' + target + '_output_verification.json')).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=('metaNeuro', 'metaUro'))
    main(parser.parse_args().target)
