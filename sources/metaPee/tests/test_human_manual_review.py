"""Regression checks for the reviewed metaPee evidence and replay contract."""
from __future__ import annotations
import copy
import csv
import json
from pathlib import Path
import pytest
from scripts import apply_human_manual_review as importer
from scripts import astra_fulltext_common as common
from scripts import astra_fulltext_overlay as fulltext
from scripts import human_review_overlay as overlay
from scripts import key_comparison_policy as policy
from scripts import provisional_ui_runtime
from scripts import reconcile_screening_tracks as reconcile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def evidence():
    read = lambda name: json.loads((ROOT / name).read_text())
    ledger = read('combined/human_review/current_decisions.json')
    records = {row['context_id']: row for row in read('combined/effective_results.json')['results']}
    tracks = {name: {row['context_id']: row for row in read(
        'outputs/poolability_policy_2026-09-05/' + name + '_effective_results.json')['results']}
        for name in ('chatgpt', 'claude_gemini')}
    return ledger, records, tracks


def test_all_decisions_completed_and_source_packets_unchanged(evidence):
    ledger, records, tracks = evidence
    assert len(ledger['decisions']) == 68
    assert len(ledger['record_snapshots']) == 56
    assert importer.sha(ROOT / 'combined/human_review' / ledger['input_file']) == ledger['input_sha256']
    assert all(event['applied'] for event in ledger['decisions'])
    assert json.loads((ROOT / 'combined/provisional_ui/pending_manifest.json').read_text())['rows'] == []
    assert json.loads((ROOT / 'combined/human_review/review_status.json').read_text())['review_rows'] == 0
    for name, digest in ledger['source_hashes'].items():
        assert importer.sha(ROOT / 'outputs/poolability_policy_2026-09-05' / (name + '_effective_results.json')) == digest
    for cid in ledger['record_snapshots']:
        assert common.is_human(records[cid])
        assert records[cid]['manual_review_pending'] is False
        assert records[cid]['requires_key_review'] is False


def test_pipeline_selection_uses_exact_index_and_atomic_bundle(evidence):
    ledger, records, tracks = evidence
    for event in ledger['decisions']:
        if event['action'] != 'select': continue
        name, cid = event['selected_track'], event['context_id']
        extraction = policy.extracts(tracks[name][cid])[event['source_indices'][name]]
        retained = [item for item in fulltext.candidates_for_record(records[cid], tracks)
                    if item['pair_index'] == event['estimate_pair'] - 1]
        assert len(retained) == 1
        assert importer.bundle(retained[0]['extraction']) == importer.bundle(extraction)
    # Pair1 is ChatGPT source index1, not positional index0, for this PMID.
    record = records['36557227|Carnitine|Kidney and renal pelvis cancer']
    assert record['extraction']['effect_size'] == 2.31
    assert record['extraction']['ci_lower'] == 1.36


def test_selenium_inverted_exactly_once(evidence):
    ledger, records, tracks = evidence
    cid = '2790827|Selenium|Bladder cancer'
    original = policy.extracts(tracks['chatgpt'][cid])[0]
    item, = fulltext.candidates_for_record(records[cid], tracks)
    extraction = item['extraction']
    assert extraction['effect_type'] == 'OR'
    assert extraction['effect_size'] == pytest.approx(1 / original['effect_size'])
    assert extraction['ci_lower'] == pytest.approx(1 / original['ci_upper'])
    assert extraction['ci_upper'] == pytest.approx(1 / original['ci_lower'])
    assert extraction['inversion_applied'] is True
    assert extraction['needs_inversion'] is False
    visible = reconcile.app_row(item)
    assert visible['Effect Size'] == extraction['effect_size']
    assert visible['Lower CI'] == extraction['ci_lower']


def test_pair_removal_preserves_other_estimates(evidence):
    _, records, tracks = evidence
    cid = '23011480|Processed meat intake|Renal cell carcinoma'
    items = fulltext.candidates_for_record(records[cid], tracks)
    assert [(item['pair_index'] + 1, item['extraction']['effect_size']) for item in items] == [(1, 1.21), (2, 2.15)]
    assert records['12053032|Dairy|Bladder cancer']['final_status'] == 'EXCLUDE'
    assert records['12053032|Lactobacillus|Bladder cancer']['final_status'] == 'EXCLUDE'
    assert records['12053032|Fermented foods|Bladder cancer']['extraction']['effect_size'] == .61
    # Unreviewed pair1 remains and reviewed pair2 is updated, not deleted.
    other = records['19328261|Red meat|Renal cell carcinoma']
    assert [row['effect_size'] for row in policy.extracts(other)] == [4.43, 3.04]


def test_matching_corrections_retained_once_with_compatible_metadata(evidence):
    _, records, tracks = evidence
    expected = [('25258006|Processed meat intake|Renal cell carcinoma', 1.78, 493179, 'Female'),
                ('25258006|Red meat|Renal cell carcinoma', 1.36, 493179, 'Female'),
                ('30737562|Mediterranean diet|Bladder cancer', .85, 646222, 'Mixed')]
    for cid, effect, total, gender in expected:
        extraction, = policy.extracts(records[cid])
        assert (extraction['effect_size'], extraction['total_n'], extraction['gender']) == (effect, total, gender)
    corrected = records['27149545|Mediterranean diet|Bladder cancer']['extraction']
    assert importer.bundle(corrected) == dict(total_n=37442, effect_type='HR', effect_size=.89, ci_lower=.62, ci_upper=1.26)
    assert corrected['comparison_group'] == 'Q5 (healthy)'
    assert corrected['reference_group'] == 'Q1 (unhealthy)'
    assert 'per 1 standard deviation' not in corrected['comparison_type'].lower()


def test_all_snapshots_survive_model_and_astra_rebuild(evidence):
    ledger, records, tracks = evidence
    for cid, saved in ledger['record_snapshots'].items():
        a, b = tracks['chatgpt'][cid], tracks['claude_gemini'][cid]
        base = reconcile.reconcile_context(a, b)[0]
        base = fulltext.replay_record(base, a, b, ROOT / 'combined')
        actual = overlay.replay_record(base, a, b, ROOT / 'combined')
        assert common.digest(actual) == saved['record_sha256'] == common.digest(records[cid])


def test_changed_source_is_not_silently_replayed(evidence):
    ledger, records, tracks = evidence
    cid = next(iter(ledger['record_snapshots']))
    changed = copy.deepcopy(tracks['chatgpt'][cid])
    changed['title'] += ' changed'
    with pytest.raises(ValueError, match='Human-reviewed source changed'):
        overlay.replay_record(records[cid], changed, tracks['claude_gemini'][cid], ROOT / 'combined')


def test_changed_correction_note_is_not_silently_applied():
    for rid in importer.CORRECTIONS:
        entry = {'id': rid, 'manual_review': 'Use N=123 and HR=9 instead'}
        with pytest.raises(ValueError, match='Unexpected correction instruction'):
            importer.corrected_extraction(entry, {'chatgpt': {}})


def test_serving_view_ready_and_completed_summary_status():
    assert provisional_ui_runtime.ready(ROOT)
    manifest = json.loads((ROOT / 'combined/provisional_ui/manifest.json').read_text())
    assert manifest['pending_rows'] == 0
    assert manifest['human_review_rows_resolved'] == 68
    assert 'combined/human_review/current_decisions.json' in manifest['protected_source_hashes']
    for mode in ('yes', 'no'):
        summary = json.loads((ROOT / f'combined/provisional_ui/subexposure_views/{mode}/Plot/summary_manifest.json').read_text())
        assert summary['manual_review_pending'] is False
