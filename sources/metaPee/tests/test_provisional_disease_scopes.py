"""Saved disease parents use identical rows and populations in UI and summaries."""
import contextlib
import hashlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from openpyxl import load_workbook
import app
from scripts import build_summary_plots as summary
from scripts import provisional_disease_scopes as helper
from scripts import provisional_ui_runtime as runtime

SKIN = Path(app.BASE_DIR).name == 'MetaCutis_et_ Capillus'
PARENT = 'skin_cancer' if SKIN else 'kidney_and_renal_pelvis_cancer'
CHILD = 'cutaneous_melanoma' if SKIN else 'renal_cell_carcinoma'
OTHER = 'basal_cell_carcinoma' if SKIN else 'clear_cell_renal_cell_carcinoma'


class ProvisionalDiseaseScopeTests(unittest.TestCase):
    def row(self, pmid, n, effect=.8, **changes):
        row = {'PMID': str(pmid), 'Study': f'Example {pmid} et al. (2020)',
            'Record ID': f'example-{pmid}-{effect}', 'Effect Type': 'RR', 'Outcome Type': 'RR',
            'Effect Size': effect, 'Lower CI': effect * .8, 'Upper CI': effect * 1.2,
            'Sample Size': n, 'Cases': 50, 'SE': .1, 'Gender': 'All',
            'Quality Score': 'Moderate', 'Primary Estimate': True,
            'policy_poolable': True, 'manual_poolable': True,
            'pool_in_parent_meta_analysis': True, 'subexposure_pooling_selected': True,
            'exposure_measurement_type': 'dietary_intake', 'Endpoint': 'Incidence',
            'manual_review_pending': True,
            'provisional_ui_selection': {'selected_track': 'chatgpt', 'review_ids': [f'review-{pmid}']}}
        row.update(changes)
        return row

    def save_source(self, root, key, rows):
        disease = app.get_scope_config()[key]['disease']
        payload = {'success': True, 'canonical_track': 'combined', 'studies': rows,
            'disease': disease, 'cancer': disease, 'exposure': 'Vitamin D',
            'endpoint': 'Incidence', 'outcome': 'Incidence', 'include_subexposures': 'no'}
        path = root / 'vitamin_d' / f'{app.safe_path_component(disease)}_incidence_true_core.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def build_fixture(self, root):
        self.save_source(root, CHILD, [self.row(1, 1000), self.row(2, 2000, 1.2, Cases=80),
            self.row(3, 1500, .75, Cases=None, **{'Effect Type': 'OR', 'Outcome Type': 'OR'}),
            self.row(4, 9999, **{'Quality Score': 'Fair', 'policy_poolable': False}),
            self.row(5, 9999, manual_pooling_eligible=False)])
        self.save_source(root, OTHER, [self.row(1, 1000, .9, effect_is_adjusted=True,
            exposure_measurement_type='circulating_biomarker')])
        with contextlib.redirect_stdout(io.StringIO()):
            helper.materialize(root)
        return root / 'vitamin_d' / f'{PARENT}_incidence_true_core.json'

    def test_parent_rows_stay_visible_and_identical_across_routes_and_repeated_builds(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'no/Cached_results'
            path = self.build_fixture(root)
            payload = json.loads(path.read_text())
            self.assertEqual(len(payload['studies']), 6)
            self.assertEqual(payload['include_subexposures'], 'no')
            self.assertEqual(sum(row['disease_hierarchy_pooling_selected'] for row in payload['studies']), 3)
            self.assertEqual(path.read_bytes(), path.with_name(path.name.replace('_core', '_all')).read_bytes())
            before = path.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                helper.materialize(root)
            self.assertEqual(before, path.read_bytes())
            with patch.object(app, 'get_cache_path', side_effect=AssertionError('Recomposed a saved parent')):
                result = app.load_disease_hierarchy_result(payload['disease'], 'Vitamin D', 'Incidence', True,
                                                          False, payload)
            self.assertIs(result, payload)
            visible = runtime.identify_rows(payload, app.BASE_DIR, 'no')
            bound = runtime.bind_submitted_rows(visible['studies'], payload, app.BASE_DIR, 'no')
            for saved, selected in zip(payload['studies'], bound):
                for key in ('Quality Score', 'provisional_ui_selection', 'policy_poolable',
                            'manual_pooling_eligible', 'disease_hierarchy_pooling_selected'):
                    self.assertEqual(selected.get(key), saved.get(key))
            for source, expected in payload['disease_hierarchy_materialization']['source_hashes'].items():
                self.assertEqual(hashlib.sha256((root / source).read_bytes()).hexdigest(), expected)

    def test_summary_and_workbook_count_only_the_final_independent_populations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'no/Cached_results'
            path = self.build_fixture(root)
            payload = json.loads(path.read_text())
            disease = payload['disease']
            with contextlib.redirect_stdout(io.StringIO()):
                analysis = helper.analyze_parent(pd.DataFrame(payload['studies']),
                    pd.DataFrame(payload['studies']), disease, 'Vitamin D')
                record = summary.summarize_cache(path, disease, 'vitamin_d', False)
            self.assertEqual(len(analysis['pooled_rows']), 3)
            self.assertEqual(record['number studies' if SKIN else 'number estimates'], 3)
            self.assertEqual(record['total N'], 4500)
            p0 = analysis['baseline']['p0']
            self.assertEqual(record['total Cases'], 130 + (math.floor(1500 * p0 + .5) if p0 is not None else 0))
            headline = analysis['result']['headline']
            canonical = headline.get('canonical_rr') or headline
            self.assertAlmostEqual(record['Pooled RR'], canonical['pooled_es'])
            exported = Path(folder) / 'summary.xlsx'
            summary.workbook(exported, [record])
            sheet = load_workbook(exported, data_only=True).active
            values = list(sheet.values)
            row = dict(zip(values[0], values[1]))
            self.assertEqual(row['total N'], 4500)
            self.assertEqual(row['number studies' if SKIN else 'number estimates'], 3)

    def test_dietary_summary_can_select_the_dietary_sibling_of_a_selected_biomarker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.build_fixture(Path(folder) / 'no/Cached_results')
            payload = json.loads(path.read_text())
            with contextlib.redirect_stdout(io.StringIO()):
                combined = summary.summarize_cache(path, payload['disease'], 'vitamin_d', False)
                dietary = summary.summarize_cache(path, payload['disease'], 'vitamin_d', True)
            self.assertEqual(combined['total N'], 4500)
            self.assertEqual(dietary['total N'], 4500)
            self.assertNotEqual(combined['Pooled RR'], dietary['Pooled RR'])


if __name__ == '__main__':
    unittest.main()
