import copy,json,math,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from scripts import subexposure_policy as policy
from scripts import exposure_taxonomy as taxonomy
import app
import meta_analysis
ROOT=Path(__file__).resolve().parents[1]

class SubexposureTests(unittest.TestCase):
    def row(self,pmid='1',effect=.8,**kwargs):
        row={'PMID':pmid,'Study':f'Example {pmid} et al. (2020)','Effect Type':'RR','Effect Size':effect,
             'Lower CI':effect*.8,'Upper CI':effect*1.2,'Sample Size':1000,'Cases':None,'Estimated Cases':None,
             'Quality Score':'Good','SE':.1,'Gender':'All','Primary Estimate':True,'manual_poolable':True,
             'policy_poolable':True,'pool_in_parent_meta_analysis':True,'exposure_measurement_type':'human_biospecimen'}
        row.update(kwargs);return row

    def test_transitive_diamond_closure_and_cycle_detection(self):
        h={'Child':['Parent'],'Other':['Parent'],'Grandchild':['Child','Other']}
        self.assertEqual(set(policy.descendants('Parent',h)),{'Child','Other','Grandchild'})
        with self.assertRaises(ValueError):policy.descendants('A',{'A':['B'],'B':['A']})

    def test_yes_parent_superset_no_direct_and_raw_decisions_preserved(self):
        h={'Child':['Parent'],'Grandchild':['Child']}
        sources={'Parent':[self.row('1')],'Child':[self.row('2')],'Grandchild':[self.row('3'),self.row('4',**{'Quality Score':'Fair','display_only':True})]}
        before=copy.deepcopy(sources)
        yes=policy.project_rows(sources,'Parent',h,True);no=policy.project_rows(sources,'Parent',h,False)
        child=policy.project_rows(sources,'Child',h,True)
        self.assertEqual(len(yes),4);self.assertGreaterEqual(len(yes),len(child));self.assertEqual(len(no),1)
        self.assertEqual(len(policy.pooling_rows(yes)),3);self.assertEqual(sources,before)
        self.assertEqual({r['exposure_measurement_type'] for r in yes},{'human_biospecimen'})

    def test_same_population_prefers_parent_and_dedups_sibling_and_diamond(self):
        h={'Child':['Parent'],'Other':['Parent'],'Grandchild':['Child','Other']}
        sources={'Parent':[self.row('1',1.2)],'Child':[self.row('1',.8),self.row('2',.9)],'Other':[self.row('2',1.1)],'Grandchild':[self.row('3')]}
        result=policy.project_rows(sources,'Parent',h,True);pool=policy.pooling_rows(result)
        self.assertEqual(len(result),5);self.assertEqual(len(pool),3)
        self.assertEqual(next(r for r in pool if r['PMID']=='1')['subexposure_source'],'Parent')
        self.assertEqual(len({r['PMID'] for r in pool}),len(pool))

    def test_within_source_overlap_dedup_and_explicit_disjoint_sexes(self):
        a=self.row('1');b=self.row('1',.9,effect_is_adjusted=True)
        pool=policy.pooling_rows(policy.project_rows({'Child':[a,b]},'Child',{},True))
        self.assertEqual(len(pool),1);self.assertTrue(pool[0]['effect_is_adjusted'])
        a['Gender']='Male';b['Gender']='Female'
        self.assertEqual(len(policy.pooling_rows(policy.project_rows({'Child':[a,b]},'Child',{},True))),2)

    def test_explicit_subpopulation_and_unspecified_total_cannot_be_pooled_together(self):
        rows=[self.row('1',independent_population_id='Cohort A'),self.row('1',.9)]
        result=policy.project_rows({'Child':rows},'Child',{},True)
        self.assertEqual(len(policy.pooling_rows(result)),1)

    def test_fair_manual_clinical_and_missing_numbers_never_win(self):
        for change in [{'Quality Score':'Fair'},{'manual_poolable':False},{'policy_poolable':False},{'final_status':'EXCLUDE'},{'Sample Size':None}]:
            parent=self.row('1',**change);child=self.row('1')
            rows=policy.project_rows({'Parent':[parent],'Child':[child]},'Parent',{'Child':['Parent']},True)
            pool=policy.pooling_rows(rows);self.assertEqual(len(pool),1);self.assertEqual(pool[0]['subexposure_source'],'Child')
            self.assertEqual(len(rows),2)

    def test_mode_and_taxonomy(self):
        self.assertTrue(policy.include_mode());self.assertFalse(policy.include_mode('no'))
        with self.assertRaises(ValueError):policy.include_mode('wrong')
        for name in ['DASH diet','MIND diet','Mediterranean diet','Early time-restricted eating','Intermittent fasting','Dietary plant diversity']:
            self.assertEqual(taxonomy.exposure_category(name,ROOT),'Diet')
        for name in ['Oleic acid','Omega-9 fatty acids']:
            self.assertEqual(taxonomy.exposure_category(name,ROOT),'Fatty Acids & Lipids')
        self.assertEqual(taxonomy.exposure_category('Vitamin B6',ROOT),'B Vitamins')
        self.assertEqual(len({taxonomy.CATEGORY_COLORS[k] for k in ['Diet','B Vitamins','Fatty Acids & Lipids']}),3)

    def test_saved_mode_cache_summary_and_parent_counts(self):
        h=json.loads((ROOT/'data/exposure_hierarchy.json').read_text());roots={m:ROOT/'Subexposure_results'/m for m in ['yes','no']}
        yes_files=list(roots['yes'].glob('*/*_true_all.json'));self.assertTrue(yes_files)
        yes_by_scope={}
        differences=0
        for path in yes_files:
            yes=json.loads(path.read_text());no=json.loads((roots['no']/path.relative_to(roots['yes'])).read_text())
            self.assertEqual(yes['include_subexposures'],'yes');self.assertEqual(no['include_subexposures'],'no')
            self.assertTrue(all(not r['subexposure_inherited'] for r in no['studies']))
            self.assertGreaterEqual(len(yes['studies']),len(no['studies']))
            if len(yes['studies'])>len(no['studies']):differences+=1
            yes_by_scope[(policy.key(yes['exposure']),yes['disease'],yes['endpoint'])]=yes
            if yes.get('headline'):
                self.assertEqual(yes['headline']['n_studies'],yes['subexposure_projection']['pooling_rows'])
        self.assertGreater(differences,0)
        for (exposure,disease,endpoint),parent in yes_by_scope.items():
            for child in policy.descendants(exposure,h):
                child= yes_by_scope.get((policy.key(child),disease,endpoint))
                if child:self.assertGreaterEqual(len(parent['studies']),len(child['studies']))
        for mode in roots:
            manifest=json.loads((ROOT/'Plot/subexposures'/mode/'summary_manifest.json').read_text())
            self.assertEqual(manifest['include_subexposures'],mode)
            for scope in manifest['scopes'].values():
                for entry in scope.get('workbooks',{}).values():
                    if entry.get('available'):self.assertTrue((ROOT/'Plot/subexposures'/mode/entry['filename']).is_file())

    def test_ui_toggle_and_mode_transport_are_present(self):
        client=app.app.test_client()
        for mode in ['yes','no']:
            html=client.get('/?include_subexposures='+mode).get_data(as_text=True)
            self.assertIn('Include subexposures',html);self.assertIn('value="'+mode+'" selected',html)
            self.assertIn('/summary?scope=prostate_cancer&include_subexposures='+mode,html)
            self.assertEqual(client.get('/api/config?include_subexposures='+mode).status_code,200)
            summary=client.get('/summary?include_subexposures='+mode)
            self.assertEqual(summary.status_code,200)
        self.assertEqual(client.get('/?include_subexposures=invalid').status_code,400)
        source=(ROOT/'static/subexposures.js').read_text();self.assertIn('include_subexposures: mode',source)
        self.assertIn("url.searchParams.set('include_subexposures', mode)",source)

    def test_browser_mode_transport_preserves_summary_language_and_json(self):
        js=r"""
(async()=>{
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const calls=[];let ready,change,clicks=0;
const link={tagName:'A',value:'/summary?lang=nl',getAttribute(){return this.value},setAttribute(k,v){this.value=v}};
const toggle={value:'',addEventListener(name,cb){change=cb}};
const context={URL,JSON,String,location:{href:'https://site.test/?include_subexposures=no',origin:'https://site.test'},
 history:{replaceState(a,b,url){context.location.href=String(url)}},
 document:{addEventListener(n,cb){ready=cb},querySelectorAll(){return [link]},getElementById(id){return id==='include-subexposures'?toggle:id==='analyze-btn'?{click(){clicks++}}:null}},
 fetch(...args){calls.push(args);return Promise.resolve({})}};
context.window=context;vm.createContext(context);vm.runInContext(fs.readFileSync('static/subexposures.js','utf8'),context);
ready();assert.equal(toggle.value,'no');assert.match(link.value,/include_subexposures=no/);assert.match(link.value,/lang=nl/);
await context.fetch('/reanalyze',{headers:{'Content-Type':'application/json'},body:JSON.stringify({studies:[{PMID:'1'}]})});
assert.equal(JSON.parse(calls[0][1].body).include_subexposures,'no');assert.match(calls[0][0],/include_subexposures=no/);
const stale=context.fetch('/analyze');toggle.value='yes';change();await assert.rejects(stale,{name:'AbortError'});assert.equal(clicks,1);assert.match(link.value,/include_subexposures=yes/);assert.match(link.value,/lang=nl/);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result=subprocess.run(['node','-e',js],capture_output=True,text=True,cwd=ROOT)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_mode_api_pooled_result_and_reanalysis_keep_plots_isolated(self):
        import hashlib
        root=ROOT/'Subexposure_results'
        candidates=[]
        for path in (root/'yes').glob('*/*_true_all.json'):
            yes=json.loads(path.read_text());no=json.loads((root/'no'/path.relative_to(root/'yes')).read_text())
            if yes.get('headline') and no.get('headline') and len(yes['studies'])>len(no['studies']):candidates.append((path,yes,no))
        self.assertTrue(candidates)
        path,yes,no=candidates[0];client=app.app.test_client()
        protected={source:hashlib.sha256(source.read_bytes()).hexdigest() for source in [path,root/'no'/path.relative_to(root/'yes')]}
        for data in [yes,no]:
            for k in ['plot_url','funnel_plot_url','baujat_plot_url']:
                if data.get(k):
                    f=ROOT/str(data[k]).split('?')[0];protected[f]=hashlib.sha256(f.read_bytes()).hexdigest()
        for mode,data in [('yes',yes),('no',no)]:
            args={'disease':data['disease'],'exposure':data['exposure'],'outcome':data['endpoint'],'endpoint':data['endpoint'],
                  'exclude_meta':True,'include_subexposures':mode,'evidence_track':'combined'}
            response=client.post('/analyze',json=args);self.assertEqual(response.status_code,200,response.get_json())
            actual=response.get_json();self.assertEqual(len(actual['studies']),len(data['studies']))
            self.assertEqual(actual['include_subexposures'],mode)
            # Recompute the current finite-interval baseline; cached lifetime-risk
            # summaries are historical and must not be reused as current estimates.
            projected_rr=actual['canonical_rr']['pooled_es']
            self.assertEqual(actual['analysis_settings']['effect_measure'],'RR')
            self.assertEqual(actual['headline']['pooled_es'],projected_rr)
            self.assertEqual(actual['headline']['baseline_risk'],actual['baseline_risk']['p0'])
            response=client.post('/reanalyze',json={**args,'studies':actual['studies'],'quality_filter':'Moderate+'})
            actual=response.get_json();self.assertEqual(response.status_code,200,actual)
            self.assertAlmostEqual(actual['canonical_rr']['pooled_es'],projected_rr,places=8)
            self.assertEqual(actual['plot_url'],actual['forest_plots'].get('log') or actual['forest_plots'].get('original'))
            for k in ['plot_url','funnel_plot_url','baujat_plot_url']:
                if actual.get(k):
                    import base64
                    self.assertTrue(actual[k].startswith('data:image/png;base64,'))
                    self.assertEqual(base64.b64decode(actual[k].split(',',1)[1])[:8],b'\x89PNG\r\n\x1a\n')
        for path,digest in protected.items():self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)

if __name__=='__main__':unittest.main()
