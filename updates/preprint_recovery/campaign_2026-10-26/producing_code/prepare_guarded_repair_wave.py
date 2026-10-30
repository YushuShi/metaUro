"""Prepare one bounded repair wave, reusing every valid paid response.

No network calls. Historical job inputs, attempts and cost receipts are retained.
"""
import copy
import json
from pathlib import Path
import preprint_screening as p

BASE = Path(__file__).resolve().parents[1]


def main():
    report = json.loads((BASE/'screening_run/offline_revalidation.json').read_text())
    gate_path = BASE/'article_availability_gate/authorized_linked_article_gate.json'
    gates = {r['pmid']: r for r in json.loads(gate_path.read_text())['records']}
    override_path = BASE/'converter_inputs/neuro_minimized_context_overrides.json'
    overrides = json.loads(override_path.read_text()) if override_path.exists() else {}
    contexts = overrides.get('overrides_by_context_id', overrides.get('contexts', overrides))
    if isinstance(contexts, list):
        contexts = {r['context_id']: r for r in contexts}
    jobs, audit = [], []
    for item in report['jobs']:
        if item.get('status') == 'source_ineligible_PMC_available':
            audit.append({'job': item['job'], 'status': item['status']})
            continue
        job = json.loads((BASE/item['job']).read_text())
        pmid = job['source']['published_pmid']
        gate = gates[pmid]
        job['pmc_availability_gate'] = {'path': str(gate_path.resolve()),
            'sha256': p.file_hash(gate_path), 'pmid': pmid,
            'record_sha256': p.digest(gate)}
        needs_minimizing = any((v.get('problem') or {}).get('http_status') == 400
                              for v in item['roles'].values())
        if needs_minimizing:
            for row in job['contexts']:
                override = contexts.get(row['context_id'])
                if not override:
                    raise ValueError('Missing precise native R2 input override: '+row['context_id'])
                row['r2_records'] = copy.deepcopy(override['r2_records'])
                if len(json.dumps(row['r2_records'])) > 100000:
                    raise ValueError('Oversized R2 input remains after target-row selection')
        for role, state in item['roles'].items():
            if state.get('valid'):
                continue
            folder = BASE/'screening_run/calls'/role/state['fingerprint']
            response_path = folder/'response.json'
            if response_path.exists():
                saved = json.loads(response_path.read_text())
                truncated = saved['finish_reason'] == 'length'
                job.setdefault('acknowledged_retry', {})[role] = {
                    'reason': 'acknowledged_output_truncation' if truncated else 'acknowledged_validation_error',
                    'attempt': 1, 'max_completion_tokens': 65536,
                    'previous_response_sha256': p.file_hash(response_path)}
                if not truncated:
                    job.setdefault('review_feedback', {})[role] = {
                        'validation_error': state.get('error'),
                        'own_previous_response': p.parse_json(saved['raw_output'])}
            else:
                problem = state.get('problem') or {}
                status = problem.get('http_status')
                if status not in {400, 429} or problem.get('response_received') is not False:
                    raise ValueError('Ambiguous prior dispatch cannot be retried automatically')
                job.setdefault('rejected_request_retry', {})[role] = {
                    'reason': 'explicit_input_rejection_corrected' if status == 400 else 'explicit_rate_limit_rejection',
                    'attempt': 1, 'previous_problem_sha256': p.file_hash(folder/'problem.json')}
        p.validate_source(job)
        destination = BASE/'screening_jobs_repair'/Path(item['job']).name
        p.save(destination, job)
        jobs.append(str(destination.relative_to(BASE)))
        audit.append({'job': str(destination.relative_to(BASE)), 'pmid': pmid,
            'reused_roles': [r for r, s in item['roles'].items() if s.get('valid')],
            'repaired_roles': [r for r, s in item['roles'].items() if not s.get('valid')],
            'r2_target_rows_minimized': needs_minimizing})
    p.save(BASE/'screening_jobs_repair_manifest.json', {'jobs': jobs,
        'job_count': len(jobs), 'policy': 'one_bounded_failed_role_repair_wave',
        'audit': audit, 'gate_sha256': p.file_hash(gate_path)})
    print(json.dumps({'jobs': len(jobs), 'source_ineligible_removed': 1}))


if __name__ == '__main__':
    main()
