"""No-network regressions for current/saved PMC-first source eligibility."""
import json,hashlib
from unittest.mock import Mock
import pytest
import preprint_retrieval_gate as gate
import fetch_linked_fulltext as fetcher


def fixture(tmp_path,monkeypatch,fresh='pmcid_missing',saved=False):
    monkeypatch.setattr(gate,'WORKSPACE',tmp_path)
    freshrow={'pmid':'123','availability':fresh,'identity_sources':{'current':'official'}}
    savedrow={'pmid':'123','verified_full_pmc_available_any_track':saved,'preprint_source_eligible':not saved}
    reports=[]
    for name,row in [('fresh',freshrow),('saved',savedrow)]:
        p=tmp_path/(name+'.json');p.write_text(json.dumps({'records':{'123':row}}));reports.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    p=tmp_path/'gate.json';p.write_text(json.dumps({'schema_version':1,'source_reports':reports,'records':[{'pmid':'123','availability':fresh,'saved_any_track_checked':True,'saved_any_track_full_text_available':saved,'preprint_fetch_allowed':True,'any_track_gate_record_sha256':gate._canonical_hash(savedrow)}]}));return p


def test_missing_pmc_and_no_any_track_body_permits_retrieval_only(tmp_path,monkeypatch):
    p=fixture(tmp_path,monkeypatch)
    proof=gate.authorize_preprint_retrieval('123',p)
    assert proof['preprint_fetch_allowed'] is True and proof['model_dispatch_authorized'] is False
    with pytest.raises(gate.PreprintRetrievalDenied):gate.authorize_preprint_retrieval('124',p)


@pytest.mark.parametrize('fresh,saved',[('available',False),('unknown',False),('pmcid_missing',True)])
def test_full_pmc_any_track_or_unknown_never_allows_fallback(tmp_path,monkeypatch,fresh,saved):
    p=fixture(tmp_path,monkeypatch,fresh,saved)
    with pytest.raises(gate.PreprintRetrievalDenied):gate.authorize_preprint_retrieval('123',p)


def test_bound_report_tamper_and_mismatched_fresh_fact_are_denied(tmp_path,monkeypatch):
    p=fixture(tmp_path,monkeypatch);(tmp_path/'saved.json').write_text('{}')
    with pytest.raises(gate.PreprintRetrievalDenied,match='changed'):gate.authorize_preprint_retrieval('123',p)
    p=fixture(tmp_path,monkeypatch,fresh='available');d=json.load(open(p));d['records'][0]['availability']='pmcid_missing';p.write_text(json.dumps(d))
    with pytest.raises(gate.PreprintRetrievalDenied,match='disagrees'):gate.authorize_preprint_retrieval('123',p)


def test_gate_runs_before_any_preprint_capture_or_network(monkeypatch):
    capture=Mock();monkeypatch.setattr(fetcher,'capture',capture)
    def deny(*a,**k):raise gate.PreprintRetrievalDenied('existing full PMC')
    monkeypatch.setattr(fetcher,'authorize_preprint_retrieval',deny)
    with pytest.raises(gate.PreprintRetrievalDenied):fetcher.fetch('123','PPRtest','https://example.com/paper.pdf',{})
    capture.assert_not_called()


def test_generic_unavailable_and_unproven_fulltext_failure_denied(tmp_path,monkeypatch):
    for state in ('unavailable','full_text_unavailable'):
        p=fixture(tmp_path,monkeypatch,fresh=state)
        with pytest.raises(gate.PreprintRetrievalDenied):gate.authorize_preprint_retrieval('123',p)


def test_one_format_error_cannot_stand_in_for_all_pmc_unavailability(tmp_path,monkeypatch):
    p=fixture(tmp_path,monkeypatch,fresh='full_text_unavailable')
    fresh=tmp_path/'fresh.json';d=json.load(open(fresh));d['records']['123']['proofs']=[{'format':'xml','status':500}];fresh.write_text(json.dumps(d))
    doc=json.load(open(p));doc['source_reports'][0]['sha256']=hashlib.sha256(fresh.read_bytes()).hexdigest();p.write_text(json.dumps(doc))
    with pytest.raises(gate.PreprintRetrievalDenied,match='actual-full-PMC'):gate.authorize_preprint_retrieval('123',p)
