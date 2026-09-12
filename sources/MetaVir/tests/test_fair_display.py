import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import pandas as pd
from scripts.display_eligibility import eligible_fair, display_row
from scripts import monthly_evidence_update as monthly
import meta_analysis
ROOT=Path(__file__).resolve().parents[1]

class FairDisplayTests(unittest.TestCase):
    def item(self):
        return dict(pmid='123456', context_id='123456|Vitamin A|Test disease', exposure='Vitamin A', cancer='Test disease',
                    disease='Test disease', endpoint='Incidence', authors=['Example A'], published_date='2020',
                    eligibility='INCLUDE', final_status='ELIGIBLE_NOT_POOLABLE', jbi={'grade':'Fair'}, extraction={})

    def test_missing_quantitative_evidence_is_visible_without_fabrication(self):
        item=self.item();before=copy.deepcopy(item)
        row=monthly.study_row(item)
        self.assertEqual(row['Quality Score'],'Fair');self.assertTrue(row['display_only'])
        for k in ['Sample Size','Cases','Effect Size','Lower CI','Upper CI']:self.assertIsNone(row[k])
        self.assertFalse(row['policy_poolable']);self.assertFalse(row['Primary Estimate'])
        self.assertEqual(item,before)

    def test_clinical_exclusion_never_enters_fair_projection(self):
        for change in [{'final_status':'EXCLUDE'}, {'eligibility':'EXCLUDE'}, {'jbi':{'grade':'Poor'}}]:
            self.assertFalse(eligible_fair(dict(self.item(),**change)))

    def test_fair_only_scope_succeeds_with_table_and_no_pooled_effect(self):
        row=display_row(self.item());frame=pd.DataFrame([row])
        result=meta_analysis.perform_meta_analysis(frame,'Test disease','Vitamin A',outcome='Incidence',df_all=frame,generate_plots=False)
        self.assertTrue(result['success']);self.assertIsNone(result['headline']);self.assertIsNone(result['plot_url'])
        self.assertEqual(result['studies'][0]['PMID'],'123456')

    def test_visible_fair_does_not_change_pooled_effect(self):
        fair=display_row(self.item())
        good=dict(fair,PMID='654321',Study='Control et al. (2020)',display_only=False,policy_poolable=True,manual_poolable=True,
                  **{'Primary Estimate':True,'pool_in_parent_meta_analysis':True,'Quality Score':'Good','Effect Type':'RR',
                     'Effect Size':0.8,'Lower CI':0.6,'Upper CI':1.0,'Sample Size':10000,'Cases':100,'Estimated Cases':100,'SE':0.130309})
        fair.update({'Effect Type':'RR','Effect Size':20.,'Lower CI':10.,'Upper CI':30.,'Sample Size':10000,'Cases':100,'SE':.3})
        a=pd.DataFrame([good]);b=pd.DataFrame([good,fair])
        kwargs=dict(disease='Test disease',exposure='Vitamin A',outcome='Incidence',generate_plots=False)
        with patch.object(meta_analysis,'baseline_risk_for_disease',return_value=.01):
            baseline=meta_analysis.perform_meta_analysis(a,df_all=a,**kwargs)
            displayed=meta_analysis.perform_meta_analysis(b,df_all=b,**kwargs)
        self.assertTrue(baseline.get('success'),baseline)
        self.assertEqual(baseline.get('headline'),displayed.get('headline'))
        self.assertEqual(len(displayed['studies']),2)

    def test_default_table_filter_shows_fair_but_pooling_quality_stays_moderate(self):
        source=(ROOT/'static/script.js').read_text()
        html=(ROOT/'templates/index.html').read_text()
        self.assertIn('<option value="Moderate+" selected>',html)
        self.assertNotIn('<option value="Fair+"',html)
        self.assertIn("quality_filter: selectedPoolingQuality()",source)
        self.assertNotRegex(source,r"study\['(?:Effect Size|Lower CI|Upper CI)'\] \|\| (?:1|0\.5|1\.5)")
        # Execute the real filtering function: Fair stays visible, Good remains selected quality.
        start=source.index('    function applyFilters() {')
        end=source.index('\n    function ',start+10)
        code=source[start:end]
        js="""const assert=require('assert');
let allStudies=[{'Quality Score':'Fair'}, {'Quality Score':'Good'}], currentStudies=[];
const elements={filterQuality:{value:'Moderate+'}};
function meetsDefaultJbiThreshold(s){return ['Moderate','Good'].includes(s['Quality Score']);}
function sortCurrentStudies(){};function renderStudiesTable(){};
"""+code+"\napplyFilters();assert.equal(currentStudies.length,2);assert.equal(currentStudies.filter(meetsDefaultJbiThreshold).length,1);"
        result=subprocess.run(['node','-e',js],text=True,capture_output=True,cwd=ROOT)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_fair_only_refresh_clears_previous_pooled_results_and_plots(self):
        source=(ROOT/'static/script.js').read_text()
        start=source.index('    function clearPreviousAnalysis(data) {')
        end=source.index('\n    function ',start+10)
        code=source[start:end]
        js="""const assert=require('assert');let lastHeadlineData={pooled_es:2},lastHeadlineStudyCount=3;
function el(){return {src:'old.png',textContent:'2',innerHTML:'old',hidden:false,
  classList:{add(){this.hidden=true},toggle(k,v){this.hidden=v}},removeAttribute(k){delete this[k]}};}
const elements={};for(const name of ['headlineResult','heterogeneityStats','influenceSection','pooledPowerAnalysis','pooledEs','pooledCi','valI2','valTau2','valEggers','looTbody','forestPlot','funnelPlot','baujatPlot'])elements[name]=el();
const notice=el();const document={getElementById(){return notice}};
"""+code+"""
clearPreviousAnalysis({headline:null});assert.equal(lastHeadlineData,null);assert.equal(lastHeadlineStudyCount,null);
assert.equal(elements.pooledEs.textContent,'NA');assert.equal(elements.forestPlot.src,undefined);
assert.equal(elements.funnelPlot.src,undefined);assert.equal(elements.baujatPlot.src,undefined);
assert.equal(elements.looTbody.innerHTML,'');assert.equal(notice.classList.hidden,false);
"""
        result=subprocess.run(['node','-e',js],text=True,capture_output=True,cwd=ROOT)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_future_cache_builder_keeps_fair_without_promoting_status(self):
        item=self.item();before=copy.deepcopy(item)
        from scripts import build_combined_results as build
        grouped,_,_=build.build_grouped_studies({item['context_id']:item},{},{item['context_id']},monthly)
        row=grouped['Vitamin A'][0]
        self.assertTrue(row['display_only']);self.assertFalse(row['policy_poolable'])
        self.assertEqual(item,before)

    def test_disabled_fair_cannot_be_sent_by_select_all(self):
        source=(ROOT/'static/script.js').read_text()
        start=source.index('    function selectedStudiesFromTable() {')
        end=source.index('\n    ',source.index('\n    }',start)+6)
        code=source[start:end]
        js="""const assert=require('assert');const currentStudies=[{PMID:'fair'},{PMID:'good'}];
const document={querySelectorAll(){return [{checked:true,disabled:true,dataset:{index:'0'}},{checked:true,disabled:false,dataset:{index:'1'}}]}};
"""+code+"\nconst selected=selectedStudiesFromTable();assert.equal(selected.length,1);assert.equal(selected[0].PMID,'good');"
        result=subprocess.run(['node','-e',js],text=True,capture_output=True,cwd=ROOT)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("querySelectorAll('.study-checkbox:not(:disabled)')",source)
        self.assertIn("!fairQualityOptIn && (study.display_only === true",source)

if __name__=='__main__':unittest.main()
