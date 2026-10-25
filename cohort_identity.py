"""Conservative, optional cohort selection without altering recorded evidence.

The registry is additive metadata. Unknown identities/dates and overlapping date
intervals are retained. Cohort rounds stay distinct; inseparable multi-cohort estimates remain whole.
Definitely older reports overlapping an accepted newer cohort are excluded. Apply selection only after ordinary eligibility checks.
"""
from __future__ import annotations

import calendar
import hashlib
from decimal import Decimal, InvalidOperation
import json
import math
import re
import unicodedata
from datetime import date
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

_UNKNOWN = {'', 'na', 'n/a', 'none', 'null', 'unknown', 'not reported', 'not specified', 'nr', 'nan'}
_NAME_FIELDS = ('cohort_names', 'Cohort Names', 'cohort_name', 'Cohort Name', 'Cohort name', 'Study Name', 'Study name', 'study_name', 'Cohort', 'cohort')
_DATE_FIELDS = ('Cohort Publication Date', 'publication_date', 'Publication Date', 'Publication date', 'pub_date', 'PubDate', 'Year', 'year', 'Publication Year')
_STRATA = (('Exposure', 'exposure', 'exposure_name'), ('Disease', 'disease', 'disease_name', 'outcome'), ('Endpoint', 'endpoint', 'Outcome Type', 'outcome_type'))


def _text(value):
    if value is None or isinstance(value, float) and math.isnan(value):
        return ''
    return str(value).strip()


def _round_number(value):
    text = re.sub(r'^(?:round|cohort|study)\s+', '', _text(value).lower())
    return {'i': '1', 'ii': '2', 'iii': '3', 'iv': '4'}.get(text, text) if re.fullmatch(r'i{1,3}|iv|[0-9]+', text) else ''


def _has_round_number(name, cohort_round):
    target = _round_number(cohort_round)
    text = re.sub(r'[^a-z0-9]+', ' ', _text(name).lower())
    text = re.sub(r'(?<=[1-4])(?=i{1,3}\b|iv\b)', ' ', text)
    text = re.sub(r'(?<=[a-z])(?=[1-4]\b)', ' ', text)
    tokens = text.split()
    if target:
        return any(_round_number(token) == target for token in tokens)
    # Preserve explicitly recorded nonnumeric rounds/cycles such as KORA F4,
    # NHANES V, version 2.0, and 2013-2015 when the label already contains them.
    suffix = re.sub(r'^(?:round|cohort|study)\s+', '', _text(cohort_round).lower())
    suffix = re.sub(r'[^a-z0-9]+', ' ', suffix).strip()
    suffix = re.sub(r'(?<=[a-z])(?=[1-4]\b)', ' ', suffix)
    return bool(suffix and re.search(r'(?<!\w)' + re.escape(suffix) + r'(?!\w)', ' '.join(tokens)))


def normalize_cohort_name(value):
    """Canonical identity, explicitly preserving numbered cohort generations."""
    cohort_round = None
    if isinstance(value, dict):
        cohort_round = value.get('round')
        value = value.get('cohort_id') or value.get('canonical_name') or value.get('name') or ''
    name = unicodedata.normalize('NFKD', _text(value)).encode('ascii', 'ignore').decode().lower()
    name = re.sub(r"[’']", '', name)
    name = re.sub(r'[^a-z0-9]+', ' ', name).strip()
    name = re.sub(r'\b(women|men|nurse|nurses|physician|physicians) s\b', lambda m: m.group(1) + 's', name)
    # Separate redundant Arabic/Roman round annotations without changing years.
    name = re.sub(r'(?<=[1-4])(?=i{1,3}\b|iv\b)', ' ', name)
    name = re.sub(r'(?<=[a-z])(?=[1-4]\b)', ' ', name)
    if name in _UNKNOWN:
        return ''
    # Parenthesized acronyms and written names must map to the same identity.
    aliases = (
        (r'\bnurses? health stud(?:y|ies)\b', 'nhs'),
        (r'\bhealth professionals follow up study\b', 'hpfs'),
        (r'\bwomens health initiative\b', 'whi'),
        (r'\bwomens health study\b', 'whs'),
        (r'\beuropean prospective investigation into cancer and nutrition\b', 'epic'),
        (r'\buk biobank\b', 'ukb'),
        (r'\bnational institutes of health(?: nih)?\b', 'nih'),
        (r'\bamerican association of retired persons(?: aarp)?\b', 'aarp'),
        (r'\bnih aarp(?: diet and health)?(?: study|cohort)?\b', 'nih aarp'),
        (r'\bmulti ?ethnic study of atherosclerosis\b', 'mesa'),
        (r'\bmulti ?ethnic study mesa\b', 'mesa'),
        (r'\bmulti ?ethnic cohort(?: study)?\b', 'mec'),
        (r'\bmulti ?ethnic study mec\b', 'mec'),
    )
    for pattern, short in aliases:
        name = re.sub(pattern, short, name)
        name = re.sub(r'\b(' + short + r')\s+\1\b', r'\1', name)
    round_text = re.sub(r'^(?:round|cohort|study)\s+', '', _text(cohort_round).lower())
    if _round_number(round_text) and not _has_round_number(name, round_text):
        name += ' ' + round_text
    # NHS II and NHS 2 are aliases; unsuffixed NHS remains the first cohort.
    match = re.fullmatch(r'nhs(?: (?:cohort|study))?(?: (i{1,3}|iv|[1-4]))?(?: nhs(?: (i{1,3}|iv|[1-4]))?)?', name)
    if match:
        suffix = match.group(1) or match.group(2) or '1'
        suffix = {'i': '1', 'ii': '2', 'iii': '3', 'iv': '4'}.get(suffix, suffix)
        return 'nhs ' + suffix
    name = re.sub(r'\b(i|ii|iii|iv)\b', lambda m: {'i':'1','ii':'2','iii':'3','iv':'4'}[m.group()], name)
    name = re.sub(r'\b([1-4]) \1\b', r'\1', name)
    return name


def _pmid(row):
    value = next((_text(row.get(k)) for k in ('PMID', 'pmid', 'PubMed ID', 'pubmed_id') if _text(row.get(k))), '')
    return re.sub(r'\.0$', '', value)


def load_registry(repo_root=None):
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parent
    path = root if root.name.endswith('.json') else root / 'data' / 'cohort_registry.json'
    if not path.exists():
        return {}
    stat = path.stat()
    return _cached_registry(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=24)
def _cached_registry(path, modified_ns, size):
    # Metadata is immutable to callers. A new file stamp invalidates the cache.
    del modified_ns, size
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    return payload.get('records', {})


def _cohorts(value):
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, str):
        # Only split explicit list delimiters, never a hyphen within a name.
        value = re.split(r';|\|', value)
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(sorted({name for item in value if (name := normalize_cohort_name(item))}))


def _date_interval(value):
    if isinstance(value, dict):
        value = value.get('date') or value.get('value') or value.get('year')
    text = _text(value)
    text = re.sub(r'\.0$', '', text)
    m = re.match(r'^(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2}))?)?(?:\b|T)', text)
    if not m:
        return None
    try:
        year, month, day = [int(x) if x else None for x in m.groups()]
        if year < 1800 or year > 2200:
            return None
        if month is None:
            return date(year, 1, 1), date(year, 12, 31)
        if day is None:
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        stamp = date(year, month, day)
        return stamp, stamp
    except (ValueError, TypeError):
        return None


def _cohort_values(row, record):
    by_record = record.get('row_cohort_names', {})
    if isinstance(by_record, dict):
        for field in ('Effect ID', 'effect_record_id', 'Screening Record ID', 'record_id', 'context_id'):
            record_id = _text(row.get(field))
            specific = by_record.get(record_id) if record_id else None
            if isinstance(specific, dict) and 'cohort_names' in specific:
                specific = specific['cohort_names']
            if _cohorts(specific):
                return specific, 'registry_exact_record'
            if record_id and record_id in by_record:
                return [], 'registry_exact_record_unresolved'
    prior_source = (row.get('cohort_provenance') or {}).get('identity_source') if isinstance(row.get('cohort_provenance'), dict) else None
    derived = bool(prior_source and (prior_source.startswith('registry_') or prior_source == 'unresolved'))
    if not derived:
        for field in _NAME_FIELDS:
            if _cohorts(row.get(field)):
                return row[field], 'saved_row'
    if record.get('status') not in ('ambiguous', 'not_reported') and _cohorts(record.get('cohort_names')):
        return record.get('cohort_names'), 'registry_publication'
    return None, 'unresolved'


def _registry_publication_date(record):
    value = record.get('publication_date')
    if not _date_interval(value):
        return None
    text = _text(value)
    precision = _text(record.get('publication_date_precision')).casefold()
    if precision in {'year', 'yyyy'}:
        text = text[:4]
    elif precision in {'month', 'yyyy-mm'}:
        text = text[:7]
    return text if _date_interval(text) else None


def _identity(row, registry):
    record = registry.get(_pmid(row), {})
    values, _ = _cohort_values(row, record)
    names = _cohorts(values)
    # Verified registry precision outranks raw fields, which may contain padded dates.
    verified = _registry_publication_date(record)
    if verified is not None:
        interval = _date_interval(verified)
    else:
        intervals = [stamp for field in _DATE_FIELDS if (stamp := _date_interval(row.get(field)))]
        interval = min(intervals, key=lambda stamp: (stamp[1] - stamp[0]).days) if intervals else None
    return names, interval


def _stratum(row, group_fields):
    groups = [(key,) for key in group_fields] if group_fields is not None else _STRATA
    return tuple(next((_text(row.get(k)).casefold() for k in keys if _text(row.get(k))), '') for keys in groups)


def select_latest_cohort_records(rows, enabled=True, repo_root=None, group_fields=None):
    """Return retained rows and a complete selection audit, never mutate inputs.

    Rows of one paper at the newest date are kept together. Tied/overlapping
    dates of different papers and unknown identities or dates remain included.
    ``group_fields=[]`` is appropriate when the caller already selected a
    single poolable stratum. The default protects exposure/outcome strata; pass explicit sex fields only when intentionally separating cohorts by sex.
    """
    rows = list(rows)
    audit = {'enabled': bool(enabled), 'input_rows': len(rows), 'retained_rows': len(rows), 'excluded_rows': 0,
             'unknown_cohort_rows': 0, 'unknown_date_rows': 0, 'multi_cohort_rows': 0, 'excluded': [],
             'policy': 'newest_first_publication_per_overlapping_identified_cohort_and_poolable_stratum; combined_estimates_whole; unknowns_and_ties_retained'}
    if not enabled:
        return rows, audit
    registry = load_registry(repo_root)
    identities = [_identity(row, registry) for row in rows]
    groups = {}
    for i, (names, interval) in enumerate(identities):
        if not names:
            audit['unknown_cohort_rows'] += 1
        elif interval is None:
            audit['unknown_date_rows'] += 1
        else:
            groups.setdefault(_stratum(rows[i], group_fields), []).append(i)
        if len(names) > 1:
            audit['multi_cohort_rows'] += 1
    excluded = set()
    for indices in groups.values():
        # Prefer the newest usable publication before considering older reports.
        # Only accepted newer reports can suppress a row: if a combined NHS/HPFS
        # estimate was itself superseded by a newer NHS estimate, an older
        # independent HPFS report may still contribute. Never split a coefficient.
        accepted = []
        for i in sorted(indices, key=lambda j: identities[j][1][0], reverse=True):
            names, interval = identities[i]
            pmid = _pmid(rows[i])
            later = [j for j in accepted
                     if pmid and _pmid(rows[j]) and _pmid(rows[j]) != pmid
                     and set(names).intersection(identities[j][0])
                     and identities[j][1][0] > interval[1]]
            if later:
                newest = max(later, key=lambda j: identities[j][1][0])
                excluded.add(i)
                audit['excluded'].append({'row_index': i, 'pmid': pmid, 'cohort_ids': list(names),
                    'overlapping_cohort_ids': sorted(set(names).intersection(identities[newest][0])),
                    'retained_cohort_ids': list(identities[newest][0]),
                    'publication_date_interval': [str(x) for x in interval], 'retained_pmid': _pmid(rows[newest]),
                    'reason': 'An accepted newer publication includes the same cohort; the entire earlier estimate is omitted from this view and all recorded evidence is preserved.'})
            else:
                accepted.append(i)
    selected = [row for i, row in enumerate(rows) if i not in excluded]
    audit.update(retained_rows=len(selected), excluded_rows=len(excluded))
    return selected, audit


def filter_latest_cohort_frame(frame, repo_root=None, group_fields=None):
    rows = frame.to_dict(orient='records')
    _, audit = select_latest_cohort_records(rows, repo_root=repo_root, group_fields=group_fields)
    excluded = {item['row_index'] for item in audit['excluded']}
    result = frame.iloc[[i for i in range(len(frame)) if i not in excluded]].copy()
    result.attrs['cohort_selection_audit'] = audit
    return result


def annotate_latest_cohort_rows(rows, repo_root=None):
    rows = list(rows)
    _, audit = select_latest_cohort_records(rows, repo_root=repo_root)
    excluded = {item['row_index']: item for item in audit['excluded']}
    registry = load_registry(repo_root)
    annotated = []
    for i, row in enumerate(rows):
        result = dict(row)
        names, interval = _identity(row, registry)
        result['cohort_ids'] = list(names)
        result['cohort_latest_excluded'] = i in excluded
        if i in excluded:
            result['cohort_selection_reason'] = excluded[i]['reason']
        annotated.append(result)
    return annotated


def record_metadata_key(row):
    """Stable persisted identity for a saved scientific effect record."""
    for field in ('Effect ID', 'effect_record_id', 'Screening Record ID', 'record_id', 'context_id'):
        value = _text(row.get(field))
        if value and value.lower() not in _UNKNOWN and value != '-':
            return value if value.startswith('id:') else 'id:' + value

    def first(*fields):
        return next((row[field] for field in fields if field in row and _text(row[field]).lower() not in _UNKNOWN and _text(row[field]) != '-'), '')

    def normalize(value, numeric=False):
        if value is None or isinstance(value, float) and math.isnan(value):
            return ''
        if isinstance(value, dict):
            return {str(k): normalize(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
        if isinstance(value, (tuple, list)):
            return [normalize(item) for item in value]
        text = ' '.join(_text(value).split())
        if text.lower() in _UNKNOWN or text == '-':
            return ''
        if numeric:
            try:
                number = Decimal(text.replace(',', ''))
                if number.is_finite():
                    return format(number.normalize(), 'f')
            except InvalidOperation:
                pass
        return text.casefold()

    fields = {
        'pmid': _pmid(row),
        'study': first('Study', 'study', 'Authors', 'authors', 'Reference', 'reference'),
        'exposure': first('Direct Exposure', 'direct_exposure', 'Exposure', 'exposure', 'exposure_name'),
        'disease': first('Direct Disease', 'direct_disease', 'Disease', 'disease', 'cancer'),
        'endpoint': first('Endpoint', 'endpoint', 'Outcome', 'outcome', 'Outcome Type', 'outcome_type'),
        'sex': first('Gender', 'gender', 'Sex', 'sex'),
        'effect_type': first('Original Effect Type', 'original_effect_type', 'Effect Type', 'effect_type', 'Measure', 'measure'),
        'population': first('Population', 'population'),
        'independent_population': first('independent_population_id', 'Independent Population ID', 'population_id', 'Population ID', 'independent_population'),
        'stratum': first('Stratum', 'stratum', 'Subgroup', 'subgroup', 'subgroup_name'),
    }
    payload = {key: normalize(value) for key, value in fields.items()}
    for key, aliases in {
        'effect': ('Original Effect Size', 'original_effect_size', 'Effect Size', 'effect_size', 'effect'),
        'lower_ci': ('Original Lower CI', 'original_lower_ci', 'Lower CI', 'lower_ci', 'ci_low'),
        'upper_ci': ('Original Upper CI', 'original_upper_ci', 'Upper CI', 'upper_ci', 'ci_high'),
    }.items():
        payload[key] = normalize(first(*aliases), numeric=True)
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return 'row:' + hashlib.sha256(serialized.encode()).hexdigest()


def load_record_metadata(repo_root=None):
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parent
    path = root / 'data/cohort_record_metadata.json'
    if not path.is_file():
        return {'records': {}, 'record_aliases': {}}
    stat = path.stat()
    return _cached_record_metadata(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=24)
def _cached_record_metadata(path, modified_ns, size):
    del modified_ns, size
    return json.loads(Path(path).read_text(encoding='utf-8'))


def enrich_cohort_records(rows, repo_root=None):
    """Copy saved rows with display identities/date/provenance, without selection flags."""
    registry = load_registry(repo_root)
    metadata = load_record_metadata(repo_root)
    output = []
    for raw in rows:
        row = deepcopy(raw)
        for flag in ('cohort_latest_excluded', 'cohort_selection_reason', 'cohort_pooling_selected', 'Cohort Pooling Selected', 'Cohort Selection Reason'):
            row.pop(flag, None)
        record = registry.get(_pmid(row), {})
        values, source = _cohort_values(row, record)
        if isinstance(values, str):
            values = re.split(r';|\|', values)
        elif isinstance(values, dict):
            values = [values]
        names = []
        for value in values or []:
            label = _text(value.get('canonical_name') or value.get('name') or value.get('cohort_id')) if isinstance(value, dict) else _text(value)
            if not normalize_cohort_name(value):
                continue
            if isinstance(value, dict):
                cohort_round = _text(value.get('round'))
                if cohort_round and not _has_round_number(label, cohort_round):
                    label += ' ' + cohort_round
            if label not in names:
                names.append(label)
        display_name = '; '.join(names)
        prior_source = (raw.get('cohort_provenance') or {}).get('identity_source') if isinstance(raw.get('cohort_provenance'), dict) else None
        derived = bool(prior_source and (prior_source.startswith('registry_') or prior_source == 'unresolved'))
        if not derived and row.get('Cohort Name') and row['Cohort Name'] != display_name:
            row.setdefault('cohort_original_name_value', deepcopy(row['Cohort Name']))
        row['Cohort Name'] = display_name
        verified = _registry_publication_date(record)
        if verified is not None:
            row['Cohort Publication Date'] = verified
        else:
            dates = [(stamp, row.get(field)) for field in _DATE_FIELDS if (stamp := _date_interval(row.get(field)))]
            row['Cohort Publication Date'] = _text(min(dates, key=lambda item: (item[0][1] - item[0][0]).days)[1]) if dates else ''
        row['cohort_provenance'] = {'identity_source': source, 'registry_status': record.get('status'),
                                    'registry_provenance': deepcopy(record.get('provenance'))}
        key = record_metadata_key(row)
        resolved = metadata.get('record_aliases', {}).get(key, key)
        if not isinstance(resolved, str):
            resolved = key
        row['cohort_record_metadata_id'] = resolved
        row['cohort_record_metadata'] = deepcopy(metadata.get('records', {}).get(resolved))
        row['cohort_record_metadata_source'] = 'data/cohort_record_metadata.json'
        output.append(row)
    return output
