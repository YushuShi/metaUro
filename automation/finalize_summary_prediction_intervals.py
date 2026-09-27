"""Bind completed forest-only exports without rebuilding scientific summaries."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import summary_outputs

AUDIT = ROOT / 'evidence-revisions/summary-pi-2026-09-27'


def finalize():
    reports = []
    for source in ('metaPee', 'MetaVir'):
        reports.extend(json.loads((AUDIT / f'render_report_{source}.json').read_text()))
    rendered = {item['file']: item for item in reports}
    assert len(rendered) == len(reports), 'Duplicate forest exports'
    expected = set()
    manifests = {}
    for mode in ('no', 'yes'):
        folder = ROOT / 'summary' / mode
        path = folder / 'summary_manifest.json'
        manifest = json.loads(path.read_text())
        for name, old in manifest['artifacts'].items():
            artifact = folder / name
            relative = str(artifact.relative_to(ROOT))
            current = {'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
                       'bytes': artifact.stat().st_size}
            if artifact.name.startswith('forest_'):
                expected.add(relative)
                assert relative in rendered, f'Forest export missing: {relative}'
                assert current['sha256'] == rendered[relative]['sha256'], relative
                manifest['artifacts'][name] = current
            else:
                assert current == old, f'Nonforest artifact changed: {relative}'
        for source in ('metaPee', 'MetaVir'):
            manifest['sources'][source]['input_sha256'] = summary_outputs.input_fingerprint(ROOT, source, mode)
        manifest['forest_prediction_interval_style'] = {
            'version': 'separate-summary-pi-2026-09-27',
            'source': 'unchanged published summary workbook values',
            'line': 'dark dashed line below CI, with endpoint caps; arrows mark clipping',
            'missing_bounds': 'not drawn',
        }
        manifests[path] = manifest
    assert expected == set(rendered), 'Completed exports differ from declared forests'
    for path, manifest in manifests.items():
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    bindings = []
    for path, manifest in manifests.items():
        for source, provenance in manifest['sources'].items():
            fingerprint = provenance['input_sha256']
            assert summary_outputs._current(path.parent, fingerprint, source)
            bindings.append({'mode': path.parent.name, 'source': source,
                             'input_sha256': fingerprint, 'current': True})
    (AUDIT / 'summary_bindings.json').write_text(json.dumps(bindings, indent=2) + '\n')
    print(f'Bound {len(expected)} forest PDFs; all four summary source/mode signatures are current.')


if __name__ == '__main__':
    finalize()
