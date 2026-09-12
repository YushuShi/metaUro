import copy
import unittest
from tests import test_astra_fulltext as fixtures
from scripts import build_provisional_review_ui as provisional_ui
from scripts import reconcile_screening_tracks as reconcile
from scripts import astra_fulltext_overlay as fulltext


class ProvisionalReviewTests(unittest.TestCase):
    setUp = fixtures.FulltextTests.setUp
    tearDown = fixtures.FulltextTests.tearDown
    entry = fixtures.FulltextTests.entry

    def pending(self, applied=False):
        item = self.entry()
        for record, prefix in [(self.a, 'ChatGPT'), (self.b, 'Claude+Gemini')]:
            if record.get('eligibility') == 'EXCLUDE' or record.get('final_status') == 'EXCLUDE':
                item['source_row'][prefix + ' estimate index'] = ''
        return {'id': item['id'], 'source_row': item['source_row'],
                'manual_review_required': True,
                'application': {'applied': True} if applied else None}

    def current(self):
        return reconcile.reconcile_context(self.a, self.b)[0]

    def test_one_complete_eligible_uses_atomic_bundle_and_higher_jbi(self):
        self.a['jbi'] = {'grade': 'Fair', 'answers': {'q1': 'Unclear'}}
        self.b.update(eligibility='EXCLUDE', final_status='EXCLUDE')
        self.b['jbi'] = {'grade': 'Good', 'answers': {'q1': 'Yes'}}
        prior = self.current(); original = copy.deepcopy(self.tracks)
        _, items, audit = provisional_ui.apply_context(prior, self.tracks, [self.pending()])
        self.assertEqual(len(items), 1)
        row = reconcile.app_row(items[0])
        self.assertEqual(row['combined_source'], 'chatgpt')
        self.assertEqual(row['Sample Size'], 1000)
        self.assertEqual(row['Quality Score'], 'Good')
        self.assertTrue(row['policy_poolable'])
        self.assertTrue(row['manual_review_pending'])
        self.assertEqual(self.tracks, original)
        self.assertTrue(audit[0]['pending'])

    def test_both_complete_defaults_to_chatgpt_despite_numeric_conflict(self):
        self.a['jbi']['grade'] = 'Fair'; self.b['jbi']['grade'] = 'Good'
        _, items, _ = provisional_ui.apply_context(self.current(), self.tracks, [self.pending()])
        self.assertEqual(len(items), 1)
        row = reconcile.app_row(items[0])
        self.assertEqual(row['Sample Size'], 1000)
        self.assertEqual(row['combined_source'], 'chatgpt')
        self.assertTrue(row['policy_poolable'])

    def test_temporary_source_choice_does_not_modify_applied_astra_partial(self):
        entry = self.entry(); entry['adjudication']['extraction']['total_n'] = None
        prior, _, _ = fulltext.apply_context(self.current(), self.tracks, [entry])
        snapshot = copy.deepcopy(prior)
        result, items, _ = provisional_ui.apply_context(prior, self.tracks, [self.pending(applied=True)])
        self.assertEqual(prior, snapshot)
        self.assertEqual(result['extraction'], snapshot['extraction'])
        self.assertIsNone(result['extraction']['total_n'])
        self.assertEqual(items[0]['extraction']['total_n'], 1000)
        self.assertTrue(items[0]['provisional_ui_selection']['overrides_pending_astra_partial'])

    def test_incomplete_baseline_is_preserved_when_neither_source_is_complete(self):
        entry = self.entry(); entry['adjudication']['extraction']['total_n'] = None
        prior, _, _ = fulltext.apply_context(self.current(), self.tracks, [entry])
        self.a['extraction']['total_n'] = self.b['extraction']['total_n'] = None
        _, items, _ = provisional_ui.apply_context(prior, self.tracks, [self.pending(applied=True)])
        self.assertEqual(items[0]['extraction']['effect_size'], .8)
        self.assertIsNone(items[0]['extraction']['total_n'])
        self.assertFalse(reconcile.app_row(items[0])['policy_poolable'])

    def test_independent_scientific_restriction_is_retained(self):
        self.a['exposure_classification_ambiguous'] = True
        self.b.update(eligibility='EXCLUDE', final_status='EXCLUDE')
        self.b['jbi']['grade'] = 'Good'
        _, items, _ = provisional_ui.apply_context(self.current(), self.tracks, [self.pending()])
        self.assertTrue(items[0]['provisional_ui_selection']['selection_poolable_candidate'])
        self.assertFalse(reconcile.app_row(items[0])['policy_poolable'])

    def test_independent_pending_scientific_review_keeps_chosen_source_jbi(self):
        for record in (self.a, self.b):
            record['extraction'] = {'total_n': None, 'effect_size': None, 'ci_lower': None, 'ci_upper': None}
            record['final_status'] = 'ELIGIBLE_NOT_POOLABLE'
        self.a['jbi']['grade'] = 'Fair'; self.b['jbi']['grade'] = 'Moderate'
        pending = self.pending(); pending['remaining_category'] = 'Independent scientific/manual adjudication'
        _, items, _ = provisional_ui.apply_context(self.current(), self.tracks, [pending])
        self.assertTrue(items)
        rows = [reconcile.app_row(item) for item in items]
        self.assertTrue(all(row['Quality Score'] == 'Fair' for row in rows))
        self.assertTrue(all(not row['policy_poolable'] and row['manual_review_pending'] for row in rows))

    def test_human_context_is_preserved(self):
        prior = self.current(); prior['combined_manual_authority'] = True
        result, _, _ = provisional_ui.apply_context(prior, self.tracks, [self.pending()])
        self.assertEqual(result, prior)

    def test_unrelated_accepted_pair_remains_unchanged(self):
        extra = {**self.a['extraction'], 'total_n': 4000, 'effect_size': 2.0, 'ci_lower': 1.8, 'ci_upper': 2.2}
        self.a['additional_extractions'] = [copy.deepcopy(extra)]
        self.b['additional_extractions'] = [copy.deepcopy(extra)]
        prior = self.current(); before = fulltext.candidates_for_record(prior, self.tracks)
        _, items, _ = provisional_ui.apply_context(prior, self.tracks, [self.pending()])
        old = next(item for item in before if item['extraction']['total_n'] == 4000)
        new = next(item for item in items if item['extraction']['total_n'] == 4000)
        self.assertEqual(new, old)

    def test_stale_source_number_is_rejected_before_selection(self):
        entry = self.pending(); entry['source_row']['ChatGPT sample size'] = '999'
        with self.assertRaisesRegex(ValueError, 'Source numeric bundle changed'):
            provisional_ui.apply_context(self.current(), self.tracks, [entry])


if __name__ == '__main__':
    unittest.main()
