import json
from pathlib import Path
import subprocess

import pytest

from automation.sync_sources import (
    assembled_source,
    legacy_summary_artifact,
    load_overlay,
    sha,
    sync_sources,
    write,
)


def _git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def _study(pmid):
    return {
        'PMID': str(pmid), 'Effect ID': str(pmid), 'Direct Exposure': 'Fiber',
        'Effect Type': 'RR', 'Effect Size': 0.8, 'Lower CI': 0.7,
        'Upper CI': 0.9, 'Sample Size': 1000, 'Cases': 100,
    }


def _prepare(tmp_path):
    repo, siblings = tmp_path / 'metaUro', tmp_path / 'sources'
    cache = 'Subexposure_results/yes/fiber/cancer_incidence_true_all.json'
    plot_roots = {
        'metaPee': 'combined/provisional_ui/subexposure_views/yes/Plot',
        'MetaVir': 'Plot/subexposures/yes',
    }
    for source, plot_root in plot_roots.items():
        baseline = repo / 'sources' / source
        write(baseline / cache, {'studies': [_study(1)]})
        (baseline / 'app.py').write_text('# immutable fixture\n')
        legacy = {
            f'{plot_root}/summary_manifest.json': b'{"schema_version": 2}\n',
            f'{plot_root}/exposures_meta_analysis_cancer_combined.xlsx': b'workbook',
            f'{plot_root}/forest_harmful_cancer.pdf': b'%PDF-summary',
            f'{plot_root}/plot_es_vs_heterogeneity_cancer.png': b'png-summary',
        }
        for relative, value in legacy.items():
            path = baseline / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)

        source_root = siblings / source
        run = source_root / 'data/monthly_updates/2026-09-01_2026-09-28'
        write(source_root / cache, {'studies': [_study(1), _study(2)]})
        outputs = {cache: sha(source_root / cache)}
        for relative, value in legacy.items():
            path = source_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value + b'-updated')
            outputs[relative] = sha(path)
        retained = source_root / 'static/retained-monthly-image.png'
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_bytes(b'png-runtime')
        outputs[str(retained.relative_to(source_root))] = sha(retained)
        for name in ('candidates.json', 'final_screening.json'):
            write(run / name, {'results': []})
        write(run / 'monthly_application_gate.json', {'blocked_context_ids': []})
        review = run / 'discrepancies.csv'
        review.write_text('context_id,human_decision\n')
        release = {
            'status': 'completed', 'repository': source, 'run_id': run.name,
            'end_date': '2026-09-28',
            'raw_packet_sha256': {
                name: sha(run / name) for name in ('candidates.json', 'final_screening.json')},
            'review_artifacts_sha256': {str(review.relative_to(source_root)): sha(review)},
            'application_gate_sha256': sha(run / 'monthly_application_gate.json'),
            'ui_refresh': {'status': 'refreshed', 'output_sha256': outputs},
        }
        manifest = run / 'monthly_run.json'
        write(manifest, release)
        write(source_root / 'data/monthly_automation/state.json', {
            'status': 'completed', 'repository': source, 'run_id': run.name,
            'manifest_path': str(manifest.relative_to(source_root)),
            'manifest_sha256': sha(manifest),
            'last_successful_publication_date': '2026-09-28',
            'completed_at': '2026-09-28T10:00:00+00:00',
            'human_review_pending': 0,
        })
        _git(source_root, 'init', '-q')
        _git(source_root, 'config', 'user.email', 'fixture@example.com')
        _git(source_root, 'config', 'user.name', 'Fixture')
        _git(source_root, 'add', '.')
        _git(source_root, 'commit', '-qm', 'Fixture release')
    return repo, siblings, plot_roots


@pytest.mark.parametrize('relative,expected', [
    ('Plot/subexposures/yes/summary_manifest.json', True),
    ('Plot/subexposures/yes/exposures_meta_analysis_cancer_combined.xlsx', True),
    ('combined/provisional_ui/subexposure_views/no/Plot/forest_harmful_cancer.pdf', True),
    ('combined/provisional_ui/subexposure_views/no/Plot/locales/ko/chart.png', True),
    ('static/summary-thumbnail.png', False),
    ('Plot/build_receipt.json', False),
    ('Cached_results/fiber/cancer_incidence_true_all.json', False),
])
def test_legacy_summary_artifact_is_limited_to_generated_plot_assets(relative, expected):
    assert legacy_summary_artifact(relative) is expected


def test_source_summary_outputs_are_verified_but_not_installed(tmp_path):
    repo, siblings, plot_roots = _prepare(tmp_path)
    sync_sources(repo, siblings)

    for source, plot_root in plot_roots.items():
        overlay = load_overlay(repo, source)
        assert 'static/retained-monthly-image.png' in overlay['files']
        assert not any(legacy_summary_artifact(relative) for relative in overlay['files'])
        installed = repo / 'updates' / source
        assert not any(
            legacy_summary_artifact(path.relative_to(installed))
            for path in installed.rglob('*') if path.is_file()
        )
        runtime = assembled_source(repo, source)
        assert (runtime / 'static/retained-monthly-image.png').read_bytes() == b'png-runtime'
        assert not (runtime / plot_root / 'summary_manifest.json').exists()
        assert not any(
            legacy_summary_artifact(path.relative_to(runtime))
            for path in runtime.rglob('*') if path.is_file()
        )
