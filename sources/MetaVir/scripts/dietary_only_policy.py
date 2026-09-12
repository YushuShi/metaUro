"""User-approved exposure measurement classification, separate from source facts."""
from __future__ import annotations
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
AUDIT_FIELDS = (
    'exposure_measurement_type_original', 'exposure_measurement_original_fields',
    'exposure_measurement_overridden', 'exposure_measurement_policy',
)
ALIASES = ('exposure_measurement_type', 'exposure_measurement',
           'Exposure Measurement Type', 'Exposure Measurement', 'Exposure measurement')


def key(value):
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.sub(r'[^a-z0-9]+', '_', text).strip('_')


@lru_cache(maxsize=16)
def _config(path, mtime_ns):
    return json.loads(Path(path).read_text())


def config(repo_root=None):
    path = Path(repo_root or ROOT) / 'data/dietary_only_exposures.json'
    return _config(str(path), path.stat().st_mtime_ns)


def is_dietary_only_exposure(exposure, repo_root=None):
    return key(exposure) in {key(name) for name in config(repo_root)['exposures']}


def exposure_for(record):
    return (record.get('hierarchy_requested_exposure') or record.get('Displayed Exposure')
            or record.get('displayed_exposure') or record.get('exposure')
            or record.get('Exposure') or record.get('Direct Exposure')
            or record.get('direct_exposure'))


def apply_row(record, exposure=None, repo_root=None, *, inplace=False):
    """Annotate one row/extraction, using an explicit displayed exposure first.

    Never call this on an immutable source packet. The default returns a copy.
    Only exposure-measurement fields change; eligibility and effect values do not.
    """
    if not isinstance(record, dict):
        return deepcopy(record) if not inplace else record
    row = record if inplace else deepcopy(record)
    target = exposure if exposure is not None else exposure_for(row)
    if not target or not is_dietary_only_exposure(target, repo_root):
        return row
    policy = config(repo_root)
    if not isinstance(row.get('exposure_measurement_original_fields'), dict):
        row['exposure_measurement_original_fields'] = {field: deepcopy(row[field]) for field in ALIASES if field in row}
        row['exposure_measurement_type_original'] = deepcopy(row.get('exposure_measurement_type'))
    row['exposure_measurement_type'] = 'dietary_intake'
    for field in ALIASES[1:]:
        if field in row:
            row[field] = 'Dietary intake only' if field[0].isupper() else 'dietary_intake'
    row['exposure_measurement_overridden'] = True
    row['exposure_measurement_policy'] = {
        'id': policy['policy_id'], 'version': policy['version'],
        'source': policy['source'], 'exposure': str(target),
        'label': 'Dietary intake only',
        'reason': 'User-approved classification for this exposure; original source classification is retained.',
    }
    return row


def apply_record(record, exposure=None, repo_root=None, *, inplace=False):
    """Apply to one effective screening record and its extraction/extra estimates.

    Raw model response, request, history, and audit fields are never traversed.
    """
    if not isinstance(record, dict):
        return deepcopy(record) if not inplace else record
    result = record if inplace else deepcopy(record)
    target = exposure if exposure is not None else exposure_for(result)
    apply_row(result, target, repo_root, inplace=True)
    extraction = result.get('extraction')
    if isinstance(extraction, dict):
        result['extraction'] = apply_record(extraction, target, repo_root, inplace=True)
    for field in ('additional_records', 'additional_extractions'):
        if isinstance(result.get(field), list):
            result[field] = [apply_record(child, target, repo_root, inplace=True) for child in result[field]]
    return result


def prompt_note(exposure=None, repo_root=None):
    if exposure is not None:
        if not is_dietary_only_exposure(exposure, repo_root):
            return ''
        return ('PROJECT EXPOSURE-MEASUREMENT RULE: For '+str(exposure)+', the saved exposure_measurement_type is dietary_intake (Dietary intake only). '
                'Record the actual measurement method in the evidence notes; the project classification override retains original-source provenance. '
                'This rule does not change eligibility, quality, effect values, study design, or route-of-administration facts.')
    return ('PROJECT EXPOSURE-MEASUREMENT RULE: Approved food/intake/diet and botanical/fruit/vegetable exposure names are classified as dietary_intake '
            '(Dietary intake only) by the saved-results policy. Keep the actual reported measurement method and administration route in evidence notes. '
            'The override does not change eligibility, quality, effect values, or study design.')


# Descriptive aliases for adapters in the individual repositories.
apply_dietary_only_policy = apply_record
is_dietary_only = is_dietary_only_exposure
PROMPT = prompt_note()
