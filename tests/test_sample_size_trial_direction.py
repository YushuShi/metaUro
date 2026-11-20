"""Trial annotations reflect estimate design and the selected effect direction."""
import pytest
from sample_size_trial_direction import is_randomized_trial, trial_direction


def trial(effect, pmid='1', **extra):
    return {'Design': 'Randomized controlled trial', 'PMID': pmid, 'Effect Size': effect, **extra}


@pytest.mark.parametrize('design', [
    'Randomized controlled trial', 'randomised placebo-controlled trial', 'RCT',
    'District-randomized field intervention study', 'Randomized prospective treatment study',
    'Single-center, triple-blind, parallel-group randomized placebo-controlled trial',
])
def test_randomized_design_variants(design):
    assert is_randomized_trial({'Design': design})


@pytest.mark.parametrize('design', [
    '', 'Prospective cohort', 'Mendelian randomization', 'Non-randomized clinical trial',
    'prospective cohort within randomised trial', 'RCT nested case-control analysis',
    'observational secondary analysis of a randomized trial', 'target trial emulation',
    'quasi-randomized trial', 'Clinical trial; randomization unclear', 'not randomized',
])
def test_observational_trial_cohorts_and_nonrandomized_studies_are_excluded(design):
    assert not is_randomized_trial({'Design': design})


@pytest.mark.parametrize('pool,effects,marker', [
    (.8, [.7, .99], '*'), (1.2, [1.01, 1.4], '*'),
    (.8, [.7, 1.1], '#'), (1.2, [.9, 1.4], '#'), (.8, [1], '#'),
])
def test_all_trials_must_share_point_estimate_direction(pool, effects, marker):
    rows = [trial(effect, str(index)) for index, effect in enumerate(effects)]
    # Wide intervals crossing the null do not determine the direction.
    for row in rows:
        row.update({'Lower CI': .3, 'Upper CI': 2})
    result = trial_direction(rows, pool)
    assert result['rct_marker'] == marker
    assert result['rct_count'] == len(rows)
    assert result['rct_same_direction_count'] + result['rct_other_direction_count'] == len(rows)


def test_no_trial_leaves_exposure_unmarked():
    assert trial_direction([{'Design': 'Cohort', 'Effect Size': .8}], .8) == {
        'rct_marker': '', 'rct_count': 0, 'rct_same_direction_count': 0, 'rct_other_direction_count': 0}


def test_trial_report_with_any_discordant_stratum_is_counted_once():
    result = trial_direction([trial(.7, '123'), trial(1.1, '123.0'), trial(.9, '456')], .8)
    assert result == {'rct_marker': '#', 'rct_count': 2,
                      'rct_same_direction_count': 1, 'rct_other_direction_count': 1}


def test_converted_effect_and_continuous_null_follow_native_pooling():
    assert trial_direction([trial(1.2, converted_ES=.8)], .9)['rct_marker'] == '*'
    assert trial_direction([trial(2, converted_ES=-2)], -1, null=0)['rct_marker'] == '*'
    assert trial_direction([trial(2)], -1, null=0)['rct_marker'] == '#'


def test_invalid_converted_estimate_does_not_fall_back_to_reported_contrast():
    assert trial_direction([trial(.8, converted_ES=float('nan'))], .9)['rct_marker'] == ''
    assert trial_direction([trial(1)], 1)['rct_marker'] != '*'
