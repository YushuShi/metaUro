import json
import re
import unittest
from pathlib import Path

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
        summary_position = html.index('href="/summary?', picker_position)
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
            'Explore associations between nutritional exposures and prostate cancer incidence',
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

        for source, translations in catalog.items():
            self.assertEqual(set(translations), {'zh-CN', 'zh-TW', 'nl', 'ko'}, source)
            self.assertTrue(all(value.strip() for value in translations.values()), source)
            if 'MetaVir' in source:
                self.assertTrue(all('MetaVir' in value for value in translations.values()), source)
            for token in ('RR', 'OR', 'HR', 'ARR', 'CI', 'PI', 'I²', 'τ²', 'JBI', 'LLM', 'PubMed'):
                if re.search(rf'(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])', source):
                    self.assertTrue(
                        all(token in value for value in translations.values()),
                        f'{source}: {token}',
                    )

    def test_selector_options_and_requested_chinese_terms_are_translated(self):
        catalog = json.loads((ROOT / 'static' / 'i18n-translations.json').read_text())
        exposures = json.loads((ROOT / 'static' / 'exposures.json').read_text())

        self.assertEqual(len(exposures), 231)
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

        expected_disease_options = {'Prostate cancer', 'Aggressive prostate cancer'}
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

    def test_fish_oil_and_vitamin_k_search_vocabularies_are_separate(self):
        exposures = json.loads((ROOT / 'static' / 'exposures.json').read_text())
        synonyms = json.loads((ROOT / 'data' / 'synonyms_cache.json').read_text())

        self.assertTrue({'Vitamin K1', 'Vitamin K2', 'Vitamin K3'} <= set(exposures))
        self.assertIn('phylloquinone', synonyms['Vitamin K1']['core'])
        self.assertIn('menaquinone-14', synonyms['Vitamin K2']['core'])
        self.assertIn('MK-14', synonyms['Vitamin K2']['downstream'])
        self.assertIn('menadione sodium bisulfite', synonyms['Vitamin K3']['core'])

        fish_oil_terms = synonyms['Fish oil']['core'].casefold()
        for omega_3_term in (
            'dha', 'epa', 'docosahexaenoic', 'eicosapentaenoic',
            'omega-3 index', 'plasma',
        ):
            self.assertNotIn(omega_3_term, fish_oil_terms)

        response = self.client.get('/api/synonyms')
        self.assertEqual(response.status_code, 200)
        k3_core = response.get_json()['Vitamin K3']['core']
        self.assertIsInstance(k3_core, str)
        self.assertIn('2-methyl-1,4-naphthoquinone', k3_core)

    def test_v2_additional_catalog_is_active_in_ui(self):
        response = self.client.get('/api/exposures')
        self.assertEqual(response.status_code, 200)
        exposures = response.get_json()
        self.assertIn('DASH diet', exposures)
        self.assertNotIn('Diet pills', exposures)

        response = self.client.get('/api/synonyms')
        self.assertEqual(response.status_code, 200)
        synonyms = response.get_json()
        self.assertIn(
            'Dietary Approaches to Stop Hypertension',
            synonyms['DASH diet']['core'],
        )

    def test_localized_exposure_display_keeps_canonical_analysis_value(self):
        script = (ROOT / 'static' / 'script.js').read_text()

        self.assertIn('function selectedExposureValue()', script)
        self.assertIn('elements.exposure.dataset.canonicalExposure = canonical;', script)
        self.assertIn('elements.exposure.value = uiText(canonical);', script)
        self.assertIn('const exposure = selectedExposureValue();', script)
        self.assertNotIn('const exposure = elements.exposure.value;', script)

    def test_cross_exposure_summary_serves_current_plots_and_workbook(self):
        response = self.client.get('/summary')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Prostate Cancer Evidence Summary', response.data)
        self.assertIn(b'Heterogeneity assessment', response.data)
        plot_response = self.client.get('/summary/plots/prostate_cancer/forest-protective')
        self.assertEqual(plot_response.status_code, 200)
        plot_response.close()
        workbook_response = self.client.get('/summary/workbooks/prostate_cancer/combined')
        self.assertEqual(workbook_response.status_code, 200)
        workbook_response.close()
        self.assertEqual(
            self.client.get('/summary/plots/prostate_cancer/not-a-plot').status_code,
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

    def test_default_quality_display_includes_fair_and_pooling_requires_moderate(self):
        response = self.client.get('/')
        script = (ROOT / 'static' / 'script.js').read_text()

        self.assertIn(b'<option value="Moderate+" selected>Moderate and above</option>', response.data)
        self.assertIn("const quality = elements.filterQuality ? elements.filterQuality.value : 'Moderate+';", script)
        self.assertIn('const qualityIsEligible = meetsSelectedJbiThreshold(study);', script)
        self.assertIn('qualityIsEligible && hasRequiredSampleSize && eligibleEffectMeasurement', script)
        self.assertIn("displayedAnalysis = data.subexposure_projection && selectedPoolingQuality() === 'Moderate+' ? data : await requestReanalysis(selectedStudies, analysisContext);", script)
        self.assertIn("quality_filter: selectedPoolingQuality()", script)

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
        self.assertIn('updateResultsUI(displayedAnalysis, analysisContext);', script)

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

        self.assertIn('<div class="logo">MetaVir</div>', index)
        self.assertIn("const STORAGE_KEY = 'metavir.language';", engine)
        self.assertIn("window.localStorage.setItem(STORAGE_KEY, locale)", engine)
        self.assertIn("window.localStorage.getItem(STORAGE_KEY)", engine)


if __name__ == '__main__':
    unittest.main()
