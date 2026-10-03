"""Prepare an included-only exposure reclassification review without ESearch.

This module is intentionally fail-safe: it never removes a source record.
Legacy lineage is recorded rather than treated as a blocker because these are
the immutable included records that must be freshly screened.  Stable
PMID/effect identities still require a target write before source removal.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _effect_identity(study: dict[str, Any]) -> str:
    explicit = (
        study.get("effect_record_id")
        or study.get("Effect ID")
        or study.get("Record ID")
        or study.get("Screening Record ID")
        or study.get("screening_context_id")
    )
    if explicit:
        return str(explicit)
    payload = {
        "pmid": str(study.get("PMID") or ""),
        "direct_exposure": str(
            study.get("direct_exposure")
            or study.get("Direct Exposure")
            or study.get("original_exposure_category")
            or ""
        ),
        "effect_type": study.get("Effect Type"),
        "effect_size": study.get("Effect Size"),
        "lower_ci": study.get("Lower CI"),
        "upper_ci": study.get("Upper CI"),
        "comparison_type": study.get("comparison_type"),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def _lineage_error(
    payload: dict[str, Any],
    expected_protocol: int,
    expected_models: dict[str, str],
) -> str | None:
    protocol = payload.get("screening_protocol_version")
    if protocol != expected_protocol:
        return (
            f"screening protocol {protocol!r} is stale; expected "
            f"{expected_protocol!r}"
        )
    if payload.get("canonical_track") != "chatgpt":
        return "canonical ChatGPT screening lineage is missing"
    models = payload.get("screening_models")
    if models != expected_models:
        return (
            f"screening models {models!r} are stale or incomplete; expected "
            f"{expected_models!r}"
        )
    return None


def prepare_review(
    *,
    repo_root: Path,
    cache_dir: Path | list[Path] | tuple[Path, ...],
    manifest_path: Path,
    output_path: Path,
    expected_protocol: int,
    expected_models: dict[str, str],
) -> dict[str, Any]:
    """Write a no-ESearch review packet for included narrowed-source records."""
    manifest = _load_json(manifest_path, {})
    targets = manifest.get("reclassification_targets")
    if not isinstance(targets, dict) or not targets:
        raise ValueError("Revision manifest has no reclassification_targets")
    review_records = []
    lineage_warnings = []
    seen_sources = set()
    cache_roots = (
        [cache_dir] if isinstance(cache_dir, Path) else [Path(value) for value in cache_dir]
    )
    cache_paths = sorted(
        {
            path
            for root in cache_roots
            if root.exists()
            for path in root.glob("**/*.json")
        }
    )
    for source_exposure, target_values in targets.items():
        if not isinstance(target_values, list) or not target_values:
            raise ValueError(
                f"Reclassification source {source_exposure!r} has no targets"
            )
        source_slug = _slug(source_exposure)
        for path in cache_paths:
            payload = _load_json(path, {})
            if not isinstance(payload, dict):
                continue
            payload_exposure = str(
                payload.get("exposure")
                or payload.get("batch_exposure")
                or ""
            )
            path_matches = path.parent.name.casefold() == source_slug.casefold()
            if (
                _normalize(payload_exposure) != _normalize(source_exposure)
                and not path_matches
            ):
                continue
            studies = [
                study
                for study in payload.get("studies") or []
                if isinstance(study, dict)
                and str(study.get("meta_analysis_status") or "").upper()
                != "EXCLUDE"
            ]
            if not studies:
                continue
            seen_sources.add(_normalize(source_exposure))
            error = _lineage_error(
                payload, expected_protocol, expected_models
            )
            if error:
                lineage_warnings.append(
                    {
                        "source_exposure": source_exposure,
                        "cache_path": str(path.relative_to(repo_root)),
                        "reason": error,
                    }
                )
            for study in studies:
                pmid = str(study.get("PMID") or "").strip()
                if not pmid or pmid.casefold() in {"none", "nan"}:
                    continue
                effect_id = _effect_identity(study)
                review_records.append(
                    {
                        "review_id": hashlib.sha256(
                            (
                                f"{path.relative_to(repo_root)}|{pmid}|"
                                f"{effect_id}|{source_exposure}"
                            ).encode("utf-8")
                        ).hexdigest()[:24],
                        "pmid": pmid,
                        "effect_record_id": effect_id,
                        "source_exposure": source_exposure,
                        "target_options": list(target_values),
                        "cache_path": str(path.relative_to(repo_root)),
                        "legacy_source_lineage": {
                            "canonical_track": payload.get("canonical_track"),
                            "screening_protocol_version": payload.get(
                                "screening_protocol_version"
                            ),
                            "screening_models": payload.get("screening_models"),
                            "is_current": error is None,
                            "warning": error,
                        },
                        "source_record_sha256": hashlib.sha256(
                            json.dumps(
                                study,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                default=str,
                            ).encode("utf-8")
                        ).hexdigest(),
                        "decision": "PENDING_TARGET_CLASSIFICATION",
                        "source_removal_allowed": False,
                        "reason": (
                            "Included source record requires target classification; "
                            "retain it until the same PMID/effect is accepted in a "
                            "declared target."
                        ),
                    }
                )
    packet = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "manifest_path": str(manifest_path.relative_to(repo_root)),
        "search_performed": False,
        "model_calls_performed": False,
        "review_scope": "included source-cache records only",
        "expected_screening_protocol_version": expected_protocol,
        "expected_screening_models": expected_models,
        "legacy_lineage_warnings": lineage_warnings,
        "transaction_rule": (
            "A source record may be removed only in the same committed "
            "transaction after the identical PMID/effect is accepted and "
            "written to a declared target; ambiguous or failed records remain "
            "in source for adjudication."
        ),
        "identified_source_exposures": sorted(seen_sources),
        "records": sorted(
            review_records,
            key=lambda item: (
                item["source_exposure"].casefold(),
                item["pmid"],
                item["effect_record_id"],
            ),
        ),
    }
    packet["count"] = len(packet["records"])
    _save_json(output_path, packet)
    return packet


def accepted_transaction_plan(
    review_packet: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Validate target acceptances; never infer permission to remove a source."""
    actions = []
    pending = []
    for record in review_packet.get("records") or []:
        decision = decisions.get(record["review_id"])
        if not isinstance(decision, dict):
            pending.append(
                {
                    **record,
                    "reason": "No target acceptance was supplied; retain source.",
                }
            )
            continue
        target = str(decision.get("accepted_target") or "")
        accepted_pmid = str(decision.get("pmid") or "")
        accepted_effect = str(decision.get("effect_record_id") or "")
        if (
            target not in record["target_options"]
            or accepted_pmid != record["pmid"]
            or accepted_effect != record["effect_record_id"]
        ):
            pending.append(
                {
                    **record,
                    "reason": (
                        "Target acceptance did not match the declared target and "
                        "same PMID/effect identity; retain source for adjudication."
                    ),
                }
            )
            continue
        actions.append(
            {
                **record,
                "accepted_target": target,
                "source_removal_allowed": True,
                "transaction_order": [
                    "write_and_validate_target",
                    "remove_source",
                    "commit_both_or_rollback_both",
                ],
                "reason": str(decision.get("reason") or "").strip(),
            }
        )
    return {
        "schema_version": 1,
        "actions": actions,
        "pending_adjudication": pending,
        "source_removal_count": len(actions),
    }
