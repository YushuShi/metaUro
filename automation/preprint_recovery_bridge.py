"""Preserve native preprint R3 evidence in Uro without repeating scientific work.

No retrieval, model, pooling, or source-overlay projection happens here. Native
record selectors are retained as lineage holds until a verified Uro alias exists.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

FALLBACK_MODELS = ("gpt-6-luna", "gemini-3.8-flash", "gpt-6-astra")
PRIVATE_ROOT = Path(".monthly-checkpoint/preprint_recovery")
REVIEW_ROOT = Path("manualReview/preprint_recovery/monthly")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _bound(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Preprint artifact path escapes its native repository")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Preprint artifact path escapes its native repository")
    return target


def _public(relative):
    path = Path(relative)
    return (not path.is_absolute() and ".." not in path.parts
        and not any(part.startswith(".") or part in {"source_cache", "blobs", "prompts", "responses", "raw"}
                    for part in path.parts)
        and path.suffix.lower() in {".json", ".csv", ".md"}
        and not path.name.endswith((".body.json", ".prompt.json")))


def capture_scope_recovery(root, run, native_binding):
    """Validate the native poststep receipt before the Uro scope can validate."""
    if native_binding is None:
        return None  # Historical releases predate this additive poststep.
    root, run = Path(root).resolve(), Path(run).resolve()
    binding = dict(native_binding)
    if binding.get("schema_version") != 1 or binding.get("status") != "complete":
        raise ValueError("Native preprint recovery has not completed")
    path = _bound(root, binding["manifest_path"])
    if not path.is_relative_to(run / "preprint_recovery") or _sha(path) != binding.get("manifest_sha256"):
        raise ValueError("Native preprint recovery manifest checksum or location mismatch")
    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("status") != "complete" or manifest.get("run_id") != run.name:
        raise ValueError("Native preprint recovery identity/status mismatch")
    files = dict(manifest.get("owned_public_files") or {})
    for relative, checksum in (binding.get("owned_public_files") or {}).items():
        if relative in files and files[relative] != checksum:
            raise ValueError("Native public preprint inventories disagree")
        files[relative] = checksum
    files[str(path.relative_to(root))] = binding["manifest_sha256"]
    for relative, checksum in files.items():
        source = _bound(root, relative)
        if not _public(relative) or not (source.is_relative_to(run / "preprint_recovery")
                or Path(relative).is_relative_to(Path("manualReview/preprint_recovery"))):
            raise ValueError("Private or unapproved preprint source artifact cannot be published")
        if _sha(source) != checksum:
            raise ValueError("Native public preprint artifact checksum mismatch")
    for job in manifest.get("jobs") or []:
        if job.get("result_path") and files.get(job["result_path"]) != job.get("result_sha256"):
            raise ValueError("Native preprint job result is not sealed by the public inventory")
    return {**binding, "owned_public_files": files, "cost": manifest.get("cost") or {},
        "holds": manifest.get("holds") or [], "native_jobs": manifest.get("jobs") or [],
        "projection_status": "held_pending_verified_native_aliases" if manifest.get("jobs") else "not_applicable_no_native_jobs",
        "projection_reason": "Native scientific records are retained; no guessed Uro selector/effect-ID aliases are applied.",
        "independent_reviews_performed_by_bridge": 0}


def _copy_immutable(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _sha(source) != _sha(target):
            raise ValueError("Published preprint audit artifact cannot be overwritten")
    else:
        shutil.copyfile(source, target)


def publish_scope_recovery(root, work, audit, scope, release_run_id, native_binding):
    """Copy only sealed public outputs; preserve native path aliases explicitly."""
    if native_binding is None:
        return None
    root, work, audit = Path(root).resolve(), Path(work).resolve(), Path(audit).resolve()
    manifest_path = _bound(work, native_binding["manifest_path"])
    run = manifest_path.parent.parent
    binding = capture_scope_recovery(work, run, native_binding)
    source_manifest = json.loads(manifest_path.read_text())
    if source_manifest.get("repository") != scope:
        raise ValueError("Native preprint recovery source scope mismatch")
    aliases, files, reviews = {}, {}, {}
    for relative, checksum in binding["owned_public_files"].items():
        source = _bound(work, relative)
        target = audit / scope / "preprint_recovery/files" / relative
        _copy_immutable(source, target)
        actual = str(target.relative_to(root))
        aliases[relative] = actual
        files[actual] = checksum
        if Path(relative).is_relative_to(Path("manualReview/preprint_recovery")):
            queue = root / REVIEW_ROOT / f"{release_run_id}_{scope}_{source.name}"
            _copy_immutable(source, queue)
            reviews[str(queue.relative_to(root))] = checksum
    holds = list(binding["holds"])
    lineage = [{"repository": "metaUro", "source_repository": scope, "pmid": job.get("pmid"),
        "context_id": context, "reason": "verified_Uro_native_selector_alias_required",
        "action": "Retain native R3 evidence; verify exact Uro source/effect-ID lineage before optional projection."}
        for job in binding["native_jobs"] for context in (job.get("context_ids") or [""])]
    holds.extend(lineage)
    if lineage:
        import csv
        queue = root / REVIEW_ROOT / f"{release_run_id}_{scope}_lineage_holds.csv"
        from io import StringIO
        buffer = StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(lineage[0]), lineterminator="\n")
        writer.writeheader(); writer.writerows(lineage)
        raw = buffer.getvalue().encode("utf-8-sig")
        queue.parent.mkdir(parents=True, exist_ok=True)
        if queue.exists() and queue.read_bytes() != raw:
            raise ValueError("Published native preprint lineage queue cannot be overwritten")
        if not queue.exists():
            queue.write_bytes(raw)
        reviews[str(queue.relative_to(root))] = _sha(queue)
    jobs = []
    for native_job in binding["native_jobs"]:
        job = dict(native_job)
        if job.get("result_path"):
            if job["result_path"] not in aliases:
                raise ValueError("Native preprint job result is absent from the public inventory")
            job["native_result_path"] = job["result_path"]
            job["result_path"] = aliases[job["result_path"]]
        jobs.append(job)
    wrapper = {"schema_version": 1, "status": "complete", "repository": "metaUro",
        "source_scope": scope, "run_id": release_run_id, "native_run_id": run.name,
        "source_manifest_path": aliases[binding["manifest_path"]],
        "source_manifest_sha256": binding["manifest_sha256"],
        "source_path_aliases": aliases, "owned_public_files": {**files, **reviews},
        "cost": binding["cost"], "holds": holds, "jobs": jobs,
        "projection_status": binding["projection_status"],
        "independent_reviews_performed_by_bridge": 0}
    wrapper_path = audit / scope / "preprint_recovery/recovery_manifest.json"
    raw = (json.dumps(wrapper, ensure_ascii=False, indent=2) + "\n").encode()
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    if wrapper_path.exists() and wrapper_path.read_bytes() != raw:
        raise ValueError("Published Uro preprint wrapper cannot be overwritten")
    if not wrapper_path.exists():
        wrapper_path.write_bytes(raw)
    return {**wrapper, "manifest_path": str(wrapper_path.relative_to(root)),
            "manifest_sha256": _sha(wrapper_path), "root_manual_review_files": reviews}


def validate_published_recovery(root, binding):
    """Read-only downstream verification; private source packets are not needed."""
    if binding is None:
        return None
    if binding.get("status") != "complete" or not binding.get("source_path_aliases"):
        raise ValueError("Published native preprint recovery binding is incomplete")
    for relative, checksum in {**binding["owned_public_files"], **binding.get("root_manual_review_files", {})}.items():
        if not _public(relative) or _sha(_bound(root, relative)) != checksum:
            raise ValueError("Published preprint recovery artifact checksum mismatch")
    path = _bound(root, binding["manifest_path"])
    if _sha(path) != binding["manifest_sha256"]:
        raise ValueError("Published native preprint manifest changed")
    wrapper = json.loads(path.read_text())
    if wrapper.get("repository") != "metaUro" or wrapper.get("status") != "complete":
        raise ValueError("Published Uro preprint wrapper identity mismatch")
    for relative, checksum in wrapper["owned_public_files"].items():
        if binding["owned_public_files"].get(relative) != checksum:
            raise ValueError("Published Uro preprint wrapper inventory disagrees")
    for job in wrapper.get("jobs") or []:
        if job.get("result_path") and wrapper["owned_public_files"].get(job["result_path"]) != job.get("result_sha256"):
            raise ValueError("Published native preprint job result binding disagrees")
    source = _bound(root, wrapper["source_manifest_path"])
    if _sha(source) != wrapper["source_manifest_sha256"]:
        raise ValueError("Original native preprint source manifest changed")
    native = json.loads(source.read_text())
    for relative, checksum in (native.get("owned_public_files") or {}).items():
        actual = binding["source_path_aliases"].get(relative)
        if actual is None or binding["owned_public_files"].get(actual) != checksum:
            raise ValueError("Published native preprint aliases do not reconcile")
    return {"status": "complete", "manifest_path": binding["manifest_path"],
            "manifest_sha256": binding["manifest_sha256"], "cost": binding["cost"],
            "holds": binding["holds"], "projection_status": binding["projection_status"]}


def aggregate_recovery(scopes):
    bindings = {scope: receipt["preprint_recovery"] for scope, receipt in scopes.items()
                if receipt.get("preprint_recovery") is not None}
    return {"schema_version": 1, "scopes": bindings,
        "cost": {"gateway_reported_cost_usd": sum(float(b["cost"].get("gateway_reported_cost_usd") or 0) for b in bindings.values()),
                 "unknown_cost_calls": sum(int(b["cost"].get("unknown_cost_calls") or 0) for b in bindings.values()),
                 "dispatches": sum(int(b["cost"].get("dispatches") or 0) for b in bindings.values())},
        "manual_hold_count": sum(len(b.get("holds") or []) for b in bindings.values()),
        "independent_reviews_performed_by_bridge": 0}


def checkpoint_private(root, work, scope, native_run_id):
    """Preserve private paid replay state outside every public release namespace."""
    source = Path(work) / PRIVATE_ROOT / native_run_id
    target = Path(root) / PRIVATE_ROOT / scope / native_run_id
    if source.is_dir():
        for incoming in source.rglob("*"):
            if not incoming.is_file():
                continue
            relative = incoming.relative_to(source)
            destination = target / relative
            if destination.is_file() and _sha(incoming) != _sha(destination):
                historical = target / ".history" / _sha(destination) / relative
                _copy_immutable(destination, historical)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(incoming, destination)
    return str(target) if target.is_dir() else None


def restore_private(root, work, scope, native_run_id):
    source = Path(root) / PRIVATE_ROOT / scope / native_run_id
    target = Path(work) / PRIVATE_ROOT / native_run_id
    if source.is_dir() and not target.exists():
        shutil.copytree(source, target)


def checkpoint_queues(root, work, scope, native_run_id, release_run_id):
    """Preserve unfinished native queues without replacing any human-edited file."""
    copied = []
    for source in (Path(work) / REVIEW_ROOT).glob(native_run_id + "*.csv"):
        target = Path(root) / REVIEW_ROOT / f"{release_run_id}_{scope}_{source.name}"
        if target.exists() and _sha(source) != _sha(target):
            target = target.with_name(target.stem + "_snapshot_" + _sha(source)[:16] + target.suffix)
        _copy_immutable(source, target)
        copied.append(str(target.relative_to(root)))
    return copied


def restore_uploaded_recovery(root, folder, span):
    """Restore only this already-pinned window's private calls and queue bytes."""
    from datetime import date
    from re import fullmatch
    if not fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}", span.get("run_id", "")):
        raise ValueError("Invalid preprint checkpoint window")
    date.fromisoformat(span["end_date"])
    copies = []
    for scope in ("MetaVir", "metaPee"):
        date.fromisoformat(span["start_dates"][scope])
        native_id = span["start_dates"][scope] + "_" + span["end_date"]
        source = Path(folder) / PRIVATE_ROOT / scope / native_id
        if source.is_symlink():
            raise ValueError("Private preprint checkpoint cannot be a symlink")
        for file in source.rglob("*") if source.is_dir() else []:
            if file.is_symlink():
                raise ValueError("Private preprint checkpoint contains a symlink")
            if file.is_file():
                copies.append((file, Path(root) / PRIVATE_ROOT / scope / native_id / file.relative_to(source)))
    for file in (Path(folder) / REVIEW_ROOT).glob(span["run_id"] + "*.csv"):
        if file.is_symlink():
            raise ValueError("Preprint review checkpoint cannot be a symlink")
        copies.append((file, Path(root) / REVIEW_ROOT / file.name))
    # Validate all conflicts before writing any of this artifact's recovery bytes.
    for source, target in copies:
        if target.exists() and _sha(source) != _sha(target):
            raise ValueError("Private paid source or human review checkpoint conflicts; bytes preserved")
    for source, target in copies:
        _copy_immutable(source, target)
    return len(copies)
