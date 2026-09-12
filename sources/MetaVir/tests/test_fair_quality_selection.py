import copy
import unittest
from scripts.fair_quality_selection import (annotate_rows, eligible_for_user_selection,
                                    is_fair_opt_in, prepare_quality_opt_in_rows, bind_fair_rows)


class FairSelectionTests(unittest.TestCase):
    def row(self, **changes):
        row = {'PMID': '1', 'Quality Score': 'Fair', 'Effect Type': 'RR',
               'Effect Size': .8, 'Lower CI': .6, 'Upper CI': 1.1, 'Sample Size': 1000,
               'display_only_reason': 'Eligible study; JBI Fair is below the default pooling threshold.',
               'display_only': True, 'policy_poolable': False, 'manual_poolable': False,
               'Primary Estimate': False, 'pool_in_parent_meta_analysis': False,
               'subexposure_pooling_selected': False, 'final_status': 'ELIGIBLE_NOT_POOLABLE',
               'Direct Exposure': 'Tea', 'Displayed Exposure': 'Tea'}
        row.update(changes)
        return row

    def test_default_and_annotation_do_not_release_holds(self):
        row = self.row()
        self.assertEqual(prepare_quality_opt_in_rows([row]), [row])
        self.assertFalse(is_fair_opt_in({'quality_filter': 'Fair+'}))
        self.assertFalse(is_fair_opt_in({'quality_filter': 'Moderate+', 'allow_fair_quality': True}))
        projected = annotate_rows([row])[0]
        self.assertTrue(projected['fair_quality_opt_in_available'])
        self.assertTrue(projected['display_only'])
        self.assertFalse(projected['fair_quality_selection_overrides']['display_only'])
        self.assertNotIn('Quality Score', projected['fair_quality_selection_overrides'])
        self.assertNotIn('fair_quality_opt_in_available', row)

    def test_explicit_opt_in_keeps_original_grade_status_and_numbers(self):
        row = self.row(disease_scope_pooling_selected=False,
                       disease_scope_pooling_reason='Another eligible endpoint/contrast is selected for this study population, or this row does not meet the existing pooling criteria.')
        before = copy.deepcopy(row)
        projected = prepare_quality_opt_in_rows([row], 'All', allow_fair_quality=True)[0]
        self.assertTrue(projected['fair_quality_opted_in'])
        self.assertTrue(projected['subexposure_pooling_selected'])
        self.assertTrue(projected['disease_scope_pooling_selected'])
        self.assertFalse(projected['display_only'])
        self.assertTrue(projected['Original display_only'])
        for field in ('Quality Score', 'final_status', 'Effect Size', 'Lower CI', 'Upper CI', 'Sample Size'):
            self.assertEqual(projected[field], row[field])
        self.assertEqual(row, before)

    def test_unrelated_holds_missing_quantities_and_hierarchy_stay_excluded(self):
        for change in ({'manual_review_pending': True}, {'manual_pooling_eligible': False},
                       {'poolable': False}, {'pool_in_exposure_parent_meta_analysis': False},
                       {'pool_in_disease_parent_meta_analysis': False},
                       {'key_comparison_pending': True}, {'clinical_exclusion': True},
                       {'manual_review': {'decision': 'exclude'}}, {'subexposure_inherited': True},
                       {'final_status': 'ELIGIBLE_SUBTYPE_REVIEW'}, {'display_only_reason': 'Other restriction'},
                       {'third_round_reasons': ['fair_jbi', 'required_quantitative_missing']},
                       {'missing_quantities': ['cases']}, {'Sample Size': None}, {'Lower CI': .9},
                       {'disease_scope_pooling_reason': 'Unconfirmed aggressive endpoint', 'disease_scope_pooling_selected': False}):
            with self.subTest(change=change):
                row = self.row(**change)
                self.assertFalse(eligible_for_user_selection(row))
                result = prepare_quality_opt_in_rows([row], 'All', allow_fair_quality=True)[0]
                self.assertNotIn('fair_quality_opted_in', result)
                self.assertTrue(result['display_only'])

    def test_existing_selected_study_wins_and_new_fair_duplicates_pool_once(self):
        first = self.row()
        second = self.row(Gender='Female')
        third = self.row(PMID='2')
        result = prepare_quality_opt_in_rows([first, second, third], 'All', allow_fair_quality=True)
        self.assertEqual(sum(row.get('fair_quality_opted_in', False) for row in result), 2)
        good = self.row(**{'Quality Score': 'Good', 'display_only': False, 'policy_poolable': True,
                           'manual_poolable': True, 'Primary Estimate': True,
                           'pool_in_parent_meta_analysis': True, 'subexposure_pooling_selected': True})
        result = prepare_quality_opt_in_rows([first, good], 'All', allow_fair_quality=True)
        self.assertNotIn('fair_quality_opted_in', result[0])
        self.assertEqual(result[1], good)

    def test_femina_quality_reason_recognized_but_required_fields_hold_remains(self):
        row = self.row()
        row.pop('display_only_reason')
        for field in ('display_only', 'manual_poolable', 'policy_poolable', 'Primary Estimate'):
            row.pop(field)
        row.update(ui_display_reason='Eligible; JBI Fair — shown for review, not pooled', ui_display_only=True,
                   third_round_reasons=['fair_jbi'])
        self.assertTrue(eligible_for_user_selection(row))
        row['third_round_reasons'].append('required_quantitative_missing')
        self.assertFalse(eligible_for_user_selection(row))

    def test_heart_thyroid_quality_marker_never_releases_independent_manual_flag(self):
        row = self.row()
        for field in ('display_only_reason', 'manual_poolable', 'policy_poolable',
                      'Primary Estimate', 'pool_in_parent_meta_analysis'):
            row.pop(field)
        row['pooling_exclusion_reason'] = 'Fair JBI: displayed for review; not included in pooled estimates.'
        self.assertTrue(eligible_for_user_selection(row))
        result = prepare_quality_opt_in_rows([row], 'All', allow_fair_quality=True)[0]
        self.assertFalse(result['display_only'])
        self.assertNotIn('manual_poolable', result)
        row['manual_poolable'] = False
        self.assertFalse(eligible_for_user_selection(row))

    def test_continuous_neuro_estimates_keep_their_signed_domain(self):
        row = self.row(**{'Effect Type': 'BETA', 'Effect Size': -.2, 'Lower CI': -.4, 'Upper CI': .1})
        self.assertTrue(eligible_for_user_selection(row))
        row['Effect Type'] = 'RR'
        self.assertFalse(eligible_for_user_selection(row))

    def test_dataframe_missing_metadata_and_stale_permission_are_safe(self):
        row = self.row(manual_review=float('nan'), third_round_reasons=float('nan'),
                       ui_display_reason=float('nan'), provisional_ui_selection=float('nan'),
                       fair_quality_opted_in=True)
        self.assertTrue(eligible_for_user_selection(row))
        result = prepare_quality_opt_in_rows([row])[0]
        self.assertNotIn('fair_quality_opted_in', result)
        self.assertIsNone(result['manual_review'])
        self.assertTrue(row['fair_quality_opted_in'])
        self.assertTrue(prepare_quality_opt_in_rows([row], 'All', allow_fair_quality=True)[0]['fair_quality_opted_in'])

    def test_binding_restores_source_holds_and_keeps_only_editable_values(self):
        source = self.row(**{'Record ID': 'row-1'})
        submitted = dict(source, **{'Effect Size': .9, 'Sample Size': 1500, 'Cases': 25,
                                   'Quality Score': 'Good', 'display_only': False,
                                   'manual_poolable': True, 'clinical_exclusion': False,
                                   'fair_quality_opted_in': True})
        bound = bind_fair_rows([submitted], [source], 'Tea')[0]
        self.assertEqual(bound['Quality Score'], 'Fair')
        self.assertTrue(bound['display_only'])
        self.assertFalse(bound['manual_poolable'])
        self.assertNotIn('fair_quality_opted_in', bound)
        self.assertEqual(bound['Effect Size'], .9)
        self.assertEqual(bound['Sample Size'], 1500)
        self.assertEqual(bound['Cases'], 25)
        for sources in ([dict(source, clinical_exclusion=True)], [source, source], []):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                bind_fair_rows([dict(submitted, **{'Quality Score': 'Fair'})], sources, 'Tea')
        with self.assertRaises(ValueError):
            bind_fair_rows([dict(submitted, PMID='wrong')], [source], 'Tea')

    def test_legacy_binding_requires_unique_unchanged_effect_bundle(self):
        source = self.row()
        submitted = dict(source, Cases=20)
        self.assertEqual(bind_fair_rows([submitted], [source], 'Tea')[0]['Cases'], 20)
        with self.assertRaises(ValueError):
            bind_fair_rows([dict(submitted, **{'Effect Size': .9})], [source], 'Tea')
        good = dict(source, **{'Quality Score': 'Good', 'PMID': '3'})
        self.assertEqual(bind_fair_rows([good], [source], 'Tea'), [good])


if __name__ == '__main__':
    unittest.main()
