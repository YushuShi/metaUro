"""Publish a reviewed correction for the VITAL alcohol/RCC estimate.

The immutable source snapshots and historical screening packets are inputs,
never output targets.  A new overlay selects the already saved full-text
Claude/Gemini estimate and recalculates only Alcohol's affected analyses.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from automation.sync_sources import assembled_source, load_overlay, read, sha, write

VERSION = 'vital-pooling-2026-10-22'
AUDIT = ROOT / 'evidence-revisions' / VERSION
STAGE = ROOT / 'updates/metaPee/versions' / VERSION / 'files'
PMID = '23665301'
CONTEXT = '23665301|Alcohol|Renal cell carcinoma'
EFFECT_ID = 'combined:8a689cf5a8541c61ff0b'
NULL_EFFECT_ID = 'combined:36a20c5b0336ef7683a2'
SOURCE_URL = 'https://pmc.ncbi.nlm.nih.gov/articles/PMC4420476/'
DIRECT = (
    'combined/alcohol/renal_cell_carcinoma_incidence_true_core.json',
    'combined/alcohol/renal_cell_carcinoma_incidence_true_all.json',
    'combined/provisional_ui/alcohol/renal_cell_carcinoma_incidence_true_core.json',
    'combined/provisional_ui/alcohol/renal_cell_carcinoma_incidence_true_all.json',
)
MODE_ROOT = 'combined/provisional_ui/subexposure_views/{mode}/Cached_results'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False).encode()).hexdigest()


def _source_fingerprint(hashes):
    # Matches scripts/provisional_disease_scopes.py::_encoded.
    encoded = json.dumps(hashes, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, indent=2) + '\n'
    return hashlib.sha256(encoded.encode()).hexdigest()


def _offline_requests():
    """Use pip's bundled requests only in a minimal offline build venv."""
    try:
        import requests  # noqa: F401
    except ImportError:
        import pip._vendor.requests as requests
        sys.modules['requests'] = requests


def _source():
    return assembled_source(ROOT, 'metaPee')


def _assert_numbers(row):
    expected = {'Effect Type': 'HR', 'Effect Size': 0.69,
        'Lower CI': 0.43, 'Upper CI': 1.11,
        'Sample Size': 77260, 'Cases': 249}
    for key, value in expected.items():
        if row.get(key) != value:
            raise ValueError(f'Unexpected saved VITAL {key}: {row.get(key)!r}')
    if row.get('Effect ID') != EFFECT_ID or str(row.get('PMID')) != PMID:
        raise ValueError('The selected VITAL estimate identity changed')


def _correct_record(packet):
    rows = [r for r in packet['results'] if r.get('context_id') == CONTEXT]
    if len(rows) != 1:
        raise ValueError('Expected one saved VITAL screening context')
    row = rows[0]
    if row.get('final_status') != 'ELIGIBLE_NOT_POOLABLE' or row.get('extraction') is not None:
        raise ValueError('VITAL screening decision changed before correction')
    candidates = row.get('key_review_candidates') or []
    selected = [r for r in candidates if r.get('source') == 'claude_gemini']
    if len(selected) != 1:
        raise ValueError('Expected one full-text Claude/Gemini VITAL candidate')
    extraction = copy.deepcopy(selected[0]['extraction'])
    for key, expected in {'effect_type': 'HR', 'effect_size': 0.69,
                          'ci_lower': 0.43, 'ci_upper': 1.11,
                          'total_n': 77260, 'cases': 249}.items():
        if extraction.get(key) != expected:
            raise ValueError(f'Unexpected full-text VITAL {key}')
    if (extraction.get('evidence') or {}).get('effect_size', {}).get('url') != SOURCE_URL:
        raise ValueError('Full-text evidence source changed')
    row.update(extraction=extraction, additional_extractions=[],
        final_status='INCLUDE_META', requires_key_review=False,
        missing_key_fields=[], invalid_key_fields=[],
        combined_numeric_resolved=True,
        poolability_reason='Reviewed full-text adjusted alcohol estimate is quantitatively eligible for pooling.',
        evidence_revision=VERSION,
        reviewed_pooling_resolution={
            'revision': VERSION, 'selected_source': 'claude_gemini',
            'candidate_history_retained': True,
            'selected_effect_id': EFFECT_ID, 'unselected_null_effect_id': NULL_EFFECT_ID,
            'source': SOURCE_URL, 'location': 'Table 2',
            'decision': 'Select the existing adjusted HR 0.69 (95% CI 0.43–1.11); retain the incomplete ChatGPT candidate unpooled.',
            'sample_size_note': '77,260 and 249 are whole-cohort totals; model-specific denominator and case count were not reported.',
            'p_value_note': 'The publication did not report a p value for this contrast; the saved p value is derived from its HR and CI.',
        })
    return {'before_status': 'ELIGIBLE_NOT_POOLABLE',
            'after_status': row['final_status'],
            'selected_extraction_sha256': _digest(extraction),
            'original_candidate_sha256': _digest(candidates),
            'selected_source': 'claude_gemini'}


def _correct_cache(packet):
    hits = [r for r in packet['studies'] if str(r.get('PMID')) == PMID]
    if {r.get('Effect ID') for r in hits} != {EFFECT_ID, NULL_EFFECT_ID}:
        raise ValueError('VITAL cache must retain exactly the two original candidate identities')
    row = next(r for r in hits if r['Effect ID'] == EFFECT_ID)
    _assert_numbers(row)
    if row.get('policy_poolable') is not False or row.get('Primary Estimate') is not False:
        raise ValueError('VITAL numeric candidate is already selected')
    row.update(policy_poolable=True, manual_poolable=True,
        scientific_pooling_eligible=True, **{'Primary Estimate': True},
        final_status='INCLUDE_META', requires_key_review=False,
        subexposure_pooling_selected=True, evidence_revision=VERSION,
        reviewed_pooling_resolution={
            'revision': VERSION, 'selected_source': 'claude_gemini',
            'source': SOURCE_URL, 'location': 'Table 2',
            'sample_size_note': 'Whole-cohort N and cases; model-specific counts unavailable.',
        })
    row.pop('display_only_reason', None)
    row.pop('subexposure_pooling_reason', None)
    suffix = ' · Claude+Gemini review candidate'
    if not str(row.get('Study') or '').endswith(suffix):
        raise ValueError('Saved VITAL candidate label changed')
    row['Study'] = row['Study'][:-len(suffix)]
    if 'disease_hierarchy_pooling_selected' in row:
        # Disease-parent caches are regenerated from direct rows below.  This
        # branch is used only for independent direct/cache copies.
        row['disease_hierarchy_pooling_selected'] = True
        row.pop('disease_hierarchy_pooling_reason', None)
    other = next(r for r in hits if r['Effect ID'] == NULL_EFFECT_ID)
    if other.get('policy_poolable') is not False or other.get('Effect Size') is not None:
        raise ValueError('Incomplete VITAL candidate must remain unpooled')
    other['requires_key_review'] = False
    other['display_only_reason'] = (
        'Incomplete abstract candidate; the reviewed full-text estimate from the same publication is selected for pooling.')
    other['reviewed_pooling_resolution'] = {
        'revision': VERSION, 'selected': False, 'selected_effect_id': EFFECT_ID,
        'source': SOURCE_URL, 'location': 'Table 2',
    }
    projection = packet.get('subexposure_projection')
    if (packet.get('disease') == 'Renal cell carcinoma' and
            isinstance(projection, dict) and isinstance(projection.get('pooling_rows'), int)):
        projection['pooling_rows'] += 1
    if isinstance(packet.get('disease_hierarchy_pooling_count'), int):
        packet['disease_hierarchy_pooling_count'] += 1
    packet['requires_key_review'] = any(r.get('requires_key_review') is True for r in packet['studies'])
    return {'effect_id': EFFECT_ID, 'null_effect_id': NULL_EFFECT_ID,
            'study_rows_preserved': len(packet['studies']),
            'selected_numeric_rows': sum(r.get('Effect ID') == EFFECT_ID and
                r.get('policy_poolable') is True for r in packet['studies'])}


def _immutable_hashes():
    manifest = read(ROOT / 'evidence-manifest.json')
    return {entry['path']: sha(ROOT / entry['path']) for entry in manifest['files']}


def prepare():
    from automation.autonomous_monthly import validate_completed_release
    prior = validate_completed_release(ROOT)
    if prior is None:
        raise ValueError('A completed metaUro release is required')
    if STAGE.exists() or AUDIT.exists():
        raise ValueError('The VITAL correction stage already exists')
    source = _source()
    sys.path[:0] = [str(ROOT / 'runtime_code/metaPee'), str(source)]
    previous = read(ROOT / 'updates/manifest.json')
    protected = _immutable_hashes()
    AUDIT.mkdir(parents=True)
    write(AUDIT / 'previous_overlay_index.json', previous)
    write(AUDIT / 'immutable_before.json', protected)
    write(AUDIT / 'parent_validation.json', prior)
    changes = []
    for relative in ('combined/effective_results.json',
                     'combined/provisional_ui/effective_results.json'):
        packet = read(source / relative)
        detail = _correct_record(packet)
        write(STAGE / relative, packet)
        changes.append({'path': relative, 'before_sha256': sha(source / relative),
                        'after_sha256': sha(STAGE / relative), **detail})
    for relative in DIRECT:
        packet = read(source / relative)
        detail = _correct_cache(packet)
        write(STAGE / relative, packet)
        changes.append({'path': relative, 'before_sha256': sha(source / relative),
                        'after_sha256': sha(STAGE / relative), **detail})
    # Correct the exact published parent rows in place.  Reprojecting an old
    # cache with today's code can add another historical estimate, so it is
    # deliberately not used for this one-publication scientific correction.
    for mode in ('no', 'yes'):
        relroot = MODE_ROOT.format(mode=mode)
        cache = source / relroot
        direct_rel = '_disease_scope_sources/alcohol/renal_cell_carcinoma_incidence_true_core.json'
        direct = read(cache / direct_rel)
        detail = _correct_cache(direct)
        write(STAGE / relroot / direct_rel, direct)
        changes.append({'path': relroot + '/' + direct_rel,
                        'before_sha256': sha(cache / direct_rel),
                        'after_sha256': sha(STAGE / relroot / direct_rel), **detail})
        source_hash = sha(STAGE / relroot / direct_rel)
        from scripts.subexposure_policy import eligible_for_selection
        from disease_hierarchy import eligible as disease_eligible
        for suffix in ('core', 'all'):
            for disease in ('renal_cell_carcinoma', 'kidney_and_renal_pelvis_cancer'):
                relative = f'alcohol/{disease}_incidence_true_{suffix}.json'
                before = read(cache / relative)
                packet = copy.deepcopy(before)
                detail = _correct_cache(packet)
                if [r.get('Effect ID') for r in packet['studies']] != [r.get('Effect ID') for r in before['studies']]:
                    raise ValueError('Published parent estimate identities or order changed')
                for old, new in zip(before['studies'], packet['studies']):
                    if old.get('PMID') != PMID and old != new:
                        raise ValueError('Unrelated parent evidence changed')
                selected = next(r for r in packet['studies'] if r.get('Effect ID') == EFFECT_ID)
                if not eligible_for_selection(selected) or not disease_eligible(selected):
                    raise ValueError('Reviewed VITAL estimate failed a saved scientific gate')
                materialization = packet.get('disease_hierarchy_materialization')
                if isinstance(materialization, dict):
                    materialization['source_hashes'][direct_rel] = source_hash
                    materialization['source_fingerprint'] = _source_fingerprint(materialization['source_hashes'])
                write(STAGE / relroot / relative, packet)
                changes.append({'path': relroot + '/' + relative,
                    'before_sha256': sha(cache / relative),
                    'after_sha256': sha(STAGE / relroot / relative), **detail})
        manifest_rel = relroot + '/_disease_scope_manifest.json'
        manifest = read(source / manifest_rel)
        manifest['selected_rows'] += 2
        for disease in ('renal_cell_carcinoma', 'kidney_and_renal_pelvis_cancer'):
            manifest['parents'][disease]['selected_rows'] += 1
        manifest['source_hashes'][direct_rel] = source_hash
        manifest['source_fingerprint'] = _source_fingerprint(manifest['source_hashes'])
        manifest['evidence_revision'] = VERSION
        write(STAGE / manifest_rel, manifest)
        changes.append({'path': manifest_rel, 'before_sha256': sha(source / manifest_rel),
                        'after_sha256': sha(STAGE / manifest_rel)})
        # The source-projection inventory counts direct contexts, not derived
        # disease parents.  Only the Alcohol/RCC direct context gains one row.
        subrel = relroot + '/_subexposure_manifest.json'
        packet = read(source / subrel)
        packet['counts']['pooling_rows'] += 1
        packet['evidence_revision'] = VERSION
        write(STAGE / subrel, packet)
        changes.append({'path': subrel, 'before_sha256': sha(source / subrel),
                        'after_sha256': sha(STAGE / subrel)})
    write(AUDIT / 'selection_audit.json', {'revision': VERSION,
        'context_id': CONTEXT, 'pmid': PMID,
        'selected_effect_id': EFFECT_ID, 'unselected_effect_id': NULL_EFFECT_ID,
        'primary_source': SOURCE_URL, 'source_location': 'Table 2',
        'human_instruction': 'Restore this eligible study to the poolable result.',
        'comparison': 'Highest alcohol category versus nondrinkers; adjusted HR 0.69 (0.43–1.11).',
        'cohort_totals': {'participants': 77260, 'rcc_cases': 249,
                          'model_specific_counts_available': False},
        'p_value': {'reported': None, 'saved_wald_derived': 0.12508036351027865},
        'changed_files': changes, 'new_search_performed': False,
        'new_screening_performed': False, 'model_calls': 0})
    # The existing release recalculator consumes this mapping and fills its
    # overlay inventory with every generated JSON and plot.
    write(AUDIT / 'source_release.json', {'metaPee': {'commit': 'reviewed-metaUro-vital-pooling',
        'overlay_files': {item['path']: {} for item in changes}}})
    print(f'Prepared {len(changes)} targeted evidence files', flush=True)


def results():
    if not (AUDIT / 'source_release.json').is_file():
        raise ValueError('Run prepare first')
    release = read(AUDIT / 'source_release.json')
    release['metaPee']['overlay_files'] = {name: entry
        for name, entry in release['metaPee']['overlay_files'].items()
        if name.endswith('.json')}
    write(AUDIT / 'source_release.json', release)
    _offline_requests()
    from automation import refresh_multicancer_results as refresh
    refresh.VERSION = VERSION
    refresh.AUDIT = AUDIT
    refresh.main('metaPee')
    # The source cache's analysis/plot URLs now have final bytes.  Bind the
    # parent projections and full source inventory to that final source hash.
    for mode in ('no', 'yes'):
        relroot = MODE_ROOT.format(mode=mode)
        direct_rel = '_disease_scope_sources/alcohol/renal_cell_carcinoma_incidence_true_core.json'
        direct_hash = sha(STAGE / relroot / direct_rel)
        for suffix in ('core', 'all'):
            for disease in ('renal_cell_carcinoma', 'kidney_and_renal_pelvis_cancer'):
                path = STAGE / relroot / 'alcohol' / f'{disease}_incidence_true_{suffix}.json'
                packet = read(path)
                materialization = packet.get('disease_hierarchy_materialization')
                if not isinstance(materialization, dict):
                    raise ValueError('Saved disease-parent source receipt is missing')
                materialization['source_hashes'][direct_rel] = direct_hash
                materialization['source_fingerprint'] = _source_fingerprint(materialization['source_hashes'])
                write(path, packet)
        path = STAGE / relroot / '_disease_scope_manifest.json'
        packet = read(path)
        packet['source_hashes'][direct_rel] = direct_hash
        packet['source_fingerprint'] = _source_fingerprint(packet['source_hashes'])
        write(path, packet)
    source = _source()
    manifest = read(source / 'combined/provisional_ui/manifest.json')
    for relative in manifest['protected_source_hashes']:
        if (STAGE / relative).is_file():
            manifest['protected_source_hashes'][relative] = sha(STAGE / relative)
    for mode in ('no', 'yes'):
        manifest['subexposure_modes'][mode]['pooling_rows'] += 1
        manifest['disease_parent_modes'][mode] = read(STAGE / MODE_ROOT.format(mode=mode) / '_disease_scope_manifest.json')
    manifest['evidence_revision'] = VERSION
    write(STAGE / 'combined/provisional_ui/manifest.json', manifest)
    audit = read(AUDIT / 'selection_audit.json')
    for item in audit['changed_files']:
        item['after_sha256'] = sha(STAGE / item['path'])
    write(AUDIT / 'selection_audit.json', audit)
    _register_stage_files()
    print('Recalculated affected saved pools and plots', flush=True)


def _register_stage_files():
    release = read(AUDIT / 'source_release.json')
    mapping = {}
    for path in sorted(STAGE.rglob('*')):
        if not path.is_file():
            continue
        relative = str(path.relative_to(STAGE))
        original = ROOT / 'sources/metaPee' / relative
        mapping[relative] = {'sha256': sha(path),
            'original_sha256': sha(original) if original.is_file() else None,
            'stored_path': str(path.relative_to(ROOT / 'updates/metaPee')),
            'run_id': VERSION, 'source_commit': 'reviewed-metaUro-vital-pooling'}
    release['metaPee']['overlay_files'] = mapping
    write(AUDIT / 'source_release.json', release)


def publish():
    if not read(AUDIT / 'source_release.json', {}).get('metaPee', {}).get('overlay_files'):
        raise ValueError('Run results first')
    if read(ROOT / 'updates/manifest.json') != read(AUDIT / 'previous_overlay_index.json'):
        raise ValueError('Active overlay changed during VITAL correction')
    index = read(ROOT / 'updates/manifest.json')
    for source in ('metaPee', 'MetaVir'):
        previous = load_overlay(ROOT, source)
        manifest = copy.deepcopy(previous)
        manifest['run_id'] = VERSION
        manifest['kind'] = 'evidence_scope_revision' if source == 'MetaVir' else 'scientific_adjudication_revision'
        manifest['new_search_performed'] = False
        manifest['new_screening_performed'] = False
        manifest['derived_from_overlay'] = copy.deepcopy(index['sources'][source])
        manifest['audit'] = str((AUDIT / 'selection_audit.json').relative_to(ROOT))
        manifest['audit_sha256'] = sha(AUDIT / 'selection_audit.json')
        if source == 'metaPee':
            manifest['files'].update(read(AUDIT / 'source_release.json')['metaPee']['overlay_files'])
        else:
            manifest['derived_from_prostate_scope_revision'] = previous.get('run_id')
        path = ROOT / 'updates' / source / 'versions' / VERSION / 'manifest.json'
        write(path, manifest)
        index['sources'][source] = {'manifest': str(path.relative_to(ROOT / 'updates')),
                                    'sha256': sha(path)}
    write(ROOT / 'updates/manifest.json', index)
    print('Activated reviewed VITAL overlay for local validation', flush=True)


def summaries():
    if load_overlay(ROOT, 'metaPee').get('run_id') != VERSION:
        raise ValueError('Run publish first')
    _offline_requests()
    from automation import refresh_multicancer_summaries as refresh
    refresh.VERSION = VERSION
    refresh.AUDIT = AUDIT
    refresh.SCOPES = {'metaPee': {'renal_cell_carcinoma',
                                  'kidney_and_renal_pelvis_cancer'}}
    refresh.main('metaPee')
    import summary_outputs
    for mode in ('no', 'yes'):
        path = ROOT / 'summary' / mode / 'summary_manifest.json'
        manifest = read(path)
        for source in ('MetaVir', 'metaPee'):
            manifest['sources'][source]['input_sha256'] = summary_outputs.input_fingerprint(ROOT, source, mode)
        manifest['evidence_revision'] = VERSION
        write(path, manifest)
    print('Rebuilt affected central summary workbooks and plots', flush=True)


def finalize():
    from automation.autonomous_monthly import validate_completed_release
    protected = read(AUDIT / 'immutable_before.json')
    if any(sha(ROOT / relative) != digest for relative, digest in protected.items()):
        raise ValueError('Immutable baseline source changed')
    state = read(ROOT / 'updates/monthly/state.json')
    monthly = read(ROOT / state['manifest_path'])
    for relative, digest in monthly['audit_artifact_sha256'].items():
        if sha(ROOT / relative) != digest:
            raise ValueError('Parent monthly audit changed')
    code = ('automation/restore_vital_pooling.py',
            'automation/autonomous_monthly.py', 'automation/sync_sources.py',
            'automation/refresh_multicancer_results.py',
            'automation/refresh_multicancer_summaries.py',
            'summary_outputs.py', 'study_selection.py',
            'runtime_code/manifest.json')
    summary = {str(path.relative_to(ROOT)): sha(path)
        for path in sorted((ROOT / 'summary').rglob('*'))
        if path.is_file() and path.suffix in {'.json', '.pdf', '.xlsx'}}
    write(AUDIT / 'release.json', {'schema_version': 1,
        'repository': 'metaUro', 'status': 'completed', 'revision_id': VERSION,
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'active_overlay_index': read(ROOT / 'updates/manifest.json'),
        'parent_monthly': {'manifest_path': state['manifest_path'],
            'manifest_sha256': state['manifest_sha256'], 'run_id': state['run_id'],
            'cutoff': state['search_through']['metaPee']},
        'parent_audit_artifact_sha256': monthly['audit_artifact_sha256'],
        'summary_sha256': summary,
        'code_sha256': {relative: sha(ROOT / relative) for relative in code},
        'selection_audit_sha256': sha(AUDIT / 'selection_audit.json'),
        'new_search_performed': False, 'new_screening_performed': False})
    verified = validate_completed_release(ROOT)
    if verified.get('evidence_revision') != VERSION:
        raise ValueError('Completed release does not identify the correction')
    write(AUDIT / 'immutable_after.json', {'files_verified': len(protected),
        'all_hashes_unchanged': True, 'parent_monthly_audit_unchanged': True})
    print('Completed VITAL correction release:', verified['evidence_revision'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'results', 'publish', 'summaries', 'finalize'))
    args = parser.parse_args()
    globals()[args.action]()
