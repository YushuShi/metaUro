from datetime import date
import json
from pathlib import Path

import pytest

from automation import autonomous_monthly as monthly
from automation.sync_sources import read, sha, write
from native_proof_fixture import add_native_proofs


ROOT = Path(__file__).resolve().parents[1]


def scaffold(root):
    write(root/monthly.CONFIG, {'scopes': {scope:{'bootstrap_through':'2026-08-31'} for scope in monthly.SOURCES}})
    files = {}
    for scope in monthly.SOURCES:
        path = root/'runtime_code/monthly'/scope/'automation/__init__.py'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('')
        files[str(path.relative_to(root/'runtime_code/monthly'))] = sha(path)
        base = root/'sources'/scope
        write(base/'Cached_results/fiber/cancer_incidence_true_all.json', {'studies':[study('1')]})
    write(root/'runtime_code/monthly/manifest.json', {'version':monthly.VERSION,'files':files})
    write(root/'updates/manifest.json', {'schema_version':1,'sources':{}})
    for name in ('autonomous_scope.py','autonomous_projection.py','prostate_scope_results.py','preprint_recovery_bridge.py'):
        path = root/'automation'/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('# isolated fixture\n')
    return root


def study(pmid):
    return {'PMID':pmid,'Effect ID':pmid,'Direct Exposure':'Fiber','Effect Type':'RR',
            'Effect Size':.8,'Lower CI':.7,'Upper CI':.9,'Sample Size':1000,'Cases':100}


def test_optional_ncbi_key_and_base_alias_are_normalized():
    env = monthly.environment({'API_BASE_URI':' https://gateway.example/v1 ',
        'CORNELL_API_KEY':' key ','NCBI_EMAIL':' test@example.org ',
        'MONTHLY_REQUIRE_VALID_NCBI_KEY':'true'})
    assert env['CORNELL_API_BASE_URL']=='https://gateway.example/v1'
    assert env['NCBI_API_KEY']==''
    assert env['MONTHLY_REQUIRE_VALID_NCBI_KEY']=='false'
    assert env['NCBI_EMAIL']=='test@example.org'


def test_preflight_works_without_any_external_repositories(tmp_path):
    root = scaffold(tmp_path)
    result = monthly.preflight(root,today=date(2026,10,3),environ={
        'NCBI_EMAIL':'test@example.org','CORNELL_API_KEY':'fixture','API_BASE_URI':'https://example.org'})
    assert result['ready']
    assert result['external_repository_dependencies']==[]
    assert result['window']['end_date']=='2026-10-02'
    assert result['window']['start_dates']==dict.fromkeys(monthly.SOURCES,'2026-09-01')


def test_runtime_tampering_fails_before_work(tmp_path):
    root = scaffold(tmp_path)
    (root/'runtime_code/monthly/metaPee/automation/__init__.py').write_text('tampered')
    with pytest.raises(ValueError,match='checksum'):
        monthly.runtime_manifest(root)


def test_materialized_work_is_not_hardlinked_to_immutable_evidence(tmp_path):
    root = scaffold(tmp_path)
    work = root/'runtime/monthly/fixture/metaPee'
    monthly.materialize(root,'metaPee',work)
    relative = 'Cached_results/fiber/cancer_incidence_true_all.json'
    baseline = root/'sources/metaPee'/relative
    before = baseline.read_bytes()
    assert baseline.stat().st_ino != (work/relative).stat().st_ino
    (work/relative).write_text('{}')
    assert baseline.read_bytes()==before
    assert (work/'automation/autonomous_scope.py').exists()


def test_interrupted_window_is_frozen(tmp_path):
    root = scaffold(tmp_path)
    frozen = monthly.window(root,date(2026,10,3))
    write(root/'updates/monthly/active.json',frozen)
    assert monthly.window(root,date(2026,10,9))==frozen
    frozen['run_id']='../../other'
    write(root/'updates/monthly/active.json',frozen)
    with pytest.raises(ValueError,match='run ID'):
        monthly.window(root)


def prepared_fixture(root, changed_historical=False):
    scaffold(root)
    result = {'run_id':'2026-09-01_2026-10-02','end_date':'2026-10-02','scopes':{}}
    audit = root/'updates/monthly'/result['run_id']
    write(audit/'input.json',{'runtime_sha256':'fixture','overlay_index':read(root/'updates/manifest.json')})
    relative='Cached_results/fiber/cancer_incidence_true_all.json'
    for scope in monthly.SOURCES:
        work=root/'runtime/monthly'/result['run_id']/scope
        old=study('1')
        if changed_historical and scope=='MetaVir':
            old['Effect Size']=.75
        write(work/relative,{'studies':[old,study('2')]})
        csv=root/'monthlyHumanReview'/(scope+'.csv')
        csv.parent.mkdir(parents=True,exist_ok=True)
        csv.write_text('pmid,human_decision\n')
        write(audit/scope/'screening/autonomous_scope.json',{'status':'validated'})
        result['scopes'][scope]={'output_sha256':{relative:sha(work/relative)},
            'human_review_csv':str(csv.relative_to(root)),'human_review_pending':0}
    return audit,result


def test_both_scopes_prepare_without_publishing_pointer(tmp_path):
    audit,result=prepared_fixture(tmp_path)
    before=(tmp_path/'updates/manifest.json').read_bytes()
    monthly.prepare_overlays(tmp_path,audit,result)
    assert set(result['prepared_overlay_index']['sources'])==set(monthly.SOURCES)
    assert (tmp_path/'updates/manifest.json').read_bytes()==before
    assert not (tmp_path/'updates/monthly/state.json').exists()


def test_changed_historical_effect_prevents_release(tmp_path):
    audit,result=prepared_fixture(tmp_path,changed_historical=True)
    before=(tmp_path/'updates/manifest.json').read_bytes()
    with pytest.raises(ValueError,match='historical evidence'):
        monthly.prepare_overlays(tmp_path,audit,result)
    assert (tmp_path/'updates/manifest.json').read_bytes()==before


def test_changed_historical_selection_gate_prevents_release(tmp_path):
    audit,result=prepared_fixture(tmp_path)
    relative='Cached_results/fiber/cancer_incidence_true_all.json'
    path=tmp_path/'runtime/monthly'/result['run_id']/'metaPee'/relative
    packet=read(path);packet['studies'][0]['display_only']=True
    write(path,packet);result['scopes']['metaPee']['output_sha256'][relative]=sha(path)
    with pytest.raises(ValueError,match='selection gates'):
        monthly.prepare_overlays(tmp_path,audit,result)


def test_failed_summary_build_restores_both_scope_pointers(tmp_path,monkeypatch):
    audit,result=prepared_fixture(tmp_path)
    monthly.prepare_overlays(tmp_path,audit,result)
    result['status']='validated'
    write(audit/'release.json',result)
    write(tmp_path/'updates/monthly/active.json',{'run_id':result['run_id']})
    write(tmp_path/'summary/yes/summary_manifest.json',{'preserved':True})
    before=(tmp_path/'updates/manifest.json').read_bytes()
    import summary_outputs
    def fail(*args,**kwargs):
        write(tmp_path/'summary/yes/summary_manifest.json',{'corrupt':True})
        raise RuntimeError('fixture build failure')
    monkeypatch.setattr(summary_outputs,'refresh_all',fail)
    with pytest.raises(RuntimeError,match='fixture build failure'):
        monthly.publish(tmp_path,validate=False)
    assert (tmp_path/'updates/manifest.json').read_bytes()==before
    assert read(tmp_path/'summary/yes/summary_manifest.json')=={'preserved':True}
    assert not (tmp_path/'updates/monthly/state.json').exists()


def test_monthly_workflow_has_no_remote_source_checkout():
    workflow=(ROOT/'.github/workflows/monthly-source-refresh.yml').read_text()
    assert workflow.count('uses: actions/checkout@')==1
    assert 'ref: V2' in workflow
    assert 'git push origin "HEAD:V2"' in workflow
    assert 'github.event.repository.default_branch' not in workflow
    assert 'SOURCE_METAPEE_SSH_KEY' not in workflow
    assert 'SOURCE_METAVIR_SSH_KEY' not in workflow
    assert 'git add -A -- updates summary monthlyHumanReview runtime_code' in workflow
    assert 'automation.autonomous_monthly prepare' in workflow
    assert 'automation.autonomous_monthly publish' in workflow
    assert "MONTHLY_REQUIRE_VALID_NCBI_KEY: 'false'" in workflow
    assert workflow.count('always() && (failure() || cancelled())') == 2
    assert 'automation.autonomous_monthly checkpoint' in workflow
    assert 'runs-on: ubuntu-24.04' in workflow
    assert 'bash automation/install_poppler.sh "$RUNNER_TEMP/poppler-26.07.0"' in workflow
    assert workflow.index('Install and verify the exact PDF reader') < workflow.index('Search, screen independently, extract, and pool')
    assert 'tests/test_poppler_runtime.py -q' in workflow


def test_absent_monthly_success_is_not_inferred_from_manual_overlay(tmp_path):
    scaffold(tmp_path)
    assert monthly.validate_completed_release(tmp_path) is None


def test_cancellation_checkpoint_preserves_paid_packets_without_publishing(tmp_path):
    scaffold(tmp_path)
    span=monthly.window(tmp_path,date(2026,10,3))
    write(tmp_path/'updates/monthly/active.json',span)
    work=tmp_path/'runtime/monthly'/span['run_id']/'metaPee'
    native=span['start_dates']['metaPee']+'_'+span['end_date']
    write(work/'data/monthly_updates'/native/'responses/paid.json',{'request':'complete'})
    write(work/'data/monthly_reports'/native/'cost.json',{'tokens':10})
    csv=work/'monthlyHumanReview'/(native+'.csv');csv.parent.mkdir(parents=True)
    csv.write_text('context_id,human_review_reason\nfixture,pending\n')
    before=(tmp_path/'updates/manifest.json').read_bytes()
    result=monthly.checkpoint(tmp_path)
    assert result['copied_packet_trees']==2
    recovery=tmp_path/'updates/monthly'/span['run_id']/'recovery/metaPee'
    assert read(recovery/'monthly_updates'/native/'responses/paid.json')=={'request':'complete'}
    assert read(recovery/'monthly_reports'/native/'cost.json')=={'tokens':10}
    assert (tmp_path/'monthlyHumanReview'/(span['run_id']+'_metaPee.csv')).read_bytes()==csv.read_bytes()
    assert (tmp_path/'updates/manifest.json').read_bytes()==before
    assert not (tmp_path/'updates/monthly/state.json').exists()


def complete_fixture(root):
    audit, result = prepared_fixture(root)
    for scope in monthly.SOURCES:
        screening = audit/scope/'screening'
        native_csv = audit/scope/'review/native.csv'
        native_csv.parent.mkdir(parents=True)
        native_csv.write_bytes(b'review_id,reason\n')
        write(screening/'astra/adjudications.json', {'results':[]})
        write(screening/'final_screening.json', {'results':[]})
        gate = {'review_csv':'monthlyHumanReview/native.csv', 'review_csv_sha256':sha(native_csv),
                'adjudication_path':'data/monthly_updates/'+result['run_id']+'/astra/adjudications.json',
                'adjudication_sha256':sha(screening/'astra/adjudications.json'),
                'source_final_screening_sha256':sha(screening/'final_screening.json')}
        write(screening/'monthly_application_gate.json', gate)
        result['scopes'][scope].update(run_id=result['run_id'],
            application_gate_sha256=sha(screening/'monthly_application_gate.json'),
            native_review_csv_artifact={'path':str(native_csv.relative_to(root)), 'sha256':sha(native_csv)})
        add_native_proofs(screening, scope, result['scopes'][scope])
    monthly.prepare_overlays(root,audit,result)
    write(root/'updates/manifest.json',result['prepared_overlay_index'])
    write(root/'summary/no/fixture.json',{'scope':'complete'})
    result.update(status='completed',audit_artifact_sha256={str(p.relative_to(root)):sha(p)
        for scope in monthly.SOURCES for p in (audit/scope).rglob('*') if p.is_file()},
        summary_sha256={'summary/no/fixture.json':sha(root/'summary/no/fixture.json')})
    write(audit/'release.json',result)
    write(root/'updates/monthly/state.json',{'repository':'metaUro','status':'completed',
        'run_id':result['run_id'],'search_through':dict.fromkeys(monthly.SOURCES,result['end_date']),
        'manifest_path':str((audit/'release.json').relative_to(root)), 'manifest_sha256':sha(audit/'release.json')})
    return audit,result


def test_completed_cutoff_is_a_minimum_and_verification_is_read_only(tmp_path):
    complete_fixture(tmp_path)
    before={str(p.relative_to(tmp_path)):sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    assert monthly.validate_completed_release(tmp_path,'2026-09-27')['cutoff']=='2026-10-02'
    with pytest.raises(ValueError,match='cutoff'):
        monthly.validate_completed_release(tmp_path,'2026-10-03')
    assert before=={str(p.relative_to(tmp_path)):sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    (tmp_path/'summary/no/fixture.json').write_text('{}')
    with pytest.raises(ValueError,match='checksum'):
        monthly.validate_completed_release(tmp_path)


def test_rebuilt_publication_inventory_omits_summary_builder_locks(tmp_path, monkeypatch):
    audit, result = complete_fixture(tmp_path)
    result['status'] = 'validated'
    write(audit/'release.json', result)
    write(audit/'immutable_before.json', {})
    write(tmp_path/'updates/manifest.json', read(audit/'input.json')['overlay_index'])
    write(tmp_path/'updates/monthly/active.json', {'run_id':result['run_id']})
    import summary_outputs
    def regenerate(root):
        for mode in ('no', 'yes'):
            lock = root/'summary/.locks'/(mode+'.lock')
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_bytes(b'')
        write(root/'summary/no/regenerated.json', {'verified':True})
    monkeypatch.setattr(summary_outputs, 'refresh_all', regenerate)
    published = monthly.publish(tmp_path, validate=False)
    assert not any('/.locks/' in name for name in published['summary_sha256'])
    assert 'summary/no/regenerated.json' in published['summary_sha256']
    assert monthly.validate_completed_release(tmp_path)['status'] == 'completed'


def test_completed_checkpoint_cannot_publish_without_installed_artifacts(tmp_path):
    audit,result=prepared_fixture(tmp_path)
    result['status']='completed'
    write(audit/'release.json',result)
    write(tmp_path/'updates/monthly/active.json',{'run_id':result['run_id']})
    with pytest.raises(ValueError,match='not an installed release'):
        monthly.publish(tmp_path,validate=False)
    assert not (tmp_path/'updates/monthly/state.json').exists()


def test_metadata_write_failure_rolls_back_completion_state(tmp_path,monkeypatch):
    audit,result=prepared_fixture(tmp_path)
    monthly.prepare_overlays(tmp_path,audit,result)
    result['status']='validated'
    write(audit/'release.json',result)
    write(audit/'immutable_before.json',{})
    write(tmp_path/'updates/monthly/active.json',{'run_id':result['run_id']})
    write(tmp_path/'summary/no/fixture.json',{})
    import summary_outputs
    monkeypatch.setattr(summary_outputs,'refresh_all',lambda *_:None)
    previous=(tmp_path/'updates/manifest.json').read_bytes()
    original=monthly.write
    def fail(path,value):
        if str(path).endswith('/updates/monthly/active.json'):
            raise OSError('fixture metadata failure')
        original(path,value)
    monkeypatch.setattr(monthly,'write',fail)
    with pytest.raises(OSError,match='metadata failure'):
        monthly.publish(tmp_path,validate=False)
    assert read(audit/'release.json')['status']=='validated'
    assert (tmp_path/'updates/manifest.json').read_bytes()==previous
    assert not (tmp_path/'updates/monthly/state.json').exists()


def test_parent_scope_preserves_child_winner_against_new_direct_estimate():
    from automation.autonomous_projection import preserve_reviewed_rows
    class Policy:
        @staticmethod
        def pooling_rows(rows):
            return [row for row in rows if row.get('subexposure_pooling_selected') is not False]
    previous=[{**study('1'),'Effect ID':'child','disease_hierarchy_pooling_selected':True}]
    current=[{**previous[0],'disease_hierarchy_pooling_selected':False},
             {**study('1'),'Effect ID':'new-parent','disease_hierarchy_pooling_selected':True},
             {**study('2'),'Effect ID':'new-independent','disease_hierarchy_pooling_selected':True}]
    rows=preserve_reviewed_rows(previous,current,Policy,lambda row:(row['PMID'],row['Effect ID']))
    assert rows[0]==previous[0]
    assert rows[1]['display_only'] is True and rows[1]['policy_poolable'] is False
    assert rows[1]['disease_hierarchy_pooling_selected'] is False
    assert rows[2]['disease_hierarchy_pooling_selected'] is True


def test_retry_keeps_output_hashes_against_materialization_baseline(tmp_path,monkeypatch):
    import importlib.util
    import sys
    import types
    from automation import autonomous_projection as projection
    from automation import prostate_scope_results
    monkeypatch.setattr(prostate_scope_results, 'materialize', lambda *_: [])
    module_path=ROOT/'runtime_code/monthly/MetaVir/automation/monthly_ui.py'
    spec=importlib.util.spec_from_file_location('automation.monthly_ui',module_path)
    ui=importlib.util.module_from_spec(spec);spec.loader.exec_module(ui)
    monkeypatch.setitem(sys.modules,'automation.monthly_ui',ui)
    import automation
    monkeypatch.setattr(automation,'monthly_ui',ui,raising=False)
    policy=types.SimpleNamespace(pooling_rows=lambda rows:[r for r in rows if not r.get('display_only')])
    original_import=projection.importlib.import_module
    monkeypatch.setattr(projection.importlib,'import_module',lambda name:policy if name=='scripts.subexposure_policy' else original_import(name))
    relative='combined/fiber/prostate_cancer_incidence_true_all.json'
    write(tmp_path/relative,{'studies':[study('1')], 'exposure':'Fiber','cancer':'Prostate cancer'})
    baseline={relative:sha(tmp_path/relative)}
    write(tmp_path/'automation/monthly_config.json',{'repository':'MetaVir'})
    write(tmp_path/'autonomous_materialization.json',{'ui_baseline_sha256':baseline,'output_sha256':{}})
    run=tmp_path/'data/monthly_updates/fixture'
    monkeypatch.setattr(ui,'_accepted_delta',lambda *_:{('Fiber','Prostate cancer','fixture'):[]})
    payload=read(tmp_path/relative)
    payload['studies'].append({**study('2'),'display_only':True})
    planned={relative:{'payload':payload,'mode':'no','added':1}}
    monkeypatch.setattr(ui,'_plan',lambda *_:planned)
    first=projection.refresh_ui(tmp_path,run,[])
    monkeypatch.setattr(ui,'_plan',lambda *_:{})
    second=projection.refresh_ui(tmp_path,run,[])
    assert first['output_sha256']==second['output_sha256']=={relative:sha(tmp_path/relative)}


def test_new_aggressive_rows_require_endpoint_review_and_frozen_exclusions_apply(monkeypatch):
    import sys
    import types
    from automation.autonomous_projection import endpoint_hold
    endpoint=types.SimpleNamespace(load_checkpoint=lambda:{'records':[]},
        matching_record=lambda row,*a,**kw:{'reason':'Frozen death endpoint'} if row['PMID']=='blocked' else None)
    scripts=types.ModuleType('scripts');scripts.incidence_endpoint_policy=endpoint
    review=types.ModuleType('scripts.aggressive_prostate_review');review.evidence_row_id=lambda row:row['PMID']
    monkeypatch.setitem(sys.modules,'scripts',scripts)
    monkeypatch.setitem(sys.modules,'scripts.aggressive_prostate_review',review)
    row=study('1');endpoint_hold(row,'Aggressive prostate cancer','MetaVir')
    assert row['display_only'] is True and row['manual_review_pending'] is True
    assert row['disease_scope_pooling_selected'] is False
    general=study('2');endpoint_hold(general,'Prostate cancer','MetaVir')
    assert general==study('2')
    blocked=study('blocked');endpoint_hold(blocked,'Prostate cancer','MetaVir')
    assert blocked['display_only'] is True and blocked['display_only_reason']=='Frozen death endpoint'


def test_exported_policy_holds_do_not_modify_native_review_gate_csv(tmp_path,monkeypatch):
    import csv
    import importlib.util
    import sys
    import automation
    from automation.autonomous_scope import export_review_queue
    spec=importlib.util.spec_from_file_location('automation.monthly_review_csv',ROOT/'runtime_code/monthly/MetaVir/automation/monthly_review_csv.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules,'automation.monthly_review_csv',module)
    monkeypatch.setattr(automation,'monthly_review_csv',module,raising=False)
    run=tmp_path/'data/monthly_updates/fixture'
    write(run/'final_screening.json',{'results':[{'context_id':'held','pmid':'1','final_status':'INCLUDE_META','extraction':{'effect_size':1.2}}]})
    source=tmp_path/'monthlyHumanReview/native.csv';source.parent.mkdir(parents=True)
    source.write_text('context_id,pipeline_1_decision,stage,human_review_reason,adjudication_status,human_review_status\n')
    before=source.read_bytes()
    result={'review':{'csv_path':str(source)},'ui_refresh':{'projection_holds':[{'context_id':'held','pmid':'1','disease':'Aggressive prostate cancer','exposure':'Calcium','hold':'endpoint_provenance','reason':'Requires exact endpoint review'}]}}
    target=export_review_queue(tmp_path,run,result)
    assert source.read_bytes()==before
    with target.open(encoding='utf-8-sig',newline='') as handle: rows=list(csv.DictReader(handle))
    assert len(rows)==1 and rows[0]['stage']=='Post-review scope policy'
    assert rows[0]['pipeline_1_decision']=='INCLUDE_META'
    assert rows[0]['adjudication_status']=='policy_hold' and rows[0]['human_review_status']=='pending'


def test_additive_evidence_revision_preserves_and_validates_monthly_audit(tmp_path):
    audit,parent=complete_fixture(tmp_path)
    state=read(tmp_path/'updates/monthly/state.json')
    current=monthly.load_overlay(tmp_path,'MetaVir')
    current.update(kind='evidence_scope_revision',run_id='fixture-scope')
    mp=tmp_path/'updates/MetaVir/versions/fixture-scope/manifest.json';write(mp,current)
    index=read(tmp_path/'updates/manifest.json')
    index['sources']['MetaVir']={'manifest':str(mp.relative_to(tmp_path/'updates')),'sha256':sha(mp)}
    write(tmp_path/'updates/manifest.json',index)
    code=tmp_path/'scope.py';code.write_text('# derived evidence only\n')
    summary=tmp_path/'summary/no/fixture.json';write(summary,{'scope':'derived'})
    revision={'repository':'metaUro','status':'completed','revision_id':'fixture-scope',
        'active_overlay_index':index,'parent_monthly':{'manifest_path':state['manifest_path'],
            'manifest_sha256':state['manifest_sha256'],'run_id':state['run_id'],'cutoff':parent['end_date']},
        'parent_audit_artifact_sha256':parent['audit_artifact_sha256'],
        'summary_sha256':{'summary/no/fixture.json':sha(summary)},'code_sha256':{'scope.py':sha(code)}}
    write(tmp_path/'evidence-revisions/fixture-scope/release.json',revision)
    assert monthly.validate_completed_release(tmp_path)['evidence_revision']=='fixture-scope'
    code.write_text('# tampered\n')
    with pytest.raises(ValueError,match='revision artifact checksum'):
        monthly.validate_completed_release(tmp_path)
