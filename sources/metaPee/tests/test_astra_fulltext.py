"""PMC-only text provenance, pair replacement, and durable replay safeguards."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import astra_fulltext_common as common
from scripts import astra_fulltext_overlay as overlay
from scripts import apply_astra_adjudication as abstract
from scripts import reconcile_screening_tracks as reconcile


class FulltextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.text=self.root/'PMC123.txt';self.text.write_text('The study included 1000 participants. The adjusted RR was 0.8 (95% CI 0.7 to 0.9).')
        ex={'total_n':1000,'effect_type':'RR','effect_size':1.2,'ci_lower':1.1,'ci_upper':1.3,'cases':50,'comparison_type':'High versus low','comparison_direction':'higher_vs_lower'}
        self.a={'context_id':'123|Dairy|Example','pmid':'123','exposure':'Dairy','disease':'Example','cancer':'Example','endpoint':'Incidence','eligibility':'INCLUDE','final_status':'INCLUDE_META','jbi':{'grade':'Moderate'},'extraction':ex}
        self.b=copy.deepcopy(self.a);self.b['extraction']['total_n']=2000
        self.tracks={'chatgpt':{self.a['context_id']:self.a},'claude_gemini':{self.b['context_id']:self.b}}
        self.prior,_,_=reconcile.reconcile_context(self.a,self.b)

    def tearDown(self):self.temp.cleanup()

    def entry(self):
        return {'id':'review1','source_fingerprint':common.source_digest(self.a,self.b),
            'source_row':{'Context ID':self.a['context_id'],'PMID':'123','Exposure':'Dairy','Disease':'Example','Estimate pair':'1','ChatGPT estimate index':'0','Claude+Gemini estimate index':'0'},
            'adjudication':{'id':'review1','confidence':'high','verdict':'CORRECTED','eligibility':'INCLUDE','source_quote_validated':True,
                'extraction':{'total_n':1000,'effect_type':'aRR','effect_size':.8,'ci_lower':.7,'ci_upper':.9},
                'evidence':[{'fields':['total_n'],'quote':'The study included 1000 participants.','section':'Methods'},
                            {'fields':['effect_size','ci_lower','ci_upper'],'quote':'The adjusted RR was 0.8 (95% CI 0.7 to 0.9).','section':'Results'}],
                'source':{'pmid':'123','pmcid':'PMC123','text_path':str(self.text),'text_sha256':hashlib.sha256(self.text.read_bytes()).hexdigest(),'url':'https://pmc.ncbi.nlm.nih.gov/articles/PMC123/'},'reason':'The PMC table identifies this adjusted contrast.'}}

    def test_pmc_text_correction_applies_atomic_bundle_and_keeps_sources(self):
        before=copy.deepcopy(self.tracks);result,items,audit=overlay.apply_context(self.prior,self.tracks,[self.entry()])
        self.assertTrue(audit[0]['applied']);self.assertFalse(audit[0]['follow_up_required'])
        self.assertEqual(result['extraction']['effect_size'],.8);self.assertEqual(result['extraction']['effect_type'],'RR')
        self.assertTrue(result['extraction']['effect_is_adjusted']);self.assertEqual(self.tracks,before)
        self.assertTrue(result['extraction']['direction_imputed']);self.assertEqual(len(items),1)

    def test_pdf_missing_quote_and_changed_source_all_rejected(self):
        for change in ('pdf','quote','hash','source'):
            entry=self.entry()
            if change=='pdf':entry['adjudication']['source']['text_path']=str(self.text.with_suffix('.pdf'))
            elif change=='quote':entry['adjudication']['evidence'][0]['quote']='invented quote'
            elif change=='hash':entry['adjudication']['source']['text_sha256']='0'*64
            else:entry['source_fingerprint']='0'*64
            result,_,audit=overlay.apply_context(self.prior,self.tracks,[entry])
            self.assertFalse(audit[0]['applied']);self.assertEqual(result,self.prior)

    def test_human_decisions_stay_byte_identical(self):
        self.prior['combined_manual_authority']=True
        result,_,audit=overlay.apply_context(self.prior,self.tracks,[self.entry()])
        self.assertEqual(result,self.prior);self.assertFalse(audit[0]['follow_up_required'])

    def test_fair_quality_stays_visible_and_unpooled(self):
        self.a['jbi']['grade']='Fair'
        result,items,audit=overlay.apply_context(self.prior,self.tracks,[self.entry()])
        self.assertTrue(audit[0]['applied']);self.assertEqual(result['final_status'],'ELIGIBLE_NOT_POOLABLE')
        self.assertFalse(reconcile.app_row(items[0])['policy_poolable'])

    def test_same_pair_extraction_scientific_hold_survives_numeric_replacement(self):
        self.a['extraction']['manual_poolable']=False
        result,items,audit=overlay.apply_context(self.prior,self.tracks,[self.entry()])
        self.assertTrue(audit[0]['applied']);self.assertFalse(result['extraction']['manual_poolable'])
        self.assertFalse(reconcile.app_row(items[0])['policy_poolable'])

    def test_prior_abstract_partial_pair_is_replaceable(self):
        self.a['extraction'].update(effect_size=None,ci_lower=None,ci_upper=None)
        self.prior,_,_=reconcile.reconcile_context(self.a,self.b)
        raw={'id':'abstract1','source_row':self.entry()['source_row'],'selected_pipeline':'chatgpt','adjudication':{'id':'abstract1','confidence':'high','source_quote_validated':True,'verdict':'A','eligibility':'INCLUDE','evidence_quote':'1000 participants','numeric_values':{}}}
        selected,_,_=abstract.apply_context(self.prior,self.tracks,[raw])
        self.assertIsNone(selected['extraction']['effect_size'])
        updated,_,audit=overlay.apply_context(selected,self.tracks,[self.entry()])
        self.assertTrue(audit[0]['applied']);self.assertEqual(updated['extraction']['effect_size'],.8)

    def test_unrelated_agreed_estimate_is_preserved(self):
        extra={**self.a['extraction'],'total_n':4000,'effect_size':2.0,'ci_lower':1.8,'ci_upper':2.2}
        self.a['additional_extractions']=[copy.deepcopy(extra)];self.b['additional_extractions']=[copy.deepcopy(extra)]
        prior,_,issues=reconcile.reconcile_context(self.a,self.b)
        entry=self.entry();entry['source_row']['Estimate pair']=str(issues[0]['estimate_pair'])
        updated,_,audit=overlay.apply_context(prior,self.tracks,[entry])
        agreed=next(ex for ex in common.policy.extracts(prior) if ex['total_n']==4000)
        self.assertTrue(audit[0]['applied']);self.assertIn(agreed,common.policy.extracts(updated))

    def test_idempotent_and_fingerprint_guarded_replay(self):
        result,_,_=overlay.apply_context(self.prior,self.tracks,[self.entry()])
        again,_,audit=overlay.apply_context(result,self.tracks,[self.entry()])
        self.assertEqual(again,result);self.assertTrue(audit[0]['replayed_existing_decision'])
        saved={'record':result,'record_sha256':common.digest(result),'source_fingerprint':common.source_digest(self.a,self.b)}
        path=self.root/common.CHECKPOINT_NAME;path.parent.mkdir(parents=True);path.write_text(json.dumps({'record_snapshots':{self.a['context_id']:saved}}))
        self.assertEqual(common.digest(overlay.replay_record(self.prior,self.a,self.b,self.root)),common.digest(result))
        self.b['extraction']['total_n']=9999
        self.assertEqual(overlay.replay_record(self.prior,self.a,self.b,self.root),self.prior)

    def test_incomplete_corrected_bundle_is_saved_without_pooling(self):
        entry=self.entry();entry['adjudication']['extraction']['total_n']=None
        updated,items,audit=overlay.apply_context(self.prior,self.tracks,[entry])
        self.assertTrue(audit[0]['applied']);self.assertTrue(audit[0]['follow_up_required'])
        self.assertFalse(reconcile.app_row(items[0])['policy_poolable']);self.assertIsNone(updated['extraction']['total_n'])

    def test_absent_numeric_evidence_is_rejected(self):
        entry=self.entry();entry['adjudication']['evidence'][0]['fields']=[]
        _,error=common.validate_entry(entry,self.a,self.b);self.assertIn('total_n',error)

    def test_non95_level_requires_support_in_ci_quote(self):
        entry=self.entry();entry['adjudication']['extraction']['ci_level']=99
        _,error=common.validate_entry(entry,self.a,self.b);self.assertIn('non-95%',error)
        text=self.text.read_text().replace('95%','99%');self.text.write_text(text)
        entry['adjudication']['source']['text_sha256']=hashlib.sha256(self.text.read_bytes()).hexdigest()
        entry['adjudication']['evidence'][1]['quote']=entry['adjudication']['evidence'][1]['quote'].replace('95%','99%')
        result,error=common.validate_entry(entry,self.a,self.b)
        self.assertIsNone(error);self.assertEqual(result['ci_confidence_level'],95)
        self.assertEqual(result['ci_original_confidence_level'],99)

    def test_source_match_audit_uses_same_canonical_direction(self):
        reported={'total_n':1000,'effect_type':'RR','effect_size':2.0,'ci_lower':1.5,'ci_upper':2.5,'comparison_direction':'lower_vs_higher'}
        saved={'total_n':1000,'effect_type':'RR','effect_size':.5,'ci_lower':.4,'ci_upper':1/1.5,'inversion_applied':True,'needs_inversion':True}
        self.assertEqual(common.canonical_numeric_snapshot(reported),common.canonical_numeric_snapshot(saved))

    def test_optional_metadata_fill_is_conservative_and_never_changes_numeric_bundle(self):
        ex={'total_n':1000,'effect_type':'RR','effect_size':.8,'ci_lower':.7,'ci_upper':.9,'comparison_direction':'higher_vs_lower'}
        a={**ex,'unit':'mg/day','country':'Canada','design':'Cohort','cases':200,'p_value':.9,'stratum':'Overall'}
        b={**ex,'unit':'mg/day','country':None,'design':'Cohort','cases':300,'p_value':.8,'stratum':'Overall'}
        filled=common.fill_compatible_metadata(ex,{'chatgpt':a,'claude_gemini':b},{})
        self.assertEqual(filled['country'],'Canada');self.assertEqual(filled['unit'],'mg/day')
        self.assertEqual(filled['design'],'Cohort');self.assertNotIn('cases',filled);self.assertNotIn('p_value',filled)
        self.assertNotIn('stratum',filled)
        for key in common.policy.KEY_FIELDS:self.assertEqual(filled[key],ex[key])
        self.assertTrue(filled['astra_fulltext_metadata_fills'])

    def test_metadata_conflicts_mismatched_numbers_and_invalidations_are_not_filled(self):
        ex={'total_n':1000,'effect_type':'RR','effect_size':.8,'ci_lower':.7,'ci_upper':.9,'design':'Case-control','comparison_direction':'higher_vs_lower'}
        a={**ex,'total_n':2000,'unit':'mg/day','country':'Canada','study_design':'Cohort'}
        b={**ex,'total_n':3000,'unit':'g/day','country':'France','study_design':'Cohort'}
        filled=common.fill_compatible_metadata(ex,{'chatgpt':a,'claude_gemini':b},{})
        self.assertNotIn('unit',filled);self.assertNotIn('country',filled);self.assertNotIn('study_design',filled)
        a['country']=b['country']='Canada'
        filled=common.fill_compatible_metadata(ex,{'chatgpt':a,'claude_gemini':b},{'invalidated_metadata_fields':['country']})
        self.assertNotIn('country',filled)

    def test_fulltext_measurement_correction_cannot_gain_conflicting_dose_units(self):
        ex={'total_n':1000,'effect_type':'RR','effect_size':.8,'ci_lower':.7,'ci_upper':.9,'exposure_measurement_type':'serum'}
        donor={**ex,'exposure_measurement_type':'dietary_intake','unit':'mg/day'}
        filled=common.fill_compatible_metadata(ex,{'chatgpt':donor,'claude_gemini':None},{})
        self.assertNotIn('unit',filled);self.assertEqual(filled['exposure_measurement_type'],'serum')

    def test_unchanged_malformed_source_array_can_be_replaced_by_supported_scalar(self):
        self.a['extraction']['effect_size']=[1.1,1.2]
        entry=self.entry();entry['source_row']['ChatGPT effect size']='[1.1, 1.2]'
        result,error=common.validate_entry(entry,self.a,self.b)
        self.assertIsNone(error);self.assertEqual(result['effect_size'],.8)
        entry['source_row']['ChatGPT effect size']='[1.1, 1.3]'
        _,error=common.validate_entry(entry,self.a,self.b);self.assertIn('source numeric bundle changed',error)

    def test_failed_staged_cache_build_cannot_install_decisions(self):
        from scripts import apply_astra_fulltext as apply
        source=self.root/'source';source.mkdir()
        for track,row in [('chatgpt',self.a),('claude_gemini',self.b)]:
            (source/(track+'_effective_results.json')).write_text(json.dumps({'results':[row]}))
        combined=self.root/'combined';combined.mkdir();target=combined/'effective_results.json'
        target.write_text(json.dumps({'results':[self.prior]}));before=target.read_bytes()
        packet=self.root/'input.json';packet.write_text(json.dumps({'model':common.MODEL,'source_scope':common.SOURCE_SCOPE,'complete':True,'results':[self.entry()]}))
        with patch.object(apply,'ROOT',self.root),patch.object(reconcile,'ROOT',self.root),patch.object(reconcile,'SOURCE_DIR',source),patch.object(reconcile,'materialize',return_value={'errors':[{'error':'synthetic failure'}]}):
            with self.assertRaises(RuntimeError):apply.run(packet,self.root/'output',apply=True)
        self.assertEqual(target.read_bytes(),before)
        self.assertFalse((combined/common.CHECKPOINT_NAME).exists())


if __name__=='__main__':unittest.main()
