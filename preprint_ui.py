"""Read-only, explicitly opted-in views of reviewed preprint recovery data.

This module performs no retrieval or model calls. Disabled views return their
input object unchanged, including when optional recovery data is unavailable.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path


class PreprintViewError(ValueError):
    """A released recovery dataset cannot safely be applied to this source."""


def request_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if str(value).lower() in {'true', 'yes', '1'}:
        return True
    if str(value).lower() in {'false', 'no', '0', ''}:
        return False
    raise PreprintViewError('include_preprints must be true or false.')


def preprint_manifest_path(repo_root):
    root = Path(repo_root)
    uro_layout = (root / 'source_worker.py').is_file() and (root / 'evidence-manifest.json').is_file()
    folder = 'updates' if root.name == 'metaUro' or uro_layout else 'data'
    return root / folder / 'preprint_recovery' / 'active_manifest.json'


def preprint_manifest_stamp(repo_root):
    path = preprint_manifest_path(repo_root)
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else 'unavailable'


def preprint_variant_root(root, enabled=False):
    if not enabled:
        return root
    path = Path(root)
    if 'preprints_included' in path.parts:
        return path
    if path.name == 'cohort_latest':
        return path.parent / 'preprints_included' / path.name
    return path / 'preprints_included'


def record_identity(row):
    # Use the same persisted effect identity as the cohort metadata sidecar.
    from cohort_identity import record_metadata_key
    return record_metadata_key(row)


def _recovered_row(row):
    if not isinstance(row, dict):
        return False
    return bool(row.get('preprint_derived') or row.get('preprint_context_id') or row.get('preprint_source')
                or record_identity(row).startswith('id:preprint-recovery:'))


_STATISTIC_FIELDS = {'Effect Size', 'Lower CI', 'Upper CI', 'SE', 'Sample Size', 'Cases',
    'Participants', 'Estimated Cases', 'P Value', 'effect', 'effect_size', 'ci_low', 'ci_high',
    'ci_lower', 'ci_upper', 'ci_upp', 'standard_error', 'sample_size', 'cases', 'total_n', 'p_value'}


def _introduced_metadata(context, repo_root, rows, native_rows):
    # A reanalysis submits only selected rows, so use the bound original cache
    # schema when available, rather than trusting browser-provided null fields.
    cached_keys = None
    for relative in context.get('source_cache_paths') or []:
        digest = (context.get('baseline_files') or {}).get(relative)
        if not digest or Path(relative).suffix != '.json':
            continue
        path = _bound_file(repo_root, relative, digest)
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get('studies'), list):
            if cached_keys is None:
                cached_keys = set()
            cached_keys.update(key for row in payload['studies'] if isinstance(row, dict) for key in row)
    baseline = cached_keys if cached_keys is not None else {key for row in rows for key in row}
    return {key for row in native_rows for key in row}.difference(baseline, _STATISTIC_FIELDS)


def normalize_preprint_frame(frame, metadata_fields):
    """Keep missing introduced provenance nullable before native policy guards.

    Scientific value columns and every provided peer value remain unchanged.
    This fixes pandas' truthy NaNs in new list/dict/bool/optional metadata.
    """
    fields = set(metadata_fields or []).difference(_STATISTIC_FIELDS).intersection(frame.columns)
    if not fields:
        return frame
    result = frame.copy(deep=True)
    for field in fields:
        result[field] = result[field].astype(object).where(result[field].notna(), None)
    return result


def _bound_file(repo_root, relative, expected):
    root = Path(repo_root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise PreprintViewError(f'Preprint source binding is unavailable: {relative}')
    if sha256(path.read_bytes()).hexdigest() != expected:
        raise PreprintViewError(f'Preprint source binding changed: {relative}')
    return path


def _read_manifest(repo_root):
    path = preprint_manifest_path(repo_root)
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise PreprintViewError('Preprint recovery manifest is unreadable.') from exc
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or manifest.get('status') != 'complete' or not isinstance(manifest.get('contexts'), list):
        raise PreprintViewError('Preprint recovery dataset is not a complete supported release.')
    return manifest


def _accepted(context):
    review = context.get('review') or {}
    return review.get('status') in {'settled', 'adjudicated'} and review.get('decision') == 'include'


def _validate_article_gate(context, repo_root):
    """Only unavailable-PMC articles can activate a reviewed preprint source."""
    binding = context.get('article_availability_gate') or {}
    pmid = str((context.get('publication_anchor') or {}).get('pmid') or '')
    if not pmid or str(binding.get('pmid') or '') != pmid or not binding.get('path') or not binding.get('sha256') or not binding.get('record_sha256'):
        raise PreprintViewError('Preprint source eligibility is not bound to an article availability review.')
    path = _bound_file(repo_root, binding['path'], binding['sha256'])
    try:
        proof = json.loads(path.read_text())
        if not isinstance(proof, dict):
            raise ValueError('unsupported article gate')
        records = proof.get('records')
        if proof.get('schema_version') != 1 or not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            raise ValueError('unsupported article gate')
        matches = [record for record in records if str(record.get('pmid')) == pmid]
        if len(matches) != 1:
            raise ValueError('article gate identity is not unique')
        record = matches[0]
        digest = sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                   separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        availability = record.get('availability')
        allowed = (digest == binding['record_sha256']
            and availability in {'pmcid_missing', 'full_text_unavailable'}
            and record.get('preprint_fetch_allowed') is True
            and record.get('model_dispatch_authorized') is True
            and record.get('saved_any_track_checked') is True
            and record.get('saved_any_track_full_text_available') is False
            and bool(record.get('any_track_gate_record_sha256')))
        if availability == 'pmcid_missing':
            allowed = allowed and not record.get('pmcids') and not record.get('fresh_epmc_pmcid') and not record.get('fresh_pubmed_pmcids')
        else:
            proofs = record.get('proofs') or []
            metadata = record.get('id_converter_record') or {}
            not_live = metadata.get('live') is False or str(metadata.get('live', '')).lower() == 'false'
            if metadata.get('versions') and any(str(version.get('live', '')).lower() != 'false' for version in metadata['versions']):
                not_live = False
            formats = {proof.get('format'): proof for proof in proofs if isinstance(proof, dict)} if isinstance(proofs, list) else {}
            all_formats = all(formats.get(kind, {}).get('availability') == 'full_text_unavailable'
                              and formats.get(kind, {}).get('identity_verified') is True for kind in ('xml', 'html', 'pdf'))
            allowed = allowed and isinstance(proofs, list) and bool(proofs) and (not_live or all_formats)
        allowed = allowed and not any(isinstance(proof, dict) and proof.get('body_verified') is True for proof in record.get('proofs') or [])
        if not allowed:
            raise ValueError('PMC is available, unknown, or not checked across saved tracks')
    except (OSError, TypeError, ValueError) as exc:
        raise PreprintViewError('The article does not have verified unavailable-PMC eligibility for preprint recovery.') from exc
    return record


def _matches(context, selector, aliases):
    target = context.get('selector') or {}
    required = {'source_repo', 'disease', 'exposure', 'outcome', 'include_subexposures'}
    if not required.issubset(target) or not required.issubset(selector):
        return False
    cache_path = selector.get('_cache_path')
    if cache_path is not None and cache_path not in (context.get('source_cache_paths') or context.get('baseline_files') or {}):
        return False
    # Every explicit native context field must be supplied. No fuzzy identities
    # or implicit cross-source, cross-outcome, or cross-hierarchy expansion.
    for field, expected in target.items():
        actual = selector.get(field)
        if field == 'include_subexposures':
            actual = 'yes' if request_bool(actual) else 'no'
            expected = 'yes' if request_bool(expected) else 'no'
        mapping = aliases.get(field) or {}
        if mapping.get(str(actual), actual) != mapping.get(str(expected), expected):
            return False
    return True


def preprint_status(repo_root):
    manifest = _read_manifest(repo_root)
    if manifest is None:
        return {'available': False, 'status': 'unavailable', 'contexts': 0, 'records': 0,
                'manifest_sha256': 'unavailable'}
    accepted = [item for item in manifest['contexts'] if _accepted(item)]
    for context in accepted:
        _validate_article_gate(context, repo_root)
        bindings = context.get('baseline_files') or manifest.get('baseline_files') or {}
        if not bindings:
            raise PreprintViewError('A reviewed recovery has no saved-source binding.')
        for relative, expected in bindings.items():
            _bound_file(repo_root, relative, expected)
        if context.get('native_rows_file'):
            _bound_file(repo_root, context['native_rows_file'], context.get('native_rows_sha256'))
    return {'available': True, 'status': 'complete', 'contexts': len(accepted),
            'records': sum(len(item.get('native_rows') or []) or int(item.get('native_row_count') or 0) for item in accepted),
            'manifest_sha256': preprint_manifest_stamp(repo_root)}


def applicable_preprint_contexts(repo_root, selector=None, source_root=None):
    """Accepted exact known-scope matches, also usable for summary inventories."""
    manifest = _read_manifest(repo_root)
    if manifest is None:
        return []
    selector = selector or {}
    aliases = manifest.get('selector_aliases') or {}
    result = []
    for item in manifest['contexts']:
        if not _accepted(item):
            continue
        target = item.get('selector') or {}
        if all((aliases.get(field) or {}).get(str(target.get(field)), target.get(field)) ==
               (aliases.get(field) or {}).get(str(value), value)
               for field, value in selector.items() if value not in (None, '') and not field.startswith('_')):
            if source_root is not None:
                paths = item.get('source_cache_paths') or []
                if not any((Path(repo_root)/relative).resolve().is_relative_to(Path(source_root).resolve()) for relative in paths):
                    continue
            result.append(item)
    return result


def verify_preprint_summary_asset(path, repo_root):
    """Verify a separately sealed opt-in summary; never fall back to defaults."""
    path = Path(path).resolve()
    if 'preprints_included' not in path.parts:
        return False
    namespace = Path(*path.parts[:path.parts.index('preprints_included')+1])
    seal_path = namespace / 'preprint_view_seal.json'
    try:
        seal = json.loads(seal_path.read_text())
        relative = str(path.relative_to(namespace))
        if seal.get('schema_version') != 1 or seal.get('preprint_manifest_sha256') != preprint_manifest_stamp(repo_root):
            return False
        expected = (seal.get('assets') or {}).get(relative)
        if not expected or not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected:
            return False
        for source, digest in (seal.get('producing_code') or {}).items():
            _bound_file(repo_root, source, digest)
        for item in applicable_preprint_contexts(repo_root):
            _validate_article_gate(item, repo_root)
            for source, digest in (item.get('baseline_files') or {}).items():
                _bound_file(repo_root, source, digest)
        return True
    except (OSError, ValueError, PreprintViewError):
        return False


def write_preprint_summary_seal(namespace, repo_root, producing_code=None):
    """Seal only separately generated opt-in assets, after the native builder."""
    namespace, root = Path(namespace).resolve(), Path(repo_root).resolve()
    if namespace.name != 'preprints_included' or not namespace.is_relative_to(root):
        raise PreprintViewError('Preprint summaries require a repository-local opt-in namespace.')
    if not preprint_status(root)['available']:
        raise PreprintViewError('A complete recovery manifest is required to seal summaries.')
    codes = dict(producing_code or {})
    if (root/'preprint_ui.py').is_file():
        codes['preprint_ui.py'] = sha256((root/'preprint_ui.py').read_bytes()).hexdigest()
    assets = {str(path.relative_to(namespace)): sha256(path.read_bytes()).hexdigest()
              for path in sorted(namespace.rglob('*')) if path.is_file() and path.name != 'preprint_view_seal.json'}
    seal = dict(schema_version=1, preprint_manifest_sha256=preprint_manifest_stamp(root),
                assets=assets, producing_code=codes)
    (namespace/'preprint_view_seal.json').write_text(json.dumps(seal, indent=2)+'\n')
    return seal


def compose_preprint_view(rows, repo_root, enabled=False, selector=None,
                          selected_record_ids=None, allow_additions=False):
    audit = {'enabled': bool(enabled), 'available': False, 'applied_contexts': [],
             'replaced_record_ids': [], 'recovered_record_ids': [],
             'input_rows': len(rows), 'output_rows': len(rows), 'introduced_metadata_fields': []}
    if not enabled:
        return rows, audit
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise PreprintViewError('A preprint source view requires a list of saved evidence records.')
    incoming_recovered_ids = {record_identity(row) for row in rows if _recovered_row(row)}
    manifest = _read_manifest(repo_root)
    if manifest is None:
        if incoming_recovered_ids:
            raise PreprintViewError('The selected preprint evidence is stale; reload the current saved source view.')
        audit['status'] = 'unavailable'
        return rows, audit
    audit.update(available=True, status='complete', manifest_sha256=preprint_manifest_stamp(repo_root))
    selector = dict(selector or {})
    matches = [item for item in manifest['contexts'] if _accepted(item) and
               _matches(item, selector, manifest.get('selector_aliases') or {})]
    input_ids = {record_identity(row) for row in rows}
    selected = set(selected_record_ids) if selected_record_ids is not None else input_ids
    replacements, recovered, seen_contexts, claimed_originals, seen_new = set(), [], set(), set(), set()
    added_articles = set()
    for context in matches:
        _validate_article_gate(context, repo_root)
        context_id = context.get('context_id')
        originals = set(context.get('original_record_ids') or [])
        operation = context.get('operation')
        if not isinstance(context_id, str) or not context_id or context_id in seen_contexts:
            raise PreprintViewError('Recovery context IDs must be unique.')
        seen_contexts.add(context_id)
        if operation not in {'replace', 'add'} or (operation == 'replace' and not originals) or (operation == 'add' and originals):
            raise PreprintViewError(f'Invalid recovery operation: {context_id}')
        if claimed_originals.intersection(originals):
            raise PreprintViewError('Multiple preprint versions claim the same original evidence.')
        claimed_originals.update(originals)
        bindings = context.get('baseline_files') or manifest.get('baseline_files') or {}
        if not bindings:
            raise PreprintViewError(f'Recovery has no saved-source binding: {context_id}')
        for relative, expected in bindings.items():
            _bound_file(repo_root, relative, expected)
        native_rows = context.get('native_rows')
        if native_rows is None and context.get('native_rows_file'):
            native_rows = json.loads(_bound_file(repo_root, context['native_rows_file'], context.get('native_rows_sha256')).read_text())
        if not isinstance(native_rows, list) or not all(isinstance(row, dict) for row in native_rows):
            raise PreprintViewError(f'Recovery native rows are invalid: {context_id}')
        anchor, source = context.get('publication_anchor') or {}, context.get('preprint_source') or {}
        if not anchor.get('pmid') or not anchor.get('journal_publication_date') or not source.get('id') or not source.get('source_sha256'):
            raise PreprintViewError(f'Recovery bibliographic provenance is incomplete: {context_id}')
        if operation == 'add':
            if str(anchor['pmid']) in added_articles or any(
                    str(row.get('PMID', row.get('pmid', ''))) == str(anchor['pmid'])
                    and row.get('preprint_context_id') != context_id for row in rows):
                raise PreprintViewError('An add cannot duplicate existing article evidence; use an explicit replacement.')
            added_articles.add(str(anchor['pmid']))
        chosen = []
        for row in native_rows:
            new_id = record_identity(row)
            if not new_id.startswith('id:') or new_id in originals or new_id in seen_new:
                raise PreprintViewError('Recovered effects require unique new native record IDs.')
            seen_new.add(new_id)
            mapped = set(row.get('preprint_original_record_ids') or [])
            if not mapped.issubset(originals):
                raise PreprintViewError('Recovered effect maps outside its original evidence.')
            if operation == 'replace':
                # Entire-source requests and repeated recovered submissions are
                # explicit; partial selections require a per-effect ID mapping.
                permitted = (bool(originals & input_ids) and allow_additions) or new_id in selected
                permitted = permitted or bool(mapped and mapped.issubset(selected)) or originals.issubset(selected)
            else:
                permitted = allow_additions or new_id in selected
            if not permitted:
                continue
            pmid = str(row.get('PMID', row.get('pmid', '')))
            if pmid != str(anchor['pmid']):
                raise PreprintViewError('Recovered evidence changed the journal PMID anchor.')
            result = deepcopy(row)
            submitted = next((item for item in rows if record_identity(item) == new_id), {})
            if 'Cache Row ID' in submitted:
                result['Cache Row ID'] = submitted['Cache Row ID']
            result.update(preprint_derived=True, preprint_peer_reviewed=False,
                          preprint_context_id=context_id, preprint_source=deepcopy(source),
                          preprint_publication_anchor=deepcopy(anchor),
                          preprint_original_record_ids=sorted(mapped or originals),
                          preprint_manifest_sha256=audit['manifest_sha256'])
            result['preprint_source_label'] = 'Preprint-derived evidence (not peer reviewed)'
            if source.get('url'):
                original_link = next((item.get('Link') for item in rows if record_identity(item) in originals and item.get('Link')), None)
                result['preprint_journal_article_url'] = anchor.get('url') or original_link or result.get('Link')
                result['Link'] = source['url']
            # PPR posting dates never replace the journal date used for cohorts.
            result['publication_date'] = anchor['journal_publication_date']
            chosen.append(result)
        if chosen:
            audit['introduced_metadata_fields'] = sorted(set(audit['introduced_metadata_fields']) |
                _introduced_metadata(context, repo_root, rows, native_rows))
            audit['applied_contexts'].append(context_id)
            recovered.extend(chosen)
            replacements.update(originals)
            # Discard submitted stale or edited recovered values and substitute
            # the authoritative manifest version, preserving repeat-idempotence.
            replacements.update(record_identity(row) for row in native_rows)
    if not incoming_recovered_ids.issubset({record_identity(row) for row in recovered}):
        raise PreprintViewError('The selected preprint evidence is stale or belongs to another source context; reload this analysis.')
    if not recovered:
        return rows, audit
    result = [deepcopy(row) for row in rows if record_identity(row) not in replacements] + recovered
    for row in result:
        for field in audit['introduced_metadata_fields']:
            row.setdefault(field, None)
    audit.update(replaced_record_ids=sorted(input_ids.intersection(replacements)),
                 recovered_record_ids=[record_identity(row) for row in recovered], output_rows=len(result))
    return result, audit


def install_status_endpoint(app, repo_root):
    """Expose only local release counts; no data acquisition can be triggered."""
    endpoint = 'preprint_recovery_status'
    if endpoint in app.view_functions:
        return
    from flask import jsonify
    def status():
        try:
            return jsonify(preprint_status(repo_root))
        except PreprintViewError as exc:
            return jsonify(available=False, status='invalid', contexts=0, records=0, error=str(exc)), 503
    app.add_url_rule('/api/preprint-recovery-status', endpoint, status)


def request_selector(namespace, data, source_repo=None):
    """Resolve exact canonical context names through the native application's APIs."""
    disease = data.get('disease', namespace.get('DEFAULT_DISEASE', ''))
    scope = None
    if namespace.get('get_scope_config') and namespace.get('get_major_scope_key'):
        scopes = namespace['get_scope_config']()
        scope = scopes.get(namespace['get_major_scope_key'](disease, scopes))
        if scope:
            disease = scope['disease']
    elif namespace.get('resolve_disease_scope'):
        disease = namespace['resolve_disease_scope'](disease, data.get('subcategory'))
    exposure = data.get('exposure', '')
    analysis = namespace.get('meta_analysis')
    if analysis and hasattr(analysis, 'get_canonical_name'):
        exposure = analysis.get_canonical_name(exposure)
    outcome = data.get('endpoint') or data.get('outcome') or 'Incidence'
    if scope and namespace.get('requested_endpoint'):
        outcome = namespace['requested_endpoint'](data, scope)
    elif str(outcome).casefold() == 'incidence':
        outcome = 'Incidence'
    mode = data.get('include_subexposures', 'yes')
    for name in ('subexposure_mode', 'requested_subexposures'):
        if namespace.get(name):
            mode = namespace[name]()
            break
    variant = data.get('evidence_track') or 'combined'
    if namespace.get('request_evidence_source'):
        variant = namespace['request_evidence_source']()
    selector = dict(source_repo=source_repo or namespace.get('PREPRINT_SOURCE_REPO') or
                    Path(namespace.get('__file__', '')).resolve().parent.name,
                    disease=disease, exposure=exposure, outcome=outcome,
                    include_subexposures='yes' if request_bool(mode) else 'no', source_variant=variant)
    if data.get('subcategory'):
        selector['subcategory'] = data['subcategory']
    return selector


def install_common_runtime(app, namespace, repo_root, active_context):
    """Overlay trusted cache reads before native binding/eligibility policies.

    ``active_context`` is the existing request-local settings ContextVar getter.
    Source snapshots and persisted native caches are never rewritten.
    """
    from flask import has_request_context, request, jsonify
    from functools import wraps
    import os
    root = Path(os.environ.get('META_PREPRINT_REPO_ROOT') or repo_root).resolve()
    source_repo = os.environ.get('META_PREPRINT_SOURCE_REPO') or Path(repo_root).name
    install_status_endpoint(app, root)

    analysis = namespace.get('meta_analysis')
    if analysis is not None and not getattr(analysis, '_preprint_metadata_guard_installed', False):
        original_perform = analysis.perform_meta_analysis
        @wraps(original_perform)
        def perform_source_frame(frame, *args, **kwargs):
            context = active_context()
            if context and context['settings'].get('include_preprints'):
                fields = context.get('preprint_metadata_fields', [])
                frame = normalize_preprint_frame(frame, fields)
                if kwargs.get('df_all') is not None:
                    kwargs['df_all'] = normalize_preprint_frame(kwargs['df_all'], fields)
            return original_perform(frame, *args, **kwargs)
        analysis.perform_meta_analysis = perform_source_frame
        analysis._preprint_metadata_guard_installed = True

    def summary_enabled():
        return has_request_context() and request_bool(request.args.get('include_preprints', False))

    def summary_variant_required(plot_root=None):
        if not summary_enabled():
            return False
        # The native root is a whole disease/source view. A valid empty release
        # explicitly reuses its unchanged base assets, guarded by this manifest.
        if not preprint_status(root)['available']:
            raise PreprintViewError('No complete preprint recovery summary release is available.')
        selection = request_selector(namespace, dict(request.args), source_repo)
        source_root = None
        if plot_root is not None:
            path = Path(plot_root)
            if 'preprints_included' in path.parts:
                path = Path(*path.parts[:path.parts.index('preprints_included')])
            if path.name == 'cohort_latest':
                path = path.parent
            source_root = path.parent
        return bool(applicable_preprint_contexts(root, {key: selection[key] for key in
                    ('source_repo', 'include_subexposures', 'source_variant')}, source_root=source_root))

    for name in ('active_plot_dir', 'current_summary_root', 'current_ui_plot_root', 'summary_plot_root'):
        original_root = namespace.get(name)
        if original_root:
            def make_root(original):
                @wraps(original)
                def selected_root(*args, **kwargs):
                    value = original(*args, **kwargs)
                    return str(preprint_variant_root(value, summary_variant_required(value)))
                return selected_root
            namespace[name] = make_root(original_root)
    if namespace.get('current_summary_manifest_file'):
        original_manifest = namespace['current_summary_manifest_file']
        @wraps(original_manifest)
        def selected_manifest(*args, **kwargs):
            path = Path(original_manifest(*args, **kwargs))
            return str(preprint_variant_root(path.parent, summary_variant_required(path.parent)) / path.name)
        namespace['current_summary_manifest_file'] = selected_manifest

    @app.url_defaults
    def preprint_summary_urls(endpoint, values):
        if has_request_context() and 'summary' in endpoint:
            values.setdefault('include_preprints', 'yes' if summary_enabled() else 'no')

    @app.context_processor
    def preprint_template_context():
        return {'include_preprints': summary_enabled()}

    @app.errorhandler(PreprintViewError)
    def invalid_preprint_view(error):
        return jsonify(error=str(error)), 409

    @app.before_request
    def preprint_source_selection():
        if request.method != 'POST' or not request.path.endswith(('/analyze', '/reanalyze')):
            return
        context = active_context()
        if context is None:
            return
        data = request.get_json(silent=True) or {}
        if not context['settings'].get('include_preprints'):
            if any(_recovered_row(row) for row in data.get('studies') or []):
                raise PreprintViewError('Reload the original saved evidence after turning preprints off.')
            return
        context['preprint_selector'] = request_selector(namespace, data, source_repo)
        # Recovered values are canonicalized again before native request gates;
        # their selection IDs remain bound by each app's existing source binder.
        if request.path.endswith('/reanalyze') and isinstance(data.get('studies'), list):
            selected, audit = compose_preprint_view(data['studies'], root, True,
                context['preprint_selector'], allow_additions=False)
            data['studies'][:] = selected
            context['preprint_selection_audit'] = audit
            context['preprint_metadata_fields'] = audit.get('introduced_metadata_fields', [])

    original_load = namespace.get('load_json')
    if original_load:
        @wraps(original_load)
        def load_json(path, default):
            if has_request_context() and summary_enabled() and 'preprints_included' in Path(path).parts:
                if not verify_preprint_summary_asset(path, root):
                    return deepcopy(default)
            payload = original_load(path, default)
            context = active_context() if has_request_context() else None
            if context is None or not context['settings'].get('include_preprints') or not isinstance(payload, dict) or not isinstance(payload.get('studies'), list):
                return payload
            try:
                alias = namespace.get('preprint_cache_path_alias')
                relative = str(alias(path)) if alias else str(Path(path).resolve().relative_to(root))
            except ValueError:
                return payload
            selector = {**context.get('preprint_selector', {}), '_cache_path': relative}
            rows, audit = compose_preprint_view(payload['studies'], root, True, selector, allow_additions=True)
            if rows is payload['studies']:
                return payload
            result = {**payload, 'studies': rows}
            context['preprint_selection_audit'] = audit
            context['preprint_metadata_fields'] = sorted(set(context.get('preprint_metadata_fields', [])) |
                set(audit.get('introduced_metadata_fields', [])))
            # Native cache row IDs often incorporate row positions and effect
            # IDs. Refresh those IDs from this same trusted source view.
            trusted = result
            runtime = namespace.get('provisional_ui_runtime')
            if runtime and hasattr(runtime, 'identify_rows'):
                trusted = runtime.identify_rows(result, repo_root, selector['include_subexposures'])
            elif namespace.get('cache_row_id'):
                trusted = deepcopy(result)
                for index, row in enumerate(trusted['studies']):
                    row['Cache Row ID'] = namespace['cache_row_id'](selector['disease'], payload.get('exposure') or selector['exposure'], index)
            if request.path.endswith('/reanalyze'):
                data = request.get_json(silent=True) or {}
                selected, _ = compose_preprint_view(data.get('studies') or [], root, True,
                    selector, allow_additions=False)
                ids = {record_identity(row): row.get('Cache Row ID') for row in trusted['studies']}
                for row in selected:
                    if row.get('preprint_derived') and ids.get(record_identity(row)):
                        row['Cache Row ID'] = ids[record_identity(row)]
                if isinstance(data.get('studies'), list):
                    data['studies'][:] = selected
            return result
        namespace['load_json'] = load_json

    @app.after_request
    def preprint_response_provenance(response):
        context = active_context()
        if context and context['settings'].get('include_preprints') and response.is_json:
            payload = response.get_json()
            # A reviewed add can recover a saved context with no native row or
            # cache response. Native eligibility still runs in the existing
            # response projector, which executes after this hook.
            if request.path.endswith('/analyze') and response.status_code in {200, 404} and isinstance(payload, dict) and not payload.get('studies'):
                rows, audit = compose_preprint_view([], root, True, context.get('preprint_selector'), allow_additions=True)
                if rows:
                    payload = {'studies': rows, 'disease': context['preprint_selector']['disease'],
                               'exposure': context['preprint_selector']['exposure'],
                               'outcome': context['preprint_selector']['outcome']}
                    context['preprint_selection_audit'] = audit
                    response.status_code = 200
            if isinstance(payload, dict):
                payload['preprint_selection'] = context.get('preprint_selection_audit') or {
                    'enabled': True, 'available': preprint_status(root)['available'],
                    'applied_contexts': [], 'manifest_sha256': preprint_manifest_stamp(root)}
                response.set_data(json.dumps(payload, ensure_ascii=False, default=str))
        return response
