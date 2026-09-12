import copy
import unittest
from scripts import key_comparison_policy as comparison
from scripts import nonpoolable_status_retention as retention
from scripts import build_combined_results as builder
from scripts import monthly_evidence_update as monthly
from scripts import provisional_ui_overlay as provisional
from scripts import fair_quality_selection as quality
from scripts import nonpoolable_display_selection as selection
from scripts.display_eligibility import display_row


class NonpoolableRetentionTests(unittest.TestCase):
    def pair(self):
        base = {'context_id': '1|Vitamin D|Prostate cancer', 'pmid': '1', 'exposure': 'Vitamin D',
                'cancer': 'Prostate cancer', 'jbi': {'grade': 'Good'}, 'eligibility': 'INCLUDE',
                'final_status': 'ELIGIBLE_NOT_POOLABLE', 'authors': ['Example A'],
                'extraction': {'effect_type': 'RR', 'effect_size': .8, 'ci_lower': .6,
                               'ci_upper': 1., 'total_n': 1000}}
        excluded = dict(copy.deepcopy(base), eligibility='EXCLUDE', final_status='EXCLUDE')
        return excluded, base

    def test_both_orders_resolve_without_numeric_consensus_or_source_changes(self):
        a, b = self.pair()
        before = copy.deepcopy((a, b))
        for left, right in ((a, b), (b, a)):
            result = comparison.compare_context(left, right)
            self.assertFalse(result['has_key_discrepancy'])
            self.assertEqual(result['pairs'], [])
            record = retention.resolve_record(left, right)
            self.assertEqual(record['extraction'], b['extraction'])
            self.assertEqual(record['final_status'], 'ELIGIBLE_NOT_POOLABLE')
            self.assertFalse(record['scientific_pooling_eligible'])
        self.assertEqual((a, b), before)

    def test_eligibility_only_exclusion_is_the_same_pipeline_status(self):
        a, b = self.pair()
        a.pop('final_status')
        self.assertEqual(retention.resolve_record(a, b)['final_status'], 'ELIGIBLE_NOT_POOLABLE')

    def test_other_status_pairs_and_missing_counterparts_are_unchanged(self):
        a, b = self.pair()
        for other in (dict(b, final_status='INCLUDE_META'), None, dict(b, final_status='EXCLUDE')):
            self.assertIsNone(retention.resolve_record(a, other))
        self.assertTrue(comparison.compare_context(a, dict(b, final_status='INCLUDE_META'))['has_key_discrepancy'])

    def test_human_decisions_and_protected_contexts_remain_authoritative(self):
        a, b = self.pair()
        self.assertIsNone(retention.resolve_record(a, b, dict(b, manual_review={'decision': 'exclude'})))
        self.assertIsNone(retention.resolve_record(a, b, protected=True))
        self.assertIsNone(retention.resolve_record(a, dict(b, manual_full_text_adjudication={'done': True})))

    def test_quantitative_reclassification_and_all_quality_cannot_promote_it(self):
        a, b = self.pair()
        retained = retention.resolve_record(a, b)
        builder.reclassify_poolability({b['context_id']: retained}, monthly)
        self.assertEqual(retained['final_status'], 'ELIGIBLE_NOT_POOLABLE')
        row = monthly.study_row(retained)
        self.assertTrue(row['display_only'])
        self.assertFalse(row['scientific_pooling_eligible'])
        row['Quality Score'] = 'Fair'
        selected = quality.prepare_quality_opt_in_rows([row], 'All', allow_fair_quality=True)[0]
        self.assertFalse(selected.get('fair_quality_opted_in', False))

    def test_stale_provisional_review_cannot_restore_pooling(self):
        a, b = self.pair()
        context = b['context_id']
        previous = dict(b, final_status='INCLUDE_META')
        records, audit = provisional.project({context: previous},
            {'chatgpt': {context: a}, 'claude_gemini': {context: b}},
            {'rows': [{'id': 'test-1', 'source_row': {'Context ID': context}}]})
        self.assertEqual(records[context]['final_status'], 'ELIGIBLE_NOT_POOLABLE')
        self.assertFalse(audit[0]['pending'])
        self.assertFalse(audit[0]['effective_poolable'])

    def test_both_nonpoolable_uses_richer_intact_source_and_never_pools(self):
        _, rich = self.pair()
        poor = copy.deepcopy(rich)
        poor['extraction']['effect_size'] = None
        poor['jbi'] = {'grade': 'Good', 'answers': {'q1': 'Yes'}}
        rich['jbi'] = {'grade': 'Fair', 'answers': {'q1': 'No'}}
        before = copy.deepcopy((poor, rich))
        chosen = retention.resolve_record(poor, rich)
        self.assertEqual(chosen['nonpoolable_status_resolution']['retained_track'], 'claude_gemini')
        self.assertEqual(chosen['extraction'], rich['extraction'])
        self.assertEqual(chosen['jbi'], rich['jbi'])
        self.assertEqual(comparison.compare_context(poor, rich)['pairs'], [])
        row = display_row(chosen)
        self.assertEqual(row['Effect Size'], .8)
        self.assertIn('Both pipelines', row['display_only_reason'])
        self.assertFalse(row['scientific_pooling_eligible'])
        self.assertFalse(quality.prepare_quality_opt_in_rows([row], 'All', allow_fair_quality=True)[0].get('fair_quality_opted_in', False))
        builder.reclassify_poolability({chosen['context_id']: chosen}, monthly)
        self.assertEqual(chosen['final_status'], 'ELIGIBLE_NOT_POOLABLE')
        self.assertEqual((poor, rich), before)

    def test_equal_information_uses_chatgpt_despite_values_grade_or_verbose_audit(self):
        _, a = self.pair()
        b = copy.deepcopy(a)
        a['extraction']['effect_size'] = .7
        a['jbi']['grade'] = 'Fair'
        b['extraction']['effect_size'] = .9
        b['extraction']['raw_audit'] = 'detail ' * 1000
        chosen = retention.resolve_record(a, b)
        self.assertEqual(chosen['extraction'], a['extraction'])
        self.assertTrue(chosen['nonpoolable_status_resolution']['equal_information'])
        self.assertEqual(chosen['nonpoolable_status_resolution']['retained_track'], 'chatgpt')

    def test_missing_values_aliases_and_duplicate_geography_do_not_inflate_information(self):
        a = {'extraction': {'Sample Size': 100, 'country': 'USA', 'location': 'USA', 'effect_size': 'not reported in the abstract'}}
        b = {'extraction': {'total_n': 100, 'country': 'USA'}}
        self.assertEqual(selection.information_profile(a)['score'], selection.information_profile(b)['score'])
        self.assertEqual(selection.select_display_source(a, b)['retained_track'], 'chatgpt')

    def test_distinct_selected_source_estimates_remain_separate(self):
        _, a = self.pair()
        b = copy.deepcopy(a)
        b['additional_extractions'] = [{'effect_size': 1.2, 'total_n': 200}]
        chosen = retention.resolve_record(a, b)
        rows = [display_row(item) for item in retention.display_records(chosen)]
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['Effect Size'] for row in rows], [.8, 1.2])
        self.assertIsNone(rows[1]['Lower CI'])
        self.assertTrue(all(row['scientific_pooling_eligible'] is False for row in rows))

    def test_old_receipt_cannot_keep_wrong_display_choice(self):
        _, a = self.pair()
        b = copy.deepcopy(a)
        previous = retention.resolve_record(a, b)
        previous['extraction']['effect_size'] = 9
        previous['nonpoolable_status_resolution']['version'] = 'old'
        current = retention.resolve_record(a, b, previous)
        self.assertEqual(current['extraction'], a['extraction'])
        self.assertEqual(retention.resolve_record(a, b, current), current)


if __name__ == '__main__':
    unittest.main()
