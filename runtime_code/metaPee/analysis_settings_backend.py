"""Request-local risk settings and projections of authoritative saved evidence.

Offline workflow functions retain their existing behavior. Live projections
never modify saved studies, canonical caches, or published source records.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from statistics import NormalDist
import base64
import copy
import io
import json
import math
import re
from case_counts import project_cases, project_frame, filter_frame, minimum_cases

_ACTIVE = ContextVar('seer_analysis_settings', default=None)
_COHORT_CONTEXT = ContextVar('metauro_cohort_selection', default=None)


def settings_active():
    return _ACTIVE.get() is not None


@contextmanager
def without_analysis_settings():
    """Keep generated shared evidence views independent of visitor settings."""
    token = _ACTIVE.set(None)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _study_key(row):
    projection_id = row.get('disease_hierarchy_projection_id')
    if str(projection_id or '').strip().casefold() in {'', '-', 'na', 'n/a', 'nan', 'none'}:
        projection_id = None
    return (str(row.get('PMID')), str(row.get('Study')),
            *(_number(row.get(field)) for field in ('Effect Size', 'Lower CI', 'Upper CI')),
            projection_id)


def _cohort_record_key(row):
    from cohort_identity import record_metadata_key
    return (record_metadata_key(row), _study_key(row)[-1])


def capture_pooling_rows(frame):
    """Record the actual rows retained by each repository's existing policies."""
    selected = select_cohort_pooling_frame(frame)
    # This is the engine's final working pooling frame, separate from df_all.
    # Keep recorded display rows intact while selecting this request's analysis.
    if len(selected) != len(frame):
        excluded = selected.attrs['cohort_selection_audit']['excluded']
        frame.drop(index=frame.index[[item['row_index'] for item in excluded]], inplace=True)
    active = _ACTIVE.get()
    if active is not None:
        active['pooled_study_keys'] = {_study_key(row) for row in frame.to_dict(orient='records')}
        active['pooled_plot_rows'] = frame.to_dict(orient='records')


@contextmanager
def latest_cohort_pooling(enabled=True):
    """Offline summary selection using the same final pooling gate as the UI."""
    context = {'enabled': bool(enabled)}
    token = _COHORT_CONTEXT.set(context)
    try:
        yield context
    finally:
        _COHORT_CONTEXT.reset(token)


def select_cohort_pooling_frame(frame):
    """Select latest publications only after existing pooling eligibility checks."""
    active, offline = _ACTIVE.get(), _COHORT_CONTEXT.get()
    enabled = bool(active and active['settings'].get('keep_latest_cohort')) or bool(offline and offline['enabled'])
    if not enabled:
        if offline is not None:
            offline['rows'] = frame.to_dict(orient='records')
        return frame
    from cohort_identity import filter_latest_cohort_frame
    selected = filter_latest_cohort_frame(frame, repo_root=Path(__file__).resolve().parents[2], group_fields=[])
    audit = selected.attrs.get('cohort_selection_audit', {})
    if offline is not None:
        offline.update(rows=selected.to_dict(orient='records'), audit=audit)
    if active is not None:
        active['cohort_evaluated_record_keys'] = {_cohort_record_key(row) for row in frame.to_dict(orient='records')}
        active['cohort_selection_audit'] = audit
        active['cohort_excluded_record_keys'] = {
            _cohort_record_key(row) for row in frame.to_dict(orient='records')
        }.difference(_cohort_record_key(row) for row in selected.to_dict(orient='records'))
    return selected



def _number(value):
    try:
        answer = float(value)
        return answer if math.isfinite(answer) else None
    except (TypeError, ValueError):
        return None


def _key(value):
    return ' '.join(str(value or '').split()).casefold()


def normalize_settings(values, catalog):
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError('analysis_settings must be an object.')
    defaults = catalog.get('defaults') or {}
    result = {
        'baseline_source': 'seer', 'age': 60, 'years': 10,
        'sex': defaults.get('sex', 'both'), 'race': defaults.get('race', 'all_races_ethnicities'),
        'manual_risk_percent': None, 'effect_measure': 'RR', 'forest_scale': 'both',
        'study_design': 'prospective', 'exposure_rate': 0.5, 'controls_per_case': 1.0,
        'keep_latest_cohort': False,
    }
    result.update({k: v for k, v in values.items() if k in result})
    if not isinstance(result['keep_latest_cohort'], bool):
        raise ValueError('keep_latest_cohort must be true or false.')
    choices = {
        'baseline_source': {'seer', 'manual'}, 'effect_measure': {'RR', 'HR', 'OR'},
        'forest_scale': {'both', 'log', 'original'},
        'study_design': {'prospective', 'retrospective'}, 'sex': {'both', 'male', 'female'},
    }
    for field, allowed in choices.items():
        if result[field] not in allowed:
            raise ValueError(f'Invalid {field}.')
    for field, lower, upper in [('age', 30, 90), ('years', 1, 50)]:
        value = _number(result[field])
        if isinstance(result[field], bool) or value is None or not value.is_integer() or not lower <= value <= upper:
            raise ValueError(f'{field} must be an integer from {lower} to {upper}.')
        result[field] = int(value)
    if result['age'] + result['years'] > 90:
        raise ValueError('Starting age plus follow-up must not exceed 90.')
    for field, published_values in [('age', catalog.get('ages', list(range(30, 91, 5)))),
                                    ('years', catalog.get('years', list(range(5, 51, 5))))]:
        if result[field] not in published_values:
            raise ValueError(f'{field} must use an available five-year setting.')
    exposure_rate = _number(result['exposure_rate'])
    if exposure_rate is None or not 0 < exposure_rate < 1:
        raise ValueError('exposure_rate must be between 0 and 1, excluding the endpoints.')
    result['exposure_rate'] = exposure_rate
    controls = _number(result['controls_per_case'])
    if isinstance(result['controls_per_case'], bool) or controls is None or controls <= 0:
        raise ValueError('Controls per case must be a finite number greater than zero.')
    result['controls_per_case'] = controls
    if not isinstance(result['race'], str) or not result['race'].strip():
        raise ValueError('An exact race/ethnicity identifier is required.')
    if result['baseline_source'] == 'manual':
        risk = _number(result['manual_risk_percent'])
        if risk is None or not 0 <= risk < 100:
            raise ValueError('Manual baseline risk must be a percentage from 0 up to, but excluding, 100.')
        result['manual_risk_percent'] = risk
    else:
        result['manual_risk_percent'] = None
    return result


def resolve_baseline(catalog, disease, subcategory, settings):
    baseline = {'available': False, 'p0': None, 'risk_percent': None,
                'source': settings['baseline_source'], 'reason': '',
                'age': settings['age'], 'years': settings['years'],
                'sex': settings['sex'], 'race': settings['race'],
                'identity_approximation': True}
    if settings['baseline_source'] == 'manual':
        percent = settings['manual_risk_percent']
        baseline.update(available=True, p0=percent / 100, risk_percent=percent,
                        source='manual', identity_approximation=False,
                        reason='User-supplied finite-interval baseline risk.')
        return baseline
    requested = subcategory if str(subcategory or '').strip() else disease
    if str(subcategory or '').strip() and catalog.get('repository') == 'metaFemina':
        parents = [row for row in catalog.get('diseases', [])
                   if _key(disease) in {_key(row.get('id')), _key(row.get('label')),
                                      *(_key(alias) for alias in row.get('aliases', []))}]
        parent_key = _key(parents[0].get('id')) if len(parents) == 1 else ''
        prefix = next((prefix for names, prefix in [
            (('breast',), 'breast'), (('uterine', 'uterus'), 'uterus'),
            (('ovarian', 'ovary'), 'ovary')]
            if parent_key.startswith(names)), None)
        if prefix is None:
            baseline['reason'] = 'No unique parent disease mapping is available for this subtype.'
            return baseline
        subtype = str(subcategory).strip()
        explicit_prefix = next((p for p in ('breast', 'uterus', 'ovary') if subtype.startswith(p + '_')), None)
        if explicit_prefix and explicit_prefix != prefix:
            baseline['reason'] = 'The subtype belongs to a different parent cancer; no risk was substituted.'
            return baseline
        requested = subtype if explicit_prefix else prefix + '_' + subtype
    candidates = [row for row in catalog.get('diseases', [])
                  if _key(requested) in {_key(row.get('id')), _key(row.get('label')),
                                         *(_key(alias) for alias in row.get('aliases', []))}]
    if len(candidates) != 1:
        baseline['reason'] = 'No unique exact disease/subtype mapping is available; parent cancer risk was not substituted.'
        return baseline
    mapping = candidates[0]
    baseline['disease'] = mapping['id']
    baseline['disease_label'] = mapping.get('label')
    records = [row for row in catalog.get('records', [])
               if row['disease'] == mapping['id'] and row['sex'] == settings['sex']
               and row['race'] == settings['race'] and row['age'] == settings['age']
               and row['years'] == settings['years']]
    if len(records) != 1:
        baseline['reason'] = mapping.get('unsupported_reason') or 'No directly published SEER value matches this exact disease, age, follow-up, sex and race/ethnicity.'
        return baseline
    record = records[0]
    percent = _number(record['risk_percent'])
    if percent is None or not 0 <= percent < 100:
        baseline['reason'] = 'The matching source value is unavailable for finite-risk conversion.'
        return baseline
    baseline.update(available=True, p0=percent / 100, risk_percent=percent,
                    source=record.get('source'), source_key=record.get('source_key'),
                    source_details=catalog.get('source'), identity_approximation=False,
                    reason='Exact directly published SEER finite-interval risk.')
    return baseline


def ratio_to_rr(value, effect_type, p0):
    ratio = _number(value)
    if ratio is None or ratio <= 0:
        raise ValueError('Effect estimates must be finite positive ratios.')
    kind = str(effect_type or '').strip().upper()
    aliases = {'ODDS RATIO': 'OR', 'HAZARD RATIO': 'HR', 'RISK RATIO': 'RR',
               'RELATIVE RISK': 'RR', 'INCIDENCE RATE RATIO': 'IRR'}
    kind = aliases.get(kind, kind)
    if kind not in {'RR', 'IRR', 'OR', 'HR', 'RRR'}:
        raise ValueError('Only compatible ratio measures can be converted.')
    if p0 is None or p0 == 0 or kind in {'RR', 'RRR'}:
        return ratio
    if kind == 'OR':
        return ratio / (1 - p0 + p0 * ratio)
    return -math.expm1(ratio * math.log1p(-p0)) / p0


def rr_to_display(value, target, p0):
    rr = _number(value)
    if rr is None or rr <= 0:
        return None
    if target == 'RR' or p0 is None or p0 == 0:
        return rr
    if rr * p0 >= 1:
        return None
    if target == 'OR':
        return rr * (1 - p0) / (1 - rr * p0)
    return math.log1p(-rr * p0) / math.log1p(-p0)


def sample_size_for_or(odds_ratio, exposure_rate=0.5, controls_per_case=1.0):
    """Unmatched case-control planning from OR and exposure prevalence.

    Allocation r is controls/case. The null variance uses the allocation-weighted
    pooled exposure probability; the alternative uses separate probabilities.
    No disease incidence is an input to this calculation.
    """
    answer = {'available': False, 'required_per_group': None, 'total': None,
              'required_cases': None, 'required_controls': None,
              'design': 'retrospective', 'measure': 'OR', 'exposure_rate': exposure_rate,
              'alpha': 0.05, 'power': 0.8, 'allocation_ratio': controls_per_case,
              'controls_per_case': controls_per_case, 'odds_ratio': _number(odds_ratio),
              'proportions_label': 'Exposure probability in controls and cases', 'reason': ''}
    odds, q0, ratio = _number(odds_ratio), _number(exposure_rate), _number(controls_per_case)
    if odds is None or odds <= 0 or q0 is None or not 0 < q0 < 1 or ratio is None or ratio <= 0:
        answer['reason'] = 'A positive OR, control exposure percentage between 0 and 100, and positive case-control ratio are required.'
        return answer
    q1 = 1 / (1 + ((1 - q0) / q0) / odds)
    answer.update(p0_for_power=q0, p1_for_power=q1)
    difference = abs(q1 - q0)
    if difference < 1e-12:
        answer['reason'] = 'No finite sample size exists for OR = 1.'
        return answer
    pooled = (q1 + ratio * q0) / (1 + ratio)
    za, zb = NormalDist().inv_cdf(0.975), NormalDist().inv_cdf(0.8)
    try:
        required = (za * math.sqrt((1 + 1 / ratio) * pooled * (1 - pooled))
                    + zb * math.sqrt(q1 * (1 - q1) + q0 * (1 - q0) / ratio)) ** 2 / difference ** 2
    except (OverflowError, ZeroDivisionError, ValueError):
        answer['reason'] = 'The requested allocation does not give a finite sample size.'
        return answer
    if not math.isfinite(required) or not math.isfinite(required * ratio):
        answer['reason'] = 'The requested allocation does not give a finite sample size.'
        return answer
    cases = math.ceil(required)
    controls = math.ceil(cases * ratio)
    answer.update(available=True, required_cases=cases, required_controls=controls,
                  required_per_group=cases if ratio == 1 else None, total=cases + controls,
                  reason='Unmatched case-control normal approximation; two-sided alpha 0.05 and power 80%.')
    return answer


def sample_size_for_rr(rr, p0, design='prospective', exposure_rate=0.5, controls_per_case=1.0):
    if design == 'retrospective':
        answer = sample_size_for_or(rr_to_display(rr, 'OR', p0), exposure_rate, controls_per_case)
        answer['conversion_baseline_risk'] = p0
        answer['identity_approximation'] = p0 is None
        return answer
    answer = {'available': False, 'required_per_group': None, 'total': None,
              'design': design, 'measure': 'RR', 'exposure_rate': exposure_rate,
              'alpha': 0.05, 'power': 0.8, 'allocation_ratio': 1,
              'baseline_risk': p0, 'reason': ''}
    rr = _number(rr)
    if p0 is None or not 0 < p0 < 1 or rr is None or rr <= 0 or rr * p0 >= 1:
        answer['reason'] = 'An available baseline risk and a feasible nonzero risk contrast are required.'
        return answer
    first, second = p0, p0 * rr
    answer['proportions_label'] = 'Outcome probability in unexposed and exposed groups'
    answer.update(p0_for_power=first, p1_for_power=second)
    difference = abs(second - first)
    if difference < 1e-12:
        answer['reason'] = 'No finite sample size exists for a zero effect contrast.'
        return answer
    average = (first + second) / 2
    za, zb = NormalDist().inv_cdf(0.975), NormalDist().inv_cdf(0.8)
    n = math.ceil((za * math.sqrt(2 * average * (1 - average))
                   + zb * math.sqrt(first * (1 - first) + second * (1 - second))) ** 2
                  / difference ** 2)
    answer.update(available=True, required_per_group=n, total=2 * n,
                  reason='Equal-group, two-sided normal approximation; alpha 0.05 and power 80%.')
    return answer


def _study_conversion(study, p0, target, normalizer=None, converter=None):
    result = copy.deepcopy(study)
    kind = study.get('Effect Type') or study.get('Original Effect Type')
    if normalizer is not None:
        kind = normalizer(kind) or kind
    values = []
    for field, fallback in [('Effect Size', 'Original Effect Size'), ('Lower CI', 'Original Lower CI'), ('Upper CI', 'Original Upper CI')]:
        value = study.get(field)
        if _number(value) is None:
            value = study.get(fallback)
        try:
            values.append(converter(value, kind) if converter is not None else ratio_to_rr(value, kind, p0))
        except ValueError:
            values.append(None)
    result.update({'Canonical RR': values[0], 'Canonical Lower CI': values[1], 'Canonical Upper CI': values[2],
                   'Displayed Effect Type': target, 'Displayed Effect Size': rr_to_display(values[0], target, p0),
                   'Displayed Lower CI': rr_to_display(values[1], target, p0),
                   'Displayed Upper CI': rr_to_display(values[2], target, p0),
                   'Conversion Baseline Risk': p0,
                   'Conversion Baseline Risk Percent': None if p0 is None else p0 * 100,
                   'Conversion Identity Approximation': p0 is None})
    return project_cases(result, p0)


def _study_label(study, compact=False):
    """Readable bibliographic identity, independent of saved effect/stratum keys."""
    original = str(study.get('Study') or '').strip()
    author = study.get('Authors') or study.get('Author') or ''
    if isinstance(author, (list, tuple)):
        author = author[0] if author else ''
    author = str(author).split(',')[0].strip()
    if not author:
        author = re.split(r'\bet\s+al\.?|\(|\[|—', original, maxsplit=1, flags=re.I)[0].strip()
    # PubMed author strings use surname followed by initials. Preserve compound
    # surnames, apostrophes and hyphens while removing only trailing initials.
    surname = re.sub(r'(?:\s+[A-Z](?:\.?[A-Z])*\.?)+$', '', author).strip() or author
    year_match = re.search(r'\b(?:18|19|20)\d{2}\b', str(study.get('Year') or original))
    year = year_match.group(0) if year_match else ''
    raw_pmid = study.get('PMID')
    pmid = str(raw_pmid).strip() if raw_pmid is not None else ''
    if re.fullmatch(r'\d+\.0', pmid):
        pmid = pmid[:-2]
    if pmid.casefold() in {'', 'nan', 'none', 'na', 'n/a', '-'}:
        match = re.search(r'PMID\s*:?\s*(\d+)', original, re.I)
        pmid = match.group(1) if match else ''
    if compact:
        label = surname or original or 'Study'
        if year:
            label += f' ({year})'
        if pmid:
            label += f' [PMID: {pmid}]'
        return label
    label = original or (author + (f' ({year})' if year else '')) or 'Study'
    if year and year not in label:
        label += f' ({year})'
    if pmid and not re.search(r'\b' + re.escape(pmid) + r'\b', label):
        label += f' [PMID: {pmid}]'
    return label


def _distinct_study_labels(studies, compact=False):
    labels = [_study_label(study, compact) for study in studies]
    counts, seen = {}, {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    distinct = []
    for label in labels:
        seen[label] = seen.get(label, 0) + 1
        distinct.append(f'{label} [estimate {seen[label]}]' if counts[label] > 1 else label)
    return distinct


def normalize_leave_one_out_rows(rows):
    """Expose citation-only labels while retaining the saved source identity."""
    normalized = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = copy.deepcopy(row)
        raw = str(item.get('omitted') or '').strip()
        if raw:
            item['source_omitted'] = raw
            item['omitted'] = _study_label({'Study': raw}, compact=True)
        normalized.append(item)
    return normalized


def forest_annotation_rows(studies, canonical, context):
    """Exact numerical annotations, independently of the logarithmic plot axis."""
    target = context['settings']['effect_measure']
    selected = []
    for study in studies:
        if study.get('Settings Pooling Selected') is False:
            continue
        values = [study.get(key) for key in ('Displayed Effect Size', 'Displayed Lower CI', 'Displayed Upper CI')]
        if all(v is not None and v > 0 for v in values) and values[1] <= values[0] <= values[2]:
            selected.append((study, values))
    # Forest labels are deliberately citation-only. Multiple estimates from the
    # same publication may share a label; provenance remains in source_study.
    labels = [_study_label(study, compact=True) for study, _ in selected]
    rows = [(label, str(study.get('Study') or label), *values)
            for label, (study, values) in zip(labels, selected)]
    pooled = [rr_to_display(canonical.get(key), target, context['baseline']['p0'])
              for key in ('pooled_es', 'ci_low', 'ci_upp')]
    if all(v is not None and v > 0 for v in pooled):
        rows.append(('Pooled result', 'Pooled result', *pooled))
    return [{'study': label, 'source_study': original, 'estimate': point, 'ci_low': low, 'ci_upp': high,
             'log_estimate': math.log(point), 'log_ci_low': math.log(low), 'log_ci_upp': math.log(high)}
            for label, original, point, low, high in rows]


def _forest_plots(studies, canonical, context):
    """One logarithmic forest, with compact four-significant-digit CI columns."""
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter
    import textwrap
    target, choice = context['settings']['effect_measure'], context['settings']['forest_scale']
    rows = forest_annotation_rows(studies, canonical, context)
    if not rows:
        return {}
    label_scales = ['original', 'log'] if choice == 'both' else [choice]
    # Keep numerical columns unchanged; transfer 1.7 units from Study to the
    # graphical axis, making its 3.4-unit width 1.5 times wider.
    figure = Figure(figsize=(13.5 if choice == 'both' else 11.7, max(3.4, 2 + 0.36 * len(rows))), layout='constrained')
    grid = figure.add_gridspec(1, 2 + len(label_scales), width_ratios=[4.2, 5.1] + [1.8] * len(label_scales))
    names = figure.add_subplot(grid[0, 0])
    axis = figure.add_subplot(grid[0, 1], sharey=names)
    columns = [figure.add_subplot(grid[0, 2 + i], sharey=names) for i in range(len(label_scales))]
    names.set_ylim(len(rows) - 0.4, -1.1)
    names.set_xlim(0, 1)
    names.axis('off')
    names.text(0, -0.8, 'Study', weight='bold', fontsize=9)
    for y, row in enumerate(rows):
        pooled = row['study'] == 'Pooled result'
        weight = 'bold' if pooled else 'normal'
        names.text(0, y, textwrap.fill(row['study'], 54, break_long_words=False), va='center', fontsize=8, weight=weight)
        point, low, high = row['estimate'], row['ci_low'], row['ci_upp']
        axis.errorbar(point, y, xerr=[[point - low], [high - point]], fmt='D' if pooled else 'o',
                      color='#1e3a5f' if pooled else '#377d97', capsize=3)
    axis.tick_params(axis='y', left=False, labelleft=False)
    axis.set_xscale('log')
    lower, upper = axis.get_xlim()
    close_range = math.log10(upper / lower) <= 2
    axis.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5) if close_range else (1,), numticks=7))
    axis.xaxis.set_major_formatter(FuncFormatter(lambda value, position: f'{value:.3g}'))
    axis.xaxis.set_minor_formatter(NullFormatter())
    axis.tick_params(axis='x', labelsize=8)
    axis.axvline(1, color='#777777', linestyle='--', linewidth=1)
    axis.set_xlabel(f'{target} (logarithmic axis)')
    for column, scale in zip(columns, label_scales):
        column.axis('off')
        column.set_xlim(0, 1)
        column.text(0.03, -0.8, f'{"ln(" + target + ")" if scale == "log" else target} (95% CI)', weight='bold', fontsize=9)
        for y, row in enumerate(rows):
            prefix = 'log_' if scale == 'log' else ''
            values = [row[prefix + key] for key in ('estimate', 'ci_low', 'ci_upp')]
            formatted = [(f'{v:.3e}'.replace('e-0', 'e-').replace('e+0', 'e+')
                          if v and (abs(v) < 0.01 or abs(v) >= 10000) else f'{v:.4g}')
                         for v in values]
            column.text(0.03, y, f'{formatted[0]} ({formatted[1]}, {formatted[2]})', va='center',
                        fontsize=8, weight='bold' if row['study'] == 'Pooled result' else 'normal')
    figure.suptitle(f'{context["disease"]} — {target}', fontsize=12)
    buffer = io.BytesIO()
    figure.savefig(buffer, format='png', dpi=140)
    value = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    figure.clear()
    return {'log': value}


def _diagnostic_plots(context, canonical):
    """Render fresh diagnostics on the same canonical log-RR observations."""
    from matplotlib.figure import Figure
    rows = []
    for row in context.get('pooled_plot_rows', []):
        effect, error = _number(row.get('log_ES')), _number(row.get('log_SE'))
        if effect is not None and error is not None and error > 0:
            rows.append((effect, error, row))
    if len(rows) < 2:
        return {}
    labels = _distinct_study_labels([row[2] for row in rows], compact=True)
    def encode(figure):
        buffer = io.BytesIO()
        figure.savefig(buffer, format='png', dpi=140)
        figure.clear()
        return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    figure = Figure(figsize=(8, 5.5), layout='constrained')
    axis = figure.subplots()
    axis.scatter([r[0] for r in rows], [r[1] for r in rows], color='#377d97')
    pooled = _number(canonical.get('pooled_es'))
    if pooled is not None and pooled > 0:
        center = math.log(pooled)
        maximum = max(r[1] for r in rows) * 1.1
        axis.plot([center - 1.96 * maximum, center, center + 1.96 * maximum],
                  [maximum, 0, maximum], linestyle='--', color='#777777')
        axis.axvline(center, color='#dc2626', linestyle='--', linewidth=1.5,
                     label=f'Pooled RR = {pooled:.4g}')
        axis.legend(loc='best', fontsize=8, frameon=False)
    axis.invert_yaxis()
    axis.set(xlabel='Canonical log(RR)', ylabel='Standard error of log(RR)',
             title=f'Funnel Plot: {context.get("disease", "")} vs {context.get("exposure", "")}')
    plots = {'funnel': encode(figure)}
    if len(rows) >= 3:
        weights = [1 / r[1] ** 2 for r in rows]
        total = sum(weights)
        mean = sum(w * r[0] for w, r in zip(weights, rows)) / total
        contributions = [w * (r[0] - mean) ** 2 for w, r in zip(weights, rows)]
        influences = []
        for weight, row in zip(weights, rows):
            omitted_mean = (total * mean - weight * row[0]) / (total - weight)
            influences.append(abs(omitted_mean - mean) * math.sqrt(total))
        # Keep each author/year/PMID directly beside its dot. Overlap is
        # intentional; no leader lines or detached label columns are used.
        figure = Figure(figsize=(8, 5.5), layout='constrained')
        axis = figure.subplots()
        axis.scatter(contributions, influences, color='#377d97', zorder=3)
        axis.margins(x=0.15, y=0.15)
        for label, contribution, influence in zip(labels, contributions, influences):
            axis.annotate(label, (contribution, influence), xytext=(4, 4),
                          textcoords='offset points', fontsize=7.5, va='bottom',
                          ha='left', annotation_clip=False)
        axis.set(xlabel='Contribution to Cochran Q', ylabel='Standardized change in fixed-effect log(RR)')
        figure.suptitle('Baujat diagnostic on canonical RR', fontsize=12)
        plots['baujat'] = encode(figure)
    return plots


def _project(result, context):
    if not isinstance(result, dict):
        return result
    result = copy.deepcopy(result)
    settings, baseline = context['settings'], context['baseline']
    result['analysis_settings'] = settings
    result['cohort_selection'] = context.get('cohort_selection_audit', {'enabled': False})
    result['baseline_risk'] = baseline
    result['min_cases'] = context.get('min_cases', 0)
    result['_analysis_settings_applied'] = True
    headline = result.get('headline') or {}
    canonical = headline.get('canonical_rr') or {key: headline.get(key) for key in ('pooled_es', 'ci_low', 'ci_upp', 'pi_low', 'pi_upp')}
    canonical = {key: _number(canonical.get(key)) for key in ('pooled_es', 'ci_low', 'ci_upp', 'pi_low', 'pi_upp')}
    result['canonical_rr'] = canonical
    headline['canonical_rr'] = canonical
    headline['effect_measure'] = settings['effect_measure']
    headline['baseline_risk'] = baseline['p0']
    headline['baseline_risk_percent'] = baseline['risk_percent']
    headline['loo_results'] = normalize_leave_one_out_rows(headline.get('loo_results'))
    for key, value in canonical.items():
        headline[key] = rr_to_display(value, settings['effect_measure'], baseline['p0'])
    result['headline'] = headline if canonical['pooled_es'] is not None else None
    result['effect_size_conversion'] = {
        'target_measure': settings['effect_measure'], 'pooling_measure': 'RR',
        'baseline_risk': baseline['p0'], 'baseline_risk_percent': baseline['risk_percent'],
        'baseline_source': baseline, 'identity_approximation': baseline['identity_approximation'],
    }
    result['studies'] = [_study_conversion(row, baseline['p0'], settings['effect_measure'],
                        context.get('normalizer'), context.get('converter')) for row in result.get('studies', [])]
    result['studies'] = [project_cases(row, {**baseline, 'p0': row['Conversion Baseline Risk']}) for row in result['studies']]
    from cohort_identity import enrich_cohort_records
    result['studies'] = enrich_cohort_records(result['studies'], repo_root=Path(__file__).resolve().parents[2])
    if 'pooled_study_keys' in context:
        for row in result['studies']:
            evaluated = _cohort_record_key(row) in context.get('cohort_evaluated_record_keys', set())
            excluded = _cohort_record_key(row) in context.get('cohort_excluded_record_keys', set())
            row['Settings Pooling Selected'] = _study_key(row) in context['pooled_study_keys'] and not excluded
            row['cohort_selection_evaluated'] = evaluated
            row['cohort_latest_excluded'] = excluded if evaluated else None
            if row['cohort_latest_excluded']:
                row['cohort_selection_reason'] = 'A later publication from the same cohort was retained.'
    result['all_studies'] = copy.deepcopy(result['studies'])
    result['planning_or'] = {key: rr_to_display(value, 'OR', baseline['p0']) for key, value in canonical.items()}
    result['sample_size'] = sample_size_for_rr(canonical['pooled_es'], baseline['p0'], settings['study_design'], settings['exposure_rate'], settings.get('controls_per_case', 1.0))
    result['sample_size']['effect_estimate'] = result['planning_or'] if settings['study_design'] == 'retrospective' else canonical
    result['forest_annotations'] = forest_annotation_rows(result['studies'], canonical, context)
    result['forest_axis_scale'] = 'log'
    result['forest_label_scale'] = settings['forest_scale']
    plots = _forest_plots(result['studies'], canonical, context)
    result['forest_plots'] = plots
    result['plot_url'] = plots.get('log') or plots.get('original')
    diagnostics = _diagnostic_plots(context, canonical)
    result['funnel_plot_url'] = diagnostics.get('funnel')
    result['baujat_plot_url'] = diagnostics.get('baujat')
    return result


def configure_meta_analysis(namespace):
    """Bind request-aware helpers while retaining the existing pooling policies."""
    old_baseline = namespace.get('baseline_risk_for_disease')
    if old_baseline:
        @wraps(old_baseline)
        def baseline(*args, **kwargs):
            active = _ACTIVE.get()
            return active['baseline']['p0'] if active is not None else old_baseline(*args, **kwargs)
        namespace['baseline_risk_for_disease'] = baseline
    old_conversion = namespace.get('convert_ratio_to_rr')
    if old_conversion:
        @wraps(old_conversion)
        def conversion(value, kind, risk, **kwargs):
            active = _ACTIVE.get()
            if active is None:
                return old_conversion(value, kind, risk, **kwargs)
            normalized = namespace.get('normalize_effect_type', lambda v: v)(kind)
            current_risk = active['baseline']['p0']
            if current_risk is None or current_risk == 0:
                return ratio_to_rr(value, normalized or kind, current_risk)
            return old_conversion(value, kind, current_risk, **kwargs)
        namespace['convert_ratio_to_rr'] = conversion
    old_gut_conversion = namespace.get('convert_effect_to_rr')
    if old_gut_conversion:
        @wraps(old_gut_conversion)
        def gut_conversion(value, kind, disease):
            active = _ACTIVE.get()
            if active is None or active['baseline']['p0'] not in (None, 0):
                return old_gut_conversion(value, kind, disease)
            normalized = namespace.get('normalize_effect_type', lambda v: v)(kind)
            return ratio_to_rr(value, normalized or kind, active['baseline']['p0'])
        namespace['convert_effect_to_rr'] = gut_conversion
    original = namespace['perform_meta_analysis']
    @wraps(original)
    def perform(frame, disease, exposure, *args, **kwargs):
        active = _ACTIVE.get()
        if active is None:
            return original(frame, disease, exposure, *args, **kwargs)
        frame = frame.copy(deep=True)
        active['exposure'] = exposure
        active['pooled_study_keys'] = set()
        active['pooled_plot_rows'] = []
        active['normalizer'] = namespace.get('normalize_effect_type')
        if 'convert_ratio_to_rr' in namespace:
            active['converter'] = lambda value, kind: namespace['convert_ratio_to_rr'](value, kind, active['baseline']['p0'])
        elif 'convert_effect_to_rr' in namespace:
            active['converter'] = lambda value, kind: namespace['convert_effect_to_rr'](value, kind, disease)
        all_frame = kwargs.get('df_all')
        all_frame = frame.copy(deep=True) if all_frame is None else all_frame.copy(deep=True)
        # Refresh request-local estimates before the user's optional cutoff.
        case_baseline = active['baseline']
        frame = project_frame(frame, case_baseline)
        all_frame = project_frame(all_frame, case_baseline)
        for current in (frame, all_frame):
            if 'Settings Pooling Selected' not in current.columns:
                current['Settings Pooling Selected'] = True
        frame = filter_frame(frame, active.get('min_cases', 0))
        if active['quality_filter'] == 'Moderate+':
            quality = frame.get('Quality Score')
            mask = quality.astype(str).str.strip().str.casefold().isin({'moderate', 'good'}) if quality is not None else frame.index.to_series().map(lambda _: False)
            frame = frame.loc[mask].copy()
            if 'Quality Score' in all_frame.columns:
                all_frame['Settings Pooling Selected'] = all_frame['Quality Score'].astype(str).str.strip().str.casefold().isin({'moderate', 'good'})
        kwargs['df_all'] = all_frame
        kwargs['generate_plots'] = False
        result = original(frame, disease, exposure, *args, **kwargs)
        if isinstance(result, dict) and str(result.get('error', '')).startswith(('No studies', 'No conventional', 'No eligible')):
            result = {'success': True, 'studies': all_frame.to_dict(orient='records'), 'headline': None,
                      'analysis_error': result['error'], 'summary_html': '<p>No studies meet the selected pooling criteria.</p>'}
        # Preserve source/audit columns omitted by legacy response projections.
        originals = {}
        for row in all_frame.to_dict(orient='records'):
            key = _study_key(row)
            originals.setdefault(key, []).append(row)
        if isinstance(result, dict):
            for row in result.get('studies', []):
                key = _study_key(row)
                matches = originals.get(key, [])
                if matches:
                    source = matches.pop(0)
                    for field, value in source.items():
                        row.setdefault(field, value)
        return _project(result, active)
    namespace['perform_meta_analysis'] = perform


def install_app(app, meta_analysis, app_file):
    """Recompute cached and interactive responses under validated request settings."""
    from flask import g, jsonify, request
    repo = Path(app_file).resolve().parent

    @app.before_request
    def baseline_settings_before_request():
        if request.path not in {'/analyze', '/reanalyze', '/chatgpt/analyze', '/chatgpt/reanalyze'} or request.method != 'POST':
            return None
        data = request.get_json(silent=True) or {}
        catalog_path = repo / 'static' / 'seer-baseline.json'
        try:
            catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            catalog = {'defaults': {'sex': 'male' if repo.name == 'MetaVir' else 'female' if repo.name == 'metaFemina' else 'both'}}
        try:
            settings = normalize_settings(data.get('analysis_settings'), catalog)
            min_cases = minimum_cases(data.get('min_cases', 0))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        disease = data.get('disease', '')
        context = {'settings': settings, 'baseline': resolve_baseline(catalog, disease, data.get('subcategory'), settings),
                   'disease': disease, 'exposure': data.get('exposure', ''), 'repository': repo.name,
                   'quality_filter': data.get('quality_filter', 'Moderate+'), 'min_cases': min_cases}
        g.seer_settings_token = _ACTIVE.set(context)
        g.seer_settings_context = context
        return None

    @app.after_request
    def baseline_settings_after_request(response):
        context = getattr(g, 'seer_settings_context', None)
        if context is None or response.status_code >= 400 or not response.is_json:
            return response
        payload = response.get_json()
        if not isinstance(payload, dict) or payload.get('error'):
            return response
        if not payload.get('_analysis_settings_applied'):
            data = request.get_json(silent=True) or {}
            rows = payload.get('studies') or []
            if rows:
                import pandas as pd
                frame = pd.DataFrame(copy.deepcopy(rows))
                for column in ('Effect Size', 'Lower CI', 'Upper CI', 'SE'):
                    if column in frame:
                        frame[column] = pd.to_numeric(frame[column], errors='coerce')
                if 'Study' not in frame:
                    frame['Study'] = frame.get('PMID', 'Unnamed study')
                if 'Cases' not in frame:
                    frame['Cases'] = None
                canonical_disease = payload.get('disease') or data.get('disease', '')
                selected_endpoint = payload.get('endpoint') or data.get('endpoint') or data.get('outcome') or 'Incidence'
                context['disease'] = canonical_disease
                result = meta_analysis.perform_meta_analysis(frame, canonical_disease, data.get('exposure', ''),
                    outcome=selected_endpoint, exclude_meta=data.get('exclude_meta', True),
                    df_all=frame, screening_stats=payload.get('screening_stats'), generate_plots=False)
                payload.update(result)
            else:
                payload = _project(payload, context)
        payload.pop('_analysis_settings_applied', None)
        # Pandas and old cache payloads can carry nonfinite values. JSON null is
        # the explicit unavailable representation for this response contract.
        def clean(value):
            if isinstance(value, dict):
                return {str(k): clean(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [clean(v) for v in value]
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if hasattr(value, 'item'):
                return clean(value.item())
            return value
        response.set_data(json.dumps(clean(payload), allow_nan=False, ensure_ascii=False))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.teardown_request
    def baseline_settings_teardown_request(error):
        token = getattr(g, 'seer_settings_token', None)
        if token is not None:
            _ACTIVE.reset(token)
            g.seer_settings_token = None
