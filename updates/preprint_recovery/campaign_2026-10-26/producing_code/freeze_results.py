"""Freeze one explicit authoritative result per current native context."""
import json
from pathlib import Path
import preprint_screening as p

BASE = Path(__file__).resolve().parents[1]


def main():
    pending = [str(f.parent) for f in (BASE/'screening_run/calls').glob('*/*/dispatch.json')
               if not f.with_name('response.json').is_file() and not f.with_name('problem.json').is_file()]
    if pending:
        raise ValueError('Cannot freeze while dispatched calls lack an acknowledgement/error receipt')
    manifest = json.loads((BASE/'screening_jobs_final_manifest.json').read_text())
    jobs, authority = [], {}
    for relative in manifest['jobs']:
        planned = json.loads((BASE/relative).read_text())
        p.validate_source(planned)
        job_id = p.digest({'job': planned, 'text_sha256': planned['source']['text_sha256'], 'version': p.VERSION})
        result_path = BASE/'screening_run/jobs'/job_id/'result.json'
        inputs_path = result_path.with_name('inputs.json')
        inputs = json.loads(inputs_path.read_text())
        result = json.loads(result_path.read_text())
        if p.digest(inputs) != p.digest(planned) or result.get('job_id') != job_id:
            raise ValueError('Frozen job/result identity differs from final plan')
        if result['status'] == 'reviewed':
            text = p.validate_source(inputs)
            for review in result['reviews'].values():
                p.validate_review(review, inputs, text)
            p.validate_adjudication(result['adjudication'], inputs, text, result['reviews'])
        elif result['status'] != 'operational_or_validation_hold':
            raise ValueError('An incomplete job has no explicit hold outcome')
        ids = [c['context_id'] for c in inputs['contexts']]
        for cid in ids:
            key = inputs['repository']+'|'+cid
            if key in authority:
                raise ValueError('Duplicate native context in final source view')
            authority[key] = job_id
        jobs.append({'job_id': job_id, 'repository': inputs['repository'],
            'job_file': str(inputs_path.relative_to(BASE)), 'plan_job_file': relative,
            'result_file': str(result_path.relative_to(BASE)), 'result_path': str(result_path.resolve()),
            'inputs_sha256': p.file_hash(inputs_path), 'result_sha256': p.file_hash(result_path),
            'context_ids': ids, 'status': result['status']})
    index = {'schema_version': 1, 'frozen_at': p.now(), 'jobs': jobs,
        'authoritative_context_jobs': authority,
        'policy': 'One latest explicitly scoped result per native context; all superseded attempts remain audited.',
        'excluded_PMC_available_PMID': '34010397'}
    p.save(BASE/'screening_run/active_result_index.json', index)
    import native_overlay_converter
    native_overlay_converter.active_result_files(BASE/'screening_run', BASE/'screening_run/active_result_index.json')
    cost = p.cost_report(BASE/'screening_run')
    print(json.dumps({'frozen_jobs': len(jobs), 'frozen_contexts': len(authority),
        **{k: v for k, v in cost.items() if k != 'calls'}}))


if __name__ == '__main__':
    main()
