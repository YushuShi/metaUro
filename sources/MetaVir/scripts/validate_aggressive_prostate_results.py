#!/usr/bin/env python3
"""Validate published subtype membership and denominator invariants offline."""
import json
from pathlib import Path
import sys
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.aggressive_prostate_policy import AGGRESSIVE, GENERAL, key
from scripts import incidence_endpoint_policy


def read(path):
    return json.loads(path.read_text())


def group(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(str(row['PMID']), key(row.get('Direct Exposure')))].append(row)
    return grouped


def validate():
    review_root = ROOT/'data/aggressive_prostate_review'
    manifest = read(review_root/'manifest.json')
    assert manifest['complete'], 'Sol review is incomplete'
    assert set(manifest['model_returned_counts']) == {'gpt-5.6-sol'}, 'Unexpected review model'
    reviews = {r['pmid']: r for r in read(review_root/'review.json')['reviews']}
    case_document = read(review_root/'case_counts.json')
    assert case_document['complete'], 'Endpoint case-count review is incomplete'
    case_reviews = {r['row_id']: r for r in case_document['records']}
    endpoint_checkpoint = incidence_endpoint_policy.load_checkpoint()
    expected_endpoint_records = {
        incidence_endpoint_policy.record_id(record)
        for record in endpoint_checkpoint['records']
    }
    endpoint_records_by_root = {}
    counts = {}
    for directory in ['Cached_results', 'combined', 'Subexposure_results/yes', 'Subexposure_results/no']:
        count = {'scopes': 0, 'aggressive_rows': 0, 'aggressive_pmids': set(),
                 'paired_prospective_groups': 0,
                 'preserved_nonpoolable_rows_without_review_metadata': 0}
        endpoint_records = set()
        for path in sorted((ROOT/directory).glob('*/*_incidence_true_all.json')):
            payload = read(path)
            for row in payload.get('studies', []):
                assert incidence_endpoint_policy.matching_record(row, endpoint_checkpoint) is None, (
                    f'Non-diagnosis endpoint remained active: {directory}, {path}, '
                    f"{row.get('PMID')}, {row.get('Effect ID')}"
                )
            endpoint_records.update(
                record['record_id']
                for record in (payload.get('incidence_endpoint_exclusions') or {}).get('records', [])
            )
        for path in sorted((ROOT/directory).glob('*/aggressive_prostate_cancer_incidence_true_all.json')):
            child = read(path)
            parent = read(path.with_name('prostate_cancer_incidence_true_all.json'))
            core = read(path.with_name(path.name.replace('_all.json', '_core.json')))
            assert child['studies'] == core['studies'], path
            assert child['cancer'] == AGGRESSIVE, path
            assert parent['cancer'] == GENERAL, path
            assert not child.get('headline') or child['headline']['baseline_risk'] is None, path
            pg, cg = group(parent['studies']), group(child['studies'])
            for identity, children in cg.items():
                assert identity in pg, f'Child article missing from parent: {directory}, {identity}'
                generals = [r for r in pg[identity] if r.get('Disease Endpoint') == 'general']
                if generals:
                    assert not any(r.get('Disease Endpoint') == 'aggressive' for r in pg[identity]), f'Duplicate endpoint inheritance: {identity}'
                if generals and reviews.get(identity[0], {}).get('prospective_cohort') is True:
                    assert len({r.get('Sample Size') for r in generals + children}) == 1, f'Unequal cohort N: {identity}'
                    count['paired_prospective_groups'] += 1
                for row in children:
                    assert row['Disease Endpoint'] == 'aggressive', path
                    assert row.get('Estimated Cases') is None, path
                    source = row.get('disease_scope_review')
                    if source:
                        assert source.get('model') == 'gpt-5.6-sol', path
                        if source.get('origin') == 'saved_estimate':
                            confirmation = case_reviews.get(source['source_row_id'], {})
                            expected = confirmation.get('endpoint_cases') if confirmation.get('status') == 'confirmed' else None
                            assert row.get('Cases') == expected, f'Wrong aggressive case count: {identity}'
                    else:
                        # A handful of newer combined-cache display rows were
                        # already scoped before the frozen review metadata was
                        # projected.  They are retained only when explicitly
                        # non-poolable; this focused endpoint replay must not
                        # fabricate review provenance for them.
                        assert isinstance(row.get('disease_scope_original_row'), dict), path
                        assert row.get('display_only') is True, path
                        assert row.get('pool_in_parent_meta_analysis') is False, path
                        assert row.get('disease_scope_pooling_selected') is False, path
                        count['preserved_nonpoolable_rows_without_review_metadata'] += 1
                    count['aggressive_pmids'].add(str(row['PMID']))
            count['scopes'] += 1
            count['aggressive_rows'] += len(child['studies'])
        assert count['aggressive_rows'], f'No aggressive evidence in {directory}'
        count['aggressive_pmids'] = len(count['aggressive_pmids'])
        counts[directory] = count
        endpoint_records_by_root[directory] = sorted(endpoint_records)
    assert set(endpoint_records_by_root['Cached_results']).union(
        endpoint_records_by_root['combined']) == expected_endpoint_records, (
        'Primary incidence projections do not cover every endpoint exclusion record'
    )
    report = {'passed': True, 'model': 'gpt-5.6-sol',
              'reviewed_articles': manifest['reviewed_article_count'],
              'abstracts_available': manifest['abstract_available_count'],
              'checks': ['all/core agreement', 'aggressive article membership in parent',
                         'separate general estimates prevent duplicate inheritance',
                         'paired prospective cohort N equality',
                         'no inherited general baseline or estimated aggressive cases',
                         'saved aggressive case counts match endpoint-specific Sol confirmation',
                         'pre-existing nonpoolable display rows preserved without fabricated review metadata',
                         'reviewed death/progression endpoints absent from active studies'],
              'incidence_endpoint_policy': {
                  'version': incidence_endpoint_policy.VERSION,
                  'record_count': len(expected_endpoint_records),
                  'records_by_root': endpoint_records_by_root,
              },
              'tracks': counts}
    (review_root/'validation_report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    validate()
