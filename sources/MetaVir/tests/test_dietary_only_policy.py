import copy,json,unittest
from pathlib import Path
from unittest.mock import patch
from scripts import dietary_only_policy as policy
from scripts import subexposure_policy as hierarchy
from scripts import monthly_evidence_update as monthly
from scripts import dual_screening
import app
ROOT=Path(__file__).resolve().parents[1]

class DietaryOnlyPolicyTests(unittest.TestCase):
    def test_approved_scope_and_isolated_nutrients_remain_distinct(self):
        for name in ['Dairy','Eggs','Garlic','Soy','Tomato','Artichoke extract','DASH diet']:
            self.assertTrue(policy.is_dietary_only_exposure(name),name)
        for name in ['Curcumin','Berberine','Cannabidiol','Quercetin','Vitamin C','Oleic acid','Alcohol']:
            self.assertFalse(policy.is_dietary_only_exposure(name),name)

    def test_override_is_idempotent_and_preserves_raw_evidence_and_decisions(self):
        source={'exposure':'Garlic','final_status':'ELIGIBLE_NOT_POOLABLE','manual_poolable':False,
                'extraction':{'effect_size':.8,'total_n':1000,'exposure_measurement_type':'human_biospecimen',
                  'evidence':{'exposure_measurement':{'excerpt':'Measured in plasma','source':'abstract'}},
                  'additional_records':[{'effect_size':.7,'exposure_measurement_type':'unclear'}]}}
        original=copy.deepcopy(source);result=policy.apply_record(source)
        self.assertEqual(source,original);self.assertEqual(result,policy.apply_record(result))
        self.assertEqual(result['final_status'],'ELIGIBLE_NOT_POOLABLE');self.assertFalse(result['manual_poolable'])
        e=result['extraction'];self.assertEqual(e['exposure_measurement_type'],'dietary_intake')
        self.assertEqual(e['exposure_measurement_type_original'],'human_biospecimen')
        self.assertEqual(e['evidence'],original['extraction']['evidence']);self.assertEqual(e['effect_size'],.8)
        self.assertEqual(e['additional_records'][0]['exposure_measurement_type'],'dietary_intake')

    def test_inherited_botanical_view_does_not_relabel_nutrient_source(self):
        source={'PMID':'1','exposure_measurement_type':'human_biospecimen','Quality Score':'Fair','Sample Size':100}
        rows=hierarchy.project_rows({'Quercetin':[source]},'Garlic',{'Quercetin':['Garlic']},True)
        parent=[policy.apply_row(row,'Garlic') for row in rows]
        child=[policy.apply_row(row,'Quercetin') for row in hierarchy.project_rows({'Quercetin':[source]},'Quercetin',{},False)]
        self.assertEqual(parent[0]['exposure_measurement_type'],'dietary_intake')
        self.assertEqual(child[0]['exposure_measurement_type'],'human_biospecimen')
        self.assertEqual(source['exposure_measurement_type'],'human_biospecimen')
        self.assertFalse(parent[0]['subexposure_pooling_selected'])

    def test_future_fair_row_preserves_classification_provenance(self):
        item={'exposure':'Garlic','pmid':'1','final_status':'ELIGIBLE_NOT_POOLABLE','jbi':{'grade':'Fair'},
              'extraction':{'exposure_measurement_type':'human_biospecimen','total_n':100}}
        original_row=monthly.study_row(item)
        row=monthly.study_row(policy.apply_record(item))
        self.assertEqual(row['Record ID'], original_row['Record ID'])
        self.assertEqual(row['exposure_measurement_type'],'dietary_intake')
        self.assertEqual(row['exposure_measurement_type_original'],'human_biospecimen')
        self.assertEqual(row['Quality Score'],'Fair');self.assertFalse(row['policy_poolable'])
        self.assertIn('PROJECT EXPOSURE-MEASUREMENT RULE',dual_screening.SECOND_INSTRUCTIONS)
        self.assertIn('PROJECT EXPOSURE-MEASUREMENT RULE',dual_screening.FULL_TEXT_INSTRUCTIONS)

    def test_saved_effective_and_mode_rows_all_apply_by_displayed_exposure(self):
        checked=0
        for folder in ['Cached_results','ClaudeGemini_results','combined','Subexposure_results/yes','Subexposure_results/no']:
            for path in (ROOT/folder).glob('*/*_true_all.json'):
                packet=json.loads(path.read_text());exposure=packet.get('exposure') or path.parent.name
                if policy.is_dietary_only_exposure(exposure):
                    for row in packet.get('studies',[]):
                        self.assertEqual(row['exposure_measurement_type'],'dietary_intake',str(path))
                        self.assertTrue(row['exposure_measurement_overridden']);checked+=1
        self.assertGreater(checked,0)
        report=json.loads((ROOT/'outputs/dietary_only_2026-09-05/application_report.json').read_text())
        self.assertTrue(report['raw_sources_verified_unchanged']);self.assertTrue(report['non_measurement_fields_unchanged'])

    def test_saved_analysis_and_reanalysis_enforce_approved_classification(self):
        for path in (ROOT/'Subexposure_results/yes').glob('*/*_true_all.json'):
            data=json.loads(path.read_text())
            if policy.is_dietary_only_exposure(data.get('exposure')) and data.get('headline') and data.get('studies'):break
        else:self.fail('No dietary analysis scope available')
        client=app.app.test_client();args={'disease':data.get('disease') or data.get('cancer'),'exposure':data['exposure'],
                'outcome':data.get('endpoint') or data.get('outcome'),'endpoint':data.get('endpoint') or data.get('outcome'),
                'exclude_meta':True,'include_subexposures':'yes'}
        response=client.post('/analyze',json=args);self.assertEqual(response.status_code,200,response.get_json())
        self.assertTrue(all(row['exposure_measurement_type']=='dietary_intake' for row in response.get_json()['studies']))
        rows=[copy.deepcopy(row) for row in response.get_json()['studies'] if row.get('subexposure_pooling_selected') and row.get('Quality Score') in ['Good','Moderate']]
        for row in rows:row['exposure_measurement_type']='human_biospecimen'
        args.update(studies=rows,quality_filter='Moderate+')
        captured=[]
        def analysis(frame,*a,**kw):
            captured.extend(frame.to_dict('records'));return {'studies':frame.to_dict('records'),'success':True}
        with patch.object(app.meta_analysis,'perform_meta_analysis',side_effect=analysis):
            response=client.post('/reanalyze',json=args)
        self.assertEqual(response.status_code,200,response.get_json());self.assertTrue(captured)
        self.assertTrue(all(row['exposure_measurement_type']=='dietary_intake' for row in captured))
        js=(ROOT/'static/script.js').read_text();self.assertIn('Dietary intake only</option>',js)
        self.assertIn('User-approved exposure classification',js)

if __name__=='__main__':unittest.main()
