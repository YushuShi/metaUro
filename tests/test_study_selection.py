from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study_selection import _report_counts, build_selection_flow, selection_catalog


def _first(pmid, context, decision='PASS'):
    return {'pmid': pmid, 'context_id': context, 'decision': decision}


def _final(pmid, context, status, reason=None):
    return {'pmid': pmid, 'context_id': context, 'final_status': status, 'exclusion_code': reason}


def test_reports_are_partitioned_across_conflicting_contexts_without_double_counting():
    first = [_first('1', 'a'), _first('1', 'b', 'FAIL'), _first('1', 'c'),
             _first('2', 'd'), _first('2', 'e'), _first('3', 'f', 'FAIL'),
             _first('4', 'g')]
    final = [_final('1', 'a', 'INCLUDE_META'), _final('1', 'c', 'EXCLUDE', 'S2-REVIEW'),
             _final('2', 'd', 'EXCLUDE', 'S2-ANIMAL'), _final('2', 'e', 'EXCLUDE', 'S2-REVIEW'),
             _final('4', 'g', 'ELIGIBLE_NOT_POOLABLE')]
    result = _report_counts(first, final)
    assert (result['screened'], result['screening_excluded'], result['assessed']) == (4, 1, 3)
    assert (result['eligibility_excluded'], result['included_reports']) == (1, 2)
    assert (result['poolable_reports'], result['nonpoolable_reports']) == (1, 1)
    assert result['reasons'] == [{'label': 'Multiple reasons across exposure or cancer contexts', 'count': 1}]


def test_effect_rows_and_first_stage_exclusions_do_not_inflate_counts_or_reasons():
    first = [_first('1', 'a'), _first('1', 'b', 'FAIL'), _first('2', 'c')]
    final = [_final('1', 'a', 'EXCLUDE', 'S2-REVIEW'),
             _final('1', 'a', 'EXCLUDE', 'S2-REVIEW'),
             _final('1', 'b', 'EXCLUDE', 'S1-NOT-DIRECT'),
             _final('2', 'c', 'INCLUDE_META'), _final('2', 'c', 'INCLUDE_META')]
    result = _report_counts(first, final)
    assert (result['assessed'], result['eligibility_excluded'], result['included_reports']) == (2, 1, 1)
    assert result['reasons'] == [{'label': 'Review or meta-analysis without an eligible primary estimate', 'count': 1}]


@pytest.mark.parametrize('first,final', [
    ([_first('1', 'a')], []),
    ([_first('1', 'a', 'FAIL')], [_final('1', 'a', 'INCLUDE_META')]),
    ([_first('1', 'a')], [_final('2', 'a', 'INCLUDE_META')]),
    ([_first('1', 'a'), _first('1', 'a')], [_final('1', 'a', 'INCLUDE_META')]),
    ([_first('1', 'a')], [_final('1', 'a', 'PENDING')]),
])
def test_incomplete_or_mismatched_audits_are_not_presented_as_complete(first, final):
    with pytest.raises(ValueError):
        _report_counts(first, final)


def test_current_urinary_flow_is_exposure_specific_and_reconciles():
    result = build_selection_flow('urinary', 'Bladder cancer', 'Vitamin D')
    assert (result['screened'], result['screening_excluded'], result['assessed']) == (88, 49, 39)
    assert (result['eligibility_excluded'], result['included_reports'], result['poolable_reports'], result['nonpoolable_reports']) == (24, 15, 10, 5)
    assert sum(item['count'] for item in result['reasons']) == result['eligibility_excluded']
    assert result['initial_reasons'] == [{'label': 'No direct nutritional exposure–cancer evidence at initial screening', 'count': 49}]
    assert result['context_diseases'] == [
        'Bladder cancer', 'Urothelial carcinoma of the bladder',
        'Squamous cell carcinoma of the bladder', 'Adenocarcinoma of the bladder',
        'Small cell carcinoma of the bladder',
    ]
    assert len(result['provenance']) >= 5
    assert result['supplemental_reports'] == 0
    assert sum(item['count'] for item in result['nonpoolable_reasons']) == result['nonpoolable_reports']
    assert all(len(item['sha256']) == 64 for item in result['provenance'])
    result['reasons'].clear()
    assert build_selection_flow('urinary', 'Bladder cancer', 'Vitamin D')['reasons']


def test_vital_alcohol_rcc_estimate_is_pooled_once_and_prisma_reconciles():
    import json
    from automation.sync_sources import assembled_source

    root = Path(__file__).resolve().parents[1]
    packet_path = (
        assembled_source(root, 'metaPee')
        / 'combined/provisional_ui/subexposure_views/no/Cached_results/alcohol/'
        'renal_cell_carcinoma_incidence_true_core.json'
    )
    packet = json.loads(packet_path.read_text())
    selected = [
        row for row in packet['studies']
        if str(row.get('PMID')) == '23665301'
        and row.get('subexposure_pooling_selected') is True
        and row.get('disease_hierarchy_pooling_selected') is not False
    ]
    assert len(selected) == 1
    assert selected[0]['Primary Estimate'] is True
    assert selected[0]['Effect Type'] == 'HR'
    assert selected[0]['Effect Size'] == pytest.approx(0.69)
    assert selected[0]['Lower CI'] == pytest.approx(0.43)
    assert selected[0]['Upper CI'] == pytest.approx(1.11)

    flow = build_selection_flow('urinary', 'Renal cell carcinoma', 'Alcohol')
    assert (flow['included_reports'], flow['poolable_reports'], flow['nonpoolable_reports']) == (38, 19, 19)
    assert 'Specific pooling reason not recorded' not in {
        reason['label'] for reason in flow['nonpoolable_reasons']
    }


def test_current_prostate_selection_is_exposure_specific():
    result = build_selection_flow('prostate', 'Prostate cancer', 'Vitamin D')
    # The prior monthly ledger includes first-stage FAIL PMIDs 42553583, 42608357, and 42737443.
    assert (result['screened'], result['screening_excluded'], result['assessed']) == (1218, 741, 477)
    assert (result['eligibility_excluded'], result['included_reports'], result['poolable_reports'], result['nonpoolable_reports']) == (417, 60, 28, 32)
    assert sum(item['count'] for item in result['reasons']) == result['eligibility_excluded']
    assert result['context_diseases'] == ['Prostate cancer']
    assert len(result['provenance']) >= 5
    assert result['supplemental_reports'] == 0
    assert sum(item['count'] for item in result['nonpoolable_reasons']) == result['nonpoolable_reports']


def test_primary_pooling_reasons_count_publications_once_and_ignore_pooled_reports():
    from selection_pooling_reasons import pooling_reason_counts
    rows = [
        {'PMID': '1', 'Quality Score': 'Fair', 'missing_quantities': ['effect size']},
        {'PMID': '1', 'Quality Score': 'Fair'},
        {'PMID': '2', 'missing_quantities': ['confidence interval']},
        {'PMID': '3', 'manual_review_pending': True},
    ]
    reasons = pooling_reason_counts({'1', '2', '3', '4'}, {'3'}, rows)
    assert {row['label']: row['count'] for row in reasons} == {
        'Quality below the current pooling threshold': 1,
        'Missing quantitative information': 1,
        'Specific pooling reason not recorded': 1,
    }


def test_current_complete_estimate_does_not_reuse_old_missing_data_reason():
    from selection_pooling_reasons import pooling_reason_counts
    reasons = pooling_reason_counts({'1'}, set(), [{'PMID': '1', 'final_status': 'INCLUDE_META'}], [{'pmid': '1', 'missing_key_fields': ['effect_size']}])
    assert reasons == [{'label': 'Specific pooling reason not recorded', 'count': 1}]


def test_current_moderate_quality_is_not_overridden_by_old_fair_or_other_estimate():
    from selection_pooling_reasons import pooling_reason_counts
    rows = [{'PMID': '1', 'final_status': 'ELIGIBLE_NOT_POOLABLE', 'Quality Score': 'Moderate'}, {'PMID': '2', 'Quality Score': 'Fair'}, {'PMID': '2', 'Quality Score': 'Moderate'}]
    reasons = pooling_reason_counts({'1', '2'}, set(), rows, [{'pmid': '1', 'jbi': {'grade': 'Fair'}}])
    assert reasons == [{'label': 'Specific pooling reason not recorded', 'count': 2}]


def test_pool_is_below_retained_and_print_has_dedicated_handler():
    root = Path(__file__).resolve().parents[1]
    template = (root / 'templates/study_selection.html').read_text()
    assert template.index('flow-box--nonpoolable') < template.index('flow-stage--pooled')
    assert 'flow-box--included flow-next' in template
    assert 'id="print-selection"' in template
    assert 'window.print()' in (root / 'static/study-selection.js').read_text()
    css = (root / 'static/study-selection.css').read_text()
    assert '.flow-stage--pooled .flow-box--pool { grid-column: 2;' in css
    assert '@media screen and (max-width: 750px)' in css


def test_selector_only_lists_current_direct_exposure_pools():
    urinary = selection_catalog('urinary')
    prostate = selection_catalog('prostate')
    assert {disease: len(exposures) for disease, exposures in urinary.items()} == {
        'Bladder cancer': 55 + 1,  # Reviewed multisite UPF addition.
        'Clear cell renal cell carcinoma': 3,
        'Kidney and renal pelvis cancer': 50,
        'Papillary renal cell carcinoma': 1,
        'Renal cell carcinoma': 43 + 1,
        'Urothelial carcinoma of the bladder': 9,
    }
    assert len(prostate['Prostate cancer']) == 101
    import json
    root = Path(__file__).resolve().parents[1]
    registry = json.loads((root/'evidence-revisions/multicancer-2026-09-29/metaPee/registry.json').read_text())
    for disease in ('Bladder cancer', 'Renal cell carcinoma'):
        assert 'Ultra-processed food intake' in urinary[disease]
        assert any(entry['record']['cancer'] == disease and entry['record']['exposure'] == 'Ultra-processed food intake'
                   and not entry['hold'] for entry in registry['entries'])
    assert 'Egg protein' not in prostate['Prostate cancer']
    unavailable = build_selection_flow('prostate', 'Aggressive prostate cancer')
    assert unavailable['available'] is False and unavailable['exposures'] == []


def test_invalid_cohort_is_rejected():
    with pytest.raises(ValueError):
        build_selection_flow('../unrelated')


def test_targeted_review_is_not_fabricated_as_initial_screening():
    result = build_selection_flow('urinary', 'Bladder cancer', 'Ultra-processed food intake')
    assert result['supplemental_reports'] > 0
    assert result['assessed'] == result['screened'] - result['screening_excluded'] + result['supplemental_reports']
    assert result['included_reports'] + result['eligibility_excluded'] == result['assessed']
    assert result['poolable_reports'] + result['nonpoolable_reports'] == result['included_reports']
