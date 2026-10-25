import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import pandas as pd
import pytest
import summary_outputs

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('source', ['MetaVir', 'metaPee'])
def test_final_capture_selects_only_working_frame_and_keeps_rounds(source):
    path = ROOT / 'runtime_code' / source / 'analysis_settings_backend.py'
    spec = importlib.util.spec_from_file_location('cohort_runtime_' + source, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    records = [{'PMID': str(i + 1), 'Study': f'Paper {i}', 'cohort_name': name, 'Year': year,
                'Effect Size': 1.2, 'Lower CI': 1.0, 'Upper CI': 1.5}
               for i, (name, year) in enumerate([('NHS II', 2010), ('NHS II', 2020), ('NHS III', 2018)])]
    records[0].update(Gender='-', Exposure='Historical raw label')
    records[1].update(Gender='Female', Exposure='Current raw label')
    before = copy.deepcopy(records)
    default = pd.DataFrame(records)
    module.capture_pooling_rows(default)
    assert len(default) == 3
    latest = pd.DataFrame(records)
    with module.latest_cohort_pooling(True) as context:
        module.capture_pooling_rows(latest)
    assert latest['PMID'].tolist() == ['2', '3']
    assert len(context['rows']) == 2
    assert context['audit']['excluded_rows'] == 1
    assert records == before


def test_cohort_summary_is_a_separate_optional_directory():
    assert summary_outputs.summary_dir(ROOT, 'MetaVir', 'yes') == ROOT / 'summary/yes'
    assert summary_outputs.summary_dir(ROOT, 'MetaVir', 'yes', True) == ROOT / 'summary/yes/cohort_latest'
    assert summary_outputs.summary_dir(ROOT, 'metaPee', 'no', True) == ROOT / 'summary/no/cohort_latest'


def test_source_snapshots_remain_exactly_recorded():
    manifest = json.loads((ROOT / 'runtime_code/manifest.json').read_text())
    for source, entry in manifest['sources'].items():
        for filename, hashes in entry['files'].items():
            path = ROOT / 'sources' / source / filename
            assert hashlib.sha256(path.read_bytes()).hexdigest() == hashes['snapshot_sha256']


@pytest.mark.parametrize('source', ['MetaVir', 'metaPee'])
def test_identical_effect_values_keep_precise_cohort_and_forest_flags(source, monkeypatch):
    path = ROOT / 'runtime_code' / source / 'analysis_settings_backend.py'
    spec = importlib.util.spec_from_file_location('precise_cohort_' + source, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = {'PMID': '999997', 'Study': 'Old report', 'Year': 2010,
            'Effect Type': 'RR', 'Effect Size': 1.2, 'Lower CI': 1.0, 'Upper CI': 1.5}
    records = [dict(base, **{'Effect ID': 'older-nhs', 'cohort_name': 'NHS II'}),
               dict(base, **{'Effect ID': 'older-hpfs', 'cohort_name': 'HPFS'}),
               dict(base, **{'Effect ID': 'newer-nhs', 'PMID': '999998', 'Study': 'New report',
                             'cohort_name': 'NHS II', 'Year': 2024})]
    settings = module.normalize_settings({'keep_latest_cohort': True}, {})
    context = {'settings': settings, 'baseline': {'p0': .1, 'risk_percent': 10, 'identity_approximation': False}}
    token = module._ACTIVE.set(context)
    try:
        frame = pd.DataFrame(records)
        module.capture_pooling_rows(frame)
    finally:
        module._ACTIVE.reset(token)
    assert frame['Effect ID'].tolist() == ['older-hpfs', 'newer-nhs']
    monkeypatch.setattr(module, '_forest_plots', lambda *args: {})
    monkeypatch.setattr(module, '_diagnostic_plots', lambda *args: {})
    result = module._project({'studies': records, 'headline': {'pooled_es': 1.2, 'ci_low': 1.0, 'ci_upp': 1.5}}, context)
    flags = {row['Effect ID']: (row['cohort_latest_excluded'], row['Settings Pooling Selected']) for row in result['studies']}
    assert flags == {'older-nhs': (True, False), 'older-hpfs': (False, True), 'newer-nhs': (False, True)}
    assert len(result['forest_annotations']) == 3  # two retained estimates plus pooled result
