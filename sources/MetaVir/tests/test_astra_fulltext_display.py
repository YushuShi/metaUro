import copy
import unittest
from tests import test_astra_fulltext_overlay as fixtures
from scripts import astra_fulltext_common as common


class FulltextDisplayTests(unittest.TestCase):
    setUp = fixtures.FulltextOverlayTests.setUp
    tearDown = fixtures.FulltextOverlayTests.tearDown
    entry = fixtures.FulltextOverlayTests.entry

    def test_pmc_evidence_is_visible_for_fair_and_good_studies(self):
        from scripts import monthly_evidence_update as monthly
        extraction, error = common.validate_entry(self.entry(), self.a, self.b)
        self.assertIsNone(error)
        for quality in ('Fair', 'Good'):
            with self.subTest(quality=quality):
                record = copy.deepcopy(self.previous)
                record['cancer'] = 'Prostate cancer'
                record['jbi']['grade'] = quality
                record['extraction'] = extraction
                record['final_status'] = 'ELIGIBLE_NOT_POOLABLE' if quality == 'Fair' else 'INCLUDE_META'
                row = monthly.study_row(record)
                self.assertEqual(row['PMCID'], 'PMC456')
                self.assertEqual(row['evidence_source_scope'], common.SOURCE_SCOPE)
                self.assertEqual(row['extraction_evidence']['sample_size']['source'], 'pmc_full_text')
                self.assertTrue(row['extraction_supporting_text']['sample_size'])
                self.assertTrue(row['extraction_supporting_text']['confidence_interval'])
                self.assertIn('PMC456', row['evidence_excerpt_source'])
                if quality == 'Fair':
                    self.assertFalse(row['policy_poolable'])

    def test_explicit_reverse_direction_survives_poolability_and_display(self):
        from scripts import monthly_evidence_update as monthly
        from scripts import dual_screening
        for direction in ('no_vs_yes', 'per_unit_decrease'):
            item = self.entry()
            item['adjudication']['extraction']['comparison_direction'] = direction
            extraction, error = common.validate_entry(item, self.a, self.b)
            self.assertIsNone(error)
            normalized = dual_screening.apply_poolability_policy(copy.deepcopy(extraction))
            self.assertTrue(normalized['needs_inversion'])
            self.assertFalse(normalized['direction_imputed'])
            for quality in ('Fair', 'Good'):
                record = copy.deepcopy(self.previous)
                record.update(cancer='Prostate cancer', extraction=normalized)
                record['jbi']['grade'] = quality
                record['final_status'] = 'ELIGIBLE_NOT_POOLABLE' if quality == 'Fair' else 'INCLUDE_META'
                row = monthly.study_row(record)
                self.assertAlmostEqual(row['Effect Size'], 1 / 1.2)
                self.assertAlmostEqual(row['Lower CI'], 1 / 1.5)

    def test_good_partial_fulltext_study_remains_visible_and_unpooled(self):
        from scripts import monthly_evidence_update as monthly
        from scripts import build_combined_results as builder
        extraction, error = common.validate_entry(self.entry(), self.a, self.b)
        self.assertIsNone(error)
        extraction.update(ci_lower=None, ci_upper=None, p_value=None)
        record = copy.deepcopy(self.previous)
        record.update(cancer='Prostate cancer', extraction=extraction,
                      astra_fulltext_adjudication={'version': common.VERSION},
                      astra_quantitative_follow_up_required=True,
                      final_status='ELIGIBLE_NOT_POOLABLE')
        record['jbi']['grade'] = 'Good'
        grouped, _, _ = builder.build_grouped_studies({fixtures.CID: record}, {}, {fixtures.CID}, monthly)
        self.assertTrue(grouped)
        for rows in grouped.values():
            for row in rows:
                self.assertEqual(row['Quality Score'], 'Good')
                self.assertTrue(row['display_only'])
                self.assertFalse(row['policy_poolable'])
                self.assertIsNone(row['Lower CI'])

        # This exposure need not already have a base cache. Its first visible
        # study may be a supported partial result with no poolable estimate.
        selected = {fixtures.CID: {'selected_source': 'verified-original-month'}}
        for exposure in grouped:
            self.assertEqual(builder.selected_source_for_new_scope(
                exposure, {fixtures.CID: record}, selected, monthly),
                'verified-original-month')


if __name__ == '__main__':
    unittest.main()
