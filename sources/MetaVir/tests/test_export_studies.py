import json
import unittest
from pathlib import Path

from app import app


ROOT = Path(__file__).resolve().parents[1]


class ExportStudiesTests(unittest.TestCase):
    def test_export_button_is_immediately_after_update_analysis(self):
        html = app.test_client().get('/').get_data(as_text=True)

        deselect_position = html.index('id="deselect-all-btn"')
        update_position = html.index('id="update-btn"')
        export_position = html.index('id="export-studies-btn"')
        self.assertLess(deselect_position, update_position)
        self.assertLess(update_position, export_position)
        self.assertIn('background-color: var(--primary);">Export</button>', html)

    def test_csv_export_uses_displayed_studies_and_identifiers(self):
        script = (ROOT / 'static' / 'script.js').read_text(encoding='utf-8')

        self.assertIn('function exportStudiesToCsv()', script)
        self.assertIn('const rows = currentStudies.map((study, index)', script)
        self.assertIn("studyIdentifier(study, 'PMID')", script)
        self.assertIn("studyIdentifier(study, 'PMCID')", script)
        self.assertIn("study.Ethnicity || study.ethnicity || 'Mixed'", script)
        self.assertIn('metavir_extracted_studies', script)
        self.assertIn("new Blob([`\\uFEFF${csv}`]", script)
        self.assertIn("type: 'text/csv;charset=utf-8'", script)

    def test_export_ui_and_csv_headers_are_translated(self):
        catalog = json.loads((ROOT / 'static' / 'i18n-translations.json').read_text(encoding='utf-8'))

        for source in ('Export', 'No studies to export.', 'Row', 'Selected', 'Study',
                       'Effect Type', 'Effect Size', 'Sample Size', 'Quality Score',
                       'Ethnicity', 'Mixed'):
            self.assertEqual(set(catalog[source]), {'zh-CN', 'zh-TW', 'nl', 'ko'})
        self.assertEqual(catalog['Export']['zh-CN'], '导出')
        self.assertEqual(catalog['Export']['zh-TW'], '匯出')
        self.assertEqual(catalog['Export']['ko'], '내보내기')


if __name__ == '__main__':
    unittest.main()
