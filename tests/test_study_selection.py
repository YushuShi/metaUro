from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study_selection import _report_counts, build_selection_flow


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
    assert result['reasons'] == [{'label': 'Review article', 'count': 1}]


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


def test_current_urinary_audit_reconciles_and_preserves_unknown_counts():
    result = build_selection_flow('urinary')
    assert (result['screened'], result['screening_excluded'], result['assessed']) == (15095, 13981, 1114)
    assert (result['eligibility_excluded'], result['included_reports'], result['poolable_reports'], result['nonpoolable_reports']) == (794, 320, 174, 146)
    assert sum(item['count'] for item in result['reasons']) == result['eligibility_excluded']
    for key in ('identified', 'duplicate_removed', 'automation_removed', 'other_removed', 'reports_sought', 'reports_not_retrieved', 'full_text_assessed', 'full_text_excluded', 'unique_studies'):
        assert result[key] is None
    assert len(result['provenance']) == 5
    assert 'Human review updates applied on 14 September 2026' in result['cohort_description']
    assert 'Duplicate-article decisions applied on 14 September 2026' in result['cohort_description']
    assert all(len(item['sha256']) == 64 for item in result['provenance'])
    result['reasons'].clear()
    assert build_selection_flow('urinary')['reasons']


def test_current_prostate_selection_reflects_applied_human_decisions():
    result = build_selection_flow('prostate')
    assert (result['screened'], result['screening_excluded'], result['assessed']) == (24974, 21729, 3245)
    assert (result['eligibility_excluded'], result['included_reports'], result['poolable_reports'], result['nonpoolable_reports']) == (2638, 607, 312, 295)
    assert sum(item['count'] for item in result['reasons']) == result['eligibility_excluded']
    assert result['unique_studies'] is None
    assert result['full_text_assessed'] is None
    assert result['provisional_reports'] == 0
    assert not any('not counted as included here' in note for note in result['notes'])
    assert len(result['provenance']) == 6
    assert 'Human review updates applied on 14 September 2026' in result['cohort_description']
    assert 'Duplicate-article decisions applied on 14 September 2026' in result['cohort_description']
    assert all(name not in ' '.join(result['notes']) for name in ('metaPee', 'MetaVir'))


def test_invalid_cohort_is_rejected():
    with pytest.raises(ValueError):
        build_selection_flow('../unrelated')
