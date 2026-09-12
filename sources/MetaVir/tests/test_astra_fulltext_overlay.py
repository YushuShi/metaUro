import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from scripts import astra_fulltext_overlay as overlay
from scripts import astra_fulltext_common as common
from scripts import key_comparison_policy as policy

CID = '123|Milk|Breast cancer'
TEXT = 'PMID 123. PMC456. Among 1000 participants, adjusted RR was 1.20 (95% CI 0.90 to 1.50). The endpoint was incident breast cancer.'


def source(point=1.3, quality='Fair'):
    return {'context_id': CID, 'pmid': '123', 'exposure': 'Milk', 'cancer': 'Breast cancer',
            'eligibility': 'INCLUDE', 'final_status': 'ELIGIBLE_NOT_POOLABLE',
            'extraction': {'total_n': 1000, 'effect_type': 'RR', 'effect_size': point,
                           'ci_lower': .9, 'ci_upper': 1.5}, 'jbi': {'grade': quality}}


class FulltextOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.text = Path(self.temp.name) / 'PMC456.txt'
        self.text.write_text(TEXT)
        self.a, self.b = source(1.1), source(1.3, 'Good')
        self.previous = source(1.3)
        self.tracks = {'chatgpt': {CID: self.a}, 'claude_gemini': {CID: self.b}}

    def tearDown(self):
        self.temp.cleanup()

    def entry(self, pair=0, eligibility='INCLUDE', rid=None):
        current = policy.compare_context(self.a, self.b)['pairs'][pair]
        row = {'Context ID': CID, 'PMID': '123', 'Estimate pair': str(pair + 1)}
        for track, prefix in [('chatgpt', 'ChatGPT'), ('claude_gemini', 'Claude+Gemini')]:
            row[prefix + ' estimate index'] = current[track + '_index']
            for key, label in [('total_n', 'sample size'), ('effect_size', 'effect size'), ('ci_lower', 'CI lower'), ('ci_upper', 'CI upper')]:
                row[prefix + ' ' + label] = (current[track] or {}).get(key)
        rid = rid or f'review-{pair + 1}'
        return {'id': rid, 'source_row': row, 'source_fingerprint': common.source_digest(self.a, self.b),
                'adjudication': {'id': rid, 'confidence': 'high', 'verdict': 'CORRECTED', 'eligibility': eligibility,
                    'source_quote_validated': True, 'source': {'pmid': '123', 'pmcid': 'PMC456',
                    'text_path': str(self.text), 'text_sha256': hashlib.sha256(self.text.read_bytes()).hexdigest(),
                    'url': 'https://pmc.ncbi.nlm.nih.gov/articles/PMC456/'},
                    'evidence': [{'quote': TEXT, 'fields': ['total_n', 'effect_size', 'ci_lower', 'ci_upper']}],
                    'extraction': {'total_n': 1000, 'effect_type': 'aRR', 'effect_size': 1.2, 'ci_lower': .9, 'ci_upper': 1.5},
                    'reason': 'The full text reports the adjusted incidence estimate.'}}

    def apply(self, entries=None, protected=()):
        return overlay.apply_entries({CID: self.previous}, self.tracks, entries or [self.entry()], protected)

    def test_corrected_atomic_bundle_keeps_fair_and_sources(self):
        before = copy.deepcopy((self.previous, self.tracks))
        result, audit = self.apply()
        row = result[CID]
        self.assertTrue(audit[0]['applied'])
        self.assertEqual(row['extraction']['effect_size'], 1.2)
        self.assertEqual(row['extraction']['effect_type'], 'RR')
        self.assertEqual(row['jbi']['grade'], 'Fair')
        self.assertEqual(row['final_status'], 'ELIGIBLE_NOT_POOLABLE')
        self.assertEqual((self.previous, self.tracks), before)

    def test_previous_abstract_partial_can_gain_total_n(self):
        self.previous['extraction']['total_n'] = None
        self.previous['astra_quantitative_follow_up_required'] = True
        self.previous['astra_preserved_pooling_hold'] = True
        self.previous['astra_adjudication'] = {'source_scope': 'TITLE_ABSTRACT_ONLY'}
        result, audit = self.apply()
        self.assertTrue(audit[0]['applied'])
        self.assertEqual(result[CID]['extraction']['total_n'], 1000)
        self.assertFalse(result[CID]['astra_quantitative_follow_up_required'])
        self.assertFalse(result[CID]['astra_preserved_pooling_hold'])
        self.assertIn('astra_adjudication', result[CID])

    def test_uncertain_or_changed_text_preserves_result(self):
        item = self.entry()
        item['adjudication']['confidence'] = 'medium'
        result, audit = self.apply([item])
        self.assertEqual(result[CID], self.previous)
        self.assertFalse(audit[0]['applied'])
        item = self.entry()
        self.text.write_text(TEXT + ' changed')
        result, audit = self.apply([item])
        self.assertEqual(result[CID], self.previous)
        self.assertIn('SHA256', audit[0]['reason'])

    def test_pdf_and_wrong_quote_are_rejected(self):
        item = self.entry()
        item['adjudication']['source']['text_path'] = str(self.text.with_suffix('.pdf'))
        self.assertFalse(self.apply([item])[1][0]['applied'])
        item = self.entry()
        item['adjudication']['evidence'][0]['quote'] = 'Invented result'
        self.assertFalse(self.apply([item])[1][0]['applied'])

    def test_manual_authority_and_source_fingerprint_protected(self):
        self.previous['decision_authority'] = 'human_manual_review'
        result, audit = self.apply()
        self.assertEqual(result[CID], self.previous)
        self.assertFalse(audit[0]['follow_up_required'])
        self.previous.pop('decision_authority')
        item = self.entry()
        self.a['extraction']['total_n'] = 999
        self.assertFalse(self.apply([item])[1][0]['applied'])

    def test_source_clinical_hold_survives_complete_correction(self):
        self.previous['manual_review_hold'] = True
        result, audit = self.apply()
        self.assertTrue(audit[0]['applied'])
        self.assertTrue(result[CID]['astra_preserved_pooling_hold'])

    def test_nested_same_pair_gates_and_overlap_metadata_survive(self):
        self.a['extraction'].update(manual_poolable=False, included_in_parent_meta_analysis=False,
                                  full_text_pooling_block=True, manual_overlap_role='companion', overlap_group='shared-cohort')
        result, audit = self.apply()
        self.assertTrue(audit[0]['applied'])
        extraction = result[CID]['extraction']
        self.assertFalse(extraction['manual_poolable'])
        self.assertFalse(extraction['included_in_parent_meta_analysis'])
        self.assertTrue(extraction['full_text_pooling_block'])
        self.assertEqual(extraction['manual_overlap_role'], 'companion')
        self.assertEqual(extraction['overlap_group'], 'shared-cohort')
        self.assertTrue(result[CID]['astra_preserved_pooling_hold'])

    def test_numeric_only_nested_hold_is_not_retained_after_completion(self):
        self.a['extraction'].update(manual_pooling_eligible=False, quantitative_complete=False)
        result, audit = self.apply()
        self.assertTrue(audit[0]['applied'])
        self.assertFalse(result[CID]['astra_preserved_pooling_hold'])
        self.assertNotIn('manual_pooling_eligible', result[CID]['extraction'])

    def test_partial_atomic_bundle_does_not_fill_n_from_models(self):
        item = self.entry()
        item['adjudication']['extraction']['total_n'] = None
        result, audit = self.apply([item])
        self.assertTrue(audit[0]['applied'])
        self.assertIsNone(result[CID]['extraction']['total_n'])
        self.assertTrue(audit[0]['follow_up_required'])
        self.assertTrue(result[CID]['astra_preserved_pooling_hold'])

    def test_unrelated_additional_estimate_is_preserved(self):
        extra = {'total_n': 700, 'effect_type': 'OR', 'effect_size': .5, 'ci_lower': .4, 'ci_upper': .6, 'stratum': 'men'}
        for record in [self.a, self.b, self.previous]:
            record['additional_extractions'] = [copy.deepcopy(extra)]
        result, audit = self.apply()
        self.assertTrue(audit[0]['applied'])
        self.assertEqual(result[CID]['additional_extractions'][0], policy.normalise_estimate(extra))
        self.assertEqual(result[CID]['extraction']['effect_size'], 1.2)

    def test_duplicate_pair_fails_closed(self):
        item = self.entry()
        result, audit = self.apply([item, copy.deepcopy(item)])
        self.assertEqual(result[CID], self.previous)
        self.assertEqual(sum(x['applied'] for x in audit), 0)

    def test_clear_exclusion_is_saved_without_extracting_numbers(self):
        item = self.entry(eligibility='EXCLUDE')
        item['adjudication'].pop('extraction')
        result, audit = self.apply([item])
        self.assertTrue(audit[0]['applied'])
        self.assertEqual(result[CID]['final_status'], 'EXCLUDE')
        self.assertIsNone(result[CID]['extraction'])

    def test_excluding_last_saved_pair_does_not_clear_another_unresolved_pair(self):
        for record in [self.a, self.b]:
            record['additional_extractions'] = [{'total_n': 700, 'effect_type': 'OR',
                'effect_size': .5 if record is self.a else .6, 'ci_lower': .4, 'ci_upper': .8}]
        self.previous['key_comparison_pending'] = True
        result, audit = self.apply([self.entry(eligibility='EXCLUDE')])
        self.assertEqual(result[CID], self.previous)
        self.assertFalse(audit[0]['applied'])
        self.assertIn('another unresolved', audit[0]['reason'])

    def test_durable_replay_keeps_fulltext_after_abstract_baseline_rebuild(self):
        item = self.entry()
        result, audit = self.apply([item])
        checkpoint = overlay.snapshot_checkpoint(result, self.tracks, [item], audit)
        root = Path(self.temp.name) / 'combined'
        path = root / overlay.CHECKPOINT_NAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(checkpoint))
        replay, events = overlay.apply_checkpoint({CID: self.previous}, self.tracks, root)
        self.assertEqual(replay[CID], result[CID])
        self.assertTrue(events[0]['applied'])
        self.a['extraction']['total_n'] = 999
        replay, events = overlay.apply_checkpoint({CID: self.previous}, self.tracks, root)
        self.assertEqual(replay[CID], self.previous)
        self.assertFalse(events[0]['applied'])

    def test_replay_respects_new_manual_decision(self):
        item = self.entry()
        result, audit = self.apply([item])
        checkpoint = overlay.snapshot_checkpoint(result, self.tracks, [item], audit)
        root = Path(self.temp.name) / 'combined'
        path = root / overlay.CHECKPOINT_NAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(checkpoint))
        self.previous['human_adjudication'] = {'reviewed': True}
        replay, events = overlay.apply_checkpoint({CID: self.previous}, self.tracks, root)
        self.assertEqual(replay[CID], self.previous)
        self.assertFalse(events[0]['applied'])

    def test_packet_requires_complete_exact_astra_pmc_scope(self):
        with self.assertRaises(ValueError):
            common.packet_entries({'model': 'gpt-6-astra', 'source_scope': 'TITLE_ABSTRACT_ONLY', 'complete': True, 'results': []})
        with self.assertRaises(ValueError):
            common.packet_entries({'model': 'gpt-6-astra', 'source_scope': overlay.SOURCE_SCOPE, 'complete': False, 'results': []})


if __name__ == '__main__':
    unittest.main()
