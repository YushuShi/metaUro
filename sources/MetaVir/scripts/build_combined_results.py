#!/usr/bin/env python3
"""Build the public MetaVir cache from ChatGPT, Claude+Gemini, and Fable review.

``Cached_results`` remains the audited output of the monthly ChatGPT pipeline.
This script creates the separate ``combined`` projection consumed by the web
application.  It reconstructs the selected ChatGPT lineage, applies only safe
Fable 5.1 adjudications, retains later base-cache contexts, and rebuilds every
derived cache analysis.
"""

from __future__ import annotations


import argparse
import copy
import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from scripts import dietary_only_policy as dietary_policy
from scripts.display_eligibility import eligible_fair, eligible_fulltext_display
DEFAULT_BASE_CACHE_ROOT = REPO_ROOT / "Cached_results"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "combined"
DEFAULT_STATIC_ROOT = REPO_ROOT / "static"
DEFAULT_MONTHLY_ROOT = REPO_ROOT / "data" / "monthly_updates"
DEFAULT_PARALLEL_ROOT = (
    REPO_ROOT
    / "data"
    / "claude_gemini_screening"
    / "all_history_through_2026-08-29"
)
DEFAULT_ADJUDICATION_CSV = (
    REPO_ROOT
    / "outputs"
    / "fable51_discrepancy_adjudication_2026-09-03"
    / "fable51Adjudication.csv"
)

FABLE_MODEL = "claude-fable-5-1"
COMBINED_SCHEMA_VERSION = 1
FIELD_ORDER = {
    "SCREENING_DECISION": 0,
    "SAMPLE_SIZE": 1,
    "EFFECT_SIZE": 2,
    "CI_OR_P_VALUE": 3,
}
ESTIMATE_FIELDS = {"EFFECT_SIZE", "CI_OR_P_VALUE"}
PIPELINE_CHOICES = {"CHATGPT", "CLAUDE_GEMINI"}
AUTO_WINNERS = {
    "CHATGPT_CORRECT": "CHATGPT",
    "CLAUDE_GEMINI_CORRECT": "CLAUDE_GEMINI",
}
SCREENING_STATUSES = {"EXCLUDE", "ELIGIBLE_NOT_POOLABLE", "INCLUDE_META"}
ESTIMATE_BUNDLE_KEYS = (
    "effect_size",
    "effect_type",
    "ci_lower",
    "ci_upper",
    "p_value",
    "comparison_type",
    "needs_inversion",
    "cases",
    "ci_confidence_level",
    "wald_inputs_same_estimate",
    "ci_method",
    "p_value_method",
    "p_value_is_exact",
    "p_value_reported_as",
    "ci_source_p_value",
    "wald_derivations",
    "direction_imputed",
    "direction_imputation_reason",
    "direction_original_comparison",
)
REQUIRED_CUSTOM_ESTIMATE_KEYS = {
    "effect_size",
    "effect_type",
    "ci_lower",
    "ci_upper",
    "p_value",
    "comparison_type",
    "needs_inversion",
    "cases",
}
ESTIMATE_EVIDENCE_KEYS = (
    "effect_size",
    "confidence_interval",
    "p_value",
    "comparison",
    "cases",
)
GENERATOR_FILES = {
    "build_combined_results_sha256": Path(__file__).resolve(),
    "current_comparison_adapter_sha256": REPO_ROOT / "scripts/current_comparison_adapter.py",
    "key_comparison_policy_sha256": REPO_ROOT / "scripts/key_comparison_policy.py",
    "human_manual_review_sha256": REPO_ROOT / "scripts/human_manual_review.py",
    "curator_dedup_sha256": REPO_ROOT / "scripts/curator_dedup.py",
    "meta_analysis_sha256": REPO_ROOT / "meta_analysis.py",
    "effect_measure_sha256": REPO_ROOT / "effect_measure.py",
    "monthly_evidence_update_sha256": (
        REPO_ROOT / "scripts" / "monthly_evidence_update.py"
    ),
    "dual_screening_sha256": REPO_ROOT / "scripts" / "dual_screening.py",
}


def _load_project_modules():
    # Import after constants so ``--help`` remains fast and import failures say
    # which project dependencies are missing.
    import meta_analysis
    from scripts import monthly_evidence_update

    return meta_analysis, monthly_evidence_update


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def load_result_map(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    packet = load_json_object(path)
    rows = packet.get("results")
    if not isinstance(rows, list):
        raise ValueError(f"Expected a results array in {path}")
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"Non-object result in {path}")
        context_id = str(row.get("context_id") or "")
        if not context_id:
            raise ValueError(f"Result without context_id in {path}")
        if context_id in mapped:
            raise ValueError(f"Duplicate context_id {context_id} in {path}")
        mapped[context_id] = row
    return packet, mapped


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_digest(value: Any) -> str:
    material = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def safe_component(value: Any) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", str(value).lower())).strip("_")


def strict_bool(value: Any, *, field: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{field} must be the literal true or false, got {value!r}")


def normalized_status(row: dict[str, Any], *, claude_gemini: bool = False) -> str:
    if not claude_gemini:
        return str(row.get("final_status") or "")
    status = str(row.get("final_status") or row.get("full_text_status") or "")
    if status:
        return status
    if row.get("eligibility") == "EXCLUDE":
        return "EXCLUDE"
    return "ELIGIBLE_NOT_POOLABLE" if row.get("needs_full_text") else "INCLUDE_META"


def normalized_pipeline_row(row: dict[str, Any], pipeline: str) -> dict[str, Any]:
    result = copy.deepcopy(row)
    result["final_status"] = normalized_status(
        row, claude_gemini=pipeline == "CLAUDE_GEMINI"
    )
    result["combined_selected_pipeline"] = pipeline
    return result


def load_fable_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {
        "PMID",
        "exposure",
        "cancer",
        "context_id",
        "discrepancy_type",
        "discrepant_field",
        "fable51_verdict",
        "fable51_manual_review_needed",
        "fable51_adjudication_status",
        "title_abstract_mesh_assessment",
        "fable51_evidence_source",
        "fable51_supporting_excerpt",
        "one_sentence_explanation",
        "model_requested",
        "model_reported",
        "source_scope",
    }
    missing = required - set(rows[0] if rows else [])
    if missing:
        raise ValueError(f"Fable adjudication CSV is missing columns: {sorted(missing)}")
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["context_id"], row["discrepant_field"])
        if key in seen:
            raise ValueError(f"Duplicate Fable field adjudication: {key}")
        seen.add(key)
        if row["discrepant_field"] not in FIELD_ORDER:
            raise ValueError(f"Unsupported discrepant field {row['discrepant_field']!r}")
        expected_context = "|".join(
            (row["PMID"].strip(), row["exposure"].strip(), row["cancer"].strip())
        )
        if row["context_id"] != expected_context:
            raise ValueError(
                f"Fable context_id does not match PMID/exposure/cancer: {key}"
            )
        if row["model_requested"].strip() != FABLE_MODEL:
            raise ValueError(
                f"Fable row {key} requested {row['model_requested']!r}, "
                f"expected {FABLE_MODEL!r}"
            )
        if row["model_reported"].strip() != FABLE_MODEL:
            raise ValueError(
                f"Fable row {key} reported {row['model_reported']!r}, "
                f"expected {FABLE_MODEL!r}"
            )
        if row["source_scope"].strip() != "TITLE_ABSTRACT_MESH_ONLY":
            raise ValueError(
                f"Fable row {key} has unsupported source_scope "
                f"{row['source_scope']!r}"
            )
        strict_bool(
            row["fable51_manual_review_needed"],
            field="fable51_manual_review_needed",
        )
    return rows


def load_manual_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": COMBINED_SCHEMA_VERSION, "overrides": []}
    packet = load_json_object(path)
    if packet.get("schema_version") != COMBINED_SCHEMA_VERSION:
        raise ValueError(f"Unsupported manual override schema in {path}")
    overrides = packet.get("overrides")
    if not isinstance(overrides, list):
        raise ValueError("_manual_overrides.json overrides must be an array")
    return packet


def _evidence_payload(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an evidence object")
    if not str(value.get("excerpt") or "").strip():
        raise ValueError(f"{label} requires a supporting excerpt")
    if not str(value.get("source") or "").strip():
        raise ValueError(f"{label} requires a source")
    return value


def _validate_custom_override(
    key: tuple[str, str], override: dict[str, Any]
) -> None:
    field = key[1]
    if "custom_value" not in override:
        raise ValueError(f"CUSTOM manual override {key} requires custom_value")
    if not str(override.get("custom_reason") or "").strip():
        raise ValueError(f"CUSTOM manual override {key} requires custom_reason")

    custom_value = override["custom_value"]
    if field == "SCREENING_DECISION":
        status = str(custom_value or "").upper()
        if status not in SCREENING_STATUSES:
            raise ValueError(
                "Custom screening status must be EXCLUDE, "
                "ELIGIBLE_NOT_POOLABLE, or INCLUDE_META"
            )
        if status == "EXCLUDE" and not str(
            override.get("custom_exclusion_code") or ""
        ).strip():
            raise ValueError(
                f"CUSTOM exclusion override {key} requires custom_exclusion_code"
            )
        return

    evidence = override.get("custom_evidence")
    if not isinstance(evidence, dict):
        raise ValueError(f"CUSTOM manual override {key} requires custom_evidence")
    if field == "SAMPLE_SIZE":
        if (
            isinstance(custom_value, bool)
            or not isinstance(custom_value, int)
            or custom_value <= 0
        ):
            raise ValueError(f"CUSTOM sample size {key} must be a positive integer")
        unexpected = set(evidence) - {"sample_size"}
        if unexpected:
            raise ValueError(
                f"Unsupported custom evidence keys for {field}: {sorted(unexpected)}"
            )
        _evidence_payload(evidence.get("sample_size"), label="sample_size evidence")
        return

    if field not in ESTIMATE_FIELDS:
        raise ValueError(f"Unsupported CUSTOM discrepant field {field!r}")
    if not isinstance(custom_value, dict):
        raise ValueError(f"Custom {field} override must provide a full estimate object")
    missing = REQUIRED_CUSTOM_ESTIMATE_KEYS - set(custom_value)
    if missing:
        raise ValueError(
            f"Custom {field} estimate is missing bundle keys: {sorted(missing)}"
        )
    unexpected = set(custom_value) - set(ESTIMATE_BUNDLE_KEYS)
    if unexpected:
        raise ValueError(
            f"Unsupported custom estimate keys for {field}: {sorted(unexpected)}"
        )
    effect_size = custom_value["effect_size"]
    if (
        isinstance(effect_size, bool)
        or not isinstance(effect_size, (int, float))
        or not math.isfinite(float(effect_size))
        or float(effect_size) <= 0
    ):
        raise ValueError(f"Custom {field} effect_size must be a positive number")
    if not str(custom_value["effect_type"] or "").strip():
        raise ValueError(f"Custom {field} effect_type must be non-empty")
    if not isinstance(custom_value["needs_inversion"], bool):
        raise ValueError(f"Custom {field} needs_inversion must be boolean")
    cases = custom_value["cases"]
    if cases is not None and (
        isinstance(cases, bool)
        or not isinstance(cases, int)
        or cases <= 0
    ):
        raise ValueError(f"Custom {field} cases must be a positive integer or null")

    lower, upper, p_value = (
        custom_value["ci_lower"],
        custom_value["ci_upper"],
        custom_value["p_value"],
    )
    if (lower is None) != (upper is None):
        raise ValueError(f"Custom {field} must provide both CI bounds or neither")
    if lower is not None:
        for label, value in (("ci_lower", lower), ("ci_upper", upper)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"Custom {field} {label} must be a positive number")
        if not float(lower) <= float(effect_size) <= float(upper):
            raise ValueError(f"Custom {field} CI must contain effect_size")
    if p_value is not None and (
        isinstance(p_value, bool)
        or not isinstance(p_value, (int, float))
        or not math.isfinite(float(p_value))
        or not 0 <= float(p_value) <= 1
    ):
        raise ValueError(f"Custom {field} p_value must be between 0 and 1")
    if lower is None and p_value is None:
        raise ValueError(f"Custom {field} requires a CI or p-value")

    unexpected_evidence = set(evidence) - set(ESTIMATE_EVIDENCE_KEYS)
    if unexpected_evidence:
        raise ValueError(
            f"Unsupported custom estimate evidence keys: {sorted(unexpected_evidence)}"
        )
    _evidence_payload(evidence.get("effect_size"), label="effect_size evidence")
    if evidence.get("comparison") is not None:
        _evidence_payload(evidence.get("comparison"), label="comparison evidence")
    if lower is not None:
        _evidence_payload(
            evidence.get("confidence_interval"),
            label="confidence_interval evidence",
        )
    if p_value is not None:
        _evidence_payload(evidence.get("p_value"), label="p_value evidence")
    if evidence.get("cases") is not None:
        _evidence_payload(evidence["cases"], label="cases evidence")


def manual_override_map(
    packet: dict[str, Any],
    fable_rows: Iterable[dict[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = list(fable_rows)
    allowed = {(row["context_id"], row["discrepant_field"]) for row in rows}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for override in packet.get("overrides", []):
        if not isinstance(override, dict):
            raise ValueError("Every manual override must be an object")
        key = (
            str(override.get("context_id") or ""),
            str(override.get("discrepant_field") or ""),
        )
        if key not in allowed:
            raise ValueError(f"Manual override does not match a discrepancy: {key}")
        if key in result:
            raise ValueError(f"Duplicate manual override: {key}")
        pipeline = str(override.get("selected_pipeline") or "").upper()
        if pipeline not in PIPELINE_CHOICES | {"CUSTOM", "NO_CHANGE"}:
            raise ValueError(
                f"Manual override {key} selected_pipeline must be CHATGPT, "
                "CLAUDE_GEMINI, CUSTOM, or NO_CHANGE"
            )
        if pipeline == "CUSTOM":
            _validate_custom_override(key, override)
        result[key] = override

    estimate_fields_by_context: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row["discrepant_field"] in ESTIMATE_FIELDS:
            estimate_fields_by_context[row["context_id"]].add(row["discrepant_field"])
    for context_id, estimate_fields in estimate_fields_by_context.items():
        custom_fields = {
            field
            for field in estimate_fields
            if str(
                result.get((context_id, field), {}).get("selected_pipeline") or ""
            ).upper()
            == "CUSTOM"
        }
        if not custom_fields:
            continue
        if custom_fields != estimate_fields:
            raise ValueError(
                f"CUSTOM estimate overrides for {context_id} must cover every "
                f"discrepant estimate field: {sorted(estimate_fields)}"
            )
        bundle_digests = {
            canonical_digest(
                {
                    "custom_value": result[(context_id, field)]["custom_value"],
                    "custom_evidence": result[(context_id, field)]["custom_evidence"],
                }
            )
            for field in custom_fields
        }
        if len(bundle_digests) != 1:
            raise ValueError(
                f"CUSTOM estimate overrides for {context_id} must use one identical "
                "value-and-evidence bundle"
            )
    return result


def automatic_choice(row: dict[str, str]) -> str | None:
    if row["fable51_adjudication_status"] != "MODEL_ADJUDICATED":
        return None
    if row["title_abstract_mesh_assessment"] != "SUFFICIENT":
        return None
    if strict_bool(
        row["fable51_manual_review_needed"],
        field="fable51_manual_review_needed",
    ):
        return None
    verdict = row["fable51_verdict"]
    if verdict in AUTO_WINNERS:
        if row.get("fable51_evidence_source") not in {"TITLE", "ABSTRACT", "MESH"}:
            return None
        if not str(row.get("fable51_supporting_excerpt") or "").strip():
            return None
        return AUTO_WINNERS[verdict]
    if verdict == "NO_REPORTABLE_DISCREPANCY":
        return "NO_CHANGE"
    return None


def choice_for_row(
    row: dict[str, str],
    overrides: dict[tuple[str, str], dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None]:
    override = overrides.get((row["context_id"], row["discrepant_field"]))
    if override:
        return str(override["selected_pipeline"]).upper(), override
    return automatic_choice(row), None


def copy_evidence_fields(
    target_extraction: dict[str, Any],
    source_extraction: dict[str, Any],
    fields: Iterable[str],
) -> None:
    target_evidence = target_extraction.setdefault("evidence", {})
    source_evidence = source_extraction.get("evidence") or {}
    for field in fields:
        if field in source_evidence:
            target_evidence[field] = copy.deepcopy(source_evidence[field])
        else:
            target_evidence.pop(field, None)


def copy_sample_size(target: dict[str, Any], source: dict[str, Any]) -> None:
    target_extraction = target.setdefault("extraction", {})
    source_extraction = source.get("extraction") or {}
    target_extraction["total_n"] = copy.deepcopy(source_extraction.get("total_n"))
    copy_evidence_fields(target_extraction, source_extraction, ("sample_size",))


def copy_estimate_bundle(target: dict[str, Any], source: dict[str, Any]) -> None:
    target_extraction = target.setdefault("extraction", {})
    source_extraction = source.get("extraction") or {}
    for key in ESTIMATE_BUNDLE_KEYS:
        if key in source_extraction:
            target_extraction[key] = copy.deepcopy(source_extraction[key])
        else:
            target_extraction.pop(key, None)
    copy_evidence_fields(
        target_extraction,
        source_extraction,
        ESTIMATE_EVIDENCE_KEYS,
    )


def apply_custom_value(
    target: dict[str, Any], field: str, override: dict[str, Any]
) -> None:
    extraction = target.setdefault("extraction", {})
    custom_value = override["custom_value"]
    target["combined_selected_pipeline"] = "MANUAL_CUSTOM"
    if field == "SCREENING_DECISION":
        status = str(custom_value or "").upper()
        target["final_status"] = status
        target["full_text_status"] = status
        target["eligibility"] = "EXCLUDE" if status == "EXCLUDE" else "INCLUDE"
        target["needs_full_text"] = status == "ELIGIBLE_NOT_POOLABLE"
        target["reason"] = str(override["custom_reason"]).strip()
        target.pop("exclusion_reason", None)
        if status == "EXCLUDE":
            target["exclusion_code"] = str(
                override["custom_exclusion_code"]
            ).strip()
        else:
            target.pop("exclusion_code", None)
        return
    if field == "SAMPLE_SIZE":
        extraction["total_n"] = custom_value
        evidence = extraction.setdefault("evidence", {})
        evidence.pop("sample_size", None)
        evidence["sample_size"] = copy.deepcopy(
            override["custom_evidence"]["sample_size"]
        )
        return
    for key in ESTIMATE_BUNDLE_KEYS:
        extraction.pop(key, None)
    extraction.update(copy.deepcopy(custom_value))
    evidence = extraction.setdefault("evidence", {})
    for key in ESTIMATE_EVIDENCE_KEYS:
        evidence.pop(key, None)
    evidence.update(copy.deepcopy(override["custom_evidence"]))


def value_for_field(result: dict[str, Any], field: str) -> Any:
    extraction = result.get("extraction") or {}
    if field == "SCREENING_DECISION":
        return result.get("final_status")
    if field == "SAMPLE_SIZE":
        return extraction.get("total_n")
    if field == "EFFECT_SIZE":
        return {
            "effect_size": extraction.get("effect_size"),
            "effect_type": extraction.get("effect_type"),
            "comparison_type": extraction.get("comparison_type"),
        }
    if field == "CI_OR_P_VALUE":
        return {
            "ci_lower": extraction.get("ci_lower"),
            "ci_upper": extraction.get("ci_upper"),
            "p_value": extraction.get("p_value"),
        }
    return extraction.get("effect_type")


def apply_fable_adjudications(
    selected_chatgpt: dict[str, dict[str, Any]],
    claude_gemini: dict[str, dict[str, Any]],
    fable_rows: list[dict[str, str]],
    overrides: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    combined = {
        context_id: normalized_pipeline_row(row, "CHATGPT")
        for context_id, row in selected_chatgpt.items()
    }
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in fable_rows:
        grouped[row["context_id"]].append(row)

    ledger: list[dict[str, Any]] = []
    for context_id in sorted(grouped):
        if context_id not in combined:
            raise ValueError(f"Fable context is absent from selected ChatGPT lineage: {context_id}")
        if context_id not in claude_gemini:
            raise ValueError(f"Fable context is absent from Claude+Gemini results: {context_id}")
        chat = normalized_pipeline_row(selected_chatgpt[context_id], "CHATGPT")
        other = normalized_pipeline_row(claude_gemini[context_id], "CLAUDE_GEMINI")
        result = combined[context_id]
        context_rows = sorted(
            grouped[context_id], key=lambda row: FIELD_ORDER[row["discrepant_field"]]
        )
        resolutions = {
            row["discrepant_field"]: choice_for_row(row, overrides)
            for row in context_rows
        }
        applied_fields: dict[str, str] = {}

        screening_row = next(
            (row for row in context_rows if row["discrepant_field"] == "SCREENING_DECISION"),
            None,
        )
        if screening_row:
            choice, override = resolutions["SCREENING_DECISION"]
            if choice in PIPELINE_CHOICES:
                result = copy.deepcopy(chat if choice == "CHATGPT" else other)
                combined[context_id] = result
                applied_fields["SCREENING_DECISION"] = choice
            elif choice == "CUSTOM":
                apply_custom_value(result, "SCREENING_DECISION", override)
                applied_fields["SCREENING_DECISION"] = "CUSTOM"
            elif choice == "NO_CHANGE":
                applied_fields["SCREENING_DECISION"] = "NO_CHANGE"

        sample_row = next(
            (row for row in context_rows if row["discrepant_field"] == "SAMPLE_SIZE"),
            None,
        )
        if sample_row:
            choice, override = resolutions["SAMPLE_SIZE"]
            if choice in PIPELINE_CHOICES:
                copy_sample_size(result, chat if choice == "CHATGPT" else other)
                applied_fields["SAMPLE_SIZE"] = choice
            elif choice == "CUSTOM":
                apply_custom_value(result, "SAMPLE_SIZE", override)
                applied_fields["SAMPLE_SIZE"] = "CUSTOM"
            elif choice == "NO_CHANGE":
                applied_fields["SAMPLE_SIZE"] = "NO_CHANGE"

        estimate_rows = [
            row for row in context_rows if row["discrepant_field"] in ESTIMATE_FIELDS
        ]
        estimate_applied = False
        estimate_pending_reason = ""
        if estimate_rows:
            estimate_choices = [resolutions[row["discrepant_field"]][0] for row in estimate_rows]
            if any(choice is None for choice in estimate_choices):
                estimate_pending_reason = (
                    "The estimate-dependent fields stay together until every discrepant "
                    "field in the bundle is resolved."
                )
            else:
                pipeline_choices = {
                    choice for choice in estimate_choices if choice in PIPELINE_CHOICES
                }
                custom_overrides = [
                    resolutions[row["discrepant_field"]][1]
                    for row in estimate_rows
                    if resolutions[row["discrepant_field"]][0] == "CUSTOM"
                ]
                if custom_overrides:
                    if len(custom_overrides) != len(estimate_rows):
                        raise ValueError(
                            f"CUSTOM estimate override for {context_id} cannot be "
                            "combined with another estimate resolution"
                        )
                    bundles = {
                        canonical_digest(
                            {
                                "custom_value": override["custom_value"],
                                "custom_evidence": override["custom_evidence"],
                            }
                        )
                        for override in custom_overrides
                    }
                    if len(bundles) != 1:
                        raise ValueError(
                            f"CUSTOM estimate overrides for {context_id} conflict"
                        )
                    apply_custom_value(
                        result,
                        estimate_rows[0]["discrepant_field"],
                        custom_overrides[0],
                    )
                    estimate_applied = True
                    for row in estimate_rows:
                        applied_fields[row["discrepant_field"]] = "CUSTOM"
                elif len(pipeline_choices) > 1:
                    estimate_pending_reason = (
                        "Resolved field winners conflict within one estimate bundle; "
                        "manual reconciliation is required."
                    )
                else:
                    if pipeline_choices:
                        pipeline = next(iter(pipeline_choices))
                        copy_estimate_bundle(result, chat if pipeline == "CHATGPT" else other)
                    estimate_applied = True
                    for row in estimate_rows:
                        choice, _ = resolutions[row["discrepant_field"]]
                        applied_fields[row["discrepant_field"]] = choice or ""
            if not estimate_applied:
                # An affirmed ChatGPT field already equals the provisional
                # coherent ChatGPT bundle, even when a different field in that
                # bundle still needs review. Record that field as resolved
                # without mixing any Claude+Gemini value into the estimate.
                for row in estimate_rows:
                    choice, _ = resolutions[row["discrepant_field"]]
                    if choice in {"CHATGPT", "NO_CHANGE"}:
                        applied_fields[row["discrepant_field"]] = choice

        result["combined_adjudicated_fields"] = dict(sorted(applied_fields.items()))
        for row in context_rows:
            field = row["discrepant_field"]
            choice, override = resolutions[field]
            applied = field in applied_fields
            if applied:
                if choice == "NO_CHANGE":
                    action = "retained_no_reportable_discrepancy"
                elif override:
                    action = f"manual_override_{choice.lower()}"
                else:
                    action = f"applied_{choice.lower()}"
                pending_reason = ""
            else:
                action = "pending_manual_review"
                pending_reason = (
                    estimate_pending_reason if field in ESTIMATE_FIELDS else ""
                ) or str(row.get("fable51_manual_review_reason") or "")
                if row["fable51_verdict"] == "NEITHER_CORRECT":
                    pending_reason = pending_reason or "Neither pipeline supplied a supported value."
            ledger.append(
                {
                    "PMID": row["PMID"],
                    "first_author_last_name": row.get("first_author_last_name", ""),
                    "exposure": row["exposure"],
                    "cancer": row["cancer"],
                    "context_id": context_id,
                    "discrepancy_type": row["discrepancy_type"],
                    "discrepant_field": field,
                    "chatgpt_claim": row.get("chatgpt_claim", ""),
                    "claude_gemini_claim": row.get("claude_gemini_claim", ""),
                    "fable51_verdict": row["fable51_verdict"],
                    "fable51_adjudication_status": row["fable51_adjudication_status"],
                    "title_abstract_mesh_assessment": row["title_abstract_mesh_assessment"],
                    "fable51_manual_review_needed": strict_bool(
                        row["fable51_manual_review_needed"],
                        field="fable51_manual_review_needed",
                    ),
                    "merge_action": action,
                    "combined_value": value_for_field(result, field),
                    "pending_reason": pending_reason,
                    "one_sentence_explanation": row["one_sentence_explanation"],
                    "pubmed_url": row.get("pubmed_url", ""),
                }
            )
    ledger.sort(
        key=lambda row: (
            int(row["PMID"]) if str(row["PMID"]).isdigit() else 10**20,
            row["exposure"].casefold(),
            FIELD_ORDER[row["discrepant_field"]],
        )
    )
    return combined, ledger


def selected_chatgpt_lineage(
    monthly_root: Path,
    parallel_root: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    selected_packet, selected_map = load_result_map(parallel_root / "r1_selected_decisions.json")
    del selected_packet
    packet_by_source: dict[str, dict[str, Any]] = {}
    results_by_source: dict[str, dict[str, dict[str, Any]]] = {}
    for selected in selected_map.values():
        source = str(selected.get("selected_source") or "")
        if not source:
            raise ValueError(f"Selected R1 decision lacks selected_source: {selected.get('context_id')}")
        if source not in results_by_source:
            packet, rows = load_result_map(monthly_root / source / "chatgpt_final_screening.json")
            packet_by_source[source] = packet
            results_by_source[source] = rows
    chatgpt: dict[str, dict[str, Any]] = {}
    for context_id, selected in selected_map.items():
        source = str(selected["selected_source"])
        try:
            chatgpt[context_id] = results_by_source[source][context_id]
        except KeyError as exc:
            raise ValueError(
                f"Selected source {source} has no final ChatGPT row for {context_id}"
            ) from exc
    return chatgpt, selected_map, packet_by_source


def claude_gemini_lineage(parallel_root: Path) -> dict[str, dict[str, Any]]:
    _, r2 = load_result_map(parallel_root / "claude_r2_results.json")
    _, r3 = load_result_map(parallel_root / "gemini_r3_results.json")
    unexpected = set(r3) - set(r2)
    if unexpected:
        raise ValueError(f"Gemini contexts without Claude R2 source: {len(unexpected)}")
    return {context_id: r3.get(context_id, row) for context_id, row in r2.items()}


def study_fingerprint(study: dict[str, Any]) -> str:
    fields = (
        "PMID", "Direct Exposure", "Effect Size", "Lower CI", "Upper CI",
        "Effect Type", "comparison_type",
    )
    return canonical_digest({field: study.get(field) for field in fields})


def base_cache_inventory(base_root: Path) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for path in sorted(base_root.glob("*/prostate_cancer_incidence_true_core.json")):
        payload = load_json_object(path)
        exposure = str(payload.get("exposure") or "").strip()
        if not exposure:
            raise ValueError(f"Base cache lacks exposure: {path}")
        if exposure in inventory:
            raise ValueError(f"Duplicate base cache exposure: {exposure}")
        all_path = path.with_name(path.name.replace("_core.json", "_all.json"))
        if not all_path.exists():
            raise ValueError(f"Missing all-cache partner for {path}")
        inventory[exposure] = {
            "slug": path.parent.name,
            "core_path": path,
            "all_path": all_path,
            "core": payload,
            "all": load_json_object(all_path),
        }
    if not inventory:
        raise ValueError(f"No base cache files found below {base_root}")
    return inventory


def add_base_supplements(
    grouped: dict[str, list[dict[str, Any]]],
    inventory: dict[str, dict[str, Any]],
    selected_contexts: set[str],
    monthly: Any,
) -> int:
    """Retain later monthly cache rows outside the frozen adjudication snapshot."""
    supplements = 0
    seen: set[tuple[str, str]] = set()
    for exposure, entry in inventory.items():
        cancer = str(entry["core"].get("cancer") or "Prostate cancer")
        for original in entry["core"].get("studies", []):
            if original.get("Hierarchy Inherited") is True:
                continue
            direct = str(original.get("Direct Exposure") or exposure)
            context_id = monthly.dual_screening.context_id(
                str(original.get("PMID") or ""), direct, cancer
            )
            if context_id in selected_contexts:
                continue
            row = copy.deepcopy(original)
            row["Direct Exposure"] = direct
            row["Displayed Exposure"] = direct
            row["Hierarchy Inherited"] = False
            row["pool_in_parent_meta_analysis"] = True
            identity = (context_id, str(row.get("Effect ID") or study_fingerprint(row)))
            if identity in seen:
                continue
            seen.add(identity)
            targets = [direct, *monthly.inherited_exposures(direct)]
            for target in dict.fromkeys(targets):
                displayed = copy.deepcopy(row)
                displayed["Displayed Exposure"] = target
                displayed["Hierarchy Inherited"] = target != direct
                displayed["pool_in_parent_meta_analysis"] = target == direct
                grouped[target].append(displayed)
            supplements += 1
    return supplements


def selected_source_for_new_scope(exposure, combined_rows, selected, monthly):
    """Retain source metadata for scopes created by supported unpooled evidence."""
    for context_id, row in combined_rows.items():
        direct = str(row.get("direct_exposure") or row.get("exposure") or "")
        if context_id not in selected:
            continue
        if direct != exposure and exposure not in monthly.inherited_exposures(direct):
            continue
        if (row.get("final_status") == "INCLUDE_META" or eligible_fair(row)
                or eligible_fulltext_display(row) or row.get("key_comparison_pending") is True
                or row.get('provisional_ui_display_only') is True):
            return str(selected[context_id]["selected_source"])
    raise ValueError(f"No selected screening source for visible exposure scope: {exposure}")


def build_grouped_studies(
    combined_rows: dict[str, dict[str, Any]],
    inventory: dict[str, dict[str, Any]],
    selected_contexts: set[str],
    monthly: Any,
) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    from scripts.display_eligibility import eligible_fair, eligible_fulltext_display
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    direct_count = 0
    for context_id in sorted(combined_rows):
        item = combined_rows[context_id]
        if item.get("key_comparison_pending") is True:
            for candidate in item.get("key_comparison_candidates", []):
                if normalized_status(candidate, claude_gemini=candidate.get("comparison_candidate_track") == "CLAUDE_GEMINI") == "EXCLUDE":
                    continue
                direct = str(candidate.get("direct_exposure") or candidate.get("exposure") or "")
                for target in dict.fromkeys([direct, *monthly.inherited_exposures(direct)]):
                    displayed = copy.deepcopy(candidate)
                    displayed.update(exposure=target, direct_exposure=direct, hierarchy_inherited=target != direct,
                                     pool_in_parent_meta_analysis=False)
                    row = monthly.study_row(displayed)
                    track = candidate.get("comparison_candidate_track", "unknown")
                    row.update(display_only=True, policy_poolable=False, manual_poolable=False,
                               pool_in_parent_meta_analysis=False, key_comparison_pending=True,
                               comparison_candidate_track=track,
                               display_only_reason="Current ChatGPT / Claude+Gemini key discrepancy awaits review; this candidate is not pooled.")
                    for field in ("Effect ID", "Record ID"):
                        row[field] = str(row.get(field) or context_id) + ":" + track.lower()
                    grouped[target].append(row)
                direct_count += 1
            continue
        display_only = eligible_fair(item) or eligible_fulltext_display(item) or item.get('provisional_ui_display_only') is True
        if item.get("final_status") != "INCLUDE_META" and not display_only:
            continue
        direct = str(item.get("direct_exposure") or item.get("exposure") or "").strip()
        cancer = str(item.get("cancer") or "").strip()
        if not direct or not cancer:
            raise ValueError(f"Included row lacks direct exposure/cancer: {context_id}")
        extraction = item.get("extraction") or {}
        if not display_only:
            try:
                effect = float(extraction.get("effect_size"))
                lower = float(extraction.get("ci_lower"))
                upper = float(extraction.get("ci_upper"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"INCLUDE_META row lacks numeric estimate: {context_id}") from exc
            if not (effect > 0 and lower > 0 and upper > 0 and lower <= effect <= upper):
                raise ValueError(f"INCLUDE_META row has incoherent estimate: {context_id}")
        targets = [direct, *monthly.inherited_exposures(direct)]
        for target in dict.fromkeys(targets):
            display_item = copy.deepcopy(item)
            display_item.update(
                {
                    "exposure": target,
                    "direct_exposure": direct,
                    "hierarchy_inherited": target != direct,
                    "pool_in_parent_meta_analysis": target == direct,
                }
            )
            from scripts.nonpoolable_status_retention import display_records
            from scripts.human_manual_review import display_records as human_display_records
            display_items = human_display_records(display_item) if display_item.get("human_adjudication") else display_records(display_item)
            grouped[target].extend(monthly.study_row(source_item) for source_item in display_items)
        direct_count += 1
    supplements = add_base_supplements(
        grouped, inventory, selected_contexts, monthly
    )
    for exposure, rows in grouped.items():
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            identity = str(row.get("Effect ID") or study_fingerprint(row))
            if identity in unique and unique[identity] != row:
                raise ValueError(f"Conflicting duplicate effect for {exposure}: {identity}")
            unique[identity] = row
        grouped[exposure] = sorted(
            unique.values(),
            key=lambda row: (
                int(row.get("PMID")) if str(row.get("PMID", "")).isdigit() else 10**20,
                str(row.get("Direct Exposure") or "").casefold(),
                str(row.get("Effect ID") or ""),
            ),
        )
    return dict(grouped), direct_count + supplements, supplements


def reclassify_poolability(
    rows: dict[str, dict[str, Any]], monthly: Any,
) -> list[dict[str, Any]]:
    """Reapply current quantitative policy without changing model eligibility.

    Historical model packets and Fable verdicts remain immutable; this is a
    separately versioned deterministic projection of their selected evidence.
    Explicit curator exclusions and non-poolable custom decisions are retained.
    """
    audit = []
    for context_id, row in rows.items():
        previous = str(row.get("final_status") or "")
        if previous == "EXCLUDE" or not isinstance(row.get("extraction"), dict):
            continue
        extraction = monthly.dual_screening.apply_poolability_policy(row["extraction"])
        comparison_pending = row.get("key_comparison_pending") is True
        nonpoolable_retained = row.get("nonpoolable_status_retained") is True
        preserve_custom = (
            (row.get("combined_adjudicated_fields") or {}).get("SCREENING_DECISION") == "CUSTOM"
            and previous != "INCLUDE_META"
        )
        status = "ELIGIBLE_NOT_POOLABLE" if (comparison_pending or nonpoolable_retained or row.get("astra_preserved_pooling_hold")) else previous if preserve_custom else (
            "INCLUDE_META" if monthly.dual_screening.is_poolable(extraction, row.get("jbi"))
            else "ELIGIBLE_NOT_POOLABLE"
        )
        if status == "INCLUDE_META":
            guarded = monthly.dual_screening.apply_whole_food_subtype_disposition(
                dict(row, final_status=status)
            )
            status = guarded["final_status"]
        issues = monthly.dual_screening.required_quantitative_issues(extraction)
        if (row.get("jbi") or {}).get("grade") not in {"Moderate", "Good"}:
            issues.append("JBI grade must be Moderate or Good")
        if preserve_custom:
            issues.append("curator screening decision retained")
        if row.get("astra_preserved_pooling_hold"):
            issues.append("Existing source clinical/manual pooling gate retained after Astra adjudication")
        if nonpoolable_retained:
            issues.append("Eligible-not-poolable pipeline resolution is retained without pooling")
        if comparison_pending:
            issues.append("Current cross-model key discrepancy awaits review; source candidates remain unpooled")
        if status == "ELIGIBLE_SUBTYPE_REVIEW":
            issues.extend(guarded.get("subtype_review_reasons") or [])
        row["final_status"] = status
        row["poolability_policy"] = {
            "version": 1,
            "source_status": previous,
            "source_reason": row.get("reason"),
            "source_full_text_status": row.get("full_text_status"),
            "remaining_issues": issues,
            "direction_imputed": extraction.get("direction_imputed", False),
        }
        reason = (
            "Poolable with total sample size, a ratio effect and usable CI or exact p-value; optional metadata and reported cases are not required."
            if status == "INCLUDE_META" else
            "Not pooled: " + "; ".join(issues) + "."
        )
        row["reason"] = reason
        row["full_text_status"] = status
        row["missing_key_fields"] = monthly.dual_screening.missing_key_fields(extraction)
        row["invalid_key_fields"] = monthly.dual_screening.invalid_key_fields(extraction)
        row["needs_full_text"] = monthly.dual_screening.needs_full_text(extraction, row.get("jbi"))
        audit.append({
            "PMID": str(row.get("pmid") or ""),
            "first_author_last_name": re.sub(r"\s+[A-Z]{1,6}$", "", str((row.get("authors") or [""])[0])),
            "exposure": row.get("exposure"),
            "context_id": context_id,
            "previous_status": previous,
            "final_status": status,
            "direction_imputed": extraction.get("direction_imputed", False),
            "one_sentence_explanation": reason,
        })
    # Classification follows screening decisions; it cannot lift an existing subtype guard.
    for row in rows.values():
        dietary_policy.apply_record(row, inplace=True)
    return audit


def numeric_frame(studies: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(studies)
    for column in (
        "Effect Size", "Lower CI", "Upper CI", "SE", "Cases", "Sample Size", "Estimated Cases"
    ):
        if column in frame:
            frame[column] = pd.to_numeric(
                frame[column]
                .astype(str)
                .str.replace(",", "", regex=False)
                .str.strip(),
                errors="coerce",
            )
    return frame


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def deterministic_plot_url(url: Any, build_id: str) -> Any:
    if not isinstance(url, str) or not url:
        return url
    return re.sub(r"\?t=\d+$", "", url) + f"?v={build_id[:12]}"


def analysis_fields(
    studies: list[dict[str, Any]],
    disease: str,
    exposure: str,
    screening_stats: dict[str, Any],
    meta_analysis: Any,
    *,
    generate_plots: bool,
    build_id: str,
    canonical_track: str = "combined",
) -> dict[str, Any]:
    stats = copy.deepcopy(screening_stats)
    stats["extracted"] = len(studies)
    stats["canonical_track"] = canonical_track
    if not studies:
        return {
            "studies": [],
            "summary_html": "<p>No poolable studies are present under the current evidence policy.</p>",
            "headline": None,
            "screening_stats": stats,
            "plot_url": None,
            "funnel_plot_url": None,
            "baujat_plot_url": None,
        }
    frame = numeric_frame(studies)
    parent_pooling = (
        frame["pool_in_parent_meta_analysis"].map(meta_analysis.is_parent_pooling_eligible)
        if "pool_in_parent_meta_analysis" in frame
        else pd.Series(True, index=frame.index)
    )
    if not parent_pooling.any():
        return {
            "studies": studies,
            "summary_html": (
                "<p>Eligible studies are listed below. No studies meet the default pooling criteria.</p>"
                if frame.get("display_only", pd.Series(False, index=frame.index)).eq(True).any() else
                "<p>Screened child-exposure evidence is listed for this parent category "
                "but is not pooled in the parent meta-analysis.</p>"
            ),
            "headline": None,
            "screening_stats": stats,
            "plot_url": None,
            "funnel_plot_url": None,
            "baujat_plot_url": None,
        }
    result = meta_analysis.perform_meta_analysis(
        frame,
        disease,
        exposure,
        outcome="Incidence",
        exclude_meta=True,
        df_all=frame,
        screening_stats=stats,
        generate_plots=generate_plots,
        plot_namespace="chatgpt" if canonical_track == "chatgpt" else "",
    )
    if not result.get("success"):
        raise RuntimeError(f"Meta-analysis rebuild failed for {exposure}: {result}")
    return {
        "studies": studies,
        "summary_html": result.get("summary_html"),
        "headline": result.get("headline"),
        "screening_stats": stats,
        "plot_url": deterministic_plot_url(result.get("plot_url"), build_id),
        "funnel_plot_url": deterministic_plot_url(result.get("funnel_plot_url"), build_id),
        "baujat_plot_url": deterministic_plot_url(result.get("baujat_plot_url"), build_id),
    }


def relative_source(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return path.name


def write_json(path: Path, value: Any, *, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=indent, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def transactionally_replace_directories(
    replacements: list[tuple[Path, Path]],
) -> None:
    token = uuid.uuid4().hex
    backups: list[tuple[Path, Path]] = []
    installed: list[tuple[Path, Path]] = []
    try:
        for staged, target in replacements:
            if not staged.is_dir():
                raise ValueError(f"Staged replacement is missing: {staged}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = target.parent / f".{target.name}.combined-backup-{token}"
                if backup.exists():
                    raise RuntimeError(f"Backup path already exists: {backup}")
                target.rename(backup)
                backups.append((backup, target))
        for staged, target in replacements:
            staged.rename(target)
            installed.append((target, staged))
    except Exception:
        for target, _staged in reversed(installed):
            if target.exists():
                shutil.rmtree(target)
        for backup, target in reversed(backups):
            if backup.exists():
                backup.rename(target)
        raise
    else:
        for backup, _target in backups:
            shutil.rmtree(backup)


def readme_text() -> str:
    return """# Combined MetaVir results

This directory is the production evidence cache used by the MetaVir UI.
`Cached_results/` contains only ChatGPT evidence under the current pooling policy. These caches
are rebuilt from the selected ChatGPT lineage, evidence-backed Claude+Gemini
substitutions chosen by `claude-fable-5-1`, and any curator entries in
`_manual_overrides.json`.

The current policy requires total sample size, a ratio effect, and a usable CI
or exact same-estimate p-value; cases and exposure/model metadata are optional.
Unstated comparison directions default to high versus low or yes versus no and
are marked `direction_imputed`. Original model packets remain unchanged.
`_poolability_audit.json` records current poolability and remaining issues.

`_pending_review.json` lists unresolved fields. To resolve one, add an entry to
the `overrides` array in `_manual_overrides.json` with its `context_id`,
`discrepant_field`, and `selected_pipeline` (`CHATGPT`, `CLAUDE_GEMINI`,
`NO_CHANGE`, or `CUSTOM`). Then run:

```bash
python scripts/build_combined_results.py
python scripts/build_summary_plots.py
```

Do not edit the derived exposure cache files directly; the next rebuild will
replace them.

Every `CUSTOM` entry needs `custom_value` and `custom_reason`. A custom sample
size must be a positive integer and include `custom_evidence.sample_size` with
non-empty `excerpt` and `source` fields. A custom screening exclusion also
needs `custom_exclusion_code`. A custom estimate is an atomic bundle: provide
`effect_size`, `effect_type`, `ci_lower`, `ci_upper`, `p_value`,
`comparison_type`, `needs_inversion`, and `cases`, plus evidence for the effect,
comparison, and each supplied CI/p-value. If both estimate discrepancy rows are
listed for a context, add identical CUSTOM bundles for both so no value is mixed
with another pipeline's estimate or evidence.

Automatic pipeline winners must include a verbatim supporting excerpt from the
title, abstract, or MeSH. Compact/provider-filtered answers without that evidence
remain provisional ChatGPT values in `_pending_review.json`.
"""


def build(args: argparse.Namespace) -> dict[str, Any]:
    meta_analysis, monthly = _load_project_modules()
    fable_rows = load_fable_rows(args.adjudication_csv)
    manual_packet = load_manual_overrides(args.output_root / "_manual_overrides.json")
    overrides = manual_override_map(manual_packet, fable_rows)

    chatgpt, selected, source_packets = selected_chatgpt_lineage(
        args.monthly_root, args.parallel_root
    )
    claude_gemini = claude_gemini_lineage(args.parallel_root)
    comparison = None
    if not getattr(args, "historical_comparison", False):
        from scripts import current_comparison_adapter
        comparison = current_comparison_adapter.prepare(args, monthly)
        combined_rows, ledger = comparison["combined_rows"], comparison["ledger"]
        chatgpt, claude_gemini = comparison["chatgpt"], comparison["claude_gemini"]
    else:
        combined_rows, ledger = apply_fable_adjudications(chatgpt, claude_gemini, fable_rows, overrides)
    from scripts import astra_combined_overlay as astra
    combined_rows, astra_audit = astra.apply_checkpoint(
        combined_rows, {'chatgpt': chatgpt, 'claude_gemini': claude_gemini},
        args.output_root, {key[0] for key in overrides},
    )
    from scripts import astra_fulltext_overlay as fulltext
    combined_rows, fulltext_audit = fulltext.apply_checkpoint(
        combined_rows, {'chatgpt': chatgpt, 'claude_gemini': claude_gemini},
        args.output_root, {key[0] for key in overrides},
    )
    fulltext_resolved = {item['context_id'] for item in fulltext_audit if item['applied']}
    astra_resolved = {item['context_id'] for item in astra_audit if item['applied']}
    for entry in ledger:
        if entry['context_id'] in fulltext_resolved:
            entry.update(merge_action='applied_astra_pmc_fulltext',
                         pending_reason='Remaining estimate review' if combined_rows[entry['context_id']].get('key_comparison_pending') else '',
                         astra_fulltext_adjudication=combined_rows[entry['context_id']]['astra_fulltext_adjudication'])
        elif entry['context_id'] in astra_resolved:
            entry.update(merge_action='applied_astra_title_abstract', pending_reason='',
                         astra_adjudication=combined_rows[entry['context_id']]['astra_adjudication'])
    from scripts import nonpoolable_status_retention as retention
    combined_rows, nonpoolable_resolved = retention.apply_records(
        combined_rows, {'chatgpt': chatgpt, 'claude_gemini': claude_gemini}, {key[0] for key in overrides})
    for entry in ledger:
        if entry['context_id'] in nonpoolable_resolved:
            entry.update(merge_action='retained_eligible_not_poolable', pending_reason='',
                         combined_value='ELIGIBLE_NOT_POOLABLE')
    poolability_audit = reclassify_poolability(combined_rows, monthly)
    # Targeted human-review refreshes keep all unrelated saved scientific records.
    # The optional baseline is used only for that invocation, never future discovery.
    if getattr(args, "preserve_canonical_from", None):
        _, combined_rows = load_result_map(args.preserve_canonical_from)
    from scripts import human_manual_review as human
    combined_rows, human_audit = human.apply_checkpoint(combined_rows, args.output_root, {"chatgpt": chatgpt, "claude_gemini": claude_gemini})
    human_contexts = {item["context_id"] for item in human_audit}
    from scripts import curator_dedup as curator
    combined_rows, curator_audit = curator.apply_checkpoint(combined_rows, args.output_root, {"chatgpt": chatgpt, "claude_gemini": claude_gemini})
    curator_contexts = {item["context_id"] for item in curator_audit}
    for entry in poolability_audit:
        actual = combined_rows.get(entry["context_id"])
        if actual:
            entry.update(final_status=actual["final_status"],
                         one_sentence_explanation=actual.get("reason"),
                         direction_imputed=(actual.get("extraction") or {}).get("direction_imputed", False))
            if actual.get("human_adjudication"):
                entry["decision_authority"] = "human_manual_review"
            if actual.get("curator_deduplication"):
                entry["decision_authority"] = "human_curator_deduplication"
    for entry in ledger:
        if entry["context_id"] in human_contexts:
            entry.update(merge_action="applied_human_manual_review", pending_reason="",
                         manual_final_precedence=True)
        if entry["context_id"] in curator_contexts:
            entry.update(merge_action="applied_curator_deduplication", pending_reason="",
                         manual_final_precedence=True)
    if comparison is not None:
        for entry in comparison["comparison_rows"]:
            if entry["context_id"] in human_contexts:
                entry.update(remaining_review=False, combined_action="applied_human_manual_review")
            if entry["context_id"] in curator_contexts:
                entry.update(remaining_review=False, combined_action="applied_curator_deduplication")
    from scripts import provisional_ui_overlay as provisional
    ui_packet, provisional_audit = provisional.effective_packet({'results':list(combined_rows.values())},
        {'chatgpt':chatgpt,'claude_gemini':claude_gemini},args.output_root,{key[0] for key in overrides})
    ui_packet = curator.protect_ui_packet(ui_packet, args.output_root)
    ui_records = {row['context_id']:row for row in ui_packet['results']}
    for entry in ledger:
        entry["combined_value"] = value_for_field(
            combined_rows[entry["context_id"]], entry["discrepant_field"]
        )
    inventory = base_cache_inventory(args.base_cache_root)
    grouped, direct_study_count, supplement_count = build_grouped_studies(
        ui_records, inventory, set(selected), monthly
    )

    if getattr(args, "preserve_canonical_from", None):
        refreshed_contexts = set(getattr(args, "refresh_context", None) or (human_contexts | curator_contexts))
        grouped = human.merge_saved_scope_rows(grouped, args.output_root, refreshed_contexts)
    grouped = curator.filter_grouped(grouped, args.output_root)

    fable_digest = sha256_file(args.adjudication_csv)
    manual_digest = canonical_digest(manual_packet)
    selected_digest = sha256_file(args.parallel_root / "r1_selected_decisions.json")
    chatgpt_lineage_digest = canonical_digest(chatgpt)
    claude_gemini_lineage_digest = canonical_digest(claude_gemini)
    combined_rows_digest = canonical_digest(combined_rows)
    projected_studies_digest = canonical_digest(grouped)
    generator_digests = {
        name: sha256_file(path) for name, path in GENERATOR_FILES.items()
    }
    build_id = canonical_digest(
        {
            "schema_version": COMBINED_SCHEMA_VERSION,
            "fable": fable_digest,
            "manual": manual_digest,
            "selected": selected_digest,
            "chatgpt_lineage": chatgpt_lineage_digest,
            "claude_gemini_lineage": claude_gemini_lineage_digest,
            "combined_rows": combined_rows_digest,
            "projected_studies": projected_studies_digest,
            "generator_code": generator_digests,
        }
    )
    status_counts = Counter(
        str(row.get("final_status") or "") for row in combined_rows.values()
    )
    action_counts = Counter(row["merge_action"] for row in ledger)
    pending = [row for row in ledger if row["merge_action"] == "pending_manual_review"]
    pending_contexts = {row["context_id"] for row in pending}
    applied_contexts = {
        row["context_id"]
        for row in ledger
        if row["merge_action"].startswith(("applied_", "manual_override_"))
        or row["merge_action"] == "retained_no_reportable_discrepancy"
    }
    combined_screening_stats = {
        "total_fetched": len(
            {str(row.get("pmid") or "") for row in combined_rows.values()}
        ),
        "after_prefilter": len(combined_rows),
        "prefilter_skip": {},
        "llm_screened_in": (
            status_counts["INCLUDE_META"] + status_counts["ELIGIBLE_NOT_POOLABLE"]
        ),
        "llm_screened_out": status_counts["EXCLUDE"],
        "consensus_bypassed": 0,
        "fable_applied_fields": sum(
            count
            for action, count in action_counts.items()
            if action.startswith(("applied_", "manual_override_"))
        ),
        "fable_pending_fields": len(pending),
        "canonical_track": "combined",
    }

    all_exposures = sorted(set(inventory) | set(grouped), key=str.casefold)
    slug_by_exposure = {
        exposure: inventory.get(exposure, {}).get("slug") or safe_component(exposure)
        for exposure in all_exposures
    }
    if len(set(slug_by_exposure.values())) != len(slug_by_exposure):
        raise ValueError("Exposure names collide after filesystem normalization")

    with tempfile.TemporaryDirectory(
        prefix=".combined-build-", dir=args.output_root.parent
    ) as temporary:
        staging_parent = Path(temporary)
        staging_combined = staging_parent / "combined"
        staging_static = staging_parent / "static"
        staging_combined.mkdir()
        staging_static.mkdir()
        if (args.output_root / "_history").exists():
            shutil.copytree(args.output_root / "_history", staging_combined / "_history")
        if comparison is not None and args.output_root.exists():
            archive = staging_combined / "_history" / "before_current_key_comparison_2026-09-05"
            if not archive.exists():
                shutil.copytree(args.output_root, archive, ignore=shutil.ignore_patterns("_history"))
        for slug in slug_by_exposure.values():
            (staging_static / slug).mkdir(parents=True, exist_ok=True)
            if human_audit and (args.static_root / slug).exists():
                shutil.copytree(args.static_root / slug, staging_static / slug, dirs_exist_ok=True)

        with working_directory(staging_parent):
            for exposure in all_exposures:
                entry = inventory.get(exposure)
                studies = grouped.get(exposure, [])
                source_cache = copy.deepcopy(entry["core"] if entry else {})
                rebuilt = analysis_fields(
                    studies,
                    str(source_cache.get("cancer") or "Prostate cancer"),
                    exposure,
                    combined_screening_stats,
                    meta_analysis,
                    generate_plots=not args.skip_plots,
                    build_id=build_id,
                )
                direct_ledger = [row for row in ledger if row["exposure"] == exposure]
                projection = {
                    "schema_version": COMBINED_SCHEMA_VERSION,
                    "build_id": build_id,
                    "base_track": "chatgpt",
                    "adjudicator": FABLE_MODEL,
                    "applied_field_count": sum(
                        row["merge_action"].startswith(("applied_", "manual_override_"))
                        for row in direct_ledger
                    ),
                    "pending_field_count": sum(
                        row["merge_action"] == "pending_manual_review"
                        for row in direct_ledger
                    ),
                }
                for suffix in ("all", "core"):
                    cache = copy.deepcopy(entry[suffix] if entry else {})
                    cache.update(rebuilt)
                    cache.update(
                        {
                            "exposure": exposure,
                            "cancer": str(cache.get("cancer") or "Prostate cancer"),
                            "outcome": "Incidence",
                            "canonical_track": "combined",
                            "combined_projection": projection,
                        }
                    )
                    if isinstance(cache.get("screening_stats"), dict):
                        cache["screening_stats"]["canonical_track"] = "combined"
                    if not entry:
                        source = selected_source_for_new_scope(exposure, ui_records, selected, monthly)
                        packet = source_packets[source]
                        for key in (
                            "screening_models", "screening_input_digests", "protocol_version"
                        ):
                            if key in packet:
                                target_key = "screening_protocol_version" if key == "protocol_version" else key
                                cache[target_key] = copy.deepcopy(packet[key])
                    output = (
                        staging_combined
                        / slug_by_exposure[exposure]
                        / f"prostate_cancer_incidence_true_{suffix}.json"
                    )
                    write_json(output, monthly.json_compatible(cache), indent=4)

        manifest = {
            "schema_version": COMBINED_SCHEMA_VERSION,
            "canonical_track": "combined",
            "build_id": build_id,
            "sources": {
                "base_cache": relative_source(args.base_cache_root),
                "selected_chatgpt": relative_source(
                    args.parallel_root / "r1_selected_decisions.json"
                ),
                "claude_r2": relative_source(args.parallel_root / "claude_r2_results.json"),
                "gemini_r3": relative_source(args.parallel_root / "gemini_r3_results.json"),
                "fable51_adjudication": relative_source(args.adjudication_csv),
            },
            "source_digests": {
                "selected_chatgpt_sha256": selected_digest,
                "chatgpt_lineage_sha256": chatgpt_lineage_digest,
                "claude_gemini_lineage_sha256": claude_gemini_lineage_digest,
                "fable51_adjudication_sha256": fable_digest,
                "manual_overrides_sha256": manual_digest,
                "combined_rows_sha256": combined_rows_digest,
                "astra_checkpoint_sha256": sha256_file(args.output_root / astra.CHECKPOINT_NAME) if (args.output_root / astra.CHECKPOINT_NAME).exists() else None,
                "projected_studies_sha256": projected_studies_digest,
            },
            "generator_digests": generator_digests,
            "policy": {
                "poolability": (
                    "Require total N, ratio effect and usable CI or derivable exact p-value; "
                    "cases, exposure units, adjustment/stratum and direct measurement are optional; "
                    "missing comparison direction defaults to high versus low or yes versus no and is marked imputed."
                ),
                "automatic_adjudication": (
                    "Apply only MODEL_ADJUDICATED, SUFFICIENT, manual-review-false "
                    "Fable winners with a verbatim title/abstract/MeSH excerpt; retain "
                    "ChatGPT provisionally for all unresolved fields."
                ),
                "estimate_bundle": (
                    "Effect, measure, CI/p-value, comparison, inversion, cases, and evidence "
                    "move together only after every discrepant estimate field is resolved."
                ),
            },
            "counts": {
                "selected_contexts": len(combined_rows),
                "combined_statuses": dict(sorted(status_counts.items())),
                "fable_field_rows": len(ledger),
                "fable_contexts": len({row["context_id"] for row in ledger}),
                "merge_actions": dict(sorted(action_counts.items())),
                "applied_contexts": len(applied_contexts),
                "pending_fields": len(pending),
                "pending_contexts": len(pending_contexts),
                "manual_overrides": len(overrides),
                "base_supplemental_direct_studies": supplement_count,
                "direct_ui_studies": direct_study_count,
                "displayed_ui_studies": sum(len(rows) for rows in grouped.values()),
                "cache_exposures": len(all_exposures),
                "cache_files": len(all_exposures) * 2,
            },
        }
        if comparison is not None:
            comparison["summary"]["current_pending_review_fields"] = len(pending)
            comparison["summary"]["current_combined_statuses"] = dict(status_counts)
            manifest["current_comparison"] = comparison["summary"]
            manifest["sources"]["effective_current_tracks"] = "outputs/fair_display_2026-09-05/*_poolability.json"
            write_json(staging_combined / "final_screening.json", {
                "schema_version": 1, "policy_version": comparison["summary"]["policy_version"],
                "canonical_track": "combined", "new_gateway_calls": 0,
                "counts": dict(status_counts), "results": list(combined_rows.values()),
            })
            write_json(staging_combined / "_current_comparison.json", comparison["summary"])
            write_json(staging_combined / "_current_discrepancies.json", {"results": comparison["comparison_rows"]})
            write_json(staging_combined / "_nonkey_field_fills.json", {"records": comparison["metadata_fills"]})
            write_json(staging_combined / "_p_value_reconciliation.json", {"records": comparison["p_resolutions"]})
            write_json(staging_combined / "_historical_adjudication_ledger.json", {"records": comparison["historical_ledger"]})
        write_json(staging_combined / "_merge_manifest.json", manifest)
        write_json(staging_combined / "_poolability_audit.json", {
            "policy_version": 1, "records": poolability_audit,
        })
        write_json(
            staging_combined / "_adjudication_ledger.json",
            {"schema_version": COMBINED_SCHEMA_VERSION, "records": ledger},
        )
        write_json(
            staging_combined / "_pending_review.json",
            {
                "schema_version": COMBINED_SCHEMA_VERSION,
                "provisional_pipeline": "CHATGPT",
                "records": pending,
            },
        )
        checkpoint_path = args.output_root / astra.CHECKPOINT_NAME
        if checkpoint_path.exists():
            shutil.copy2(checkpoint_path, staging_combined / astra.CHECKPOINT_NAME)
            write_json(staging_combined / 'astra_application_audit.json', {'version': astra.VERSION, 'records': astra_audit})
            write_json(staging_combined / 'astra_adjudication/review_dispositions.json', astra.review_dispositions(astra_audit, combined_rows))
        fulltext_root = args.output_root / 'astra_fulltext'
        if fulltext_root.exists():
            shutil.copytree(fulltext_root, staging_combined / 'astra_fulltext', dirs_exist_ok=True)
            write_json(staging_combined / 'astra_fulltext/replay_audit.json', fulltext_audit)
        provisional_root = args.output_root / 'provisional_ui'
        if provisional_root.exists():
            shutil.copytree(provisional_root,staging_combined/'provisional_ui',dirs_exist_ok=True)
            provisional.persist(staging_combined,ui_packet,provisional_audit)
        # The specialized aggressive-cancer review owns these independent scopes.
        # A general-incidence review refresh must not erase them.
        if human_audit:
            for path in args.output_root.glob("*/aggressive_prostate_cancer_incidence_true_*.json"):
                target = staging_combined / path.relative_to(args.output_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                preserved = json.loads(path.read_text())
                preserved.setdefault("combined_projection", {})["build_id"] = build_id
                preserved["human_review_preserved_scope"] = {
                    "reason": "Separate aggressive-cancer endpoint evidence is unchanged by this general-incidence review",
                    "source_study_rows_sha256": human.digest(preserved.get("studies", [])),
                }
                write_json(target, preserved, indent=4)
            aggressive_manifest = args.output_root / "_aggressive_prostate_manifest.json"
            if aggressive_manifest.exists():
                shutil.copy2(aggressive_manifest, staging_combined / aggressive_manifest.name)
        human_root = args.output_root / "human_review"
        if human_root.exists():
            shutil.copytree(human_root, staging_combined / "human_review", dirs_exist_ok=True)
            write_json(staging_combined / "human_review/replay_audit.json", human_audit)
        curator_root = args.output_root / "curator_dedup"
        if curator_root.exists():
            shutil.copytree(curator_root, staging_combined / "curator_dedup", dirs_exist_ok=True)
            write_json(staging_combined / "curator_dedup/replay_audit.json", curator_audit)
        write_json(staging_combined / "_manual_overrides.json", manual_packet)
        (staging_combined / "README.md").write_text(readme_text(), encoding="utf-8")

        replacements = [(staging_combined, args.output_root)]
        if not args.skip_plots:
            replacements.extend(
                (staging_static / slug, args.static_root / slug)
                for slug in sorted(slug_by_exposure.values())
            )
        transactionally_replace_directories(replacements)

    if comparison is not None:
        from scripts import current_comparison_adapter
        report_root = current_comparison_adapter.CURRENT_OUTPUT_ROOT
        for name in ("summary", "comparison_rows", "metadata_fills", "p_resolutions"):
            write_json(report_root / (name + ".json"), comparison[name])
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-cache-root", type=Path, default=DEFAULT_BASE_CACHE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--static-root", type=Path, default=DEFAULT_STATIC_ROOT)
    parser.add_argument("--monthly-root", type=Path, default=DEFAULT_MONTHLY_ROOT)
    parser.add_argument("--parallel-root", type=Path, default=DEFAULT_PARALLEL_ROOT)
    parser.add_argument("--adjudication-csv", type=Path, default=DEFAULT_ADJUDICATION_CSV)
    parser.add_argument("--preserve-canonical-from", type=Path, help="Use this saved canonical packet for a targeted evidence refresh, before replaying durable human decisions.")
    parser.add_argument("--refresh-context", action="append", help="With --preserve-canonical-from, regenerate only this context's saved study rows; repeat for each targeted context.")
    parser.add_argument("--historical-comparison", action="store_true", help="Reconstruct the historical Fable snapshot without the current key-only comparison.")
    parser.add_argument(
        "--skip-plots",
        action="store_true",
        help="Do not replace static plot directories (intended for tests only).",
    )
    args = parser.parse_args(argv)
    if args.refresh_context and not args.preserve_canonical_from:
        parser.error("--refresh-context requires --preserve-canonical-from")
    for field in (
        "base_cache_root", "output_root", "static_root", "monthly_root",
        "parallel_root", "adjudication_csv",
    ):
        setattr(args, field, getattr(args, field).resolve())
    return args


def main(argv: list[str] | None = None) -> int:
    manifest = build(parse_args(argv))
    print(json.dumps(manifest["counts"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
