import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
import meta_analysis
from app import app, get_scope_config


ROOT = Path(__file__).resolve().parents[1]


class MetaVirProstateScopeTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_scope_config_and_homepage_are_prostate_only(self):
        scopes = get_scope_config()
        self.assertEqual(list(scopes), ['prostate'])
        self.assertEqual(scopes['prostate']['disease'], 'Prostate cancer')
        self.assertEqual(scopes['prostate']['lifetime_risk_percent'], 13.2)
        self.assertEqual(scopes['prostate']['subcategories'][0]['key'], 'aggressive_prostate_cancer')
        self.assertIsNone(scopes['prostate']['subcategories'][0]['lifetime_risk_percent'])

        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('MetaVir: Prostate Cancer & Nutrition', html)
        self.assertIn('value="Prostate cancer"', html)
        self.assertIn('data-subcategory="aggressive_prostate_cancer"', html)
        self.assertIn('&nbsp;&nbsp;&nbsp;&nbsp;Aggressive prostate cancer', html)
        for unsupported in ('Breast cancer', 'Ovarian cancer', 'Uterine cancer'):
            self.assertNotIn(unsupported, html)

    def test_combined_default_and_explicit_chatgpt_evidence_views(self):
        for url in ['/', '/combined']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('data-evidence-track="combined"', html)
            self.assertIn('Combined evidence', html)
            self.assertNotIn('>Combined</a>', html)
        self.assertIn('data-evidence-track="chatgpt"', self.client.get('/chatgpt').get_data(as_text=True))

    def test_backend_rejects_unsupported_diseases_and_subcategories(self):
        unsupported = self.client.post('/analyze', json={
            'disease': 'Breast cancer',
            'exposure': 'Coffee',
        })
        self.assertEqual(unsupported.status_code, 400)
        self.assertIn('prostate cancer only', unsupported.get_json()['error'])

        subcategory = self.client.post('/analyze', json={
            'disease': 'Prostate cancer',
            'subcategory': 'legacy-subtype',
            'exposure': 'Coffee',
        })
        self.assertEqual(subcategory.status_code, 400)
        self.assertIn('not supported', subcategory.get_json()['error'])

        survival = self.client.post('/analyze', json={
            'disease': 'Prostate cancer',
            'exposure': 'Coffee',
            'outcome': 'Survival',
        })
        self.assertEqual(survival.status_code, 400)
        self.assertIn('incidence only', survival.get_json()['error'])

    def test_web_rejects_a_noncombined_cache(self):
        with tempfile.TemporaryDirectory() as tempdir, patch('app.CACHE_DIR', tempdir):
            (Path(tempdir) / '_merge_manifest.json').write_text(json.dumps({
                "schema_version": 1,
                "canonical_track": "combined",
                "build_id": "test-build",
            }))
            path = Path(app_module.get_cache_path(
                'Prostate cancer', 'Tomato', 'Incidence', True, False
            ))
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"canonical_track": "deepseek", "studies": []}))
            response = self.client.post('/analyze', json={
                'disease': 'Prostate cancer',
                'exposure': 'Tomato',
                'outcome': 'Incidence',
                'exclude_meta': True, 'evidence_track': 'combined',
            })
        self.assertEqual(response.status_code, 409)
        self.assertIn(
            'not part of the current adjudicated combined',
            response.get_json()['error'],
        )

    def test_web_accepts_a_combined_cache(self):
        with tempfile.TemporaryDirectory() as tempdir, patch('app.CACHE_DIR', tempdir):
            (Path(tempdir) / '_merge_manifest.json').write_text(json.dumps({
                "schema_version": 1,
                "canonical_track": "combined",
                "build_id": "test-build",
            }))
            path = Path(app_module.get_cache_path(
                'Prostate cancer', 'Tomato', 'Incidence', True, False
            ))
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "canonical_track": "combined",
                "combined_projection": {"build_id": "test-build"},
                "studies": [{"PMID": "123"}],
            }))
            response = self.client.post('/analyze', json={
                'disease': 'Prostate cancer',
                'exposure': 'Tomato',
                'outcome': 'Incidence',
                'exclude_meta': True, 'evidence_track': 'combined',
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['studies'][0]['PMID'], '123')

    def test_chatgpt_request_reads_only_saved_chatgpt_cache(self):
        with tempfile.TemporaryDirectory() as tempdir, patch('app.CHATGPT_CACHE_DIR', tempdir):
            path = Path(app_module.get_cache_path('Prostate cancer', 'Tomato', 'Incidence', True, False, cache_dir=tempdir))
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'canonical_track': 'chatgpt',
                'studies': [{'PMID': '123', 'Ethnicity': 'White', 'Gender': 'Male'}]}))
            response = self.client.post('/analyze', json={'disease': 'Prostate cancer',
                'exposure': 'Tomato', 'outcome': 'Incidence', 'exclude_meta': True, 'evidence_track': 'chatgpt'})
            config = self.client.get('/api/config?track=chatgpt').get_json()
            self.assertEqual(response.status_code, 200)
            result = response.get_json()
            self.assertEqual(result['evidence_track'], 'chatgpt')
            self.assertEqual(result['canonical_track'], 'chatgpt')
            self.assertEqual(result['studies'][0]['Ethnicity'], 'White')
            self.assertEqual(result['studies'][0]['Gender'], 'Male')
            self.assertEqual(config['canonical_track'], 'chatgpt')
            path.write_text(json.dumps({'canonical_track': 'combined', 'studies': []}))
            response = self.client.post('/analyze', json={'disease': 'Prostate cancer',
                'exposure': 'Tomato', 'outcome': 'Incidence', 'exclude_meta': True, 'evidence_track': 'chatgpt'})
            self.assertEqual(response.status_code, 409)

    def test_chatgpt_subexposures_use_saved_children_and_preserve_population(self):
        with tempfile.TemporaryDirectory() as tempdir, patch('app.CHATGPT_CACHE_DIR', tempdir), patch('app.DATA_DIR', tempdir):
            Path(tempdir,'exposure_hierarchy.json').write_text(json.dumps({'Child':['Parent']}))
            filename='prostate_cancer_incidence_true_core.json'
            child=Path(tempdir,'child',filename);child.parent.mkdir()
            saved={'canonical_track':'chatgpt','studies':[{'PMID':'2','Effect Type':'OR','Effect Size':1.5,
                'Lower CI':1.1,'Upper CI':2,'Quality Score':'Good','Sample Size':1000,'Ethnicity':'Black','Gender':'Male'}]}
            child.write_text(json.dumps(saved));before=child.read_bytes()
            parent={'canonical_track':'chatgpt','studies':[{'PMID':'1','Ethnicity':'White','Gender':'Male'}]}
            with app.test_request_context('/analyze',method='POST',json={'include_subexposures':'yes'}):
                yes=app_module.project_chatgpt_subexposures(json.loads(json.dumps(parent)),'Parent',filename)
            with app.test_request_context('/analyze',method='POST',json={'include_subexposures':'no'}):
                no=app_module.project_chatgpt_subexposures(json.loads(json.dumps(parent)),'Parent',filename)
            self.assertEqual({r['PMID'] for r in yes['studies']},{'1','2'})
            self.assertEqual({r['PMID'] for r in no['studies']},{'1'})
            self.assertEqual(yes['studies'][1]['Ethnicity'],'Black')
            self.assertEqual(yes['studies'][1]['Gender'],'Male')
            self.assertEqual(before,child.read_bytes())

    def test_web_rejects_a_stale_combined_build(self):
        with tempfile.TemporaryDirectory() as tempdir, patch('app.CACHE_DIR', tempdir):
            (Path(tempdir) / '_merge_manifest.json').write_text(json.dumps({
                "schema_version": 1,
                "canonical_track": "combined",
                "build_id": "current-build",
            }))
            path = Path(app_module.get_cache_path(
                'Prostate cancer', 'Tomato', 'Incidence', True, False
            ))
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "canonical_track": "combined",
                "combined_projection": {"build_id": "stale-build"},
                "studies": [{"PMID": "123"}],
            }))
            response = self.client.post('/analyze', json={
                'disease': 'Prostate cancer',
                'exposure': 'Tomato',
                'outcome': 'Incidence',
                'exclude_meta': True, 'evidence_track': 'combined',
            })
        self.assertEqual(response.status_code, 409)
        self.assertIn('current adjudicated combined', response.get_json()['error'])

    def test_reanalysis_defaults_to_prostate_scope(self):
        studies = [{
            'PMID': '1',
            'Quality Score': 'Good',
            'Effect Type': 'RR',
            'Effect Size': 1.1,
            'Lower CI': 1.0,
            'Upper CI': 1.2,
        }]
        with patch('app.meta_analysis.perform_meta_analysis', return_value={'headline': {}}) as perform:
            response = self.client.post('/reanalyze', json={'studies': studies})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(perform.call_args.args[1], 'Prostate cancer')

    def test_review_reporting_remains_available_for_prostate_evidence(self):
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
                    'disease': 'Prostate cancer',
                    'exposure': 'Coffee',
                    'outcome': 'Incidence',
                })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()['context_key'],
            'prostate_cancer_coffee_incidence',
        )
        self.assertFalse(response.get_json()['results_changed'])

    def test_frontend_uses_prostate_risk_and_blue_palette(self):
        script = (ROOT / 'static' / 'script.js').read_text(encoding='utf-8')
        style = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')
        template = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')

        self.assertIn('window.AnalysisSettings.p0()', script)
        self.assertIn('Math.round(totalN * studyBaselineRisk(study, selectedDiseaseValue(), selectedBaselineRisk()))', script)
        self.assertIn('id="pooled-incidence"', template)
        self.assertIn('--primary: #2563eb;', style)
        self.assertNotIn('#e91e63', style.lower())
        for legacy_color in ('#7a244f', '#d5b3c3', '#fffafa', '75, 22, 47', '#a0522d'):
            self.assertNotIn(legacy_color, (style + script + template).lower())

    def test_exposure_api_includes_additional_catalog_and_aliases(self):
        exposures = self.client.get('/api/exposures').get_json()
        synonyms = self.client.get('/api/synonyms').get_json()

        self.assertEqual(len(exposures), len(set(value.casefold() for value in exposures)))
        self.assertIn('PQQ', exposures)
        self.assertIn('methoxatin', synonyms['PQQ']['core'])
        self.assertIn("fetch('/api/exposures')", (ROOT / 'static' / 'script.js').read_text())

    def test_statistical_backstop_excludes_mr_but_not_nongenetic_iv_studies(self):
        for title in (
            'Two-sample MR analysis of lycopene and prostate cancer',
            'Genetically predicted tomato intake and prostate cancer risk',
            'Genetic variants as instrumental variables for lycopene exposure',
        ):
            self.assertTrue(meta_analysis.is_mendelian_randomization_study({'Title': title}))

        self.assertFalse(meta_analysis.is_mendelian_randomization_study({
            'Title': 'Instrumental-variable analysis of food prices and tomato intake',
        }))


if __name__ == '__main__':
    unittest.main()
