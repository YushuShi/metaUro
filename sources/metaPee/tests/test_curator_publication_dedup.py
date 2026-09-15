"""The curator's publication choice is durable and limited to one scope."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import pytest
from scripts import apply_curator_publication_dedup as importer
from scripts import astra_fulltext_common as common
from scripts import astra_fulltext_overlay as fulltext
from scripts import curator_dedup_overlay as overlay
from scripts import human_review_overlay as human
from scripts import reconcile_screening_tracks as reconcile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def evidence():
    read = lambda path: json.loads((ROOT / path).read_text())
    records = {r['context_id']: r for r in read('combined/effective_results.json')['results']}
    tracks = {name: {r['context_id']: r for r in read(
        'outputs/poolability_policy_2026-09-05/' + name + '_effective_results.json')['results']}
        for name in ('chatgpt', 'claude_gemini')}
    return overlay.checkpoint(ROOT / 'combined'), records, tracks


def test_curator_choice_preserves_selected_record_and_prior_human_receipts(evidence):
    ledger, records, tracks = evidence
    assert ledger['decisions'] == [importer.DECISION]
    assert common.digest(records[importer.RETAINED]) == ledger['retained_record_sha256']
    assert importer.sha(ROOT / 'combined/human_review/current_decisions.json') == ledger['prior_human_checkpoint_sha256']
    target = records[importer.TARGET]
    assert target['final_status'] == 'EXCLUDE'
    assert common.is_human(target)
    assert fulltext.candidates_for_record(target, tracks) == []
    assert target['requires_key_review'] is False
    assert len(target['curator_dedup_history']) == 1
    assert records['28159493|Alcohol|Renal cell carcinoma']['final_status'] == 'ELIGIBLE_NOT_POOLABLE'


def test_exclusion_replayed_after_model_astra_and_human_layers(evidence):
    ledger, records, tracks = evidence
    cid = importer.TARGET
    a, b = tracks['chatgpt'][cid], tracks['claude_gemini'][cid]
    model = reconcile.reconcile_context(a, b)[0]
    rebuilt = fulltext.replay_record(model, a, b, ROOT / 'combined')
    rebuilt = human.replay_record(rebuilt, a, b, ROOT / 'combined')
    actual = overlay.replay_record(rebuilt, a, b, ROOT / 'combined')
    assert common.digest(actual) == ledger['record_snapshots'][cid]['record_sha256']
    assert overlay.replay_record(actual, a, b, ROOT / 'combined') == actual
    assert actual == records[cid]
    other = records['28159493|Alcohol|Renal cell carcinoma']
    assert overlay.replay_record(other, tracks['chatgpt'][other['context_id']],
                                 tracks['claude_gemini'][other['context_id']], ROOT / 'combined') is other


def test_replay_rejects_changed_source(evidence):
    _, records, tracks = evidence
    changed = copy.deepcopy(tracks['chatgpt'][importer.TARGET])
    changed['title'] += ' changed'
    with pytest.raises(ValueError, match='Curator-deduplicated source changed'):
        overlay.replay_record(records[importer.TARGET], changed,
                              tracks['claude_gemini'][importer.TARGET], ROOT / 'combined')


def test_parent_filter_blocks_inherited_article_without_global_removal():
    rows = [{'PMID': '28159493', 'disease_hierarchy_inherited': False},
            {'PMID': '28159493', 'disease_hierarchy_inherited': True},
            {'PMID': '25866035'}, {'PMID': '99999999'}]
    original = copy.deepcopy(rows)
    kept = overlay.filter_rows(rows, 'Alcohol', 'Kidney and renal pelvis cancer')
    assert [row['PMID'] for row in kept] == ['25866035', '99999999']
    assert rows == original
    for exposure, disease, endpoint in [('Alcohol', 'Renal cell carcinoma', 'Incidence'),
                                        ('Coffee', 'Kidney and renal pelvis cancer', 'Incidence'),
                                        ('Alcohol', 'Kidney and renal pelvis cancer', 'Mortality')]:
        assert overlay.filter_rows(rows, exposure, disease, endpoint) is rows


def test_dynamic_disease_composition_cannot_restore_rejected_child():
    from disease_hierarchy import compose_result
    scopes = {'parent': {'disease': 'Kidney and renal pelvis cancer'},
              'child': {'disease': 'Renal cell carcinoma'}}
    base = {'canonical_track': 'combined', 'studies': [{'PMID': '25866035', 'Sample Size': 477325}]}
    child = {'canonical_track': 'combined', 'studies': [
        {'PMID': '28159493', 'Sample Size': 477325},
        {'PMID': '25866035', 'Sample Size': 477325}]}
    original = copy.deepcopy(child)
    composed = compose_result(base, [('child', child, 'child-cache')], 'parent', scopes,
                              'Alcohol', 'Incidence', include_direct_only=True)
    assert [row['PMID'] for row in composed['studies']] == ['25866035', '25866035']
    assert child == original


def test_checkpoint_cache_revalidates_changed_decisions(tmp_path, evidence):
    ledger = copy.deepcopy(evidence[0])
    path = tmp_path / overlay.CHECKPOINT
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(ledger))
    assert overlay.checkpoint(tmp_path)['decisions'] == [importer.DECISION]
    ledger['decisions'][0]['retained_pmid'] = 'changed without a new receipt'
    path.write_text(json.dumps(ledger))
    with pytest.raises(ValueError, match='Invalid curator publication-dedup checkpoint'):
        overlay.checkpoint(tmp_path)


@pytest.mark.parametrize('mode', ['yes', 'no'])
def test_saved_target_and_child_views_remain_scoped(mode):
    root = ROOT / 'combined/provisional_ui/subexposure_views' / mode / 'Cached_results/alcohol'
    for suffix in ('core', 'all'):
        parent = json.loads((root / ('kidney_and_renal_pelvis_cancer_incidence_true_' + suffix + '.json')).read_text())
        assert all(str(row.get('PMID')) != '28159493' for row in parent['studies'])
        retained = [row for row in parent['studies'] if str(row.get('PMID')) == '25866035'
                    and row.get('Effect Size') is not None]
        assert len(retained) == 1
        assert [(r['Sample Size'], r['Effect Type'], r['Effect Size'], r['Lower CI'], r['Upper CI']) for r in retained] == [(477325, 'HR', .91, .63, 1.3)]
        child = json.loads((root / ('renal_cell_carcinoma_incidence_true_' + suffix + '.json')).read_text())
        assert any(str(row.get('PMID')) == '28159493' for row in child['studies'])
    manifest = json.loads((ROOT / 'combined/provisional_ui/manifest.json').read_text())
    assert 'combined/' + overlay.CHECKPOINT in manifest['protected_source_hashes']


def test_import_reapplication_is_a_verified_noop(tmp_path):
    before = importer.sha(ROOT / 'combined/effective_results.json')
    result = importer.run(tmp_path / 'repeat', apply=True)
    assert result['already_applied'] is True
    assert result['writes_to_source'] == 0
    assert importer.sha(ROOT / 'combined/effective_results.json') == before
