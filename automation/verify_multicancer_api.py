"""Verify primary-source rows through the public derivative API, without screening."""
from pathlib import Path
import json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    from app import app
    from source_worker import close_workers
    client=app.test_client();reports=[]
    try:
        for mode in ('no','yes'):
            for source,disease,exposure,expected in [
                ('MetaVir','Prostate cancer','Ultra-processed food intake',3),
                ('MetaVir','Aggressive prostate cancer','Non-nutritive sweetener intake',1),
                ('metaPee','Kidney and renal pelvis cancer','Non-nutritive sweetener intake',1),
                ('metaPee','Kidney and renal pelvis cancer','Sugar-sweetened beverage intake',1),
                ('metaPee','Bladder cancer','Processed meat intake',2)]:
                response=client.post('/evidence/'+source+'/analyze',json={'disease':disease,'exposure':exposure,
                    'outcome':'Incidence','exclude_meta':True,'include_subexposures':mode})
                data=response.get_json();assert response.status_code==200,(source,disease,exposure,response.status_code,data.get('error'))
                rows=[r for r in data['studies'] if r.get('source_review_id')]
                assert len(rows)==expected,(source,disease,exposure,len(rows))
                assert all(r['Direct Exposure']==exposure for r in rows)
                if disease=='Aggressive prostate cancer':
                    assert rows[0]['Sample Size'] is None and rows[0]['policy_poolable'] is False
                elif exposure=='Processed meat intake':
                    male=next(r for r in rows if r['Gender']=='Male');female=next(r for r in rows if r['Gender']=='Female')
                    assert male['policy_poolable'] is False and male.get('Settings Pooling Selected') is not True
                    assert female['policy_poolable'] is True
                else:
                    assert all(r['policy_poolable'] is True and r.get('Settings Pooling Selected') is not False for r in rows)
                reports.append({'source':source,'mode':mode,'disease':disease,'exposure':exposure,
                    'status':response.status_code,'reviewed_rows':len(rows),'holds_verified':True})
                print(source,mode,disease,exposure,'verified',flush=True)
        for source in ('MetaVir','metaPee'):
            response=client.get('/evidence/'+source+'/api/exposures');assert response.status_code==200
            assert 'Non-nutritive sweetener intake' in response.get_json() and 'Sugar-sweetened beverage intake' in response.get_json()
        (ROOT/'evidence-revisions/multicancer-2026-09-29/api_verification.json').write_text(json.dumps(reports,indent=2)+'\n')
    finally:close_workers()


if __name__=='__main__':main()
