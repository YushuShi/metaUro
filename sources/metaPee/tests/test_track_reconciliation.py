import copy, unittest
from scripts import reconcile_screening_tracks as reconcile
from scripts.subexposure_policy import project_rows,pooling_rows


def record(extraction=None,**extra):
    return {'context_id':'1|Tea|Cancer','pmid':'1','exposure':'Tea','cancer':'Cancer','outcome':'Incidence',
        'eligibility':'INCLUDE','final_status':'INCLUDE_META','jbi':{'grade':'Good'},
        'extraction':extraction or {'total_n':1000,'effect_type':'RR','effect_size':1.2,'ci_lower':1.1,'ci_upper':1.3,'p_value':.2},**extra}


class TrackReconciliationTests(unittest.TestCase):
    def test_rounding_direction_and_nonkey_fill(self):
        gpt=record();gpt['extraction'].update(total_n='1,000',effect_size=1.2004,comparison_direction='higher_vs_lower',country='Japan')
        cg=record();cg['extraction'].update(comparison_direction='lower_vs_higher',country='NA')
        frozen=copy.deepcopy([gpt,cg])
        merged,entries,issues=reconcile.reconcile_context(gpt,cg)
        self.assertFalse(issues);self.assertEqual(merged['extraction']['effect_size'],1.2)
        self.assertEqual(merged['extraction']['country'],'Japan')
        self.assertEqual(merged['extraction']['comparison_direction'],'lower_vs_higher')
        self.assertEqual([gpt,cg],frozen)

    def test_p_only_discrepancy_uses_wald_and_retains_provenance(self):
        a=record();b=record();a['extraction']['p_value']=.8;b['extraction']['p_value']=.9
        merged,entries,issues=reconcile.reconcile_context(a,b)
        self.assertFalse(issues);self.assertLess(merged['extraction']['p_value'],.001)
        self.assertEqual(merged['extraction']['combined_original_p_value'],.9)
        self.assertEqual(entries[0]['p_resolution']['matching_source'],'neither')

    def test_key_disagreement_retains_both_without_winner(self):
        a=record();b=record();b['extraction']['total_n']=1001
        merged,entries,issues=reconcile.reconcile_context(a,b)
        self.assertEqual(issues[0]['key_differences'],['total_n'])
        self.assertIsNone(merged['extraction']);self.assertEqual(len(merged['key_review_candidates']),2)
        rows=[reconcile.app_row(e) for e in entries]
        self.assertEqual({r['Sample Size'] for r in rows},{1000,1001})
        self.assertTrue(all(r['requires_key_review'] and not r['policy_poolable'] for r in rows))

    def test_excluded_counterpart_is_explicitly_reviewed(self):
        a=record(eligibility='EXCLUDE',final_status='EXCLUDE');b=record()
        merged,entries,issues=reconcile.reconcile_context(a,b)
        self.assertEqual(len(issues),1);self.assertIsNone(issues[0]['chatgpt'])
        self.assertEqual(len(entries),1);self.assertFalse(reconcile.app_row(entries[0])['policy_poolable'])
        merged,entries,issues=reconcile.reconcile_context(a,a)
        self.assertFalse(issues);self.assertFalse(entries);self.assertEqual(merged['final_status'],'EXCLUDE')

    def test_manual_exclusion_overrides_machine_inclusion(self):
        a=record(eligibility='EXCLUDE',final_status='EXCLUDE',manual_review={'reviewer':'user','verbatim_note':'Exclude both lung-cancer entries'},manual_poolable=False)
        b=record()
        merged,entries,issues=reconcile.reconcile_context(a,b)
        self.assertTrue(merged['combined_manual_authority']);self.assertEqual(merged['final_status'],'EXCLUDE')
        self.assertFalse(entries);self.assertFalse(issues);self.assertEqual(merged['manual_review'],a['manual_review'])

    def test_extra_estimate_order_does_not_create_discrepancy(self):
        main=record()['extraction'];extra={**main,'total_n':500,'effect_size':.8,'ci_lower':.7,'ci_upper':.9}
        a=record({**main,'additional_records':[extra]});b=record({**extra,'additional_records':[main]})
        merged,entries,issues=reconcile.reconcile_context(a,b)
        self.assertFalse(issues);self.assertEqual(len(entries),2)
        self.assertEqual({e['source_estimate_index'] for e in entries},{0,1})

    def test_independent_sexes_survive_overlap_guard(self):
        male={**record()['extraction'],'gender':'Male'};female={**male,'gender':'Female','effect_size':1.3,'ci_lower':1.2,'ci_upper':1.4}
        a=record({**male,'additional_records':[female]})
        _,entries,_=reconcile.reconcile_context(a,a)
        rows=[reconcile.app_row(e) for e in entries]
        projected=project_rows({'Tea':rows},'Tea',{},include=False)
        self.assertEqual(len(pooling_rows(projected)),2)
        rows[1]['Gender']='Mixed'
        self.assertEqual(len(pooling_rows(project_rows({'Tea':rows},'Tea',{},include=False))),1)

    def test_fair_and_explicit_clinical_gates_remain_unpooled(self):
        a=record(jbi={'grade':'Fair'},final_status='ELIGIBLE_NOT_POOLABLE')
        _,entries,_=reconcile.reconcile_context(a,a)
        self.assertFalse(reconcile.app_row(entries[0])['policy_poolable'])

    def test_comparison_uses_saved_numbers_but_app_respects_explicit_inversion(self):
        a=record();a['extraction']['needs_inversion']=True
        merged,entries,issues=reconcile.reconcile_context(a,a)
        self.assertFalse(issues);self.assertEqual(merged['extraction']['effect_size'],1.2)
        display=reconcile.app_row(entries[0])
        self.assertAlmostEqual(display['Effect Size'],1/1.2)
        self.assertAlmostEqual(display['Lower CI'],1/1.3)
        self.assertTrue(display['effect_inverted']);self.assertEqual(display['Original Effect Size'],1.2)

    def test_derived_p_does_not_claim_original_atomic_receipt(self):
        a=record();b=record();a['extraction']['p_value']=.8;b['extraction'].update(p_value=.9,estimate_bundle_digest='source-receipt')
        original=copy.deepcopy(b)
        merged,_,_=reconcile.reconcile_context(a,b)
        self.assertEqual(b,original)
        self.assertNotIn('estimate_bundle_digest',merged['extraction'])
        self.assertEqual(merged['extraction']['combined_source_estimate_provenance']['estimate_bundle_digest'],'source-receipt')

    def test_jbi_answers_are_not_synthesized_across_reviewers(self):
        a=record(jbi={'grade':'Good','score_percent':90,'answers':{'q1':'yes','q2':'yes'}})
        b=record(jbi={'grade':'Fair','score_percent':None,'answers':{'q1':'no'}})
        merged,entries,_=reconcile.reconcile_context(a,b)
        self.assertEqual(merged['jbi'],b['jbi'])
        self.assertEqual(entries[0]['source_record']['jbi'],b['jbi'])
        self.assertFalse(any(fill['field'].startswith('jbi') for fill in merged['combined_nonkey_fills']))
        b=record(full_text_pooling_block='Clinical source conflict')
        _,entries,_=reconcile.reconcile_context(b,b)
        self.assertFalse(reconcile.app_row(entries[0])['policy_poolable'])


if __name__=='__main__':unittest.main()
