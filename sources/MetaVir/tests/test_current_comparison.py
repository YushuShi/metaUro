import copy,json,math,unittest
from pathlib import Path
from scripts import current_comparison_adapter as adapter
from scripts import key_comparison_policy as policy
from scripts import build_combined_results as builder
from scripts import monthly_evidence_update as monthly
ROOT=Path(__file__).resolve().parents[1]

class CurrentComparisonTests(unittest.TestCase):
    def test_numeric_rounding_and_direction_are_not_new_discrepancies(self):
        a={'extraction':{'total_n':'1,000','effect_size':1.231,'ci_lower':1.112,'ci_upper':1.382,'needs_inversion':True}}
        b={'extraction':{'total_n':1000,'effect_size':1.23,'ci_lower':1.11,'ci_upper':1.38,'needs_inversion':False}}
        self.assertFalse(policy.compare_context(a,b)['has_key_discrepancy'])
        b['extraction']['total_n']=1001
        self.assertEqual(policy.compare_context(a,b)['pairs'][0]['differences'],['total_n'])

    def test_missing_nonkeys_do_not_make_hybrid_key_bundle_or_jbi_rating(self):
        preferred={'extraction':{'total_n':None,'effect_size':.8,'ci_lower':.6,'ci_upper':1,'cases':None,'location':'NA'},
                   'jbi':{'grade':'Fair','answers':{'q1':'Yes'}}}
        donor={'extraction':{'total_n':1000,'effect_size':.7,'ci_lower':.5,'ci_upper':.9,'cases':40,'location':'USA'},
               'jbi':{'grade':'Good','answers':{'q1':'Yes','q2':'Yes'}}}
        original=copy.deepcopy(preferred);out,fills=adapter._merge_metadata(preferred,[('CLAUDE_GEMINI',donor)])
        self.assertEqual(preferred,original);self.assertIsNone(out['extraction']['total_n'])
        self.assertEqual(out['extraction']['effect_size'],.8);self.assertEqual(out['extraction']['cases'],40)
        self.assertEqual(out['extraction']['location'],'USA');self.assertEqual(out['jbi'],preferred['jbi'])
        del preferred['jbi'];out,_=adapter._merge_metadata(preferred,[('CLAUDE_GEMINI',donor)])
        self.assertEqual(out['jbi'],donor['jbi'])

    def test_missing_counterpart_maps_to_historical_screening_field(self):
        p=policy.compare_context({'final_status':'EXCLUDE'},{'eligibility':'INCLUDE','extraction':{'total_n':100}})['pairs'][0]
        self.assertEqual(adapter._current_fields(p,True),{'SCREENING_DECISION'})
        self.assertEqual(adapter._current_fields({'differences':['total_n','effect_size','ci']},False),{'SAMPLE_SIZE','EFFECT_SIZE','CI_OR_P_VALUE'})

    def test_new_pending_key_difference_cannot_be_promoted_by_poolability(self):
        row={'pmid':'1','exposure':'Vitamin D','final_status':'ELIGIBLE_NOT_POOLABLE','jbi':{'grade':'Good'},
             'key_comparison_pending':True,'extraction':{'effect_size':.8,'effect_type':'RR','ci_lower':.6,'ci_upper':1.,'total_n':1000}}
        builder.reclassify_poolability({'1':row},monthly)
        self.assertEqual(row['final_status'],'ELIGIBLE_NOT_POOLABLE')
        self.assertTrue(any('cross-model' in x for x in row['poolability_policy']['remaining_issues']))

    def test_installed_current_records_and_subset_audit(self):
        root=ROOT/'combined';summary=json.loads((root/'_current_comparison.json').read_text())
        packet=json.loads((root/'final_screening.json').read_text());rows={r['context_id']:r for r in packet['results']}
        self.assertEqual(summary['shared_reviewed_contexts'],6314)
        self.assertFalse(summary['current_context_set_is_subset_of_previous']);self.assertFalse(summary['current_field_set_is_subset_of_previous'])
        self.assertEqual(summary['new_unpooled_pending_contexts'],18)
        pending=[r for r in rows.values() if r.get('key_comparison_pending')]
        # This comparison report is the historical pre-adjudication audit.
        # Subsequent validated Astra decisions resolve some of those contexts.
        original_pending={entry[0] for entry in summary['new_discrepancy_fields']}
        self.assertEqual(len(original_pending),summary['new_unpooled_pending_contexts'])
        self.assertLessEqual({r['context_id'] for r in pending},original_pending)
        resolved=original_pending-{r['context_id'] for r in pending}
        for context_id in resolved:
            row=rows[context_id]
            action=row.get('key_comparison',{}).get('action')
            if action=='astra_title_abstract_resolved':
                decision=row['astra_adjudication']['decision']
                self.assertTrue(decision['source_quote_validated'])
                self.assertEqual(decision['adjudicator_model'],'gpt-6-astra')
            elif action=='astra_pmc_fulltext_resolved':
                adjudication=row['astra_fulltext_adjudication']
                self.assertEqual(adjudication['source_scope'],'PMC_FULL_TEXT_TEXT_ONLY')
                self.assertTrue(adjudication['source_fingerprint'])
                self.assertTrue(adjudication['entries'])
                for entry in adjudication['entries']:
                    self.assertTrue(entry['adjudication']['source_quote_validated'])
                    self.assertTrue(entry['adjudication']['numeric_inputs_validated'])
            else:
                self.fail(f'{context_id} lost its pending flag without a validated adjudication.')
        self.assertEqual(len(pending)+len(resolved),len(original_pending))
        for row in pending:
            self.assertEqual(row['final_status'],'ELIGIBLE_NOT_POOLABLE')
            self.assertEqual({c['comparison_candidate_track'] for c in row['key_comparison_candidates']},{'CHATGPT','CLAUDE_GEMINI'})
        archive=root/'_history/before_current_key_comparison_2026-09-05'
        self.assertTrue((archive/'_merge_manifest.json').is_file())
        self.assertEqual(json.loads((archive/'_merge_manifest.json').read_text())['counts']['fable_field_rows'],631)

    def test_pending_candidates_remain_visible_but_never_selected_in_both_modes(self):
        records=json.loads((ROOT/'combined/final_screening.json').read_text())['results']
        expected=set()
        for record in records:
            if record.get('key_comparison_pending') is not True:continue
            for candidate in record.get('key_comparison_candidates',[]):
                track=candidate.get('comparison_candidate_track')
                if builder.normalized_status(candidate,claude_gemini=track=='CLAUDE_GEMINI')=='EXCLUDE':continue
                expected.add((str(candidate.get('pmid')),candidate.get('direct_exposure') or candidate.get('exposure'),track))
        for mode in ['yes','no']:
            visible=set()
            for path in (ROOT/'Subexposure_results'/mode).glob('*/*_true_all.json'):
                for row in json.loads(path.read_text()).get('studies',[]):
                    if row.get('key_comparison_pending') is True:
                        self.assertTrue(row['display_only']);self.assertFalse(row['subexposure_pooling_selected'])
                        visible.add((str(row.get('PMID')),row.get('Direct Exposure'),row.get('comparison_candidate_track')))
            self.assertLessEqual(expected,visible,f'Current unresolved candidates are missing from the {mode} display mode.')

if __name__=='__main__':unittest.main()
