"""Read-only publication counts from the current saved pooling flags."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path


@lru_cache(maxsize=2)
def _catalog(signatures):
    entries = {}
    for filename, _modified, _size in signatures:
        raw = Path(filename).read_bytes()
        payload = json.loads(raw)
        disease = payload.get('disease') or payload.get('cancer')
        exposure = payload.get('exposure')
        rows = payload.get('studies') or []
        if not disease or not exposure or not rows:
            continue
        saved, pooled = set(), set()
        for row in rows:
            pmid = str(row.get('PMID') or row.get('pmid') or '').strip()
            if not pmid.isdigit():
                raise ValueError('Saved publication identifiers are unavailable.')
            saved.add(pmid)
            if (payload.get('headline') and row.get('subexposure_pooling_selected') is True
                    and row.get('disease_hierarchy_pooling_selected') is not False
                    and row.get('disease_scope_pooling_selected') is not False
                    and row.get('display_only') is not True):
                pooled.add(pmid)
        key = (disease, exposure)
        if key in entries:
            raise ValueError('Duplicate saved disease and exposure.')
        entries[key] = {'saved_reports': len(saved), 'pooled_reports': len(pooled),
                        'outside_pool': len(saved - pooled), 'source_path': filename,
                        'source_sha256': hashlib.sha256(raw).hexdigest()}
    return entries


def saved_selection(cache_root, disease=None, exposure=None):
    paths = sorted(Path(cache_root).glob('*/*_incidence_true_core.json'))
    signatures = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
    entries = _catalog(signatures)
    choices = {}
    for current_disease, current_exposure in sorted(entries):
        choices.setdefault(current_disease, []).append(current_exposure)
    if not choices:
        return {'available': False, 'catalog': {}}
    disease = disease if disease in choices else next(iter(choices))
    exposures = choices[disease]
    exposure = exposure if exposure in exposures else ('Vitamin A' if 'Vitamin A' in exposures else exposures[0])
    return {'available': True, 'catalog': choices, 'disease': disease,
            'exposure': exposure, 'exposures': exposures, **entries[(disease, exposure)]}
