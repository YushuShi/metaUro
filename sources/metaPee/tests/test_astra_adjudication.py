"""Validated abstract adjudication preserves independent estimates and source gates."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import apply_astra_adjudication as astra
from scripts import reconcile_screening_tracks as reconcile

class AstraApplicationTests(unittest.TestCase):
    def extraction(self,effect=1.2,n=1000):
        return {'effect_type':'RR','effect_size':effect,'ci_lower':effect*.8,'ci_upper':effect*1.2,'total_n':n,'cases':50,
            'comparison_type':'High versus low','comparison_direction':'higher_vs_lower','exposure_measurement_type':'dietary_intake','needs_inversion':False}
    def record(self,effect=1.2,quality='Moderate',extra=None):
        row={'context_id':'123|Dairy|Example','pmid':'123','exposure':'Dairy','disease':'Example','cancer':'Example','endpoint':'Incidence',
            'eligibility':'INCLUDE','final_status':'INCLUDE_META','jbi':{'grade':quality},'extraction':self.extraction(effect)}
        if extra:row['additional_extractions']=[copy.deepcopy(extra)]
        return row
    def state(self,quality='Moderate',extra=None):
        a=self.record(1.2,quality,extra);b=self.record(1.5,quality,extra)
        tracks={'chatgpt':{a['context_id']:a},'claude_gemini':{b['context_id']:b}}
        combined,_,issues=reconcile.reconcile_context(a,b)
        return combined,tracks,issues
    def entry(self,pair=1,verdict='A',eligibility='INCLUDE',confidence='high',quote=True):
        return {'id':'row'+str(pair),'source_row':{'Context ID':'123|Dairy|Example','PMID':'123','Exposure':'Dairy','Disease':'Example','Estimate pair':str(pair),'ChatGPT estimate index':'0','Claude+Gemini estimate index':'0'},
            'selected_pipeline':{'A':'chatgpt','B':'claude_gemini'}.get(verdict),
            'adjudication':{'id':'row'+str(pair),'verdict':verdict,'eligibility':eligibility,'confidence':confidence,'source_quote_validated':quote,
                'evidence_quote':'The study included 1000 participants and reported risk ratio 1.20.','numeric_values':{},'effect_type':'RR'}}
    def test_low_confidence_and_unvalidated_quote_leave_combined_exactly_unchanged(self):
        for confidence,quote in [('medium',True),('high',False)]:
            current,tracks,_=self.state();before=copy.deepcopy((current,tracks))
            result,_,audit=astra.apply_context(current,tracks,[self.entry(confidence=confidence,quote=quote)])
            self.assertEqual(result,current);self.assertEqual((current,tracks),before);self.assertFalse(audit[0]['applied'])
    def test_high_confidence_selection_preserves_source_packets_and_releases_only_selected_pair(self):
        current,tracks,_=self.state();before=copy.deepcopy(tracks)
        result,items,audit=astra.apply_context(current,tracks,[self.entry()])
        self.assertEqual(result['extraction']['effect_size'],1.2)
        self.assertFalse(result['requires_key_review']);self.assertEqual(len(items),1)
        self.assertEqual(tracks,before);self.assertTrue(audit[0]['applied'])
        self.assertFalse(result['combined_numeric_consensus'])
        self.assertEqual(result['combined_accepted_estimate_sources'][0]['source'],'chatgpt')
        self.assertEqual(astra.candidates_for_record(result,tracks)[0]['extraction'],items[0]['extraction'])
    def test_blinded_candidate_letters_follow_the_validated_pipeline_mapping(self):
        current,tracks,_=self.state();entry=self.entry(verdict='A');entry['selected_pipeline']='claude_gemini'
        result,_,audit=astra.apply_context(current,tracks,[entry])
        self.assertTrue(audit[0]['applied']);self.assertEqual(result['extraction']['effect_size'],1.5)
        entry['selected_pipeline']=None
        result,_,audit=astra.apply_context(current,tracks,[entry]);self.assertEqual(result,current)

    def test_existing_manual_authority_wins_even_against_validated_astra(self):
        current,tracks,_=self.state();current['combined_manual_authority']=True
        result,_,audit=astra.apply_context(current,tracks,[self.entry()])
        self.assertEqual(result,current);self.assertFalse(audit[0]['applied'])
    def test_fair_and_explicit_clinical_block_remain_unpooled(self):
        for quality,block in [('Fair',False),('Moderate',True)]:
            current,tracks,_=self.state(quality)
            if block:tracks['chatgpt'][current['context_id']]['full_text_pooling_block']=True
            result,items,_=astra.apply_context(current,tracks,[self.entry()])
            self.assertFalse(reconcile.app_row(items[0])['policy_poolable'])
            self.assertEqual(result['final_status'],'ELIGIBLE_NOT_POOLABLE')
            self.assertEqual(items[0]['source_record']['jbi']['grade'],quality)
    def test_unrelated_agreed_estimate_survives_partial_adjudication(self):
        current,tracks,issues=self.state(extra=self.extraction(.8,4000))
        pair=issues[0]['estimate_pair'];entry=self.entry(pair)
        result,items,_=astra.apply_context(current,tracks,[entry])
        self.assertEqual({item['extraction']['effect_size'] for item in items},{.8,1.2})
        self.assertEqual(len(astra.policy.extracts(result)),2)
    def test_excluding_one_disputed_pair_cannot_erase_an_agreed_estimate(self):
        current,tracks,issues=self.state(extra=self.extraction(.8,4000))
        result,items,_=astra.apply_context(current,tracks,[self.entry(issues[0]['estimate_pair'],eligibility='EXCLUDE')])
        self.assertEqual(len(items),1);self.assertEqual(result['extraction']['effect_size'],.8)
        self.assertNotEqual(result['final_status'],'EXCLUDE')
    def test_all_disputed_pairs_excluded_leaves_no_old_candidate(self):
        current,tracks,_=self.state()
        result,items,_=astra.apply_context(current,tracks,[self.entry(eligibility='EXCLUDE')])
        self.assertEqual(result['final_status'],'EXCLUDE');self.assertEqual(items,[])
        self.assertEqual(result['key_review_candidates'],[])
    def test_wrong_source_index_or_numeric_bundle_remains_unresolved(self):
        for change in ['index','numeric','type','stale_csv']:
            current,tracks,_=self.state();entry=self.entry()
            if change=='index':entry['source_row']['ChatGPT estimate index']='2'
            elif change=='numeric':entry['adjudication']['numeric_values']={'effect_size':1.9}
            elif change=='type':entry['adjudication']['effect_type']='OR'
            else:entry['source_row']['ChatGPT sample size']='2000'
            result,_,audit=astra.apply_context(current,tracks,[entry])
            self.assertEqual(result,current);self.assertFalse(audit[0]['applied'])
    def test_validated_partial_source_bundle_is_saved_but_requires_quantitative_followup(self):
        current,tracks,_=self.state()
        tracks['chatgpt'][current['context_id']]['extraction']['ci_upper']=None
        current,_,_=reconcile.reconcile_context(tracks['chatgpt'][current['context_id']],tracks['claude_gemini'][current['context_id']])
        result,items,audit=astra.apply_context(current,tracks,[self.entry()])
        self.assertTrue(audit[0]['applied']);self.assertTrue(audit[0]['follow_up_required'])
        self.assertEqual(result['extraction']['effect_size'],1.2);self.assertIsNone(result['extraction']['ci_upper'])
        self.assertEqual(result['final_status'],'ELIGIBLE_NOT_POOLABLE')
        self.assertFalse(reconcile.app_row(items[0])['policy_poolable'])

    def test_neither_requires_complete_explicit_bundle_and_keeps_source_provenance(self):
        current,tracks,_=self.state();entry=self.entry(verdict='NEITHER')
        entry['adjudication']['numeric_values']={'sample_size':2000,'effect_size':.9,'ci_lower':.8}
        result,_,audit=astra.apply_context(current,tracks,[entry]);self.assertEqual(result,current)
        entry['adjudication']['numeric_values']['ci_upper']=1.1
        result,items,audit=astra.apply_context(current,tracks,[entry])
        self.assertTrue(audit[0]['applied']);self.assertEqual(result['extraction']['effect_size'],.9)
        self.assertEqual(result['extraction']['combined_replaced_extraction']['effect_size'],1.5)
    def test_duplicate_and_mixed_exclusion_decisions_do_not_exclude_whole_context(self):
        current,tracks,_=self.state();entry=self.entry();duplicate=copy.deepcopy(entry)
        duplicate['adjudication']['eligibility']='EXCLUDE'
        result,items,audit=astra.apply_context(current,tracks,[entry,duplicate])
        self.assertEqual(sum(item['applied'] for item in audit),0)
        self.assertEqual(result,current)
        reverse,_,reverse_audit=astra.apply_context(current,tracks,[duplicate,entry])
        self.assertEqual(reverse,current);self.assertFalse(any(row['applied'] for row in reverse_audit))
    def fixture_files(self,root):
        current,tracks,_=self.state()
        disease={'metaBreath':'Lung and bronchus cancer','metaPee':'Bladder cancer','MetaCutis_et_ Capillus':'Acne vulgaris'}[astra.ROOT.name]
        cid='123|Dairy|'+disease
        current.update(context_id=cid,disease=disease,cancer=disease)
        for candidate in current['key_review_candidates']:candidate['source_context_id']=cid
        entry=self.entry();entry['source_row'].update({'Context ID':cid,'Disease':disease})
        for track,mapping in tracks.items():
            row=next(iter(mapping.values()));row.update(context_id=cid,disease=disease,cancer=disease)
            astra.dump(root/'source'/f'{track}_effective_results.json',{'results':[row]})
        astra.dump(root/'data/exposure_hierarchy.json',{})
        astra.dump(root/'combined/effective_results.json',{'canonical_track':'combined','results':[current]})
        astra.dump(root/'packet.json',{'model':'gpt-6-astra','source_scope':'TITLE_ABSTRACT_ONLY','results':[entry]})
        return disease
    def test_staged_apply_updates_combined_modes_without_changing_sources(self):
        from scripts import rebuild_relaxed_poolability as rebuild
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);disease=self.fixture_files(root)
            original_sources={path:path.read_bytes() for path in (root/'source').glob('*.json')}
            slug=lambda value:reconcile.re.sub('[^a-zA-Z0-9]+','_',value.lower()).strip('_')
            paths=lambda exposure,disease,endpoint:[root/'Cached_results'/slug(exposure)/(slug(disease)+'_'+slug(endpoint)+'_true_'+suffix+'.json') for suffix in ('all','core')]
            with patch.object(astra,'ROOT',root),patch.object(reconcile,'ROOT',root),patch.object(reconcile,'SOURCE_DIR',root/'source'),patch.object(rebuild,'cache_paths',paths):
                report=astra.run(root/'packet.json',root/'application',apply=True)
            self.assertTrue(report['source_packets_verified_unchanged']);self.assertEqual(report['changed_contexts'],1)
            self.assertTrue((root/'application/before/combined/effective_results.json').is_file())
            combined=astra.read(root/'combined/effective_results.json')['results'][0]
            self.assertEqual(combined['extraction']['effect_size'],1.2)
            for mode in ('yes','no'):
                caches=list((root/'combined/subexposure_views'/mode/'Cached_results').glob('*/*_true_all.json'))
                self.assertEqual(len(caches),1)
                row=astra.read(caches[0])['studies'][0]
                self.assertEqual(row['Effect Size'],1.2);self.assertFalse(row['requires_key_review'])
            for path,before in original_sources.items():self.assertEqual(path.read_bytes(),before)
    def test_materialization_failure_cannot_partially_replace_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.fixture_files(root)
            before=(root/'combined/effective_results.json').read_bytes()
            with patch.object(astra,'ROOT',root),patch.object(reconcile,'ROOT',root),patch.object(reconcile,'SOURCE_DIR',root/'source'),patch.object(reconcile,'materialize',return_value={'errors':[{'error':'synthetic failure'}]}):
                with self.assertRaises(RuntimeError):astra.run(root/'packet.json',root/'application',apply=True)
            self.assertEqual((root/'combined/effective_results.json').read_bytes(),before)
            self.assertFalse((root/'combined/subexposure_views').exists())

    def test_applied_overlay_is_idempotent(self):
        current,tracks,_=self.state();entry=self.entry()
        result,_,_=astra.apply_context(current,tracks,[entry])
        again,_,audit=astra.apply_context(result,tracks,[entry])
        self.assertEqual(result,again)
        self.assertTrue(audit[0]['applied']);self.assertFalse(audit[0]['follow_up_required'])
        self.assertTrue(audit[0]['replayed_existing_decision'])
        excluded,_,_=astra.apply_context(current,tracks,[self.entry(eligibility='EXCLUDE')])
        again,_,audit=astra.apply_context(excluded,tracks,[self.entry(eligibility='EXCLUDE')])
        self.assertEqual(excluded,again);self.assertTrue(audit[0]['applied'])

if __name__=='__main__':unittest.main()
