"""Provenance for additive latest-cohort summary releases."""
import hashlib
import json
from pathlib import Path


def cohort_record_metadata_sha256(repo_root):
    path = Path(repo_root) / 'data/cohort_record_metadata.json'
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def cohort_policy_sha256(repo_root):
    path = Path(repo_root) / 'cohort_identity.py'
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def cohort_registry_sha256(repo_root):
    path = Path(repo_root) / 'data/cohort_registry.json'
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def cohort_summary_audit(datasets):
    result = {}
    for dataset, rows in datasets.items():
        audits = {str(row.get('Exposure', row.get('exposure', i))): row.get('cohort_selection') or {}
                  for i, row in enumerate(rows)}
        result[dataset] = {'exposures': audits, 'totals': {
            key: sum(int(audit.get(key, 0)) for audit in audits.values())
            for key in ['input_rows', 'retained_rows', 'excluded_rows', 'unknown_cohort_rows', 'unknown_date_rows']}}
    return result


def bind_cohort_manifest(manifest, repo_root, enabled=True):
    manifest['keep_latest_cohort'] = bool(enabled)
    if enabled:
        manifest['cohort_registry_sha256'] = cohort_registry_sha256(repo_root)
        manifest['cohort_record_metadata_sha256'] = cohort_record_metadata_sha256(repo_root)
        manifest['cohort_policy_sha256'] = cohort_policy_sha256(repo_root)
        manifest['cohort_policy'] = 'newest definitely dated publication within each poolable comparison; older publications sharing any named cohort with a retained newer publication excluded; multi-cohort estimates kept whole; numbered cohorts separate; unknowns and ties retained; all source evidence preserved'
    return manifest


def cohort_manifest_current(manifest, repo_root):
    return (isinstance(manifest, dict) and manifest.get('keep_latest_cohort') is True
            and 'cohort_registry_sha256' in manifest
            and manifest['cohort_registry_sha256'] == cohort_registry_sha256(repo_root)
            and 'cohort_record_metadata_sha256' in manifest
            and manifest['cohort_record_metadata_sha256'] == cohort_record_metadata_sha256(repo_root)
            and 'cohort_policy_sha256' in manifest
            and manifest['cohort_policy_sha256'] == cohort_policy_sha256(repo_root))


def validate_cohort_summary_file(filepath, payload, default, repo_root, enabled):
    path = Path(filepath)
    if not enabled or 'cohort_latest' not in path.parts:
        return payload
    if path.name in {'summary_manifest.json', 'parent_summary_manifest.json', 'subcategory_summary_manifest.json'}:
        return payload if cohort_manifest_current(payload, repo_root) else default
    if path.name == 'workbook_manifest.json':
        for name in ['parent_summary_manifest.json', 'summary_manifest.json']:
            manifest_path = path.parent / name
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text())
                return payload if cohort_manifest_current(manifest, repo_root) else default
        return default
    return payload
