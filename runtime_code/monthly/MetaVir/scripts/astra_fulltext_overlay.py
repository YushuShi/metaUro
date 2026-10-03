"""Guarded, combined-only PMC-text adjudications and durable snapshot replay.

The common validator verifies the PMC text, quotes, source indices and atomic
numeric bundle. This adapter preserves the repository's manual and quality
gates and leaves the two screening tracks untouched.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from scripts import astra_fulltext_common as common
from scripts import astra_combined_overlay as abstract
from scripts import key_comparison_policy as policy

VERSION = 'astra-pmc-fulltext-2026-09-07-v1'
SOURCE_SCOPE = 'PMC_FULL_TEXT_TEXT_ONLY'
CHECKPOINT_NAME = 'astra_fulltext/current_decisions.json'
CLINICAL_HOLDS = ('manual_review_hold', 'full_text_pooling_block',
                  'exposure_classification_ambiguous', 'is_mendelian_randomization')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def packet_entries(packet):
    if isinstance(packet, list):
        return packet
    for key in ('entries', 'results', 'decisions'):
        if isinstance(packet.get(key), list):
            return packet[key]
    raise ValueError('A full-text adjudication packet requires an entries/results array.')


def entry_context(entry):
    source = entry.get('source_row') or {}
    return str(source.get('Context ID') or source.get('context_id') or entry.get('context_id') or ''), source


def is_human(record):
    return common.is_human(record) or abstract.is_human(record)


def authority(row):
    if is_human(row):
        return 'human/manual'
    if row.get('astra_fulltext_adjudication'):
        return 'Astra PMC full text'
    return abstract.retained_authority(row)


def _same_estimate(left, right):
    if left is None or right is None:
        return False
    return not policy.key_differences(left, right) and (
        policy.missing(left.get('effect_type')) or policy.missing(right.get('effect_type'))
        or abstract.effect_type(left['effect_type']) == abstract.effect_type(right['effect_type']))


def _quality(previous, a, b):
    # Numeric correction does not constitute a new JBI assessment. Preserve the
    # existing assessment; where absent, use the more conservative saved grade.
    if isinstance(previous.get('jbi'), dict) and previous['jbi'].get('grade'):
        return copy.deepcopy(previous['jbi'])
    values = [x['jbi'] for x in (a, b) if isinstance(x, dict)
              and isinstance(x.get('jbi'), dict) and x['jbi'].get('grade')]
    ranks = {'poor': 0, 'fair': 1, 'moderate': 2, 'good': 3}
    return copy.deepcopy(min(values, key=lambda x: ranks.get(str(x['grade']).lower(), -1))) if values else None


def _current_target(extractions, pair, all_pairs, previous):
    candidates = [value for value in (pair.get('chatgpt'), pair.get('claude_gemini')) if value is not None]
    matches = [i for i, value in enumerate(extractions)
               if any(_same_estimate(value, candidate) for candidate in candidates)]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, 'More than one saved combined estimate matches this source pair.'
    # A previous Fable or abstract correction may differ from both candidates.
    # A single-estimate context still has one unambiguous target.
    if len(all_pairs) == 1 and len(extractions) <= 1:
        return 0 if extractions else None, None
    # A full-text snapshot's per-estimate identity remains valid on repeat apply.
    identities = [i for i, value in enumerate(extractions)
                  if value.get('astra_fulltext_pair') == pair.get('_pair_number')]
    if len(identities) == 1:
        return identities[0], None
    return None, 'The reviewed source pair cannot be uniquely mapped to a saved combined estimate.'


def _set_extractions(row, values):
    row.pop('additional_records', None)
    row.pop('additional_extractions', None)
    row['extraction'] = copy.deepcopy(values[0]) if values else None
    if len(values) > 1:
        row['additional_extractions'] = copy.deepcopy(values[1:])


def _scientific_hold(previous, a, b, extractions=()):
    # Old abstract quantitative-gap flags can be cleared by a now-complete
    # correction. Independently documented scientific/manual holds cannot.
    candidates = [x for x in (previous, a, b, *extractions) if isinstance(x, dict)]
    return any(any(x.get(field) for field in CLINICAL_HOLDS)
               or any(x.get(field) is False for field in ('manual_poolable', 'included_in_parent_meta_analysis', 'pool_in_parent_meta_analysis'))
               or str(x.get('final_status') or x.get('full_text_status')) in {'MR_SEPARATE', 'ELIGIBLE_SUBTYPE_REVIEW'}
               for x in candidates)


def apply_entries(records, tracks, entries, protected_contexts=()):
    updated = copy.deepcopy(records)
    grouped = defaultdict(list)
    audit = []
    for entry in entries:
        grouped[entry_context(entry)[0]].append(entry)
    for cid, items in grouped.items():
        previous = records.get(cid)
        a = tracks.get('chatgpt', {}).get(cid)
        b = tracks.get('claude_gemini', {}).get(cid)
        events = []
        def event(entry, reason, applied=False, follow_up=True, **more):
            value = {'id': entry.get('id'), 'context_id': cid, 'applied': applied,
                     'follow_up_required': follow_up, 'reason': reason,
                     'context_authority': authority(previous or {}), **more}
            events.append(value)
            return value
        if previous is None:
            audit.extend(event(x, 'Context is absent from the saved combined universe.') for x in items)
            continue
        if cid in protected_contexts or any(is_human(x) for x in (previous, a, b) if x):
            audit.extend(event(x, 'Existing human/manual authority preserved.', follow_up=False) for x in items)
            continue
        pairs = policy.compare_context(a, b)['pairs']
        for index, pair in enumerate(pairs):
            pair['_pair_number'] = index + 1
        row = copy.deepcopy(previous)
        original_extractions = policy.extracts(previous)
        replacements = {}
        excluded_indices = set()
        additions = []
        reviewed_pairs = set()
        accepted = []
        duplicate_pairs = {common.pair_index(x) for x in items
                           if sum(common.pair_index(y) == common.pair_index(x) for y in items) > 1}
        for entry in items:
            number = common.pair_index(entry)
            if number in duplicate_pairs:
                event(entry, 'Duplicate decisions address the same source estimate pair.')
                continue
            extraction, error = common.validate_entry(entry, a, b)
            if error:
                event(entry, error)
                continue
            # The common validator returns a zero-based comparison-pair index.
            if number is None or number < 0 or number >= len(pairs):
                event(entry, 'The reviewed estimate pair is absent from the current source comparison.')
                continue
            pair = pairs[number]
            index, error = _current_target(original_extractions, pair, pairs, previous)
            if error:
                event(entry, error)
                continue
            if index in replacements or index in excluded_indices:
                event(entry, 'Multiple source pairs map to the same saved combined estimate.')
                continue
            eligibility = str((entry.get('adjudication') or {}).get('eligibility') or '').upper()
            if eligibility == 'EXCLUDE':
                if index is not None:
                    excluded_indices.add(index)
            else:
                originals = [original_extractions[index] if index is not None else None,
                             pair.get('chatgpt'), pair.get('claude_gemini'), previous, a, b]
                extraction = common.preserve_scientific_gates(extraction, originals)
                for field in CLINICAL_HOLDS:
                    if any((value or {}).get(field) for value in originals):
                        extraction[field] = True
                extraction['astra_fulltext_pair'] = number + 1
                extraction['astra_fulltext_review_id'] = entry.get('id')
                extraction['astra_fulltext_source_scope'] = SOURCE_SCOPE
                if index is None:
                    additions.append(extraction)
                else:
                    replacements[index] = extraction
            reviewed_pairs.add(number)
            accepted.append(copy.deepcopy(entry))
            complete = eligibility == 'EXCLUDE' or common.coherent(extraction)
            event(entry, 'Validated PMC-text correction applied.' if complete else
                  'Validated PMC-text correction applied; missing quantitative information remains unpooled.',
                  applied=True, follow_up=not complete, estimate_pair=number + 1)
        if accepted:
            values = [replacements.get(i, value) for i, value in enumerate(original_extractions)
                      if i not in excluded_indices] + additions
            _set_extractions(row, values)
            row['jbi'] = _quality(previous, a, b)
            unresolved = any(not item['applied'] and item['follow_up_required'] for item in events)
            source_pending = bool(previous.get('key_comparison_pending') or previous.get('requires_key_review')
                                  or any(word in str(previous.get('final_status') or '') for word in ('PENDING', 'UNRESOLVED')))
            unresolved = unresolved or (source_pending and any(pair['differences'] and index not in reviewed_pairs
                                                              for index, pair in enumerate(pairs)))
            if not values and unresolved:
                for item in events:
                    if item['applied']:
                        item.update(applied=False, follow_up_required=True,
                                    reason='Excluding this pair would clear a context with another unresolved estimate; prior result preserved.')
                audit.extend(events)
                continue
            gap = any(not common.coherent(value) for value in values)
            hold = _scientific_hold(previous, a, b, values)
            row['eligibility'] = 'INCLUDE' if values else 'EXCLUDE'
            row['final_status'] = row['full_text_status'] = 'ELIGIBLE_NOT_POOLABLE' if values else 'EXCLUDE'
            row['astra_preserved_pooling_hold'] = bool(values and (gap or hold or unresolved))
            row['astra_quantitative_follow_up_required'] = bool(values and gap)
            row['key_comparison_pending'] = unresolved
            row['requires_key_review'] = unresolved
            row['screen_layer'] = 'combined'
            row['astra_fulltext_adjudication'] = {
                'version': VERSION, 'source_scope': SOURCE_SCOPE, 'model': 'gpt-6-astra',
                'source_fingerprint': common.source_digest(a, b), 'entries': accepted,
                'previous_combined_sha256': digest(previous),
                'previous_authority': authority(previous),
                'unresolved_pair_review': unresolved, 'scientific_pooling_hold': hold,
                'quantitative_follow_up_required': gap,
            }
            row['combined_adjudication'] = {'resolution': 'ASTRA_PMC_FULLTEXT_PARTIAL' if unresolved else 'ASTRA_PMC_FULLTEXT_RESOLVED',
                                           'selected_pipeline': 'ASTRA_PMC_FULLTEXT',
                                           'explanation': 'Validated full-text PMC adjudication; prior human and JBI gates preserved.'}
            row['key_comparison'] = {**copy.deepcopy(previous.get('key_comparison') or {}),
                                     'action': 'astra_pmc_fulltext_resolved' if not unresolved else 'astra_pmc_fulltext_partial',
                                     'prior_decision_reused': False}
            if not unresolved:
                row.pop('key_comparison_candidates', None)
                row.pop('key_comparison_additional_review_reason', None)
            updated[cid] = row
        for item in events:
            final = updated[cid]
            item.update(context_status=final.get('final_status'), context_authority=authority(final))
        audit.extend(events)
    return updated, audit


def snapshot_checkpoint(records, tracks, entries, audit, prior=None):
    snapshots = copy.deepcopy((prior or {}).get('snapshots') or {})
    for cid in {x['context_id'] for x in audit if x.get('applied')}:
        row = copy.deepcopy(records[cid])
        snapshots[cid] = {'record': row, 'record_sha256': digest(row),
                          'source_fingerprint': common.source_digest(tracks.get('chatgpt', {}).get(cid), tracks.get('claude_gemini', {}).get(cid))}
    return {'version': VERSION, 'model': 'gpt-6-astra', 'source_scope': SOURCE_SCOPE,
            'entries': copy.deepcopy(entries), 'snapshots': snapshots, 'audit': copy.deepcopy(audit)}


def apply_checkpoint(records, tracks, combined_root, protected_contexts=()):
    path = Path(combined_root) / CHECKPOINT_NAME
    if not path.exists():
        return records, []
    checkpoint = json.loads(path.read_text())
    if checkpoint.get('version') != VERSION or checkpoint.get('source_scope') != SOURCE_SCOPE:
        raise ValueError('Unsupported Astra PMC full-text checkpoint.')
    result = copy.deepcopy(records)
    audit = []
    for cid, snapshot in checkpoint.get('snapshots', {}).items():
        a, b = tracks.get('chatgpt', {}).get(cid), tracks.get('claude_gemini', {}).get(cid)
        reason = None
        if cid not in result:
            reason = 'Saved full-text context is absent from the rebuilt combined universe.'
        elif cid in protected_contexts or any(is_human(x) for x in (result[cid], a, b) if x):
            reason = 'Existing human/manual authority preserved.'
        elif snapshot.get('source_fingerprint') != common.source_digest(a, b):
            reason = 'Screening sources changed; the full-text snapshot requires revalidation.'
        elif snapshot.get('record_sha256') != digest(snapshot.get('record')):
            raise ValueError('Full-text checkpoint record integrity failed: ' + cid)
        if reason is None:
            result[cid] = copy.deepcopy(snapshot['record'])
        audit.append({'context_id': cid, 'applied': reason is None,
                      'follow_up_required': bool(reason and 'human/manual' not in reason),
                      'reason': reason or 'Validated full-text snapshot replayed with unchanged sources.'})
    return result, audit


def normalize_femina(records, audit):
    from scripts import build_combined_results as release
    for cid in {event['context_id'] for event in audit if event.get('applied')}:
        row = records[cid]
        reported_type = (row.get('extraction') or {}).get('effect_type')
        if isinstance(row.get('extraction'), dict):
            row['extraction'] = femina_extraction_bridge(row['extraction'])
        extraction = row.get('extraction') or {}
        point = policy.number(extraction.get('effect_size'))
        ci_pair_incomplete = policy.missing(extraction.get('ci_lower')) != policy.missing(extraction.get('ci_upper'))
        if extraction.get('needs_inversion') and (point is None or point <= 0 or ci_pair_incomplete
                                                 or not common.relaxed.canonical_ratio(extraction.get('effect_type'))):
            # Keep the reported contrast and known quantities when there is no
            # usable ratio to reciprocate yet. Never invent an effect to satisfy
            # the older all-or-nothing inversion contract.
            row['final_status'] = row['full_text_status'] = 'ELIGIBLE_NOT_POOLABLE'
            row['astra_preserved_pooling_hold'] = True
            row['astra_quantitative_follow_up_required'] = True
            row['poolable_after_policy'] = False
            row['screen_layer'] = 'combined'
            row['source_screen_layer'] = 'full_text'
            row['direct_exposure'] = str(row.get('exposure') or '')
            row['inherited_exposures'] = release.monthly.inherited_exposures(row['direct_exposure'])
            extraction['astra_fulltext_inversion_deferred'] = True
            extraction['missing_quantitative_fields'] = [key for key in policy.KEY_FIELDS if policy.missing(extraction.get(key))]
            row['missing_quantitative_fields'] = list(extraction['missing_quantitative_fields'])
            records[cid] = release.bind_final_row_digests(release.dietary.apply_record(row, row.get('exposure'), release.ROOT))
            continue
        status = row['final_status']
        if status != 'EXCLUDE' and not row.get('astra_preserved_pooling_hold'):
            status = release.current_poolability_status('ELIGIBLE_NOT_POOLABLE', row)
        records[cid] = release.normalize_final_row({}, row, status, row['combined_adjudication'])
        if reported_type and not common.relaxed.canonical_ratio(reported_type) and isinstance(records[cid].get('extraction'), dict):
            # Keep a supported non-ratio statistic recognizable in the study
            # table. Femina's ratio-only pooling normalizer otherwise blanks it.
            records[cid]['extraction']['effect_type'] = reported_type
            records[cid]['astra_preserved_pooling_hold'] = True
            if records[cid]['final_status'] != 'EXCLUDE':
                records[cid]['final_status'] = records[cid]['full_text_status'] = 'ELIGIBLE_NOT_POOLABLE'
            release.bind_final_row_digests(records[cid])
    return records


def femina_extraction_bridge(extraction):
    """Map validated PMC fields into Femina's established UI/inversion contract."""
    result = copy.deepcopy(extraction)
    if not result.get('astra_fulltext_provenance'):
        return result
    evidence = result.get('evidence') or {}
    quotes = [str((evidence.get(field) or {}).get('excerpt') or '').strip()
              for field in ('sample_size', 'total_n', 'effect_size', 'confidence_interval', 'ci_lower', 'ci_upper')]
    result['supporting_text'] = '\n'.join(dict.fromkeys(quote for quote in quotes if quote))
    result['comparison_direction_imputed'] = result.get('direction_imputed') is True
    result['effect_adjusted'] = result.get('effect_is_adjusted') is True
    if any(result.get(field) is False for field in ('p_value_is_exact', 'p_value_targets_selected_estimate', 'wald_inputs_same_estimate')):
        # Femina's earlier extraction contract interprets absent assertion flags
        # leniently. An explicit full-text finding needs named provenance so a
        # later cache rebuild cannot derive a CI from a non-target/bounded p.
        result['p_value_provenance'] = {'kind': 'fulltext_non_target_or_non_exact',
                                      'exact': result.get('p_value_is_exact'),
                                      'same_estimate': False if result.get('p_value_targets_selected_estimate') is False
                                          or result.get('wald_inputs_same_estimate') is False else None,
                                      'supporting_text': result.get('p_value_method')}
    if result.get('comparison_direction_imputed'):
        result['comparison_imputation_method'] = result.get('direction_imputation_reason') or 'Protocol default higher versus lower or yes versus no'
    if result.get('reference_category') is None and result.get('reference_group'):
        result['reference_category'] = result['reference_group']
    if result.get('needs_inversion') and not result.get('inversion_applied'):
        # The old contract proves inversion with explicit numerator/denominator
        # labels. A continuous per-unit decrement has an algebraic no-change
        # reference, while categorical labels come from the validated contrast.
        reported = result.get('reported_comparison_direction')
        if reported == 'per_unit_decrease':
            result.setdefault('reported_comparison_type', result.get('comparison_type'))
            result['comparison_type'] = 'One-unit decrease versus no change'
            result['comparison_group'] = 'One-unit decrease'
            result['reference_group'] = result['reference_category'] = 'no change'
            result['comparison_type_imputed'] = True
            result['reference_category_imputed'] = True
            result['comparison_imputation_method'] = 'Algebraic labels for the explicitly reported per-unit decrement'
        elif (len(re.split(r'\s+(?:versus|vs\.?|compared\s+(?:with|to))\s+',
                           str(result.get('comparison_type') or ''), maxsplit=1, flags=re.I)) != 2
              and result.get('comparison_group') and result.get('reference_group')):
            result.setdefault('reported_comparison_type', result.get('comparison_type'))
            result['comparison_type'] = f"{result['comparison_group']} versus {result['reference_group']}"
            result['comparison_type_imputed'] = True
        if result.get('comparison_type'):
            sides = re.split(r'\s+(?:versus|vs\.?|compared\s+(?:with|to))\s+',
                             str(result['comparison_type']), maxsplit=1, flags=re.I)
            if len(sides) == 2 and all(part.strip() for part in sides):
                # Bind the inversion proof to the exact stated denominator;
                # an abbreviated reference_group can remain in source audit.
                result['reference_category'] = sides[1].strip()
    return result


def apply_femina_checkpoint(records, tracks, combined_root, protected_contexts=()):
    if not (Path(combined_root) / CHECKPOINT_NAME).exists():
        return records, []
    from scripts import build_combined_results as release
    tracks = copy.deepcopy(tracks)
    for track in ('chatgpt', 'claude_gemini'):
        source = release.ROOT / 'data/poolability_policy/2026-09-05' / (track + '_effective_results.json')
        if source.exists():
            tracks[track].update({row['context_id']: row for row in json.loads(source.read_text())['results']})
    result, audit = apply_checkpoint(records, tracks, combined_root, protected_contexts)
    return normalize_femina(result, audit), audit
