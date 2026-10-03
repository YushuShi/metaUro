"""Mocked service throttling never becomes empty evidence or an invalid-key claim."""
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
import io
from pathlib import Path
from unittest import mock

import pytest
from Bio import Entrez
from scripts import ncbi_resilience as resilience


class Http429(RuntimeError):
    code = 429
    headers = {'Retry-After': '3'}


@pytest.fixture(autouse=True)
def clean_service_state(monkeypatch):
    monkeypatch.setattr(resilience, '_THROTTLED_KEYS', set())
    monkeypatch.setattr(resilience, '_COOLDOWN_UNTIL', 0.0)
    clock = [100.0]
    monkeypatch.setattr(resilience.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(resilience.time, 'sleep', mock.Mock(side_effect=lambda seconds: clock.__setitem__(0, clock[0]+seconds)))
    monkeypatch.setenv('NCBI_EMAIL', 'fixture@example.org')
    monkeypatch.setenv('NCBI_API_KEY', 'fixture-throttled-key')
    monkeypatch.setenv('MONTHLY_AUTOMATION_REQUIRE_GATE', 'true')
    for name in ('email', 'api_key', 'max_tries'):
        monkeypatch.setattr(Entrez, name, getattr(Entrez, name))


@pytest.fixture
def engine(monkeypatch):
    if (Path(__file__).resolve().parents[1] / 'scripts/full_ncbi_rescreen.py').is_file():
        from scripts import full_ncbi_rescreen as full
        module = full.screenlib
        module._throttle_test_search = lambda: full._entrez_search_page('same exact query', date(2026, 1, 1), date(2026, 1, 2), retstart=0, retmax=5)
    else:
        from scripts import monthly_evidence_update as module
        module._throttle_test_search = lambda: module.entrez_search('same exact query', '2026-01-01', '2026-01-02')
    for name in ('load_dotenv', 'load_runtime_environment', 'load_external_credentials_if_requested',
                 'load_authorized_external_credentials', 'configure_ncbi_socket_timeout', 'wait_for_ncbi_request_slot'):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, mock.Mock())
    for name in ('_ENTREZ_REJECTED_KEY_DIGEST', '_ENTREZ_VALIDATED_KEY_DIGEST'):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, None)
    if hasattr(module, 'NCBI_RATE_LIMITER'):
        monkeypatch.setattr(module.NCBI_RATE_LIMITER, 'wait', mock.Mock())
    Entrez.api_key = 'fixture-throttled-key'
    return module


def test_monthly_keyed_pacing_leaves_headroom_without_changing_manual_defaults(monkeypatch):
    assert resilience.request_interval(.11, keyed=True) >= 1
    assert resilience.request_interval(.37, keyed=False) == .37
    monkeypatch.delenv('MONTHLY_AUTOMATION_REQUIRE_GATE')
    assert resilience.request_interval(.11, keyed=True) == .11


def test_throttle_reason_is_distinct_and_retry_after_is_respected(capsys):
    Entrez.api_key = 'fixture-throttled-key'
    assert resilience.handle_throttle(Http429('HTTP 429'), Entrez.api_key, Entrez)
    assert Entrez.api_key is None
    assert resilience.is_throttled('fixture-throttled-key')
    resilience.time.sleep.assert_called_once_with(3.0)
    output = capsys.readouterr().out
    assert 'throttled (HTTP 429)' in output and 'invalid' not in output
    assert 'fixture-throttled-key' not in output


def test_long_server_retry_after_fails_closed_without_early_retry():
    error = Http429('HTTP 429')
    error.headers = {'Retry-After': '120'}
    with pytest.raises(RuntimeError, match='bounded retry window'):
        resilience.handle_throttle(error, 'fixture-throttled-key', Entrez)
    resilience.time.sleep.assert_not_called()


def test_http_date_retry_after_is_understood():
    error = Http429('HTTP 429')
    error.headers = {'Retry-After': format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)}
    assert 18 <= resilience.retry_after_seconds(error) <= 20


def test_wrapped_backend_429_is_recognized_but_other_numbers_are_not():
    assert resilience.http_status(RuntimeError('Search Backend failed: HTTP request returned 429 status.')) == 429
    assert resilience.http_status(RuntimeError('429 records returned')) is None


def test_key_validation_429_retries_same_query_publicly_and_latches(engine):
    if not hasattr(engine, 'fetch_pmc_full_text') or engine.__name__.endswith('screen_additional_articles'):
        pytest.skip('Neuro native configuration performs no validation ESearch')
    attempts = []
    def request(**kwargs):
        attempts.append((kwargs.get('api_key'), {key:value for key,value in kwargs.items() if key != 'api_key'}))
        if kwargs.get('api_key'):
            raise Http429('HTTP 429')
        return io.BytesIO(b'fixture')
    with mock.patch.object(Entrez, 'esearch', side_effect=request), mock.patch.object(Entrez, 'read', return_value={'Count':'0','IdList':[]}):
        assert engine.configure_entrez() == 'throttled_http_429'
        assert engine.configure_entrez() == 'throttled_http_429'
    assert len(attempts) == 2 and attempts[0][1] == attempts[1][1]
    assert [a[0] for a in attempts] == ['fixture-throttled-key', None]
    assert engine._ENTREZ_REJECTED_KEY_DIGEST is None


def test_validation_public_failure_is_not_success(engine):
    if engine.__name__.endswith('screen_additional_articles'):
        pytest.skip('Neuro native configuration performs no validation ESearch')
    with mock.patch.object(Entrez, 'esearch', side_effect=Http429('HTTP 429')):
        with pytest.raises(RuntimeError):
            engine.configure_entrez()
    assert Entrez.api_key is None


def test_runtime_discovery_retries_exact_parameters_publicly(engine):
    attempts = []
    def request(**kwargs):
        attempts.append((kwargs.get('api_key'), {key:value for key,value in kwargs.items() if key != 'api_key'}))
        if kwargs.get('api_key'):
            raise Http429('HTTP 429')
        return io.BytesIO(b'fixture')
    with mock.patch.object(Entrez, 'esearch', side_effect=request), mock.patch.object(Entrez, 'read', return_value={'Count':'0','IdList':[]}):
        engine._throttle_test_search()
    assert len(attempts) >= 2 and attempts[0][1] == attempts[1][1]
    assert all(a[0] is None for a in attempts[1:])
    assert engine._ENTREZ_REJECTED_KEY_DIGEST is None
    # A later configure call may not restore the throttled optional key.
    with mock.patch.object(Entrez, 'esearch') as request:
        engine.configure_entrez()
    request.assert_not_called()
    assert Entrez.api_key is None


def test_public_429_discovery_exhaustion_raises_not_empty_success(engine):
    with mock.patch.object(Entrez, 'esearch', side_effect=Http429('HTTP 429')) as request:
        with pytest.raises(RuntimeError):
            engine._throttle_test_search()
    assert request.call_count >= 2
    assert Entrez.api_key is None


def test_runtime_fetch_retries_exact_parameters_publicly(engine):
    attempts = []
    def request(**kwargs):
        attempts.append((kwargs.get('api_key'), {key:value for key,value in kwargs.items() if key != 'api_key'}))
        if kwargs.get('api_key'):
            raise Http429('HTTP 429')
        return io.BytesIO(b'<article/>')
    with mock.patch.object(Entrez, 'efetch', side_effect=request):
        if engine.__name__.endswith('screen_additional_articles'):
            with mock.patch.object(Entrez, 'read', return_value={'PubmedArticle': [{}]}), mock.patch.object(engine, 'parse_pubmed_article', return_value=('1', {'PMID':'1'})):
                assert engine.fetch_pubmed_batch(['1']) == {'1': {'PMID':'1'}}
        else:
            engine.fetch_pmc_full_text('PMC1')
    assert len(attempts) == 2 and attempts[0][1] == attempts[1][1]
    assert [a[0] for a in attempts] == ['fixture-throttled-key', None]


def test_configure_and_request_capture_never_restore_a_throttled_key():
    Entrez.api_key = 'fixture-throttled-key'
    resilience.handle_throttle(Http429('HTTP 429'), Entrez.api_key, Entrez)
    assert resilience.configure_key(Entrez, 'fixture-throttled-key') is None
    # Even a legacy external assignment is cleared before the wire credential is captured.
    Entrez.api_key = 'fixture-throttled-key'
    assert resilience.request_key(Entrez) is None
    assert Entrez.api_key is None


def test_cooldown_is_shared_between_workers_and_extensions_are_observed(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(resilience.time, 'monotonic', lambda: now[0])
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) == 1:
            # A different worker extends the service cooldown during our wait.
            resilience._COOLDOWN_UNTIL = 105.0
    monkeypatch.setattr(resilience.time, 'sleep', sleep)
    resilience._COOLDOWN_UNTIL = 103.0
    resilience.wait_for_cooldown()
    assert sleeps == [3.0, 2.0]
    assert now[0] == 105.0


@pytest.mark.parametrize('status,expected', [(502, True), (400, False), (404, False)])
def test_specific_backend_envelope_retry_classification(status, expected):
    error = RuntimeError(f'Search Backend failed: Pubmed 2.0 search API: HTTP request returned {status} status.')
    assert resilience.retryable_error(error, lambda error: True) is expected


def test_validation_retries_backend_502_without_changing_query_or_key():
    attempts = []
    Entrez.api_key = 'fixture-throttled-key'
    def request(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError('Search Backend failed: Pubmed 2.0 search API: HTTP request returned 502 status.')
        return io.BytesIO(b'fixture')
    with mock.patch.object(Entrez, 'esearch', side_effect=request), mock.patch.object(Entrez, 'read', return_value={'Count':'0','IdList':[]}):
        assert resilience.validate_key(Entrez, term='same query', wait=lambda: None, key_error=lambda error,key: False, retryable=lambda error: False) == 'working'
    assert len(attempts) == 2 and attempts[0] == attempts[1]


def test_runtime_search_retries_embedded_backend_502_with_identical_parameters(engine):
    attempts=[]
    def request(**kwargs):
        attempts.append(kwargs)
        if len(attempts)==1:
            raise RuntimeError('Search Backend failed: Pubmed 2.0 search API: HTTP request returned 502 status.')
        return io.BytesIO(b'fixture')
    with mock.patch.object(Entrez,'esearch',side_effect=request), mock.patch.object(Entrez,'read',return_value={'Count':'0','IdList':[]}):
        engine._throttle_test_search()
    assert len(attempts)==2 and attempts[0]==attempts[1]
