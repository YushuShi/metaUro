#!/usr/bin/env python3
"""Build a reversible combined UI view for pairs awaiting human review."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import provisional_review_policy as provisional
from scripts import astra_fulltext_common as common
from scripts import astra_fulltext_overlay as fulltext
from scripts import apply_astra_adjudication as abstract
from scripts import reconcile_screening_tracks as reconcile
from scripts import key_comparison_policy as policy

ALIAS = {'MetaCutis_et_ Capillus': 'metaCutis'}.get(ROOT.name, ROOT.name)
VIEW = ROOT / 'combined/provisional_ui'


def read(path):
    return json.loads(Path(path).read_text())


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_current_summaries():
    """Build summaries only after the new saved cache has been installed."""
    from scripts import build_summary_plots as summary_builder
    from scripts import plot_localization

    ui_catalog = ROOT / 'static/i18n-translations.json'
    if ui_catalog.exists():
        for label, translations in read(ui_catalog).items():
            if isinstance(translations, dict):
                for language, value in translations.items():
                    plot_localization.DISEASE_LABELS.setdefault(label, {}).setdefault(language, value)
    override = ROOT / 'combined/astra_adjudication/plot_translation_overrides.json'
    if override.exists():
        catalog = plot_localization._site_catalog(str(ROOT.resolve()))
        for label, translations in read(override)['translations'].items():
            for language, value in translations.items():
                catalog.setdefault(label, {}).setdefault(language, value)
                if hasattr(plot_localization, 'GUT_EXPOSURE_LABELS'):
                    plot_localization.GUT_EXPOSURE_LABELS.setdefault(label, {}).setdefault(language, value)

    result = {}
    for mode in ('yes', 'no'):
        root = VIEW / 'subexposure_views' / mode
        built = summary_builder.build(root / 'Cached_results', root / 'Plot')
        source = (built.get('sources') or {}).get(ALIAS) or {}
        scopes = source.get('scopes') or list(built.get('scopes', {}))
        artifacts = source.get('artifacts')
        if artifacts is None:
            artifacts = []
            for scope in built.get('scopes', {}).values():
                artifacts.extend(
                    entry['filename'] for entry in scope.get('workbooks', {}).values()
                    if entry.get('filename')
                )
                for entry in scope.get('plots', {}).values():
                    if entry.get('available') and entry.get('filename'):
                        artifacts.append(entry['filename'])
                        artifacts.extend((entry.get('locales') or {}).values())
        result[mode] = {
            'scopes': len(scopes),
            'pdfs': sum(str(path).endswith('.pdf') for path in artifacts),
            'workbooks': sum(str(path).endswith('.xlsx') for path in artifacts),
        }
    return result


def pair_index(entry):
    return int((entry.get('source_row') or {})['Estimate pair']) - 1


def finalized(prior, entry):
    history = {event.get('id'): event for event in prior.get('astra_fulltext_history', [])}
    event = history.get(entry['id'])
    return bool(event and event.get('applied') and not event.get('follow_up_required'))


def verify_pair(pair, reviews):
    """Reject a stale queue when source indices or its original numbers changed."""
    for entry in reviews:
        row = entry['source_row']
        for track, prefix in [('chatgpt', 'ChatGPT'), ('claude_gemini', 'Claude+Gemini')]:
            extraction = pair.get(track) or {}
            index = row.get(prefix + ' estimate index')
            if not policy.missing(index) and not policy.numeric_equal(index, pair.get(track + '_index'), 'count'):
                raise ValueError('Source estimate index changed: ' + entry['id'])
            for key, suffix in [('total_n', 'sample size'), ('effect_size', 'effect size'),
                                ('ci_lower', 'CI lower'), ('ci_upper', 'CI upper')]:
                column = prefix + ' ' + suffix
                if column in row and not common.source_value_equal(row[column], extraction.get(key),
                        'count' if key == 'total_n' else 'ratio'):
                    raise ValueError('Source numeric bundle changed: ' + entry['id'] + ' / ' + column)


def marked_candidate(item, decision, *, allow_pooling=False):
    item = copy.deepcopy(item)
    if decision.get('higher_jbi'):
        item['source_record']['jbi'] = copy.deepcopy(decision['higher_jbi'])
    # Human-review status stays pending even when the temporary source rule
    # chooses a complete bundle for pooling.
    item['requires_key_review'] = not allow_pooling
    item['manual_review_pending'] = True
    item['provisional_ui_selection'] = copy.deepcopy(decision)
    return item


def apply_context(prior, tracks, entries):
    cid = prior['context_id']
    a, b = (tracks[track].get(cid) for track in ('chatgpt', 'claude_gemini'))
    original = fulltext.candidates_for_record(prior, tracks)
    if any(common.is_human(row) for row in (prior, a, b)):
        return copy.deepcopy(prior), original, [
            {'id': row['id'], 'context_id': cid, 'pending': True,
             'changed': False, 'reason': 'Existing human decision remains authoritative.'}
            for row in entries]
    if policy.nonpoolable_status_resolution(a, b):
        resolved, candidates, _ = reconcile.reconcile_context(a, b)
        independent_ids = reconcile.preserve_independent_review(resolved,candidates,entries)
        existing_resolution = prior.get('nonpoolable_status_resolution') or {}
        resolution = resolved['nonpoolable_status_resolution']
        unchanged_retention = (resolution.get('resolution_kind') == 'both_eligible_not_poolable' and existing_resolution == resolution)
        previous = prior.get('prior_automated_adjudication') if unchanged_retention else prior
        if previous is not None:resolved['prior_automated_adjudication'] = copy.deepcopy(previous)
        return resolved, candidates, [{'id': entry['id'], 'context_id': cid, 'pending': entry['id'] in independent_ids,
            'changed': True, 'reason': ('Independent scientific/manual adjudication remains pending.' if entry['id'] in independent_ids else resolved['poolability_reason']),
            'display_candidates': len(candidates), 'pooling_candidates': 0} for entry in entries]
    pairs = policy.compare_context(a, b)['pairs']
    grouped = defaultdict(list)
    for entry in entries:
        row = entry['source_row']
        if row.get('Context ID') != cid or str(row.get('PMID')) != str(prior.get('pmid')):
            raise ValueError('Pending review context identity mismatch: ' + entry['id'])
        grouped[pair_index(entry)].append(entry)
    items = copy.deepcopy(original)
    audit = []
    for index, reviews in sorted(grouped.items()):
        if not 0 <= index < len(pairs):
            raise ValueError('Pending review pair is absent from current sources: ' + reviews[0]['id'])
        verify_pair(pairs[index], reviews)
        if any(finalized(prior, review) for review in reviews):
            audit.extend({'id': review['id'], 'context_id': cid, 'pair_index': index,
                          'pending': True, 'changed': False,
                          'reason': 'A finalized full-text correction remains authoritative.'}
                         for review in reviews)
            continue
        current = [item for item in items if item['pair_index'] == index]
        accepted = [item for item in current if not item.get('requires_key_review')]
        baseline = accepted[0]['extraction'] if len(accepted) == 1 else None
        pair = pairs[index]
        decision = provisional.select_pending_pair(a, b, pair.get('chatgpt'),
            pair.get('claude_gemini'), reviews, baseline_estimate=baseline)
        replacements = []
        if decision['protect_adjudicated_quantities']:
            # Neither original source supplies a complete bundle in this case;
            # retain the supported partial Astra values without adding numbers.
            replacements = [marked_candidate(item, decision, allow_pooling=False)
                            for item in current]
        elif decision.get('selected_track'):
            track = decision['selected_track']
            source = copy.deepcopy({'chatgpt': a, 'claude_gemini': b}[track])
            if decision.get('higher_jbi'):
                source['jbi'] = copy.deepcopy(decision['higher_jbi'])
            extraction = copy.deepcopy(decision.get('extraction') or {})
            extraction = common.preserve_scientific_gates(extraction,
                [item.get('extraction') for item in current])
            for gate in ('full_text_pooling_block', 'exposure_classification_ambiguous',
                         'hierarchy_inherited', 'broader_outcome_projection'):
                if prior.get(gate) or extraction.get(gate):
                    source[gate] = True
            selected = reconcile.candidate(source, extraction, track, index,
                source_index=pair.get(track + '_index') if pair.get(track + '_index') is not None else index)
            selected = abstract.checked_status(selected)
            # Check the shared helper's complete-bundle decision as well as
            # the repository's independent JBI/scientific pooling criteria.
            selected = marked_candidate(selected, decision,
                allow_pooling=decision['selection_poolable_candidate'])
            replacements = [selected]
        else:
            replacements = [marked_candidate(item, decision, allow_pooling=False)
                            for item in current]
        items = [item for item in items if item['pair_index'] != index] + replacements
        audit.extend({'id': review['id'], 'context_id': cid, 'pair_index': index,
                      'pending': True, 'changed': bool(replacements),
                      'decision': copy.deepcopy(decision), 'reason': decision['reason'],
                      'display_candidates': len(replacements),
                      'pooling_candidates': sum(reconcile.app_row(item)['policy_poolable'] for item in replacements)}
                     for review in reviews)
    items.sort(key=lambda item: (item['pair_index'], item['source_estimate_index'], item['source']))
    result = copy.deepcopy(prior)
    # Keep the adjudicated record intact inside the view. The provisional
    # candidates are explicit UI data, never a new final adjudication.
    result['manual_review_pending'] = True
    result['provisional_ui_candidates'] = [
        {key: copy.deepcopy(value) for key, value in item.items() if key != 'source_record'}
        | {'source_gate_snapshot': abstract.candidate_receipt(item).get('source_gate_snapshot', {}),
           'jbi': copy.deepcopy(item['source_record'].get('jbi'))}
        for item in items]
    result['provisional_ui_review_ids'] = [entry['id'] for entry in entries]
    return result, items, audit


def build(output, apply=False, summary=True):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use an empty application output directory.')
    output.mkdir(parents=True, exist_ok=True)
    source_paths = {track: reconcile.SOURCE_DIR / (track + '_effective_results.json')
                    for track in ('chatgpt', 'claude_gemini')}
    protected = [*source_paths.values(), ROOT / 'combined/effective_results.json',
                 ROOT / 'combined/astra_fulltext/current_decisions.json',
                 ROOT / 'combined/astra_fulltext/review_dispositions.json']
    human_checkpoint = ROOT / 'combined/human_review/current_decisions.json'
    human_decisions = read(human_checkpoint) if human_checkpoint.exists() else {}
    if human_checkpoint.exists():
        protected.append(human_checkpoint)
        if human_decisions.get('input_file'):
            protected.append(human_checkpoint.parent / Path(human_decisions['input_file']).name)
    from scripts import curator_dedup_overlay
    curator_checkpoint = ROOT / 'combined' / curator_dedup_overlay.CHECKPOINT
    if curator_checkpoint.exists():
        protected.append(curator_checkpoint)
    hashes = {str(path.relative_to(ROOT)): sha(path) for path in protected}
    tracks = {track: {row['context_id']: row for row in read(path)['results']}
              for track, path in source_paths.items()}
    packet = read(ROOT / 'combined/effective_results.json')
    entries = provisional.load_pending(ROOT, ALIAS)
    frozen_pending_count = len(entries)
    supplemental = ROOT.parent / 'screening_status_reports/provisional_human_review_2026-09-07' / ('additional_human_reviews_' + ALIAS + '.json')
    if supplemental.exists():
        entries += read(supplemental).get('rows') or []
    if not entries and not human_decisions.get('decisions'):
        raise ValueError('No pending review manifest was found.')
    ids = [entry['id'] for entry in entries]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate pending review IDs.')
    grouped = defaultdict(list)
    for entry in entries:
        grouped[entry['source_row']['Context ID']].append(entry)
    results, audit, all_items, changed_scopes = [], [], {}, set()
    for original in packet['results']:
        cid = original['context_id']
        if cid not in grouped:
            results.append(original)
            continue
        result, items, events = apply_context(original, tracks, grouped[cid])
        results.append(result)
        all_items[cid] = items
        audit.extend(events)
        if any(event['changed'] for event in events):
            changed_scopes.add(reconcile.scope(original))
    if {row['id'] for row in audit} != set(ids):
        raise ValueError('Pending review rows are missing from saved combined evidence.')
    counts = Counter(row['reason'] for row in audit)
    report = {'version': provisional.VERSION, 'repository': ALIAS,
              'pending_rows': len(entries), 'frozen_pending_rows': frozen_pending_count,
              'supplemental_pending_rows': len(entries) - frozen_pending_count, 'reason_counts': dict(counts),
              'changed_scopes': len(changed_scopes), 'protected_source_hashes': hashes,
              'model_calls': 0, 'human_review_rows_resolved': len(human_decisions.get('decisions', []))}
    write(output / 'review_audit.json', audit)
    write(output / 'proposed_effective_results.json', {**packet, 'results': results})
    if apply:
        stage = output / 'staged_provisional_ui'
        stage.mkdir()
        for path in (ROOT / 'combined').glob('*/*_true_*.json'):
            target = stage / path.relative_to(ROOT / 'combined')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        scope_items = defaultdict(list)
        for record in packet['results']:
            key = reconcile.scope(record)
            if key in changed_scopes:
                scope_items[key].extend(all_items[record['context_id']] if record['context_id'] in all_items else
                    fulltext.candidates_for_record(record, tracks))
        old_root, old_audit = reconcile.COMBINED, reconcile.AUDIT
        try:
            reconcile.COMBINED, reconcile.AUDIT = stage, output / 'cache_audit'
            report['materialization'] = reconcile.materialize(scope_items)
            if report['materialization']['errors']:
                raise RuntimeError('Provisional cache generation failed; nothing installed.')
        finally:
            reconcile.COMBINED, reconcile.AUDIT = old_root, old_audit
        old_version = abstract.VERSION
        try:
            abstract.VERSION = provisional.VERSION
            report['subexposure_modes'] = abstract.build_combined_modes(
                stage.glob('*/*_true_all.json'), stage / 'subexposure_views')
        finally:
            abstract.VERSION = old_version
        parent_helper = ROOT / 'scripts/provisional_disease_scopes.py'
        if parent_helper.exists():
            from scripts import provisional_disease_scopes
            report['disease_parent_modes'] = {mode: provisional_disease_scopes.materialize(stage / 'subexposure_views' / mode / 'Cached_results') for mode in ('yes', 'no')}
        outcomes = defaultdict(list)
        for mode in ('yes', 'no'):
            for cache in (stage / 'subexposure_views' / mode / 'Cached_results').glob('*/*_true_all.json'):
                value = read(cache)
                for row in value.get('studies') or []:
                    selection = row.get('provisional_ui_selection') or {}
                    for review_id in selection.get('review_ids') or []:
                        outcomes[review_id].append({
                            'mode': mode, 'visible': True,
                            'displayed_exposure': value.get('exposure'),
                            'direct_exposure': row.get('Direct Exposure'),
                            'disease': value.get('disease') or value.get('cancer'),
                            'record_id': row.get('Record ID'),
                            'source': row.get('combined_source'),
                            'jbi_grade': row.get('Quality Score'),
                            'sample_size': row.get('Sample Size'),
                            'effect_type': row.get('Effect Type'),
                            'effect_size': row.get('Effect Size'),
                            'ci_lower': row.get('Lower CI'), 'ci_upper': row.get('Upper CI'),
                            'policy_poolable': bool(row.get('policy_poolable')),
                            'pooling_selected': bool(row.get('policy_poolable') and row.get('subexposure_pooling_selected') and row.get('disease_hierarchy_pooling_selected') is not False),
                            'manual_review_pending': bool(row.get('manual_review_pending'))})
        for event in audit:
            event['ui_outcomes'] = outcomes[event['id']]
        write(output / 'review_audit.json', audit)
        write(stage / 'effective_results.json', {**packet, 'results': results,
              'provisional_ui_version': provisional.VERSION})
        write(stage / 'pending_manifest.json', {'version': provisional.VERSION, 'rows': entries})
        write(stage / 'review_audit.json', audit)
        assert all(sha(ROOT / path) == value for path, value in hashes.items())
        manifest = {**report, 'canonical_track': 'combined', 'complete': True,
                    'pending_manifest_sha256': sha(stage / 'pending_manifest.json')}
        write(stage / 'manifest.json', manifest)
        (stage / 'README.md').write_text(
            '# Saved temporary combined UI view\n\n'
            'Pending human-review pairs use the complete eligible pipeline when the other is incomplete or excluded; '
            'when both are complete, ChatGPT is selected. The higher saved JBI assessment is used while pending. '
            'Independent scientific and cohort restrictions still govern pooling. These choices do not resolve reviews.\n\n'
            '`effective_results.json` retains adjudicated records and adds explicit provisional candidates; '
            '`review_audit.json` records selections and actual UI outcomes. Both hierarchy modes are saved under '
            '`subexposure_views`. Original tracks and authoritative human/Astra combined results are unchanged. '
            'The app refuses stale views when a protected source changes. Rebuild offline with '
            '`python scripts/build_provisional_review_ui.py --output <new-empty-audit-folder> --apply`. '
            'Review pending metadata is retained in files without adding visible UI badges.\n')
        if not entries:
            (stage / 'README.md').write_text(
                '# Saved combined UI view\n\n'
                'All supplied human reviews are resolved. This view is rebuilt from the '
                'authoritative combined records and retains their human decisions and existing '
                'scientific and quality criteria. No temporary pipeline selections remain.\n\n'
                'Both exposure hierarchy modes and disease-parent projections are saved under '
                '`subexposure_views`. The readiness manifest verifies the immutable model '
                'packets, canonical combined data, and human-decision checkpoint. Rebuild '
                'offline with `python scripts/build_provisional_review_ui.py --output '
                '<new-empty-audit-folder> --apply`.\n')
        backup = output / 'before_provisional_ui'
        if VIEW.exists():
            shutil.move(str(VIEW), backup)
        try:
            shutil.move(str(stage), VIEW)
            if summary:
                report['summary_modes'] = build_current_summaries()
                manifest = {**report, 'canonical_track': 'combined', 'complete': True,
                            'pending_manifest_sha256': sha(VIEW / 'pending_manifest.json')}
                write(VIEW / 'manifest.json', manifest)
        except BaseException:
            if VIEW.exists():
                shutil.rmtree(VIEW)
            if backup.exists():
                shutil.move(str(backup), VIEW)
            raise
    report['protected_sources_unchanged'] = all(sha(ROOT / path) == value for path, value in hashes.items())
    assert report['protected_sources_unchanged']
    write(output / 'application_report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--skip-summary', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.apply, not args.skip_summary), indent=2))
