import copy
import json
from pathlib import Path

from prostate_scopes import LABEL, SCOPE_ID, aggressive_only, project_payload, extend_catalog

ROOT=Path(__file__).resolve().parents[1]


def test_projection_removes_aggressive_estimates_without_excluding_the_whole_paper():
    parent={'studies':[{'PMID':'1','Disease Endpoint':'general','Effect Size':0.9},
        {'PMID':'1','Disease Endpoint':'aggressive','Effect Size':0.7},
        {'PMID':'2','disease_scope_review':{'endpoint':'aggressive'}},
        {'PMID':'3','Disease Endpoint':'uncertain','display_only':True},
        {'PMID':'4','Disease Endpoint':'nonaggressive'}],
        'headline':{'pooled_es':0.8},'screening_stats':{'extracted':5}}
    before=copy.deepcopy(parent);child=project_payload(parent)
    assert parent==before
    assert [r['PMID'] for r in child['studies']]==['1','3','4']
    assert child['studies'][1]['display_only'] is True
    assert child['disease']==LABEL
    assert 'headline' not in child
    assert child['screening_stats']['extracted']==3


def test_catalog_uses_explicit_general_prostate_risk_for_evidence_restriction():
    original=json.loads((ROOT/'sources/MetaVir/static/seer-baseline.json').read_text())
    result=extend_catalog(original)
    mapping=next(d for d in result['diseases'] if d['id']==SCOPE_ID)
    assert mapping['parent_id']=='prostate_cancer'
    assert mapping['baseline_scope']=='prostate_cancer'
    assert [r['risk_percent'] for r in result['records'] if r['disease']==SCOPE_ID]==[
        r['risk_percent'] for r in original['records'] if r['disease']=='prostate_cancer']
    assert extend_catalog(result)==result


def test_published_scope_is_exact_parent_subset_with_unchanged_study_fields():
    from automation.sync_sources import assembled_source
    root=assembled_source(ROOT,'MetaVir')
    for mode in ('yes','no'):
        children=list((root/'Subexposure_results'/mode).glob(f'*/{SCOPE_ID}_incidence_true_core.json'))
        assert len(children)==124
        for path in children:
            child=json.loads(path.read_text())
            parent=json.loads(path.with_name('prostate_cancer_incidence_true_core.json').read_text())
            assert child['studies']==[r for r in parent['studies'] if not aggressive_only(r)]


def test_new_scope_resolves_in_ui_analysis_and_study_selection():
    from app import app
    from source_worker import close_workers
    from study_selection import build_selection_flow, selection_catalog
    app.config['TESTING']=True
    try:
        client=app.test_client()
        scope=next(d for d in client.get('/api/scopes').json if d['id']==SCOPE_ID)
        assert scope['label']==LABEL and scope['parent_id']=='prostate_cancer'
        response=client.post('/evidence/prostate/analyze?include_subexposures=no',json={
            'disease':'Prostate cancer','subcategory':SCOPE_ID,'exposure':'Selenium',
            'outcome':'Incidence','exclude_meta':True})
        assert response.status_code==200,response.data[:500]
        data=response.json
        assert data['disease']==LABEL
        assert data['studies'] and not any(aggressive_only(r) for r in data['studies'])
        assert data['baseline_risk']['available']
        assert LABEL in selection_catalog('prostate')
        flow=build_selection_flow('prostate',LABEL,'Selenium')
        assert flow['available'] and flow['scope_note']
        assert sum(x['count'] for x in flow['reasons'])==flow['eligibility_excluded']
    finally:
        close_workers()


def test_selection_scope_exclusions_match_removed_parent_reports_only():
    from study_selection import _current_pool_packets, build_selection_flow
    packets = _current_pool_packets('MetaVir')
    checked = 0
    for (disease, exposure), (child, _) in packets.items():
        if disease != LABEL:
            continue
        parent = packets[('Prostate cancer', exposure)][0]
        parent_pmids = {str(row['PMID']) for row in parent['studies']}
        child_pmids = {str(row['PMID']) for row in child['studies']}
        parent_flow = build_selection_flow('MetaVir', 'Prostate cancer', exposure)
        child_flow = build_selection_flow('MetaVir', LABEL, exposure)
        scope_excluded = sum(row['count'] for row in child_flow['reasons']
                             if row['label'] == 'Aggressive-only evidence outside the selected prostate scope')
        assert scope_excluded == len(parent_pmids - child_pmids), exposure
        assert child_flow['included_reports'] == parent_flow['included_reports'] - scope_excluded, exposure
        assert sum(row['count'] for row in child_flow['reasons']) == child_flow['eligibility_excluded'], exposure
        checked += 1
    assert checked > 0
    # These audit-retained reports were already absent from the parent cache;
    # their absence must never manufacture an aggressive-endpoint exclusion.
    for exposure in ('Soy', 'Eggs'):
        flow = build_selection_flow('MetaVir', LABEL, exposure)
        assert not any(row['label'] == 'Aggressive-only evidence outside the selected prostate scope'
                       for row in flow['reasons']), exposure
