"""Additional abstract estimates keep their own reported comparison provenance."""
import copy

import pytest

from scripts.aggressive_prostate_policy import _comparison_orientation, _derived_row


@pytest.mark.parametrize('comparison,expected', [
    ('Deficient (<12 ng/mL) versus adequate (20-<30 ng/mL) circulating 25(OH)D', 'lower_vs_higher'),
    ('Quartile 1 vs. Quartile 4 for total vitamin D intake', 'lower_vs_higher'),
    ('Lowest versus highest quintile', 'lower_vs_higher'),
    ('Highest compared with lowest quartile', 'higher_vs_lower'),
    ('Highest lifetime alcohol intake quartile compared with non-drinkers', 'higher_vs_lower'),
    ('High versus low intake; exact category definitions are not stated', 'higher_vs_lower'),
    ('> or = 2,000 vs. 500-<750 mg/day', 'higher_vs_lower'),
    ('430 g/d dairy intake versus 20.2 g/d dairy intake', 'higher_vs_lower'),
    ('Per 10 μg/dL increase', 'per_unit_increase'),
    ('Per 1-SD higher dietary intake', 'per_unit_increase'),
    ('Per 10 μg/dL decrease', 'per_unit_decrease'),
    ('Extreme quartiles of serum lycopene', None),
    ('Upper third of serum concentration', None),
    ('Lowest quartile; reference not specified', None),
])
def test_explicit_direction_only(comparison, expected):
    assert _comparison_orientation(comparison) == expected


def derived(comparison):
    template = {'PMID': '123', 'Direct Exposure': 'Vitamin D',
        'comparison': 'Unrelated old comparison', 'comparison_type': 'High versus low',
        'effect_inverted': True, 'direction_imputed': True,
        'direction_original_comparison': 'Unrelated previous estimate',
        'direction_imputation_reason': 'Unrelated assumption',
        'comparison_direction': 'higher_vs_lower', 'comparison_group': 'Wrong group',
        'Original Effect Size': 99, 'Primary Estimate': True}
    estimate = {'endpoint': 'aggressive', 'effect_type': 'OR', 'effect_size': 2.33,
        'lower_ci': 1.26, 'upper_ci': 4.28, 'sample_size': 1000, 'cases': 30,
        'comparison': comparison, 'effect_excerpt': 'Reported OR 2.33 (1.26–4.28).'}
    before = copy.deepcopy((template, estimate))
    result = _derived_row(template, estimate, {}, 'source-row')
    assert (template, estimate) == before
    return result


def test_reciprocal_reverses_ci_and_preserves_new_original_numbers():
    comparison = 'Deficient (<12 ng/mL) versus adequate (20-<30 ng/mL) circulating 25(OH)D'
    row = derived(comparison)
    assert row['Effect Size'] == pytest.approx(1 / 2.33)
    assert row['Lower CI'] == pytest.approx(1 / 4.28)
    assert row['Upper CI'] == pytest.approx(1 / 1.26)
    assert row['Original Effect Size'] == 2.33
    assert row['Original Lower CI'] == 1.26
    assert row['direction_original_comparison'] == comparison
    assert row['comparison_type'] == row['comparison'] == 'Higher versus lower'
    assert row['effect_inverted'] is True
    assert row['needs_inversion'] is False
    assert row['direction_imputed'] is False
    assert 'direction_imputation_reason' not in row
    assert 'comparison_group' not in row
    audit = row['disease_scope_review']['direction_normalization']
    assert audit['reported_estimate']['Effect Size'] == 2.33
    assert audit['inverted'] is True


def test_forward_estimate_does_not_inherit_template_inversion():
    row = derived('Highest versus lowest quintile')
    assert row['Effect Size'] == 2.33
    assert row['Original Effect Size'] == 2.33
    assert row['effect_inverted'] is False
    assert row['direction_original_comparison'] == 'Highest versus lowest quintile'


def test_unknown_numeric_direction_stays_visible_without_pooling():
    row = derived('Extreme quartiles of vitamin D')
    assert row['Effect Size'] == 2.33
    assert row['display_only'] is True
    assert row['policy_poolable'] is False
    assert row['pool_in_parent_meta_analysis'] is False
    assert row['disease_scope_review']['direction_normalization']['status'] == 'needs_review'
