"""Request-local case estimates and an optional minimum-case filter.

Reported counts are never replaced. Estimates use the selected cumulative
baseline risk, not a saved estimate or a study's historical lifetime risk.
"""
from collections.abc import Mapping
import math

CASE_FIELDS = ('Estimated Cases', 'estimated_cases', 'Cases Estimated', 'cases_estimated',
               'Estimated Cases Method', 'Case Estimation', 'case_estimation', 'Case Estimation Incidence')


def count(value, *, allow_zero=True):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer() or number < 0:
        return None
    if number == 0 and not allow_zero:
        return None
    return int(number)


def minimum_cases(value=0):
    if value is None or value == '':
        return 0
    result = count(value)
    if result is None:
        raise ValueError('Minimum cases must be a nonnegative whole number.')
    return result


def project_cases(row, baseline):
    """Return a copy with fresh estimates; do not change source evidence."""
    result = dict(row)
    info = dict(baseline) if isinstance(baseline, Mapping) else {'p0': baseline}
    try:
        risk = float(info.get('p0'))
    except (TypeError, ValueError):
        risk = float('nan')
    reported = count(row.get('Cases'))
    size = next((n for key in ('Sample Size', 'Participants', 'total_n')
                 if (n := count(row.get(key), allow_zero=False)) is not None), None)
    estimate = provenance = None
    if reported is None and size is not None and math.isfinite(risk) and 0 <= risk <= 1:
        estimate = min(size, int(round(size * risk)))
        provenance = {key: info[key] for key in
                      ('source', 'disease', 'sex', 'race', 'age', 'years') if key in info}
        provenance.update(method='sample_size_times_selected_baseline_risk',
                          total_n=size, risk=risk, risk_percent=risk * 100,
                          rounding='nearest_integer_ties_to_even')
    result.update({
        'Estimated Cases': estimate, 'estimated_cases': estimate,
        'Cases Estimated': estimate is not None, 'cases_estimated': estimate is not None,
        'Estimated Cases Method': 'N × selected baseline cumulative incidence' if estimate is not None else None,
        'Case Estimation': provenance, 'case_estimation': provenance,
        'Case Estimation Incidence': risk if estimate is not None else None,
    })
    return result


def effective_cases(row):
    reported = count(row.get('Cases'))
    return reported if reported is not None else count(row.get('Estimated Cases'))


def meets_minimum(row, minimum=0):
    minimum = minimum_cases(minimum)
    value = effective_cases(row)
    return minimum == 0 or (value is not None and value >= minimum)


def project_frame(frame, baseline):
    result = frame.copy(deep=True)
    rows = [project_cases(row, baseline(row) if callable(baseline) else baseline)
            for row in result.to_dict(orient='records')]
    for field in CASE_FIELDS:
        result[field] = [row[field] for row in rows]
    return result


def filter_frame(frame, minimum=0):
    minimum = minimum_cases(minimum)
    if minimum == 0:
        return frame
    return frame.loc[[meets_minimum(row, minimum) for row in frame.to_dict(orient='records')]].copy()
