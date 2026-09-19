"""Endpoint hierarchy, cohort denominators, and reproducible saved projections."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import aggressive_prostate_policy as policy
from scripts import build_aggressive_prostate_results as builder


def study(row_id='one', **changes):
    row = {'PMID': '123', 'Effect ID': row_id, 'Study': row_id,
           'Direct Exposure': 'Tomato', 'Sample Size': 1000, 'Cases': 100,
           'Estimated Cases': 132, 'Effect Type': 'RR', 'Effect Size': 1.5,
           'Lower CI': 1.1, 'Upper CI': 2.0, 'Quality Score': 'Good',
           'comparison_type': 'Highest versus lowest', 'Primary Estimate': True,
           'effect_is_adjusted': True, 'pool_in_parent_meta_analysis': True}
    row.update(changes)
    return row


def additional(endpoint='aggressive', **changes):
    result = {'exposure_key': 'tomato', 'endpoint': endpoint, 'effect_type': 'HR',
              'effect_size': 1.8, 'lower_ci': 1.2, 'upper_ci': 2.5,
              'sample_size': 800, 'cases': 40, 'comparison': 'Highest versus lowest',
              'effect_excerpt': 'The aggressive cancer hazard ratio was 1.8 (1.2–2.5).',
              'count_excerpt': '800 participants; 40 aggressive cases.',
              'endpoint_definition': 'Advanced or high-grade prostate cancer'}
    result.update(changes)
    return result


def review(classifications, **changes):
    result = {'article_classification': 'both',
              'aggressive_definition': 'Advanced or high-grade cancer',
              'aggressive_definition_excerpt': 'We analyzed advanced or high-grade cancer.',
              'prospective_cohort': False, 'cohort_total_n': None,
              'cohort_total_n_excerpt': '', 'additional_estimates': [],
              'estimate_classifications': [
                  {'row_id': rid, 'endpoint': endpoint, 'supported_by_abstract': True,
                   'endpoint_support_source': 'abstract', 'endpoint_excerpt': endpoint,
                   'reason': 'The abstract identifies this endpoint.'}
                  for rid, endpoint in classifications.items()]}
    result.update(changes)
    return result


def project(rows, adjudication):
    return policy.project(rows, 'Tomato', {'123': adjudication}, lambda row: row['Effect ID'])


class AggressiveProstatePolicyTests(unittest.TestCase):
    def test_aggressive_case_count_uses_endpoint_review_without_changing_cohort_n(self):
        row = study(Cases=300, **{'Sample Size': 47855})
        adjudication = review({'one': 'aggressive'})
        parent, child, _ = policy.project([row], 'Tomato', {'123': adjudication},
            lambda r: r['Effect ID'], case_reviews={'one': {
                'status': 'confirmed', 'endpoint_cases': 126,
                'excerpt': '300 cases, including 126 advanced cases', 'source': 'abstract'}})
        for result in parent + child:
            self.assertEqual(result['Cases'], 126)
            self.assertEqual(result['Sample Size'], 47855)
            self.assertEqual(result['disease_scope_original_row']['Cases'], 300)

    def test_unverified_aggressive_cases_do_not_reuse_overall_cases(self):
        parent, child, _ = policy.project([study(Cases=300)], 'Tomato',
            {'123': review({'one': 'aggressive'})}, lambda r: r['Effect ID'], case_reviews={})
        for row in parent + child:
            self.assertIsNone(row['Cases'])
            self.assertIsNone(row['Estimated Cases'])

    def test_aggressive_only_inherits_once_and_preserves_reported_counts(self):
        original = study(Cases=40)
        untouched = copy.deepcopy(original)
        parent, child, _ = project([original], review({'one': 'aggressive'}))
        self.assertEqual(len(parent), 1)
        self.assertEqual(len(child), 1)
        self.assertEqual(parent[0]['Disease Scope'], policy.GENERAL)
        self.assertEqual(child[0]['Disease Scope'], policy.AGGRESSIVE)
        self.assertEqual(parent[0]['disease_scope_review']['inherited_from'], policy.AGGRESSIVE)
        for row in parent + child:
            self.assertEqual(row['Cases'], 40)
            self.assertEqual(row['Sample Size'], 1000)
            self.assertIsNone(row['Estimated Cases'])
        self.assertEqual(original, untouched)

    def test_both_endpoints_keep_general_in_parent_without_child_duplicate(self):
        parent, child, audit = project([study('general'), study('aggressive')],
                                      review({'general': 'general', 'aggressive': 'aggressive'}))
        self.assertEqual([r['Effect ID'] for r in parent], ['general'])
        self.assertEqual([r['Effect ID'] for r in child], ['aggressive'])
        self.assertIn('separate_general_endpoint_prevents_duplicate_inheritance', [a['action'] for a in audit])

    def test_prospective_separate_endpoints_share_abstract_denominator(self):
        parent, child, _ = project([study('general'), study('aggressive', **{'Sample Size': 40, 'Cases': 40})],
            review({'general': 'general', 'aggressive': 'aggressive'}, prospective_cohort=True,
                   cohort_total_n=1200, cohort_total_n_excerpt='The cohort included 1200 men.'))
        self.assertEqual([r['Sample Size'] for r in parent + child], [1200, 1200])
        self.assertEqual([r['Cases'] for r in parent + child], [100, 40])

    def test_prospective_new_endpoint_can_use_saved_overall_denominator(self):
        parent, child, _ = project([study()], review({'one': 'general'}, prospective_cohort=True,
            additional_estimates=[additional(sample_size=None)]))
        self.assertEqual([r['Sample Size'] for r in parent + child], [1000, 1000])
        self.assertIsNot(child[0].get('display_only'), True)
        self.assertTrue(child[0]['disease_scope_pooling_selected'])
        self.assertEqual(child[0]['disease_scope_review']['cohort_sample_size']['source'], 'saved_overall_endpoint')

    def test_unresolved_prospective_denominator_holds_both_scopes(self):
        parent, child, _ = project([study(**{'Sample Size': None})], review({'one': 'general'},
            prospective_cohort=True, additional_estimates=[additional(sample_size=None)]))
        for row in parent + child:
            self.assertIsNone(row['Sample Size'])
            self.assertTrue(row['display_only'])
            self.assertFalse(row['disease_scope_pooling_selected'])

    def test_nonprospective_new_endpoint_cannot_borrow_overall_sample(self):
        parent, child, _ = project([study()], review({'one': 'general'},
            additional_estimates=[additional(sample_size=None)]))
        self.assertEqual(parent[0]['Sample Size'], 1000)
        self.assertIsNone(child[0]['Sample Size'])
        self.assertTrue(child[0]['display_only'])
        self.assertFalse(child[0]['disease_scope_pooling_selected'])

    def test_nonprospective_reported_endpoint_samples_remain_distinct(self):
        parent, child, _ = project([study()], review({'one': 'general'},
            additional_estimates=[additional(sample_size=800)]))
        self.assertEqual([r['Sample Size'] for r in parent + child], [1000, 800])

    def test_qualitative_aggressive_analysis_never_copies_general_effect(self):
        parent, child, _ = project([study()], review({'one': 'general'}, additional_estimates=[
            additional(effect_type=None, effect_size=None, lower_ci=None, upper_ci=None,
                       sample_size=None, cases=None, effect_excerpt='Tomato was examined in relation to aggressive cancer.')]))
        self.assertEqual(parent[0]['Effect Size'], 1.5)
        self.assertEqual(len(child), 1)
        for field in ['Effect Size', 'Lower CI', 'Upper CI', 'P Value', 'Cases', 'Estimated Cases']:
            self.assertIsNone(child[0][field])
        self.assertTrue(child[0]['display_only'])
        self.assertFalse(child[0]['disease_scope_pooling_selected'])

    def test_article_level_both_does_not_relabel_general_effect(self):
        parent, child, _ = project([study()], review({'one': 'general'}))
        self.assertEqual(parent[0]['Disease Endpoint'], 'general')
        self.assertEqual(child, [])

    def test_existing_article_evidence_can_disambiguate_both_endpoints(self):
        adjudication = review({'general': 'general', 'aggressive': 'aggressive'})
        for decision in adjudication['estimate_classifications']:
            decision.update(supported_by_abstract=False, endpoint_support_source='existing_evidence')
        parent, child, _ = project([study('general'), study('aggressive')], adjudication)
        self.assertEqual([r['Effect ID'] for r in parent], ['general'])
        self.assertEqual([r['Effect ID'] for r in child], ['aggressive'])
        self.assertEqual(child[0]['disease_scope_review']['endpoint_support_source'], 'existing_evidence')
        adjudication['aggressive_definition_excerpt'] = ''
        _, child, _ = project([study('general'), study('aggressive')], adjudication)
        self.assertEqual(child, [])

    def test_unsupported_estimate_stays_uncertain(self):
        adjudication = review({'one': 'aggressive'})
        adjudication['estimate_classifications'][0].update(supported_by_abstract=False,
                                                         endpoint_support_source='none')
        parent, child, _ = project([study()], adjudication)
        self.assertEqual(parent[0]['Disease Endpoint'], 'uncertain')
        self.assertEqual(child, [])

    def test_new_estimate_does_not_claim_original_adjustment_or_lift_hold(self):
        _, child, _ = project([study(policy_poolable=False)], review({'one': 'general'},
            additional_estimates=[additional()]))
        self.assertFalse(child[0]['policy_poolable'])
        self.assertNotIn('effect_is_adjusted', child[0])
        self.assertNotIn('Primary Estimate', child[0])
        self.assertFalse(child[0]['disease_scope_pooling_selected'])
        self.assertEqual(child[0]['Disease Endpoint Definition'], 'Advanced or high-grade prostate cancer')

    def test_overlapping_aggressive_contrasts_display_but_pool_once(self):
        parent, child, _ = project([study()], review({'one': 'aggressive'}, additional_estimates=[
            additional(), additional(effect_size=2.1, upper_ci=3.0, comparison='Per serving')]))
        self.assertEqual(len(child), 3)
        self.assertEqual(len(parent), 3)
        self.assertEqual(sum(r['disease_scope_pooling_selected'] for r in child), 1)
        self.assertTrue(child[0]['disease_scope_pooling_selected'])
        self.assertEqual(sum(r['disease_scope_pooling_selected'] for r in parent), 1)

    def test_same_saved_numeric_estimate_is_not_added_again(self):
        original = study(**{'Effect Type': 'HR', 'Effect Size': '1.8',
                            'Lower CI': '1.2', 'Upper CI': '2.5'})
        parent, child, _ = project([original], review({'one': 'aggressive'},
            additional_estimates=[additional(comparison='highest versus lowest')]))
        self.assertEqual(len(child), 1)
        self.assertEqual(len(parent), 1)

    def test_explicit_independent_populations_remain_poolable(self):
        _, child, _ = project([study('a', independent_population_id='cohort-a'),
                               study('b', independent_population_id='cohort-b')],
                              review({'a': 'aggressive', 'b': 'aggressive'}))
        self.assertTrue(all(r['disease_scope_pooling_selected'] for r in child))

    def test_reprojection_restores_source_rows_and_is_idempotent(self):
        originals = [study('general'), study('aggressive')]
        adjudication = review({'general': 'general', 'aggressive': 'aggressive'},
                              additional_estimates=[additional()])
        parent, child, audit = project(originals, adjudication)
        self.assertEqual(policy.source_rows(parent + child), originals)
        self.assertEqual(project(parent + child, adjudication), (parent, child, audit))
        restored, empty, _ = project(parent + child, review({'general': 'general', 'aggressive': 'general'}))
        self.assertEqual(len(restored), 2)
        self.assertEqual(empty, [])

    def test_source_rows_restores_endpoint_exclusion_snapshot(self):
        original = study('death')
        projected = study('diagnosis')
        restored = policy.source_rows([projected], [original])
        self.assertEqual({row['Effect ID'] for row in restored}, {'death', 'diagnosis'})

    def test_analysis_signature_detects_endpoint_and_selection_changes(self):
        original = study(**{'Disease Endpoint': 'general', 'disease_scope_pooling_selected': True})
        self.assertNotEqual(builder.analysis_signature([original]), builder.analysis_signature([
            {**original, 'Disease Endpoint': 'aggressive'}]))
        self.assertNotEqual(builder.analysis_signature([original]), builder.analysis_signature([
            {**original, 'disease_scope_pooling_selected': False}]))

    def test_inherited_aggressive_ratio_does_not_use_parent_baseline(self):
        import pandas as pd
        import meta_analysis
        import analysis_settings_backend as settings_backend
        for kind in ('RR', 'OR'):
            original = study(**{'Effect Type': kind, 'Effect Size': 2.0,
                                'Lower CI': 1.5, 'Upper CI': 2.5, 'SE': .13,
                                'Cases': None, 'Disease Endpoint': 'aggressive',
                                'Disease Scope': policy.GENERAL})
            frame = pd.DataFrame([original])
            result = meta_analysis.perform_meta_analysis(frame, policy.GENERAL, 'Tomato',
                df_all=frame, generate_plots=False)
            self.assertNotIn('error', result)
            self.assertEqual(result['headline']['canonical_rr']['pooled_es'], 2.0)
            converted = settings_backend._study_conversion(original, .132, 'RR',
                converter=lambda *_: self.fail('An inherited aggressive effect used the parent converter.'))
            self.assertEqual(converted['Canonical RR'], 2.0)
            self.assertIsNone(converted['Conversion Baseline Risk'])
            self.assertIsNone(converted['Estimated Cases'])

    def test_manual_child_baseline_converts_aggressive_or(self):
        import pandas as pd
        import meta_analysis
        import analysis_settings_backend as settings_backend
        original = study(**{'Effect Type': 'OR', 'Effect Size': 2.0,
                            'Lower CI': 1.5, 'Upper CI': 2.5, 'SE': .13,
                            'Disease Endpoint': 'aggressive', 'Disease Scope': policy.AGGRESSIVE})
        expected = 2.0 / (.98 + .02 * 2.0)
        frame = pd.DataFrame([original])
        with patch.object(meta_analysis, 'baseline_risk_for_disease', return_value=.02):
            result = meta_analysis.perform_meta_analysis(frame, policy.AGGRESSIVE, 'Tomato',
                df_all=frame, generate_plots=False)
        self.assertNotIn('error', result)
        self.assertAlmostEqual(result['headline']['canonical_rr']['pooled_es'], expected)
        converted = settings_backend._study_conversion(original, .02, 'RR')
        self.assertAlmostEqual(converted['Canonical RR'], expected)
        self.assertEqual(converted['Conversion Baseline Risk'], .02)

    def test_builder_reuses_originals_from_child_cache_on_later_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'Cached_results'
            exposure = cache / 'tomato'
            exposure.mkdir(parents=True)
            source = exposure / 'prostate_cancer_incidence_true_all.json'
            source.write_text(json.dumps({'exposure': 'Tomato', 'canonical_track': 'chatgpt',
                                          'studies': [study('general'), study('aggressive')]}))
            (cache / '_merge_manifest.json').write_text(json.dumps({'build_id': 'original-build', 'source': 'unchanged'}))
            reviews = root / 'reviews'
            reviews.mkdir()
            document = {'reviews': [{'pmid': '123', **review({'general': 'general', 'aggressive': 'aggressive'})}]}
            (reviews / 'review.json').write_text(json.dumps(document))
            def rebuild(payload, rows, disease, digest, plots):
                return {**payload, 'studies': rows, 'disease': disease, 'cancer': disease}
            with patch('scripts.aggressive_prostate_review.evidence_row_id', lambda row: row['Effect ID']), patch.object(builder, 'rebuild', side_effect=rebuild):
                builder.build(reviews, [cache], generate_plots=False)
                self.assertEqual(len(json.loads(source.read_text())['studies']), 1)
                document['reviews'][0] = {'pmid': '123', **review({'general': 'general', 'aggressive': 'general'})}
                (reviews / 'review.json').write_text(json.dumps(document))
                builder.build(reviews, [cache], generate_plots=False)
            self.assertEqual(len(json.loads(source.read_text())['studies']), 2)
            self.assertEqual(json.loads((exposure / 'aggressive_prostate_cancer_incidence_true_all.json').read_text())['studies'], [])
            lineage = json.loads((cache / '_merge_manifest.json').read_text())
            self.assertEqual(lineage['build_id'], 'original-build')
            self.assertEqual(lineage['source'], 'unchanged')
            self.assertEqual(lineage['disease_scope_projection']['counts']['aggressive_rows'], 0)


if __name__ == '__main__':
    unittest.main()
