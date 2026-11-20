"""Presentation metadata from the actual, direction-harmonized pooling rows."""
import math
import re


def is_randomized_trial(row):
    """Use the estimate's design, not the name of its parent trial cohort."""
    design = ' '.join(str(row.get(key) or '') for key in
                      ('Design', 'design', 'Study Design', 'study_design', 'Study Type', 'study_type')).lower()
    observational = r'cohort|case[\s-]*control|observational|cross[\s-]*sectional|mendelian|emulation'
    nonrandom = r'non[\s-]*random|not\s+random|quasi[\s-]*random|unclear|unknown\s+random'
    if re.search(observational, design) or re.search(nonrandom, design):
        return False
    return bool(re.search(r'\brct\b|\brandomi[sz]ed\b', design))


def _effect(row):
    # Native conversion can invert a contrast or harmonize a continuous scale.
    for field in ('converted_ES', 'Effect Size', 'effect'):
        if field not in row:
            continue
        try:
            value = float(row.get(field))
        except (TypeError, ValueError):
            if field == 'converted_ES':
                return None
            continue
        if math.isfinite(value):
            return value
        if field == 'converted_ES':
            return None
    return None


def trial_direction(rows, pooled_effect, null=1.0):
    """Mark * only when every selected trial estimate shares the pooled direction.

    Report counts are distinct publications. A report with several estimates is
    discordant if any estimate differs in direction, including an exact null.
    """
    empty = {'rct_marker': '', 'rct_count': 0, 'rct_same_direction_count': 0,
             'rct_other_direction_count': 0}
    try:
        pooled_effect = float(pooled_effect)
    except (TypeError, ValueError):
        return empty
    if not math.isfinite(pooled_effect):
        return empty
    trials = {}
    pooled_direction = (pooled_effect > null) - (pooled_effect < null)
    for index, row in enumerate(rows):
        if not is_randomized_trial(row):
            continue
        effect = _effect(row)
        if effect is None:
            continue
        identity = str(row.get('PMID') or row.get('pmid') or '').strip().removesuffix('.0')
        if identity.casefold() in {'', '-', 'none', 'nan', 'n/a'}:
            identity = str(row.get('DOI') or row.get('Study') or row.get('study') or index)
        same = pooled_direction != 0 and (effect > null) - (effect < null) == pooled_direction
        trials[identity] = trials.get(identity, True) and same
    same_count = sum(trials.values())
    other_count = len(trials) - same_count
    return {'rct_marker': ('#' if other_count else '*') if trials else '',
            'rct_count': len(trials), 'rct_same_direction_count': same_count,
            'rct_other_direction_count': other_count}
