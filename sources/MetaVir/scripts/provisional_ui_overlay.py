"""Reversible pending-review projections for combined UI caches only."""
from __future__ import annotations
import copy
import json
import re
from collections import defaultdict
from pathlib import Path
from scripts import provisional_review_policy as temporary
from scripts import astra_fulltext_common as common
from scripts import astra_fulltext_overlay as fulltext
from scripts import key_comparison_policy as policy

ROOT = Path(__file__).resolve().parents[1]
ALIAS = 'metaVir' if ROOT.name == 'MetaVir' else ROOT.name
CHECKPOINT = 'provisional_ui/pending_manifest.json'
SUPPLEMENTAL_CHECKPOINT = 'provisional_ui/supplemental_pending_manifest.json'
MARKERS = ('manual_review_pending', 'provisional_ui_selection', 'provisional_origin_context_id',
           'provisional_ui_display_only', 'provisional_candidate_track')


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False, default=str)+'\n')


def scientific_fingerprint(row):
    return common.digest({key: row.get(key) for key in (
        'extraction', 'additional_extractions', 'additional_records', 'eligibility',
        'final_status', 'full_text_status', 'jbi', 'decision_authority',
        'astra_fulltext_adjudication', 'astra_adjudication', 'human_adjudication')})


def checkpoint(records, tracks, entries):
    rows = []
    for entry in entries:
        cid = str((entry.get('source_row') or {}).get('Context ID') or '')
        if cid not in records: raise ValueError('Pending context absent: '+cid)
        rows.append({**copy.deepcopy(entry),
            'provisional_source_fingerprint': common.source_digest(tracks['chatgpt'].get(cid), tracks['claude_gemini'].get(cid)),
            'provisional_baseline_fingerprint': scientific_fingerprint(records[cid])})
    return {'version': temporary.VERSION, 'repository': ALIAS, 'rows': rows}


def _normalize_row(previous, extraction, selection, candidate_track, *, candidate=False):
    row = copy.deepcopy(previous)
    row.pop('additional_extractions', None); row.pop('additional_records', None)
    row['extraction'] = copy.deepcopy(extraction or {})
    row['jbi'] = copy.deepcopy(selection.get('higher_jbi') or row.get('jbi'))
    row['manual_review_pending'] = True
    row['provisional_origin_context_id'] = previous['context_id']
    row['provisional_candidate_track'] = candidate_track
    row['provisional_ui_selection'] = {k: copy.deepcopy(v) for k, v in selection.items()
                                      if k not in {'extraction', 'higher_jbi'}}
    row['key_comparison_pending'] = False  # Authoritative record retains its flag.
    row['eligibility'] = 'INCLUDE'
    row['final_status'] = row['full_text_status'] = 'ELIGIBLE_NOT_POOLABLE'
    can_pool = selection.get('selection_poolable_candidate') is True and not candidate
    hold = bool(selection.get('independent_source_hold')) or fulltext._scientific_hold(previous, {}, {}, [row['extraction']])
    error = None
    if can_pool and not hold:
        try:
            if ROOT.name == 'metaFemina':
                from scripts import build_combined_results as release
                extraction=row['extraction']
                if extraction.get('needs_inversion') and not extraction.get('inversion_applied'):
                    sides=re.split(r'\s+(?:versus|vs\.?|compared\s+(?:with|to))\s+',str(extraction.get('comparison_type') or ''),maxsplit=1,flags=re.I)
                    if len(sides)==2 and all(side.strip() for side in sides):
                        extraction['reference_category']=sides[1].strip()
                extraction['comparison_direction_imputed']=extraction.get('direction_imputed') is True
                if extraction.get('effect_is_adjusted') is True:extraction['effect_adjusted']=True
                row['extraction'] = release.normalized_extraction(row)
                can_pool = release.screening_contract.is_poolable(row['extraction'], row.get('jbi'))
            else:
                from scripts import dual_screening
                row['extraction'] = dual_screening.apply_poolability_policy(row['extraction'])
                can_pool = dual_screening.is_poolable(row['extraction'], row.get('jbi'))
                if can_pool:
                    guarded = dual_screening.apply_whole_food_subtype_disposition(dict(row, final_status='INCLUDE_META'))
                    can_pool = guarded['final_status'] == 'INCLUDE_META'
        except (ValueError, TypeError, OverflowError) as exc:
            can_pool = False; error = str(exc)
    else:
        can_pool = False
    if can_pool:
        row['final_status'] = row['full_text_status'] = 'INCLUDE_META'
    if selection.get('separate_scientific_scope') == 'MR_SEPARATE':
        row['final_status'] = row['full_text_status'] = 'MR_SEPARATE'
        row['is_mendelian_randomization'] = True
        can_pool = False
    row['provisional_ui_display_only'] = not can_pool and not selection.get('separate_scientific_scope')
    row['provisional_ui_selection'].update(effective_poolable=can_pool,
        independent_hold_preserved=hold, normalization_error=error)
    return row


def project(records, tracks, saved, protected=()):
    """Return effective view records; never mutate scientific records or tracks."""
    output = copy.deepcopy(records); audit = []; groups = defaultdict(list)
    for entry in saved.get('rows', []):
        groups[str((entry.get('source_row') or {}).get('Context ID') or '')].append(entry)
    for cid, entries in groups.items():
        previous = records.get(cid); a = tracks.get('chatgpt', {}).get(cid); b = tracks.get('claude_gemini', {}).get(cid)
        ids = [entry['id'] for entry in entries]
        from scripts.nonpoolable_status_retention import resolve_record
        retained = resolve_record(a, b, previous, cid in protected)
        if retained is not None:
            output[cid] = retained
            audit.extend({'id': rid, 'context_id': cid, 'pending': False, 'projected': True,
                          'effective_poolable': False, 'reason': retained['reason']} for rid in ids)
            continue
        reason = None
        if previous is None: reason = 'Context no longer exists.'
        elif cid in protected or any(common.is_human(x) for x in (previous, a, b)): reason = 'Human authority preserved.'
        elif any(entry.get('provisional_source_fingerprint') != common.source_digest(a,b) for entry in entries): reason = 'Source screening changed; provisional selection withheld.'
        elif any(entry.get('provisional_baseline_fingerprint') != scientific_fingerprint(previous) for entry in entries): reason = 'Authoritative scientific decision changed; provisional selection withheld.'
        pairs = policy.compare_context(a,b)['pairs'] if previous else []
        extractions = policy.extracts(previous)
        if reason is None and (len(pairs) != 1 or len(extractions) > 1 or any(common.pair_index(e) != 0 for e in entries)):
            reason = 'Ambiguous multi-estimate context retained for review; finalized pairs preserved.'
        if reason:
            audit.extend({'id': rid, 'context_id': cid, 'pending': True, 'projected': False, 'reason': reason} for rid in ids)
            continue
        pair = pairs[0]
        selection = temporary.select_pending_pair(a,b,pair.get('chatgpt'),pair.get('claude_gemini'),entries,
                         baseline_estimate=extractions[0] if extractions else None)
        selection['independent_source_hold'] = fulltext._scientific_hold(previous,a,b,
            [pair.get('chatgpt'),pair.get('claude_gemini')])
        if (all(entry.get('provisional_separate_scientific_scope') == 'MR_SEPARATE' for entry in entries)
            and all(isinstance(source, dict) and 'MR_SEPARATE' in
                    (source.get('final_status'), source.get('full_text_status')) for source in (a, b))):
            selection['separate_scientific_scope'] = 'MR_SEPARATE'
            selection['independent_source_hold'] = True
        if selection.get('protect_adjudicated_quantities'):
            row = _normalize_row(previous, extractions[0] if extractions else None, selection, 'adjudicated_combined', candidate=True)
            # Preserve the existing supported partial bundle byte-for-byte.
            row['extraction'] = copy.deepcopy(previous.get('extraction'))
            output[cid] = row
        elif selection.get('selected_track'):
            output[cid] = _normalize_row(previous, selection['extraction'], selection, selection['selected_track'])
        else:
            candidates = [(track, source, pair.get(track)) for track,source in [('chatgpt',a),('claude_gemini',b)]
                          if isinstance(source,dict) and not policy.is_excluded(source)]
            if not candidates:
                output[cid] = copy.deepcopy(previous)
                output[cid].update(manual_review_pending=True, provisional_ui_selection=selection,
                                   final_status='EXCLUDE', full_text_status='EXCLUDE')
            for index,(track,source,extraction) in enumerate(candidates):
                extraction=copy.deepcopy(extraction or {})
                extraction.update(common.normalize_extraction(policy.normalise_estimate(extraction)))
                row = _normalize_row(previous, extraction, selection, track, candidate=True)
                key = cid if index == 0 else cid+'::pending:'+track
                row['context_id'] = key
                output[key] = row
        rendered = [r for r in output.values() if r.get('provisional_origin_context_id') == cid]
        audit.extend({'id': rid, 'context_id': cid, 'pending': True, 'projected': True,
            **{k:v for k,v in selection.items() if k not in {'extraction','higher_jbi'}},
            'effective_poolable': any(r.get('final_status') == 'INCLUDE_META' for r in rendered),
            'display_rows': len(rendered),
            'projected_rows': [{'context_id':r['context_id'],'source':r.get('provisional_candidate_track'),
                'jbi_grade':(r.get('jbi') or {}).get('grade'),'status':r.get('final_status'),
                'extraction':{k:(r.get('extraction') or {}).get(k) for k in (*policy.KEY_FIELDS,'effect_type')},
                'independent_hold_preserved':(r.get('provisional_ui_selection') or {}).get('independent_hold_preserved'),
                'normalization_error':(r.get('provisional_ui_selection') or {}).get('normalization_error')} for r in rendered]} for rid in ids)
    return output, audit


def project_supplemental(records, tracks, combined_root, protected=()):
    """Replay separate scientific review items without extending the discrepancy manifest."""
    path = Path(combined_root)/SUPPLEMENTAL_CHECKPOINT
    if not path.exists(): return records, []
    saved = json.loads(path.read_text())
    # A supplemental receipt cannot replace a normal projected comparison context.
    for entry in saved.get('rows', []):
        cid = (entry.get('source_row') or {}).get('Context ID')
        if records.get(cid, {}).get('manual_review_pending') is True:
            raise ValueError('Supplemental review duplicates a projected context: '+str(cid))
    return project(records, tracks, saved, protected)


def effective_packet(packet, tracks, combined_root, protected=()):
    path = Path(combined_root)/CHECKPOINT
    if not path.exists(): return packet, []
    if ROOT.name == 'metaFemina':
        tracks = copy.deepcopy(tracks)
        for track in ('chatgpt','claude_gemini'):
            source = ROOT/'data/poolability_policy/2026-09-05'/(track+'_effective_results.json')
            if source.exists():tracks.setdefault(track,{}).update({r['context_id']:r for r in json.loads(source.read_text())['results']})
    records = {r['context_id']:r for r in packet['results']}
    views,audit = project(records,tracks,json.loads(path.read_text()),protected)
    views, supplemental = project_supplemental(views, tracks, combined_root, protected)
    effective = copy.deepcopy(packet); effective['results'] = [views[cid] for cid in sorted(views)]
    effective['provisional_ui_version'] = temporary.VERSION
    effective['authoritative_results_digest'] = common.digest(records)
    effective['provisional_supplemental_audit'] = supplemental
    return effective,audit


def persist(combined_root, effective, audit):
    if not audit:return
    write(Path(combined_root)/'provisional_ui/effective_screening.json',effective)
    write(Path(combined_root)/'provisional_ui/application_audit.json',audit)
    if effective.get('provisional_supplemental_audit'):
        write(Path(combined_root)/'provisional_ui/supplemental_application_audit.json',effective['provisional_supplemental_audit'])


def decorate_row(row, item):
    for key in MARKERS:
        if key in item:row[key] = copy.deepcopy(item[key])
    if item.get('provisional_ui_display_only'):
        row.update(ui_display_only=True,display_only=True,policy_poolable=False,manual_poolable=False,
                   pool_in_parent_meta_analysis=False)
        row['ui_display_reason'] = row['display_only_reason'] = 'This estimate does not meet the pooling criteria.'
    return row
