import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
import meta_analysis
from app import DEFAULT_DISEASE, app, get_major_scope_key, get_scope_config

class MetaPeeUrinaryScopeTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_scope_config_and_homepage_use_registry_cancers(self):
        scopes = get_scope_config()
        self.assertEqual(len(scopes), 12)
        self.assertEqual(list(scopes)[0], 'bladder_cancer')
        self.assertEqual(scopes['bladder_cancer']['disease'], 'Bladder cancer')
        self.assertEqual(scopes['bladder_cancer']['lifetime_risk_percent'], 2.1)
        self.assertIn(
            'urinary bladder cancer', scopes['bladder_cancer']['aliases']
        )
        self.assertEqual(scopes['bladder_cancer']['subcategories'], [])
        self.assertEqual(DEFAULT_DISEASE, 'Bladder cancer')
        self.assertEqual(
            get_major_scope_key('renal cell cancer', scopes),
            'renal_cell_carcinoma',
        )

        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('MetaPee', html)
        self.assertIn('value="Bladder cancer"', html)
        self.assertIn('value="Renal cell carcinoma"', html)
        self.assertNotIn('data-subcategory=', html)
        for unsupported in ('Prostate cancer', 'Breast cancer', 'Ovarian cancer'):
            self.assertNotIn(unsupported, html)

    def test_backend_rejects_unsupported_diseases_and_subcategories(self):
        unsupported = self.client.post('/analyze', json={
            'disease': 'Prostate cancer',
            'exposure': 'Coffee',
        })
        self.assertEqual(unsupported.status_code, 400)
        self.assertIn('urinary-system cancers only', unsupported.get_json()['error'])

        subcategory = self.client.post('/analyze', json={
            'disease': 'Bladder cancer',
            'subcategory': 'legacy-subtype',
            'exposure': 'Coffee',
        })
        self.assertEqual(subcategory.status_code, 400)
        self.assertIn('not supported', subcategory.get_json()['error'])

        survival = self.client.post('/analyze', json={
            'disease': 'Bladder cancer',
            'exposure': 'Coffee',
            'outcome': 'Survival',
        })
        self.assertEqual(survival.status_code, 400)
        self.assertIn('incidence only', survival.get_json()['error'])

    def test_config_api_exposes_registry_scopes_and_default(self):
        payload = self.client.get('/api/config').get_json()
        self.assertEqual(payload['default_disease'], 'Bladder cancer')
        self.assertEqual(len(payload['disease_scopes']), 12)
        self.assertEqual(
            payload['disease_scopes'][0]['lifetime_incidence_both_percent'],
            2.1,
        )
        self.assertIn(
            'urinary bladder cancer', payload['disease_scopes'][0]['aliases']
        )
        self.assertEqual(len(payload['cached_results_by_disease']), 12)
        self.assertIn(
            'Avocado', payload['cached_results_by_disease']['bladder_cancer']
        )
        self.assertIn(
            'Alcohol',
            payload['cached_results_by_disease']['renal_cell_carcinoma'],
        )

    def test_config_inventory_uses_only_canonical_core_ui_caches(self):
        with tempfile.TemporaryDirectory() as tempdir, patch('app.CACHE_DIR', tempdir):
            accepted = Path(tempdir) / 'avocado' / 'bladder_cancer_incidence_true_core.json'
            accepted.parent.mkdir(parents=True)
            accepted.write_text(json.dumps({
                'canonical_track': 'chatgpt',
                'cancer': 'Bladder cancer',
                'exposure': 'Avocado',
            }))
            rejected = Path(tempdir) / 'tea' / 'renal_cell_carcinoma_incidence_true_core.json'
            rejected.parent.mkdir(parents=True)
            rejected.write_text(json.dumps({
                'canonical_track': 'deepseek',
                'cancer': 'Renal cell carcinoma',
                'exposure': 'Tea',
            }))
            payload = self.client.get('/api/config').get_json()

        self.assertEqual(
            payload['cached_results_by_disease']['bladder_cancer'], ['Avocado']
        )
        self.assertEqual(
            payload['cached_results_by_disease']['renal_cell_carcinoma'], []
        )

    def test_summary_scope_query_selects_a_saved_cancer_scope(self):
        def summary_scope(label):
            return {
                'label': label,
                'cache_count': 1,
                'workbooks': {
                    'combined': {'available': False, 'exposure_count': 1},
                    'dietary': {'available': False, 'exposure_count': 0},
                },
                'plots': {
                    name: {'available': False}
                    for name in app_module.SUMMARY_PLOT_NAMES
                },
            }

        manifest = {'scopes': {
            'bladder_cancer': summary_scope('Bladder cancer'),
            'renal_cell_carcinoma': summary_scope('Renal cell carcinoma'),
        }}
        with patch('app.load_summary_manifest', return_value=manifest):
            response = self.client.get('/summary?scope=renal_cell_carcinoma')
            fallback = self.client.get('/summary?scope=not_a_real_scope')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<h2>Renal cell carcinoma</h2>', html)
        self.assertIn(
            '<option value="renal_cell_carcinoma" selected>', html
        )
        self.assertIn('<h2>Bladder cancer</h2>', fallback.get_data(as_text=True))

    def test_web_rejects_a_non_chatgpt_cache(self):
        with tempfile.TemporaryDirectory() as tempdir, patch('app.CACHE_DIR', tempdir):
            path = Path(app_module.get_cache_path(
                'Bladder cancer', 'Tomato', 'Incidence', True, False
            ))
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"canonical_track": "deepseek", "studies": []}))
            response = self.client.post('/analyze', json={
                'disease': 'bladder carcinoma',
                'exposure': 'Tomato',
                'outcome': 'Incidence',
                'exclude_meta': True,
            })
        self.assertEqual(response.status_code, 409)
        self.assertIn('not a ChatGPT-canonical', response.get_json()['error'])

    def test_reanalysis_defaults_to_bladder_scope(self):
        canonical_study = {
            'PMID': '1',
            'Quality Score': 'Good',
            'Effect Type': 'RR',
            'Effect Size': 1.1,
            'Lower CI': 1.0,
            'Upper CI': 1.2,
        }
        row_id = app_module.cache_row_id('Bladder cancer', 'Tomato', 0)
        requested_study = dict(canonical_study, **{'Cache Row ID': row_id})
        with tempfile.TemporaryDirectory() as tempdir, patch('app.CACHE_DIR', tempdir):
            path = Path(app_module.get_cache_path(
                'Bladder cancer', 'Tomato', 'Incidence', True, False
            ))
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                'canonical_track': 'chatgpt',
                'exposure': 'Tomato',
                'studies': [canonical_study],
            }))
            with patch(
                'app.meta_analysis.perform_meta_analysis',
                return_value={'headline': {}},
            ) as perform:
                response = self.client.post('/reanalyze', json={
                    'studies': [requested_study],
                    'exposure': 'Tomato',
                })
                arbitrary = self.client.post('/reanalyze', json={
                    'studies': [canonical_study],
                    'exposure': 'Tomato',
                })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(perform.call_args.args[1], 'Bladder cancer')
        self.assertFalse(perform.call_args.kwargs['generate_plots'])
        self.assertTrue(response.get_json()['interactive_reanalysis'])
        self.assertEqual(arbitrary.status_code, 400)
        self.assertIn('saved estimates', arbitrary.get_json()['error'])

    def test_review_reporting_accepts_alias_and_uses_canonical_context(self):
        self.client.get('/')
        with tempfile.TemporaryDirectory() as tempdir:
            review_store = Path(tempdir) / 'verifications.json'
            review_store.write_text('{}', encoding='utf-8')
            with (
                patch('app.VERIFICATIONS_FILE', str(review_store)),
                patch('app.find_review_study', return_value={'PMID': '123'}),
            ):
                response = self.client.post('/exclude', json={
                    'pmid': '123',
                    'disease': 'urinary bladder cancer',
                    'exposure': 'Coffee',
                    'outcome': 'Incidence',
                })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()['context_key'],
            'bladder_cancer_coffee_incidence',
        )
        self.assertFalse(response.get_json()['results_changed'])

    def test_statistical_backstop_excludes_mr_but_not_nongenetic_iv_studies(self):
        for title in (
            'Two-sample MR analysis of lycopene and bladder cancer',
            'Genetically predicted tomato intake and kidney cancer risk',
            'Genetic variants as instrumental variables for lycopene exposure',
        ):
            self.assertTrue(meta_analysis.is_mendelian_randomization_study({'Title': title}))

        self.assertFalse(meta_analysis.is_mendelian_randomization_study({
            'Title': 'Instrumental-variable analysis of food prices and tomato intake',
        }))


if __name__ == '__main__':
    unittest.main()
