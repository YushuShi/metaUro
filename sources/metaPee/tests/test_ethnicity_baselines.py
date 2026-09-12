"""Published population risks must retain their exact endpoint and stratum."""
import csv
from pathlib import Path

import pytest

import analysis_settings_backend as backend
from scripts.build_ethnicity_baselines import build, load_json

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def catalog():
    return load_json(ROOT / 'static/seer-baseline.json')


def resolve(catalog, disease='Bladder cancer', **settings):
    return backend.resolve_baseline(catalog, disease, None, backend.normalize_settings(settings, catalog))


def test_direct_ethnicity_values_are_distinct_and_preserve_published_sexes(catalog):
    assert len(catalog['races']) == 8
    assert {row['id'] for row in catalog['sexes']} == {'both', 'female', 'male'}
    assert resolve(catalog)['risk_percent'] == pytest.approx(0.4540)
    white = resolve(catalog, race='non_hispanic_white')
    black = resolve(catalog, race='non_hispanic_black')
    assert white['risk_percent'] == pytest.approx(0.5585)
    assert black['risk_percent'] == pytest.approx(0.3130)
    assert resolve(catalog, race='black_includes_hispanic')['risk_percent'] == pytest.approx(0.3060)
    assert resolve(catalog, race='non_hispanic_black', sex='male')['risk_percent'] == pytest.approx(0.4914)
    assert resolve(catalog, race='non_hispanic_black', sex='female')['risk_percent'] == pytest.approx(0.1615)
    assert black['risk_percent'] != pytest.approx((0.4914 + 0.1615) / 2)
    kidney = resolve(catalog, 'Kidney and renal pelvis cancer', race='non_hispanic_black')
    assert kidney['risk_percent'] == pytest.approx(0.5507)


def test_all_records_match_exact_frozen_source_and_csv_coordinate(catalog):
    base = ROOT / 'incidenceRate/published_seer'
    sources = {str(site): load_json(base / f'sources/risk_comparison_site_{site}.json')['data'] for site in (71, 72)}
    sexes = {item['id']: item for item in catalog['sexes']}
    races = {item['id']: item for item in catalog['races']}
    disease_sites = {item['id']: item['source_site_code'] for item in catalog['diseases']}
    matrices = {}
    for row in catalog['records']:
        sex = sexes[row['sex']]
        key = f"10_{sex['seer_code']}_{races[row['race']]['seer_code']}_{300 + row['age'] // 5}_{row['years']}_{disease_sites[row['disease']]}"
        assert row['source_key'] == key
        point = sources[disease_sites[row['disease']]][key]
        assert point['year_range'] == 8
        assert point['data_series'][0][0] == row['risk_percent']
        sex_filename = 'both_sexes' if row['sex'] == 'both' else row['sex']
        filename = f"{row['disease']}__{sex_filename}__{row['race']}__risk_percent.csv"
        if filename not in matrices:
            with (base / filename).open(newline='') as handle:
                matrices[filename] = {int(item['starting_age']): item for item in csv.DictReader(handle)}
        assert matrices[filename][row['age']][str(row['years'])] == row['risk_percent']
        assert row['age'] + row['years'] <= 90
    assert len(catalog['records']) == 3552
    assert len(matrices) == 48


def test_missing_histology_and_group_never_borrow_another_risk(catalog):
    for race in catalog['races']:
        for disease in ('Renal cell carcinoma', 'Urothelial carcinoma of the bladder'):
            answer = resolve(catalog, disease, race=race['id'])
            assert not answer['available'] and answer['p0'] is None
            assert answer['identity_approximation']
    assert not resolve(catalog, race='asian_includes_hispanic')['available']
    with pytest.raises(ValueError, match='exceed 90'):
        resolve(catalog, age=80, years=15, race='non_hispanic_white')
    manual = resolve(catalog, 'Renal cell carcinoma', baseline_source='manual', manual_risk_percent=2.5)
    assert manual['p0'] == 0.025


def test_conversion_cases_and_planning_use_selected_population(catalog):
    study = {'Effect Type': 'OR', 'Effect Size': 2, 'Lower CI': 1.5, 'Upper CI': 2.6,
             'Sample Size': 100000, 'Cases': None, 'Estimated Cases': 1}
    white = resolve(catalog, race='non_hispanic_white')['p0']
    black = resolve(catalog, race='non_hispanic_black')['p0']
    a = backend._study_conversion(study, white, 'RR')
    b = backend._study_conversion(study, black, 'RR')
    assert a['Canonical RR'] == pytest.approx(2 / (1 + white))
    assert b['Canonical RR'] == pytest.approx(2 / (1 + black))
    assert a['Canonical RR'] != b['Canonical RR']
    assert (a['Estimated Cases'], b['Estimated Cases']) == (558, 313)
    assert backend.sample_size_for_rr(a['Canonical RR'], white)['total'] != backend.sample_size_for_rr(b['Canonical RR'], black)['total']
    assert study['Estimated Cases'] == 1


def test_published_package_is_reproducible():
    outputs, summary = build(ROOT)
    assert summary['race_ethnicity_groups'] == 8
    assert summary['matrix_count'] == 288
    assert summary['same_vintage_extraction_discrepancies'] == 0
    for path, expected in outputs.items():
        assert path.read_bytes() == expected, path
