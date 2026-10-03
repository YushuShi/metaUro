#!/usr/bin/env python3
"""Apply saved Sol abstract reviews to both incidence tracks, without model calls."""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import aggressive_prostate_policy as policy
from scripts import build_combined_results as combined
from scripts import incidence_endpoint_policy


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, payload):
    from scripts.monthly_evidence_update import json_compatible
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_compatible(payload), indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')


def analysis_signature(rows):
    fields = ['PMID', 'Effect ID', 'Effect Type', 'Effect Size', 'Lower CI', 'Upper CI',
              'Sample Size', 'Cases', 'Estimated Cases', 'display_only', 'Quality Score',
              'pool_in_parent_meta_analysis', 'manual_poolable', 'policy_poolable',
              'subexposure_pooling_selected', 'Disease Endpoint',
              'disease_scope_pooling_selected']
    return json.dumps([{k: r.get(k) for k in fields} for r in rows], sort_keys=True)


def rebuild(payload, rows, disease, review_digest, generate_plots):
    import meta_analysis
    frame = combined.numeric_frame(rows)
    result = {}
    if rows:
        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter('ignore')
            namespace = (
                'chatgpt/incidence_endpoint_v1'
                if payload['canonical_track'] == 'chatgpt'
                else 'incidence_endpoint_v1'
            )
            result = meta_analysis.perform_meta_analysis(frame, disease, payload['exposure'],
                outcome='Incidence', exclude_meta=True, df_all=frame,
                generate_plots=generate_plots,
                plot_namespace=namespace)
        if result.get('error'):
            from scripts.subexposure_policy import eligible_for_selection
            if any(eligible_for_selection(r) for r in rows):
                raise RuntimeError(f"{disease}/{payload['exposure']}: {result['error']}")
    rebuilt = copy.deepcopy(payload)
    for field in ['headline', 'plot_url', 'funnel_plot_url', 'baujat_plot_url']:
        rebuilt[field] = result.get(field)
    rebuilt['summary_html'] = result.get('summary_html') or '<p>Eligible evidence is listed below. No studies meet the default pooling criteria.</p>'
    if disease == policy.GENERAL and any(r.get('Disease Endpoint') == 'aggressive' and r.get('disease_scope_pooling_selected') for r in rows):
        if rebuilt.get('headline'):
            rebuilt['headline']['effect_conversion_method'] = 'per_endpoint_with_identity_approximation'
        rebuilt['summary_html'] = '<p>Inherited aggressive endpoints use HR ≈ RR ≈ OR because an aggressive incidence baseline is unavailable.</p>' + rebuilt['summary_html']
    for field in ['forest_plots', 'forest_plot', 'canonical_rr', 'analysis_settings']:
        rebuilt.pop(field, None)
    if disease == policy.AGGRESSIVE and result.get('headline'):
        rebuilt['summary_html'] = ('<p>No matching published aggressive prostate cancer baseline is available. '
            'These saved summaries use the HR ≈ RR ≈ OR identity approximation; provide incidence in Settings for baseline-dependent calculations.</p>' + rebuilt['summary_html'])
    rebuilt.update(studies=rows, cancer=disease, disease=disease, outcome='Incidence')
    if isinstance(rebuilt.get('screening_stats'), dict):
        rebuilt['screening_stats']['extracted'] = len(rows)
    return rebuilt


def build(review_root=ROOT/'data/aggressive_prostate_review', cache_roots=None, generate_plots=True):
    from scripts.aggressive_prostate_review import evidence_row_id
    require_complete_endpoint_checkpoint = cache_roots is None
    cache_roots = cache_roots or [ROOT/'Cached_results', ROOT/'combined']
    review_path = review_root/'review.json'
    document = read(review_path)
    if document.get('complete') is False:
        raise ValueError('Finish the Sol abstract review before projecting disease scopes.')
    reviews = {str(r['pmid']): r for r in document['reviews']}
    case_path = review_root/'case_counts.json'
    case_document = read(case_path) if case_path.exists() else None
    if case_document is not None and case_document.get('complete') is False:
        raise ValueError('Finish the aggressive endpoint case-count review before projecting.')
    case_reviews = {r['row_id']: r for r in case_document['records']} if case_document is not None else {}
    case_digest = hashlib.sha256(case_path.read_bytes()).hexdigest() if case_path.exists() else None
    review_digest = hashlib.sha256(review_path.read_bytes()).hexdigest()
    endpoint_checkpoint = incidence_endpoint_policy.load_checkpoint()
    endpoint_checkpoint_digest = incidence_endpoint_policy.file_sha256(
        incidence_endpoint_policy.DEFAULT_CHECKPOINT)
    expected_endpoint_records = {
        incidence_endpoint_policy.record_id(record)
        for record in endpoint_checkpoint['records']
    }
    endpoint_pmids = {str(record['pmid']) for record in endpoint_checkpoint['records']}
    matched_endpoint_records = set()
    audit = {'policy_version': policy.POLICY_VERSION, 'model': 'gpt-5.6-sol',
             'review_sha256': review_digest, 'case_review_sha256': case_digest,
             'incidence_endpoint_policy': {
                 'version': incidence_endpoint_policy.VERSION,
                 'checkpoint_sha256': endpoint_checkpoint_digest,
                 'record_count': len(expected_endpoint_records),
             },
             'new_model_calls': 0, 'tracks': {}, 'actions': []}
    with tempfile.TemporaryDirectory(prefix='metavir-aggressive-') as tmp:
        stage = Path(tmp)
        targets = []
        for cache_root in cache_roots:
            track = 'chatgpt' if cache_root.name == 'Cached_results' else 'combined'
            track_counts = {'parent_rows': 0, 'aggressive_rows': 0, 'aggressive_pmids': set(), 'exposures_with_aggressive_evidence': 0}
            parent_paths = sorted(cache_root.glob('*/prostate_cancer_incidence_true_all.json'))
            for source in parent_paths:
                base = read(source)
                child_path = source.with_name('aggressive_prostate_cancer_incidence_true_all.json')
                previous_child = read(child_path) if child_path.exists() else {}
                preserved_excluded_sources = []
                for prior_payload in (base, previous_child):
                    prior_endpoint_state = prior_payload.get('incidence_endpoint_exclusions') or {}
                    preserved_excluded_sources.extend(
                        prior_endpoint_state.get('source_rows') or [])
                originals = policy.source_rows(
                    base.get('studies', []) + previous_child.get('studies', []),
                    preserved_excluded_sources)
                # A focused human checkpoint must not rewrite unrelated cache
                # scopes merely because newer monthly rows postdate the frozen
                # aggressive-disease review.
                if (require_complete_endpoint_checkpoint
                        and not {str(row.get('PMID')) for row in originals}.intersection(endpoint_pmids)):
                    parent, child = base.get('studies', []), previous_child.get('studies', [])
                    track_counts['parent_rows'] += len(parent)
                    track_counts['aggressive_rows'] += len(child)
                    track_counts['aggressive_pmids'].update(str(r['PMID']) for r in child)
                    track_counts['exposures_with_aggressive_evidence'] += bool(child)
                    continue
                reviewed_originals, pending_originals = [], []
                for row in originals:
                    review = reviews.get(str(row.get('PMID')))
                    reviewed_ids = {
                        decision['row_id']
                        for decision in (review or {}).get('estimate_classifications', [])
                    }
                    if row.get('evidence_revision') == 'multicancer-2026-09-29' or (review is not None and evidence_row_id(row) in reviewed_ids):
                        reviewed_originals.append(row)
                    else:
                        pending_originals.append(row)
                parent, child, actions = policy.project(
                    reviewed_originals, base['exposure'], reviews, evidence_row_id,
                    case_reviews=case_reviews,
                    endpoint_exclusions=endpoint_checkpoint)
                # Rows added after the frozen aggressive review remain in the
                # general incidence scope byte-for-byte and are never promoted
                # to the aggressive child without a review.
                parent.extend(copy.deepcopy(pending_originals))
                actions.extend({
                    'action': 'unreviewed_parent_row_preserved',
                    'track': track,
                    'PMID': str(row.get('PMID')),
                    'exposure': base['exposure'],
                } for row in pending_originals)
                endpoint_actions = [
                    action for action in actions
                    if action.get('action') == 'excluded_non_diagnosis_endpoint'
                ]
                matched_endpoint_records.update(
                    action['record_id'] for action in endpoint_actions)
                endpoint_source_rows = []
                endpoint_source_identities = set()
                for action in endpoint_actions:
                    source_row = action.get('excluded_source_row')
                    if source_row is None:
                        continue
                    identity = json.dumps(source_row, sort_keys=True, default=str)
                    if identity not in endpoint_source_identities:
                        endpoint_source_identities.add(identity)
                        endpoint_source_rows.append(source_row)
                endpoint_state = {
                    'version': incidence_endpoint_policy.VERSION,
                    'checkpoint_sha256': endpoint_checkpoint_digest,
                    'records': [
                        {k: v for k, v in action.items()
                         if k != 'excluded_source_row'}
                        for action in endpoint_actions
                    ],
                    'source_rows': endpoint_source_rows,
                }
                audit['actions'].extend(
                    dict({k: v for k, v in action.items()
                          if k != 'excluded_source_row'}, track=track)
                    for action in actions)
                track_counts['parent_rows'] += len(parent)
                track_counts['aggressive_rows'] += len(child)
                track_counts['aggressive_pmids'].update(str(r['PMID']) for r in child)
                track_counts['exposures_with_aggressive_evidence'] += bool(child)
                for disease, rows, prior in [(policy.GENERAL, parent, base), (policy.AGGRESSIVE, child, previous_child)]:
                    changed = not prior or analysis_signature(prior.get('studies', [])) != analysis_signature(rows)
                    old_cwd = Path.cwd()
                    try:
                        os.chdir(stage)
                        output = rebuild(base, rows, disease, review_digest, generate_plots) if changed else copy.deepcopy(prior)
                    finally:
                        os.chdir(old_cwd)
                    output.update(studies=rows, cancer=disease, disease=disease)
                    if changed or endpoint_actions or output.get('incidence_endpoint_exclusions'):
                        output['disease_scope_projection'] = {
                            'policy_version': policy.POLICY_VERSION, 'review_sha256': review_digest,
                            'case_review_sha256': case_digest,
                            'model': 'gpt-5.6-sol', 'parent': policy.GENERAL,
                            'scope': disease, 'reviewed_rows': len(rows),
                            'baseline_source': 'manual_required' if disease == policy.AGGRESSIVE else 'published_general_prostate',
                            'incidence_endpoint_policy_version': incidence_endpoint_policy.VERSION,
                            'incidence_endpoint_checkpoint_sha256': endpoint_checkpoint_digest,
                        }
                        output['incidence_endpoint_exclusions'] = endpoint_state
                    for suffix in ['all', 'core']:
                        relative = Path(source.parent.name)/f'{policy.key(disease)}_incidence_true_{suffix}.json'
                        staged = stage/cache_root.name/relative
                        write(staged, output)
                        targets.append((staged, cache_root/relative))
            track_counts['aggressive_pmids'] = len(track_counts['aggressive_pmids'])
            audit['tracks'][track] = track_counts
            merge_manifest = cache_root/'_merge_manifest.json'
            if merge_manifest.exists():
                lineage = read(merge_manifest)
                lineage['disease_scope_projection'] = {
                    'policy_version': policy.POLICY_VERSION, 'model': 'gpt-5.6-sol',
                    'review_sha256': review_digest, 'counts': track_counts,
                    'case_review_sha256': case_digest,
                }
                staged_lineage = stage/cache_root.name/'_merge_manifest.json'
                write(staged_lineage, lineage)
                targets.append((staged_lineage, merge_manifest))
            staged_manifest = stage/cache_root.name/'_aggressive_prostate_manifest.json'
            write(staged_manifest, {k:v for k,v in audit.items() if k != 'actions'})
            targets.append((staged_manifest, cache_root/'_aggressive_prostate_manifest.json'))
        unmatched = sorted(expected_endpoint_records - matched_endpoint_records)
        if unmatched and require_complete_endpoint_checkpoint:
            raise ValueError(
                'Incidence-endpoint checkpoint records did not match the saved evidence: '
                + ', '.join(unmatched))
        audit['incidence_endpoint_policy']['matched_record_count'] = len(matched_endpoint_records)
        audit['incidence_endpoint_policy']['unmatched_records'] = unmatched
        # All endpoint caches have been validated before publishing any projection.
        for source, destination in targets:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
        if (stage/'static').exists():
            shutil.copytree(stage/'static', ROOT/'static', dirs_exist_ok=True)
    write(review_root/'projection_audit.json', audit)
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review-root', type=Path, default=ROOT/'data/aggressive_prostate_review')
    parser.add_argument('--skip-plots', action='store_true')
    parser.add_argument('--skip-summary', action='store_true')
    args = parser.parse_args()
    audit = build(args.review_root, generate_plots=not args.skip_plots)
    print(json.dumps(audit['tracks'], indent=2), flush=True)
    if not args.skip_summary:
        from scripts import build_summary_plots, rebuild_subexposure_results
        build_summary_plots.build(ROOT/'combined', ROOT/'Plot')
        rebuild_subexposure_results.build()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
