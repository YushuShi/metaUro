"""Append a reviewed delta to metaUro's preserved active caches only.

Native screening applies to its isolated extraction cache. This projection is
the sole path into published evidence: all historical rows and scientific gates
are retained, and only affected analysis contexts are recalculated.
"""
from __future__ import annotations
import base64
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile


def cache_profiles(repo, scope):
    if scope == 'MetaVir':
        roots = [('combined','no'), ('Subexposure_results/no','no'), ('Subexposure_results/yes','yes')]
    else:
        roots = [('combined/provisional_ui/Cached_results','no'),
                 ('combined/provisional_ui/subexposure_views/no/Cached_results','no'),
                 ('combined/provisional_ui/subexposure_views/yes/Cached_results','yes')]
    return [{'cache': path, 'mode': mode, 'plot': None} for path,mode in roots if (repo/path).is_dir()]


def preserve_reviewed_rows(previous, incoming, policy, identity):
    """Keep approved parent selections when a new correlated child is found."""
    old_ids = {identity(row) for row in previous}
    selected = {str(row.get('PMID')) for row in policy.pooling_rows(previous)
                if row.get('disease_hierarchy_pooling_selected') is not False
                and row.get('disease_scope_pooling_selected') is not False}
    new = [copy.deepcopy(row) for row in incoming if identity(row) not in old_ids]
    for row in new:
        if str(row.get('PMID')) in selected:
            row.update(subexposure_pooling_selected=False,
                       disease_hierarchy_pooling_selected=False,
                       display_only=True, policy_poolable=False, manual_poolable=False,
                       monthly_projection_hold='overlapping_parent_publication',
                       subexposure_pooling_reason='Existing reviewed publication estimate retained in the parent scope to avoid correlated duplication.')
    return copy.deepcopy(previous) + new


def endpoint_hold(row, disease, scope):
    """Do not let a generic extraction fabricate a diagnosis-defined subtype."""
    if scope != 'MetaVir':
        return
    from scripts import incidence_endpoint_policy as endpoint
    from scripts.aggressive_prostate_review import evidence_row_id
    record = endpoint.matching_record(row, endpoint.load_checkpoint(), source_row_id=evidence_row_id(row))
    if record or disease == 'Aggressive prostate cancer':
        reason = (record['reason'] if record else
                  'New aggressive-prostate estimate needs a validated per-estimate diagnosis endpoint and endpoint-specific case-count review. Generated outcome labels are not source evidence.')
        row.update(display_only=True, policy_poolable=False, manual_poolable=False,
                   subexposure_pooling_selected=False, disease_scope_pooling_selected=False,
                   display_only_reason=reason, monthly_projection_hold='endpoint_provenance',
                   manual_review_pending=True)


def _calculate(repo, scope, relative, item, policy):
    import pandas as pd
    import meta_analysis
    import analysis_settings_backend as settings
    from runtime_code.trend_ci_runtime import install_core, install_completion
    from automation import monthly_ui as ui
    install_core(meta_analysis)
    install_completion()
    payload = item['payload']
    rows = payload['studies']
    candidates = [row for row in policy.pooling_rows(rows)
                  if row.get('disease_hierarchy_pooling_selected') is not False
                  and row.get('disease_scope_pooling_selected') is not False]
    disease = payload.get('disease') or payload['cancer']
    exposure = payload['exposure']
    catalog = ui._read(repo/'static/seer-baseline.json')
    options = settings.normalize_settings(None, catalog)
    baseline = settings.resolve_baseline(catalog, disease, None, options)
    previous = (payload.get('headline') or {}).get('baseline_risk')
    if previous is not None:
        baseline.update(p0=previous, risk_percent=previous*100, available=True, identity_approximation=False)
    context = {'settings':options, 'baseline':baseline, 'disease':disease, 'exposure':exposure,
               'repository':'metaUro', 'quality_filter':'Moderate+', 'min_cases':0,
               'pooled_plot_rows':[], 'pooled_study_keys':set()}
    token = settings._ACTIVE.set(context)
    oldcwd = Path.cwd()
    key = hashlib.sha256(json.dumps([rows,item['mode'],disease,exposure],sort_keys=True).encode()).hexdigest()[:20]
    try:
        with tempfile.TemporaryDirectory(prefix='metauro-monthly-plot-') as folder:
            os.chdir(folder)
            if candidates:
                result = meta_analysis.perform_meta_analysis(pd.DataFrame(candidates), disease, exposure,
                    outcome='Incidence', exclude_meta=True, df_all=pd.DataFrame(rows), generate_plots=True)
                if result.get('error') or result.get('success') is False:
                    raise ValueError('Monthly pool calculation failed')
            else:
                result = {'success':True, 'headline':None, 'summary_html':'<p>No studies meet the pooling criteria.</p>',
                          'plot_url':None,'funnel_plot_url':None,'baujat_plot_url':None}
            for field in ('plot_url','funnel_plot_url','baujat_plot_url'):
                url = result.get(field)
                if not url:
                    continue
                if str(url).startswith('data:image/png;base64,'):
                    blob = base64.b64decode(str(url).split(',',1)[1])
                    tail = Path(field+'.png')
                else:
                    original = Path(folder)/str(url).split('?',1)[0]
                    if not original.is_file():
                        raise ValueError('Monthly plot was not written')
                    blob = original.read_bytes()
                    tail = original.relative_to(Path(folder)/'static')
                if scope == 'metaPee':
                    target = repo/'combined/provisional_ui/static'/item['mode']/'monthly'/key/tail
                    result[field] = f"provisional-assets/{item['mode']}/monthly/{key}/" + str(tail)
                else:
                    target = repo/'static/subexposures'/item['mode']/'monthly'/key/tail
                    result[field] = f"static/subexposures/{item['mode']}/monthly/{key}/" + str(tail)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
            result.pop('studies',None)
            payload.update(ui._clean(result), studies=rows, pooled_study_count=len(context['pooled_plot_rows']))
            if isinstance(payload.get('forest_plot'),dict):
                payload['forest_plot']['url'] = payload.get('plot_url')
            if isinstance(payload.get('screening_stats'),dict):
                payload['screening_stats']['extracted'] = len(rows)
            return payload
    finally:
        os.chdir(oldcwd)
        settings._ACTIVE.reset(token)


def refresh_ui(repo_root, run_dir, blocked_context_ids):
    from automation import monthly_ui as ui
    repo, run = Path(repo_root).resolve(), Path(run_dir).resolve()
    scope = ui._read(repo/'automation/monthly_config.json')['repository']
    delta = ui._accepted_delta(repo, run, blocked_context_ids)
    for (_exposure, disease, _filename), rows in delta.items():
        for row in rows:
            endpoint_hold(row, disease, scope)
    profiles = cache_profiles(repo, scope)
    if not profiles:
        raise ValueError('No supported metaUro active cache layout')
    report = {'schema_version':1,'status':'no_new_accepted_rows','output_sha256':{},
              'accepted_contexts':len(delta),'blocked_context_ids':sorted(blocked_context_ids),
              'historical_rows_preserved':True}
    materialized = ui._read(repo/'autonomous_materialization.json')
    catalog_outputs = materialized.get('output_sha256', {})
    for relative, digest in catalog_outputs.items():
        if relative != 'static/i18n-translations.json' or hashlib.sha256((repo/relative).read_bytes()).hexdigest() != digest:
            raise ValueError('Materialized presentation catalog has changed')
    report['output_sha256'].update(catalog_outputs)
    policy = importlib.import_module('scripts.subexposure_policy')
    plan = ui._plan(repo, profiles, delta, policy) if delta else {}
    roots = [repo/profile['cache'] for profile in profiles]
    roots += [repo/'static/subexposures',repo/'combined/provisional_ui/static']
    # Compare every replay against the initial writable-copy inventory. A
    # later stage failure must never erase the already-applied delta's hashes.
    before = materialized['ui_baseline_sha256']
    for relative,item in plan.items():
        previous = ui._read(repo/relative,{})
        old_rows = previous.get('studies',[])
        if item['payload']['studies'][:len(old_rows)] != old_rows:
            raise ValueError('Monthly projection changes historical rows')
        # Existing correlated estimates keep their reviewed selection. New rows
        # from the same PMID/target remain visible but cannot displace a pool.
        selected_pmids = {str(r.get('PMID')) for r in policy.pooling_rows(old_rows)}
        incoming = item['payload']['studies'][len(old_rows):]
        for row in incoming:
            if str(row.get('PMID')) in selected_pmids:
                row.update(subexposure_pooling_selected=False,
                           display_only=True, policy_poolable=False, manual_poolable=False,
                           monthly_projection_hold='overlapping_publication',
                           subexposure_pooling_reason='Existing reviewed estimate from this publication is retained to avoid counting a correlated estimate twice.')
        if policy.pooling_rows(incoming):
            payload = _calculate(repo,scope,relative,item,policy)
        else:
            # Display-only additions cannot change previously approved pooled
            # estimates, uncertainty, weights, or diagnostic plot bytes.
            payload = item['payload']
            if isinstance(payload.get('screening_stats'),dict):
                payload['screening_stats']['extracted'] = len(payload['studies'])
        if payload['studies'][:len(old_rows)] != old_rows:
            raise ValueError('Calculation mutated historical scientific rows')
        ui._write(repo/relative,payload)
    # Disease hierarchy derivatives are rebuilt from conserved direct caches.
    if scope == 'metaPee' and plan:
        from scripts import provisional_disease_scopes
        affected = {item['payload']['exposure'] for item in plan.values()}
        for profile in profiles:
            provisional_disease_scopes.materialize(repo/profile['cache'],affected_exposures=affected)
        # Native hierarchy builders rank all rows again. Preserve the approved
        # historical parent winner when a new same-publication direct/child row
        # would otherwise displace it, then recalculate that parent only.
        source_root = Path(materialized['source_root'])
        for profile in profiles:
            for path in (repo/profile['cache']).glob('*/*_true_*.json'):
                payload = ui._read(path)
                if payload.get('exposure') not in affected or not payload.get('disease_hierarchy_prebuilt'):
                    continue
                relative = str(path.relative_to(repo))
                previous_payload = ui._read(source_root/relative, {})
                previous = previous_payload.get('studies', [])
                protected = preserve_reviewed_rows(previous, payload.get('studies', []), policy, ui._identity)
                new_poolable = [row for row in policy.pooling_rows(protected[len(previous):])
                                if not row.get('display_only') and row.get('disease_hierarchy_pooling_selected') is not False]
                if new_poolable:
                    payload['studies'] = protected
                    item = {'payload':payload,'mode':profile['mode']}
                    ui._write(path, _calculate(repo,scope,relative,item,policy))
                else:
                    # A display-only descendant must not erase the parent plot
                    # or replace its approved headline with a new calculation.
                    preserved = copy.deepcopy(previous_payload) if previous_payload else payload
                    preserved['studies'] = protected
                    if isinstance(preserved.get('screening_stats'), dict):
                        preserved['screening_stats']['extracted'] = len(protected)
                    ui._write(path, preserved)
    after = ui._hashes(repo,roots)
    changed = {**catalog_outputs, **{path:after[path] for path in after if after[path] != before.get(path)}}
    if set(before)-set(after):
        raise ValueError('Autonomous monthly projection deleted an existing result')
    holds = {}
    for relative in changed:
        if not relative.endswith('.json') or relative == 'static/i18n-translations.json':
            continue
        packet = ui._read(repo/relative)
        for row in packet.get('studies', []):
            if row.get('monthly_projection_hold') and row.get('Monthly Context ID'):
                cid = row['Monthly Context ID']
                holds[cid] = {'context_id':cid, 'pmid':str(row.get('PMID')), 'exposure':row.get('Direct Exposure'),
                              'disease':packet.get('disease') or packet.get('cancer'),
                              'reason':row.get('display_only_reason') or row.get('subexposure_pooling_reason'),
                              'hold':row['monthly_projection_hold']}
    report.update(status='refreshed',output_sha256=changed,changed_paths=sorted(changed),
                  projection_holds=list(holds.values()),
                  refreshed_scopes=len(plan),added_cache_rows=sum(item['added'] for item in plan.values()))
    ui._write(run/'ui_refresh_manifest.json',report)
    return report
