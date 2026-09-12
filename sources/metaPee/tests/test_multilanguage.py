import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app


ROOT = Path(__file__).resolve().parents[1]


class MultiLanguagePageTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_summary_navigation_is_between_language_and_about(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        picker_position = html.index('data-language-picker')
        summary_position = html.index('href="/summary"', picker_position)
        about_position = html.index('href="/about"', picker_position)
        self.assertLess(picker_position, summary_position)
        self.assertLess(summary_position, about_position)

    def test_picker_offers_requested_languages_and_english_reset(self):
        response = self.client.get('/')
        html = response.get_data(as_text=True)

        self.assertIn('data-language="zh-CN"', html)
        self.assertIn('data-language="zh-TW"', html)
        self.assertIn('data-language="nl"', html)
        self.assertIn('data-language="ko"', html)
        self.assertIn('data-language="en"', html)
        self.assertIn('aria-label="Choose language"', html)

    def test_homepage_uses_current_subtitle_without_demo_banner(self):
        response = self.client.get('/')
        html = response.get_data(as_text=True)

        self.assertIn(
            'Explore associations between nutritional exposures and urinary system cancer incidence',
            html,
        )
        self.assertNotIn('Public Cached Demonstration Mode', html)
        self.assertNotIn('Live PubMed searches and LLM extraction are locked.', html)
        self.assertNotIn('id="read-only-banner"', html)

    def test_language_picker_and_engine_are_available_on_all_pages(self):
        for url in ('/', '/summary', '/about'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'data-language-picker', response.data)
            self.assertIn(b'i18n.js', response.data)

    def test_translation_catalog_is_complete_and_preserves_scientific_tokens(self):
        catalog = json.loads((ROOT / 'static' / 'i18n-translations.json').read_text())
        self.assertGreaterEqual(len(catalog), 200)
        self.assertEqual(list(catalog), sorted(catalog))

        for source, translations in catalog.items():
            self.assertEqual(set(translations), {'zh-CN', 'zh-TW', 'nl', 'ko'}, source)
            self.assertTrue(all(value.strip() for value in translations.values()), source)
            if 'MetaPee' in source:
                self.assertTrue(all('MetaPee' in value for value in translations.values()), source)
            for token in ('RR', 'OR', 'HR', 'ARR', 'CI', 'PI', 'I²', 'τ²', 'JBI', 'LLM', 'PubMed'):
                if re.search(rf'(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])', source):
                    self.assertTrue(
                        all(token in value for value in translations.values()),
                        f'{source}: {token}',
                    )

    def test_selector_options_and_requested_chinese_terms_are_translated(self):
        catalog = json.loads((ROOT / 'static' / 'i18n-translations.json').read_text())
        exposures = json.loads((ROOT / 'static' / 'exposures.json').read_text())

        self.assertEqual(len(exposures), 236)
        self.assertNotIn('Diet pills', exposures)
        self.assertNotIn('Diet pills', catalog)
        self.assertFalse([exposure for exposure in exposures if exposure not in catalog])
        self.assertEqual(catalog['Nutritional Exposure']['zh-CN'], '营养因子')
        self.assertEqual(catalog['Nutritional Exposure']['zh-TW'], '營養因子')
        self.assertEqual(catalog['Meta-Analysis']['zh-CN'], '荟萃分析')
        self.assertEqual(catalog['Meta-Analysis']['zh-TW'], '薈萃分析')
        self.assertEqual(catalog['Quality']['zh-CN'], '文章质量')
        self.assertEqual(catalog['Quality']['zh-TW'], '文章品質')

        translated_text = ' '.join(
            translation
            for translations in catalog.values()
            for locale, translation in translations.items()
            if locale in {'zh-CN', 'zh-TW'}
        )
        self.assertNotIn('营养暴露因素', translated_text)
        self.assertNotIn('營養暴露因素', translated_text)
        self.assertNotIn('營養暴露因子', translated_text)

        expected_disease_options = {
            'Bladder cancer',
            'Urothelial carcinoma of the bladder',
            'Squamous cell carcinoma of the bladder',
            'Adenocarcinoma of the bladder',
            'Small cell carcinoma of the bladder',
            'Kidney and renal pelvis cancer',
            'Renal cell carcinoma',
            'Clear cell renal cell carcinoma',
            'Papillary renal cell carcinoma',
            'Chromophobe renal cell carcinoma',
            'Urothelial carcinoma of the renal pelvis',
            'Renal sarcoma',
        }
        self.assertFalse(expected_disease_options - catalog.keys())

    def test_additional_v2_exposures_have_native_translations(self):
        catalog = json.loads((ROOT / 'static' / 'i18n-translations.json').read_text())
        additional_v2 = json.loads(
            (ROOT / 'data' / 'additionalExposureV2.json').read_text()
        )

        self.assertEqual(len(additional_v2), 29)
        self.assertNotIn('Diet pills', additional_v2)
        self.assertFalse([name for name in additional_v2 if name not in catalog])
        for name in additional_v2:
            self.assertEqual(
                set(catalog[name]),
                {'zh-CN', 'zh-TW', 'nl', 'ko'},
                name,
            )
            self.assertTrue(all(value.strip() for value in catalog[name].values()), name)

    def test_requested_exposures_have_curated_aliases_and_translations(self):
        catalog = json.loads((ROOT / 'static' / 'i18n-translations.json').read_text())
        synonyms = json.loads((ROOT / 'data' / 'synonyms_cache.json').read_text())
        expected = {
            'Sesame': 'Sesame, sesame seed, sesame seeds, Sesamum indicum, Sesamum indicum L., Sesamum orientale, gingelly, beniseed',
            'Fo-ti': 'Fo-ti, Fo ti, He Shou Wu, He-Shou-Wu, Heshouwu, Ho Shou Wu, Chinese fleeceflower, Chinese fleeceflower root, fleeceflower root, fleece-flower root, tuber fleeceflower, Polygoni Multiflori Radix, Polygonum multiflorum, Polygonum multiflorum Thunb., Fallopia multiflora, Reynoutria multiflora, Pleuropterus multiflorus',
            'Oleic acid': 'Oleic acid, cis-oleic acid, cis-9-octadecenoic acid, (9Z)-octadec-9-enoic acid, (Z)-octadec-9-enoic acid, oleinic acid, 18:1 n-9, C18:1 n-9',
            'Omega-9 fatty acids': 'Omega-9 fatty acids, Omega 9, omega-9, omega 9 fatty acid, omega 9 fatty acids, omega-9 fatty acid, omega-9 fatty acids, omega-9 monounsaturated fatty acid(s), omega-9 MUFA(s), n-9 fatty acid(s), n-9 MUFA(s), ω-9 fatty acid(s)',
            'Avocado': 'Avocado, avocados, avocado fruit, avocado pear, alligator pear, Persea americana, Persea americana Mill., Persea gratissima, aguacate, palta',
        }

        for exposure, aliases in expected.items():
            self.assertEqual(synonyms[exposure]['core'], aliases)
            self.assertIn(exposure, catalog)

    def test_public_assets_use_metapee_urinary_copy_and_orange_theme(self):
        style = (ROOT / 'static' / 'style.css').read_text()
        public_copy = '\n'.join(
            (ROOT / path).read_text()
            for path in (
                'templates/index.html',
                'templates/summary.html',
                'templates/about.html',
                'static/i18n-translations.json',
            )
        )

        self.assertIn('--primary: #c2410c;', style)
        self.assertIn('--primary-rgb: 194, 65, 12;', style)
        self.assertNotRegex(public_copy, re.compile(r'prostat', re.IGNORECASE))
        self.assertNotIn('MetaVir', public_copy)

    def test_localized_exposure_display_keeps_canonical_analysis_value(self):
        script = (ROOT / 'static' / 'script.js').read_text()

        self.assertIn('function selectedExposureValue()', script)
        self.assertIn('elements.exposure.dataset.canonicalExposure = canonical;', script)
        self.assertIn('elements.exposure.value = uiText(canonical);', script)
        self.assertIn('const exposure = selectedExposureValue();', script)
        self.assertNotIn('const exposure = elements.exposure.value;', script)

    def test_saved_result_inventory_filters_exposures_by_selected_disease(self):
        script = (ROOT / 'static' / 'script.js').read_text()

        self.assertIn('configData.cached_results_by_disease', script)
        self.assertIn('function refreshAvailableExposures()', script)
        self.assertIn(
            "elements.disease.addEventListener('change', refreshAvailableExposures)",
            script,
        )
        self.assertIn('available.has(exposure.toLowerCase())', script)

    def test_cross_exposure_summary_uses_urinary_system_scope(self):
        # Exercise the deterministic empty state independently of whether a
        # real screening run has already generated summary artifacts locally.
        with patch('app.load_summary_manifest', return_value={'scopes': {}}):
            response = self.client.get('/summary')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Urinary System Cancers Evidence Summary', response.data)
        self.assertIn(b'Summary plots are not available yet', response.data)
        self.assertEqual(
            self.client.get('/summary/plots/bladder_cancer/not-a-plot').status_code,
            404,
        )

    def test_dynamic_metadata_is_explicitly_excluded_from_translation(self):
        script = (ROOT / 'static' / 'script.js').read_text()

        self.assertIn('class="notranslate" translate="no"', script)
        self.assertRegex(script, r'class="notranslate" translate="no"[^>]*>\$\{study\.Journal')
        self.assertRegex(script, r'class="notranslate" translate="no"[^>]*>\$\{study\.Year')
        self.assertRegex(script, r'class="notranslate" translate="no"[^>]*>\$\{study\.Reference')
        self.assertIn('${study.exposure_measurement_supporting_text}', script)
        self.assertIn('class="notranslate" translate="no">"${study.exposure_measurement_supporting_text}"', script)

    def test_per_item_jbi_scores_remain_available_on_hover(self):
        script = (ROOT / 'static' / 'script.js').read_text()
        style = (ROOT / 'static' / 'style.css').read_text()

        self.assertIn('function itemizedJbiEntries(study)', script)
        self.assertIn("qualityBadge.addEventListener('mouseenter', showTooltip)", script)
        self.assertIn("qualityBadge.addEventListener('focus', showTooltip)", script)
        self.assertIn("uiText('Overall rating: {rating}'", script)
        self.assertNotIn('cursor: help', script)
        self.assertIn('.jbi-tooltip-list', style)

    def test_default_quality_filter_is_moderate_or_better(self):
        response = self.client.get('/')
        script = (ROOT / 'static' / 'script.js').read_text()
        self.assertIn(b'<option value="Moderate+" selected>Moderate and above</option>', response.data)
        self.assertNotIn(b'<option value="Fair+"', response.data)
        self.assertIn("return elements.filterQuality ? elements.filterQuality.value : 'Moderate+';", script)
        self.assertIn("return selectedQualityFilter() === 'All';", script)
        self.assertIn('const qualityIsEligible = matchesQualitySelection(study);', script)
        self.assertIn('fair_quality_opt_in_available !== true', script)
        self.assertIn('fair_quality_selection_overrides', script)
        self.assertIn('quality_filter: selectedQualityFilter()', script)
        self.assertIn('allow_fair_quality: fairQualityRequested()', script)

    def test_saved_result_case_filter_has_no_hidden_50_case_cutoff(self):
        response = self.client.get('/')
        script = (ROOT / 'static' / 'script.js').read_text()

        self.assertIn(b'id="filter-min-cases" value="0"', response.data)
        self.assertIn('finalCasesVal >= minCases', script)
        self.assertNotIn('finalCasesVal > minCases', script)

    def test_dynamic_statistical_interpretations_are_localized_and_rerendered(self):
        script = (ROOT / 'static' / 'script.js').read_text()

        self.assertIn('function renderHeadlineInterpretation(measure, es, low, upp)', script)
        self.assertIn('function renderResultsInterpretation(measure, es, low, upp)', script)
        self.assertIn('function renderFunnelInterpretation()', script)
        self.assertIn("uiText('Statistically significant ({direction})'", script)
        self.assertIn('I² = {i2}%', script)
        self.assertIn("Egger's test indicates no significant funnel plot asymmetry", script)
        self.assertIn("The significant funnel plot asymmetry (Egger's p={p})", script)
        self.assertIn('if (lastHeadlineData) applyResultMeasureTransformation();', script)
        self.assertIn('lastAnalysisContext ? lastAnalysisContext.exposure', script)
        self.assertIn('lastAnalysisContext ? lastAnalysisContext.disease', script)
        self.assertIn('updateResultsUI(data, analysisContext);', script)

        self.assertNotIn("elements.interpretation.classList.add('notranslate')", script)
        self.assertNotIn("elements.funnelInterpretation.classList.add('notranslate')", script)
        self.assertNotIn("elements.resultsInterpretation.classList.add('notranslate')", script)
        self.assertNotIn("elements.interpretation.setAttribute('translate', 'no')", script)
        self.assertNotIn("elements.funnelInterpretation.setAttribute('translate', 'no')", script)
        self.assertNotIn("elements.resultsInterpretation.setAttribute('translate', 'no')", script)

    def test_review_reporting_messages_are_translated(self):
        catalog = json.loads((ROOT / 'static' / 'i18n-translations.json').read_text())
        script = (ROOT / 'static' / 'script.js').read_text()
        messages = (
            'Article flag status is temporarily unavailable. The evidence results are still shown, but do not rely on the displayed flag counts.',
            'Flag this study for developer review? Two flags will email developers.',
            'Two matching submissions were saved and the review email is pending; results remain unchanged.',
            'The review request was saved and its email notification is pending. Results remain unchanged.',
            'Your flag was already recorded for this article.',
            'The verification was not saved: {message}',
            'The flag was not saved: {message}',
        )

        for message in messages:
            self.assertIn(message, catalog)
            self.assertEqual(set(catalog[message]), {'zh-CN', 'zh-TW', 'nl', 'ko'})
            self.assertIn(f"uiText('{message}'", script)

        self.assertNotIn(
            'Flag this study for developer review? Two flags will email developers but will not change the results.',
            script,
        )

    def test_brand_name_and_language_choice_persist(self):
        engine = (ROOT / 'static' / 'i18n.js').read_text()
        index = (ROOT / 'templates' / 'index.html').read_text()

        self.assertIn('<div class="logo">MetaPee</div>', index)
        self.assertIn("const STORAGE_KEY = 'metapee.language';", engine)
        self.assertIn("window.localStorage.setItem(STORAGE_KEY, locale)", engine)
        self.assertIn("window.localStorage.getItem(STORAGE_KEY)", engine)


if __name__ == '__main__':
    unittest.main()
