"""Verify completed source releases before rebuilding an aggregate application."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from datetime import date, timedelta


def monthly_cutoff(today=None):
    """Keep delayed aggregate retries bound to the scheduled complete-day window."""
    today = today or date.today()
    if today.day >= 28:
        return today.replace(day=27)
    return (today.replace(day=1) - timedelta(days=1)).replace(day=27)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def artifact(root, relative, checksum, allow_deleted=False):
    part = Path(relative)
    path = (root / part).resolve()
    if part.is_absolute() or '..' in part.parts or not path.is_relative_to(root):
        raise ValueError('Source release artifact path escapes its repository')
    if checksum is None:
        if not allow_deleted:
            raise ValueError('Source release artifact checksum is absent')
        if path.exists():
            raise ValueError('Source release deletion receipt does not match the current artifact')
    elif not path.is_file() or sha(path) != checksum:
        raise ValueError('Source release artifact failed its recorded checksum')
    return path


def lineage_outputs(root, manifest, manifest_path, seen=None):
    """Include unchanged files from prior successful months after missed retries."""
    seen = set() if seen is None else seen
    run_id = manifest['run_id']
    if run_id in seen:
        raise ValueError('Source release lineage contains a cycle')
    seen.add(run_id)
    result = {}
    previous = manifest.get('watermark_source', {})
    if previous.get('kind') == 'successful_release':
        name = 'data/monthly_updates/' + previous['run_id'] + '/' + manifest_path.name
        path = artifact(root, name, previous['manifest_sha256'])
        earlier = read(path)
        if (earlier.get('status') != 'completed' or earlier.get('run_id') != previous['run_id']
                or earlier.get('repository') != manifest.get('repository')):
            raise ValueError('Source release predecessor is not a completed release for this repository')
        result.update(lineage_outputs(root, earlier, path, seen))
    ui = manifest.get('ui_refresh', {})
    if ui.get('status') not in {'refreshed', 'already_current', 'no_new_accepted_rows'}:
        raise ValueError('Source release did not complete its UI refresh')
    outputs = ui.get('output_sha256')
    if not isinstance(outputs, dict):
        raise ValueError('Source release lacks UI artifact checksums')
    result.update(outputs)
    return result


def validate_release(root, required_cutoff=None):
    root = Path(root).resolve()
    state_path = root / 'data/monthly_automation/state.json'
    if not state_path.is_file():
        return None
    state = read(state_path)
    if state.get('status') != 'completed':
        return None
    cutoff = date.fromisoformat(state['last_successful_publication_date'])
    if required_cutoff and cutoff != required_cutoff:
        return None
    path = artifact(root, state['manifest_path'], state.get('manifest_sha256'))
    manifest = read(path)
    if (manifest.get('status') != 'completed' or manifest.get('repository') != state.get('repository')
            or manifest.get('run_id') != state.get('run_id') or manifest.get('end_date') != str(cutoff)):
        raise ValueError('Source release state and manifest disagree')
    raw = manifest.get('raw_packet_sha256', {})
    if not isinstance(raw, dict) or not {'candidates.json', 'final_screening.json'}.issubset(raw):
        raise ValueError('Source release lacks complete screening receipts')
    for relative, checksum in raw.items():
        artifact(path.parent, relative, checksum)
    reviews = manifest.get('review_artifacts_sha256', {})
    if not isinstance(reviews, dict) or not reviews:
        raise ValueError('Source release lacks its human-review artifacts')
    for relative, checksum in reviews.items():
        artifact(root, relative, checksum)
    artifact(path.parent, 'monthly_application_gate.json', manifest.get('application_gate_sha256'))
    for relative, checksum in lineage_outputs(root, manifest, path).items():
        artifact(root, relative, checksum, allow_deleted=True)
    return {'repository': state['repository'], 'cutoff': str(cutoff), 'run_id': state['run_id'],
            'state_sha256': sha(state_path), 'manifest_sha256': state['manifest_sha256']}
