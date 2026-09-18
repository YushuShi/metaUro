"""Selected-population estimates, user cutoffs, and source isolation."""
import copy
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from case_counts import count, project_cases, project_frame, filter_frame, minimum_cases, meets_minimum


@pytest.mark.parametrize('reported', [0, 1, 49, 50, 51, '1,200'])
def test_reported_cases_take_precedence(reported):
    source = {'Cases': reported, 'Sample Size': 2000, 'Estimated Cases': 999}
    result = project_cases(source, {'p0': .1})
    assert result['Cases'] == reported
    assert result['Estimated Cases'] is None
    assert result['Cases Estimated'] is False
    assert meets_minimum(result, count(reported))
    assert not meets_minimum(result, count(reported) + 1)
    assert source['Estimated Cases'] == 999


@pytest.mark.parametrize('missing', [None, '', '-', 'NA', float('nan')])
def test_current_baseline_replaces_stale_estimates(missing):
    source = {'Cases': missing, 'Sample Size': '1,000', 'Estimated Cases': 900}
    original = copy.deepcopy(source)
    first = project_cases(source, {'p0': .049, 'age': 60, 'years': 10, 'sex': 'female', 'race': 'all'})
    second = project_cases(first, {'p0': .1, 'age': 70, 'years': 15, 'sex': 'male', 'race': 'asian'})
    assert first['Estimated Cases'] == 49
    assert not meets_minimum(first, 50)
    assert second['Estimated Cases'] == 100
    assert meets_minimum(second, 50)
    assert second['Case Estimation']['age'] == 70
    assert second['Case Estimation']['race'] == 'asian'
    unavailable = project_cases(second, None)
    assert unavailable['Estimated Cases'] is None
    assert unavailable['Case Estimation'] is None
    assert meets_minimum(unavailable, 0)
    assert not meets_minimum(unavailable, 1)
    assert source['Estimated Cases'] == original['Estimated Cases']


def test_zero_risk_unknown_size_rounding_and_validation():
    assert project_cases({'Sample Size': 100}, 0)['Estimated Cases'] == 0
    assert project_cases({'Sample Size': 100}, .025)['Estimated Cases'] == 2
    assert project_cases({'Sample Size': 100}, .035)['Estimated Cases'] == 4
    assert project_cases({'Participants': '1,000'}, .1)['Estimated Cases'] == 100
    for size in [None, '', 0, -1, 'NA']:
        result = project_cases({'Sample Size': size}, .1)
        assert result['Estimated Cases'] is None
        assert meets_minimum(result)
    for value in [-1, .5, True, '50 studies', 'NaN']:
        with pytest.raises(ValueError):
            minimum_cases(value)
    assert minimum_cases() == 0


def test_frames_preserve_reported_counts_and_default_inclusion():
    frame = pd.DataFrame([{'Cases': value, 'Sample Size': 1000} for value in [0, 1, 49, 50, 51, None]])
    result = project_frame(frame, .1)
    assert len(filter_frame(result)) == 6
    assert len(filter_frame(result, 50)) == 3
    assert len(filter_frame(project_frame(frame, None), 50)) == 2
    assert 'Estimated Cases' not in frame
    assert len(filter_frame(result, 1000)) == 0


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required for browser case-count behavior')
def test_browser_counts_and_selection():
    root = Path(__file__).resolve().parents[1]
    scripts = ['tests/case_counts_runtime.cjs', 'tests/minimum_case_filter_runtime.cjs',
               'tests/case_settings_runtime.cjs']
    for script in scripts:
        if not (root / script).exists():
            continue
        result = subprocess.run(['node', script], cwd=root, text=True, capture_output=True)
        assert result.returncode == 0, result.stdout + result.stderr
