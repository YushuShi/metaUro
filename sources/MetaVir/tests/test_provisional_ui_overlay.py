import copy
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path
from scripts import provisional_ui_overlay as ui

CANCER='Prostate cancer' if ui.ROOT.name=='MetaVir' else 'Breast cancer'
CID='123|Vitamin D|Prostate cancer' if ui.ROOT.name=='MetaVir' else '123|Vitamin D|parent:breast'


def record(effect=1.2, grade='Good', n=1000, status='INCLUDE_META'):
    return {'context_id':CID,'pmid':'123','exposure':'Vitamin D','cancer':CANCER,
        'scope_type':'parent','major_site_id':'breast','eligibility':'INCLUDE',
        'final_status':status,'full_text_status':status,'jbi':{'grade':grade,'score_percent':90},
        'authors':['Test A'],'published_date':'2020','combined_adjudication':{'resolution':'PENDING_MANUAL_REVIEW'},
        'extraction':{'effect_type':'RR','effect_size':effect,'ci_lower':effect*.8,
            'ci_upper':effect*1.2,'total_n':n,'comparison_type':'Highest versus lowest',
            'comparison_direction':'higher_vs_lower','reference_category':'lowest','needs_inversion':False,
            'exposure_measurement_type':'dietary_intake','supporting_text':'Saved source evidence'}}


class ProvisionalUiTests(unittest.TestCase):
    def project(self,a,b,baseline=None,applied=False,protected=(),separate=False):
        baseline=baseline or record();rows={CID:baseline};tracks={'chatgpt':{CID:a},'claude_gemini':{CID:b}}
        entry={'id':'review1','manual_review_required':True,'application':{'applied':applied},
               'source_row':{'Context ID':CID,'Estimate pair':'1'}}
        if separate:entry['provisional_separate_scientific_scope']='MR_SEPARATE'
        saved=ui.checkpoint(rows,tracks,[entry]);before=copy.deepcopy((rows,tracks))
        output,audit=ui.project(rows,tracks,saved,protected)
        self.assertEqual((rows,tracks),before)
        return output,audit,saved,rows,tracks

    def test_complete_chatgpt_wins_with_higher_saved_jbi_while_pending(self):
        a,b=record(1.2,'Fair'),record(1.5,'Good')
        output,audit,*_=self.project(a,b)
        row=output[CID]
        self.assertEqual(row['extraction']['effect_size'],1.2)
        self.assertEqual(row['jbi'],b['jbi'])
        self.assertTrue(row['manual_review_pending'])
        self.assertEqual(audit[0]['selected_track'],'chatgpt')
        self.assertEqual(row['combined_adjudication']['resolution'],'PENDING_MANUAL_REVIEW')

    def test_complete_other_is_atomic_and_independent_hold_survives(self):
        a,b=record(n=None),record(1.5,n=2000)
        baseline=record();baseline['full_text_pooling_block']=True
        output,audit,*_=self.project(a,b,baseline)
        self.assertEqual(output[CID]['extraction']['total_n'],2000)
        self.assertEqual(output[CID]['extraction']['effect_size'],1.5)
        self.assertEqual(audit[0]['selected_track'],'claude_gemini')
        self.assertEqual(output[CID]['final_status'],'ELIGIBLE_NOT_POOLABLE')

    def test_applied_astra_partial_uses_complete_source_only_in_projection(self):
        baseline=record(n=None,status='ELIGIBLE_NOT_POOLABLE')
        baseline['astra_fulltext_adjudication']={'model':'gpt-6-astra'}
        output,audit,*_=self.project(record(),record(1.5),baseline,applied=True)
        self.assertEqual(output[CID]['extraction']['total_n'],1000)
        self.assertTrue(audit[0]['overrides_pending_astra_partial'])

    def test_incomplete_astra_partial_retains_its_numeric_bundle(self):
        baseline=record(.7,n=None,status='ELIGIBLE_NOT_POOLABLE')
        output,audit,*_=self.project(record(n=None),record(1.5,n=None),baseline,applied=True)
        self.assertEqual(output[CID]['extraction'],baseline['extraction'])
        self.assertTrue(audit[0]['protect_adjudicated_quantities'])
        self.assertFalse(audit[0]['effective_poolable'])

    def test_human_and_changed_sources_are_protected(self):
        output,audit,saved,rows,tracks=self.project(record(),record(1.5),protected={CID})
        self.assertEqual(output,rows);self.assertFalse(audit[0]['projected'])
        tracks['chatgpt'][CID]['extraction']['total_n']=2000
        output,audit=ui.project(rows,tracks,saved)
        self.assertEqual(output,rows);self.assertIn('Source screening changed',audit[0]['reason'])

    def test_changed_authoritative_decision_and_unrelated_pairs_are_protected(self):
        _,_,saved,rows,tracks=self.project(record(),record(1.5))
        rows[CID]['extraction']['total_n']=3000
        output,audit=ui.project(rows,tracks,saved)
        self.assertEqual(output,rows);self.assertIn('Authoritative scientific decision changed',audit[0]['reason'])
        baseline=record();baseline['additional_extractions']=[record(2)['extraction']]
        output,audit,*_=self.project(record(),record(1.5),baseline)
        self.assertEqual(output[CID],baseline);self.assertIn('multi-estimate',audit[0]['reason'])

    def test_saved_reverse_comparison_has_a_consistent_display_direction(self):
        a,b=record(n=None),record(1.6)
        b['extraction'].update(ci_lower=.7,ci_upper=3.8,comparison_type='lower versus higher dietary folate intake',
            comparison_direction='lower_vs_higher',needs_inversion=True,reference_category=None)
        output,audit,*_=self.project(a,b)
        self.assertIsNone(audit[0]['projected_rows'][0]['normalization_error'])
        from scripts import monthly_evidence_update as monthly
        row=monthly.study_row(output[CID])
        self.assertAlmostEqual(row['Effect Size'],1/1.6)
        self.assertAlmostEqual(row['Lower CI'],1/3.8)

    def test_pending_mr_default_and_higher_jbi_stay_outside_conventional_ui(self):
        a,b=record(1.2,'Moderate',status='MR_SEPARATE'),record(1.5,'Good',status='MR_SEPARATE')
        baseline=record(status='PENDING_MANUAL_REVIEW');baseline['extraction']=None
        output,audit,*_=self.project(a,b,baseline,separate=True)
        row=output[CID]
        self.assertEqual(row['extraction']['effect_size'],1.2)
        self.assertEqual(row['jbi'],b['jbi'])
        self.assertEqual(row['final_status'],'MR_SEPARATE')
        self.assertTrue(row['manual_review_pending'])
        self.assertFalse(row['provisional_ui_display_only'])
        self.assertFalse(audit[0]['effective_poolable'])
        if ui.ROOT.name=='metaFemina':
            from scripts.project_fair_ui import eligible_provisional_display, eligible_fair
            self.assertFalse(eligible_provisional_display(row))
            self.assertFalse(eligible_fair(row))

    def test_supplemental_checkpoint_replays_and_holds_changed_sources(self):
        a,b=record(status='MR_SEPARATE'),record(1.5,status='MR_SEPARATE')
        output,audit,saved,rows,tracks=self.project(a,b,separate=True)
        with tempfile.TemporaryDirectory() as tmp:
            ui.write(Path(tmp)/ui.SUPPLEMENTAL_CHECKPOINT,saved)
            actual,receipt=ui.project_supplemental(rows,tracks,tmp)
            self.assertEqual(actual,output)
            self.assertEqual(receipt,audit)
            tracks['chatgpt'][CID]['extraction']['total_n']=2000
            actual,receipt=ui.project_supplemental(rows,tracks,tmp)
            self.assertEqual(actual,rows)
            self.assertFalse(receipt[0]['projected'])

    def test_subtype_visibility_uses_exact_original_context_lineage(self):
        from scripts import apply_provisional_ui as app
        audit=[{'id':'review1','context_id':CID,'selected_track':'claude_gemini'}]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'metaFemina'
            for mode in ['yes','no']:
                ui.write(root/'data/subexposure_views'/mode/'subcategory_results/breast/dcis/vitamin_d.json',
                    {'studies':[{'context_id':CID+'|subcategory-ui:abc','Original Screening Context ID':CID,
                       'Quality Score':'Good','Sample Size':1000,'Effect Size':1.2,
                       'subexposure_pooling_selected':True},
                       {'context_id':'different scope','PMID':'123','Quality Score':'Fair'}]})
            with patch.object(app,'ROOT',root):receipt=app.visibility(audit)[0]
            for mode in ['yes','no']:
                self.assertEqual(receipt['ui_modes'][mode]['visible_rows'],1)
                self.assertEqual(receipt['ui_modes'][mode]['pooling_selected_rows'],1)
                self.assertEqual(receipt['ui_modes'][mode]['rows'][0]['source'],'claude_gemini')


if __name__=='__main__':unittest.main()
