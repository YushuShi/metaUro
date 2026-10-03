"""Stage an offline, scoped status/queue refresh from saved pipeline evidence."""
from __future__ import annotations
import argparse
import copy
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import current_comparison_adapter as adapter
from scripts import nonpoolable_status_retention as retention
from scripts import subexposure_policy as hierarchy_policy
from scripts.display_eligibility import display_row


SOURCE_HASHES = {}

def read(path):
    raw = path.read_bytes()
    SOURCE_HASHES[str(path.resolve())] = hashlib.sha256(raw).hexdigest()
    return json.loads(raw.decode('utf-8'))


def stage_refresh(repo, output):
    tracks, _, _, source_paths = adapter.current_tracks()
    tracks = {name.lower(): rows for name, rows in tracks.items()}
    packet = read(repo / 'combined/final_screening.json')
    original = {row['context_id']: row for row in packet['results']}
    review_status = read(repo / 'combined/human_review/review_status.json')
    csv_path = repo / 'combined/human_review' / review_status['file']
    with csv_path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        columns, review_rows = reader.fieldnames, list(reader)
    protected = {row['Context ID'] for row in review_rows if row.get('Manual review results', '').strip()}
    manual = read(repo / 'combined/_manual_overrides.json')
    protected.update(row['context_id'] for row in manual.get('overrides', []))
    from scripts import key_comparison_policy as comparison
    both_contexts = {cid for cid in set(tracks['chatgpt']) & set(tracks['claude_gemini'])
                     if (comparison.nonpoolable_status_resolution(tracks['chatgpt'][cid], tracks['claude_gemini'][cid]) or {}).get('resolution_kind') == 'both_eligible_not_poolable'}
    both_tracks = {name: {cid: row for cid, row in rows.items() if cid in both_contexts} for name, rows in tracks.items()}
    resolved_records, contexts = retention.apply_records(original, both_tracks, protected)
    targets = set(contexts)
    changes = []
    generated_manifest = []

    def write(relative, value):
        source, target = repo / relative, output / relative
        text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str) + '\n'
        if source.is_file() and source.read_text(encoding='utf-8') == text:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        changes.append(relative)
        generated_manifest.append({'repo': 'MetaVir', 'relative_path': relative,
            'original_sha256': SOURCE_HASHES.get(str(source.resolve()), hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None),
            'staged_sha256': hashlib.sha256(target.read_bytes()).hexdigest()})

    updated = copy.deepcopy(packet)
    updated['results'] = [resolved_records[row['context_id']] for row in packet['results']]
    updated['counts'] = dict(Counter(row.get('final_status') for row in updated['results']))
    updated['nonpoolable_status_retention_version'] = retention.VERSION
    write('combined/final_screening.json', updated)
    removed = [row for row in review_rows if row['Context ID'] in targets and not row.get('Manual review results', '').strip()]
    removed_ids = {row['Review ID'] for row in removed}

    for relative in ('combined/_pending_review.json', 'combined/_adjudication_ledger.json'):
        data = read(repo / relative)
        if '_pending' in relative:
            data['records'] = [row for row in data['records'] if row.get('context_id') not in targets]
        else:
            for row in data['records']:
                if row.get('context_id') in targets:
                    row.update(merge_action='retained_eligible_not_poolable', pending_reason='',
                               combined_value='ELIGIBLE_NOT_POOLABLE', manual_review_required=False,
                               nonpoolable_status_retention_version=retention.VERSION)
        write(relative, data)
    for relative in ('combined/provisional_ui/pending_manifest.json',):
        if not (repo / relative).exists():
            continue
        data = read(repo / relative)
        data['rows'] = [row for row in data['rows'] if row.get('id') not in removed_ids]
        data['nonpoolable_status_retention_version'] = retention.VERSION
        write(relative, data)
    relative = 'combined/provisional_ui/effective_screening.json'
    if (repo / relative).exists():
        data = read(repo / relative)
        for index, row in enumerate(data['results']):
            context = row.get('provisional_origin_context_id') or row.get('context_id')
            if context in targets:
                data['results'][index] = copy.deepcopy(resolved_records[context])
        from scripts.astra_fulltext_common import digest
        data['authoritative_results_digest'] = digest(resolved_records)
        data['nonpoolable_status_retention_version'] = retention.VERSION
        write(relative, data)
    for name in ('application_audit.json', 'review_dispositions.json', 'review_audit.json'):
        relative = 'combined/provisional_ui/' + name
        if not (repo / relative).exists():
            continue
        data = read(repo / relative)
        rows = data if isinstance(data, list) else data.get('rows', data.get('results', []))
        for row in rows:
            if row.get('id') in removed_ids or row.get('context_id') in targets:
                row.update(pending=False, manual_review_required=False, effective_poolable=False,
                           reason=retention.BOTH_REASON, resolution=retention.VERSION)
        write(relative, data)

    def context_for(row):
        return (row.get('context_id') or row.get('provisional_origin_context_id') or
                '|'.join([str(row.get('PMID') or ''), str(row.get('Direct Exposure') or ''), 'Prostate cancer']))

    hierarchy = read(repo / 'data/exposure_hierarchy.json')
    direct_new = defaultdict(list)
    source_rows = {}
    for context in contexts:
        source_rows[context] = [display_row(item) for item in retention.display_records(resolved_records[context])]
        for row in source_rows[context]:
            row.update({'Disease Endpoint': 'general', 'Disease Scope': 'Prostate cancer',
                        'disease_scope_pooling_selected': False, 'disease_scope_pooling_reason': retention.BOTH_REASON})
            direct_new[row['Direct Exposure']].append(row)
    cache_count = row_count = added_count = 0
    cache_paths = sorted((repo / 'combined').glob('*/*_true_*.json'))
    cache_paths += sorted((repo / 'Subexposure_results').glob('*/*/*_true_*.json'))
    for path in cache_paths:
        data = read(path)
        if not isinstance(data.get('studies'), list):
            continue
        previous = copy.deepcopy(data)
        touched = False
        replacement = []
        replaced_contexts = set()
        for row in data['studies']:
            context = context_for(row)
            if context in targets:
                if (row.get('subexposure_pooling_selected') is True and
                        row.get('disease_scope_pooling_selected') is not False and row.get('display_only') is not True):
                    raise ValueError('A pooled target requires numerical recalculation: ' + str(path))
                row_count += 1
                touched = True
                if context in replaced_contexts:
                    continue
                replaced_contexts.add(context)
                routing = {key: copy.deepcopy(value) for key, value in row.items()
                           if key in {'Displayed Exposure', 'Hierarchy Inherited', 'Disease Endpoint', 'Disease Scope',
                                      'include_subexposures', 'subexposure_source', 'subexposure_inherited'}
                           or key.startswith('aggressive_prostate_')}
                for fresh in source_rows[context]:
                    fresh = dict(copy.deepcopy(fresh), **routing)
                    if 'disease_scope_original_row' in row:
                        normative_routing = {key: copy.deepcopy(value) for key, value in row['disease_scope_original_row'].items()
                                             if key in {'Displayed Exposure', 'Hierarchy Inherited', 'Disease Endpoint', 'Disease Scope',
                                                        'include_subexposures', 'subexposure_source', 'subexposure_inherited'}
                                             or key.startswith('aggressive_prostate_')}
                        fresh['disease_scope_original_row'] = dict(copy.deepcopy(fresh), **normative_routing)
                    replacement.append(retention.decorate_study(fresh))
            else:
                replacement.append(row)
        data['studies'] = replacement
        # Newly retained studies enter existing general-disease scopes using the
        # same hierarchy projection, with all pooling gates false.
        if str(data.get('disease') or data.get('cancer')).casefold() == 'prostate cancer':
            mode = path.relative_to(repo).parts[1] if path.relative_to(repo).parts[0] == 'Subexposure_results' else 'yes'
            candidates = hierarchy_policy.project_rows(direct_new, data['exposure'], hierarchy, include=mode)
            present = {context_for(row) for row in data['studies']}
            for row in candidates:
                if context_for(row) not in present:
                    data['studies'].append(retention.decorate_study(row))
                    added_count += 1
                    touched = True
        if not touched:
            continue
        # No numerical inputs selected for pooling have changed in MetaVir.
        for key in ('headline', 'summary_html', 'plot_url', 'funnel_plot_url', 'baujat_plot_url'):
            assert data.get(key) == previous.get(key), (path, key)
        if isinstance(data.get('subexposure_projection'), dict):
            data['subexposure_projection']['table_rows'] = len(data['studies'])
        if isinstance(data.get('screening_stats'), dict):
            data['screening_stats']['extracted'] = len(data['studies'])
        data['nonpoolable_status_retention_version'] = retention.VERSION
        write(str(path.relative_to(repo)), data)
        cache_count += 1

    # A retained direct exposure can have had no prior cache at all. Use the
    # standard builder over every applicable canonical record for that scope.
    from scripts import build_combined_results as builder
    from scripts import monthly_evidence_update as monthly
    import meta_analysis
    build_manifest = read(repo / 'combined/_merge_manifest.json')
    new_scopes = []
    statuses = Counter(item.get('final_status') for item in resolved_records.values())
    statistics = {'total_fetched': len({item.get('pmid') for item in resolved_records.values()}),
                  'after_prefilter': len(resolved_records), 'prefilter_skip': {},
                  'llm_screened_in': statuses['INCLUDE_META'] + statuses['ELIGIBLE_NOT_POOLABLE'],
                  'llm_screened_out': statuses['EXCLUDE'], 'consensus_bypassed': 0}
    for exposure in direct_new:
        slug = hierarchy_policy.key(exposure)
        relative = f'combined/{slug}/prostate_cancer_incidence_true_all.json'
        if (repo / relative).exists():
            continue
        accepted_exposures = {hierarchy_policy.key(label) for label in
                              [exposure, *hierarchy_policy.descendants(exposure, hierarchy)]}
        scoped = {cid: item for cid, item in resolved_records.items()
                  if hierarchy_policy.key(item.get('direct_exposure') or item.get('exposure')) in accepted_exposures}
        grouped, _, _ = builder.build_grouped_studies(scoped, {}, set(scoped), monthly)
        rows = grouped.get(exposure, [])
        sources = defaultdict(list)
        for row in rows:
            row.update({'Disease Endpoint': 'general', 'Disease Scope': 'Prostate cancer'})
            sources[row['Direct Exposure']].append(row)
        for mode in (None, 'yes', 'no'):
            projected = rows if mode is None else hierarchy_policy.project_rows(sources, exposure, hierarchy, include=mode)
            payload = builder.analysis_fields(projected, 'Prostate cancer', exposure, statistics,
                                               meta_analysis, generate_plots=False, build_id=build_manifest['build_id'])
            payload.update(exposure=exposure, cancer='Prostate cancer', disease='Prostate cancer', outcome='Incidence',
                           canonical_track='combined', combined_projection={'schema_version': 1, 'build_id': build_manifest['build_id']},
                           nonpoolable_status_retention_version=retention.VERSION)
            if mode:
                payload.update(include_subexposures=mode, subexposure_projection={'mode': mode, 'table_rows': len(projected),
                    'pooling_rows': len(hierarchy_policy.pooling_rows(projected)), 'policy': 'transitive-child-closure-distinct-populations-v1'})
            assert payload['headline'] is None, 'A newly materialized pooled scope requires its summary exports: ' + exposure
            base = 'combined' if mode is None else 'Subexposure_results/' + mode
            for suffix in ('all', 'core'):
                relative = f'{base}/{slug}/prostate_cancer_incidence_true_{suffix}.json'
                if (repo / relative).exists():
                    continue
                write(relative, payload)
                cache_count += 1
                added_count += len(projected)
        new_scopes.append(exposure)

    remaining = [row for row in review_rows if row['Review ID'] not in removed_ids]
    filename = 'metaVir_human_manual_review_2026-09-08_both_nonpoolable.csv'
    relative = 'combined/human_review/' + filename
    target = output / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(remaining)
    changes.append(relative)
    generated_manifest.append({'repo': 'MetaVir', 'relative_path': relative, 'original_sha256': None,
        'staged_sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    status = read(repo / 'combined/human_review/review_status.json')
    status.update(file=filename, review_rows=len(remaining), articles=len({row['PMID'] for row in remaining}),
                  csv_sha256=hashlib.sha256(target.read_bytes()).hexdigest(), policy_version=retention.VERSION,
                  temporary_pooling_rows=sum(row['Temporary pooling eligible'] == 'Yes' for row in remaining))
    status['temporary_pipeline_counts'] = dict(Counter(row['Temporary UI pipeline'] for row in remaining))
    write('combined/human_review/review_status.json', status)
    report = {'alias': 'metaVir', 'folder': 'MetaVir', 'version': retention.VERSION,
              'resolved_contexts': contexts, 'removed_review_ids': sorted(removed_ids),
              'before_rows': len(review_rows), 'after_rows': len(remaining),
              'before_pmids': len({row['PMID'] for row in review_rows}),
              'after_pmids': len({row['PMID'] for row in remaining}), 'new_csv': relative,
              'cache_files_changed': cache_count, 'existing_rows_marked_nonpoolable': row_count,
              'display_rows_added': added_count, 'pooled_results_unchanged': True,
              'new_direct_scopes': new_scopes,
              'source_sha256': {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in source_paths.items()},
              'model_calls': 0}
    report['both_source_contexts'] = len(both_contexts)
    report['protected_contexts'] = sorted(both_contexts - targets)
    report['selected_pipeline_counts'] = dict(Counter(resolved_records[c]['nonpoolable_status_resolution']['retained_track'] for c in contexts))
    report['equal_information_chatgpt_contexts'] = sum(resolved_records[c]['nonpoolable_status_resolution']['equal_information'] for c in contexts)
    write('combined/_both_nonpoolable_status_retention.json', report)
    for name in ('_merge_manifest.json', '_current_comparison.json'):
        relative = 'combined/' + name
        data = read(repo / relative)
        data['both_nonpoolable_status_retention'] = {key: value for key, value in report.items()
                                               if key not in ('resolved_contexts', 'removed_review_ids')}
        if name == '_merge_manifest.json':
            data['counts']['combined_statuses'] = updated['counts']
            pending = read(output / 'combined/_pending_review.json')['records']
            data['counts'].update(pending_fields=len(pending), pending_contexts=len({row['context_id'] for row in pending}))
        write(relative, data)
    (output / 'generated_manifest.json').write_text(json.dumps(generated_manifest, indent=2) + '\n')
    (output / 'resolution_report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key not in ('resolved_contexts', 'removed_review_ids', 'source_sha256')}, indent=2))
    return changes


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=ROOT)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    if args.output_root.resolve() == args.repo_root.resolve():
        parser.error('Use a separate staging directory for a reviewable refresh.')
    stage_refresh(args.repo_root, args.output_root)
