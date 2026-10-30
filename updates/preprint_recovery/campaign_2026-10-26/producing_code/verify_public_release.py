"""Verify public provenance, source views and review queues before publication."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parent
REPOS = ['MetaVir', 'metaThyroid', 'metaGut', 'metaFemina', 'metaHeart', 'metaBreath',
         'metaPee', 'metaNeuro', 'MetaCutis_et_ Capillus', 'metaMeta/metaOnco', 'metaUro']
BODY_KEYS = {'abstract', 'abstracttext', 'full_text', 'fulltext', 'raw_output',
             'model_prompt', 'prompt', 'api_key', 'apikey'}
HTTP_KEYS = {'authorization', 'proxy-authorization', 'cookie', 'set-cookie'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish-report', action='store_true')
    args = parser.parse_args()
    findings, report, checked = [], {}, 0
    for repo in REPOS:
        root = ROOT/repo/('updates' if repo == 'metaUro' else 'data')/'preprint_recovery'
        campaign = root/'campaign_2026-10-26'
        for path in root.rglob('*.json'):
            if path.name == 'publication_sanitization_verification.json':
                continue
            checked += 1
            stack = [json.loads(path.read_text())]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, item in value.items():
                        prohibited = key.casefold() in BODY_KEYS or ('receipts' in path.parts and key.casefold() in HTTP_KEYS)
                        if prohibited and isinstance(item, str) and item.strip():
                            findings.append([repo, str(path.relative_to(root)), key])
                        stack.append(item)
                elif isinstance(value, list):
                    stack.extend(value)
                elif isinstance(value, str) and 'receipts' in path.parts:
                    if re.search(r'(?im)^\s*(?:set-cookie|cookie|authorization|proxy-authorization)\s*:', value):
                        findings.append([repo, str(path.relative_to(root)), 'raw_sensitive_header'])
        receipt = json.loads((root/'release_freeze_receipt.json').read_text())
        if (receipt['active_manifest_sha256'] != sha(root/'active_manifest.json')
                or receipt['model_cost_ledger_sha256'] != sha(campaign/'model_cost_and_attempts.json')):
            raise ValueError('Final release receipt does not bind current public data: '+repo)
        expected = json.loads((campaign/'application_source_manifest_hashes.json').read_text())[repo]['sha256']
        if expected != sha(root/'active_manifest.json'):
            raise ValueError('Cross-repository frozen source inventory changed: '+repo)
        source_export = campaign/'europepmc'
        file_manifest = json.loads((source_export/'public_export_manifest.json').read_text())
        files = file_manifest['files']
        for relative, descriptor in files.items():
            path = source_export/relative
            if not path.is_file() or sha(path) != descriptor['sha256']:
                raise ValueError('Sanitized public source receipt bytes changed: '+repo+'/'+relative)
        review_root = (ROOT/'metaMeta' if repo == 'metaMeta/metaOnco' else ROOT/repo)/'manualReview/preprint_recovery/2026-10-26'
        for name in ('discrepancies.csv', 'operational_holds.csv', 'source_unavailable.csv', 'source_lineage_holds.csv'):
            if not (review_root/name).is_file():
                raise ValueError('Required manual-review queue is absent: '+repo+'/'+name)
        report[repo] = {'active_manifest_sha256': sha(root/'active_manifest.json'),
            'source_export_files_verified': len(files), 'manual_review_queues_present': True}
    if findings:
        raise ValueError('Public data contains prohibited source/header fields: '+str(findings[:5]))
    result = {'status': 'passed', 'public_JSON_files_checked': checked, 'findings': 0,
        'private_article_bodies_and_model_prompts_published': False,
        'sensitive_HTTP_headers_published': False,
        'human_permission_authorization_metadata_is_not_an_HTTP_header': True,
        'verification_producer_sha256': sha(__file__), 'repositories': report}
    output = BASE/'publication_sanitization_verification.json'
    output.write_text(json.dumps(result, indent=2)+'\n')
    if args.publish_report:
        for repo in REPOS:
            folder = ROOT/repo/('updates' if repo == 'metaUro' else 'data')/'preprint_recovery/campaign_2026-10-26'
            shutil.copyfile(output, folder/'validation/publication_sanitization_verification.json')
            shutil.copyfile(__file__, folder/'producing_code/verify_public_release.py')
    print(json.dumps({'status': 'passed', 'JSON_files_checked': checked, 'repositories': len(REPOS)}))


if __name__ == '__main__':
    main()
