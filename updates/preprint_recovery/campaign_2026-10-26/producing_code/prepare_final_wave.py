"""Prepare the last bounded validation repair and Astra missing-field clarification."""
import json
from pathlib import Path
import preprint_screening as p

BASE = Path(__file__).resolve().parents[1]
CLARIFICATION = {
    'purpose': 'Separate actual unresolved disputes from facts known to be unreported.',
    'rule': ('Keep unresolved_fields as an explicit list of unresolved identity, eligibility, '
        'source alignment or reviewer-bundle discrepancies. A quantity that the preprint '
        'simply does not report is a known limitation, not automatically an unresolved '
        'dispute. Preserve such fields in unreported_fields (and optional_unreported_fields '
        'if optional under the exact native criteria). For ELIGIBLE_NOT_POOLABLE, known '
        'missing quantities can fully settle the nonpoolable classification; retain those '
        'missing facts and never invent values. For INCLUDE_META, a missing quantity that '
        'the native protocol requires for pooling remains an unresolved eligibility problem. '
        'Reassess against the original source and complete reviewer bundles. Do not change '
        'scientific facts to obtain an empty list. Keep UNRESOLVED for any actual dispute, '
        'or when it cannot be determined whether the missing fact is required. Supply '
        'unresolved_fields, unreported_fields and optional_unreported_fields explicitly; '
        'select one complete existing reviewer bundle with no mixing or repairs.')}


def latest_call(job, role):
    matches = []
    ids = sorted(x['context_id'] for x in job['contexts'])
    for path in (BASE/'screening_run/calls'/role).glob('*/dispatch.json'):
        dispatch = json.loads(path.read_text())
        if dispatch['context_ids'] == ids and dispatch['source_sha256'] == job['source']['text_sha256']:
            matches.append((dispatch['created_at'], path, dispatch))
    return max(matches, key=lambda x: x[0]) if matches else None


def main():
    batch = json.loads((BASE/'screening_run/guarded_repair_complete.json').read_text())
    files, audit = [], []
    for item in batch['results']:
        job = json.loads((BASE/item['job_file']).read_text())
        result_path = BASE/'screening_run/jobs'/item['job_id']/'result.json'
        result = json.loads(result_path.read_text())
        repaired, clarify = [], False
        if result['status'] == 'reviewed':
            clarify = any(r['decision'] != 'UNRESOLVED' and r['confidence'] == 'high'
                          and r.get('unresolved_fields') for r in result['adjudication']['results'])
            if clarify:
                response = Path(result['receipts']['astra'])
                previous = json.loads(response.with_name('dispatch.json').read_text())
                attempt = max((previous.get('acknowledged_retry') or {}).get('attempt', 0),
                              (previous.get('rejected_request_retry') or {}).get('attempt', 0)) + 1
                if attempt > 2:
                    raise ValueError('Astra clarification exceeds bounded retry policy')
                job.setdefault('acknowledged_retry', {})['astra'] = {
                    'attempt': attempt, 'reason': 'acknowledged_validation_error',
                    'previous_response_sha256': p.file_hash(response), 'max_completion_tokens': 65536}
                job['adjudication_feedback'] = {**CLARIFICATION,
                    'own_previous_adjudication': result['adjudication']}
        else:
            for role, problem in result.get('problems', {}).items():
                latest = latest_call(job, role)
                if not latest:
                    continue
                _, dispatch_path, dispatch = latest
                attempt = max((dispatch.get('acknowledged_retry') or {}).get('attempt', 0),
                              (dispatch.get('rejected_request_retry') or {}).get('attempt', 0)) + 1
                if attempt > 2:
                    continue
                response_path = dispatch_path.with_name('response.json')
                rejected_path = dispatch_path.with_name('problem.json')
                if response_path.exists():
                    response = json.loads(response_path.read_text())
                    job.get('rejected_request_retry', {}).pop(role, None)
                    job.setdefault('acknowledged_retry', {})[role] = {
                        'attempt': attempt,
                        'reason': 'acknowledged_output_truncation' if response['finish_reason'] == 'length' else 'acknowledged_validation_error',
                        'previous_response_sha256': p.file_hash(response_path), 'max_completion_tokens': 65536}
                    if response['finish_reason'] != 'length':
                        try:
                            previous_output = p.parse_json(response['raw_output'])
                        except ValueError:
                            previous_output = response['raw_output']
                        job.setdefault('review_feedback', {})[role] = {
                            'validation_error': problem,
                            'own_previous_response': previous_output,
                            'quote_format': 'Use short 6–20 word verbatim contiguous passages. Copy source punctuation and any intervening text exactly; do not add a period, elide a URL or paraphrase. At least one quote must support the actual target finding. Preserve all real scientific uncertainty.'}
                elif rejected_path.exists():
                    rejection = json.loads(rejected_path.read_text())
                    if rejection.get('http_status') != 429 or rejection.get('response_received') is not False:
                        continue
                    job.get('acknowledged_retry', {}).pop(role, None)
                    job.setdefault('rejected_request_retry', {})[role] = {
                        'attempt': attempt, 'reason': 'explicit_rate_limit_rejection',
                        'previous_problem_sha256': p.file_hash(rejected_path)}
                else:
                    continue
                repaired.append(role)
            if repaired:
                job['adjudication_feedback'] = CLARIFICATION
        p.validate_source(job)
        path = BASE/'screening_jobs_final'/Path(item['job_file']).name
        p.save(path, job)
        files.append(str(path.relative_to(BASE)))
        audit.append({'job': str(path.relative_to(BASE)), 'failed_roles_repaired': repaired,
                      'astra_missing_field_clarification': clarify})
    p.save(BASE/'screening_jobs_final_manifest.json', {'jobs': files, 'audit': audit,
        'policy': 'last_bounded_repair_no_further_automatic_attempts'})
    print(json.dumps({'jobs': len(files), 'changed_jobs': sum(bool(x['failed_roles_repaired']) or x['astra_missing_field_clarification'] for x in audit)}))


if __name__ == '__main__':
    main()
