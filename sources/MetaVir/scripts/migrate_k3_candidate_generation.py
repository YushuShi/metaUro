#!/usr/bin/env python3
"""Repair the comma-fragmented Vitamin K3 discovery generation.

The migration is deliberately two-phase. ``--prepare`` is offline and archives
the superseded packet/checkpoints. ``--execute`` performs only the query jobs
whose byte-exact PubMed query changed, stages a complete replacement generation,
and commits it only after validation. The state sidecar blocks ordinary screening
between preparation and a successfully resealed generation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import dual_screening  # noqa: E402
from scripts import monthly_evidence_update as monthly  # noqa: E402


TARGET_EXPOSURE = "Vitamin K3"
CORRECT_ALIAS = "2-methyl-1,4-naphthoquinone"
LEGACY_FRAGMENTS = ("2-methyl-1", "4-naphthoquinone")
STATE_NAME = monthly.CANDIDATE_GENERATION_STATE_NAME
STAGING_NAME = ".k3_generation_staging"
ARCHIVE_ROOT_NAME = "superseded_generations"
MIGRATION_PROTOCOL_VERSION = "k3-comma-alias-generation-v1"


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()


def sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_json(path: Path) -> dict[str, Any]:
    value = monthly.load_json(path, {})
    if not isinstance(value, dict):
        raise RuntimeError(f"Required JSON artifact is malformed: {path}")
    return value


def _current_and_legacy_terms() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    current = monthly.exposure_terms(revision_aware=True)
    aliases = current.get(TARGET_EXPOSURE)
    if not isinstance(aliases, list):
        raise RuntimeError("Vitamin K3 is absent from the revision-aware vocabulary")
    if aliases.count(CORRECT_ALIAS) != 1 or any(
        fragment in aliases for fragment in LEGACY_FRAGMENTS
    ):
        raise RuntimeError(
            "Current Vitamin K3 vocabulary must contain the exact comma-bearing alias "
            "once and no legacy fragments"
        )
    legacy = copy.deepcopy(current)
    index = legacy[TARGET_EXPOSURE].index(CORRECT_ALIAS)
    legacy[TARGET_EXPOSURE][index : index + 1] = list(LEGACY_FRAGMENTS)
    return current, legacy


def _query_jobs(terms: dict[str, list[str]]) -> dict[tuple[str, str], dict[str, str]]:
    jobs: dict[tuple[str, str], dict[str, str]] = {}
    for exposure, aliases in terms.items():
        for cancer, cancer_query in monthly.CANCER_QUERIES.items():
            query = monthly.build_query(aliases, cancer_query)
            jobs[(exposure, cancer)] = {
                "exposure": exposure,
                "cancer": cancer,
                "outcome": "Incidence",
                "query": query,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            }
    return jobs


def _changed_jobs(
    current: dict[str, list[str]],
    legacy: dict[str, list[str]],
) -> list[dict[str, str]]:
    current_jobs, legacy_jobs = _query_jobs(current), _query_jobs(legacy)
    if set(current_jobs) != set(legacy_jobs):
        raise RuntimeError("Vocabulary correction changed the search-job scope")
    changed = []
    for key in current_jobs:
        if current_jobs[key]["query_sha256"] == legacy_jobs[key]["query_sha256"]:
            continue
        changed.append(
            {
                "exposure": key[0],
                "cancer": key[1],
                "outcome": "Incidence",
                "old_query_sha256": legacy_jobs[key]["query_sha256"],
                "new_query_sha256": current_jobs[key]["query_sha256"],
            }
        )
    expected = [
        {
            "exposure": TARGET_EXPOSURE,
            "cancer": cancer,
            "outcome": "Incidence",
            "old_query_sha256": legacy_jobs[(TARGET_EXPOSURE, cancer)][
                "query_sha256"
            ],
            "new_query_sha256": current_jobs[(TARGET_EXPOSURE, cancer)][
                "query_sha256"
            ],
        }
        for cancer in monthly.CANCER_QUERIES
    ]
    if changed != expected:
        raise RuntimeError(
            "The correction must change every authoritative Vitamin K3 job and no other job"
        )
    return changed


def _artifact_manifest(directory: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(directory.iterdir(), key=lambda value: value.name.casefold()):
        if not path.is_file() or path.name == STATE_NAME:
            continue
        records.append(
            {
                "name": path.name,
                "sha256": monthly.file_sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    return records


def _archive_generation(
    output_dir: Path,
    generation_id: str,
    artifacts: list[dict[str, Any]],
) -> Path:
    archive = output_dir / ARCHIVE_ROOT_NAME / generation_id
    archive.mkdir(parents=True, exist_ok=True)
    for record in artifacts:
        source = output_dir / record["name"]
        target = archive / record["name"]
        if target.exists():
            if monthly.file_sha256(target) != record["sha256"]:
                raise RuntimeError(f"Archived artifact digest mismatch: {target}")
            continue
        shutil.copy2(source, target)
        if monthly.file_sha256(target) != record["sha256"]:
            raise RuntimeError(f"Archived artifact copy failed verification: {target}")
    archive_manifest = {
        "schema_version": 1,
        "migration_protocol_version": MIGRATION_PROTOCOL_VERSION,
        "generation_id": generation_id,
        "archived_at": utc_now(),
        "artifacts": artifacts,
    }
    monthly.save_json(archive / "archive_manifest.json", archive_manifest)
    return archive


def _verify_archive(output_dir: Path, state: dict[str, Any]) -> Path:
    archive = output_dir / str(state.get("archive_directory") or "")
    manifest = _required_json(archive / "archive_manifest.json")
    if manifest.get("generation_id") != state.get("superseded_generation_id"):
        raise RuntimeError("Superseded archive generation ID is stale")
    for record in manifest.get("artifacts", []):
        path = archive / str(record.get("name") or "")
        if (
            not path.is_file()
            or monthly.file_sha256(path) != record.get("sha256")
            or path.stat().st_size != record.get("bytes")
        ):
            raise RuntimeError(f"Superseded archive artifact failed verification: {path}")
    return archive


def prepare(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    state_path = output_dir / STATE_NAME
    existing = monthly.load_json(state_path, {})
    if existing:
        if (
            existing.get("schema_version") != 1
            or existing.get("migration_protocol_version")
            != MIGRATION_PROTOCOL_VERSION
        ):
            raise RuntimeError("An incompatible candidate-generation migration exists")
        _verify_archive(output_dir, existing)
        return existing

    candidate_path = output_dir / "candidates.json"
    progress_path = output_dir / "chatgpt_first_screen_progress.json"
    candidates = _required_json(candidate_path)
    progress = _required_json(progress_path)
    current_terms, legacy_terms = _current_and_legacy_terms()
    monthly._validate_current_revision_search_scope(
        candidates,
        exact_terms=legacy_terms,
    )
    changed = _changed_jobs(current_terms, legacy_terms)
    completion = {
        (item["exposure"], item["cancer"]): item
        for item in candidates.get("search_completion", [])
    }
    for job in changed:
        audit = completion.get((job["exposure"], job["cancer"]))
        if not audit or audit.get("query_sha256") != job["old_query_sha256"]:
            raise RuntimeError("Superseded search audit does not match the legacy query")

    candidate_sha = monthly.file_sha256(candidate_path)
    progress_sha = monthly.file_sha256(progress_path)
    generation_id = f"k3_alias_fragmented_{candidate_sha[:16]}"
    artifacts = _artifact_manifest(output_dir)
    archive = _archive_generation(output_dir, generation_id, artifacts)
    old_expected = dual_screening.expected_contexts(candidates)
    old_results = progress.get("results_by_context") or {}
    if not isinstance(old_results, dict) or any(
        cid not in old_expected for cid in old_results
    ):
        raise RuntimeError("First-screen checkpoint contains unknown candidate contexts")

    state = {
        "schema_version": 1,
        "migration_protocol_version": MIGRATION_PROTOCOL_VERSION,
        "status": "prepared",
        "prepared_at": utc_now(),
        "superseded_generation_id": generation_id,
        "archive_directory": str(archive.relative_to(output_dir)),
        "superseded_candidate_sha256": candidate_sha,
        "superseded_progress_sha256": progress_sha,
        "superseded_first_input_digest": progress.get("input_digest"),
        "superseded_vocabulary_digest": monthly.exposure_vocabulary_digest(
            legacy_terms
        ),
        "corrected_vocabulary_digest": monthly.exposure_vocabulary_digest(
            current_terms
        ),
        "synonyms_cache_sha256": monthly.file_sha256(monthly.SYNONYMS_FILE),
        "changed_jobs": changed,
        "unchanged_jobs": len(_query_jobs(current_terms)) - len(changed),
        "superseded_candidate_pmids": candidates.get("candidate_pmids"),
        "superseded_candidate_contexts": candidates.get("candidate_contexts"),
        "superseded_r1_completed_contexts": len(old_results),
        "superseded_r1_k3_completed_contexts": sum(
            item.get("exposure") == TARGET_EXPOSURE
            for item in old_results.values()
            if isinstance(item, dict)
        ),
        "staging_directory": STAGING_NAME,
    }
    monthly.save_json(state_path, state)
    return state


def _verify_prepared_inputs(
    output_dir: Path,
    state: dict[str, Any],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    archive = _verify_archive(output_dir, state)
    archived_candidate_path = archive / "candidates.json"
    archived_progress_path = archive / "chatgpt_first_screen_progress.json"
    if (
        monthly.file_sha256(archived_candidate_path)
        != state.get("superseded_candidate_sha256")
        or monthly.file_sha256(archived_progress_path)
        != state.get("superseded_progress_sha256")
    ):
        raise RuntimeError("Superseded candidate/checkpoint archive digest changed")
    current_terms, legacy_terms = _current_and_legacy_terms()
    if (
        monthly.exposure_vocabulary_digest(current_terms)
        != state.get("corrected_vocabulary_digest")
        or monthly.exposure_vocabulary_digest(legacy_terms)
        != state.get("superseded_vocabulary_digest")
        or monthly.file_sha256(monthly.SYNONYMS_FILE)
        != state.get("synonyms_cache_sha256")
        or _changed_jobs(current_terms, legacy_terms) != state.get("changed_jobs")
    ):
        raise RuntimeError("Vocabulary or changed-job scope changed after preparation")
    candidates = _required_json(archived_candidate_path)
    monthly._validate_current_revision_search_scope(
        candidates,
        exact_terms=legacy_terms,
    )
    return archive, current_terms, legacy_terms


def _run_corrected_searches(
    output_dir: Path,
    state: dict[str, Any],
    current_terms: dict[str, list[str]],
) -> dict[str, Any]:
    staging = output_dir / STAGING_NAME
    staging.mkdir(parents=True, exist_ok=True)
    result_path = staging / "corrected_search_results.json"
    if state.get("status") in {"searched", "ready_to_commit", "resealed"}:
        result = _required_json(result_path)
        if monthly.file_sha256(result_path) != state.get("search_results_sha256"):
            raise RuntimeError("Staged corrected search results failed digest validation")
        return result

    if state.get("status") != "prepared":
        raise RuntimeError(f"Unsupported migration status: {state.get('status')!r}")
    api_key_status = monthly.configure_entrez()
    records = []
    requests = 0
    started = time.time()
    current_jobs = _query_jobs(current_terms)
    for changed in state["changed_jobs"]:
        key = (changed["exposure"], changed["cancer"])
        job = current_jobs[key]
        found = monthly.entrez_search(
            job["query"],
            None,
            str(monthly.revision_manifest().get("revision_date") or ""),
        )
        audit = getattr(found, "audit", None)
        if not isinstance(audit, dict) or audit.get("complete") is not True:
            raise RuntimeError(f"Corrected ESearch audit is incomplete for {key!r}")
        requests += int(audit.get("request_count") or 0)
        records.append(
            {
                "exposure": key[0],
                "cancer": key[1],
                "outcome": "Incidence",
                "query_sha256": job["query_sha256"],
                "pmids": sorted({str(pmid) for pmid in found}, key=int),
                "audit": audit,
            }
        )
    packet = {
        "schema_version": 1,
        "migration_protocol_version": MIGRATION_PROTOCOL_VERSION,
        "created_at": utc_now(),
        "api_key_status": api_key_status,
        "changed_jobs": len(records),
        "esearch_requests": requests,
        "elapsed_seconds": round(time.time() - started, 1),
        "records": records,
    }
    monthly.save_json(result_path, packet)
    state.update(
        {
            "status": "searched",
            "searched_at": utc_now(),
            "search_results_sha256": monthly.file_sha256(result_path),
            "migration_esearch_requests": requests,
        }
    )
    monthly.save_json(output_dir / STATE_NAME, state)
    return packet


def _source_records(
    archive: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, set[tuple[str, str]]]]:
    candidates = _required_json(archive / "candidates.json")
    out_packet = _required_json(archive / "out_of_window_records.json")
    records: dict[str, dict[str, Any]] = {}
    contexts: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for record in [*candidates.get("articles", []), *out_packet.get("records", [])]:
        pmid = str(record.get("pmid") or "")
        if not pmid:
            raise RuntimeError("Superseded metadata contains an empty PMID")
        base = {key: value for key, value in record.items() if key != "candidate_contexts"}
        if pmid in records and records[pmid] != base:
            raise RuntimeError(f"Conflicting superseded metadata for PMID {pmid}")
        records[pmid] = base
        for context in record.get("candidate_contexts", []):
            exposure = str(context.get("exposure") or "")
            cancer = str(context.get("cancer") or "")
            if exposure and cancer and exposure != TARGET_EXPOSURE:
                contexts[pmid].add((exposure, cancer))
    for omission in candidates.get("redundant_parent_contexts", []):
        pmid = str(omission.get("pmid") or "")
        exposure = str(omission.get("exposure") or "")
        cancer = str(omission.get("cancer") or "")
        if pmid and exposure and cancer and exposure != TARGET_EXPOSURE:
            contexts[pmid].add((exposure, cancer))
    return records, contexts


def _build_resealed_generation(
    output_dir: Path,
    state: dict[str, Any],
    archive: Path,
    current_terms: dict[str, list[str]],
    search_packet: dict[str, Any],
) -> dict[str, Any]:
    staging = output_dir / STAGING_NAME
    commit_manifest_path = staging / "commit_manifest.json"
    if state.get("status") in {"ready_to_commit", "resealed"}:
        manifest = _required_json(commit_manifest_path)
        if monthly.file_sha256(commit_manifest_path) != state.get(
            "commit_manifest_sha256"
        ):
            raise RuntimeError("Staged commit manifest failed digest validation")
        return manifest
    if state.get("status") != "searched":
        raise RuntimeError("Corrected searches must be sealed before generation rebuild")

    old_candidates = _required_json(archive / "candidates.json")
    old_progress = _required_json(archive / "chatgpt_first_screen_progress.json")
    records_by_pmid, contexts_by_pmid = _source_records(archive)
    search_records = search_packet.get("records")
    if not isinstance(search_records, list) or len(search_records) != len(
        state["changed_jobs"]
    ):
        raise RuntimeError("Corrected search-result scope is incomplete")
    new_k3_contexts: set[tuple[str, str, str]] = set()
    for result in search_records:
        if result.get("exposure") != TARGET_EXPOSURE:
            raise RuntimeError("Corrected search result contains a non-K3 exposure")
        cancer = str(result.get("cancer") or "")
        for pmid in result.get("pmids", []):
            pmid = str(pmid)
            contexts_by_pmid[pmid].add((TARGET_EXPOSURE, cancer))
            new_k3_contexts.add((pmid, TARGET_EXPOSURE, cancer))

    redundant = monthly.dedupe_parent_search_contexts(contexts_by_pmid)
    needed_pmids = sorted(
        (pmid for pmid, contexts in contexts_by_pmid.items() if contexts),
        key=int,
    )
    missing_metadata = [pmid for pmid in needed_pmids if pmid not in records_by_pmid]
    if missing_metadata:
        fetched, fetch_requests = monthly.entrez_fetch(missing_metadata)
        records_by_pmid.update(fetched)
    else:
        fetch_requests = 0

    candidate_records, out_of_window = [], []
    start_date = old_candidates.get("publication_date_start")
    end_date = str(old_candidates.get("publication_date_end") or "")
    old_candidate_pmids = {
        str(record.get("pmid") or "") for record in old_candidates.get("articles", [])
    }
    for pmid in needed_pmids:
        record = copy.deepcopy(records_by_pmid[pmid])
        record["candidate_contexts"] = [
            {"exposure": exposure, "cancer": cancer, "outcome": "Incidence"}
            for exposure, cancer in sorted(contexts_by_pmid[pmid])
        ]
        selected_date = monthly.select_in_window_publication_date(
            record, start_date, end_date
        )
        if selected_date is None:
            if pmid in old_candidate_pmids:
                raise RuntimeError(
                    f"Previously in-window PMID {pmid} became out of window during migration"
                )
            out_of_window.append(record)
            continue
        record["published_date"] = selected_date
        candidate_records.append(record)

    completion = {
        (item["exposure"], item["cancer"]): copy.deepcopy(item)
        for item in old_candidates.get("search_completion", [])
    }
    for result in search_records:
        key = (result["exposure"], result["cancer"])
        completion[key] = {
            "exposure": key[0],
            "cancer": key[1],
            "query_sha256": result["query_sha256"],
            **result["audit"],
        }
    search_completion = sorted(
        completion.values(),
        key=lambda item: (item["exposure"].casefold(), item["cancer"].casefold()),
    )
    esearch_requests = sum(
        int(item.get("request_count") or 0) for item in search_completion
    )
    old_efetch = int((old_candidates.get("ncbi_requests") or {}).get("efetch") or 0)
    candidate_packet = copy.deepcopy(old_candidates)
    candidate_packet.update(
        {
            "created_at": utc_now(),
            "vocabulary_digest": monthly.exposure_vocabulary_digest(current_terms),
            "search_completion": search_completion,
            "redundant_parent_contexts_omitted": len(redundant),
            "redundant_parent_contexts": redundant,
            "ncbi_requests": {
                "esearch": esearch_requests,
                "efetch": old_efetch + fetch_requests,
                "total": esearch_requests + old_efetch + fetch_requests,
            },
            "candidate_pmids": len(candidate_records),
            "candidate_contexts": sum(
                len(record["candidate_contexts"]) for record in candidate_records
            ),
            "postfetch_out_of_window_omitted": len(out_of_window),
            "generation_migration": {
                "protocol_version": MIGRATION_PROTOCOL_VERSION,
                "superseded_generation_id": state["superseded_generation_id"],
                "superseded_candidate_sha256": state[
                    "superseded_candidate_sha256"
                ],
                "changed_jobs": state["changed_jobs"],
                "unchanged_jobs_reused": state["unchanged_jobs"],
                "metadata_records_reused": len(needed_pmids) - len(missing_metadata),
                "metadata_records_fetched": len(missing_metadata),
                "migration_efetch_requests": fetch_requests,
            },
            "articles": candidate_records,
        }
    )
    monthly._validate_current_revision_search_scope(candidate_packet)

    old_expected = dual_screening.expected_contexts(old_candidates)
    new_expected = dual_screening.expected_contexts(candidate_packet)
    model = str(old_progress.get("model") or "")
    if model != dual_screening.CHATGPT_FIRST_MODEL:
        raise RuntimeError("Superseded R1 checkpoint used an unexpected model")
    old_digests = dual_screening.first_context_input_digests(old_candidates, model)
    new_digests = dual_screening.first_context_input_digests(candidate_packet, model)
    carried: dict[str, dict[str, Any]] = {}
    invalidated = []
    for cid, result in (old_progress.get("results_by_context") or {}).items():
        if cid not in old_expected:
            raise RuntimeError(f"Superseded R1 result has unknown context: {cid}")
        validated = dual_screening.validate_first(
            {"results": [result]}, {cid: old_expected[cid]}
        )[0]
        if validated != result:
            raise RuntimeError(f"Superseded R1 result is not canonical: {cid}")
        if old_expected[cid]["exposure"] == TARGET_EXPOSURE:
            reason = "vitamin_k3_query_changed"
        elif cid not in new_expected:
            reason = "removed_candidate_lineage"
        elif old_digests[cid] != new_digests[cid]:
            reason = "complete_context_input_changed"
        else:
            carried[cid] = result
            continue
        invalidated.append({"context_id": cid, "reason": reason})

    new_input_digest = dual_screening.first_input_digest(
        candidate_packet, model
    )
    progress_packet = {
        "updated_at": utc_now(),
        "model": model,
        "input_digest": new_input_digest,
        "usage": old_progress.get("usage")
        or dual_screening.empty_usage(model),
        "results_by_context": carried,
        "context_input_digests": {
            cid: new_digests[cid] for cid in carried
        },
        "deferred_screening_problems": [],
        "generation_migration": {
            "protocol_version": MIGRATION_PROTOCOL_VERSION,
            "superseded_first_input_digest": old_progress.get("input_digest"),
            "carried_contexts": len(carried),
            "invalidated_contexts": len(invalidated),
            "carried_context_ids_sha256": sha256_json(sorted(carried)),
            "usage_includes_superseded_generation_spend": True,
        },
    }
    invalidation_counts = dict(Counter(item["reason"] for item in invalidated))
    audit_packet = {
        "schema_version": 1,
        "migration_protocol_version": MIGRATION_PROTOCOL_VERSION,
        "created_at": utc_now(),
        "superseded_candidate_sha256": state["superseded_candidate_sha256"],
        "new_first_input_digest": new_input_digest,
        "old_completed_contexts": len(old_progress.get("results_by_context") or {}),
        "carried_contexts": len(carried),
        "invalidated_contexts": len(invalidated),
        "invalidation_counts": invalidation_counts,
        "invalidated": invalidated,
        "old_candidate_contexts": len(old_expected),
        "new_candidate_contexts": len(new_expected),
        "old_k3_candidate_contexts": sum(
            value["exposure"] == TARGET_EXPOSURE for value in old_expected.values()
        ),
        "new_k3_candidate_contexts": sum(
            value["exposure"] == TARGET_EXPOSURE for value in new_expected.values()
        ),
        "corrected_k3_search_contexts": len(new_k3_contexts),
    }
    ledger_packet = {
        "schema_version": 1,
        "updated_at": utc_now(),
        "pending_count": 0,
        "pending": [],
        "events": [
            {
                "event": "generation_migration",
                "at": utc_now(),
                "migration_protocol_version": MIGRATION_PROTOCOL_VERSION,
                "superseded_ledger_sha256": next(
                    (
                        record["sha256"]
                        for record in _required_json(
                            archive / "archive_manifest.json"
                        ).get("artifacts", [])
                        if record["name"] == "screening_problem_ledger.json"
                    ),
                    None,
                ),
                "superseded_pending_problems": len(
                    old_progress.get("deferred_screening_problems") or []
                ),
                "reason": (
                    "Superseded-generation deferred units were invalidated and will be "
                    "reconstructed from missing contexts under the resealed generation."
                ),
            }
        ],
    }
    validation_packet = dual_screening.validation_status_packet(
        candidate_packet,
        ("chatgpt",),
        completed=False,
    )

    staged = {
        "candidates.json": candidate_packet,
        "out_of_window_records.json": {
            "count": len(out_of_window),
            "records": out_of_window,
        },
        "chatgpt_first_screen_progress.json": progress_packet,
        "screening_problem_ledger.json": ledger_packet,
        "validation_status.json": validation_packet,
        "r1_generation_migration_audit.json": audit_packet,
    }
    artifact_records = []
    for name, packet in staged.items():
        path = staging / name
        monthly.save_json(path, packet)
        artifact_records.append(
            {
                "name": name,
                "sha256": monthly.file_sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    manifest = {
        "schema_version": 1,
        "migration_protocol_version": MIGRATION_PROTOCOL_VERSION,
        "created_at": utc_now(),
        "artifacts": artifact_records,
        "counts": audit_packet,
    }
    monthly.save_json(commit_manifest_path, manifest)
    state.update(
        {
            "status": "ready_to_commit",
            "ready_to_commit_at": utc_now(),
            "commit_manifest_sha256": monthly.file_sha256(commit_manifest_path),
            "migration_counts": {
                "old_candidate_pmids": old_candidates.get("candidate_pmids"),
                "new_candidate_pmids": len(candidate_records),
                "old_candidate_contexts": len(old_expected),
                "new_candidate_contexts": len(new_expected),
                "carried_r1_contexts": len(carried),
                "invalidated_r1_contexts": len(invalidated),
                "invalidation_counts": invalidation_counts,
            },
        }
    )
    monthly.save_json(output_dir / STATE_NAME, state)
    return manifest


def _atomic_copy(source: Path, target: Path) -> None:
    temporary = target.with_suffix(target.suffix + ".generation-migration.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def _commit_generation(
    output_dir: Path,
    state: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    if state.get("status") == "resealed":
        monthly.require_candidate_generation_ready(output_dir)
        return state
    if state.get("status") != "ready_to_commit":
        raise RuntimeError("Candidate generation is not ready for commit")
    staging = output_dir / STAGING_NAME
    for record in manifest.get("artifacts", []):
        source = staging / str(record.get("name") or "")
        if (
            not source.is_file()
            or monthly.file_sha256(source) != record.get("sha256")
            or source.stat().st_size != record.get("bytes")
        ):
            raise RuntimeError(f"Staged migration artifact failed validation: {source}")
    for record in manifest["artifacts"]:
        _atomic_copy(staging / record["name"], output_dir / record["name"])
    for record in manifest["artifacts"]:
        target = output_dir / record["name"]
        if monthly.file_sha256(target) != record["sha256"]:
            raise RuntimeError(f"Committed migration artifact failed validation: {target}")

    candidates = _required_json(output_dir / "candidates.json")
    monthly._validate_current_revision_search_scope(candidates)
    progress = _required_json(output_dir / "chatgpt_first_screen_progress.json")
    if progress.get("input_digest") != dual_screening.first_input_digest(
        candidates, dual_screening.CHATGPT_FIRST_MODEL
    ):
        raise RuntimeError("Migrated R1 checkpoint is not bound to the resealed candidates")
    state.update(
        {
            "status": "resealed",
            "resealed_at": utc_now(),
            "candidate_sha256": monthly.file_sha256(
                output_dir / "candidates.json"
            ),
            "progress_sha256": monthly.file_sha256(
                output_dir / "chatgpt_first_screen_progress.json"
            ),
            "vocabulary_digest": candidates.get("vocabulary_digest"),
            "first_input_digest": progress.get("input_digest"),
        }
    )
    monthly.save_json(output_dir / STATE_NAME, state)
    monthly.require_candidate_generation_ready(output_dir)
    return state


def execute(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    state_path = output_dir / STATE_NAME
    state = _required_json(state_path)
    if state.get("status") == "resealed":
        monthly.require_candidate_generation_ready(output_dir)
        return state
    archive, current_terms, _legacy_terms = _verify_prepared_inputs(
        output_dir, state
    )
    search_packet = _run_corrected_searches(
        output_dir, state, current_terms
    )
    state = _required_json(state_path)
    manifest = _build_resealed_generation(
        output_dir,
        state,
        archive,
        current_terms,
        search_packet,
    )
    state = _required_json(state_path)
    return _commit_generation(output_dir, state, manifest)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=(
            monthly.DATA_DIR
            / "monthly_updates"
            / "all_history_through_2026-08-29"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    packet = prepare(args.output_dir) if args.prepare else execute(args.output_dir)
    print(json.dumps(packet, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
