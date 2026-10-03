#!/usr/bin/env python3
"""Screen the sealed bacterial-abbreviation delta without repeating discovery.

This driver deliberately has no ESearch or apply stage.  It verifies the sealed
delta packet and every source digest, EFetches only the packet's listed PMIDs,
and delegates screening to the repository's canonical monthly implementation.
All generated packets and costs remain in a separate delta artifact tree.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import inspect
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import monthly_evidence_update as monthly  # noqa: E402


END_DATE = "2026-08-29"
RUN_NAME = f"bacterial_alias_delta_through_{END_DATE}"
DELTA_DIR = REPO_ROOT / "data" / RUN_NAME
DELTA_PACKET_PATH = DELTA_DIR / "bacterial_alias_delta.json"
DELTA_CHECKPOINT_PATH = DELTA_DIR / "search_checkpoint.json"
OUTPUT_DIR = REPO_ROOT / "data" / "monthly_updates" / RUN_NAME
REPORT_DIR = REPO_ROOT / "data" / "monthly_reports" / RUN_NAME
RUN_MANIFEST_PATH = OUTPUT_DIR / "delta_run_manifest.json"
FETCH_RECEIPT_PATH = OUTPUT_DIR / "metadata_fetch_receipt.json"
ERROR_RECEIPT_PATH = OUTPUT_DIR / "delta_driver_errors.json"
LOCK_PATH = OUTPUT_DIR / ".driver.lock"
EXPECTED_MODELS = {
    "first": "gpt-5.6-luna",
    "second": "gpt-5.6-sol",
    "full_text": "gpt-5.6-luna",
}


def _is_femina() -> bool:
    return hasattr(monthly, "candidate_context_index")


def _is_gut() -> bool:
    return hasattr(monthly, "candidate_context_map")


def _is_dual_repository() -> bool:
    return hasattr(monthly, "cost_accounting")


def _is_cutis() -> bool:
    return _is_dual_repository() and len(inspect.signature(monthly.context_id).parameters) == 4


def _repository_label() -> str:
    try:
        return str(load_json(DELTA_PACKET_PATH).get("repository") or REPO_ROOT.name)
    except RuntimeError:
        return REPO_ROOT.name


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Required JSON is unavailable or malformed: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Required JSON must contain an object: {path}")
    return value


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _repo_relative_file(relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeError(f"Unsafe source-artifact path: {relative!r}")
    path = REPO_ROOT.joinpath(*candidate.parts)
    cursor = REPO_ROOT
    for part in candidate.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RuntimeError(f"Source-artifact path traverses a symlink: {relative!r}")
    if not path.is_file():
        raise RuntimeError(f"Sealed source artifact is missing: {relative}")
    return path


def _additional_catalog() -> dict[str, Any]:
    raw = load_json(REPO_ROOT / "data" / "additionalExposure.json")
    exposures = raw.get("exposures") if isinstance(raw.get("exposures"), dict) else raw
    if not isinstance(exposures, dict):
        raise RuntimeError("additionalExposure.json has no exposure mapping")
    return exposures


def _canonical_packet_digest(packet: Mapping[str, Any]) -> str:
    return object_digest({key: value for key, value in packet.items() if key != "packet_digest"})


def _context_identity(row: Mapping[str, Any]) -> str:
    pmid = str(row.get("pmid") or "").strip()
    exposure = str(row.get("exposure") or "").strip()
    if _is_cutis():
        disease = str(row.get("disease") or "").strip()
        endpoint = str(row.get("endpoint") or "").strip()
        components = (pmid, exposure, disease, endpoint)
    else:
        cancer = str(row.get("cancer") or "").strip()
        components = (pmid, exposure, cancer)
    if any(not part or "|" in part for part in components):
        raise RuntimeError(f"Invalid delta context components: {dict(row)!r}")
    return "|".join(components)


def validate_delta_packet() -> dict[str, Any]:
    """Validate the immutable discovery packet and return it unchanged."""
    packet = load_json(DELTA_PACKET_PATH)
    checkpoint = load_json(DELTA_CHECKPOINT_PATH)
    if packet.get("schema_version") != 1 or packet.get("status") != "complete":
        raise RuntimeError("The bacterial-alias delta packet is not complete schema v1")
    if packet.get("publication_date_start") is not None:
        raise RuntimeError("The bacterial-alias delta must be an all-history scope")
    if packet.get("publication_date_end") != END_DATE:
        raise RuntimeError("The bacterial-alias delta has the wrong inclusive end date")
    if packet.get("source_packets_unchanged") is not True:
        raise RuntimeError("The delta packet does not attest unchanged source packets")
    if packet.get("active_screening_checkpoints_rewritten") is not False:
        raise RuntimeError("The delta packet rewrote an active screening checkpoint")
    recorded_digest = str(packet.get("packet_digest") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", recorded_digest):
        raise RuntimeError("The delta packet has no valid packet digest")
    if _canonical_packet_digest(packet) != recorded_digest:
        raise RuntimeError("The delta packet digest does not match its content")
    if (
        checkpoint.get("schema_version") != 1
        or checkpoint.get("status") != "complete"
        or checkpoint.get("packet_digest") != recorded_digest
        or checkpoint.get("plan_digest") != packet.get("plan_digest")
        or checkpoint.get("source_artifacts") != packet.get("source_artifacts")
    ):
        raise RuntimeError("The delta search checkpoint is not bound to the packet")

    alias_delta = packet.get("alias_delta")
    if not isinstance(alias_delta, dict) or object_digest(alias_delta) != packet.get("alias_delta_digest"):
        raise RuntimeError("The alias-delta vocabulary digest is invalid")
    catalog = _additional_catalog()
    contexts = packet.get("new_contexts")
    if not isinstance(contexts, list) or any(not isinstance(row, dict) for row in contexts):
        raise RuntimeError("The delta packet has an invalid new_contexts array")
    if len(contexts) != packet.get("new_context_count"):
        raise RuntimeError("The delta context count does not match new_contexts")
    seen: set[str] = set()
    for row in contexts:
        context = _context_identity(row)
        if row.get("context_id") != context or context in seen:
            raise RuntimeError(f"Invalid or duplicate delta context: {context}")
        seen.add(context)
        if row.get("requires_screening") is not True:
            raise RuntimeError(f"Delta context is not approved for screening: {context}")
        exposure = str(row.get("exposure") or "")
        matches = row.get("matched_alias_delta")
        allowed = alias_delta.get(exposure)
        if (
            exposure not in catalog
            or not isinstance(matches, list)
            or not matches
            or not isinstance(allowed, list)
            or not set(matches).issubset(set(allowed))
        ):
            raise RuntimeError(f"Delta alias binding is invalid: {context}")
        catalog_row = catalog[exposure]
        configured_aliases = (
            list(catalog_row.get("core") or []) + list(catalog_row.get("guarded") or [])
            if isinstance(catalog_row, dict)
            else list(catalog_row or [])
        )
        if not set(matches).issubset(set(configured_aliases)):
            raise RuntimeError(f"Delta aliases are absent from additionalExposure.json: {context}")

    pmids = sorted({str(row["pmid"]) for row in contexts}, key=lambda value: int(value))
    if packet.get("new_context_pmids") != pmids or packet.get("new_context_pmid_count") != len(pmids):
        raise RuntimeError("The delta PMID index does not match its exact contexts")
    queries = packet.get("queries")
    if (
        not isinstance(queries, list)
        or len(queries) != packet.get("query_count")
        or packet.get("completed_query_count") != packet.get("query_count")
    ):
        raise RuntimeError("The delta search plan is incomplete")

    artifacts = packet.get("source_artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise RuntimeError("The delta packet has no sealed source artifacts")
    for relative, expected in artifacts.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected or "")):
            raise RuntimeError(f"Invalid source digest for {relative}")
        if file_sha256(_repo_relative_file(str(relative))) != expected:
            raise RuntimeError(f"Sealed source artifact changed: {relative}")
    return packet


def _candidate_context(row: Mapping[str, Any]) -> dict[str, Any]:
    exposure = str(row["exposure"])
    if _is_cutis():
        disease = str(row["disease"])
        endpoint = str(row["endpoint"])
        return {
            "exposure": exposure,
            "disease": disease,
            "cancer": disease,
            "endpoint": endpoint,
            "outcome": endpoint,
        }
    cancer = str(row["cancer"])
    if _is_femina():
        return {
            "exposure": exposure,
            "cancer": cancer,
            "outcome": "Incidence",
            **monthly.cancer_scope_metadata(cancer),
        }
    return {
        "exposure": exposure,
        "cancer": cancer,
        "outcome": "Incidence",
        "catalog_source": "additional",
        "direct_search_context": True,
        "included_source_context": False,
    }


def _screening_context_id(row: Mapping[str, Any]) -> str:
    if _is_femina():
        metadata = monthly.cancer_scope_metadata(str(row["cancer"]))
        return monthly.context_id(
            str(row["pmid"]),
            str(row["exposure"]),
            str(row["cancer"]),
            metadata.get("scope_id"),
        )
    return _context_identity(row)


def build_candidate_packet(
    delta: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
    *,
    request_count: int,
    ncbi_status: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the exact delta candidate packet and its no-ESearch receipt."""
    contexts = list(delta["new_contexts"])
    requested = list(delta["new_context_pmids"])
    returned = sorted((str(value) for value in records), key=lambda value: int(value))
    if returned != requested:
        missing = sorted(set(requested) - set(returned), key=lambda value: int(value))
        unexpected = sorted(set(returned) - set(requested), key=lambda value: int(value))
        raise RuntimeError(
            f"NCBI EFetch did not return the exact delta PMID set; missing={missing}, unexpected={unexpected}"
        )
    grouped: dict[str, list[dict[str, Any]]] = {pmid: [] for pmid in requested}
    context_map: list[dict[str, str]] = []
    for row in contexts:
        pmid = str(row["pmid"])
        grouped[pmid].append(_candidate_context(row))
        context_map.append(
            {
                "delta_context_id": str(row["context_id"]),
                "screening_context_id": _screening_context_id(row),
            }
        )
    articles: list[dict[str, Any]] = []
    for pmid in requested:
        article = copy.deepcopy(dict(records[pmid]))
        if str(article.get("pmid") or "") != pmid:
            raise RuntimeError(f"NCBI metadata changed the requested PMID identity: {pmid}")
        article["candidate_contexts"] = grouped[pmid]
        articles.append(article)
    screening_ids = [row["screening_context_id"] for row in context_map]
    if len(screening_ids) != len(set(screening_ids)):
        raise RuntimeError("Delta contexts collide after canonical scope conversion")
    candidate = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "run_mode": "bacterial_alias_delta",
        "publication_date_start": None,
        "publication_date_end": END_DATE,
        "catalog": "additional",
        "exposure_scope": "additional",
        "selected_exposures": sorted({str(row["exposure"]) for row in contexts}),
        "listed_exposures": sorted({str(row["exposure"]) for row in contexts}),
        "discovery_complete": True,
        "search_policy": "immutable bacterial-alias delta; no ESearch in this follow-on",
        "source_delta_packet": str(DELTA_PACKET_PATH.relative_to(REPO_ROOT)),
        "source_delta_packet_digest": delta["packet_digest"],
        "source_artifacts": copy.deepcopy(delta["source_artifacts"]),
        "source_packets_unchanged": True,
        "candidate_pmids": len(requested),
        "candidate_contexts": len(contexts),
        "ncbi_requests": {"esearch": 0, "efetch": int(request_count), "total": int(request_count)},
        "articles": articles,
    }
    receipt = {
        "schema_version": 1,
        "status": "complete",
        "created_at": datetime.now().astimezone().isoformat(),
        "operation": "NCBI PubMed EFetch metadata only",
        "esearch_requests": 0,
        "efetch_requests": int(request_count),
        "ncbi_api_key_status": ncbi_status,
        "requested_pmids": requested,
        "returned_pmids": returned,
        "requested_pmids_digest": object_digest(requested),
        "delta_context_count": len(contexts),
        "screening_context_count": len(screening_ids),
        "context_map": context_map,
        "context_map_digest": object_digest(context_map),
        "source_delta_packet_sha256": file_sha256(DELTA_PACKET_PATH),
        "source_delta_packet_digest": delta["packet_digest"],
        "source_checkpoint_sha256": file_sha256(DELTA_CHECKPOINT_PATH),
        "verified_source_artifacts": copy.deepcopy(delta["source_artifacts"]),
    }
    return candidate, receipt


def _configure_entrez_efetch_only() -> str:
    """Configure credentials without issuing the usual validation ESearch."""
    signature = inspect.signature(monthly.configure_entrez)
    if "validate_api_key" in signature.parameters:
        return str(monthly.configure_entrez(validate_api_key=False))
    if hasattr(monthly, "load_external_credentials_if_requested"):
        monthly.load_external_credentials_if_requested()
    if hasattr(monthly, "configure_ncbi_socket_timeout"):
        monthly.configure_ncbi_socket_timeout()
    monthly.Entrez.email = os.getenv("NCBI_EMAIL") or os.getenv("PUBMED_EMAIL")
    monthly.Entrez.api_key = os.getenv("NCBI_API_KEY") or os.getenv("PUBMED_API_KEY")
    if not monthly.Entrez.email:
        raise RuntimeError("NCBI_EMAIL (or PUBMED_EMAIL) is not configured")
    return "configured_not_validated" if monthly.Entrez.api_key else "not_configured"


def _validate_prepared(delta: Mapping[str, Any]) -> dict[str, Any]:
    if not RUN_MANIFEST_PATH.is_file() or not FETCH_RECEIPT_PATH.is_file():
        raise RuntimeError("A partial delta preparation exists; refuse to overwrite it")
    manifest = load_json(RUN_MANIFEST_PATH)
    receipt = load_json(FETCH_RECEIPT_PATH)
    candidate_path = OUTPUT_DIR / "candidates.json"
    if (
        manifest.get("status") != "prepared"
        or manifest.get("source_delta_packet_digest") != delta.get("packet_digest")
        or manifest.get("source_delta_packet_sha256") != file_sha256(DELTA_PACKET_PATH)
        or manifest.get("source_checkpoint_sha256") != file_sha256(DELTA_CHECKPOINT_PATH)
        or manifest.get("candidate_sha256") != file_sha256(candidate_path)
        or manifest.get("metadata_fetch_receipt_sha256") != file_sha256(FETCH_RECEIPT_PATH)
    ):
        raise RuntimeError("The prepared delta run is stale or tampered")
    candidates = load_json(candidate_path)
    pmids = sorted(
        {str(article.get("pmid") or "") for article in candidates.get("articles", [])},
        key=lambda value: int(value),
    )
    if pmids != delta.get("new_context_pmids") or receipt.get("requested_pmids") != pmids:
        raise RuntimeError("Prepared metadata no longer covers the exact delta PMIDs")
    expected = {_screening_context_id(row) for row in delta["new_contexts"]}
    if _is_femina():
        actual = set(monthly.candidate_context_index(candidates))
    elif _is_gut():
        actual = set(monthly.candidate_context_map(candidates))
    elif _is_cutis():
        actual = {
            monthly.context_id(
                str(article["pmid"]),
                str(context["exposure"]),
                str(context["disease"]),
                str(context["endpoint"]),
            )
            for article in candidates["articles"]
            for context in article["candidate_contexts"]
        }
    else:
        actual = {
            monthly.context_id(
                str(article["pmid"]),
                str(context["exposure"]),
                str(context["cancer"]),
            )
            for article in candidates["articles"]
            for context in article["candidate_contexts"]
        }
    if actual != expected:
        raise RuntimeError("Prepared candidates do not preserve the exact delta context scope")
    return candidates


def prepare_metadata(delta: Mapping[str, Any]) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    candidate_path = OUTPUT_DIR / "candidates.json"
    existing = [path for path in (candidate_path, FETCH_RECEIPT_PATH, RUN_MANIFEST_PATH) if path.exists()]
    if existing:
        if len(existing) != 3:
            raise RuntimeError("A partial delta preparation exists; refuse to overwrite it")
        return _validate_prepared(delta)
    ncbi_status = _configure_entrez_efetch_only()
    requested = list(delta["new_context_pmids"])
    records, request_count = monthly.entrez_fetch(requested)
    candidate, receipt = build_candidate_packet(
        delta,
        records,
        request_count=request_count,
        ncbi_status=ncbi_status,
    )
    save_json(candidate_path, candidate)
    receipt["candidates_sha256"] = file_sha256(candidate_path)
    save_json(FETCH_RECEIPT_PATH, receipt)
    manifest = {
        "schema_version": 1,
        "status": "prepared",
        "created_at": datetime.now().astimezone().isoformat(),
        "repository": _repository_label(),
        "run_name": RUN_NAME,
        "pipeline": "gpt-5.6-luna -> gpt-5.6-sol -> deterministic Wald -> conditional gpt-5.6-luna",
        "apply_permitted": False,
        "esearch_permitted": False,
        "source_packets_mutable": False,
        "source_delta_packet_digest": delta["packet_digest"],
        "source_delta_packet_sha256": file_sha256(DELTA_PACKET_PATH),
        "source_checkpoint_sha256": file_sha256(DELTA_CHECKPOINT_PATH),
        "source_artifacts": copy.deepcopy(delta["source_artifacts"]),
        "delta_context_count": len(delta["new_contexts"]),
        "delta_pmid_count": len(requested),
        "candidate_sha256": file_sha256(candidate_path),
        "metadata_fetch_receipt_sha256": file_sha256(FETCH_RECEIPT_PATH),
    }
    save_json(RUN_MANIFEST_PATH, manifest)
    validate_delta_packet()
    return _validate_prepared(delta)


def _write_local_dual_costs(output_dir: Path) -> Path:
    accounting = monthly.cost_accounting
    cost_path = output_dir / "cost.txt"
    parameters = inspect.signature(accounting.write_cost_report).parameters
    kwargs: dict[str, Any] = {"report_path": cost_path}
    if "exposure_scope" in parameters:
        kwargs["exposure_scope"] = "additional"
    accounting.write_cost_report(output_dir, REPO_ROOT, **kwargs)
    additional_path = output_dir / "additionalExposureCost.txt"
    if hasattr(accounting, "write_additional_exposure_cost_report"):
        accounting.write_additional_exposure_cost_report(
            output_dir,
            REPO_ROOT,
            report_path=additional_path,
        )
    elif "exposure_scope" in parameters:
        accounting.write_cost_report(
            output_dir,
            REPO_ROOT,
            report_path=additional_path,
            exposure_scope="additional",
        )
    return cost_path


def _install_local_cost_isolation() -> None:
    """Keep live stage cost updates inside the delta tree for this process."""
    if _is_dual_repository():
        monthly.cost_accounting.update_cost_report_for_run = _write_local_dual_costs
        return
    if _is_gut():
        original = monthly._write_cost_text
        protected = {
            (REPO_ROOT / "cost.txt").absolute(),
            (REPO_ROOT / "additionalExposureCost.txt").absolute(),
        }

        def delta_write(path: Path, content: str) -> None:
            if Path(path).absolute() in protected:
                return
            original(path, content)

        monthly._write_cost_text = delta_write


def _write_local_costs() -> Path:
    if _is_dual_repository():
        path = _write_local_dual_costs(OUTPUT_DIR)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("cost.txt", "additionalExposureCost.txt"):
            source = OUTPUT_DIR / name
            if source.is_file():
                (REPORT_DIR / name).write_bytes(source.read_bytes())
        return path
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return monthly.write_run_cost_report(OUTPUT_DIR, REPORT_DIR)


def _canonical_stage_packet(stage: str) -> tuple[Path, dict[str, Any]]:
    stem = {
        "first": "first_screen.json",
        "second": "second_screen.json",
        "full_text": "full_text_review.json",
    }[stage]
    chatgpt_path = OUTPUT_DIR / f"chatgpt_{stem}"
    path = chatgpt_path if chatgpt_path.is_file() else OUTPUT_DIR / stem
    return path, load_json(path)


def _validate_stage_model(stage: str) -> None:
    path, packet = _canonical_stage_packet(stage)
    model = str(packet.get("model") or "").casefold()
    if model != EXPECTED_MODELS[stage]:
        raise RuntimeError(f"Canonical {stage} packet used the wrong model: {model!r}")
    if any(OUTPUT_DIR.glob("deepseek_*screen*.json")):
        raise RuntimeError("DeepSeek artifacts are prohibited in the canonical delta run")
    if not path.is_file():
        raise RuntimeError(f"Canonical {stage} packet is missing")


def _record_stage(stage: str, delta: Mapping[str, Any]) -> None:
    validate_delta_packet()
    manifest = load_json(RUN_MANIFEST_PATH)
    artifacts: dict[str, str] = {}
    for path in sorted(OUTPUT_DIR.iterdir()):
        if (
            path.is_file()
            and path != RUN_MANIFEST_PATH
            and not path.name.startswith(".")
            and path.suffix in {".json", ".txt", ".csv"}
        ):
            artifacts[path.name] = file_sha256(path)
    completed_stages = ["prepare"]
    if (OUTPUT_DIR / "first_screen.json").is_file():
        completed_stages.append("first-screen")
    if (OUTPUT_DIR / "second_screen.json").is_file():
        completed_stages.append("second-screen")
    if (OUTPUT_DIR / "full_text_review.json").is_file():
        completed_stages.append("full-text")
    if (OUTPUT_DIR / "final_screening.json").is_file():
        completed_stages.append("finalize")
    if (OUTPUT_DIR / "cost.txt").is_file():
        completed_stages.append("cost-report")
    manifest.update(
        {
            "last_completed_stage": stage,
            "completed_stages": completed_stages,
            "screening_complete": "finalize" in completed_stages,
            "updated_at": datetime.now().astimezone().isoformat(),
            "source_delta_packet_sha256": file_sha256(DELTA_PACKET_PATH),
            "source_checkpoint_sha256": file_sha256(DELTA_CHECKPOINT_PATH),
            "artifacts": artifacts,
        }
    )
    save_json(RUN_MANIFEST_PATH, manifest)


def run_stage(stage: str, *, batch_size: int) -> None:
    delta = validate_delta_packet()
    if stage == "validate":
        if (OUTPUT_DIR / "candidates.json").exists():
            _validate_prepared(delta)
        print(json.dumps({"repository": _repository_label(), "contexts": delta["new_context_count"], "pmids": delta["new_context_pmid_count"], "status": "valid"}))
        return
    prepare_metadata(delta)
    _install_local_cost_isolation()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if stage == "prepare":
        _write_local_costs()
    elif stage == "first-screen":
        monthly.first_screen(OUTPUT_DIR, batch_size=batch_size, force=False)
        _validate_stage_model("first")
    elif stage == "second-screen":
        _validate_stage_model("first")
        monthly.second_screen(OUTPUT_DIR, batch_size=min(batch_size, 8), force=False)
        _validate_stage_model("second")
        monthly.write_reports(OUTPUT_DIR, REPORT_DIR)
    elif stage == "full-text":
        _validate_stage_model("first")
        _validate_stage_model("second")
        monthly.full_text_review(OUTPUT_DIR, force=False)
        _validate_stage_model("full_text")
    elif stage == "finalize":
        for required in ("first", "second", "full_text"):
            _validate_stage_model(required)
        monthly.finalize_evidence(OUTPUT_DIR, REPORT_DIR)
    elif stage == "cost-report":
        _write_local_costs()
    elif stage == "all":
        monthly.first_screen(OUTPUT_DIR, batch_size=batch_size, force=False)
        _validate_stage_model("first")
        monthly.second_screen(OUTPUT_DIR, batch_size=min(batch_size, 8), force=False)
        _validate_stage_model("second")
        monthly.write_reports(OUTPUT_DIR, REPORT_DIR)
        monthly.full_text_review(OUTPUT_DIR, force=False)
        _validate_stage_model("full_text")
        monthly.finalize_evidence(OUTPUT_DIR, REPORT_DIR)
        _write_local_costs()
    else:  # pragma: no cover - argparse protects this branch
        raise RuntimeError(f"Unknown stage: {stage}")
    _record_stage(stage, delta)


def _redacted_error(exc: BaseException) -> str:
    text = str(exc)
    text = re.sub(r"(?i)(api[_ -]?key|authorization|bearer)(\s*[:=]\s*)\S+", r"\1\2[REDACTED]", text)
    return text[:2000]


def record_error(stage: str, exc: BaseException) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    packet: dict[str, Any]
    if ERROR_RECEIPT_PATH.exists():
        packet = load_json(ERROR_RECEIPT_PATH)
    else:
        packet = {"schema_version": 1, "errors": []}
    errors = packet.get("errors")
    if not isinstance(errors, list):
        errors = []
    errors.append(
        {
            "created_at": datetime.now().astimezone().isoformat(),
            "stage": stage,
            "error_type": type(exc).__name__,
            "message": _redacted_error(exc),
            "source_delta_packet_sha256": file_sha256(DELTA_PACKET_PATH) if DELTA_PACKET_PATH.is_file() else None,
        }
    )
    packet["errors"] = errors
    save_json(ERROR_RECEIPT_PATH, packet)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=("validate", "prepare", "first-screen", "second-screen", "full-text", "finalize", "cost-report", "all"),
        default="validate",
    )
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    return args


def main() -> int:
    args = parse_args()
    if args.stage == "validate":
        run_stage(args.stage, batch_size=args.batch_size)
        return 0
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another bacterial-alias delta driver is active") from exc
        run_stage(args.stage, batch_size=args.batch_size)
    return 0


if __name__ == "__main__":
    selected_stage = "unknown"
    try:
        selected_stage = parse_args().stage
        raise SystemExit(main())
    except Exception as error:
        record_error(selected_stage, error)
        print(f"Bacterial-alias delta failed: {type(error).__name__}: {_redacted_error(error)}", file=sys.stderr, flush=True)
        raise
