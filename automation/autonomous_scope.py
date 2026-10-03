"""Isolated monthly worker; copied into a writable, repository-local scope view."""
from __future__ import annotations
import argparse
import json
import csv
import hashlib
from pathlib import Path


def export_review_queue(root, run, result):
    """Add downstream policy holds without changing the native application gate."""
    source = Path(result['review']['csv_path'])
    if not source.is_absolute():
        source = root/source
    target = root/'monthlyHumanReview'/(run.name+'_export.csv')
    with source.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    primary = {row['context_id']:row for row in json.loads((run/'final_screening.json').read_text())['results']}
    from automation.monthly_review_csv import safe, side
    independent = {}
    for stage, names in (('R2',('claude_r2_results.json','claude_r2_progress.json')),
                         ('R3',('gemini_r3_results.json','gemini_r3_progress.json'))):
        path = next((run/'monthly_parallel'/name for name in names if (run/'monthly_parallel'/name).is_file()),None)
        if path:
            packet = json.loads(path.read_text())
            for item in packet.get('results',[]):
                independent[item['context_id']] = (item,stage,packet.get('model') or '')
    for hold in result['ui_refresh'].get('projection_holds', []):
        record = primary.get(hold['context_id'], {})
        extraction = record.get('extraction') or {}
        row = {'review_id':hashlib.sha256(json.dumps(hold,sort_keys=True).encode()).hexdigest(),
               'run_id':run.name, 'source_scope':root.name, **hold, 'human_review_reason':hold['reason'],
               'disagreement_fields':hold['hold'], 'stage':'Post-review scope policy',
               'human_review_status':'pending', 'adjudication_status':'policy_hold',
               'pipeline_1':'Primary GPT', 'pipeline_1_decision':record.get('final_status'),
               'primary_claims':json.dumps(record,ensure_ascii=False,sort_keys=True),
               'pubmed_url':'https://pubmed.ncbi.nlm.nih.gov/'+hold['pmid']+'/', 'outcome':'Incidence'}
        row.update({'pipeline_1_'+key:extraction.get(key) for key in ('effect_size','effect_type','ci_lower','ci_upper','total_n','cases')})
        row['pipeline_1_model'] = record.get('model') or ''
        if hold['context_id'] in independent:
            claim,stage,model = independent[hold['context_id']]
            values = side(claim,'Independent Claude/Gemini',stage,model)
            for key,value in values.items():
                row['pipeline_2' if key=='pipeline' else 'pipeline_2_'+key] = value
            row['validation_claims'] = json.dumps(claim,ensure_ascii=False,sort_keys=True)
        rows.append({key:safe(value) for key,value in row.items()})
    with target.open('w',encoding='utf-8-sig',newline='') as handle:
        writer = csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', required=True, choices=('metaPee', 'MetaVir'))
    parser.add_argument('--start-date', required=True)
    parser.add_argument('--end-date', required=True)
    args = parser.parse_args(argv)
    from automation import monthly_pipeline as pipeline
    from automation.autonomous_projection import refresh_ui
    root = Path.cwd().resolve()
    config = pipeline.load_config(root, 'automation/monthly_config.json')
    config['repository'] = args.scope
    # The parent pins the complete window and runtime; native immutable packets
    # provide exact request-level resume without loading historical R1 lineages.
    from datetime import date, timedelta
    config['bootstrap'] = {'last_successful_publication_date': str(date.fromisoformat(args.start_date) - timedelta(days=1)),
                           'provenance': 'metaUro autonomous pinned scope watermark'}
    config.pop('adapter', None)
    config['validation_commands'] = [[
        '{python}', 'scripts/validate_monthly_artifacts.py', '--start-date', '{start_date}',
        '--end-date', '{end_date}', '--updates-root', 'data/monthly_updates',
        '--reports-root', 'data/monthly_reports']]
    config['ui_commands'] = []
    pipeline.atomic_json(root / 'automation/monthly_config.json', config)
    try:
        result = pipeline.run_pipeline(root, config, ui_refresher=refresh_ui,
                                      today=date.fromisoformat(args.end_date) + timedelta(days=1))
    except BaseException:
        # A later native validation failure must still expose any downstream
        # policy holds in the checkpoint's root human-review export.
        run = root/'data/monthly_updates'/(args.start_date+'_'+args.end_date)
        try:
            saved = pipeline.read_json(run/pipeline.MANIFEST_FILENAME)
            saved['ui_refresh'] = pipeline.read_json(run/'ui_refresh_manifest.json')
            if saved['ui_refresh'].get('projection_holds'):
                export_review_queue(root,run,saved)
        except (OSError, ValueError, KeyError, pipeline.PipelineError):
            pass
        raise
    if result.get('status') != 'validated':
        raise RuntimeError('Monthly evidence pipeline did not validate')
    run = root / 'data/monthly_updates' / result['run_id']
    parallel = pipeline.read_json(run/'monthly_parallel/monthly_status.json')
    if parallel.get('complete') is not True or parallel.get('models') != {'r2':'claude-opus-5','r3':'gemini-3.7-flash'}:
        raise RuntimeError('Independent Claude/Gemini review did not complete')
    review = result['review']
    exported_queue = export_review_queue(root, run, result)
    pending = set(review['blocked_context_ids']) | {row['context_id'] for row in result['ui_refresh'].get('projection_holds', [])}
    receipt = {'repository': 'metaUro', 'source_scope': args.scope, 'status': 'validated',
               'run_id': result['run_id'], 'start_date': args.start_date, 'end_date': args.end_date,
               'models': {'primary': ['gpt-5.6-luna','gpt-5.6-sol','gpt-5.6-luna'],
                          'validation': ['shared verified R1','claude-opus-5','gemini-3.7-flash']},
               'raw_packet_sha256': pipeline.packet_hashes(run),
               'independent_review': parallel,
               'native_manifest_sha256': pipeline.sha256(run / pipeline.MANIFEST_FILENAME),
               'application_gate_sha256': result['application_gate_sha256'],
               'human_review_pending': len(pending),
               'human_review_csv': str(exported_queue),
               'native_human_review_csv': review['csv_path'],
               'projection_hold_context_ids': sorted(pending - set(review['blocked_context_ids'])),
               'output_sha256': result['ui_refresh']['output_sha256'],
               'ui_refresh': result['ui_refresh']}
    pipeline.atomic_json(run / 'autonomous_scope.json', receipt)
    print(json.dumps({key: receipt[key] for key in ('source_scope','status','run_id','human_review_pending')}, indent=2))


if __name__ == '__main__':
    main()
