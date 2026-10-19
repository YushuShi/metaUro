"""Run and publish metaUro's two self-contained urinary evidence pipelines.

Legacy scope names identify immutable datasets, never external repositories.
All discovery, screening, calculations and release inputs live in this checkout.
"""
from __future__ import annotations

import argparse
import copy
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from automation.sync_sources import (SOURCES, allowed, assembled_source, bound,
    legacy_summary_artifact, load_overlay, read, sha, write, _check_rows)

ROOT = Path(__file__).resolve().parents[1]
VERSION = "metauro-autonomous-monthly-v1"
MODELS = ("gpt-5.6-luna", "gpt-5.6-sol", "claude-opus-5", "gemini-3.7-flash", "gpt-6-astra")
CONFIG = "automation/autonomous_monthly_config.json"


def environment(environ=None):
    env = dict(os.environ if environ is None else environ)
    for name in ("NCBI_EMAIL", "NCBI_API_KEY", "CORNELL_API_KEY", "CORNELL_API_BASE_URL", "API_BASE_URI"):
        env[name] = str(env.get(name) or "").strip()
    env["CORNELL_API_BASE_URL"] = env["CORNELL_API_BASE_URL"] or env["API_BASE_URI"]
    env.update(MONTHLY_REQUIRE_VALID_NCBI_KEY="false", MONTHLY_AUTOMATION_REQUIRE_GATE="true",
               MPLBACKEND="Agg", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    return env


def runtime_manifest(root):
    path = root / "runtime_code/monthly/manifest.json"
    manifest = read(path)
    if not manifest or manifest.get("version") != VERSION:
        raise ValueError("Standalone monthly runtime manifest is missing")
    for relative, checksum in manifest["files"].items():
        if sha(bound(root / "runtime_code/monthly", relative)) != checksum:
            raise ValueError("Standalone runtime checksum mismatch: " + relative)
    return manifest


def immutable_hashes(root):
    manifest = read(root / "evidence-manifest.json")
    result = {entry["path"]: sha(bound(root, entry["path"])) for entry in manifest["files"]}
    result["evidence-manifest.json"] = sha(root / "evidence-manifest.json")
    return result


def window(root, today=None):
    config = read(root / CONFIG)
    active = read(root / "updates/monthly/active.json", {})
    if active and active.get("status") != "completed":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}", str(active.get("run_id", ""))):
            raise ValueError("Invalid active monthly run ID")
        date.fromisoformat(active["end_date"])
        if set(active["start_dates"]) != set(SOURCES):
            raise ValueError("Both urinary scopes are required")
        for value in active["start_dates"].values():
            date.fromisoformat(value)
        return active
    state = read(root / "updates/monthly/state.json", {})
    end = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    starts = {scope: str(date.fromisoformat(state.get("search_through", {}).get(scope)
                     or config["scopes"][scope]["bootstrap_through"]) + timedelta(days=1)) for scope in SOURCES}
    return {"run_id": min(starts.values()) + "_" + str(end), "start_dates": starts,
            "end_date": str(end), "status": "prepared",
            "no_work": all(start > str(end) for start in starts.values())}


def preflight(root=ROOT, *, gateway=False, environ=None, today=None):
    root = Path(root).resolve()
    manifest = runtime_manifest(root)
    env = environment(environ)
    missing = [key for key in ("NCBI_EMAIL", "CORNELL_API_KEY", "CORNELL_API_BASE_URL") if not env.get(key)]
    result = {"repository": "metaUro", "version": VERSION, "ready": not missing,
              "missing_secret_names": missing, "optional_ncbi_api_key": True,
              "runtime_sha256": sha(root / "runtime_code/monthly/manifest.json"),
              "runtime_files": len(manifest["files"]), "external_repository_dependencies": [],
              "models": list(MODELS), "window": window(root, today), "model_requests": 0}
    if gateway and not missing:
        from openai import OpenAI
        client = OpenAI(api_key=env["CORNELL_API_KEY"], base_url=env["CORNELL_API_BASE_URL"], timeout=45, max_retries=0)
        available = {item.id for item in client.models.list()}
        result["missing_models"] = sorted(set(MODELS) - available)
        result["ready"] = not result["missing_models"]
        result["gateway_model_listing_checked"] = True
    return result


def _copy_ignore(directory, names):
    return {name for name in names if name in {".git", "__pycache__", "mykey.env", ".complete", "Plot", "node_modules"}
            or name.endswith((".pyc", ".log"))}


def materialize(root, scope, work):
    """Create a writable independent copy; never hardlink writeable evidence."""
    if scope not in SOURCES:
        raise ValueError("Unknown urinary scope")
    runtime_manifest(root)
    if work.exists():
        raise ValueError("Workspace already exists; resume through its pinned receipt")
    current = assembled_source(root, scope)
    shutil.copytree(current, work, ignore=_copy_ignore)
    runtime = root / "runtime_code/monthly" / scope
    for path in runtime.rglob("*"):
        if path.is_file():
            target = work / path.relative_to(runtime)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    # Calculations use metaUro's already published request/quality/baseline
    # controls, including the trend-CI protections, rather than older defaults.
    settings = root / 'runtime_code' / scope / 'analysis_settings_backend.py'
    if settings.exists():
        shutil.copyfile(settings, work / settings.name)
    # Shared worker is copied into the isolated namespace, where its native
    # imports resolve exclusively against this pinned runtime and dataset.
    for name in ("autonomous_scope.py", "autonomous_projection.py", "prostate_scope_results.py"):
        shutil.copyfile(root / "automation" / name, work / "automation" / name)
    # The approved ASB rows already exist in the urinary evidence. Their
    # translated summary label must travel with the next published overlay,
    # rather than editing the immutable source catalog or today's summaries.
    relative = 'static/i18n-translations.json'
    catalog = read(work / relative, {})
    if scope == 'MetaVir':
        from prostate_scopes import LABEL, TRANSLATIONS, extend_catalog
        catalog[LABEL] = TRANSLATIONS
        write(work / relative, catalog)
        baseline_path = work / 'static/seer-baseline.json'
        if baseline_path.exists():
            write(baseline_path, extend_catalog(read(baseline_path)))
    labels = read(root / 'runtime_code/monthly/metaPee' / relative, {}).get(
        'Non-nutritive sweetener intake', {})
    if labels:
        entry = catalog.setdefault('Non-nutritive sweetener intake', {})
        for locale, label in labels.items():
            entry.setdefault(locale, label)
        write(work / relative, catalog)
    outputs = {}
    if (work / relative).exists() and (not (current / relative).exists()
            or sha(work / relative) != sha(current / relative)):
        outputs[relative] = sha(work / relative)
    from automation.autonomous_projection import cache_profiles
    roots = [work / profile['cache'] for profile in cache_profiles(work, scope)]
    roots += [work/'static/subexposures', work/'combined/provisional_ui/static']
    baseline = {str(path.relative_to(work)): sha(path) for folder in roots
                for path in folder.rglob('*') if path.is_file()}
    write(work / 'autonomous_materialization.json', {
        'output_sha256': outputs, 'ui_baseline_sha256': baseline,
        'source_root': str(current.resolve())})
    return work


def _copy_tree_exact(source, target):
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=_copy_ignore)


def checkpoint(root=ROOT, span=None):
    """Preserve in-flight paid packets after failure or workflow cancellation."""
    root = Path(root).resolve()
    span = span or read(root/'updates/monthly/active.json', {})
    if not span:
        return {'status':'no_active_run'}
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}',str(span.get('run_id',''))):
        raise ValueError('Invalid checkpoint run ID')
    date.fromisoformat(span['end_date'])
    if set(span['start_dates']) != set(SOURCES):
        raise ValueError('Invalid checkpoint source scopes')
    audit = root/'updates/monthly'/span['run_id']
    copied = 0
    for scope in SOURCES:
        date.fromisoformat(span['start_dates'][scope])
        work = root/'runtime/monthly'/span['run_id']/scope
        native_id = span['start_dates'][scope]+'_'+span['end_date']
        for kind in ('monthly_updates','monthly_reports'):
            source = work/'data'/kind/native_id
            if source.exists():
                _copy_tree_exact(source,audit/'recovery'/scope/kind/native_id)
                copied += 1
        candidates = [work/'monthlyHumanReview'/(native_id+'_export.csv'),
                      work/'monthlyHumanReview'/(native_id+'.csv')]
        candidate = next((path for path in candidates if path.is_file()),None)
        if candidate is not None:
            target = root/'monthlyHumanReview'/(span['run_id']+'_'+scope+'.csv')
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(candidate,target)
    return {'status':'checkpoint_saved','run_id':span['run_id'],'copied_packet_trees':copied}


def prepare(root=ROOT, *, today=None):
    root = Path(root).resolve()
    check = preflight(root, gateway=True, today=today)
    if not check["ready"]:
        raise ValueError("Monthly preflight failed: " + json.dumps(check))
    span = check["window"]
    if span["no_work"]:
        return {"status": "current", **span}
    audit = root / "updates/monthly" / span["run_id"]
    receipt_path = audit / "release.json"
    old = read(receipt_path, {})
    if old.get("status") == "completed":
        completed = validate_completed_release(root)
        if completed and completed['run_id'] == span['run_id']:
            return old
        # A completed artifact from a failed push is a screening checkpoint,
        # not a locally installed release. Rebuild its publication from inputs.
    current_index = read(root / "updates/manifest.json", {"schema_version": 1, "sources": {}})
    pinned = read(audit / "input.json", {})
    inputs = {"version": VERSION, "runtime_sha256": check["runtime_sha256"],
              "overlay_index": current_index, "window": span}
    if pinned and pinned != inputs:
        raise ValueError("Interrupted run inputs changed; retain receipts and review before resuming")
    if not pinned:
        write(audit / "input.json", inputs)
        write(audit / "immutable_before.json", immutable_hashes(root))
    write(root / "updates/monthly/active.json", span)
    result = {"version": VERSION, "repository": "metaUro", "status": "running", **span,
              "scopes": {}, "input_sha256": sha(audit / "input.json")}
    result["status"] = "running"
    write(receipt_path, result)
    try:
        for scope in SOURCES:
            work = root / "runtime/monthly" / span["run_id"] / scope
            if not work.exists():
                materialize(root, scope, work)
                recovery = audit / "recovery" / scope
                for kind in ("monthly_updates", "monthly_reports"):
                    if (recovery / kind).exists():
                        shutil.copytree(recovery / kind, work / "data" / kind, dirs_exist_ok=True)
                native_id = span['start_dates'][scope] + '_' + span['end_date']
                for kind, label in (('monthly_updates', 'screening'), ('monthly_reports', 'reports')):
                    saved = audit / scope / label
                    if saved.exists():
                        shutil.copytree(saved, work/'data'/kind/native_id, dirs_exist_ok=True)
            env = environment()
            env["METAURO_REPO_ROOT"] = str(root)
            env["PYTHONPATH"] = str(root)
            command = [sys.executable, "-B", "-m", "automation.autonomous_scope", "--scope", scope,
                       "--start-date", span["start_dates"][scope], "--end-date", span["end_date"]]
            subprocess.run(command, cwd=work, env=env, check=True)
            native = work / "data/monthly_updates" / (span["start_dates"][scope] + "_" + span["end_date"])
            scope_receipt = read(native / "autonomous_scope.json")
            if scope_receipt.get("status") != "validated":
                raise ValueError("Independent scope has not validated")
            result["scopes"][scope] = scope_receipt
            _copy_tree_exact(native, audit / scope / "screening")
            gate = read(native/'monthly_application_gate.json')
            native_csv = bound(work, gate['review_csv'])
            if (sha(native/'monthly_application_gate.json') != scope_receipt['application_gate_sha256']
                    or sha(native_csv) != gate['review_csv_sha256']
                    or native_csv != Path(scope_receipt['native_human_review_csv']).resolve()):
                raise ValueError('Native review CSV does not match the validated application gate')
            preserved_csv = audit/scope/'review/native.csv'
            preserved_csv.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(native_csv, preserved_csv)
            scope_receipt['native_review_csv_artifact'] = {
                'path':str(preserved_csv.relative_to(root)), 'sha256':sha(preserved_csv)}
            reports = work / "data/monthly_reports" / native.name
            if reports.exists():
                _copy_tree_exact(reports, audit / scope / "reports")
            root_csv = root / "monthlyHumanReview" / (span["run_id"] + "_" + scope + ".csv")
            root_csv.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(scope_receipt["human_review_csv"]), root_csv)
            result["scopes"][scope]["human_review_csv"] = str(root_csv.relative_to(root))
            write(receipt_path, result)
        prepare_overlays(root, audit, result)
        protected = read(audit / "immutable_before.json")
        if any(sha(bound(root, name)) != digest for name, digest in protected.items()):
            raise ValueError("Immutable baseline changed")
        result.update(status="validated", immutable_files_verified=len(protected),
                      audit_artifact_sha256={str(p.relative_to(root)):sha(p) for scope in SOURCES
                         for p in (audit/scope).rglob('*') if p.is_file()})
        write(receipt_path, result)
        return result
    except BaseException as exc:
        result.update(status="failed", error_type=type(exc).__name__)
        write(receipt_path, result)
        checkpoint(root,span)
        raise


def prepare_overlays(root, audit, result):
    """Bind both additive scope deltas before changing the shared publication pointer."""
    prepared = {}
    for scope in SOURCES:
        work = root / "runtime/monthly" / result["run_id"] / scope
        receipt = result["scopes"][scope]
        old = load_overlay(root, scope) or {}
        current = assembled_source(root, scope)
        files = copy.deepcopy(old.get("files", {}))
        version = "autonomous-" + result["run_id"]
        for relative, digest in receipt["output_sha256"].items():
            if not allowed(relative) or legacy_summary_artifact(relative) or digest is None:
                raise ValueError("Unapproved monthly output: " + relative)
            incoming = bound(work, relative)
            if sha(incoming) != digest:
                raise ValueError("Changed scope artifact after validation")
            previous = bound(current, relative)
            if relative.endswith(".json"):
                from runtime_code.trend_ci_projection import correct_tree
                packet = read(incoming)
                if correct_tree(copy.deepcopy(packet)):
                    raise ValueError("Monthly artifact contains trend-derived uncertainty")
                if previous.is_file():
                    prior = read(previous)
                    _check_rows(prior, packet)
                    if isinstance(prior, dict) and prior.get('studies'):
                        encoded = lambda row: json.dumps(row,sort_keys=True,ensure_ascii=False)
                        old_rows = Counter(encoded(row) for row in prior['studies'])
                        new_rows = Counter(encoded(row) for row in packet['studies'])
                        if old_rows - new_rows:
                            raise ValueError('Monthly output changes historical study fields or selection gates')
            stored = "versions/" + version + "/files/" + relative
            destination = bound(root / "updates" / scope, stored)
            if destination.exists() and sha(destination) != digest:
                raise ValueError("Versioned evidence artifact cannot be overwritten")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(incoming, destination)
            baseline = bound(root / "sources" / scope, relative)
            files[relative] = {"stored_path": stored, "sha256": digest,
                "original_sha256": sha(baseline) if baseline.exists() else None,
                "run_id": result["run_id"], "producer": VERSION}
        reviews = copy.deepcopy(old.get("review_files", []))
        csv_path = bound(root, receipt["human_review_csv"])
        review_store = "versions/" + version + "/review/" + csv_path.name
        target = bound(root / "updates" / scope, review_store)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(csv_path, target)
        reviews.append({"filename": csv_path.name, "stored_path": review_store,
                        "sha256": sha(target), "run_id": result["run_id"]})
        manifest = {"schema_version": 1, "kind": "autonomous_monthly_evidence", "source": scope,
                    "producer": "metaUro", "run_id": result["run_id"], "files": files,
                    "review_files": reviews, "search_through": result["end_date"],
                    "human_review_pending": receipt["human_review_pending"],
                    "new_search_performed": True, "new_screening_performed": True,
                    "runtime_sha256": read(audit / "input.json")["runtime_sha256"],
                    "screening_receipt": str((audit / scope / "screening/autonomous_scope.json").relative_to(root)),
                    "screening_receipt_sha256": sha(audit / scope / "screening/autonomous_scope.json")}
        runs = copy.deepcopy(old.get('screening_runs', []))
        lineage = {'run_id': result['run_id'], 'files': {}}
        for name in ('first_screen.json', 'final_screening.json', 'monthly_application_filter.json'):
            packet = audit/scope/'screening'/name
            if packet.exists():
                lineage['files'][name] = {'path': str(packet.relative_to(root)), 'sha256': sha(packet)}
        if lineage['files']:
            runs.append(lineage)
        manifest['screening_runs'] = runs
        manifest_path = root / "updates" / scope / "versions" / version / "manifest.json"
        write(manifest_path, manifest)
        prepared[scope] = {"manifest": str(manifest_path.relative_to(root / "updates")), "sha256": sha(manifest_path)}
    result["prepared_overlay_index"] = {"schema_version": 1, "sources": prepared}


def summary_lock_scratch(relative):
    """Recognize only the summary builder's non-artifact lock directory."""
    parts = Path(relative).parts
    return len(parts) == 3 and parts[:2] == ('summary', '.locks') and parts[2].endswith('.lock')


def summary_inventory(root):
    return {str(path.relative_to(root)):sha(path) for path in (root/'summary').rglob('*')
            if path.is_file() and not summary_lock_scratch(path.relative_to(root))}


def publish(root=ROOT, *, validate=True):
    """Validate summaries before marking the local release publishable by GitHub."""
    root = Path(root).resolve()
    active = read(root / "updates/monthly/active.json")
    audit = root / "updates/monthly" / active["run_id"]
    result = read(audit / "release.json")
    if result.get("status") == "completed":
        completed = validate_completed_release(root)
        if completed and completed['run_id'] == active['run_id']:
            return result
        raise ValueError('Completed checkpoint is not an installed release; run prepare again')
    if result.get("status") != "validated":
        raise ValueError("Both independent scopes must validate before publication")
    previous = read(root / "updates/manifest.json")
    if previous != read(audit / "input.json")["overlay_index"]:
        raise ValueError("Published evidence changed since monthly preparation")
    summary_backup = root / "runtime/monthly" / active["run_id"] / "summary-before"
    _copy_tree_exact(root / "summary", summary_backup)
    metadata = {path: path.read_bytes() if path.exists() else None for path in (
        audit/'release.json', root/'updates/monthly/state.json', root/'updates/monthly/active.json')}
    try:
        write(root / "updates/manifest.json", result["prepared_overlay_index"])
        from summary_outputs import refresh_all
        refresh_all(root)
        if validate:
            # Historical live-data count assertions belong to the code-release
            # suite. A new month's legitimate additions change those counts.
            subprocess.run([sys.executable, "-m", "pytest", "tests/test_autonomous_monthly.py",
                "tests/test_source_updates.py", "tests/test_trend_ci_guard.py",
                "tests/test_release_artifact_policy.py", "-q"], cwd=root, check=True)
        protected = read(audit / "immutable_before.json")
        if any(sha(bound(root, name)) != digest for name, digest in protected.items()):
            raise ValueError("Immutable evidence changed before publication")
        result.update(status="completed", completed_at=datetime.now(timezone.utc).isoformat(),
                      summary_sha256=summary_inventory(root))
        write(audit / "release.json", result)
        write(root / "updates/monthly/state.json", {"version": VERSION, "repository": "metaUro",
              "status": "completed", "run_id": active["run_id"],
              "search_through": {scope: result["end_date"] for scope in SOURCES},
              "manifest_path": str((audit / "release.json").relative_to(root)),
              "manifest_sha256": sha(audit / "release.json"), "completed_at": result["completed_at"]})
        write(root / "updates/monthly/active.json", {**active, "status": "completed"})
        return result
    except BaseException:
        write(root / "updates/manifest.json", previous)
        _copy_tree_exact(summary_backup, root / "summary")
        for path, content in metadata.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise


def validate_completed_release(repo_root=ROOT, require_through=None):
    """Read-only release verification for consumers such as metaOnco.

Returns a common cutoff, run ID and manifest hash. Does not materialize views,
import an analysis engine, make requests, or perform pooling/review work.
"""
    root = Path(repo_root).resolve()
    state = read(root / "updates/monthly/state.json", {})
    if not state:
        return None
    if state.get("repository") != "metaUro" or state.get("status") != "completed":
        raise ValueError("No completed autonomous metaUro monthly release")
    cutoffs = state.get("search_through", {})
    if set(cutoffs) != set(SOURCES) or len(set(cutoffs.values())) != 1:
        raise ValueError("Urinary scope search cutoffs disagree")
    cutoff = next(iter(cutoffs.values()))
    date.fromisoformat(cutoff)
    if require_through is not None and date.fromisoformat(cutoff) < date.fromisoformat(str(require_through)):
        raise ValueError("metaUro has not completed the required publication cutoff")
    path = bound(root, state["manifest_path"])
    if sha(path) != state.get("manifest_sha256"):
        raise ValueError("metaUro release receipt checksum mismatch")
    release = read(path)
    if (release.get("status") != "completed" or release.get("run_id") != state.get("run_id")
            or release.get("end_date") != cutoff or set(release.get("scopes", {})) != set(SOURCES)):
        raise ValueError("Incomplete or mismatched autonomous release receipt")
    revision = None
    if read(root / "updates/manifest.json") != release["prepared_overlay_index"]:
        current = load_overlay(root, 'MetaVir') or {}
        revision_id = current.get('run_id', '')
        revision_path = bound(root, 'evidence-revisions/' + revision_id + '/release.json')
        if current.get('kind') != 'evidence_scope_revision' or not revision_path.is_file():
            raise ValueError("Active overlay differs from the completed monthly release")
        revision = read(revision_path)
        expected_parent = {'manifest_path':state['manifest_path'],
            'manifest_sha256':state['manifest_sha256'], 'run_id':state['run_id'], 'cutoff':cutoff}
        if (revision.get('repository') != 'metaUro' or revision.get('status') != 'completed'
                or revision.get('revision_id') != revision_id
                or revision.get('parent_monthly') != expected_parent
                or revision.get('active_overlay_index') != read(root/'updates/manifest.json')
                or revision.get('parent_audit_artifact_sha256') != release.get('audit_artifact_sha256')):
            raise ValueError('Evidence revision does not reconcile with the completed monthly release')
        for field in ('summary_sha256','code_sha256'):
            inventory = revision.get(field)
            if not isinstance(inventory, dict) or not inventory:
                raise ValueError('Evidence revision inventory is absent')
            for relative, digest in inventory.items():
                if sha(bound(root, relative)) != digest:
                    raise ValueError('Evidence revision artifact checksum mismatch: '+relative)
    summary_inventory = revision['summary_sha256'] if revision else release.get('summary_sha256', {})
    for inventory in (release.get("audit_artifact_sha256", {}), summary_inventory):
        if not inventory:
            raise ValueError("Release artifact inventory is absent")
        for relative, digest in inventory.items():
            if sha(bound(root,relative)) != digest:
                raise ValueError("Release artifact checksum mismatch: " + relative)
    for scope in SOURCES:
        receipt = release['scopes'][scope]
        screening = path.parent/scope/'screening'
        native_manifest = screening/'monthly_run_manifest.json'
        if sha(native_manifest) != receipt.get('native_manifest_sha256'):
            raise ValueError('Native monthly manifest checksum mismatch')
        native = read(native_manifest)
        if (native.get('repository') != scope or native.get('run_id') != receipt.get('run_id')
                or native.get('status') not in {'validated', 'completed'}):
            raise ValueError('Native monthly manifest identity mismatch')
        raw = native.get('raw_packet_sha256', {})
        if (not {'candidates.json', 'final_screening.json'}.issubset(raw)
                or raw != receipt.get('raw_packet_sha256')):
            raise ValueError('Native raw screening inventory mismatch')
        for relative, digest in raw.items():
            if sha(bound(screening, relative)) != digest:
                raise ValueError('Native raw screening checksum mismatch: ' + relative)
        parallel = native.get('independent_review', {})
        artifacts = parallel.get('artifact_sha256', {})
        status = {key:value for key,value in parallel.items() if key != 'artifact_sha256'}
        if (parallel.get('complete') is not True
                or parallel.get('models') != {'r2':'claude-opus-5', 'r3':'gemini-3.7-flash'}
                or status != receipt.get('independent_review')
                or 'monthly_parallel/monthly_status.json' not in artifacts):
            raise ValueError('Independent review status or inventory mismatch')
        for inventory in (artifacts, parallel.get('source_sha256', {}),
                          native.get('review', {}).get('source_sha256', {})):
            if not inventory:
                raise ValueError('Native review evidence inventory is absent')
            for relative, digest in inventory.items():
                if sha(bound(screening, relative)) != digest:
                    raise ValueError('Native review evidence checksum mismatch: ' + relative)
        if read(screening/'monthly_parallel/monthly_status.json') != status:
            raise ValueError('Independent review status receipt mismatch')
        gate_path = screening/'monthly_application_gate.json'
        if (sha(gate_path) != receipt.get('application_gate_sha256')
                or native.get('application_gate_sha256') != receipt.get('application_gate_sha256')):
            raise ValueError('Native application gate checksum mismatch')
        gate = read(gate_path)
        applied_filter = read(screening/'monthly_application_filter.json', {})
        if (applied_filter.get('gate_sha256') != receipt['application_gate_sha256']
                or applied_filter.get('source_final_screening_sha256') != gate.get('source_final_screening_sha256')):
            raise ValueError('Native application filter checksum mismatch')
        if native.get('review_artifacts_sha256') != {
                gate['review_csv']:gate['review_csv_sha256'],
                gate['adjudication_path']:gate['adjudication_sha256']}:
            raise ValueError('Native review artifact inventory mismatch')
        native_review = receipt.get('native_review_csv_artifact', {})
        native_csv = path.parent/scope/'review/native.csv'
        relative_csv = str(native_csv.relative_to(root))
        if (native_review.get('path') != relative_csv
                or native_review.get('sha256') != gate.get('review_csv_sha256')
                or release['audit_artifact_sha256'].get(relative_csv) != gate.get('review_csv_sha256')
                or sha(native_csv) != gate.get('review_csv_sha256')):
            raise ValueError('Preserved native review CSV checksum mismatch')
        relative_adjudication = Path(gate['adjudication_path']).relative_to(
            Path('data/monthly_updates')/receipt['run_id'])
        if (sha(bound(screening, relative_adjudication)) != gate.get('adjudication_sha256')
                or sha(screening/'final_screening.json') != gate.get('source_final_screening_sha256')):
            raise ValueError('Native application gate evidence checksum mismatch')
        manifest = load_overlay(root,scope)
        if manifest.get("search_through") != cutoff or manifest.get("producer") != "metaUro":
            raise ValueError("Monthly scope provenance is invalid")
        for entry in manifest["files"].values():
            if not entry.get("deleted") and sha(bound(root/'updates'/scope,entry['stored_path'])) != entry['sha256']:
                raise ValueError("Active monthly output checksum mismatch")
        for entry in manifest.get('review_files',[]):
            if sha(bound(root/'updates'/scope,entry['stored_path'])) != entry['sha256']:
                raise ValueError("Human review checksum mismatch")
    return {"repository":"metaUro", "cutoff":cutoff, "run_id":state['run_id'],
            "manifest_sha256":state['manifest_sha256'], "manifest_path":state['manifest_path'],
            "source_scopes":list(SOURCES), "status":"completed",
            **({'evidence_revision':revision['revision_id']} if revision else {})}


def verify_git_index(root=ROOT):
    """Require Git's staged release to retain every locally validated byte.

    Compare unfiltered Git object hashes, including manifests and their proof
    files, after staging. Normalization, clean filters, missing files, merge
    entries and stale staged content all block publication.
    """
    root = Path(root).resolve()
    completed = validate_completed_release(root)
    if completed is None:
        raise ValueError('A completed local release is required before Git publication')
    runtime_manifest(root)
    required = {'evidence-manifest.json'}
    required.update(entry['path'] for entry in read(root/'evidence-manifest.json')['files'])
    for name in ('updates', 'summary', 'monthlyHumanReview', 'runtime_code'):
        required.update(str(path.relative_to(root)) for path in (root/name).rglob('*')
                        if path.is_file() and '__pycache__' not in path.parts
                        and path.name != '.DS_Store'
                        and not summary_lock_scratch(path.relative_to(root))
                        and not path.name.endswith(('.sqlite3-wal', '.sqlite3-shm')))
    # Recovery can contain obsolete SQLite sidecars. A sidecar explicitly bound
    # by a completed proof remains mandatory, just like every other audit file.
    release = read(bound(root, completed['manifest_path']))
    for inventory in ('audit_artifact_sha256', 'summary_sha256'):
        required.update(release[inventory])
    raw = subprocess.run(['git', 'ls-files', '--stage', '-z'], cwd=root,
                         capture_output=True, check=True).stdout
    entries = {}
    for entry in raw.split(b'\0'):
        if entry:
            header, relative = entry.split(b'\t', 1)
            mode, oid, stage = header.decode('ascii').split()
            entries.setdefault(os.fsdecode(relative), []).append((mode, oid, stage))
    algorithm = subprocess.run(['git', 'rev-parse', '--show-object-format'], cwd=root,
                               capture_output=True, text=True, check=True).stdout.strip()
    if algorithm not in {'sha1', 'sha256'}:
        raise ValueError('Unsupported Git object hash format')
    for relative in sorted(required):
        values = entries.get(relative, [])
        if len(values) != 1 or values[0][2] != '0' or values[0][0] not in {'100644', '100755'}:
            raise ValueError('Required release file is missing or unresolved in Git index: ' + relative)
        path = bound(root, relative)
        digest = hashlib.new(algorithm)
        digest.update(('blob ' + str(path.stat().st_size) + '\0').encode('ascii'))
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != values[0][1]:
            raise ValueError('Git index bytes differ from validated release: ' + relative)
    return {'status':'verified', 'run_id':completed['run_id'],
            'required_files':len(required), 'git_object_format':algorithm}


def restore(root=ROOT):
    """Recover paid request checkpoints from this repository's failed runs only."""
    root = Path(root).resolve()
    repository = os.environ.get('GITHUB_REPOSITORY', 'YushuShi/metaUro')
    if repository != 'YushuShi/metaUro':
        raise ValueError('Unexpected recovery repository')
    listing = json.loads(subprocess.check_output(['gh','api',f'repos/{repository}/actions/artifacts?per_page=100'],text=True))
    artifacts = [item for item in listing.get('artifacts',[]) if item.get('name') == 'metauro-monthly-checkpoint' and not item.get('expired')]
    current = read(root/'updates/manifest.json')
    runtime_sha = sha(root/'runtime_code/monthly/manifest.json')
    for item in sorted(artifacts,key=lambda item:item['created_at'],reverse=True):
        with tempfile.TemporaryDirectory(prefix='metauro-checkpoint-') as temp:
            folder = Path(temp)
            subprocess.run(['gh','run','download',str(item['workflow_run']['id']),'--repo',repository,
                            '--name','metauro-monthly-checkpoint','--dir',str(folder)],check=True)
            for input_file in sorted(folder.glob('updates/monthly/*/input.json'),reverse=True):
                pinned = read(input_file)
                if pinned.get('overlay_index') != current or pinned.get('runtime_sha256') != runtime_sha:
                    continue
                span = pinned.get('window',{})
                run_id = span.get('run_id','')
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}',run_id):
                    continue
                target = root/'updates/monthly'/run_id
                if target.exists():
                    return {'status':'local_checkpoint_retained','run_id':run_id}
                shutil.copytree(input_file.parent,target)
                # Checkpoints do not contain the entire published overlay and
                # summary tree. Always replay preparation before installation.
                release = read(target/'release.json', {})
                if release:
                    release.update(status='recovered_checkpoint')
                    write(target/'release.json', release)
                write(root/'updates/monthly/active.json',{**span,'status':'prepared'})
                review = folder/'monthlyHumanReview'
                if review.exists():
                    for source in review.glob(run_id+'*.csv'):
                        destination = root/'monthlyHumanReview'/source.name
                        if not destination.exists():
                            destination.parent.mkdir(parents=True,exist_ok=True)
                            shutil.copyfile(source,destination)
                return {'status':'restored','run_id':run_id,'artifact_id':item['id']}
    return {'status':'no_matching_checkpoint'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("command", choices=("preflight", "prepare", "publish", "restore", "checkpoint", "verify-index"))
    parser.add_argument("--gateway", action="store_true")
    args = parser.parse_args(argv)
    if args.command == 'verify-index':
        print(json.dumps(verify_git_index(args.repo_root), indent=2))
        return 0
    if args.command == "preflight":
        result = preflight(args.repo_root, gateway=args.gateway)
        print(json.dumps(result, indent=2))
        return 0 if result["ready"] else 1
    if args.command == 'restore':
        print(json.dumps(restore(args.repo_root),indent=2))
        return 0
    if args.command == 'checkpoint':
        print(json.dumps(checkpoint(args.repo_root),indent=2))
        return 0
    result = prepare(args.repo_root) if args.command == "prepare" else publish(args.repo_root)
    print(json.dumps({key: result.get(key) for key in ("repository", "status", "run_id", "end_date")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
