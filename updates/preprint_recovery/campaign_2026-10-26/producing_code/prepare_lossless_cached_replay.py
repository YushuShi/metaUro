"""Find held jobs whose paid responses now pass lossless lexical validation.

No reviewer requests are added. The replay may need the first Astra adjudication
of a newly valid pair; it cannot call a reviewer without an exact paid cache.
"""
import json
from pathlib import Path
import preprint_screening as p

BASE = Path(__file__).resolve().parents[1]


def cached_review(job, role, text):
    prompt = p.review_prompt(job, text)
    feedback = job.get('review_feedback', {}).get(role)
    if feedback:
        prompt += ('\n\nPREVIOUS ACKNOWLEDGED RESPONSE VALIDATION FEEDBACK:\n'
            + json.dumps(feedback, ensure_ascii=False)
            + '\nReturn a fresh complete JSON for the same contexts and source only.\n'
            'Copy short source passages exactly; do not paraphrase evidence quotes.\n'
            'Do not change scientific facts to satisfy validation. Unsupported facts\n'
            'must remain null/uncertain. Preserve each native context exactly once.')
    model, effort = p.MODELS[role]
    request = {'protocol_version': p.VERSION, 'model': model, 'reasoning_effort': effort,
        'drop_params': False, 'system': p.SOURCE_RULE, 'prompt': prompt,
        'source_sha256': job['source']['text_sha256'],
        'context_ids': sorted(x['context_id'] for x in job['contexts'])}
    for key in ('acknowledged_retry', 'rejected_request_retry'):
        retry = job.get(key, {}).get(role)
        if retry:
            request[key] = retry
            request['max_completion_tokens'] = 65536
    response = BASE/'screening_run/calls'/role/p.digest(request)/'response.json'
    if not response.exists():
        raise ValueError('Exact paid reviewer response does not exist')
    raw = json.loads(response.read_text())
    if (raw['request_sha256'] != p.digest(request) or raw['finish_reason'] == 'length'
            or not p.model_matches(model, raw['model_reported'])):
        raise ValueError('Incompatible paid response')
    return p.validate_review(p.parse_json(raw['raw_output']), job, text)


def main():
    manifest = json.loads((BASE/'screening_jobs_final_manifest.json').read_text())
    jobs, audit = [], []
    for relative in manifest['jobs']:
        job = json.loads((BASE/relative).read_text())
        job_id = p.digest({'job': job, 'text_sha256': job['source']['text_sha256'], 'version': p.VERSION})
        result_path = BASE/'screening_run/jobs'/job_id/'result.json'
        if not result_path.exists() or json.loads(result_path.read_text())['status'] == 'reviewed':
            continue
        text = p.validate_source(job)
        errors = {}
        for role in ('luna', 'gemini'):
            try:
                cached_review(job, role, text)
            except (ValueError, KeyError) as exc:
                errors[role] = str(exc)
        audit.append({'job': relative, 'valid_cached_pair': not errors, 'errors': errors})
        if not errors:
            jobs.append(relative)
    p.save(BASE/'screening_jobs_lossless_replay_manifest.json', {'jobs': jobs,
        'audit': audit, 'policy': 'exact_cached_reviewers_only_no_new_reviewer_request'})
    print(json.dumps({'cached_pairs_ready_for_final_adjudication': len(jobs), 'audit': audit}))


if __name__ == '__main__':
    main()
