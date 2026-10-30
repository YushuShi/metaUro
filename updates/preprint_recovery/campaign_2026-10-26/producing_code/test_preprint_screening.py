import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
import preprint_screening as pipeline


@pytest.fixture
def source_job(tmp_path):
    text = 'Methods and Results. Higher intake had an HR of 1.2 (95% CI 1.1 to 1.3).\n' * 150
    path = tmp_path/'source.txt'
    path.write_text(text)
    gate_path = tmp_path/'gate.json'
    gate_record = {'pmid': '123456', 'availability': 'pmcid_missing',
                   'preprint_fetch_allowed': True, 'model_dispatch_authorized': True,
                   'saved_any_track_checked': True, 'saved_any_track_full_text_available': False}
    pipeline.save(gate_path, {'records': [gate_record]})
    job = {'native_criteria': 'Native direct-exposure and endpoint criteria',
           'pmc_availability_gate': {'path': str(gate_path), 'sha256': pipeline.file_hash(gate_path),
                 'pmid': '123456', 'record_sha256': pipeline.digest(gate_record)},
           'source': {'preprint_id': 'PPR123', 'published_pmid': '123456',
                      'relation': {'type': 'Preprint in', 'id': 'PPR123'},
                      'text_path': str(path), 'text_sha256': pipeline.file_hash(path),
                      'full_text_verified': True, 'raw_path': str(path), 'raw_source_sha256': pipeline.file_hash(path),
                      'retrieval_url': 'https://example.org/source-v1.pdf'},
           'contexts': [{'context_id': '123456|Food|Disease', 'pmid': '123456',
                         'dispatch_authorized': True}]}
    extraction = {k: None for k in pipeline.QUANTITATIVE}
    extraction.update(effect_type='HR', effect_size=1.2, ci_lower=1.1, ci_upper=1.3,
                      total_n=1000, cases=50)
    payload = {'identity_valid': True, 'identity_reason': 'Same article DOI and population',
               'results': [{'context_id': job['contexts'][0]['context_id'],
                            'status': 'INCLUDE_META', 'reason': 'Direct source result',
                            'extraction': extraction, 'cohort_names': ['NHS II'],
                            'jbi': {'checklist_type': 'cohort', 'answers': {
                                'q'+str(i): 'Yes' for i in range(1, 12)}},
                            'evidence_quotes': [{'quote': 'Higher intake had an HR of 1.2 (95% CI 1.1 to 1.3).'}]}]}
    return job, payload, text


def test_source_requires_native_dispatch_and_exact_preprint_link(source_job):
    job, _, _ = source_job
    pipeline.validate_source(job)
    bad = copy.deepcopy(job)
    bad['contexts'][0]['dispatch_authorized'] = False
    with pytest.raises(ValueError, match='lineage'):
        pipeline.validate_source(bad)
    bad = copy.deepcopy(job)
    bad['source']['relation']['id'] = 'PPR999'
    with pytest.raises(ValueError, match='another source'):
        pipeline.validate_source(bad)


def test_changed_source_is_rejected(source_job):
    job, _, _ = source_job
    from pathlib import Path
    Path(job['source']['text_path']).write_text('unrelated replacement')
    with pytest.raises(ValueError, match='checksum'):
        pipeline.validate_source(job)


@pytest.mark.parametrize('change', [
    {'availability': 'available'}, {'availability': 'unknown'},
    {'saved_any_track_full_text_available': True}, {'saved_any_track_checked': False}])
def test_any_track_pmc_availability_blocks_preprints(source_job, change):
    from pathlib import Path
    job, _, _ = source_job
    gate = job['pmc_availability_gate']
    report = json.loads(Path(gate['path']).read_text())
    report['records'][0].update(change)
    pipeline.save(gate['path'], report)
    gate.update(sha256=pipeline.file_hash(gate['path']), record_sha256=pipeline.digest(report['records'][0]))
    with pytest.raises(ValueError, match='PMC full text'):
        pipeline.validate_source(job)


def test_raw_source_hash_is_checked_separately(source_job, tmp_path):
    job, _, _ = source_job
    raw = tmp_path/'source.pdf'
    raw.write_bytes(b'changed')
    job['source']['raw_path'] = str(raw)
    with pytest.raises(ValueError, match='Raw preprint'):
        pipeline.validate_source(job)


def test_continuous_effects_can_be_negative_but_ratios_cannot(source_job):
    job, payload, text = source_job
    row = payload['results'][0]['extraction']
    row.update(effect_type='MD', effect_size=-2, ci_lower=-3, ci_upper=-1)
    pipeline.validate_review(payload, job, text)
    row['effect_type'] = 'RR'
    with pytest.raises(ValueError, match='Nonpositive ratio'):
        pipeline.validate_review(payload, job, text)


def test_sourced_quotes_and_exact_context_coverage_are_required(source_job):
    job, payload, text = source_job
    bad = copy.deepcopy(payload)
    bad['results'][0]['evidence_quotes'][0]['quote'] = 'invented source finding'
    with pytest.raises(ValueError, match='quotes'):
        pipeline.validate_review(bad, job, text)


def test_added_terminal_period_is_removed_only_from_a_verbatim_excerpt(source_job):
    job, payload, text = source_job
    source = text.replace('1.3).', '1.3) (fully adjusted).')
    quote = payload['results'][0]['evidence_quotes'][0]
    original = quote['quote']
    pipeline.validate_review(payload, job, source)
    assert quote['quote'] == original[:-1]
    assert quote['quote_original'] == original
    assert pipeline.verify_quote(quote['quote'], source)
    quote['quote'] = 'Higher intake had a lower risk in a different unreported model.'
    with pytest.raises(ValueError, match='quotes'):
        pipeline.validate_review(payload, job, source)
    bad = copy.deepcopy(payload)
    bad['results'].append(copy.deepcopy(bad['results'][0]))
    with pytest.raises(ValueError, match='duplicate'):
        pipeline.validate_review(bad, job, text)


def test_complete_cohort_round_names_remain_distinct(source_job):
    job, payload, text = source_job
    payload['results'][0]['cohort_names'] = ['NHS', 'NHS II', 'NHS III']
    result = pipeline.validate_review(payload, job, text)
    assert result['results'][0]['cohort_names'] == ['NHS', 'NHS II', 'NHS III']


def test_native_heart_nonapplicable_jbi_items_stay_na(source_job):
    job, payload, text = source_job
    answers = payload['results'][0]['jbi']['answers']
    answers.update(q12='NA', q13='NA')
    pipeline.validate_review(payload, job, text)
    answers['q13'] = 'Yes'
    with pytest.raises(ValueError, match='checklist'):
        pipeline.validate_review(payload, job, text)


def test_bounded_trend_p_is_parsed_without_turning_it_into_exact_contrast_p(source_job):
    job, payload, text = source_job
    extraction = payload['results'][0]['extraction']
    extraction.update(p_value='<0.001', p_value_relation='Trend, not selected contrast',
                      p_value_provenance={'exact': False, 'kind': 'trend', 'same_estimate': False})
    result = pipeline.validate_review(payload, job, text)['results'][0]['extraction']
    assert result['p_value'] == .001
    assert result['p_value_original'] == '<0.001'
    assert result['p_value_relation'] == 'Trend, not selected contrast'
    assert result['p_value_provenance']['same_estimate'] is False


def test_native_numeric_spelling_does_not_impute_new_statistics(source_job):
    job, payload, text = source_job
    ext = payload['results'][0]['extraction']
    ext.update(ci_level='95%', cases='1,000', total_n='2,000', standard_error='NA')
    pipeline.validate_review(payload, job, text)
    assert ext['ci_level'] == 95
    assert ext['cases'] == 1000
    assert ext['total_n'] == 2000
    assert ext['standard_error'] is None
    assert ext['native_lexical_parses']['ci_level']['original'] == '95%'


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(ValueError, match='Duplicate JSON'):
        pipeline.parse_json('{"effect_size":1.1,"effect_size":2.2}')


def test_identical_duplicate_value_has_no_ambiguity_and_preserves_audit():
    parsed = pipeline.parse_json('{"extraction":{"cases":null,"cases":null}}')
    assert parsed['extraction']['cases'] is None
    assert parsed['_json_identical_duplicate_values'][0]['key'] == 'cases'
    assert parsed['_json_identical_duplicate_values'][0]['value_sha256'] == pipeline.digest(None)


def test_astra_cannot_mix_bundles_or_override_unrelated_source(source_job):
    job, payload, text = source_job
    final = {'results': [{'context_id': job['contexts'][0]['context_id'],
                         'decision': 'INCLUDE_META', 'selected_reviewer': 'luna',
                         'confidence': 'high', 'reason': 'Same result verified',
                         'unresolved_fields': [],
                         'evidence_quote': 'Higher intake had an HR of 1.2 (95% CI 1.1 to 1.3).'}]}
    pipeline.validate_adjudication(final, job, text, {'luna': payload})
    bad = copy.deepcopy(final)
    bad['results'][0]['selected_reviewer'] = 'new_mixed_estimate'
    with pytest.raises(ValueError, match='complete reviewer bundle'):
        pipeline.validate_adjudication(bad, job, text, {'luna': payload})
    payload['identity_valid'] = False
    with pytest.raises(ValueError, match='unrelated or unverified'):
        pipeline.validate_adjudication(final, job, text, {'luna': payload})


class FakeClient:
    def __init__(self, payload, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = 0
        self.finish_reason = 'stop'
        self.chat = SimpleNamespace(completions=SimpleNamespace(with_raw_response=self))

    def with_options(self, **kwargs):
        assert kwargs['max_retries'] == 0
        return self

    def create(self, **kwargs):
        self.calls += 1
        assert kwargs['reasoning_effort'] == 'xhigh'
        assert kwargs['extra_body'] == {'drop_params': False}
        if self.fail:
            raise ConnectionError('connection interrupted')
        response = SimpleNamespace(model='gpt-6-luna',
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.payload)), finish_reason=self.finish_reason)],
            usage=SimpleNamespace(model_dump=lambda: {'prompt_tokens': 100, 'completion_tokens': 20}))
        return SimpleNamespace(parse=lambda: response, headers={'x-litellm-response-cost': '0.123'})


def test_exact_response_replay_does_not_rebill(source_job, tmp_path):
    job, payload, _ = source_job
    client = FakeClient(payload)
    first = pipeline.call_model(client, tmp_path, 'luna', 'system', 'prompt', job)
    second = pipeline.call_model(client, tmp_path, 'luna', 'system', 'prompt', job)
    assert client.calls == 1
    assert first == second
    report = pipeline.cost_report(tmp_path)
    assert report['dispatches'] == 1
    assert report['gateway_reported_cost_usd'] == .123
    assert report['unknown_cost_calls'] == 0


def test_unacknowledged_attempt_is_not_repeated_or_costed_zero(source_job, tmp_path):
    job, payload, _ = source_job
    client = FakeClient(payload, fail=True)
    with pytest.raises(ConnectionError):
        pipeline.call_model(client, tmp_path, 'luna', 'system', 'prompt', job)
    with pytest.raises(RuntimeError, match='unacknowledged'):
        pipeline.call_model(client, tmp_path, 'luna', 'system', 'prompt', job)
    assert client.calls == 1
    report = pipeline.cost_report(tmp_path)
    assert report['unknown_cost_calls'] == 1
    assert report['acknowledged_responses'] == 0


def test_acknowledged_truncation_can_retry_with_separate_budget_and_cost_receipt(source_job, tmp_path):
    job, payload, _ = source_job
    client = FakeClient(payload)
    client.finish_reason = 'length'
    with pytest.raises(ValueError, match='truncated'):
        pipeline.call_model(client, tmp_path, 'luna', 'system', 'prompt', job)
    response = next((tmp_path/'calls/luna').glob('*/response.json'))
    retry_job = copy.deepcopy(job)
    retry_job['acknowledged_retry'] = {'luna': {
        'reason': 'acknowledged_output_truncation', 'attempt': 1,
        'max_completion_tokens': 65536,
        'previous_response_sha256': pipeline.file_hash(response)}}
    client.finish_reason = 'stop'
    pipeline.call_model(client, tmp_path, 'luna', 'system', 'prompt', retry_job)
    assert client.calls == 2
    receipts = [json.loads(p.read_text()) for p in (tmp_path/'calls/luna').glob('*/dispatch.json')]
    assert len(receipts) == 2
    assert any(p.get('max_completion_tokens') == 65536 for p in receipts)
    assert pipeline.cost_report(tmp_path)['gateway_reported_cost_usd'] == .246


def test_only_explicit_rejection_can_retry_without_a_completion(source_job, tmp_path):
    job, payload, _ = source_job
    client = FakeClient(payload, fail=True)
    with pytest.raises(ConnectionError):
        pipeline.call_model(client, tmp_path, 'luna', 'system', 'prompt', job)
    problem_path = next((tmp_path/'calls/luna').glob('*/problem.json'))
    problem = json.loads(problem_path.read_text())
    retry = copy.deepcopy(job)
    retry['rejected_request_retry'] = {'luna': {
        'reason': 'explicit_rate_limit_rejection', 'attempt': 1,
        'previous_problem_sha256': pipeline.file_hash(problem_path)}}
    with pytest.raises(ValueError, match='matching explicit HTTP'):
        pipeline.call_model(client, tmp_path, 'luna', 'system', 'prompt', retry)
    problem['http_status'] = 429
    pipeline.save(problem_path, problem)
    retry['rejected_request_retry']['luna']['previous_problem_sha256'] = pipeline.file_hash(problem_path)
    client.fail = False
    pipeline.call_model(client, tmp_path, 'luna', 'system', 'prompt', retry)
    assert client.calls == 2
    assert pipeline.cost_report(tmp_path)['unknown_cost_calls'] == 1
