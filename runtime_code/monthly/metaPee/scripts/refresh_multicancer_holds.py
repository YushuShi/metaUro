"""Apply a stricter registry hold to this release's rows, then refresh their pools.

This never changes a primary effect or an older screening record. It is useful
when final cohort-overlap review adds a restriction after the initial replay.
"""
from __future__ import annotations
import copy
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import apply_multicancer_evidence as build
from scripts import multicancer_evidence as review


def main():
    import requests
    requests.sessions.Session.request = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('Offline rebuild'))
    held = {e['id']: e for e in review.registry()['entries'] if e.get('hold')}
    changed = []
    memo = {}
    for path in sorted((ROOT / 'combined').rglob('*.json')):
        if any(part in {'astra_adjudication', 'astra_fulltext', '_history', 'outputs'} for part in path.parts):
            continue
        if path.name not in {'effective_results.json'} and not path.name.endswith(('_core.json', '_all.json')):
            continue
        payload = build.read(path)
        before = copy.deepcopy(payload)
        for row in payload.get('studies', []):
            entry = held.get(row.get('source_review_id'))
            if entry is None:
                continue
            expected = entry['row']
            for field in ('PMID', 'Effect Size', 'Lower CI', 'Upper CI', 'Sample Size', 'Cases', 'Gender', 'Direct Exposure'):
                assert row.get(field) == expected.get(field), (path, field)
            for field in ('policy_poolable', 'scientific_pooling_eligible', 'manual_poolable', 'pool_in_parent_meta_analysis', 'Primary Estimate', 'display_only', 'display_only_reason', 'final_status', 'overlap_review', 'combined_source_extraction'):
                if field in expected:
                    row[field] = copy.deepcopy(expected[field])
            row['subexposure_pooling_selected'] = False
            if 'disease_hierarchy_pooling_selected' in row:
                row['disease_hierarchy_pooling_selected'] = False
        for index, record in enumerate(payload.get('results', [])):
            entry = held.get(record.get('context_id'))
            if entry is None:
                continue
            for field in ('pmid', 'disease', 'exposure', 'jbi'):
                assert record.get(field) == entry['record'].get(field), (path, field)
            for field in ('effect_size', 'ci_lower', 'ci_upper', 'total_n', 'cases', 'gender'):
                assert record['extraction'].get(field) == entry['record']['extraction'].get(field), (path, field)
            payload['results'][index] = copy.deepcopy(entry['record'])
        if payload == before:
            continue
        old_hash = build.sha(path)
        if isinstance(payload.get('studies'), list):
            mode = 'yes' if '/subexposure_views/yes/' in str(path) else 'no'
            build.calculate(path, {'payload': payload, 'mode': mode}, memo)
        else:
            from collections import Counter
            for field in ('counts', 'status_counts'):
                if field in payload:
                    payload[field] = dict(Counter(r.get('final_status') for r in payload['results']))
        build.write(path, payload)
        changed.append({'path': str(path.relative_to(ROOT)), 'before_sha256': old_hash, 'after_sha256': build.sha(path)})
        print(path.relative_to(ROOT), flush=True)
    report = build.read(review.DIRECTORY / 'application_report.json')
    report['poolable_additions'] = sum(e['row']['policy_poolable'] for e in review.registry()['entries'])
    report['final_overlap_refresh'] = changed
    build.write(review.DIRECTORY / 'application_report.json', report)
    build.write(review.DIRECTORY / 'overlap_refresh.json', {'held_contexts': list(held), 'changed_files': changed, 'unique_calculations': len(memo), 'network_calls': 0})


if __name__ == '__main__':
    main()
