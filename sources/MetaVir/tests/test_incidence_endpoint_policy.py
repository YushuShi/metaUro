"""Diagnosis-only endpoint checkpoint and projection safeguards."""
from pathlib import Path

import pandas as pd

import meta_analysis
from scripts import aggressive_prostate_policy as aggressive
from scripts import dual_screening
from scripts import incidence_endpoint_policy as endpoint
from scripts import replay_incidence_endpoint_decisions as replay


def _study(row_id, *, pmid='21930800', effect_id='saved-effect'):
    return {
        'PMID': pmid,
        'Effect ID': effect_id,
        'Study': 'Endpoint test',
        'Direct Exposure': 'Eggs',
        'Sample Size': 1000,
        'Cases': 100,
        'Effect Type': 'HR',
        'Effect Size': 1.5,
        'Lower CI': 1.1,
        'Upper CI': 2.0,
        'Quality Score': 'Good',
        'comparison_type': 'Higher versus lower',
        'Primary Estimate': True,
        'pool_in_parent_meta_analysis': True,
        '_test_row_id': row_id,
    }


def _review(row_id, endpoint_name='aggressive', additional=None):
    return {
        'article_classification': 'both',
        'aggressive_definition': 'Advanced cancer at diagnosis',
        'aggressive_definition_excerpt': 'Advanced cancer at diagnosis',
        'prospective_cohort': False,
        'cohort_total_n': None,
        'cohort_total_n_excerpt': '',
        'estimate_classifications': [{
            'row_id': row_id,
            'endpoint': endpoint_name,
            'supported_by_abstract': True,
            'endpoint_support_source': 'abstract',
            'endpoint_excerpt': 'Advanced cancer at diagnosis',
            'reason': 'Diagnosis endpoint.',
        }],
        'additional_estimates': list(additional or []),
    }


def _checkpoint(record):
    return {'records': [record]}


def _record(kind, identifier, pmid='21930800'):
    return {
        'pmid': pmid,
        'match_kind': kind,
        'match_id': identifier,
        'endpoint_class': 'lethal_prostate_cancer',
        'decision': 'exclude_non_diagnosis_endpoint',
        'reason': 'The failure event is death, not first diagnosis.',
    }


def test_checkpoint_is_complete_and_unique():
    document = endpoint.load_checkpoint()
    assert len(document['records']) == 24
    assert len({endpoint.record_id(record) for record in document['records']}) == 24
    assert document['source_documents'] == [
        'data/incidence_endpoint_review/user_instruction.txt',
        'data/incidence_endpoint_review/source_appraisal.md',
    ]


def test_saved_death_row_is_removed_but_preserved_for_replay():
    fatal_id = 'fatal-row'
    diagnosis_id = 'diagnosis-row'
    rows = [_study(fatal_id), _study(diagnosis_id, effect_id='diagnosis-effect')]
    reviews = {'21930800': {
        **_review(diagnosis_id, endpoint_name='general'),
        'estimate_classifications': [
            _review(fatal_id)['estimate_classifications'][0],
            _review(diagnosis_id, endpoint_name='general')['estimate_classifications'][0],
        ],
    }}
    parent, child, audit = aggressive.project(
        rows, 'Eggs', reviews, lambda row: row['_test_row_id'],
        endpoint_exclusions=_checkpoint(_record('source_row_id', fatal_id)))
    assert [row['Effect ID'] for row in parent] == ['diagnosis-effect']
    assert child == []
    actions = [item for item in audit if item['action'] == 'excluded_non_diagnosis_endpoint']
    assert len(actions) == 1
    assert actions[0]['excluded_source_row']['_test_row_id'] == fatal_id
    restored = aggressive.source_rows(parent, [actions[0]['excluded_source_row']])
    assert {row['_test_row_id'] for row in restored} == {fatal_id, diagnosis_id}


def test_derived_death_row_is_removed_without_losing_general_template():
    row = _study('general-row', pmid='23256145')
    estimate = {
        'exposure_key': 'eggs',
        'endpoint': 'aggressive',
        'effect_type': 'HR',
        'effect_size': 1.49,
        'lower_ci': .97,
        'upper_ci': 2.28,
        'sample_size': 1000,
        'cases': 50,
        'comparison': 'Higher versus lower',
        'effect_excerpt': 'Fatal prostate cancer HR 1.49 (0.97-2.28).',
        'count_excerpt': '50 fatal events.',
        'endpoint_definition': 'Fatal prostate cancer',
    }
    review = _review('general-row', endpoint_name='general', additional=[estimate])
    # Use the deterministic ID produced by the policy rather than duplicating
    # its digest implementation in this test.
    template = aggressive.annotate(
        row, review, 'general', review['estimate_classifications'][0], 'general-row')
    derived = aggressive._derived_row(template, estimate, review, 'general-row')
    checkpoint = _checkpoint(_record(
        'derived_effect_id', derived['Effect ID'], pmid='23256145'))
    parent, child, audit = aggressive.project(
        [row], 'Eggs', {'23256145': review},
        lambda value: value['_test_row_id'], endpoint_exclusions=checkpoint)
    assert [item['Effect ID'] for item in parent] == ['saved-effect']
    assert child == []
    assert any(item.get('match_id') == derived['Effect ID'] for item in audit)


def test_future_screening_prompts_require_first_diagnosis():
    for prompt in (
        dual_screening.FIRST_INSTRUCTIONS,
        dual_screening.SECOND_INSTRUCTIONS,
        dual_screening.FULL_TEXT_INSTRUCTIONS,
    ):
        assert 'first diagnosis' in prompt
        assert 'fatal/lethal disease defined by a later' in prompt
        assert 'modeled failure event' in prompt
    assert dual_screening.SCREENING_PROTOCOL_VERSION == 10


def test_policy_source_files_exist():
    for relative in endpoint.load_checkpoint()['source_documents']:
        assert (Path(__file__).resolve().parents[1] / relative).is_file()


def test_duplicate_study_labels_do_not_break_summary_alignment():
    rows = [
        {
            **_study(f'diagnosis-{index}', pmid=str(90000000 + index), effect_id=f'effect-{index}'),
            'Study': 'Shared cohort label',
            'Effect Type': 'RR',
            'Effect Size': 1.1 + index * .1,
            'Lower CI': .9 + index * .1,
            'Upper CI': 1.3 + index * .1,
            'SE': .1,
        }
        for index in range(2)
    ]
    frame = pd.DataFrame(rows)
    result = meta_analysis.perform_meta_analysis(
        frame, 'Prostate cancer', 'Endpoint test', df_all=frame,
        generate_plots=False, exclude_meta=True)
    assert 'error' not in result, result.get('error')
    assert len(result['studies']) == 2


def test_exact_replay_filters_only_checkpoint_matches():
    fatal = _study('fatal-row')
    fatal['disease_scope_review'] = {'source_row_id': 'fatal-row'}
    diagnosis = _study('diagnosis-row', effect_id='diagnosis-effect')
    retained, actions, excluded = replay.filter_rows(
        [fatal, diagnosis], _checkpoint(_record('source_row_id', 'fatal-row')))
    assert retained == [diagnosis]
    assert retained[0] is diagnosis
    assert excluded == [fatal]
    assert actions[0]['record_id'] == 'source_row_id:fatal-row'
