"""Export release evidence without whole papers, abstracts, prompts or secrets."""
import csv
import json
import os
from pathlib import Path
import shutil
import preprint_screening as p

BASE = Path(os.environ.get('PREPRINT_WORK_DIR', Path(__file__).resolve().parents[1]))
ROOT = Path(os.environ.get('PREPRINT_WORKSPACE_ROOT', BASE.parent))
REPOS = ['MetaVir', 'metaThyroid', 'metaGut', 'metaFemina', 'metaHeart',
         'metaBreath', 'metaPee', 'metaNeuro', 'MetaCutis_et_ Capillus',
         'metaMeta/metaOnco', 'metaUro']


def csv_file(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fields, extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def main():
    active = json.loads((BASE/'screening_run/active_result_index.json').read_text())
    import native_overlay_converter
    native_overlay_converter.active_result_files(BASE/'screening_run', BASE/'screening_run/active_result_index.json')
    cost = p.cost_report(BASE/'screening_run')
    public = BASE/'release_audit_public'
    public.mkdir(exist_ok=True)
    receipts = []
    for call in cost['calls']:
        dispatch = json.loads(Path(call['dispatch_receipt']).read_text())
        response = json.loads(Path(call['response_receipt']).read_text()) if call['response_receipt'] else {}
        problem_path = Path(call['dispatch_receipt']).with_name('problem.json')
        problem = json.loads(problem_path.read_text()) if problem_path.exists() else {}
        pmids = sorted({s.split('|')[0] for s in call['context_ids']})
        receipts.append({k: v for k, v in {
            **{k: call[k] for k in ('role', 'request_sha256', 'model', 'reasoning_effort',
                'context_ids', 'response_received', 'usage', 'gateway_reported_cost_usd')},
            'published_pmids': pmids, 'created_at': dispatch['created_at'],
            'source_text_sha256': dispatch['source_sha256'],
            'raw_output_sha256': response.get('raw_output_sha256'),
            'response_receipt_sha256': p.file_hash(call['response_receipt']) if response else None,
            'model_reported': response.get('model_reported'),
            'finish_reason': response.get('finish_reason'),
            'error_type': problem.get('error_type'), 'http_status': problem.get('http_status'),
            'acknowledged_retry': dispatch.get('acknowledged_retry'),
            'rejected_request_retry': dispatch.get('rejected_request_retry'),
            'out_of_scope_after_article_PMC_recheck': '34010397' in pmids,
        }.items()})
    p.save(public/'model_cost_and_attempts.json', {
        **{k: v for k, v in cost.items() if k != 'calls'}, 'calls': receipts,
        'cost_scope': 'One shared multi-repository campaign; repository copies are not additional charges.',
        'publication_policy': 'No prompts, raw article bodies or model response bodies are published.'})
    p.save(public/'active_result_index.json', active)
    for relative in ('representative_visual_checks.json',
        'overlay_release_native/converter_tests_verification.json',
        'overlay_release_native/runtime_dependency_verification.json',
        'promotion_receipts/final_converter_verification.json',
        'promotion_receipts/final_runtime_dependency_verification.json',
        'ui_architecture/cohort_and_native_math_immutability.json',
        'ui_architecture/COMMON_FINAL_REPORT.json',
        'ui_architecture/common_optional_release_build.json',
        'ui_architecture/optional_pdf_visual_qa.json'):
        source = BASE/relative
        if not source.is_file():
            raise ValueError('Required release validation evidence is missing: '+relative)
        target = public/'validation'/Path(relative).name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for name in ('model_protocol.json', 'provider_preflight.json'):
        shutil.copyfile(BASE/'model_protocol'/name, public/name)
    sources = {}
    for repo in REPOS:
        source = ROOT/repo/('updates' if repo == 'metaUro' else 'data')/'preprint_recovery/active_manifest.json'
        manifest = json.loads(source.read_text())
        sources[repo] = {'sha256': p.file_hash(source), 'status': manifest['status'],
            'contexts': len(manifest['contexts']),
            'records': sum(len(c.get('native_rows', [])) for c in manifest['contexts'])}
    p.save(public/'application_source_manifest_hashes.json', sources)
    for name in ('native9_promotion.json', 'heart_recorded_cohort_name_completion.json',
                 'uro_promotion.json', 'onco_promotion.json'):
        source = BASE/'promotion_receipts'/name
        target = public/'validation'/name
        shutil.copyfile(source, target)
    for name in ('authorized_linked_article_gate.json', 'linked18_article_preprint_source_gate.json'):
        shutil.copyfile(BASE/'article_availability_gate'/name, public/name)
    for folder, names in {
        'article_availability_gate': ['audit_article_pmc_availability.py'],
        'audit_inventory': ['build_inventory.py', 'apply_saved_lineage_guards.py', 'verify_source_problem_delta.py'],
        'heart_inventory': ['audit_heart.py'],
    }.items():
        for name in names:
            source = BASE/folder/name
            if source.is_file():
                target = public/'producing_code'/folder/name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    code = public/'producing_code'
    code.mkdir(exist_ok=True)
    for src in (BASE/'implementation').glob('*.py'):
        shutil.copyfile(src, code/src.name)
    for src in (BASE/'implementation').glob('*.md'):
        shutil.copyfile(src, code/src.name)
    for name in ('build_common_optional_release.py', 'normalize_native_receipts.py', 'UI_CONTRACT.md'):
        source = BASE/'ui_architecture'/name
        target = code/'ui_architecture'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    snapshots = list((BASE/'screening_run/producer_snapshots').glob('*/*.py'))
    snapshots += list((BASE/'model_protocol/producer_snapshots').glob('*/*.py'))
    for src in snapshots:
        if p.file_hash(src) != src.parent.name[-64:]:
            raise ValueError('Historical producer snapshot hash differs from its receipt')
        dest = code/'historical_producer_snapshots'/src.parent.name/src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    dispositions, discrepancies, operational, packets = [], [], [], {}
    for entry in active['jobs']:
        job = json.loads((BASE/entry['job_file']).read_text())
        result = json.loads((BASE/entry['result_file']).read_text())
        selected_ids = set(entry['context_ids'])
        by_id = {r['context_id']: r for r in job['contexts'] if r['context_id'] in selected_ids}
        if result['status'] != 'reviewed':
            for row in job['contexts']:
                if row['context_id'] not in selected_ids:
                    continue
                operational.append({'repository': job['repository'], 'pmid': row['pmid'],
                    'context_id': row['context_id'], 'exposure': row['exposure'],
                    'disease': row['disease'], 'outcome': row['outcome'],
                    'title': job['source'].get('published_title'),
                    'published_url': 'https://pubmed.ncbi.nlm.nih.gov/'+str(row['pmid'])+'/',
                    'preprint_id': job['source']['preprint_id'], 'preprint_url': job['source']['retrieval_url'],
                    'reason': json.dumps(result.get('problems') or {'status': result['status']}, ensure_ascii=False),
                    'action': 'No activation; inspect preserved model and validation receipts.'})
            continue
        for row in result['adjudication']['results']:
            if row['context_id'] not in selected_ids:
                continue
            ctx = by_id[row['context_id']]
            item = {k: ctx.get(k) for k in ('repository', 'pmid', 'context_id', 'exposure', 'disease', 'outcome')}
            item['repository'] = ctx.get('repository') or job['repository']
            item.update(decision=row['decision'], confidence=row['confidence'],
                selected_reviewer=row.get('selected_reviewer'), reason=row['reason'],
                unresolved_fields=json.dumps(row.get('unresolved_fields'), ensure_ascii=False),
                unreported_fields=json.dumps(row.get('unreported_fields'), ensure_ascii=False),
                optional_unreported_fields=json.dumps(row.get('optional_unreported_fields'), ensure_ascii=False),
                known_missing_fields=json.dumps(row.get('known_missing_fields'), ensure_ascii=False),
                preprint_id=job['source']['preprint_id'],
                preprint_version=job['source'].get('preprint_version'),
                title=job['source'].get('published_title'),
                published_url='https://pubmed.ncbi.nlm.nih.gov/'+str(ctx['pmid'])+'/',
                preprint_url=job['source']['retrieval_url'],
                source_text_sha256=job['source']['text_sha256'],
                result_sha256=p.file_hash(BASE/entry['result_file']),
                astra_response_sha256=p.file_hash(result['receipts']['astra']))
            dispositions.append(item)
            if row['decision'] == 'UNRESOLVED' or row['confidence'] != 'high' or row.get('unresolved_fields'):
                packet_name = 'context_'+p.digest([job['repository'], row['context_id']])[:24]+'.json'
                item['review_packet'] = 'packets/'+packet_name
                comparison = {}
                for role, review in result['reviews'].items():
                    reviewed = next(r for r in review['results'] if r['context_id'] == row['context_id'])
                    comparison[role] = {
                        'identity_valid': review['identity_valid'], 'identity_reason': review['identity_reason'],
                        **{k: reviewed.get(k) for k in ('status', 'reason', 'cohort_names', 'jbi', 'unresolved_fields')},
                        'extraction': {k: reviewed.get('extraction', {}).get(k) for k in p.QUANTITATIVE},
                        'response_receipt_sha256': p.file_hash(result['receipts'][role])}
                packets[packet_name] = {'schema_version': 1, 'context': item,
                    'source': {k: job['source'].get(k) for k in ('published_pmid', 'published_title',
                        'published_doi', 'preprint_id', 'preprint_doi', 'preprint_version',
                        'preprint_posted_date', 'retrieval_url', 'text_sha256', 'raw_source_sha256')},
                    'reviewers': comparison,
                    'astra': {k: row.get(k) for k in ('decision', 'selected_reviewer', 'confidence',
                        'reason', 'unresolved_fields', 'unreported_fields', 'optional_unreported_fields', 'known_missing_fields')},
                    'policy': 'Facts and short reviewer rationales only; original source/model bodies remain private.'}
                discrepancies.append(item)
    fields = ['repository', 'pmid', 'context_id', 'exposure', 'disease', 'outcome',
              'title', 'published_url', 'preprint_id', 'preprint_url', 'preprint_version',
              'decision', 'confidence', 'selected_reviewer',
              'reason', 'unresolved_fields', 'unreported_fields', 'optional_unreported_fields', 'known_missing_fields',
              'source_text_sha256', 'result_sha256', 'astra_response_sha256', 'review_packet']
    csv_file(public/'context_dispositions.csv', dispositions, fields)
    for name, packet in packets.items():
        p.save(public/'packets'/name, packet)
    exclusions = []
    original = json.loads((BASE/'screening_jobs_manifest.json').read_text())
    source_gate = json.loads((BASE/'article_availability_gate/authorized_linked_article_gate.json').read_text())
    source_allowed = {r['pmid']: r['model_dispatch_authorized'] for r in source_gate['records']}
    for relative in original['jobs']:
        job = json.loads((BASE/relative).read_text())
        if source_allowed.get(job['source']['published_pmid']) is not False:
            continue
        for ctx in job['contexts']:
            exclusions.append({k: ctx.get(k) for k in ('repository', 'pmid', 'context_id', 'exposure', 'disease', 'outcome')}
                | {'reason': 'Published full PMC text is available in another saved screening track; preprint recovery forbidden.',
                   'preprint_id': job['source']['preprint_id'], 'earlier_attempts_and_costs_retained': True})
    csv_file(public/'scope_exclusions.csv', exclusions,
        ['repository', 'pmid', 'context_id', 'exposure', 'disease', 'outcome', 'preprint_id', 'reason', 'earlier_attempts_and_costs_retained'])
    handoff = json.loads((BASE/'europepmc_research/versioned_body_handoff.json').read_text())['records']
    matched = json.loads((BASE/'audit_inventory/matched_preprint_native_contexts.json').read_text())['contexts']
    unavailable = []
    for source in handoff:
        if source.get('selected_source'):
            continue
        for ctx in matched:
            if ctx['pmid'] == source['published_pmid']:
                unavailable.append({k: ctx.get(k) for k in ('repository', 'pmid', 'context_id', 'exposure', 'disease', 'outcome')}
                    | {'preprint_id': source['preprint_id'], 'reason': source.get('access_state') or 'No verified complete public body',
                       'title': source.get('published_title'),
                       'published_url': 'https://pubmed.ncbi.nlm.nih.gov/'+str(ctx['pmid'])+'/',
                       'preprint_url': 'https://europepmc.org/article/PPR/'+source['preprint_id'],
                       'action': 'Source unavailable; no scientific exclusion or native result change.'})
    p.save(public/'campaign_summary.json', {'active_jobs': len(active['jobs']),
        'dispositions': len(dispositions), 'scientific_discrepancies': len(discrepancies),
        'operational_context_holds': len(operational), 'linked_body_unavailable_contexts': len(unavailable),
        'gateway_reported_cost_usd': cost['gateway_reported_cost_usd'], 'unknown_cost_calls': cost['unknown_cost_calls'],
        'defaults': {'include_preprints': False}, 'PMC_available_article_excluded': '34010397'})
    (public/'README.md').write_text(
        'PMID-linked Europe PMC preprint recovery, 2026-10-26\n\n'
        'The Settings option include_preprints defaults to false. Original journal results and '
        'source records are preserved. Opted-in records must pass the article-level PMC '
        'availability check, the original native scientific criteria, independent Luna xhigh '
        'and Gemini Flash review, and high-confidence resolved Astra adjudication. Recovered '
        'entries are marked navy and identify the preprint version. Original journal '
        'publication dates remain the cohort-latest anchors; preprint dates are separate.\n\n'
        'This directory contains extracted facts, source identifiers/versions/URLs/hashes, '
        'sanitized retrieval receipts, producing code and financial evidence. Full papers, '
        'abstracts, prompts and raw model responses remain in the private recovery campaign '
        'and are not published here. Absolute paths in historical proof descriptors identify '
        'the original audit environment; website runtime reads only the separately hash-bound '
        'repository-local proof and native rows. Reproducing retrieval/review requires '
        'recreating the private campaign inputs and rebinding their verified hashes.\n\n'
        'The cost ledger describes one shared multi-repository campaign, including failed '
        'and superseded attempts. Copies in several repositories are not separate charges. '
        'Missing billing amounts are unknown and are not treated as zero. Europe PMC public '
        'metadata retrieval needs no API key.\n', encoding='utf-8')
    lineage_path = BASE/'release_conversion_holds_by_repository.json'
    if not lineage_path.is_file():
        raise ValueError('Final native and derivative conversion hold audit is required')
    lineage = json.loads(lineage_path.read_text())
    p.save(public/'conversion_holds_by_repository.json', lineage)
    for repo in REPOS:
        root = ROOT/repo
        destination = root/('updates' if repo == 'metaUro' else 'data')/'preprint_recovery/campaign_2026-10-26'
        shutil.copytree(public, destination, dirs_exist_ok=True)
        shutil.copytree(BASE/'europepmc_research/public_export', destination/'europepmc', dirs_exist_ok=True)
        review_repo_root = ROOT/'metaMeta' if repo == 'metaMeta/metaOnco' else root
        review_root = review_repo_root/'manualReview/preprint_recovery/2026-10-26'
        csv_file(review_root/'source_lineage_holds.csv', lineage.get(repo, []),
            ['repository', 'pmid', 'context_id', 'exposure', 'disease', 'outcome', 'reason', 'action'])
        scope_path = BASE/'special_runtime/projection_manual_scope.json'
        projection_scope = json.loads(scope_path.read_text()) if scope_path.exists() else {}
        def relevant(row):
            if repo in {'metaMeta/metaOnco', 'metaUro'}:
                scope = projection_scope.get('metaMeta' if repo == 'metaMeta/metaOnco' else repo)
                if scope is None:
                    raise ValueError('Exact derivative manual-review context scope is missing: '+repo)
                return row['repository']+'|'+row['context_id'] in scope
            return row['repository'] == repo
        csv_file(review_root/'discrepancies.csv', [x for x in discrepancies if relevant(x)], fields)
        csv_file(review_root/'operational_holds.csv', [x for x in operational if relevant(x)],
            ['repository', 'pmid', 'context_id', 'exposure', 'disease', 'outcome', 'title', 'published_url', 'preprint_id', 'preprint_url', 'reason', 'action'])
        csv_file(review_root/'source_unavailable.csv', [x for x in unavailable if relevant(x)],
            ['repository', 'pmid', 'context_id', 'exposure', 'disease', 'outcome', 'title', 'published_url', 'preprint_id', 'preprint_url', 'reason', 'action'])
        for row in discrepancies:
            if relevant(row):
                name = Path(row['review_packet']).name
                p.save(review_root/'packets'/name, packets[name])
        (review_root/'README.md').write_text(
            'This queue contains the remaining preprint-recovery issues from 2026-10-26.\n\n'
            'Scientific disagreements, operational validation holds and unavailable bodies are separate tables. '
            'None changes the original saved journal screening decision. Resolved and excluded preprint decisions '
            'are retained in the campaign audit. Source-dependent aggregate repositories reference their original '
            'review repository; those papers were not billed again.\n', encoding='utf-8')
        recovery_root = root/('updates' if repo == 'metaUro' else 'data')/'preprint_recovery'
        manifest_path = recovery_root/'active_manifest.json'
        if not manifest_path.is_file():
            raise ValueError('Final runtime source manifest has not been promoted: '+repo)
        p.save(recovery_root/'release_freeze_receipt.json', {
            'schema_version': 1, 'repository': repo, 'status': 'complete', 'released_at': p.now(),
            'active_manifest_sha256': p.file_hash(manifest_path),
            'active_result_index_sha256': p.file_hash(BASE/'screening_run/active_result_index.json'),
            'model_cost_ledger_sha256': p.file_hash(public/'model_cost_and_attempts.json'),
            'export_producer_sha256': p.file_hash(Path(__file__)),
            'include_preprints_default': False,
            'generation_status_note': 'This final receipt completes the immutable generation manifest\'s pending release-freeze state; its scientific inputs and source rows are unchanged.',
            'remaining_scientific_discrepancies': sum(relevant(x) for x in discrepancies),
            'remaining_operational_context_holds': sum(relevant(x) for x in operational),
            'remaining_conversion_holds': len(lineage.get(repo, []))})
    print(json.dumps({'repositories_exported': len(REPOS), 'discrepancies': len(discrepancies), 'operational_holds': len(operational)}))


if __name__ == '__main__':
    main()
