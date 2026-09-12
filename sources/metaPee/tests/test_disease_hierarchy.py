"""Saved disease evidence inheritance, conservative overlap protection and API checks."""
import copy
import hashlib
import json
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import app
import disease_hierarchy as hierarchy
import meta_analysis

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT.name == 'MetaCutis_et_ Capillus'
PARENT = 'skin_cancer' if SKIN else 'kidney_and_renal_pelvis_cancer'
CHILD = 'cutaneous_melanoma' if SKIN else 'renal_cell_carcinoma'
OTHER = 'basal_cell_carcinoma' if SKIN else 'clear_cell_renal_cell_carcinoma'

class DiseaseHierarchyTests(unittest.TestCase):
    def row(self, pmid='1', effect=.8, **changes):
        result = {'PMID':pmid,'Study':f'Example {pmid} et al. (2020)', 'Effect Type':'RR',
            'Effect Size':effect,'Lower CI':effect*.8,'Upper CI':effect*1.2,'Sample Size':1000,
            'Cases':50,'Quality Score':'Moderate','SE':.1,'Gender':'All','Primary Estimate':True,
            'manual_poolable':True,'policy_poolable':True,'pool_in_parent_meta_analysis':True,
            'exposure_measurement_type':'dietary_intake','Endpoint':'Incidence'}
        result.update(changes)
        return result

    def payload(self, rows, key=CHILD):
        return {'success':True,'canonical_track':'chatgpt','studies':rows,'headline':None,
            'disease':app.get_scope_config()[key]['disease'],'cancer':app.get_scope_config()[key]['disease'],
            'exposure':'Vitamin D','endpoint':'Incidence','outcome':'Incidence'}

    def composed(self, parent=None, children=None):
        children = children if children is not None else [(CHILD,self.payload([self.row()]),'child.json')]
        return hierarchy.compose_result(parent, children,PARENT,app.get_scope_config(),'Vitamin D','Incidence')

    def test_registry_preserves_exact_identity_and_nested_hierarchy(self):
        scopes=app.get_scope_config()
        self.assertEqual(scopes[CHILD]['parent_key'],PARENT)
        self.assertIn(CHILD,hierarchy.descendant_keys(scopes,PARENT))
        if SKIN:
            self.assertEqual(scopes[PARENT]['disease'],'Skin cancer')
            self.assertIsNone(meta_analysis.baseline_risk_for_disease('Skin cancer'))
            self.assertEqual(scopes['atopic_dermatitis']['label'],'Atopic dermatitis (eczema)')
            self.assertNotIn('atopic_dermatitis',hierarchy.descendant_keys(scopes,PARENT))
        else:
            self.assertEqual(scopes[OTHER]['parent_key'],CHILD)
            self.assertEqual(scopes[OTHER]['hierarchy_depth'],2)
            self.assertEqual(len(hierarchy.descendant_keys(scopes,PARENT)),6)
        scopes[CHILD]['children'].append('invalid')
        self.assertNotIn('invalid',app.get_scope_config()[CHILD]['children'])

    def test_parent_empty_includes_quantitative_child_without_direction_or_ci_level(self):
        result=self.composed()
        self.assertEqual(result['disease_hierarchy_pooling_count'],1)
        row=result['studies'][0]
        self.assertTrue(row['disease_hierarchy_inherited'])
        self.assertTrue(row['disease_hierarchy_pooling_selected'])
        self.assertNotIn('effect_direction',row)
        self.assertNotIn('confidence_level',row)
        self.assertIn('proxy',row['disease_hierarchy_reference_basis'])
        frame=pd.DataFrame(result['studies'])
        pooled=meta_analysis.perform_meta_analysis(frame,app.get_scope_config()[PARENT]['disease'],
            'Vitamin D',outcome='Incidence',exclude_meta=True,df_all=frame,generate_plots=False)
        self.assertIsNotNone(pooled.get('headline'),pooled)
        self.assertAlmostEqual(pooled['headline']['pooled_es'],.8,places=2)

    def test_overlapping_parent_preferred_without_hiding_child_source(self):
        original=self.payload([self.row()],PARENT)
        children=[(CHILD,self.payload([self.row(effect=1.4)]),'child.json')]
        before=copy.deepcopy((original,children))
        result=self.composed(original,children)
        self.assertEqual((original,children),before)
        self.assertEqual(len(result['studies']),2)
        selected=[r for r in result['studies'] if r['disease_hierarchy_pooling_selected']]
        self.assertEqual(len(selected),1)
        self.assertFalse(selected[0]['disease_hierarchy_inherited'])
        self.assertEqual({r['Disease Evidence Source'] for r in result['studies']},
            {app.get_scope_config()[key]['disease'] for key in (PARENT,CHILD)})

    def test_adjusted_child_wins_overlap_and_documented_disjoint_populations_survive(self):
        a=self.row(); b=self.row(effect=.9,effect_is_adjusted=True)
        def result_rows():
            return self.composed(children=[(CHILD,self.payload([a]),'a'),(OTHER,self.payload([b]),'b')])['studies']
        selected=[r for r in result_rows() if r['disease_hierarchy_pooling_selected']]
        self.assertEqual(len(selected),1);self.assertTrue(selected[0]['effect_is_adjusted'])
        a['Sample Size']=2000
        self.assertEqual(sum(r['disease_hierarchy_pooling_selected'] for r in result_rows()),1)
        a['Gender']='Male';b['Gender']='Female'
        self.assertEqual(sum(r['disease_hierarchy_pooling_selected'] for r in result_rows()),2)
        a['Gender']=b['Gender']='All'
        a['independent_population_id']='A';b['independent_population_id']='B'
        self.assertEqual(sum(r['disease_hierarchy_pooling_selected'] for r in result_rows()),2)
        del b['independent_population_id']
        self.assertEqual(sum(r['disease_hierarchy_pooling_selected'] for r in result_rows()),1)

    def test_saved_quality_manual_clinical_and_exposure_gates_remain(self):
        for changes in [{'Quality Score':'Fair'},{'manual_poolable':False},{'policy_poolable':False},
            {'display_only':True},{'final_status':'EXCLUDE'},{'subexposure_pooling_selected':False},
            {'Sample Size':None},{'Lower CI':None},{'pool_in_parent_meta_analysis':False},
            {'Effect Type':'SMD'}]:
            result=self.composed(parent=self.payload([self.row(**changes)],PARENT))
            selected=[r for r in result['studies'] if r['disease_hierarchy_pooling_selected']]
            self.assertEqual(len(selected),1,changes);self.assertTrue(selected[0]['disease_hierarchy_inherited'])
        payload=self.payload([self.row()]);payload['canonical_track']='other'
        self.assertIsNone(self.composed(children=[(CHILD,payload,'wrong.json')]))

    def test_subtype_context_exclusions_cannot_be_reintroduced_via_parent(self):
        scopes=app.get_scope_config()
        def exclude(frame,disease,exposure,endpoint):
            return frame.iloc[0:0] if disease==scopes[CHILD]['disease'] else frame
        result=hierarchy.compose_result(None,[(CHILD,self.payload([self.row()]),'child')],
            PARENT,scopes,'Vitamin D','Incidence',source_filter=exclude)
        self.assertEqual(len(result['studies']),1)
        self.assertFalse(result['studies'][0]['disease_hierarchy_pooling_selected'])
        self.assertTrue(result['studies'][0]['disease_hierarchy_source_context_excluded'])

    def test_user_can_choose_alternate_child_but_never_pool_overlap_twice(self):
        result=self.composed(parent=self.payload([self.row()],PARENT))
        all_frame=pd.DataFrame(result['studies'])
        selected,returned=hierarchy.select_frames(all_frame.iloc[[1]],all_frame)
        self.assertEqual(len(selected),1)
        self.assertFalse(returned.iloc[0]['disease_hierarchy_pooling_selected'])
        self.assertTrue(returned.iloc[1]['disease_hierarchy_pooling_selected'])
        self.assertIn('Selected eligible',returned.iloc[1]['disease_hierarchy_pooling_reason'])
        selected,_=hierarchy.select_frames(all_frame,all_frame)
        self.assertEqual(len(selected),1)
        plain=pd.DataFrame([self.row(),self.row('2')]);plain['disease_hierarchy_projection_id']='-'
        returned,_=hierarchy.select_frames(plain,plain)
        self.assertEqual(len(returned),2)

    def test_api_child_only_parent_reanalysis_and_source_bytes_unchanged(self):
        scopes=app.get_scope_config();parent=scopes[PARENT]['disease'];child=scopes[CHILD]['disease']
        with tempfile.TemporaryDirectory() as temporary, patch.object(app,'CACHE_DIR',temporary), patch.object(app,'log_event'):
            source=Path(app.get_cache_path(child,'Vitamin D','Incidence',True,False))
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(self.payload([self.row()])))
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            client=app.app.test_client()
            args={'disease':parent,'exposure':'Vitamin D','endpoint':'Incidence','outcome':'Incidence',
                'exclude_meta':True,'include_subexposures':'no'}
            response=client.post('/analyze',json=args);data=response.get_json()
            self.assertEqual(response.status_code,200,data)
            self.assertEqual(len(data['studies']),1)
            self.assertIsNotNone(data['canonical_rr']['pooled_es'],data)
            self.assertTrue(data['studies'][0]['Settings Pooling Selected'])
            self.assertTrue(data['studies'][0]['disease_hierarchy_inherited'])
            if SKIN:
                self.assertIsNone(data['baseline_risk']['p0'])
                self.assertTrue(data['baseline_risk']['identity_approximation'])
                contexts=app.saved_evidence_catalog()
                self.assertTrue(any(r['disease']==parent for r in contexts))
                self.assertEqual(app.find_review_study('1',parent,'Vitamin D','Incidence')['PMID'],'1')
            else:
                self.assertIn('Vitamin D',app.get_cached_result_inventory()[PARENT])
            response=client.post('/reanalyze',json={**args,'studies':data['studies']})
            repeated=response.get_json();self.assertEqual(response.status_code,200,repeated)
            self.assertAlmostEqual(repeated['canonical_rr']['pooled_es'],data['canonical_rr']['pooled_es'],places=8)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),digest)
            self.assertEqual(len(list(Path(temporary).rglob('*.json'))),1)

    def test_browser_origin_cell_executes_and_escapes_saved_disease_text(self):
        script = r"""
const fs=require('fs'),assert=require('assert');
const source=fs.readFileSync('static/script.js','utf8');
const helper=source.match(/function escapeHtml\(value\)\s*\{[\s\S]*?^\s*\}\s*$/m);
assert(helper,'The disease-origin renderer requires its escapeHtml helper.');
const cell=source.split('\n').find(line=>line.includes('class="disease-evidence-origin"')).trim();
const render=new Function('study','uiText',helper[0]+'; return `'+cell+'`;');
const row={'Disease Evidence Source':'Subtype <script>x</script> & evidence',disease_hierarchy_projection_id:'a'.repeat(64),
 disease_hierarchy_inherited:true,disease_hierarchy_pooling_selected:false,disease_hierarchy_pooling_reason:'Another "estimate" overlaps'};
const html=render(row,text=>text);
assert(html.includes('included in general category'));assert(html.includes('not selected for pooling'));
assert(html.includes('&lt;script&gt;'));assert(!html.includes('<script>'));
assert(html.includes('&quot;estimate&quot;'));
assert.equal(render({...row,disease_hierarchy_projection_id:'-'},text=>text),'');
"""
        result=subprocess.run(['node','-e',script],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_identical_parent_child_effect_has_only_one_settings_and_forest_row(self):
        scopes=app.get_scope_config();parent=scopes[PARENT]['disease']
        with tempfile.TemporaryDirectory() as temporary, patch.object(app,'CACHE_DIR',temporary), patch.object(app,'log_event'):
            for key in (PARENT,CHILD):
                source=Path(app.get_cache_path(scopes[key]['disease'],'Vitamin D','Incidence',True,False))
                source.parent.mkdir(parents=True,exist_ok=True);source.write_text(json.dumps(self.payload([self.row()],key)))
            response=app.app.test_client().post('/analyze',json={'disease':parent,'exposure':'Vitamin D',
                'endpoint':'Incidence','outcome':'Incidence','exclude_meta':True,'include_subexposures':'no'})
            data=response.get_json();self.assertEqual(response.status_code,200,data)
            self.assertEqual(len(data['studies']),2)
            self.assertEqual(sum(r['Settings Pooling Selected'] for r in data['studies']),1)
            self.assertEqual(len(data['forest_annotations']),2) # One estimate plus pooled result.

if __name__=='__main__':unittest.main()
