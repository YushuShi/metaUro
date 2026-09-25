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
    assert len(result['provenance']) == 4
    assert all(len(item['sha256']) == 64 for item in result['provenance'])
    result['reasons'].clear()
    assert build_selection_flow('urinary', 'Bladder cancer', 'Vitamin D')['reasons']


def test_current_prostate_selection_is_exposure_specific():
    result = build_selection_flow('prostate', 'Prostate cancer', 'Vitamin D')
    assert (result['screened'], result['screening_excluded'], result['assessed']) == (1215, 738, 477)
    assert (result['eligibility_excluded'], result['included_reports'], result['poolable_reports'], result['nonpoolable_reports']) == (417, 60, 28, 32)
    assert sum(item['count'] for item in result['reasons']) == result['eligibility_excluded']
    assert result['context_diseases'] == ['Prostate cancer']
    assert len(result['provenance']) == 4


def test_selector_only_lists_current_direct_exposure_pools():
    urinary = selection_catalog('urinary')
    prostate = selection_catalog('prostate')
    assert {disease: len(exposures) for disease, exposures in urinary.items()} == {
        'Bladder cancer': 55,
        'Clear cell renal cell carcinoma': 3,
        'Kidney and renal pelvis cancer': 50,
        'Papillary renal cell carcinoma': 1,
        'Renal cell carcinoma': 43,
        'Urothelial carcinoma of the bladder': 9,
    }
    assert len(prostate['Prostate cancer']) == 101
    assert 'Ultra-processed food intake' not in urinary['Renal cell carcinoma']
    assert 'Egg protein' not in prostate['Prostate cancer']
    unavailable = build_selection_flow('prostate', 'Aggressive prostate cancer')
    assert unavailable['available'] is False and unavailable['exposures'] == []


def test_invalid_cohort_is_rejected():
    with pytest.raises(ValueError):
        build_selection_flow('../unrelated')
