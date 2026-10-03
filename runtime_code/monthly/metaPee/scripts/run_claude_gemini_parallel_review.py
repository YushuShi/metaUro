#!/usr/bin/env python3
"""Run the isolated Claude-R2 / Gemini-R3 validation track for MetaPee.

This driver deliberately does not mutate any ChatGPT, DeepSeek, cache, or UI
artifact.  It reconstructs one deterministic R1 lineage from the immutable
screening generations through 2026-08-29, sends only R1-pass contexts to Claude
Opus 5, applies the existing deterministic Wald completion, and sends only the
explicit post-Wald gate to Gemini 3.7 Flash with verified PMC full text.

The command is safe to interrupt.  Model responses, gateway receipts, results,
and operational problems are atomically checkpointed under OUTPUT_DIR.
"""

from __future__ import annotations

import argparse
import copy
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

from openai import OpenAI


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import dual_screening  # noqa: E402
from scripts import monthly_evidence_update  # noqa: E402
from scripts import parallel_review_security as review_security  # noqa: E402


DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "claude_gemini_screening" / "all_history_through_2026-08-29"
COST_PATH = REPO_ROOT / "ClaudeGeminiCost.txt"
AUTHORIZED_CREDENTIAL_FILE = REPO_ROOT / "mykey.env"

CLAUDE_R2_MODEL = "claude-opus-5"
GEMINI_R3_MODEL = "gemini-3.7-flash"
PROTOCOL_VERSION = "metapee-claude-gemini-parallel-v1"
MODEL_CACHE_VALIDATION_CONTRACT_VERSION = (
    "metapee-parallel-cache-validation-v2-canonical-pmc-url"
)
R3_SOURCE_FAILURE_TYPE = "official_pmc_source_unavailable"
R3_SOURCE_DEFERRAL_DISPOSITION = (
    "decision_free_deferred_after_two_verified_official_pmc_source_failures"
)
PUBLICATION_DATE_END = "2026-08-29"
R2_MAX_CONTEXTS_PER_UNIT = 12
R3_MAX_CONTEXTS_PER_UNIT = 8
PMC_MIN_COMPLETE_TEXT_CHARACTERS = 5000
PMC_MAX_RESPONSE_BYTES = 100 * 1024 * 1024

R3_SOURCE_BINDING_FIELDS = {
    "full_text_characters_reviewed": "characters",
    "pmc_source_format": "source_format",
    "pmc_source_sha256": "sha256",
    "pmc_source_receipt_digest": "receipt_digest",
    "pmc_source_raw_sha256": "raw_sha256",
}

CATALOG_PATHS = (
    DATA_DIR / "synonyms_cache.json",
    DATA_DIR / "additionalExposure.json",
    DATA_DIR / "additionalExposureV2.json",
)

# The order is part of the scientific lineage contract.  Each later all-history
# packet replaces older contexts for every exposure it actually searched.  The
# reclassification and bacterial-alias packets overlay exact context IDs only.
R1_SOURCES = (
    {
        "name": "all_history_through_2026-08-22",
        "coverage": "baseline",
    },
    {
        "name": "all_history_through_2026-08-29",
        "coverage": "all_history_exposure_replacement",
    },
    {
        "name": "vitamin_b6_plp_diet_v2_all_history_through_2026-08-29",
        "coverage": "all_history_exposure_replacement",
    },
    {
        "name": "included_reclassification_2026-08-29",
        "coverage": "context_overlay",
    },
    {
        "name": "bacterial_alias_delta_through_2026-08-29",
        "coverage": "context_overlay",
    },
)

_WRITE_LOCK = threading.RLock()
_SOURCE_LOCK_GUARD = threading.Lock()
_SOURCE_LOCKS: dict[str, threading.Lock] = {}
_SAFE_HEADER_NAMES = {
    "x-request-id",
    "request-id",
    "x-litellm-call-id",
    "x-litellm-response-cost",
    "x-litellm-model-id",
    "x-litellm-version",
    "llm-provider",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
}


class OperationalProblem(RuntimeError):
    """A retryable source or local operational problem, never an exclusion."""

    def __init__(
        self,
        message: str,
        *,
        failure_type: str = "model_or_validation_failure",
        billing_status: str = "known_from_completed_or_failed_local_call",
    ) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.billing_status = billing_status


class GatewayBudgetExceeded(OperationalProblem):
    """Cornell rejected a call before returning any model output."""


class OfficialPMCSourceUnavailable(OperationalProblem):
    """No strict official full text survived every supported PMC route."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            failure_type=R3_SOURCE_FAILURE_TYPE,
            billing_status="no_model_call_official_source_retrieval_only",
        )


def _is_gateway_budget_exhausted(error: BaseException | str) -> bool:
    text = str(error).casefold()
    status_code = getattr(error, "status_code", None)
    has_budget_marker = (
        "budget_exceeded" in text or "budget has been exceeded" in text
    )
    has_429_status = status_code == 429 or bool(
        re.search(r"(?:error\s+code\s*:\s*|code['\"\s:=]+)?429\b", text)
    )
    return has_budget_marker and has_429_status


def _problem_record_fields(error: BaseException | str) -> tuple[str, str]:
    if isinstance(error, GatewayBudgetExceeded) or _is_gateway_budget_exhausted(error):
        return (
            "infrastructure_budget_exhausted",
            "pre_response_gateway_rejection_no_model_output",
        )
    return (
        getattr(error, "failure_type", "model_or_validation_failure"),
        getattr(
            error,
            "billing_status",
            "known_from_completed_or_failed_local_call",
        ),
    )


def now() -> str:
    return datetime.now().astimezone().isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, payload: Any) -> None:
    review_security.atomic_write_json(path, payload)


def stable_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_error(error: BaseException | str) -> str:
    return review_security.redact_error(error)


def catalog_exposures() -> list[str]:
    exposures: list[str] = []
    ownership: dict[str, str] = {}
    for path in CATALOG_PATHS:
        packet = load_json(path, None)
        if not isinstance(packet, dict):
            raise RuntimeError(f"Exposure catalog is not a JSON object: {path}")
        for raw_name in packet:
            name = str(raw_name).strip()
            if not name:
                raise RuntimeError(f"Blank exposure name in {path}")
            # MetaPee intentionally carries two exact parent/atomic overlaps
            # (Oleic acid and Omega-9 fatty acids) across catalog files.  They
            # are one exposure each for screening, disposition, and costing.
            if name in ownership:
                continue
            ownership[name] = path.name
            exposures.append(name)
    exposures = sorted(exposures, key=lambda value: (value.casefold(), value))
    if len(exposures) != 449:
        raise RuntimeError(
            f"The current three-catalog union must contain exactly 449 exposures, got {len(exposures)}"
        )
    if "Diet pills" in exposures or "Diet pill" in exposures:
        raise RuntimeError("The removed Diet pills exposure must not re-enter this review")
    return exposures


def _candidate_contexts(article: dict[str, Any]) -> list[dict[str, str]]:
    contexts = article.get("candidate_contexts") or []
    return [item for item in contexts if isinstance(item, dict)]


def _source_scope(candidates: dict[str, Any], results: list[dict[str, Any]]) -> list[str]:
    scope = candidates.get("searched_exposures") or candidates.get("selected_exposures")
    if isinstance(scope, list):
        return sorted({str(value) for value in scope if str(value).strip()}, key=str.casefold)
    return sorted(
        {str(item.get("exposure")) for item in results if str(item.get("exposure") or "")},
        key=str.casefold,
    )


def prepare_r1_lineage(output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    """Create the deterministic R1-pass packet without making network calls."""
    _recover_interrupted_active_dispatch(output_dir)
    exposures = catalog_exposures()
    allowed = set(exposures)
    current_core = set(load_json(CATALOG_PATHS[0], {}))
    selected: dict[str, dict[str, Any]] = {}
    selected_articles: dict[str, dict[str, Any]] = {}
    lineage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_manifests: list[dict[str, Any]] = []

    for precedence, spec in enumerate(R1_SOURCES):
        source_dir = DATA_DIR / "monthly_updates" / spec["name"]
        candidate_path = source_dir / "candidates.json"
        # ``first_screen.json`` is the canonical packet; the parallel track
        # never reads or rewrites a model-specific progress packet.
        first_path = source_dir / "first_screen.json"
        candidates = load_json(candidate_path, None)
        first_packet = load_json(first_path, None)
        if not isinstance(candidates, dict) or not isinstance(first_packet, dict):
            raise RuntimeError(f"Incomplete immutable R1 source: {source_dir}")
        raw_results = first_packet.get("results")
        if not isinstance(raw_results, list):
            raise RuntimeError(f"R1 results are missing: {first_path}")
        results = [
            item
            for item in raw_results
            if isinstance(item, dict) and str(item.get("exposure") or "") in allowed
        ]
        scope = [name for name in _source_scope(candidates, results) if name in allowed]
        scope_provenance = "explicit_searched_or_selected_exposures"
        if spec["name"] == "all_history_through_2026-08-22":
            # The legacy packet recorded only the count (233), not the names.
            # Current core names are the conservative audit envelope; any core
            # additions or changed aliases are explicitly replaced by the later
            # all-history packets before a context is selected.
            scope = sorted(current_core, key=str.casefold)
            scope_provenance = (
                "current_core_catalog_audit_envelope_for_legacy_packet_with_count_only; "
                "later_all_history_scope_replaces_revised_exposures"
            )

        article_by_pmid = {
            str(article.get("pmid") or ""): article
            for article in candidates.get("articles", [])
            if isinstance(article, dict) and str(article.get("pmid") or "")
        }
        if spec["coverage"] == "all_history_exposure_replacement":
            replacement = set(scope)
            for context_id in [
                context_id
                for context_id, record in selected.items()
                if str(record["result"].get("exposure") or "") in replacement
            ]:
                selected.pop(context_id, None)

        for item in results:
            context_id = str(item.get("context_id") or "")
            pmid = str(item.get("pmid") or context_id.split("|", 1)[0])
            if not context_id or pmid not in article_by_pmid:
                raise RuntimeError(
                    f"R1 result lacks matching article metadata in {spec['name']}: {context_id}"
                )
            decision = str(item.get("decision") or "").upper()
            if decision not in {"PASS", "FAIL"}:
                raise RuntimeError(f"Invalid R1 decision for {context_id}: {decision!r}")
            entry = {
                "source": spec["name"],
                "source_precedence": precedence,
                "coverage": spec["coverage"],
                "decision": decision,
                "reason": str(item.get("reason") or ""),
            }
            lineage[context_id].append(entry)
            selected[context_id] = {
                "result": dict(item),
                "source": spec["name"],
                "precedence": precedence,
            }
            selected_articles[context_id] = article_by_pmid[pmid]

        source_manifests.append(
            {
                "source": spec["name"],
                "precedence": precedence,
                "coverage": spec["coverage"],
                "scope_exposures": scope,
                "scope_exposure_count": len(scope),
                "scope_provenance": scope_provenance,
                "current_catalog_results": len(results),
                "candidate_sha256": file_digest(candidate_path),
                "first_screen_sha256": file_digest(first_path),
                "created_at": candidates.get("created_at"),
                "publication_date_start": candidates.get("publication_date_start"),
                "publication_date_end": candidates.get("publication_date_end"),
            }
        )

    conflicts: list[dict[str, Any]] = []
    for context_id, records in sorted(lineage.items()):
        decisions = sorted({record["decision"] for record in records})
        if len(decisions) < 2:
            continue
        winner = selected.get(context_id)
        conflicts.append(
            {
                "context_id": context_id,
                "source_decisions": records,
                "resolution_rule": (
                    "Use the highest-precedence applicable immutable generation; "
                    "all-history packets replace older exposure scope and delta/rescreen "
                    "packets replace only matching contexts."
                ),
                "selected_source": winner["source"] if winner else None,
                "selected_decision": winner["result"].get("decision") if winner else None,
                "selected_reason": winner["result"].get("reason") if winner else None,
            }
        )
    if len(conflicts) != 2:
        raise RuntimeError(f"Expected exactly 2 deliberate R1 conflicts, got {len(conflicts)}")

    covered_exposures = {
        exposure
        for source in source_manifests
        for exposure in source["scope_exposures"]
    }
    if covered_exposures != allowed:
        missing = sorted(allowed - covered_exposures, key=str.casefold)
        unexpected = sorted(covered_exposures - allowed, key=str.casefold)
        raise RuntimeError(
            "R1 source-scope union does not equal the current three-catalog union; "
            f"missing={missing}, unexpected={unexpected}"
        )

    pass_records = {
        context_id: record
        for context_id, record in selected.items()
        if record["result"].get("decision") == "PASS"
    }
    grouped_contexts: dict[str, list[dict[str, str]]] = defaultdict(list)
    article_versions: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for context_id, record in pass_records.items():
        result = record["result"]
        pmid = str(result.get("pmid") or context_id.split("|", 1)[0])
        grouped_contexts[pmid].append(
            {
                "exposure": str(result["exposure"]),
                "cancer": str(result.get("cancer") or "Urinary cancer"),
                "outcome": str(result.get("outcome") or "Incidence"),
            }
        )
        article_versions[pmid].append((record["precedence"], selected_articles[context_id]))

    articles: list[dict[str, Any]] = []
    article_metadata_conflicts: list[dict[str, Any]] = []
    for pmid in sorted(grouped_contexts, key=lambda value: (int(value) if value.isdigit() else 10**20, value)):
        versions = sorted(article_versions[pmid], key=lambda pair: pair[0])
        article = dict(versions[-1][1])
        comparable = {
            stable_digest(
                {
                    "title": value.get("title") or "",
                    "abstract": value.get("abstract") or "",
                    "mesh": value.get("mesh") or [],
                }
            )
            for _, value in versions
        }
        if len(comparable) > 1:
            article_metadata_conflicts.append(
                {
                    "pmid": pmid,
                    "versions": len(versions),
                    "resolution": "highest-precedence source metadata",
                }
            )
        article["candidate_contexts"] = sorted(
            {stable_digest(item): item for item in grouped_contexts[pmid]}.values(),
            key=lambda item: (item["exposure"].casefold(), item["cancer"].casefold()),
        )
        articles.append(article)

    deterministic_created_at = max(
        str(item.get("created_at") or "") for item in source_manifests
    )
    candidate_packet = {
        "schema_version": 1,
        "created_at": deterministic_created_at,
        "generation": PROTOCOL_VERSION,
        "publication_date_start": None,
        "publication_date_end": PUBLICATION_DATE_END,
        "catalog_exposures": exposures,
        "catalog_exposure_count": len(exposures),
        "candidate_contexts": len(pass_records),
        "candidate_pmids": len(articles),
        "articles": articles,
    }
    pass_results: list[dict[str, Any]] = []
    selected_decisions: list[dict[str, Any]] = []
    for context_id, record in sorted(selected.items()):
        result = dict(record["result"])
        result["selected_source"] = record["source"]
        result["source_decision_lineage"] = lineage[context_id]
        selected_decisions.append(result)
        if result.get("decision") == "PASS":
            pass_results.append(result)
    disposition_counts: dict[str, dict[str, Any]] = {}
    selected_by_exposure: dict[str, Counter[str]] = defaultdict(Counter)
    for item in selected_decisions:
        selected_by_exposure[str(item["exposure"])][str(item["decision"])] += 1
    for exposure in exposures:
        counter = selected_by_exposure[exposure]
        passes = int(counter["PASS"])
        failures = int(counter["FAIL"])
        disposition_counts[exposure] = {
            "exposure": exposure,
            "selected_pass": passes,
            "selected_fail": failures,
            "selected_total": passes + failures,
            "disposition": (
                "no_candidate"
                if passes + failures == 0
                else "pass_and_fail"
                if passes and failures
                else "pass_only"
                if passes
                else "fail_only"
            ),
        }
    first_packet = {
        "schema_version": 2,
        "created_at": deterministic_created_at,
        "track": "chatgpt_r1_source_lineage",
        "model": "existing_chatgpt_r1_artifacts",
        "protocol_version": PROTOCOL_VERSION,
        "input_digest": stable_digest(
            {
                "sources": source_manifests,
                "selected": selected_decisions,
                "candidate_digest": stable_digest(candidate_packet),
            }
        ),
        "pass": len(pass_results),
        "fail": sum(item.get("decision") == "FAIL" for item in selected_decisions),
        "results": pass_results,
    }
    precedence_manifest = {
        "schema_version": 1,
        "created_at": deterministic_created_at,
        "protocol_version": PROTOCOL_VERSION,
        "catalog_files": [
            {"path": str(path.relative_to(REPO_ROOT)), "sha256": file_digest(path)}
            for path in CATALOG_PATHS
        ],
        "catalog_exposure_count": len(exposures),
        "sources": source_manifests,
        "selected_contexts": len(selected_decisions),
        "selected_pass": len(pass_results),
        "selected_fail": len(selected_decisions) - len(pass_results),
        "decision_conflict_count": len(conflicts),
        "decision_conflicts": conflicts,
        "article_metadata_conflicts": article_metadata_conflicts,
        "parallel_reviewer_transport": {
            "api_style": "chat_completions",
            "schema_transport": "prompt_embedded_schema_with_strict_local_validation",
            "response_format_omitted": True,
            "reason": (
                "Cornell Bedrock Claude Opus 5 rejects Responses text.format; "
                "Chat Completions preserves provider compatibility."
            ),
        },
        "source_scope_union_exposures": len(covered_exposures),
        "source_scope_union_matches_current_catalogs": True,
        "exposure_disposition_summary": dict(
            Counter(item["disposition"] for item in disposition_counts.values())
        ),
        "exposure_dispositions": list(disposition_counts.values()),
        "lineage_policy": (
            "All-history packets replace prior contexts for searched exposure scope; "
            "incremental, reclassification, and bacterial-delta packets overlay only "
            "their explicit context IDs. Highest precedence wins a decision conflict."
        ),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    save_json(output_dir / "r1_precedence_manifest.json", precedence_manifest)
    save_json(output_dir / "r1_selected_decisions.json", {
        "schema_version": 1,
        "created_at": deterministic_created_at,
        "results": selected_decisions,
    })
    save_json(output_dir / "r1_pass_candidates.json", candidate_packet)
    save_json(output_dir / "r1_pass_packet.json", first_packet)
    save_json(output_dir / "exposure_coverage.json", {
        "schema_version": 1,
        "created_at": deterministic_created_at,
        "catalog_exposures": len(exposures),
        "source_scope_union_matches_current_catalogs": True,
        "disposition_summary": precedence_manifest["exposure_disposition_summary"],
        "exposures": list(disposition_counts.values()),
    })
    _write_active_dispatch(
        output_dir,
        stage="not_started",
        model="",
        units=[],
    )
    _write_cost_files(output_dir)
    write_outputs_and_state(output_dir, stopped_reason="prepared")
    return precedence_manifest


def _r2_prompt(
    article_batch: list[dict[str, Any]],
    expected: dict[str, dict[str, Any]],
) -> str:
    """Return an R2 prompt whose article input is exactly title/abstract/MeSH."""
    compact: list[dict[str, Any]] = []
    for article in article_batch:
        pmid = str(article.get("pmid") or "")
        contexts = [
            {
                "context_id": context_id,
                "exposure": value["exposure"],
                "cancer": value["cancer"],
                "outcome": "Incidence",
            }
            for context_id, value in expected.items()
            if value["pmid"] == pmid
        ]
        if contexts:
            compact.append(
                {
                    "pmid": pmid,
                    "title": article.get("title") or "",
                    "abstract": str(article.get("abstract") or ""),
                    "mesh": article.get("mesh") or [],
                    "contexts": contexts,
                }
            )
    return dual_screening.SECOND_INSTRUCTIONS + "\n\nARTICLES:\n" + json.dumps(
        compact, ensure_ascii=False, separators=(",", ":")
    )


def r3_gate_reasons(item: dict[str, Any]) -> list[str]:
    """Apply the requested post-Wald gate; ambiguity alone is not a reason."""
    if item.get("eligibility") != "INCLUDE":
        return []
    reasons: list[str] = []
    if (item.get("jbi") or {}).get("grade") == "Fair":
        reasons.append("JBI remains Fair after Claude R2")
    # ``validate_second`` has already normalised every stratum and attempted
    # two-sided Wald completion.  Gate on the union across the primary and all
    # additional gender/measurement strata, never on ambiguity alone.
    issues = sorted(
        set(dual_screening.missing_key_fields_for_item(item))
        | set(dual_screening.invalid_key_fields_for_item(item))
    )
    if issues:
        reasons.append("important quantitative evidence remains missing or invalid: " + ", ".join(issues))
    return reasons


def apply_parallel_r3_gate(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    original = bool(result.get("needs_full_text"))
    reasons = r3_gate_reasons(result)
    result["dual_screening_gate_before_parallel_policy"] = original
    result["parallel_r3_gate_reasons"] = reasons
    result["needs_full_text"] = bool(reasons)
    result["full_text_eligible"] = bool(reasons and result.get("pmcid"))
    result["wald_completed_before_parallel_r3_gate"] = True
    return result


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _work_unit_key(stage: str, context_ids: Iterable[str], source_sha256: str = "") -> str:
    return stable_digest(
        {
            "protocol": PROTOCOL_VERSION,
            "stage": stage,
            "context_ids": sorted(context_ids),
            "source_sha256": source_sha256,
        }
    )


def _configure_client() -> OpenAI:
    values = review_security.read_allowed_credentials(
        AUTHORIZED_CREDENTIAL_FILE,
        label="MetaPee",
        names=(
            *review_security.GATEWAY_KEY_NAMES,
            *review_security.GATEWAY_BASE_URL_NAMES,
        ),
    )
    key = review_security.first_credential(
        values, review_security.GATEWAY_KEY_NAMES
    )
    base_url = review_security.first_credential(
        values, review_security.GATEWAY_BASE_URL_NAMES
    )
    if not key or not base_url:
        raise RuntimeError("Cornell gateway key/base URL are not configured in MetaPee")
    try:
        return OpenAI(
            api_key=key,
            base_url=review_security.validated_cornell_base_url(base_url),
            timeout=600,
        )
    finally:
        values.clear()


def _usage_dict(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)

    def value(name: str, default: Any = 0) -> Any:
        if isinstance(usage, dict):
            return usage.get(name, default)
        return getattr(usage, name, default)

    input_tokens = int(value("input_tokens", value("prompt_tokens", 0)) or 0)
    output_tokens = int(value("output_tokens", value("completion_tokens", 0)) or 0)
    input_details = value("input_tokens_details", value("prompt_tokens_details", {})) or {}
    output_details = value("output_tokens_details", value("completion_tokens_details", {})) or {}
    detail = lambda obj, name: obj.get(name, 0) if isinstance(obj, dict) else getattr(obj, name, 0)
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": int(detail(input_details, "cached_tokens") or 0),
        "output_tokens": output_tokens,
        "reasoning_tokens": int(detail(output_details, "reasoning_tokens") or 0),
    }


def _safe_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        return {}
    return {
        str(key).casefold(): str(value)[:500]
        for key, value in items
        if str(key).casefold() in _SAFE_HEADER_NAMES
    }


def _validated_gateway_cost(headers: dict[str, str]) -> tuple[str | None, str]:
    raw = headers.get("x-litellm-response-cost")
    if raw in (None, ""):
        return None, "unavailable_header_absent"
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None, "unavailable_header_invalid"
    if not value.is_finite() or value < 0:
        return None, "unavailable_header_nonfinite_or_negative"
    return format(value, "f"), "validated_finite_nonnegative_gateway_header"


def _chat_completion_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            if isinstance(item, dict)
            else str(getattr(item, "text", "") or "")
            for item in content
        )
    return str(content or "")


def _cache_attempt(path: Path) -> int:
    match = re.search(r"_([0-9]+)\.json$", path.name)
    return int(match.group(1)) if match else -1


def _verify_cached_raw_output(record: dict[str, Any]) -> str:
    """Return a cached response only when its immutable raw hash still matches."""
    output_text = record.get("output_text")
    if not isinstance(output_text, str) or not output_text:
        raise ValueError("Cached model response has no raw output text")
    expected_sha256 = str(record.get("output_sha256") or "")
    actual_sha256 = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    if expected_sha256 != actual_sha256:
        raise ValueError(
            "Cached raw output SHA-256 does not match its immutable receipt"
        )
    return output_text


def _transition_cached_validation(
    record: dict[str, Any],
    *,
    validation_status: str,
    validation_error: str = "",
    validated_results: list[dict[str, Any]] | None = None,
) -> None:
    """Revalidate cached raw output while retaining its prior audit state."""
    prior_results = record.get("validated_results")
    history = [
        copy.deepcopy(item)
        for item in record.get("validation_history", [])
        if isinstance(item, dict)
    ]
    history.append(
        {
            "history_sequence": len(history) + 1,
            "superseded_at": now(),
            "transition": "cached_attempt_revalidated",
            "validation_status": str(record.get("validation_status") or ""),
            "validation_error": str(record.get("validation_error") or ""),
            "scientific_result_accepted": bool(
                record.get("scientific_result_accepted")
            ),
            "validation_contract_version": str(
                record.get("validation_contract_version") or "legacy"
            ),
            "validated_results_digest": (
                stable_digest(prior_results)
                if isinstance(prior_results, list)
                else ""
            ),
            "output_sha256": str(record.get("output_sha256") or ""),
        }
    )
    record["validation_history"] = history
    record["validation_status"] = validation_status
    record["validation_error"] = (
        safe_error(validation_error) if validation_error else ""
    )
    record["scientific_result_accepted"] = validation_status == "valid"
    record["validation_contract_version"] = (
        MODEL_CACHE_VALIDATION_CONTRACT_VERSION
    )
    if validated_results is None:
        record.pop("validated_results", None)
    else:
        record["validated_results"] = validated_results


def _call_gateway_json(
    client: OpenAI,
    *,
    output_dir: Path,
    stage: str,
    model: str,
    system: str,
    prompt: str,
    context_ids: list[str],
    exposure_weights: dict[str, int],
    validator: Callable[[dict[str, Any]], list[dict[str, Any]]],
    source_sha256: str = "",
) -> list[dict[str, Any]]:
    """Call Cornell Chat Completions with local strict JSON validation.

    Claude Opus 5 on Cornell Bedrock rejects the Responses ``text.format``
    field.  Both reviewer models therefore receive the schema in the prompt;
    ``response_format`` is intentionally omitted and the existing strict local
    validators remain authoritative.
    """
    schema_stage = "second" if stage == "claude_r2" else "full_text"
    schema = dual_screening.response_schema(schema_stage)
    base_unit_key = _work_unit_key(stage, context_ids, source_sha256)
    response_dir = output_dir / "model_response_cache" / stage
    response_dir.mkdir(parents=True, exist_ok=True)
    existing_paths = sorted(
        response_dir.glob(f"{base_unit_key}_*.json"),
        key=lambda path: (_cache_attempt(path), path.name),
        reverse=True,
    )
    last_feedback = ""
    for path in existing_paths:
        cached = load_json(path, {})
        if cached.get("validation_status") == "valid" and isinstance(
            cached.get("validated_results"), list
        ):
            return cached["validated_results"]
        should_revalidate = cached.get("validation_status") == "received" or (
            cached.get("validation_status") == "invalid"
            and cached.get("validation_contract_version")
            != MODEL_CACHE_VALIDATION_CONTRACT_VERSION
        )
        if should_revalidate:
            try:
                output_text = _verify_cached_raw_output(cached)
                payload = dual_screening.json_from_text(output_text)
                results = validator(payload)
            except Exception as exc:
                _transition_cached_validation(
                    cached,
                    validation_status="invalid",
                    validation_error=safe_error(exc),
                )
                with _WRITE_LOCK:
                    save_json(path, cached)
                    if cached.get("receipt_id"):
                        _update_gateway_receipt_validation(
                            output_dir,
                            str(cached["receipt_id"]),
                            validation_status="invalid",
                            scientific_result_accepted=False,
                            validation_error=safe_error(exc),
                        )
                        _write_cost_files(output_dir)
                last_feedback = safe_error(exc)
            else:
                _transition_cached_validation(
                    cached,
                    validation_status="valid",
                    validated_results=results,
                )
                with _WRITE_LOCK:
                    save_json(path, cached)
                    if cached.get("receipt_id"):
                        _update_gateway_receipt_validation(
                            output_dir,
                            str(cached["receipt_id"]),
                            validation_status="valid",
                            scientific_result_accepted=True,
                        )
                        _write_cost_files(output_dir)
                return results
        elif cached.get("validation_status") == "invalid":
            last_feedback = str(cached.get("validation_error") or "")

    for _repair in range(2):
        attempt = len(list(response_dir.glob(f"{base_unit_key}_*.json"))) + 1
        correction = ""
        if last_feedback:
            correction = (
                "\n\nVALIDATION FEEDBACK FROM THE PREVIOUS RESPONSE:\n"
                + last_feedback
                + "\nCorrect every violation and return the complete result set."
            )
        contracted_prompt = (
            prompt
            + correction
            + "\n\nOUTPUT CONTRACT:\nReturn exactly one JSON object matching this JSON Schema. "
            "Include every required property, use null for unavailable values, and do not add prose.\n"
            + json.dumps(schema["schema"], ensure_ascii=False, separators=(",", ":"))
        )
        input_digest = stable_digest(
            {
                "stage": stage,
                "model": model,
                "api_style": "chat_completions",
                "system": system,
                "input": contracted_prompt,
                "schema": schema,
            }
        )
        requested_at = now()
        headers: dict[str, str] = {}
        raw_response: Any = None
        response: Any = None
        try:
            completions = client.chat.completions
            raw_api = getattr(completions, "with_raw_response", None)
            if raw_api is not None:
                raw_response = raw_api.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": contracted_prompt},
                    ],
                    max_tokens=32768 if stage == "claude_r2" else 65536,
                )
                headers = _safe_headers(getattr(raw_response, "headers", None))
                response = raw_response.parse()
            else:  # pragma: no cover - compatibility with older OpenAI clients
                response = completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": contracted_prompt},
                    ],
                    max_tokens=32768 if stage == "claude_r2" else 65536,
                )
        except Exception as exc:
            if _is_gateway_budget_exhausted(exc):
                raise GatewayBudgetExceeded(
                    f"{stage} gateway call failed: {safe_error(exc)}",
                    failure_type="infrastructure_budget_exhausted",
                    billing_status="pre_response_gateway_rejection_no_model_output",
                ) from exc
            if raw_response is not None:
                # A raw response may already be billable even when SDK parsing
                # fails. Preserve any recognized gateway cost and keep the
                # attempt scientifically unaccepted instead of dropping it.
                cost_value, cost_header_status = _validated_gateway_cost(headers)
                receipt_id = stable_digest(
                    {"unit": base_unit_key, "attempt": attempt, "input": input_digest}
                )
                receipt = {
                    "schema_version": 1,
                    "receipt_id": receipt_id,
                    "requested_at": requested_at,
                    "received_at": now(),
                    "stage": stage,
                    "api_style": "chat_completions",
                    "schema_transport": "prompt_embedded_schema_with_strict_local_validation",
                    "response_format_omitted": True,
                    "model_requested": model,
                    "model_reported": "",
                    "work_unit_key": base_unit_key,
                    "attempt": attempt,
                    "context_ids": sorted(context_ids),
                    "exposure_weights": dict(
                        sorted(exposure_weights.items(), key=lambda item: item[0].casefold())
                    ),
                    "input_digest": input_digest,
                    "request_id": str(headers.get("x-request-id") or ""),
                    "usage": {
                        "input_tokens": 0,
                        "cached_input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                    },
                    "token_provenance": "unavailable_after_raw_response_parse_failure",
                    "response_cost_usd": (
                        str(cost_value) if cost_value not in (None, "") else None
                    ),
                    "cost_header_status": cost_header_status,
                    "cost_provenance": (
                        "cornell_gateway_x-litellm-response-cost"
                        if cost_value not in (None, "")
                        else "unavailable_no_local_estimate"
                    ),
                    "safe_gateway_headers": headers,
                    "output_sha256": "",
                    "validation_status": "raw_response_parse_failed",
                    "scientific_result_accepted": False,
                    "validation_error": safe_error(exc),
                }
                cache_path = response_dir / f"{base_unit_key}_{attempt:04d}.json"
                with _WRITE_LOCK:
                    save_json(
                        cache_path,
                        {
                            **receipt,
                            "failure_type": "raw_response_parse_failed",
                            "output_text": "",
                        },
                    )
                    _append_gateway_receipt(output_dir, receipt)
                    _write_cost_files(output_dir)
                last_feedback = safe_error(exc)
                continue
            # No response object exists, but the request may have crossed the
            # gateway boundary before the transport failed. Count the attempt
            # once with unknown billing instead of silently treating it as a
            # zero-cost non-call.
            receipt_id = stable_digest(
                {
                    "unit": base_unit_key,
                    "attempt": attempt,
                    "input": input_digest,
                    "failure": "gateway_transport_without_response",
                }
            )
            receipt = {
                "schema_version": 1,
                "receipt_id": receipt_id,
                "requested_at": requested_at,
                "received_at": now(),
                "stage": stage,
                "api_style": "chat_completions",
                "schema_transport": "prompt_embedded_schema_with_strict_local_validation",
                "response_format_omitted": True,
                "model_requested": model,
                "model_reported": "",
                "work_unit_key": base_unit_key,
                "attempt": attempt,
                "context_ids": sorted(context_ids),
                "exposure_weights": dict(
                    sorted(exposure_weights.items(), key=lambda item: item[0].casefold())
                ),
                "input_digest": input_digest,
                "request_id": "",
                "usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                },
                "token_provenance": "unavailable_after_gateway_transport_failure",
                "response_cost_usd": None,
                "cost_header_status": "unknown_after_gateway_transport_failure",
                "cost_provenance": "unknown_after_gateway_transport_failure",
                "billing_status": "unknown_after_gateway_transport_failure",
                "response_receipt_status": "unavailable_after_gateway_transport_failure",
                "safe_gateway_headers": {},
                "output_sha256": "",
                "validation_status": "gateway_transport_failed",
                "scientific_result_accepted": False,
                "scientific_disposition_created": False,
                "validation_error": safe_error(exc),
            }
            cache_path = response_dir / f"{base_unit_key}_{attempt:04d}.json"
            with _WRITE_LOCK:
                save_json(
                    cache_path,
                    {
                        **receipt,
                        "failure_type": "gateway_transport_without_response",
                        "output_text": "",
                    },
                )
                _append_gateway_receipt(output_dir, receipt)
                _write_cost_files(output_dir)
            raise OperationalProblem(
                f"{stage} gateway call failed: {safe_error(exc)}",
                failure_type="gateway_transport_without_response",
                billing_status="unknown_after_gateway_transport_failure",
            ) from exc

        output_text = _chat_completion_text(response)
        usage = _usage_dict(response)
        cost_value, cost_header_status = _validated_gateway_cost(headers)
        receipt = {
            "schema_version": 1,
            "receipt_id": stable_digest(
                {"unit": base_unit_key, "attempt": attempt, "input": input_digest}
            ),
            "requested_at": requested_at,
            "received_at": now(),
            "stage": stage,
            "api_style": "chat_completions",
            "schema_transport": "prompt_embedded_schema_with_strict_local_validation",
            "response_format_omitted": True,
            "model_requested": model,
            "model_reported": str(getattr(response, "model", "") or ""),
            "work_unit_key": base_unit_key,
            "dispatch_work_unit_key": _work_unit_key(stage, context_ids),
            "attempt": attempt,
            "context_ids": sorted(context_ids),
            "exposure_weights": dict(sorted(exposure_weights.items(), key=lambda item: item[0].casefold())),
            "input_digest": input_digest,
            "request_id": str(getattr(response, "_request_id", "") or headers.get("x-request-id") or ""),
            "usage": usage,
            "token_provenance": (
                "provider_reported_via_cornell_gateway_response_usage"
                if sum(usage.values())
                else "unavailable"
            ),
            "response_cost_usd": str(cost_value) if cost_value not in (None, "") else None,
            "cost_header_status": cost_header_status,
            "cost_provenance": (
                "cornell_gateway_x-litellm-response-cost"
                if cost_value not in (None, "")
                else "unavailable_no_local_estimate"
            ),
            "safe_gateway_headers": headers,
            "output_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
            "validation_status": "received",
            "scientific_result_accepted": False,
            "validation_error": "",
            "validation_contract_version": (
                MODEL_CACHE_VALIDATION_CONTRACT_VERSION
            ),
        }
        cache_path = response_dir / f"{base_unit_key}_{attempt:04d}.json"
        record = {
            **receipt,
            "validation_status": "received",
            "validation_error": "",
            "output_text": output_text,
        }
        with _WRITE_LOCK:
            save_json(cache_path, record)
            _append_gateway_receipt(output_dir, receipt)
            _write_cost_files(output_dir)
        try:
            payload = dual_screening.json_from_text(output_text)
            results = validator(payload)
        except Exception as exc:
            record["validation_status"] = "invalid"
            record["validation_error"] = safe_error(exc)
            record["validation_contract_version"] = (
                MODEL_CACHE_VALIDATION_CONTRACT_VERSION
            )
            with _WRITE_LOCK:
                save_json(cache_path, record)
                _update_gateway_receipt_validation(
                    output_dir,
                    str(receipt["receipt_id"]),
                    validation_status="invalid",
                    scientific_result_accepted=False,
                    validation_error=safe_error(exc),
                )
                _write_cost_files(output_dir)
            last_feedback = safe_error(exc)
            continue
        record["validation_status"] = "valid"
        record["scientific_result_accepted"] = True
        record["validated_results"] = results
        record["validation_contract_version"] = (
            MODEL_CACHE_VALIDATION_CONTRACT_VERSION
        )
        with _WRITE_LOCK:
            save_json(cache_path, record)
            _update_gateway_receipt_validation(
                output_dir,
                str(receipt["receipt_id"]),
                validation_status="valid",
                scientific_result_accepted=True,
            )
            _write_cost_files(output_dir)
        return results
    raise OperationalProblem(
        f"{stage} response failed strict validation twice: {last_feedback}"
    )


def _append_gateway_receipt(output_dir: Path, receipt: dict[str, Any]) -> None:
    path = output_dir / "gateway_receipts.json"
    packet = load_json(path, {})
    receipts = {
        str(item.get("receipt_id")): item
        for item in packet.get("receipts", [])
        if isinstance(item, dict) and item.get("receipt_id")
    }
    receipts[str(receipt["receipt_id"])] = receipt
    save_json(
        path,
        {
            "schema_version": 1,
            "updated_at": now(),
            "receipt_count": len(receipts),
            "receipts": sorted(receipts.values(), key=lambda item: (item["received_at"], item["receipt_id"])),
        },
    )


def _update_gateway_receipt_validation(
    output_dir: Path,
    receipt_id: str,
    *,
    validation_status: str,
    scientific_result_accepted: bool,
    validation_error: str = "",
) -> None:
    """Annotate a received attempt without dropping its usage or cost."""
    path = output_dir / "gateway_receipts.json"
    packet = load_json(path, {})
    changed = False
    for receipt in packet.get("receipts", []):
        if not isinstance(receipt, dict) or receipt.get("receipt_id") != receipt_id:
            continue
        receipt["validation_status"] = validation_status
        receipt["scientific_result_accepted"] = scientific_result_accepted
        receipt["validation_error"] = (
            safe_error(validation_error) if validation_error else ""
        )
        changed = True
        break
    if not changed:
        raise RuntimeError(f"Gateway receipt disappeared before validation: {receipt_id}")
    packet["updated_at"] = now()
    save_json(path, packet)


def _record_problem(
    output_dir: Path,
    *,
    stage: str,
    model: str,
    work_unit_key: str,
    context_ids: list[str],
    error: BaseException | str,
    failure_type: str = "model_or_validation_failure",
    billing_status: str = "known_from_completed_or_failed_local_call",
    metadata: dict[str, Any] | None = None,
) -> None:
    with _WRITE_LOCK:
        path = output_dir / "screening_problem_ledger.json"
        packet = load_json(path, {})
        unresolved = {
            str(item.get("work_unit_key")): item
            for item in packet.get("unresolved", [])
            if isinstance(item, dict) and item.get("work_unit_key")
        }
        previous = unresolved.get(work_unit_key) or {}
        record = {
            "work_unit_key": work_unit_key,
            "stage": stage,
            "model": model,
            "context_ids": sorted(context_ids),
            "pmids": sorted({value.split("|", 1)[0] for value in context_ids}),
            "exposures": sorted({value.split("|", 2)[1] for value in context_ids}, key=str.casefold),
            "attempts": int(previous.get("attempts") or 0) + 1,
            "first_recorded_at": previous.get("first_recorded_at") or now(),
            "last_recorded_at": now(),
            "error": safe_error(error),
            "failure_type": failure_type,
            "billing_status": billing_status,
            "disposition": "unresolved_operational_problem_not_exclusion",
            "scientific_disposition_created": False,
            **(metadata or {}),
        }
        unresolved[work_unit_key] = record
        events = [item for item in packet.get("events", []) if isinstance(item, dict)]
        events.append({"event": "unresolved", "at": now(), **record})
        save_json(
            path,
            {
                "schema_version": 1,
                "updated_at": now(),
                "unresolved_count": len(unresolved),
                "unresolved": sorted(unresolved.values(), key=lambda item: item["work_unit_key"]),
                "events": events,
            },
        )


def _is_official_pmc_source_failure(value: BaseException | str | dict[str, Any]) -> bool:
    if isinstance(value, dict):
        failure_type = str(value.get("failure_type") or "")
        error = str(value.get("error") or "")
    else:
        failure_type = str(getattr(value, "failure_type", "") or "")
        error = str(value)
    return failure_type == R3_SOURCE_FAILURE_TYPE or error.startswith(
        "No strict complete official PMC source for "
    )


def _load_terminal_r3_source_deferrals(
    output_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Load and strictly validate twice-failed decision-free source records."""
    packet = load_json(output_dir / "r3_verified_source_failure_deferrals.json", {})
    rows = packet.get("deferrals", []) if isinstance(packet, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError("R3 verified-source deferral packet is malformed")
    records: dict[str, dict[str, Any]] = {}
    seen_contexts: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("R3 verified-source deferral row is malformed")
        key = str(row.get("work_unit_key") or "")
        context_ids = [str(value) for value in row.get("context_ids", [])]
        if (
            not key
            or key in records
            or row.get("stage") != "gemini_r3"
            or int(row.get("attempts") or 0) < 2
            or not context_ids
            or len(context_ids) != len(set(context_ids))
            or seen_contexts.intersection(context_ids)
            or not _is_official_pmc_source_failure(row)
            or row.get("disposition") != R3_SOURCE_DEFERRAL_DISPOSITION
            or row.get("scientific_disposition_created") is not False
        ):
            raise RuntimeError(
                f"Invalid R3 verified-source deferral record: {key or '<missing>'}"
            )
        seen_contexts.update(context_ids)
        records[key] = row
    expected_count = packet.get("deferral_count") if packet else len(records)
    expected_contexts = packet.get("context_count") if packet else len(seen_contexts)
    if int(expected_count or 0) != len(records) or int(expected_contexts or 0) != len(
        seen_contexts
    ):
        raise RuntimeError("R3 verified-source deferral counts are inconsistent")
    return records


def _save_terminal_r3_source_deferrals(
    output_dir: Path,
    records: dict[str, dict[str, Any]],
) -> None:
    rows = sorted(records.values(), key=lambda item: item["work_unit_key"])
    save_json(
        output_dir / "r3_verified_source_failure_deferrals.json",
        {
            "schema_version": 1,
            "updated_at": now(),
            "deferral_count": len(rows),
            "context_count": sum(len(item["context_ids"]) for item in rows),
            "deferrals": rows,
            "note": (
                "Each record is decision-free and requires at least two failed "
                "strict official-PMC source verifications; no scientific exclusion "
                "or Gemini result is inferred."
            ),
        },
    )


def _defer_twice_failed_r3_source_problem(
    output_dir: Path,
    work_unit_key: str,
) -> bool:
    """Move only a twice-failed strict PMC retrieval out of retryable work."""
    with _WRITE_LOCK:
        terminal = _load_terminal_r3_source_deferrals(output_dir)
        if work_unit_key in terminal:
            return True
        ledger_path = output_dir / "screening_problem_ledger.json"
        packet = load_json(ledger_path, {})
        unresolved = {
            str(item.get("work_unit_key")): item
            for item in packet.get("unresolved", [])
            if isinstance(item, dict) and item.get("work_unit_key")
        }
        record = unresolved.get(work_unit_key)
        if (
            not isinstance(record, dict)
            or record.get("stage") != "gemini_r3"
            or int(record.get("attempts") or 0) < 2
            or not _is_official_pmc_source_failure(record)
        ):
            return False
        deferred = {
            **copy.deepcopy(record),
            "deferred_at": now(),
            "verification_attempts": int(record["attempts"]),
            "disposition": R3_SOURCE_DEFERRAL_DISPOSITION,
            "scientific_disposition_created": False,
        }
        terminal[work_unit_key] = deferred
        # Commit the terminal record first. If the process stops before the
        # ledger update, reconciliation below removes the duplicate safely.
        _save_terminal_r3_source_deferrals(output_dir, terminal)
        unresolved.pop(work_unit_key)
        events = [
            item for item in packet.get("events", []) if isinstance(item, dict)
        ]
        events.append({"event": "decision_free_source_deferred", "at": now(), **deferred})
        save_json(
            ledger_path,
            {
                "schema_version": 1,
                "updated_at": now(),
                "unresolved_count": len(unresolved),
                "unresolved": sorted(
                    unresolved.values(), key=lambda item: item["work_unit_key"]
                ),
                "events": events,
            },
        )
        return True


def _reconcile_terminal_r3_source_deferrals(output_dir: Path) -> int:
    """Resolve a crash-window duplicate after the terminal file was committed."""
    with _WRITE_LOCK:
        terminal = _load_terminal_r3_source_deferrals(output_dir)
        if not terminal:
            return 0
        ledger_path = output_dir / "screening_problem_ledger.json"
        packet = load_json(ledger_path, {})
        unresolved = {
            str(item.get("work_unit_key")): item
            for item in packet.get("unresolved", [])
            if isinstance(item, dict) and item.get("work_unit_key")
        }
        duplicates = [key for key in terminal if key in unresolved]
        if not duplicates:
            return 0
        for key in duplicates:
            unresolved.pop(key)
        events = [
            item for item in packet.get("events", []) if isinstance(item, dict)
        ]
        events.extend(
            {
                "event": "decision_free_source_deferral_reconciled",
                "at": now(),
                **terminal[key],
            }
            for key in duplicates
        )
        save_json(
            ledger_path,
            {
                "schema_version": 1,
                "updated_at": now(),
                "unresolved_count": len(unresolved),
                "unresolved": sorted(
                    unresolved.values(), key=lambda item: item["work_unit_key"]
                ),
                "events": events,
            },
        )
        return len(duplicates)


def _terminal_r3_source_context_ids(
    output_dir: Path,
    *,
    gated_context_ids: Iterable[str] | None = None,
) -> set[str]:
    records = _load_terminal_r3_source_deferrals(output_dir)
    context_ids = {
        str(context_id)
        for record in records.values()
        for context_id in record.get("context_ids", [])
    }
    if gated_context_ids is not None:
        outside = context_ids - {str(value) for value in gated_context_ids}
        if outside:
            raise RuntimeError(
                "R3 source deferrals contain contexts outside the current gate: "
                + ", ".join(sorted(outside)[:5])
            )
    return context_ids


def _migrate_budget_problem_classification(output_dir: Path) -> int:
    """Reclassify legacy 429 hard-budget failures without model calls."""
    with _WRITE_LOCK:
        path = output_dir / "screening_problem_ledger.json"
        packet = load_json(path, {})
        if not isinstance(packet, dict):
            return 0
        changed = 0
        touched = False
        for section in ("unresolved", "events"):
            rows = packet.get(section, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not _is_gateway_budget_exhausted(
                    row.get("error", "")
                ):
                    continue
                failure_type, billing_status = _problem_record_fields(
                    str(row.get("error", ""))
                )
                if (
                    row.get("failure_type") != failure_type
                    or row.get("billing_status") != billing_status
                ):
                    row["failure_type"] = failure_type
                    row["billing_status"] = billing_status
                    changed += 1
                    touched = True
                if row.get("disposition") != "unresolved_operational_problem_not_exclusion":
                    row["disposition"] = "unresolved_operational_problem_not_exclusion"
                    touched = True
                if row.get("scientific_disposition_created") is not False:
                    row["scientific_disposition_created"] = False
                    touched = True
        if touched:
            packet["updated_at"] = now()
            save_json(path, packet)
        blocked = _budget_blocked_stages(output_dir, packet=packet)
        for stage, filename, reason in (
            (
                "claude_r2",
                "claude_r2_progress.json",
                "r2_infrastructure_budget_exhausted_restart_required",
            ),
            (
                "gemini_r3",
                "gemini_r3_progress.json",
                "r3_infrastructure_budget_exhausted_restart_required",
            ),
        ):
            if stage not in blocked:
                continue
            progress_path = output_dir / filename
            progress = load_json(progress_path, {})
            if not isinstance(progress, dict) or not progress:
                continue
            progress["stopped_reason"] = reason
            progress["budget_restart_required"] = True
            save_json(progress_path, progress)
        return changed


def _budget_blocked_stages(
    output_dir: Path, *, packet: dict[str, Any] | None = None
) -> list[str]:
    ledger = packet if packet is not None else load_json(
        output_dir / "screening_problem_ledger.json", {}
    )
    if not isinstance(ledger, dict):
        return []
    blocked = {
        str(item.get("stage") or "")
        for item in ledger.get("unresolved", [])
        if isinstance(item, dict)
        and (
            item.get("failure_type") == "infrastructure_budget_exhausted"
            or _is_gateway_budget_exhausted(item.get("error", ""))
        )
    }
    return sorted(stage for stage in blocked if stage in {"claude_r2", "gemini_r3"})


def _resolve_problem(output_dir: Path, work_unit_key: str) -> None:
    with _WRITE_LOCK:
        path = output_dir / "screening_problem_ledger.json"
        packet = load_json(path, {})
        unresolved = {
            str(item.get("work_unit_key")): item
            for item in packet.get("unresolved", [])
            if isinstance(item, dict) and item.get("work_unit_key")
        }
        record = unresolved.pop(work_unit_key, None)
        if record is None:
            return
        events = [item for item in packet.get("events", []) if isinstance(item, dict)]
        events.append({"event": "resolved", "at": now(), **record})
        save_json(
            path,
            {
                "schema_version": 1,
                "updated_at": now(),
                "unresolved_count": len(unresolved),
                "unresolved": sorted(unresolved.values(), key=lambda item: item["work_unit_key"]),
                "events": events,
            },
        )


def _resolve_problems_by_completed_contexts(
    output_dir: Path,
    *,
    stage: str,
    completed_context_ids: Iterable[str],
) -> None:
    """Resolve old failures even when a resumed run changes batch boundaries."""
    completed = {str(value) for value in completed_context_ids}
    with _WRITE_LOCK:
        path = output_dir / "screening_problem_ledger.json"
        packet = load_json(path, {})
        unresolved = {
            str(item.get("work_unit_key")): item
            for item in packet.get("unresolved", [])
            if isinstance(item, dict) and item.get("work_unit_key")
        }
        resolved: list[dict[str, Any]] = []
        for key, record in list(unresolved.items()):
            context_ids = {str(value) for value in record.get("context_ids", [])}
            if (
                record.get("stage") == stage
                and context_ids
                and context_ids.issubset(completed)
            ):
                resolved.append(unresolved.pop(key))
        if not resolved:
            return
        events = [item for item in packet.get("events", []) if isinstance(item, dict)]
        events.extend({"event": "resolved", "at": now(), **record} for record in resolved)
        save_json(
            path,
            {
                "schema_version": 1,
                "updated_at": now(),
                "unresolved_count": len(unresolved),
                "unresolved": sorted(unresolved.values(), key=lambda item: item["work_unit_key"]),
                "events": events,
            },
        )


def _run_units(
    units: list[Any],
    process: Callable[[Any], tuple[str, list[dict[str, Any]]]],
    on_success: Callable[[str, list[dict[str, Any]]], None],
    on_error: Callable[[Any, Exception], None],
    workers: int,
    on_active: Callable[[list[Any]], None] | None = None,
) -> None:
    """Run a bounded rolling queue with a durable pre-submit call journal."""
    if workers <= 1:
        normal_completion = False
        try:
            for unit in units:
                if on_active is not None:
                    on_active([unit])
                try:
                    key, results = process(unit)
                except Exception as exc:  # KeyboardInterrupt/SystemExit must stop promptly.
                    on_error(unit, exc)
                    if isinstance(exc, GatewayBudgetExceeded):
                        break
                else:
                    on_success(key, results)
            normal_completion = True
        finally:
            # A BaseException can arrive after dispatch. Keep the last unit
            # journaled so resume records unknown response/billing safely.
            if normal_completion and on_active is not None:
                on_active([])
        return
    iterator = iter(enumerate(units))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures: dict[concurrent.futures.Future[Any], tuple[int, Any]] = {}
    active: dict[int, Any] = {}
    normal_completion = False
    halt_dispatch = False

    def active_in_source_order() -> list[Any]:
        return [active[index] for index in sorted(active)]

    def dispatch_next() -> bool:
        try:
            index, unit = next(iterator)
        except StopIteration:
            return False
        active[index] = unit
        if on_active is not None:
            on_active(active_in_source_order())
        # Do not roll back the journal if submit is interrupted: the callable
        # may already have entered the executor before submit returns.
        future = executor.submit(process, unit)
        futures[future] = (index, unit)
        return True

    try:
        for _ in range(min(workers, len(units))):
            dispatch_next()
        while futures:
            done, _pending = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in sorted(done, key=lambda value: futures[value][0]):
                index, unit = futures[future]
                try:
                    key, results = future.result()
                except Exception as exc:
                    on_error(unit, exc)
                    if isinstance(exc, GatewayBudgetExceeded):
                        halt_dispatch = True
                else:
                    on_success(key, results)
                futures.pop(future)
                active.pop(index, None)
                if on_active is not None:
                    on_active(active_in_source_order())
                if not halt_dispatch:
                    dispatch_next()
        normal_completion = True
    finally:
        # At most ``workers`` paid calls can be active here.  A stop waits for
        # only those calls; no unbounded future queue was submitted.
        executor.shutdown(wait=True, cancel_futures=True)
        if normal_completion and on_active is not None:
            on_active([])


def _write_active_dispatch(
    output_dir: Path,
    *,
    stage: str,
    model: str,
    units: list[dict[str, Any]],
) -> None:
    """Persist only sanitized identifiers for the bounded active call window."""
    with _WRITE_LOCK:
        path = output_dir / "active_dispatch.json"
        previous = load_json(path, {})
        history = [
            item
            for item in previous.get("recovery_history", [])
            if isinstance(item, dict)
        ]
        timestamp = now()
        save_json(
            path,
            {
                "schema_version": 2,
                "updated_at": timestamp,
                "stage": stage,
                "model": model,
                "api_style": "chat_completions" if units else None,
                "process_id": os.getpid() if units else None,
                "dispatched_at": timestamp if units else None,
                "active_count": len(units),
                "active_units": units,
                "recovery_history": history,
                "stop_behavior": (
                    "A stop submits no new work and waits for at most this bounded active set; "
                    "durable raw responses are reused on resume. Any surviving active unit is "
                    "migrated to decision-free unknown-response/unknown-billing history."
                ),
            },
        )


def _completed_context_ids_for_stage(output_dir: Path, stage: str) -> set[str]:
    """Return contexts with a durable scientific result checkpoint."""
    filenames = (
        ("claude_r2_results.json", "claude_r2_progress.json")
        if stage == "claude_r2"
        else ("gemini_r3_results.json", "gemini_r3_progress.json")
        if stage == "gemini_r3"
        else ()
    )
    completed: set[str] = set()
    for filename in filenames:
        packet = load_json(output_dir / filename, {})
        completed.update(
            str(item.get("context_id"))
            for item in packet.get("results", [])
            if isinstance(item, dict) and str(item.get("context_id") or "")
        )
    return completed


def _process_is_alive(process_id: Any) -> bool:
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    try:
        os.kill(process_id, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError):
        return False
    return True


def _recover_interrupted_active_dispatch(output_dir: Path) -> int:
    """Preserve stale calls as decision-free, billing-unknown work.

    The active snapshot is written before executor submission. If its owning
    process is gone, response, token, and cost status remain unknown unless the
    same contexts have a durable scientific checkpoint. No zero-cost or
    exclusion inference is ever made.
    """
    path = output_dir / "active_dispatch.json"
    state = load_json(path, {})
    active = state.get("active_units") or []
    if not isinstance(active, list) or not active:
        return 0
    if _process_is_alive(state.get("process_id")):
        return 0
    stage = str(state.get("stage") or "")
    if stage not in {"claude_r2", "gemini_r3"}:
        raise RuntimeError(f"Unknown stale active stage: {stage!r}")
    model = CLAUDE_R2_MODEL if stage == "claude_r2" else GEMINI_R3_MODEL
    completed = _completed_context_ids_for_stage(output_dir, stage)
    problem_packet = load_json(output_dir / "screening_problem_ledger.json", {})
    known_recovery_ids = {
        str(item.get("recovery_id"))
        for collection in (
            problem_packet.get("unresolved") or [],
            problem_packet.get("events") or [],
        )
        for item in collection
        if isinstance(item, dict) and str(item.get("recovery_id") or "")
    }
    recovered_at = now()
    recovered = 0
    recovery_units: list[dict[str, Any]] = []
    for raw in active:
        if not isinstance(raw, dict):
            raise RuntimeError("Malformed stale active unit")
        original_context_ids = sorted(
            {str(value) for value in raw.get("context_ids") or [] if str(value)}
        )
        context_ids = sorted(set(original_context_ids) - completed)
        work_unit_key = str(raw.get("work_unit_key") or "")
        if not work_unit_key or not original_context_ids:
            raise RuntimeError("Malformed stale active unit identity")
        if not context_ids:
            recovery_units.append(
                {
                    "work_unit_key": work_unit_key,
                    "context_ids": original_context_ids,
                    "status": "already_durably_completed",
                }
            )
            continue
        recovery_id = stable_digest(
            {
                "event": "interrupted_active_dispatch",
                "stage": stage,
                "work_unit_key": work_unit_key,
                "context_ids": context_ids,
                "dispatched_at": state.get("dispatched_at"),
            }
        )
        if recovery_id not in known_recovery_ids:
            _record_problem(
                output_dir,
                stage=stage,
                model=model,
                work_unit_key=f"interrupted-{recovery_id}",
                context_ids=context_ids,
                error=(
                    "The process stopped while this unit was active; no scientific "
                    "disposition was inferred."
                ),
                failure_type="interrupted_active_call",
                billing_status="unknown_after_process_interruption",
                metadata={
                    "original_work_unit_key": work_unit_key,
                    "response_receipt_status": "unknown_after_process_interruption",
                    "recovery_id": recovery_id,
                    "recovered_at": recovered_at,
                },
            )
            known_recovery_ids.add(recovery_id)
        weights = Counter(
            parts[1]
            for value in context_ids
            if len(parts := value.split("|", 2)) >= 2
        )
        _append_gateway_receipt(
            output_dir,
            {
                "schema_version": 1,
                "receipt_id": recovery_id,
                "requested_at": state.get("dispatched_at") or state.get("updated_at") or "",
                "received_at": recovered_at,
                "stage": stage,
                "api_style": str(state.get("api_style") or "chat_completions"),
                "schema_transport": "prompt_embedded_schema_with_strict_local_validation",
                "response_format_omitted": True,
                "model_requested": model,
                "model_reported": "",
                "work_unit_key": work_unit_key,
                "attempt": None,
                "context_ids": context_ids,
                "exposure_weights": dict(
                    sorted(weights.items(), key=lambda item: item[0].casefold())
                ),
                "input_digest": "",
                "request_id": "",
                "usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                },
                "token_provenance": "unavailable_after_process_interruption",
                "response_cost_usd": None,
                "cost_header_status": "unknown_after_process_interruption",
                "cost_provenance": "unknown_after_process_interruption",
                "response_receipt_status": "unknown_after_process_interruption",
                "billing_status": "unknown_after_process_interruption",
                "scientific_result_accepted": False,
                "scientific_disposition_created": False,
                "safe_gateway_headers": {},
                "output_sha256": "",
            },
        )
        recovered += 1
        recovery_units.append(
            {
                "work_unit_key": work_unit_key,
                "context_ids": context_ids,
                "status": "unknown_after_process_interruption",
                "recovery_id": recovery_id,
            }
        )
    history = [
        item for item in state.get("recovery_history", []) if isinstance(item, dict)
    ]
    history.append(
        {
            "recovered_at": recovered_at,
            "stage": stage,
            "process_id": state.get("process_id"),
            "recovered_problem_units": recovered,
            "units": recovery_units,
        }
    )
    save_json(
        path,
        {
            "schema_version": 2,
            "updated_at": recovered_at,
            "stage": "not_started",
            "model": "",
            "api_style": None,
            "process_id": None,
            "active_count": 0,
            "active_units": [],
            "recovery_history": history,
            "stop_behavior": (
                "Stale active calls are preserved as decision-free operational "
                "problems; unknown response, token, and billing data are never inferred as zero."
            ),
        },
    )
    _write_cost_files(output_dir)
    return recovered


def recover_interrupted_active_dispatch(
    output_dir: Path,
    **_legacy_arguments: Any,
) -> int:
    """Backward-compatible entry point for global cross-stage recovery."""
    return _recover_interrupted_active_dispatch(output_dir)


def _make_r2_units(
    expected: dict[str, dict[str, Any]],
    article_by_pmid: dict[str, dict[str, Any]],
    completed_context_ids: set[str],
    *,
    batch_size: int,
    max_contexts_per_unit: int = R2_MAX_CONTEXTS_PER_UNIT,
) -> list[dict[str, Any]]:
    """Pack articles deterministically while hard-capping exact contexts."""
    if batch_size < 1:
        raise ValueError("batch size must be positive")
    if max_contexts_per_unit < 1:
        raise ValueError("max_contexts_per_unit must be positive")
    by_pmid: dict[str, list[str]] = defaultdict(list)
    for context_id, row in expected.items():
        if context_id not in completed_context_ids:
            by_pmid[str(row["pmid"])].append(context_id)
    ordered_pmids = sorted(
        by_pmid,
        key=lambda value: (int(value) if value.isdigit() else 10**20, value),
    )
    missing_articles = set(ordered_pmids) - set(article_by_pmid)
    if missing_articles:
        raise RuntimeError(
            f"R2 context metadata lacks source articles: {sorted(missing_articles)}"
        )
    units: list[dict[str, Any]] = []

    def append_unit(context_ids: list[str]) -> None:
        exact = sorted(context_ids)
        pmids: list[str] = []
        for context_id in exact:
            pmid = str(expected[context_id]["pmid"])
            if pmid not in pmids:
                pmids.append(pmid)
        units.append({"pmids": pmids, "context_ids": exact})

    for article_offset in range(0, len(ordered_pmids), batch_size):
        article_group = ordered_pmids[article_offset : article_offset + batch_size]
        current: list[str] = []
        for pmid in article_group:
            pending = sorted(by_pmid[pmid])
            while pending:
                capacity = max_contexts_per_unit - len(current)
                current.extend(pending[:capacity])
                del pending[:capacity]
                if len(current) == max_contexts_per_unit:
                    append_unit(current)
                    current = []
        if current:
            append_unit(current)
    flattened = [context_id for unit in units for context_id in unit["context_ids"]]
    expected_pending = sorted(set(expected) - completed_context_ids)
    if sorted(flattened) != expected_pending or len(flattened) != len(set(flattened)):
        raise RuntimeError("Bounded R2 units do not exactly partition pending contexts")
    return units


def _r2_candidates_with_official_metadata(
    output_dir: Path,
    candidates: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Repair only truly blank titles from exact official PubMed records.

    The immutable R1 packet is never rewritten. A separate R2 input and repair
    receipt bind each official field and the repaired packet digest. PubMed may
    genuinely omit abstract or MeSH values; those optional blanks are retained.
    """
    source_digest = stable_digest(candidates)
    target_pmids = sorted(
        {
            str(article.get("pmid") or "")
            for article in candidates.get("articles", [])
            if isinstance(article, dict)
            and not str(article.get("title") or "").strip()
        },
        key=lambda value: (int(value) if value.isdigit() else 10**20, value),
    )
    if any(not re.fullmatch(r"\d+", pmid) for pmid in target_pmids):
        raise RuntimeError("Blank-title R2 metadata contains an invalid PMID")
    repair_path = output_dir / "r2_official_metadata_repair.json"
    input_path = output_dir / "r2_input_candidates.json"
    existing_repair = load_json(repair_path, {})
    existing_input = load_json(input_path, {})
    if (
        existing_repair.get("source_r1_candidates_digest") == source_digest
        and existing_repair.get("target_pmids") == target_pmids
        and existing_repair.get("status") == "complete"
        and isinstance(existing_input, dict)
        and existing_repair.get("r2_input_candidates_digest")
        == stable_digest(existing_input)
    ):
        repaired = {
            str(article.get("pmid") or ""): article
            for article in existing_input.get("articles", [])
            if isinstance(article, dict)
        }
        if any(
            pmid not in repaired
            or not str(repaired[pmid].get("title") or "").strip()
            for pmid in target_pmids
        ):
            raise RuntimeError("Cached official PubMed title repair is incomplete")
        return existing_input, existing_repair

    updated = json.loads(json.dumps(candidates, ensure_ascii=False))
    repairs: list[dict[str, Any]] = []
    request_count = 0
    records: dict[str, dict[str, Any]] = {}
    if target_pmids:
        with review_security.configured_entrez(
            monthly_evidence_update,
            AUTHORIZED_CREDENTIAL_FILE,
            label="MetaPee",
            tool="MetaPee-Claude-Gemini-PubMed",
        ):
            records, request_count = monthly_evidence_update.entrez_fetch(
                target_pmids
            )
        missing_records = sorted(set(target_pmids) - set(records))
        if missing_records:
            raise RuntimeError(
                f"Official PubMed metadata repair returned no record for {missing_records}"
            )
    article_index = {
        str(article.get("pmid") or ""): article
        for article in updated.get("articles", [])
        if isinstance(article, dict)
    }
    for pmid in target_pmids:
        article = article_index[pmid]
        official = records[pmid]
        title = str(official.get("title") or "").strip()
        if not title:
            raise RuntimeError(f"Official PubMed record still has no title for PMID {pmid}")
        before = {
            "title": article.get("title") or "",
            "abstract": article.get("abstract") or "",
            "mesh": article.get("mesh") or [],
        }
        official_fields = {
            "title": title,
            "abstract": str(official.get("abstract") or ""),
            "mesh": list(official.get("mesh") or []),
        }
        article["title"] = title
        article["abstract"] = (
            before["abstract"]
            if str(before["abstract"]).strip()
            else official_fields["abstract"]
        )
        article["mesh"] = before["mesh"] or official_fields["mesh"]
        for field in (
            "pmcid",
            "doi",
            "published_date",
            "authors",
            "journal",
            "pubmed_url",
        ):
            if official.get(field) not in (None, "", []):
                article[field] = official[field]
        after = {
            "title": article["title"],
            "abstract": article["abstract"],
            "mesh": article["mesh"],
        }
        repairs.append(
            {
                "pmid": pmid,
                "source": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "before_digest": stable_digest(before),
                "official_metadata_digest": stable_digest(official_fields),
                "final_metadata_digest": stable_digest(after),
                "field_provenance": {
                    "title": "official_pubmed_replaced_blank",
                    "abstract": (
                        "preserved_existing_nonblank"
                        if str(before["abstract"]).strip()
                        else "official_pubmed_filled_missing"
                        if official_fields["abstract"]
                        else "official_pubmed_genuinely_absent"
                    ),
                    "mesh": (
                        "preserved_existing_nonblank"
                        if before["mesh"]
                        else "official_pubmed_filled_missing"
                        if official_fields["mesh"]
                        else "official_pubmed_genuinely_absent"
                    ),
                },
                "field_digests": {
                    field: {
                        "before": stable_digest(before[field]),
                        "official": stable_digest(official_fields[field]),
                        "final": stable_digest(after[field]),
                    }
                    for field in ("title", "abstract", "mesh")
                },
                "title_characters": len(article["title"]),
                "abstract_characters": len(article["abstract"]),
                "mesh_heading_count": len(article["mesh"]),
                "officially_absent_optional_fields": [
                    field
                    for field, value in (
                        ("abstract", official_fields["abstract"]),
                        ("mesh", official_fields["mesh"]),
                    )
                    if not value
                ],
            }
        )
    updated["r2_metadata_source"] = "official_pubmed_efetch_for_blank_titles"
    updated["r2_metadata_repaired_pmids"] = target_pmids
    updated["source_r1_candidates_digest"] = source_digest
    save_json(input_path, updated)
    repair = {
        "schema_version": 1,
        "created_at": now(),
        "status": "complete",
        "source": "NCBI PubMed EFetch XML",
        "source_r1_candidates_digest": source_digest,
        "target_pmids": target_pmids,
        "target_count": len(target_pmids),
        "request_count": request_count,
        "optional_abstract_or_mesh_absence_is_not_failure": True,
        "repairs": repairs,
        "r2_input_candidates_digest": stable_digest(updated),
    }
    save_json(repair_path, repair)
    return updated, repair


def run_r2(
    output_dir: Path = OUTPUT_DIR,
    *,
    batch_size: int = 8,
    workers: int = 1,
    max_units: int = 0,
    client: OpenAI | None = None,
    resume_after_budget_reset: bool = False,
) -> dict[str, Any]:
    _migrate_budget_problem_classification(output_dir)
    _recover_interrupted_active_dispatch(output_dir)
    _reconcile_terminal_r3_source_deferrals(output_dir)
    candidates = load_json(output_dir / "r1_pass_candidates.json", None)
    first_packet = load_json(output_dir / "r1_pass_packet.json", None)
    if not isinstance(candidates, dict) or not isinstance(first_packet, dict):
        prepare_r1_lineage(output_dir)
        candidates = load_json(output_dir / "r1_pass_candidates.json", {})
        first_packet = load_json(output_dir / "r1_pass_packet.json", {})
    candidates, metadata_repair = _r2_candidates_with_official_metadata(
        output_dir, candidates
    )
    r2_source_input_digest = stable_digest(
        {
            "source_first_input_digest": first_packet.get("input_digest"),
            "r2_input_candidates_digest": metadata_repair.get(
                "r2_input_candidates_digest"
            ),
        }
    )
    expected = dual_screening.expected_contexts(candidates)
    progress_path = output_dir / "claude_r2_progress.json"
    progress_packet = load_json(progress_path, {})
    progress = {
        str(item.get("context_id")): item
        for item in progress_packet.get("results", [])
        if isinstance(item, dict) and item.get("context_id") in expected
    }
    if progress_packet.get("source_r2_input_digest") != r2_source_input_digest:
        progress = {}
    article_by_pmid = {
        str(article.get("pmid") or ""): article
        for article in candidates.get("articles", [])
        if isinstance(article, dict)
    }
    units = _make_r2_units(
        expected,
        article_by_pmid,
        set(progress),
        batch_size=batch_size,
    )
    total_pending_units = len(units)
    if max_units > 0:
        units = units[:max_units]
    budget_exhausted = False
    budget_restart_required = (
        "claude_r2" in _budget_blocked_stages(output_dir)
        and bool(units)
        and not resume_after_budget_reset
    )
    gateway = client or (
        _configure_client() if units and not budget_restart_required else None
    )

    def ids_for(unit: dict[str, Any]) -> list[str]:
        return [cid for cid in unit["context_ids"] if cid not in progress]

    def process(unit: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if gateway is None:  # pragma: no cover - guarded by non-empty units
            raise AssertionError("Claude gateway was not configured")
        context_ids = ids_for(unit)
        unit_expected = {cid: expected[cid] for cid in context_ids}
        articles = [article_by_pmid[pmid] for pmid in unit["pmids"]]
        key = _work_unit_key("claude_r2", context_ids)
        results = _call_gateway_json(
            gateway,
            output_dir=output_dir,
            stage="claude_r2",
            model=CLAUDE_R2_MODEL,
            system=dual_screening.SECOND_SYSTEM,
            prompt=_r2_prompt(articles, unit_expected),
            context_ids=context_ids,
            exposure_weights=dict(Counter(value["exposure"] for value in unit_expected.values())),
            validator=lambda payload: [
                apply_parallel_r3_gate(item)
                for item in dual_screening.validate_second(payload, unit_expected)
            ],
        )
        return key, results

    def persist(stopped_reason: str) -> None:
        packet = {
            "schema_version": 2,
            "updated_at": now(),
            "track": "claude",
            "model": CLAUDE_R2_MODEL,
            "api_style": "chat_completions",
            "schema_transport": "prompt_embedded_schema_with_strict_local_validation",
            "protocol_version": PROTOCOL_VERSION,
            "source_first_input_digest": first_packet.get("input_digest"),
            "source_r2_input_digest": r2_source_input_digest,
            "r2_official_metadata_repair_digest": stable_digest(metadata_repair),
            "expected_contexts": len(expected),
            "completed_contexts": len(progress),
            "status": "complete" if len(progress) == len(expected) else "partial",
            "stopped_reason": stopped_reason,
            "budget_restart_required": (
                "claude_r2" in _budget_blocked_stages(output_dir)
            ),
            "results": sorted(progress.values(), key=lambda item: item["context_id"]),
        }
        save_json(progress_path, packet)
        if len(progress) == len(expected):
            final = dict(packet)
            final["created_at"] = now()
            final["include"] = sum(item.get("eligibility") == "INCLUDE" for item in progress.values())
            final["exclude"] = sum(item.get("eligibility") == "EXCLUDE" for item in progress.values())
            final["gemini_r3_eligible"] = sum(bool(item.get("full_text_eligible")) for item in progress.values())
            save_json(output_dir / "claude_r2_results.json", final)
        write_outputs_and_state(output_dir, stopped_reason=stopped_reason)

    def success(key: str, results: list[dict[str, Any]]) -> None:
        with _WRITE_LOCK:
            progress.update({item["context_id"]: item for item in results})
            _resolve_problems_by_completed_contexts(
                output_dir,
                stage="claude_r2",
                completed_context_ids=progress,
            )
            persist("r2_running")

    def error(unit: dict[str, Any], exc: BaseException) -> None:
        nonlocal budget_exhausted
        context_ids = ids_for(unit)
        key = _work_unit_key("claude_r2", context_ids)
        failure_type, billing_status = _problem_record_fields(exc)
        budget_exhausted = budget_exhausted or (
            failure_type == "infrastructure_budget_exhausted"
        )
        _record_problem(
            output_dir,
            stage="claude_r2",
            model=CLAUDE_R2_MODEL,
            work_unit_key=key,
            context_ids=context_ids,
            error=exc,
            failure_type=failure_type,
            billing_status=billing_status,
        )
        with _WRITE_LOCK:
            persist("r2_unresolved_operational_problems")

    def active(active_units: list[dict[str, Any]]) -> None:
        _write_active_dispatch(
            output_dir,
            stage="claude_r2",
            model=CLAUDE_R2_MODEL,
            units=[
                {
                    "pmids": sorted(unit["pmids"]),
                    "context_ids": ids_for(unit),
                    "work_unit_key": _work_unit_key("claude_r2", ids_for(unit)),
                }
                for unit in active_units
            ],
        )

    if budget_restart_required:
        with _WRITE_LOCK:
            persist("r2_infrastructure_budget_exhausted_restart_required")
        return load_json(progress_path, {})

    _run_units(units, process, success, error, workers, on_active=active)
    stopped = (
        "r2_complete"
        if len(progress) == len(expected)
        else "r2_infrastructure_budget_exhausted_restart_required"
        if budget_exhausted
        else "r2_bounded_max_units" if max_units > 0 and total_pending_units > len(units)
        else "r2_unresolved_operational_problems"
    )
    with _WRITE_LOCK:
        persist(stopped)
    return load_json(progress_path, {})


def _normalise_pmcid(pmcid: Any) -> str:
    value = re.sub(r"\s+", "", str(pmcid or "")).upper()
    match = re.fullmatch(r"(?:PMC)?([0-9]+)", value)
    if not match:
        raise ValueError(f"Invalid PMCID: {pmcid!r}")
    return "PMC" + match.group(1)


def _normalise_pmid(pmid: Any) -> str:
    value = re.sub(r"\s+", "", str(pmid or ""))
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"Invalid PMID: {pmid!r}")
    return value


def _normalise_exact_legacy_pmc_evidence_urls(
    payload: dict[str, Any],
    *,
    expected_pmcid: str,
) -> tuple[dict[str, Any], dict[str, list[dict[str, str]]]]:
    """Canonicalize only PMC's exact legacy official article URL alias.

    The returned payload is a deep copy, so the paid provider response retained
    in ``output_text`` remains byte-for-byte immutable. Wrong-PMC and
    non-official URLs are deliberately untouched for the strict validator to
    reject. Every mechanical change is attributed to its context and field.
    """
    pmcid = _normalise_pmcid(expected_pmcid)
    legacy_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
    canonical_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    normalized = copy.deepcopy(payload)
    events: dict[str, list[dict[str, str]]] = defaultdict(list)
    rows = normalized.get("results")
    if not isinstance(rows, list):
        return normalized, events

    def visit(
        value: Any,
        *,
        context_id: str,
        field_path: str,
    ) -> None:
        if isinstance(value, dict):
            if (
                value.get("source") == "full_text"
                and value.get("url") == legacy_url
            ):
                value["url"] = canonical_url
                events[context_id].append(
                    {
                        "field": f"{field_path}.url".lstrip("."),
                        "before": legacy_url,
                        "after": canonical_url,
                        "rule": "exact_legacy_official_pmc_url_alias",
                    }
                )
            for key, child in value.items():
                visit(
                    child,
                    context_id=context_id,
                    field_path=f"{field_path}.{key}".lstrip("."),
                )
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(
                    child,
                    context_id=context_id,
                    field_path=f"{field_path}[{index}]",
                )

    for row in rows:
        if not isinstance(row, dict):
            continue
        context_id = str(row.get("context_id") or "")
        visit(row, context_id=context_id, field_path="")
    return normalized, events


def _validate_r3_payload_with_canonical_pmc_url(
    payload: dict[str, Any],
    expected: dict[str, dict[str, Any]],
    *,
    source_text: str,
    pmcid: str,
) -> list[dict[str, Any]]:
    normalized, events = _normalise_exact_legacy_pmc_evidence_urls(
        payload,
        expected_pmcid=pmcid,
    )
    rows = dual_screening.validate_full_text(
        normalized,
        expected,
        source_text=source_text,
        source_identifier=_normalise_pmcid(pmcid),
    )
    for row in rows:
        context_id = str(row.get("context_id") or "")
        row["r3_provider_url_normalization_events"] = copy.deepcopy(
            events.get(context_id, [])
        )
    return rows


def _xml_local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1].casefold()


def _xml_attribute(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if _xml_local_name(key) == name.casefold():
            return str(value).strip()
    return ""


def _own_jats_elements(element: ET.Element) -> Iterable[ET.Element]:
    """Iterate one article while excluding nested article-like documents."""
    yield element
    for child in list(element):
        if _xml_local_name(child.tag) in {"article", "sub-article", "response"}:
            continue
        yield from _own_jats_elements(child)


def _own_jats_text(element: ET.Element) -> str:
    parts: list[str] = []

    def visit(node: ET.Element, *, root: bool = False) -> None:
        if not root and _xml_local_name(node.tag) in {
            "article",
            "sub-article",
            "response",
        }:
            return
        if node.text:
            parts.append(node.text)
        for child in list(node):
            visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(element, root=True)
    return " ".join(" ".join(parts).split()).strip()


def _jats_article_text(article: ET.Element) -> str:
    blocks: list[str] = []
    allowed = {"article-title", "title", "p", "th", "td", "caption", "preformat"}
    for element in _own_jats_elements(article):
        if _xml_local_name(element.tag) not in allowed:
            continue
        value = _own_jats_text(element)
        if value and (not blocks or value != blocks[-1]):
            blocks.append(value)
    return "\n".join(blocks).strip()


def _validate_pmc_jats(
    raw: bytes,
    *,
    expected_pmcid: str,
    expected_pmid: str,
) -> tuple[str, dict[str, Any]]:
    """Verify one exact JATS article and its substantive direct body.

    Historical PMC scans may place OCR in ``preformat`` instead of ``p``.
    Preformatted body content is therefore retained and can satisfy the
    narrative-structure requirement when it is independently substantive.
    """
    expected_pmcid = _normalise_pmcid(expected_pmcid)
    expected_pmid = _normalise_pmid(expected_pmid)
    if not isinstance(raw, bytes) or not raw:
        raise OperationalProblem("PMC JATS response must be nonempty bytes")
    if len(raw) > PMC_MAX_RESPONSE_BYTES:
        raise OperationalProblem("PMC JATS response exceeds the source size limit")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise OperationalProblem(f"PMC source is not valid JATS XML: {safe_error(exc)}") from exc

    articles = [
        root
        if _xml_local_name(root.tag) == "article"
        else None,
        *(
            child
            for child in list(root)
            if _xml_local_name(child.tag) == "article"
        ),
    ]
    articles = [article for article in articles if article is not None]
    all_articles = [
        element for element in root.iter() if _xml_local_name(element.tag) == "article"
    ]
    if len(articles) != 1 or len(all_articles) != 1:
        raise OperationalProblem(
            "PMC JATS must contain exactly one non-nested top-level article for "
            f"{expected_pmcid}/{expected_pmid}"
        )
    article = articles[0]
    fronts = [child for child in list(article) if _xml_local_name(child.tag) == "front"]
    if len(fronts) != 1:
        raise OperationalProblem("PMC JATS must contain one direct article front")
    article_metas = [
        child
        for child in list(fronts[0])
        if _xml_local_name(child.tag) == "article-meta"
    ]
    if len(article_metas) != 1:
        raise OperationalProblem("PMC JATS must contain one direct article-meta")
    article_meta = article_metas[0]
    relevant = [
        element
        for element in _own_jats_elements(article)
        if _xml_local_name(element.tag) == "article-id"
        and _xml_attribute(element, "pub-id-type").casefold()
        in {"pmc", "pmcid", "pmid"}
    ]
    meta_ids = {id(element) for element in article_meta.iter()}
    pmc_values = [
        _normalise_pmcid("".join(element.itertext()).strip())
        for element in relevant
        if _xml_attribute(element, "pub-id-type").casefold() in {"pmc", "pmcid"}
    ]
    pmid_values = [
        _normalise_pmid("".join(element.itertext()).strip())
        for element in relevant
        if _xml_attribute(element, "pub-id-type").casefold() == "pmid"
    ]
    pmc_elements = [
        element
        for element in relevant
        if _xml_attribute(element, "pub-id-type").casefold() in {"pmc", "pmcid"}
    ]
    pmid_elements = [
        element
        for element in relevant
        if _xml_attribute(element, "pub-id-type").casefold() == "pmid"
    ]
    if (
        pmc_values != [expected_pmcid]
        or pmid_values != [expected_pmid]
        or len(pmc_elements) != 1
        or len(pmid_elements) != 1
        or id(pmc_elements[0]) not in meta_ids
        or id(pmid_elements[0]) not in meta_ids
    ):
        raise OperationalProblem(
            f"PMC JATS exact identity mismatch for {expected_pmcid}/{expected_pmid}"
        )
    bodies = [child for child in list(article) if _xml_local_name(child.tag) == "body"]
    if len(bodies) != 1:
        raise OperationalProblem(
            f"PMC JATS must contain exactly one direct article body for {expected_pmcid}/{expected_pmid}"
        )
    body = bodies[0]
    body_text = _own_jats_text(body)
    word_count = len(body_text.split())
    paragraphs = [
        _own_jats_text(element)
        for element in _own_jats_elements(body)
        if _xml_local_name(element.tag) == "p"
    ]
    paragraphs = [value for value in paragraphs if value]
    preformats = [
        _own_jats_text(element)
        for element in _own_jats_elements(body)
        if _xml_local_name(element.tag) == "preformat"
    ]
    preformats = [value for value in preformats if value]
    preformat_characters = sum(len(value) for value in preformats)
    text_content = _jats_article_text(article)
    if len(body_text) < 1000 or word_count < 75:
        raise OperationalProblem(
            f"PMC JATS direct body is not substantive for {expected_pmcid}/{expected_pmid}: "
            f"characters={len(body_text)}, words={word_count}"
        )
    if len(paragraphs) < 3 and preformat_characters < 1000:
        raise OperationalProblem(
            f"PMC JATS body lacks narrative paragraphs or substantive preformat OCR for {expected_pmcid}"
        )
    if len(text_content) < PMC_MIN_COMPLETE_TEXT_CHARACTERS:
        raise OperationalProblem(
            f"PMC JATS extracted text is below the {PMC_MIN_COMPLETE_TEXT_CHARACTERS}-character R3 floor for {expected_pmcid}"
        )
    text = text_content.rstrip() + "\n"
    article_xml = ET.tostring(article, encoding="utf-8")
    validation = {
        "expected_pmcid": expected_pmcid,
        "expected_pmid": expected_pmid,
        "top_level_article_count": 1,
        "matching_article_count": 1,
        "identity_verified": True,
        "direct_article_body_verified": True,
        "direct_body_characters": len(body_text),
        "direct_body_words": word_count,
        "body_paragraphs": len(paragraphs),
        "body_preformats": len(preformats),
        "body_preformat_characters": preformat_characters,
        "extracted_characters": len(text_content),
        "raw_xml_sha256": hashlib.sha256(raw).hexdigest(),
        "article_xml_sha256": hashlib.sha256(article_xml).hexdigest(),
    }
    return text, validation


_HTML_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_HTML_TEXT_BLOCKS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "th", "td", "li",
    "caption", "figcaption", "blockquote", "pre",
}
_HTML_ANCILLARY_CLASS_TOKENS = {
    "author-notes", "def-list", "fn-group", "footnotes", "glossary", "notes",
    "ref-list", "references",
}
_HTML_HIDDEN_CLASS_TOKENS = {
    "display-none", "hidden", "usa-sr-only", "visually-hidden",
}
_HTML_ANCILLARY_ROLES = {
    "doc-bibliography", "doc-endnotes", "menu", "menubar", "navigation",
}


class _PmcArticleHtmlParser(HTMLParser):
    """Extract one identity-bound canonical PMC main-article HTML body."""

    def __init__(self, expected_pmcid: str) -> None:
        super().__init__(convert_charrefs=True)
        self.expected_pmcid = expected_pmcid
        self.stack: list[dict[str, Any]] = []
        self.head_active = 0
        self.target_container_active = 0
        self.target_main_active = 0
        self.target_article_active = 0
        self.target_body_active = 0
        self.abstract_active = 0
        self.ancillary_active = 0
        self.html_count = 0
        self.head_count = 0
        self.target_container_count = 0
        self.main_count = 0
        self.article_count = 0
        self.body_count = 0
        self.body_paragraphs = 0
        self.body_sections = 0
        self.non_abstract_paragraphs = 0
        self.non_abstract_sections = 0
        self.identity_markup_outside_head = 0
        self.structure_mismatches = 0
        self.citation_pmids: list[str] = []
        self.citation_titles: list[str] = []
        self.citation_dois: list[str] = []
        self.citation_pdf_urls: list[str] = []
        self.canonical_urls: list[str] = []
        self.fulltext_urls: list[str] = []
        self.article_container_ids: list[str] = []
        self.body_text_parts: list[str] = []
        self.non_abstract_text_parts: list[str] = []
        self.text_blocks: list[str] = []
        self.active_blocks: list[dict[str, Any]] = []

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).casefold(): str(value or "") for key, value in attrs}

    @staticmethod
    def _style_hides_content(style: str) -> bool:
        declarations: dict[str, str] = {}
        for declaration in str(style or "").split(";"):
            if ":" not in declaration:
                continue
            key, value = declaration.split(":", 1)
            declarations[key.strip().casefold()] = (
                value.strip().casefold().removesuffix("!important").strip()
            )
        return declarations.get("display") == "none" or declarations.get(
            "visibility"
        ) in {"hidden", "collapse"}

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        is_void: bool,
    ) -> None:
        name = str(tag).casefold()
        attributes = self._attributes(attrs)
        class_tokens = set(attributes.get("class", "").casefold().split())
        if name == "html":
            self.html_count += 1
        target_head = name == "head"
        if target_head:
            self.head_count += 1
        if name == "meta":
            meta_name = attributes.get("name", "").casefold()
            content = " ".join(attributes.get("content", "").split())
            relevant = {
                "citation_pmid", "citation_title", "citation_doi",
                "citation_pdf_url", "citation_fulltext_html_url",
            }
            if meta_name in relevant and self.head_active <= 0:
                self.identity_markup_outside_head += 1
            elif meta_name == "citation_pmid" and content:
                self.citation_pmids.append(content)
            elif meta_name == "citation_title" and content:
                self.citation_titles.append(content)
            elif meta_name == "citation_doi" and content:
                self.citation_dois.append(content)
            elif meta_name == "citation_pdf_url" and content:
                self.citation_pdf_urls.append(content)
            elif meta_name == "citation_fulltext_html_url" and content:
                self.fulltext_urls.append(content)
        elif name == "link":
            rel_tokens = set(attributes.get("rel", "").casefold().split())
            if "canonical" in rel_tokens and self.head_active <= 0:
                self.identity_markup_outside_head += 1
            elif "canonical" in rel_tokens and attributes.get("href"):
                self.canonical_urls.append(attributes["href"].strip())

        target_container = bool(
            name == "div"
            and "pmc-article-section" in class_tokens
            and attributes.get("data-article-db", "").casefold() == "pmc"
            and _normalise_pmcid(attributes.get("data-article-id"))
            == self.expected_pmcid
        )
        if name == "div" and "pmc-article-section" in class_tokens:
            self.article_container_ids.append(attributes.get("data-article-id", ""))
        if target_container:
            self.target_container_count += 1
        inside_container = self.target_container_active > 0 or target_container
        target_main = bool(
            name == "main"
            and attributes.get("id") == "main-content"
            and inside_container
        )
        if target_main:
            self.main_count += 1
        inside_main = self.target_main_active > 0 or target_main
        target_article = bool(name == "article" and inside_main)
        if target_article:
            self.article_count += 1
        inside_article = self.target_article_active > 0 or target_article
        target_body = bool(
            name == "section"
            and inside_article
            and "main-article-body" in class_tokens
        )
        if target_body:
            self.body_count += 1
        inside_body = self.target_body_active > 0 or target_body

        element_id = attributes.get("id", "").casefold()
        role = attributes.get("role", "").casefold()
        aria_label = " ".join(attributes.get("aria-label", "").split()).casefold()
        target_abstract = bool(
            "abstract" in class_tokens
            or element_id.startswith("abstract")
            or aria_label == "abstract"
        )
        target_ancillary = bool(
            name == "nav"
            or role in _HTML_ANCILLARY_ROLES
            or bool(class_tokens & _HTML_ANCILLARY_CLASS_TOKENS)
            or bool(class_tokens & _HTML_HIDDEN_CLASS_TOKENS)
            or element_id.startswith(
                ("fn-group", "footnote", "notes", "ref-list", "references")
            )
            or "hidden" in attributes
            or "inert" in attributes
            or attributes.get("aria-hidden", "").casefold() == "true"
            or self._style_hides_content(attributes.get("style", ""))
        )
        inside_abstract = self.abstract_active > 0 or target_abstract
        inside_ancillary = self.ancillary_active > 0 or target_ancillary
        inside_narrative = inside_body and not inside_abstract and not inside_ancillary
        if inside_body and name == "p":
            self.body_paragraphs += 1
        if inside_body and name == "section" and not target_body:
            self.body_sections += 1
        if inside_narrative and name == "p":
            self.non_abstract_paragraphs += 1
        if inside_narrative and name == "section" and not target_body:
            self.non_abstract_sections += 1

        block: dict[str, Any] | None = None
        if inside_body and not inside_ancillary and name in _HTML_TEXT_BLOCKS:
            for parent in self.active_blocks:
                parent["has_child_block"] = True
            block = {"parts": [], "has_child_block": False}
            self.active_blocks.append(block)
        entry = {
            "tag": name,
            "target_head": target_head,
            "target_container": target_container,
            "target_main": target_main,
            "target_article": target_article,
            "target_body": target_body,
            "target_abstract": target_abstract,
            "target_ancillary": target_ancillary,
            "block": block,
        }
        for target, attribute in (
            (target_head, "head_active"),
            (target_container, "target_container_active"),
            (target_main, "target_main_active"),
            (target_article, "target_article_active"),
            (target_body, "target_body_active"),
            (target_abstract, "abstract_active"),
            (target_ancillary, "ancillary_active"),
        ):
            if target:
                setattr(self, attribute, getattr(self, attribute) + 1)
        if is_void:
            self._finish(entry)
        else:
            self.stack.append(entry)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._start(tag, attrs, is_void=str(tag).casefold() in _HTML_VOID_ELEMENTS)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._start(tag, attrs, is_void=True)

    def _finish(self, entry: dict[str, Any]) -> None:
        block = entry.get("block")
        if block is not None:
            if block in self.active_blocks:
                self.active_blocks.remove(block)
            value = " ".join(" ".join(block["parts"]).split())
            if value and not block["has_child_block"]:
                self.text_blocks.append(value)
        for key, attribute in (
            ("target_body", "target_body_active"),
            ("target_abstract", "abstract_active"),
            ("target_ancillary", "ancillary_active"),
            ("target_article", "target_article_active"),
            ("target_main", "target_main_active"),
            ("target_container", "target_container_active"),
            ("target_head", "head_active"),
        ):
            if entry.get(key):
                setattr(self, attribute, getattr(self, attribute) - 1)

    def handle_endtag(self, tag: str) -> None:
        name = str(tag).casefold()
        match_index = next(
            (
                index
                for index in range(len(self.stack) - 1, -1, -1)
                if self.stack[index]["tag"] == name
            ),
            None,
        )
        if match_index is None:
            self.structure_mismatches += 1
            return
        if match_index != len(self.stack) - 1:
            self.structure_mismatches += len(self.stack) - 1 - match_index
        for entry in reversed(self.stack[match_index:]):
            self._finish(entry)
        del self.stack[match_index:]

    def handle_data(self, data: str) -> None:
        if self.target_body_active <= 0:
            return
        if any(entry["tag"] in {"script", "style", "noscript", "svg"} for entry in self.stack):
            return
        if not data:
            return
        self.body_text_parts.append(data)
        if self.ancillary_active <= 0:
            for block in self.active_blocks:
                block["parts"].append(data)
            if self.abstract_active <= 0:
                self.non_abstract_text_parts.append(data)

    def close(self) -> None:
        super().close()
        if self.stack or any(
            (
                self.head_active,
                self.target_container_active,
                self.target_main_active,
                self.target_article_active,
                self.target_body_active,
                self.abstract_active,
                self.ancillary_active,
            )
        ):
            self.structure_mismatches += 1


def _validate_pmc_html(
    raw: bytes,
    *,
    expected_pmcid: str,
    expected_pmid: str,
) -> tuple[str, dict[str, Any]]:
    """Validate exact canonical PMC HTML and a substantive non-abstract body."""
    expected_pmcid = _normalise_pmcid(expected_pmcid)
    expected_pmid = _normalise_pmid(expected_pmid)
    if not isinstance(raw, bytes) or not raw:
        raise OperationalProblem("PMC HTML response must be nonempty bytes")
    if len(raw) > PMC_MAX_RESPONSE_BYTES:
        raise OperationalProblem("PMC HTML response exceeds the source size limit")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OperationalProblem("PMC HTML response is not strict UTF-8") from exc
    parser = _PmcArticleHtmlParser(expected_pmcid)
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        raise OperationalProblem(f"PMC HTML response is malformed: {safe_error(exc)}") from exc
    expected_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{expected_pmcid}/"
    observed_pmids = {
        _normalise_pmid(value) for value in parser.citation_pmids
    }
    observed_pmcids = {
        _normalise_pmcid(value) for value in parser.article_container_ids
    }
    if parser.html_count != 1 or parser.head_count != 1:
        raise OperationalProblem("PMC HTML must contain exactly one html/head document")
    if parser.identity_markup_outside_head:
        raise OperationalProblem("PMC HTML identity metadata occurs outside head")
    if parser.canonical_urls != [expected_url] or parser.fulltext_urls != [expected_url]:
        raise OperationalProblem("PMC HTML canonical/full-text URL identity mismatch")
    if observed_pmcids != {expected_pmcid} or len(parser.article_container_ids) != 1:
        raise OperationalProblem("PMC HTML PMCID identity mismatch")
    if observed_pmids != {expected_pmid} or len(parser.citation_pmids) != 1:
        raise OperationalProblem("PMC HTML PMID identity mismatch")
    if len(parser.citation_titles) != 1:
        raise OperationalProblem("PMC HTML must contain one citation title")
    if (
        parser.target_container_count != 1
        or parser.main_count != 1
        or parser.article_count != 1
        or parser.body_count != 1
        or parser.structure_mismatches
    ):
        raise OperationalProblem("PMC HTML main-article body structure is invalid")
    body_text = " ".join(" ".join(parser.body_text_parts).split())
    non_abstract_text = " ".join(" ".join(parser.non_abstract_text_parts).split())
    if (
        len(body_text) < PMC_MIN_COMPLETE_TEXT_CHARACTERS
        or len(non_abstract_text) < PMC_MIN_COMPLETE_TEXT_CHARACTERS
        or parser.body_paragraphs < 3
        or parser.non_abstract_paragraphs < 3
        or parser.body_sections < 1
        or parser.non_abstract_sections < 1
    ):
        raise OperationalProblem("PMC HTML lacks a substantive non-abstract article body")
    blocks = [" ".join(value.split()) for value in parser.text_blocks]
    blocks = [value for value in blocks if value]
    text_content = "\n".join([" ".join(parser.citation_titles[0].split()), *blocks]).strip()
    if len(text_content) < PMC_MIN_COMPLETE_TEXT_CHARACTERS:
        raise OperationalProblem("PMC HTML extracted article text is incomplete")
    text = text_content + "\n"
    validation = {
        "expected_pmcid": expected_pmcid,
        "expected_pmid": expected_pmid,
        "identity_verified": True,
        "direct_article_body_verified": True,
        "body_characters": len(body_text),
        "non_abstract_body_characters": len(non_abstract_text),
        "body_paragraphs": parser.body_paragraphs,
        "non_abstract_body_paragraphs": parser.non_abstract_paragraphs,
        "body_sections": parser.body_sections,
        "non_abstract_body_sections": parser.non_abstract_sections,
        "raw_html_sha256": hashlib.sha256(raw).hexdigest(),
        "body_text_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        "non_abstract_body_text_sha256": hashlib.sha256(
            non_abstract_text.encode("utf-8")
        ).hexdigest(),
        "extracted_characters": len(text_content),
    }
    return text, validation


PINNED_POPPLER_VERSION = "26.07.0"
PMC_PDF_MINIMUM_BYTES = 20_000
PMC_PDF_MINIMUM_PAGE_CHARACTERS = 100
PMC_PDF_MAXIMUM_PAGES = 200
PMC_PDF_POW_COOKIE_NAME = "cloudpmc-viewer-pow"
PMC_PDF_POW_DIFFICULTY = 4
PMC_PDF_MAX_POW_NONCE = 5_000_000


def _normalize_doi(value: Any) -> str:
    text = " ".join(str(value or "").split()).casefold()
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", text)
    return text.rstrip(". ,;)")


def _normalize_identity_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _validate_pmc_pdf_link_html(
    raw: bytes,
    *,
    expected_pmcid: str,
    expected_pmid: str,
    expected_title: str,
    expected_doi: str,
) -> dict[str, Any]:
    """Require one identity-matched PDF URL declared by canonical PMC HTML."""
    expected_pmcid = _normalise_pmcid(expected_pmcid)
    expected_pmid = _normalise_pmid(expected_pmid)
    expected_title = " ".join(str(expected_title or "").split())
    expected_doi = _normalize_doi(expected_doi)
    if not expected_title or not expected_doi:
        raise OperationalProblem("Strict PMC PDF fallback requires exact title and DOI")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OperationalProblem("PMC PDF-link HTML is not strict UTF-8") from exc
    parser = _PmcArticleHtmlParser(expected_pmcid)
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        raise OperationalProblem(f"PMC PDF-link HTML is malformed: {safe_error(exc)}") from exc
    expected_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{expected_pmcid}/"
    if (
        parser.html_count != 1
        or parser.head_count != 1
        or parser.identity_markup_outside_head
        or parser.canonical_urls != [expected_url]
        or parser.fulltext_urls != [expected_url]
        or parser.citation_pmids != [expected_pmid]
        or len(parser.citation_titles) != 1
        or _normalize_identity_text(parser.citation_titles[0])
        != _normalize_identity_text(expected_title)
        or len(parser.citation_dois) != 1
        or _normalize_doi(parser.citation_dois[0]) != expected_doi
        or len(parser.article_container_ids) != 1
        or _normalise_pmcid(parser.article_container_ids[0]) != expected_pmcid
        or len(parser.citation_pdf_urls) != 1
    ):
        raise OperationalProblem("PMC PDF-link HTML exact identity validation failed")
    pdf_url = parser.citation_pdf_urls[0]
    if not re.fullmatch(
        rf"https://pmc\.ncbi\.nlm\.nih\.gov/articles/{re.escape(expected_pmcid)}/pdf/[^/?#]+\.pdf",
        pdf_url,
    ):
        raise OperationalProblem("PMC PDF URL is outside the exact official article path")
    receipt = {
        "pmcid": expected_pmcid,
        "pmid": expected_pmid,
        "title": expected_title,
        "doi": expected_doi,
        "canonical_url": expected_url,
        "pdf_url": pdf_url,
        "raw_html_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_html_bytes": len(raw),
    }
    receipt["receipt_digest"] = stable_digest(receipt)
    return receipt


def _pdf_tool_version(tool: str) -> tuple[str, str]:
    executable = shutil.which(tool)
    if not executable:
        raise OperationalProblem(f"Required Poppler tool is missing: {tool}")
    completed = subprocess.run(
        [executable, "-v"], check=False, capture_output=True, timeout=10
    )
    material = (
        completed.stdout.decode("utf-8", errors="replace")
        + "\n"
        + completed.stderr.decode("utf-8", errors="replace")
    )
    match = re.search(rf"(?m)^{re.escape(tool)} version ([0-9.]+)$", material)
    if match is None or match.group(1) != PINNED_POPPLER_VERSION:
        raise OperationalProblem(
            f"Poppler {tool} must be exactly {PINNED_POPPLER_VERSION}"
        )
    return executable, match.group(1)


def _run_pdf_tool(command: list[str], *, timeout: float = 60.0) -> bytes:
    completed = subprocess.run(
        command, check=False, capture_output=True, timeout=timeout
    )
    if completed.returncode != 0:
        detail = safe_error(completed.stderr.decode("utf-8", errors="replace"))
        raise OperationalProblem(
            f"PDF tool failed ({command[0]}, exit {completed.returncode}): {detail}"
        )
    return completed.stdout


def _parse_colon_fields(raw: bytes, source: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise OperationalProblem(f"Malformed {source} output")
        key, value = line.split(":", 1)
        canonical = key.strip()
        if not canonical or canonical in fields:
            raise OperationalProblem(f"Duplicate or empty {source} field")
        fields[canonical] = value.strip()
    return fields


def _inspect_pmc_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    tools: dict[str, str] = {}
    versions: dict[str, str] = {}
    for tool in ("pdfinfo", "pdftotext", "pdfdetach"):
        tools[tool], versions[tool] = _pdf_tool_version(tool)
    with tempfile.TemporaryDirectory(prefix="metapee-pmc-pdf-") as directory:
        path = Path(directory) / "source.pdf"
        path.write_bytes(pdf_bytes)
        info = _parse_colon_fields(
            _run_pdf_tool([tools["pdfinfo"], str(path)]), "pdfinfo"
        )
        javascript = _run_pdf_tool([tools["pdfinfo"], "-js", str(path)])
        urls = _run_pdf_tool([tools["pdfinfo"], "-url", str(path)])
        attachments = _run_pdf_tool([tools["pdfdetach"], "-list", str(path)])
        extracted = _run_pdf_tool(
            [tools["pdftotext"], "-layout", "-enc", "UTF-8", str(path), "-"],
            timeout=120,
        ).decode("utf-8", errors="strict")
    attachment_match = re.fullmatch(
        r"\s*(\d+) embedded files?\s*",
        attachments.decode("utf-8", errors="strict"),
    )
    if attachment_match is None:
        raise OperationalProblem("Unable to parse PDF attachment inventory")
    pages = [
        "\n".join(line.rstrip() for line in page.splitlines()).strip()
        for page in extracted.replace("\r\n", "\n").replace("\r", "\n").split("\f")
        if page.strip()
    ]
    return {
        "versions": versions,
        "info": info,
        "javascript": javascript.decode("utf-8", errors="strict").strip(),
        "urls_sha256": hashlib.sha256(urls).hexdigest(),
        "embedded_file_count": int(attachment_match.group(1)),
        "pages": pages,
        "text": "\n\n".join(pages).strip(),
    }


def _validate_pmc_pdf(
    source_html: bytes,
    pdf_bytes: bytes,
    *,
    expected_pmcid: str,
    expected_pmid: str,
    expected_title: str,
    expected_doi: str,
) -> tuple[str, dict[str, Any]]:
    link = _validate_pmc_pdf_link_html(
        source_html,
        expected_pmcid=expected_pmcid,
        expected_pmid=expected_pmid,
        expected_title=expected_title,
        expected_doi=expected_doi,
    )
    if not isinstance(pdf_bytes, bytes) or len(pdf_bytes) < PMC_PDF_MINIMUM_BYTES:
        raise OperationalProblem("Official PMC PDF is below the minimum byte size")
    if not pdf_bytes.startswith(b"%PDF-") or not pdf_bytes.rstrip().endswith(b"%%EOF"):
        raise OperationalProblem("Official PMC PDF container is not closed")
    inspection = _inspect_pmc_pdf(pdf_bytes)
    info = inspection["info"]
    try:
        page_count = int(info.get("Pages") or 0)
    except ValueError as exc:
        raise OperationalProblem("Official PMC PDF page count is invalid") from exc
    if (
        not 1 <= page_count <= PMC_PDF_MAXIMUM_PAGES
        or len(inspection["pages"]) != page_count
    ):
        raise OperationalProblem("Official PMC PDF page/text count mismatch")
    encryption_metadata = " ".join(str(info.get("Encrypted") or "").split())
    encryption_status = encryption_metadata.split(" ", 1)[0].casefold()
    if encryption_status not in {"no", "yes"}:
        raise OperationalProblem("Official PMC PDF encryption metadata is invalid")
    if (
        " ".join(str(info.get("JavaScript") or "").split()).casefold() != "no"
        or " ".join(str(info.get("Form") or "").split()).casefold() != "none"
        or inspection["javascript"]
        or inspection["embedded_file_count"] != 0
    ):
        raise OperationalProblem("Official PMC PDF contains active or embedded content")
    text_content = str(inspection["text"])
    page_characters = [len(page) for page in inspection["pages"]]
    if len(text_content) < PMC_MIN_COMPLETE_TEXT_CHARACTERS:
        raise OperationalProblem("Official PMC PDF extracted text is incomplete")
    if any(value < PMC_PDF_MINIMUM_PAGE_CHARACTERS for value in page_characters):
        raise OperationalProblem("Official PMC PDF has a non-substantive page")
    if _normalize_identity_text(expected_title) not in _normalize_identity_text(
        " ".join(inspection["pages"][:2])
    ):
        raise OperationalProblem("Official PMC PDF first pages do not contain its title")
    text = text_content + "\n"
    validation = {
        "expected_pmcid": _normalise_pmcid(expected_pmcid),
        "expected_pmid": _normalise_pmid(expected_pmid),
        "identity_verified": True,
        "direct_article_body_verified": True,
        "source_link_receipt": link,
        "raw_pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "raw_pdf_bytes": len(pdf_bytes),
        "pages": page_count,
        "page_characters": page_characters,
        "characters": len(text_content),
        "poppler_versions": inspection["versions"],
        "pdf_url_inventory_sha256": inspection["urls_sha256"],
        "embedded_file_count": inspection["embedded_file_count"],
        "encrypted_but_readable": encryption_status == "yes",
    }
    return text, validation


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _official_get(
    url: str,
    *,
    accept: str,
    cookie: str = "",
) -> tuple[bytes, dict[str, Any]]:
    credential_values = review_security.read_allowed_credentials(
        AUTHORIZED_CREDENTIAL_FILE,
        label="MetaPee",
        names=review_security.NCBI_EMAIL_NAMES,
    )
    email = review_security.first_credential(
        credential_values, review_security.NCBI_EMAIL_NAMES
    )
    credential_values.clear()
    user_agent = "MetaPee-Claude-Gemini-PMC/1.0"
    if email:
        user_agent += f" (mailto:{email})"
    headers = {"User-Agent": user_agent, "Accept": accept}
    if cookie:
        headers["Cookie"] = cookie
    opener = urllib.request.build_opener(_NoRedirectHandler())
    last_error: BaseException | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers=headers, method="GET")
            with opener.open(request, timeout=60) as response:
                status = int(getattr(response, "status", response.getcode()))
                content_type = str(response.headers.get("Content-Type") or "")
                raw = response.read(PMC_MAX_RESPONSE_BYTES + 1)
                final_url = str(response.geturl())
            if status != 200 or final_url != url:
                raise OperationalProblem(
                    f"Official PMC GET failed closed: status={status}, final_url={final_url}"
                )
            if len(raw) > PMC_MAX_RESPONSE_BYTES:
                raise OperationalProblem("Official PMC response exceeds the size limit")
            return raw, {
                "url": url,
                "status": status,
                "content_type": " ".join(content_type.split()),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "accept": accept,
                "cookie_sent": bool(cookie),
                "received_at": now(),
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2**attempt)
    raise OperationalProblem(f"Official PMC GET failed after retries: {safe_error(last_error or '')}")


_POW_PATTERNS = {
    "challenge": re.compile(r'const\s+POW_CHALLENGE\s*=\s*"([A-Za-z0-9:_-]{16,256})"'),
    "difficulty": re.compile(r'const\s+POW_DIFFICULTY\s*=\s*"([1-6])"'),
    "cookie_name": re.compile(r'const\s+POW_COOKIE_NAME\s*=\s*"([A-Za-z0-9_-]+)"'),
    "cookie_path": re.compile(r'const\s+POW_COOKIE_PATH\s*=\s*"(/)"'),
}


def _pmc_pdf_cookie(challenge_raw: bytes) -> tuple[str, dict[str, Any]]:
    try:
        html = challenge_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OperationalProblem("PMC PDF proof-of-work page is not UTF-8") from exc
    if html.count("Preparing to download ...") != 2:
        raise OperationalProblem("PMC PDF proof-of-work page identity mismatch")
    values: dict[str, str] = {}
    for field, pattern in _POW_PATTERNS.items():
        matches = pattern.findall(html)
        if len(matches) != 1:
            raise OperationalProblem(f"PMC PDF proof-of-work {field} count differs")
        values[field] = matches[0]
    if (
        values["cookie_name"] != PMC_PDF_POW_COOKIE_NAME
        or values["cookie_path"] != "/"
        or int(values["difficulty"]) != PMC_PDF_POW_DIFFICULTY
    ):
        raise OperationalProblem("PMC PDF proof-of-work policy differs")
    prefix = "0" * PMC_PDF_POW_DIFFICULTY
    for nonce in range(PMC_PDF_MAX_POW_NONCE + 1):
        digest = hashlib.sha256(
            f"{values['challenge']}{nonce}".encode("utf-8")
        ).hexdigest()
        if digest.startswith(prefix):
            cookie = f"{PMC_PDF_POW_COOKIE_NAME}={values['challenge']},{nonce}"
            return cookie, {
                "challenge_sha256": hashlib.sha256(
                    values["challenge"].encode("utf-8")
                ).hexdigest(),
                "difficulty": PMC_PDF_POW_DIFFICULTY,
                "nonce": nonce,
                "solution_sha256": digest,
                "raw_challenge_sha256": hashlib.sha256(challenge_raw).hexdigest(),
            }
    raise OperationalProblem("PMC PDF proof-of-work nonce bound was exceeded")


def _download_official_pmc_pdf_once(pdf_url: str) -> tuple[bytes, dict[str, Any]]:
    raw, first = _official_get(pdf_url, accept="application/pdf")
    if first["content_type"].casefold().split(";", 1)[0] == "application/pdf":
        if not raw.startswith(b"%PDF-"):
            raise OperationalProblem("Official PMC direct PDF has an invalid container")
        return raw, {"route": "direct_official_pmc_pdf", "transport": first}
    cookie, solution = _pmc_pdf_cookie(raw)
    pdf, transport = _official_get(
        pdf_url, accept="application/pdf", cookie=cookie
    )
    if transport["content_type"].casefold().split(";", 1)[0] != "application/pdf":
        raise OperationalProblem("Official PMC PDF content type is invalid")
    return pdf, {
        "route": "official_pmc_proof_of_work_pdf",
        "challenge_transport": first,
        "solution": solution,
        "download_transport": transport,
        "cookie_value_sha256": hashlib.sha256(cookie.encode("utf-8")).hexdigest(),
    }


def _fetch_official_pmc_html(pmcid: str) -> tuple[bytes, dict[str, Any]]:
    raw, receipt = _official_get(
        f"https://pmc.ncbi.nlm.nih.gov/articles/{_normalise_pmcid(pmcid)}/",
        accept="text/html,application/xhtml+xml",
    )
    content_type = str(receipt.get("content_type") or "").casefold().split(";", 1)[0]
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise OperationalProblem("Official PMC HTML content type is invalid")
    return raw, receipt


def _fetch_official_pmc_pdf(
    source_html: bytes,
    *,
    expected_pmcid: str,
    expected_pmid: str,
    expected_title: str,
    expected_doi: str,
) -> tuple[bytes, dict[str, Any]]:
    link = _validate_pmc_pdf_link_html(
        source_html,
        expected_pmcid=expected_pmcid,
        expected_pmid=expected_pmid,
        expected_title=expected_title,
        expected_doi=expected_doi,
    )
    first, first_receipt = _download_official_pmc_pdf_once(str(link["pdf_url"]))
    time.sleep(0.36)
    second, second_receipt = _download_official_pmc_pdf_once(str(link["pdf_url"]))
    if first != second:
        raise OperationalProblem("Two independent official PMC PDF downloads differ")
    return first, {
        "source_link_receipt": link,
        "first_download": first_receipt,
        "second_download": second_receipt,
        "independent_downloads_byte_identical": True,
    }


def _save_bytes(path: Path, value: bytes) -> None:
    review_security.atomic_write_bytes(path, value)


def _source_lock(pmcid: str) -> threading.Lock:
    with _SOURCE_LOCK_GUARD:
        return _SOURCE_LOCKS.setdefault(pmcid, threading.Lock())


def _finalize_source_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    result = dict(receipt)
    result.pop("receipt_digest", None)
    result["receipt_digest"] = stable_digest(result)
    return result


def _source_paths(output_dir: Path, pmcid: str) -> dict[str, Path]:
    source_dir = output_dir / "verified_pmc_sources" / _normalise_pmcid(pmcid)
    return {
        "xml": source_dir / "article.xml",
        "rejected_xml": source_dir / "article.rejected.xml",
        "html": source_dir / "article.html",
        "rejected_html": source_dir / "article.rejected.html",
        "pdf_source_html": source_dir / "source.html",
        "pdf": source_dir / "article.pdf",
        "text": source_dir / "body.txt",
        "receipt": source_dir / "receipt.json",
    }


def _source_identity(output_dir: Path, expected_pmid: str) -> dict[str, str]:
    for filename in ("r2_input_candidates.json", "r1_pass_candidates.json"):
        packet = load_json(output_dir / filename, {})
        matches = [
            article
            for article in packet.get("articles", [])
            if isinstance(article, dict)
            and str(article.get("pmid") or "") == expected_pmid
        ]
        if len(matches) == 1:
            return {
                "pmid": expected_pmid,
                "title": " ".join(str(matches[0].get("title") or "").split()),
                "doi": " ".join(str(matches[0].get("doi") or "").split()),
            }
        if len(matches) > 1:
            raise OperationalProblem(
                f"Candidate metadata contains duplicate PMID {expected_pmid}"
            )
    return {"pmid": expected_pmid, "title": "", "doi": ""}


def _new_source_receipt(
    *,
    pmcid: str,
    pmid: str,
    text: str,
    raw: bytes,
    source: str,
    source_route: str,
    source_format: str,
    validation: dict[str, Any],
    transport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _finalize_source_receipt(
        {
            "schema_version": 2,
            "retrieved_at": now(),
            "pmcid": pmcid,
            "pmid": pmid,
            "status": "verified_full_text",
            "source": source,
            "source_route": source_route,
            "source_format": source_format,
            "identity_verified": True,
            "direct_article_body_verified": True,
            "minimum_characters_required": PMC_MIN_COMPLETE_TEXT_CHARACTERS,
            "characters": len(text),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "strict_source_validation": validation,
            "official_transport_receipt": transport,
        }
    )


def _validate_cached_pmc_source(
    output_dir: Path,
    pmcid: str,
    expected_pmid: str,
) -> tuple[str, dict[str, Any]] | None:
    paths = _source_paths(output_dir, pmcid)
    if not paths["receipt"].is_file() and not paths["text"].is_file():
        return None
    if not paths["receipt"].is_file() or not paths["text"].is_file():
        if paths["xml"].is_file():
            return None
        raise OperationalProblem(f"Cached PMC source is incomplete for {pmcid}")
    receipt = load_json(paths["receipt"], {})
    if not isinstance(receipt, dict):
        raise OperationalProblem(f"Cached PMC receipt is malformed for {pmcid}")
    supplied_digest = receipt.get("receipt_digest")
    digest_material = dict(receipt)
    digest_material.pop("receipt_digest", None)
    if supplied_digest != stable_digest(digest_material):
        if paths["xml"].is_file():
            return None
        raise OperationalProblem(f"Cached PMC receipt digest is invalid for {pmcid}")
    source_format = str(receipt.get("source_format") or "")
    identity = _source_identity(output_dir, expected_pmid)
    if source_format == "jats_xml":
        if not paths["xml"].is_file():
            raise OperationalProblem(f"Cached PMC JATS bytes are missing for {pmcid}")
        raw = paths["xml"].read_bytes()
        text, validation = _validate_pmc_jats(
            raw, expected_pmcid=pmcid, expected_pmid=expected_pmid
        )
    elif source_format == "pmc_html":
        if not paths["html"].is_file():
            raise OperationalProblem(f"Cached PMC HTML bytes are missing for {pmcid}")
        raw = paths["html"].read_bytes()
        text, validation = _validate_pmc_html(
            raw, expected_pmcid=pmcid, expected_pmid=expected_pmid
        )
    elif source_format == "pmc_pdf":
        if not paths["pdf_source_html"].is_file() or not paths["pdf"].is_file():
            raise OperationalProblem(f"Cached PMC PDF proof is incomplete for {pmcid}")
        source_html = paths["pdf_source_html"].read_bytes()
        raw = paths["pdf"].read_bytes()
        text, validation = _validate_pmc_pdf(
            source_html,
            raw,
            expected_pmcid=pmcid,
            expected_pmid=expected_pmid,
            expected_title=identity["title"],
            expected_doi=identity["doi"],
        )
    else:
        raise OperationalProblem(f"Cached PMC source format is invalid for {pmcid}")
    stored_text = paths["text"].read_text(encoding="utf-8")
    exact = {
        "pmcid": pmcid,
        "pmid": expected_pmid,
        "status": "verified_full_text",
        "identity_verified": True,
        "direct_article_body_verified": True,
        "minimum_characters_required": PMC_MIN_COMPLETE_TEXT_CHARACTERS,
        "characters": len(text),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "strict_source_validation": validation,
    }
    mismatches = [field for field, value in exact.items() if receipt.get(field) != value]
    if stored_text != text or mismatches:
        raise OperationalProblem(
            f"Cached PMC source proof mismatch for {pmcid}/{expected_pmid}: {mismatches}"
        )
    return text, receipt


def _verified_pmc_text_unlocked(
    output_dir: Path,
    pmcid: str,
    expected_pmid: str,
) -> tuple[str, dict[str, Any]]:
    pmcid = _normalise_pmcid(pmcid)
    expected_pmid = _normalise_pmid(expected_pmid)
    cached = _validate_cached_pmc_source(output_dir, pmcid, expected_pmid)
    if cached is not None:
        return cached
    paths = _source_paths(output_dir, pmcid)
    identity = _source_identity(output_dir, expected_pmid)
    errors: list[str] = []

    # One-time fail-closed migration of the earlier raw-JATS-only cache. The
    # bytes are fully revalidated and a new receipt/text commit is created;
    # no legacy derived text or proof metadata is trusted.
    if paths["xml"].is_file():
        legacy_raw = paths["xml"].read_bytes()
        text, validation = _validate_pmc_jats(
            legacy_raw,
            expected_pmcid=pmcid,
            expected_pmid=expected_pmid,
        )
        receipt = _new_source_receipt(
            pmcid=pmcid,
            pmid=expected_pmid,
            text=text,
            raw=legacy_raw,
            source="NCBI PMC EFetch JATS XML",
            source_route="cached_jats_revalidation",
            source_format="jats_xml",
            validation=validation,
        )
        _save_bytes(paths["text"], text.encode("utf-8"))
        save_json(paths["receipt"], receipt)
        return text, receipt

    raw_jats: bytes | None = None
    try:
        with review_security.configured_entrez(
            monthly_evidence_update,
            AUTHORIZED_CREDENTIAL_FILE,
            label="MetaPee",
            tool="MetaPee-Claude-Gemini-PMC",
        ):
            raw_jats = monthly_evidence_update.fetch_pmc_full_text(pmcid)
        text, validation = _validate_pmc_jats(
            raw_jats,
            expected_pmcid=pmcid,
            expected_pmid=expected_pmid,
        )
    except Exception as exc:
        errors.append(f"JATS: {safe_error(exc)}")
        if raw_jats is not None:
            _save_bytes(paths["rejected_xml"], raw_jats)
    else:
        receipt = _new_source_receipt(
            pmcid=pmcid,
            pmid=expected_pmid,
            text=text,
            raw=raw_jats,
            source="NCBI PMC EFetch JATS XML",
            source_route="efetch",
            source_format="jats_xml",
            validation=validation,
        )
        _save_bytes(paths["xml"], raw_jats)
        _save_bytes(paths["text"], text.encode("utf-8"))
        save_json(paths["receipt"], receipt)
        return text, receipt

    raw_html: bytes | None = None
    html_transport: dict[str, Any] | None = None
    try:
        raw_html, html_transport = _fetch_official_pmc_html(pmcid)
        text, validation = _validate_pmc_html(
            raw_html,
            expected_pmcid=pmcid,
            expected_pmid=expected_pmid,
        )
    except Exception as exc:
        errors.append(f"HTML: {safe_error(exc)}")
        if raw_html is not None:
            _save_bytes(paths["rejected_html"], raw_html)
    else:
        receipt = _new_source_receipt(
            pmcid=pmcid,
            pmid=expected_pmid,
            text=text,
            raw=raw_html,
            source="Canonical official PMC article HTML",
            source_route="pmc_canonical_article_html",
            source_format="pmc_html",
            validation=validation,
            transport=html_transport,
        )
        _save_bytes(paths["html"], raw_html)
        _save_bytes(paths["text"], text.encode("utf-8"))
        save_json(paths["receipt"], receipt)
        return text, receipt

    if raw_html is None:
        raise OfficialPMCSourceUnavailable(
            f"No strict complete official PMC source for {pmcid}/{expected_pmid}; "
            + " | ".join(errors)
        )
    try:
        raw_pdf, pdf_transport = _fetch_official_pmc_pdf(
            raw_html,
            expected_pmcid=pmcid,
            expected_pmid=expected_pmid,
            expected_title=identity["title"],
            expected_doi=identity["doi"],
        )
        text, validation = _validate_pmc_pdf(
            raw_html,
            raw_pdf,
            expected_pmcid=pmcid,
            expected_pmid=expected_pmid,
            expected_title=identity["title"],
            expected_doi=identity["doi"],
        )
    except Exception as exc:
        errors.append(f"PDF: {safe_error(exc)}")
        raise OfficialPMCSourceUnavailable(
            f"No strict complete official PMC source for {pmcid}/{expected_pmid}; "
            + " | ".join(errors)
        ) from exc
    receipt = _new_source_receipt(
        pmcid=pmcid,
        pmid=expected_pmid,
        text=text,
        raw=raw_pdf,
        source="Exact official PMC PDF",
        source_route="canonical_pmc_html_declared_pdf",
        source_format="pmc_pdf",
        validation=validation,
        transport={
            "canonical_html": html_transport,
            "official_pdf": pdf_transport,
            "canonical_html_sha256": hashlib.sha256(raw_html).hexdigest(),
        },
    )
    _save_bytes(paths["pdf_source_html"], raw_html)
    _save_bytes(paths["pdf"], raw_pdf)
    _save_bytes(paths["text"], text.encode("utf-8"))
    save_json(paths["receipt"], receipt)
    return text, receipt


def _verified_pmc_text(
    output_dir: Path,
    pmcid: str,
    expected_pmid: str,
) -> tuple[str, dict[str, Any]]:
    """Return one exact official source, serialized per canonical PMCID."""
    canonical = _normalise_pmcid(pmcid)
    with _source_lock(canonical):
        return _verified_pmc_text_unlocked(output_dir, canonical, expected_pmid)


def _canonical_source_manifest_entry(
    pmcid: str,
    expected_pmid: str,
    text: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Validate and normalize the exact proof used by a Gemini unit."""
    canonical_pmcid = _normalise_pmcid(pmcid)
    canonical_pmid = _normalise_pmid(expected_pmid)
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source_format = str(receipt.get("source_format") or "")
    receipt_digest = str(receipt.get("receipt_digest") or "")
    raw_sha256 = str(receipt.get("raw_sha256") or "")
    digest_material = dict(receipt)
    digest_material.pop("receipt_digest", None)
    if (
        canonical_pmcid != str(receipt.get("pmcid") or "")
        or canonical_pmid != str(receipt.get("pmid") or "")
        or receipt.get("status") != "verified_full_text"
        or receipt.get("identity_verified") is not True
        or receipt.get("direct_article_body_verified") is not True
        or source_format not in {"jats_xml", "pmc_html", "pmc_pdf"}
        or int(receipt.get("minimum_characters_required") or 0)
        != PMC_MIN_COMPLETE_TEXT_CHARACTERS
        or int(receipt.get("characters") or 0) != len(text)
        or len(text) < PMC_MIN_COMPLETE_TEXT_CHARACTERS
        or str(receipt.get("text_sha256") or "") != text_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", raw_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt_digest)
        or receipt_digest != stable_digest(digest_material)
        or not isinstance(receipt.get("strict_source_validation"), dict)
    ):
        raise OperationalProblem(
            f"Verified full-text proof is incomplete or stale for {canonical_pmcid}/{canonical_pmid}"
        )
    return {
        "status": "verified_full_text",
        "pmcid": canonical_pmcid,
        "pmid": canonical_pmid,
        "source_format": source_format,
        "source_route": str(receipt.get("source_route") or ""),
        "characters": len(text),
        "sha256": text_sha256,
        "receipt_digest": receipt_digest,
        "raw_sha256": raw_sha256,
        "receipt": receipt,
    }


def _validate_r3_source_manifest_entry(source: dict[str, Any]) -> None:
    receipt = source.get("receipt")
    if not isinstance(receipt, dict):
        raise RuntimeError("Canonical R3 source lacks its exact receipt")
    receipt_material = dict(receipt)
    supplied_digest = receipt_material.pop("receipt_digest", None)
    copied = {
        "pmcid": receipt.get("pmcid"),
        "pmid": receipt.get("pmid"),
        "source_format": receipt.get("source_format"),
        "source_route": receipt.get("source_route"),
        "characters": receipt.get("characters"),
        "sha256": receipt.get("text_sha256"),
        "receipt_digest": receipt.get("receipt_digest"),
        "raw_sha256": receipt.get("raw_sha256"),
    }
    if (
        source.get("status") != "verified_full_text"
        or source.get("source_format") not in {"jats_xml", "pmc_html", "pmc_pdf"}
        or not isinstance(source.get("characters"), int)
        or isinstance(source.get("characters"), bool)
        or int(source.get("characters") or 0) < PMC_MIN_COMPLETE_TEXT_CHARACTERS
        or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("raw_sha256") or ""))
        or not re.fullmatch(
            r"[0-9a-f]{64}", str(source.get("receipt_digest") or "")
        )
        or supplied_digest != stable_digest(receipt_material)
        or any(source.get(field) != value for field, value in copied.items())
    ):
        raise RuntimeError(
            f"Canonical R3 source manifest entry is invalid: {source.get('pmcid')}"
        )


def _register_r3_source(
    output_dir: Path,
    pmcid: str,
    expected_pmid: str,
    text: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Install one canonical exact-text proof per PMCID.

    Dynamic official receipts can differ while producing byte-identical review
    text. The first proof remains canonical for all split units. A changed text
    digest or identity fails closed rather than rebinding completed decisions.
    """
    candidate = _canonical_source_manifest_entry(
        pmcid, expected_pmid, text, receipt
    )
    path = output_dir / "r3_source_manifest.json"
    with _WRITE_LOCK:
        manifest = load_json(path, {})
        if not isinstance(manifest, dict):
            raise RuntimeError("Gemini R3 source manifest is malformed")
        existing = manifest.get(candidate["pmcid"])
        if existing:
            _validate_r3_source_manifest_entry(existing)
            if (
                existing.get("pmid") != candidate["pmid"]
                or existing.get("sha256") != candidate["sha256"]
                or existing.get("characters") != candidate["characters"]
            ):
                raise OperationalProblem(
                    f"Verified PMC source changed after canonicalization: {candidate['pmcid']}"
                )
            return existing
        manifest[candidate["pmcid"]] = candidate
        save_json(path, dict(sorted(manifest.items())))
    return candidate


def _r3_source_binding(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "pmc_source_status": "verified_full_text",
        **{
            result_field: source.get(source_field)
            for result_field, source_field in R3_SOURCE_BINDING_FIELDS.items()
        },
    }


def _validate_r3_source_proofs(
    results: Iterable[dict[str, Any]],
    source_manifest: dict[str, dict[str, Any]],
    *,
    exact_coverage: bool,
) -> None:
    rows = list(results)
    for key, source in source_manifest.items():
        if not isinstance(source, dict) or source.get("pmcid") != key:
            raise RuntimeError(f"Malformed canonical R3 source entry: {key}")
        _validate_r3_source_manifest_entry(source)
    completed_pmcids = {str(row.get("pmcid") or "") for row in rows}
    missing = completed_pmcids - set(source_manifest)
    extra = set(source_manifest) - completed_pmcids if exact_coverage else set()
    if missing or extra:
        raise RuntimeError(
            "Gemini R3 source-manifest coverage differs from completed results: "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )
    stale: list[str] = []
    for row in rows:
        source = source_manifest[str(row.get("pmcid") or "")]
        receipt = source.get("receipt")
        if not isinstance(receipt, dict):
            stale.append(f"{row.get('context_id')}:receipt")
            continue
        receipt_material = dict(receipt)
        supplied_digest = receipt_material.pop("receipt_digest", None)
        if supplied_digest != stable_digest(receipt_material):
            stale.append(f"{row.get('context_id')}:receipt_digest")
        copied = {
            "pmcid": receipt.get("pmcid"),
            "pmid": receipt.get("pmid"),
            "source_format": receipt.get("source_format"),
            "source_route": receipt.get("source_route"),
            "characters": receipt.get("characters"),
            "sha256": receipt.get("text_sha256"),
            "receipt_digest": receipt.get("receipt_digest"),
            "raw_sha256": receipt.get("raw_sha256"),
        }
        if any(source.get(field) != value for field, value in copied.items()):
            stale.append(f"{row.get('context_id')}:manifest_receipt_binding")
        expected = _r3_source_binding(source)
        mismatches = [
            field for field, value in expected.items() if row.get(field) != value
        ]
        if mismatches:
            stale.append(f"{row.get('context_id')}:{','.join(mismatches)}")
    if stale:
        raise RuntimeError(
            "Gemini R3 result/source proof mismatch: " + " | ".join(stale[:5])
        )


def _make_r3_units(
    gated: dict[str, dict[str, Any]],
    completed_context_ids: set[str],
    *,
    max_contexts_per_unit: int = R3_MAX_CONTEXTS_PER_UNIT,
) -> list[dict[str, Any]]:
    """Split each canonical PMCID into deterministic bounded context units."""
    if max_contexts_per_unit < 1:
        raise ValueError("max_contexts_per_unit must be positive")
    grouped: dict[str, list[str]] = defaultdict(list)
    pmids_by_pmcid: dict[str, set[str]] = defaultdict(set)
    for context_id, row in gated.items():
        if context_id in completed_context_ids:
            continue
        pmcid = _normalise_pmcid(str(row.get("pmcid") or ""))
        grouped[pmcid].append(context_id)
        pmids_by_pmcid[pmcid].add(str(row.get("pmid") or ""))
    units: list[dict[str, Any]] = []
    for pmcid in sorted(grouped):
        if len(pmids_by_pmcid[pmcid]) != 1:
            raise OperationalProblem(
                f"One canonical PMCID mapped to multiple PMIDs: {pmcid} -> "
                f"{sorted(pmids_by_pmcid[pmcid])}"
            )
        contexts = sorted(grouped[pmcid])
        for offset in range(0, len(contexts), max_contexts_per_unit):
            units.append(
                {
                    "pmcid": pmcid,
                    "pmid": next(iter(pmids_by_pmcid[pmcid])),
                    "context_ids": contexts[offset : offset + max_contexts_per_unit],
                }
            )
    flattened = [context_id for unit in units for context_id in unit["context_ids"]]
    expected_pending = sorted(set(gated) - completed_context_ids)
    if sorted(flattened) != expected_pending or len(flattened) != len(set(flattened)):
        raise RuntimeError("Bounded R3 units do not exactly partition pending contexts")
    return units


def _r3_prompt(contexts: dict[str, dict[str, Any]], full_text: str) -> str:
    compact = [
        {
            "context_id": item["context_id"],
            "pmid": item["pmid"],
            "pmcid": item["pmcid"],
            "title": item["title"],
            "exposure": item["exposure"],
            "cancer": item["cancer"],
            "parallel_r3_gate_reasons": item.get("parallel_r3_gate_reasons") or [],
            "missing_key_fields": item.get("missing_key_fields") or [],
            "invalid_key_fields": item.get("invalid_key_fields") or [],
            "abstract_jbi_grade": (item.get("jbi") or {}).get("grade"),
        }
        for item in contexts.values()
    ]
    pmcids = {
        _normalise_pmcid(item["pmcid"])
        for item in contexts.values()
    }
    if len(pmcids) != 1:
        raise ValueError("Each Gemini R3 prompt must contain exactly one PMCID")
    canonical_pmc_url = (
        f"https://pmc.ncbi.nlm.nih.gov/articles/{next(iter(pmcids))}/"
    )
    return (
        dual_screening.FULL_TEXT_INSTRUCTIONS
        + "\n\nCANONICAL EVIDENCE URL CONTRACT:\n"
        + "For every non-null full_text evidence object in this unit, set its "
        + f"url exactly to `{canonical_pmc_url}`. Do not use the legacy "
        + "www.ncbi.nlm.nih.gov/pmc URL or any other URL."
        + "\n\nDISTINCT STRATUM CONTRACT:\n"
        + "Use additional_extractions only when the "
        + "(gender, exposure_measurement_type) tuple differs from the primary "
        + "extraction and every prior additional row. A supplement-versus-food "
        + "or other exposure_subtype difference alone is not a distinct stratum "
        + "when both rows use dietary_intake. Never repeat the same "
        + "(gender, exposure_measurement_type) tuple."
        + "\n\nSTATUS AND REASON CONTRACT:\n"
        + "Every result, whether INCLUDE or EXCLUDE, requires one non-empty "
        + "single-sentence reason. For INCLUDE, exclusion_code must be null. "
        + "For EXCLUDE, exclusion_code must be exactly one of: "
        + ", ".join(sorted(dual_screening.EXCLUSION_CODES))
        + ". Do not invent or translate exclusion codes."
        + "\n\nCONTEXTS:\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        + "\n\nFULL ARTICLE TEXT:\n"
        + full_text
    )


def run_r3(
    output_dir: Path = OUTPUT_DIR,
    *,
    workers: int = 1,
    max_units: int = 0,
    max_contexts_per_unit: int = R3_MAX_CONTEXTS_PER_UNIT,
    client: OpenAI | None = None,
    source_loader: Callable[[Path, str, str], tuple[str, dict[str, Any]]] = _verified_pmc_text,
    resume_after_budget_reset: bool = False,
) -> dict[str, Any]:
    _migrate_budget_problem_classification(output_dir)
    _recover_interrupted_active_dispatch(output_dir)
    r2 = load_json(output_dir / "claude_r2_results.json", {})
    if not r2:
        r2 = load_json(output_dir / "claude_r2_progress.json", {})
    if not isinstance(r2.get("results"), list):
        raise RuntimeError("Claude R2 has no completed results; run --stage r2 first")
    gated = {
        item["context_id"]: item
        for item in r2["results"]
        if isinstance(item, dict) and item.get("full_text_eligible")
    }
    progress_path = output_dir / "gemini_r3_progress.json"
    progress_packet = load_json(progress_path, {})
    progress = {
        str(item.get("context_id")): item
        for item in progress_packet.get("results", [])
        if (
            isinstance(item, dict)
            and item.get("context_id") in gated
            and item.get("source_claude_r2_context_digest")
            == stable_digest(gated[item["context_id"]])
        )
    }
    source_manifest = load_json(output_dir / "r3_source_manifest.json", {})
    if not isinstance(source_manifest, dict):
        raise RuntimeError("Gemini R3 source manifest is malformed")
    _validate_r3_source_proofs(
        progress.values(), source_manifest, exact_coverage=False
    )
    terminal_source_context_ids = _terminal_r3_source_context_ids(
        output_dir,
        gated_context_ids=gated,
    )
    overlap = set(progress).intersection(terminal_source_context_ids)
    if overlap:
        raise RuntimeError(
            "A Gemini R3 context cannot have both a result and a decision-free "
            f"source deferral: {sorted(overlap)[:5]}"
        )
    units = _make_r3_units(
        gated,
        set(progress) | terminal_source_context_ids,
        max_contexts_per_unit=max_contexts_per_unit,
    )
    total_pending_units = len(units)
    if max_units > 0:
        units = units[:max_units]
    budget_exhausted = False
    budget_restart_required = (
        "gemini_r3" in _budget_blocked_stages(output_dir)
        and bool(units)
        and not resume_after_budget_reset
    )
    gateway = client or (
        _configure_client() if units and not budget_restart_required else None
    )

    def process(unit: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if gateway is None:  # pragma: no cover - guarded by non-empty units
            raise AssertionError("Gemini gateway was not configured")
        pmcid = str(unit["pmcid"])
        context_ids = list(unit["context_ids"])
        expected = {context_id: gated[context_id] for context_id in context_ids}
        expected_pmid = str(unit["pmid"])
        text, source_receipt = source_loader(output_dir, pmcid, expected_pmid)
        canonical_source = _register_r3_source(
            output_dir,
            pmcid,
            expected_pmid,
            text,
            source_receipt,
        )
        source_sha256 = str(canonical_source["sha256"])

        def validate_and_bind(payload: dict[str, Any]) -> list[dict[str, Any]]:
            rows = _validate_r3_payload_with_canonical_pmc_url(
                payload,
                expected,
                source_text=text,
                pmcid=pmcid,
            )
            binding = _r3_source_binding(canonical_source)
            for result in rows:
                result.update(binding)
                result["third_round_status"] = "COMPLETED"
                result["third_round_model"] = GEMINI_R3_MODEL
                result["source_claude_r2_context_digest"] = stable_digest(
                    gated[result["context_id"]]
                )
            return rows

        results = _call_gateway_json(
            gateway,
            output_dir=output_dir,
            stage="gemini_r3",
            model=GEMINI_R3_MODEL,
            system=dual_screening.FULL_TEXT_SYSTEM,
            prompt=_r3_prompt(expected, text),
            context_ids=context_ids,
            exposure_weights=dict(Counter(item["exposure"] for item in expected.values())),
            source_sha256=source_sha256,
            validator=validate_and_bind,
        )
        _validate_r3_source_proofs(
            results, {pmcid: canonical_source}, exact_coverage=True
        )
        _update_pmc_receipt_index(output_dir)
        # Problem identity intentionally omits the source digest, so a later
        # successful verified retrieval resolves an earlier retrieval failure.
        return _work_unit_key("gemini_r3", context_ids), results

    def persist(stopped_reason: str) -> None:
        manifest = load_json(output_dir / "r3_source_manifest.json", {})
        if not isinstance(manifest, dict):
            raise RuntimeError("Gemini R3 source manifest is malformed")
        terminal_context_ids = _terminal_r3_source_context_ids(
            output_dir,
            gated_context_ids=gated,
        )
        overlap = set(progress).intersection(terminal_context_ids)
        if overlap:
            raise RuntimeError(
                "A Gemini R3 context cannot have both a result and a "
                f"decision-free source deferral: {sorted(overlap)[:5]}"
            )
        settled_contexts = set(progress) | terminal_context_ids
        complete = settled_contexts == set(gated)
        _validate_r3_source_proofs(
            progress.values(), manifest, exact_coverage=complete
        )
        packet = {
            "schema_version": 2,
            "updated_at": now(),
            "track": "gemini",
            "model": GEMINI_R3_MODEL,
            "api_style": "chat_completions",
            "schema_transport": "prompt_embedded_schema_with_strict_local_validation",
            "protocol_version": PROTOCOL_VERSION,
            "known_gated_contexts": len(gated),
            "completed_contexts": len(progress),
            "decision_free_source_deferral_contexts": len(
                terminal_context_ids
            ),
            "settled_contexts": len(settled_contexts),
            "status": (
                "complete_with_decision_free_source_deferrals"
                if complete and terminal_context_ids
                else "complete" if complete else "partial"
            ),
            "stopped_reason": stopped_reason,
            "budget_restart_required": (
                "gemini_r3" in _budget_blocked_stages(output_dir)
            ),
            "source_manifest_digest": stable_digest(manifest),
            "results": sorted(progress.values(), key=lambda item: item["context_id"]),
        }
        save_json(progress_path, packet)
        r2_complete = (output_dir / "claude_r2_results.json").exists()
        if r2_complete and complete:
            final = dict(packet)
            final["created_at"] = now()
            final["status_counts"] = dict(Counter(item.get("full_text_status") for item in progress.values()))
            save_json(output_dir / "gemini_r3_results.json", final)
        write_outputs_and_state(output_dir, stopped_reason=stopped_reason)

    def success(key: str, results: list[dict[str, Any]]) -> None:
        with _WRITE_LOCK:
            progress.update({item["context_id"]: item for item in results})
            _resolve_problems_by_completed_contexts(
                output_dir,
                stage="gemini_r3",
                completed_context_ids=progress,
            )
            persist("r3_running")

    def error(unit: dict[str, Any], exc: BaseException) -> None:
        nonlocal budget_exhausted
        context_ids = list(unit["context_ids"])
        # Retrieval failures lack a source digest, so their durable key remains stable.
        key = _work_unit_key("gemini_r3", context_ids)
        failure_type, billing_status = _problem_record_fields(exc)
        budget_exhausted = budget_exhausted or (
            failure_type == "infrastructure_budget_exhausted"
        )
        _record_problem(
            output_dir,
            stage="gemini_r3",
            model=GEMINI_R3_MODEL,
            work_unit_key=key,
            context_ids=context_ids,
            error=exc,
            failure_type=failure_type,
            billing_status=billing_status,
        )
        _defer_twice_failed_r3_source_problem(output_dir, key)
        with _WRITE_LOCK:
            persist("r3_unresolved_operational_problems")

    def active(active_units: list[dict[str, Any]]) -> None:
        _write_active_dispatch(
            output_dir,
            stage="gemini_r3",
            model=GEMINI_R3_MODEL,
            units=[
                {
                    "pmcid": unit["pmcid"],
                    "context_ids": list(unit["context_ids"]),
                    "work_unit_key": _work_unit_key(
                        "gemini_r3", unit["context_ids"]
                    ),
                }
                for unit in active_units
            ],
        )

    if budget_restart_required:
        with _WRITE_LOCK:
            persist("r3_infrastructure_budget_exhausted_restart_required")
        return load_json(progress_path, {})

    _run_units(units, process, success, error, workers, on_active=active)
    terminal_source_context_ids = _terminal_r3_source_context_ids(
        output_dir,
        gated_context_ids=gated,
    )
    settled_context_ids = set(progress) | terminal_source_context_ids
    stopped = (
        "r3_complete_with_decision_free_source_deferrals"
        if (
            settled_context_ids == set(gated)
            and terminal_source_context_ids
            and (output_dir / "claude_r2_results.json").exists()
        )
        else "r3_complete"
        if settled_context_ids == set(gated) and (output_dir / "claude_r2_results.json").exists()
        else "r3_infrastructure_budget_exhausted_restart_required"
        if budget_exhausted
        else "r3_bounded_max_units" if max_units > 0 and total_pending_units > len(units)
        else "r3_waiting_for_r2" if not (output_dir / "claude_r2_results.json").exists()
        else "r3_unresolved_operational_problems"
    )
    with _WRITE_LOCK:
        persist(stopped)
    return load_json(progress_path, {})


def _update_pmc_receipt_index(output_dir: Path) -> None:
    receipts = [
        load_json(path, {})
        for path in sorted((output_dir / "verified_pmc_sources").glob("*/receipt.json"))
    ]
    receipts = [item for item in receipts if isinstance(item, dict) and item.get("pmcid")]
    save_json(
        output_dir / "pmc_source_receipts.json",
        {
            "schema_version": 1,
            "updated_at": now(),
            "verified_sources": len(receipts),
            "sources": receipts,
        },
    )


def _allocated_tokens(total: int, weights: dict[str, int]) -> dict[str, int]:
    return dual_screening._allocated_tokens(total, weights)


def _write_cost_files(output_dir: Path) -> None:
    exposures = catalog_exposures()
    receipts_packet = load_json(output_dir / "gateway_receipts.json", {})
    receipts = [item for item in receipts_packet.get("receipts", []) if isinstance(item, dict)]
    unknown_packet = load_json(output_dir / "unknown_billing_receipts.json", {})
    unknown_receipts = [
        item
        for item in unknown_packet.get("receipts", [])
        if isinstance(item, dict)
    ]
    receipts.extend(unknown_receipts)
    unknown_interrupted_receipts = sum(
        item.get("billing_status") == "unknown_after_process_interruption"
        or item.get("cost_header_status") == "unknown_after_process_interruption"
        for item in receipts
    )
    unknown_billing_receipts = sum(
        str(item.get("billing_status") or "").startswith("unknown_after_")
        or str(item.get("cost_header_status") or "").startswith("unknown_after_")
        for item in receipts
    )
    invalid_or_unaccepted_receipts = sum(
        item.get("scientific_result_accepted") is False
        and not str(item.get("billing_status") or "").startswith("unknown_after_")
        for item in receipts
    )
    totals: dict[str, dict[str, Any]] = {
        exposure: {
            "claude_r2_calls": 0,
            "claude_r2_input_tokens": 0,
            "claude_r2_cached_input_tokens": 0,
            "claude_r2_output_tokens": 0,
            "claude_r2_reasoning_tokens": 0,
            "claude_r2_cost_usd": Decimal("0"),
            "claude_r2_cost_unavailable_calls": 0,
            "claude_r2_unknown_billing_calls": 0,
            "gemini_r3_calls": 0,
            "gemini_r3_input_tokens": 0,
            "gemini_r3_cached_input_tokens": 0,
            "gemini_r3_output_tokens": 0,
            "gemini_r3_reasoning_tokens": 0,
            "gemini_r3_cost_usd": Decimal("0"),
            "gemini_r3_cost_unavailable_calls": 0,
            "gemini_r3_unknown_billing_calls": 0,
        }
        for exposure in exposures
    }
    for receipt in receipts:
        stage = str(receipt.get("stage") or "")
        prefix = "claude_r2" if stage == "claude_r2" else "gemini_r3" if stage == "gemini_r3" else ""
        if not prefix:
            continue
        weights = {
            str(name): int(weight)
            for name, weight in (receipt.get("exposure_weights") or {}).items()
            if str(name) in totals and int(weight) > 0
        }
        usage = receipt.get("usage") or {}
        for exposure in weights:
            totals[exposure][f"{prefix}_calls"] += 1
            if (
                str(receipt.get("billing_status") or "").startswith("unknown_after_")
                or str(receipt.get("cost_header_status") or "").startswith(
                    "unknown_after_"
                )
            ):
                totals[exposure][f"{prefix}_unknown_billing_calls"] += 1
        for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
            allocated = _allocated_tokens(int(usage.get(field) or 0), weights)
            for exposure, value in allocated.items():
                totals[exposure][f"{prefix}_{field}"] += value
        cost_raw = receipt.get("response_cost_usd")
        if cost_raw in (None, ""):
            for exposure in weights:
                totals[exposure][f"{prefix}_cost_unavailable_calls"] += 1
        else:
            try:
                cost = Decimal(str(cost_raw))
            except InvalidOperation:
                for exposure in weights:
                    totals[exposure][f"{prefix}_cost_unavailable_calls"] += 1
            else:
                if not cost.is_finite() or cost < 0:
                    for exposure in weights:
                        totals[exposure][f"{prefix}_cost_unavailable_calls"] += 1
                else:
                    denominator = Decimal(sum(weights.values()))
                    for exposure, weight in weights.items():
                        totals[exposure][f"{prefix}_cost_usd"] += cost * Decimal(weight) / denominator

    lines: list[str] = []
    json_rows: list[dict[str, Any]] = []
    for exposure in exposures:
        row = totals[exposure]
        serial: dict[str, Any] = {"exposure": exposure}
        fields: list[str] = [exposure]
        for prefix in ("claude_r2", "gemini_r3"):
            for field in ("calls", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
                value = int(row[f"{prefix}_{field}"])
                serial[f"{prefix}_{field}"] = value
                fields.append(f"{prefix}_{field}={value}")
            unavailable = int(row[f"{prefix}_cost_unavailable_calls"])
            unknown = int(row[f"{prefix}_unknown_billing_calls"])
            cost = row[f"{prefix}_cost_usd"]
            cost_text = "NA" if unavailable else format(cost.quantize(Decimal("0.0000000001")), "f")
            serial[f"{prefix}_cost_usd"] = None if unavailable else cost_text
            serial[f"{prefix}_cost_unavailable_calls"] = unavailable
            serial[f"{prefix}_unknown_billing_calls"] = unknown
            serial[f"{prefix}_token_provenance"] = (
                "unavailable_after_unknown_billing_attempt"
                if unknown and int(row[f"{prefix}_calls"]) == unknown
                else "partial_provider_reported_with_unknown_billing_attempt"
                if unknown
                else "provider_reported_via_cornell_gateway_response_usage"
                if int(row[f"{prefix}_calls"])
                else "no_calls"
            )
            serial[f"{prefix}_cost_provenance"] = (
                "unknown_after_unreceipted_attempt"
                if unknown
                else "partial_gateway_header_or_unavailable"
                if unavailable
                else "gateway_header_reported"
                if int(row[f"{prefix}_calls"])
                else "no_calls"
            )
            fields.append(f"{prefix}_cost_usd={cost_text}")
            fields.append(f"{prefix}_cost_unavailable_calls={unavailable}")
            fields.append(f"{prefix}_unknown_billing_calls={unknown}")
            fields.append(
                f"{prefix}_token_provenance={serial[f'{prefix}_token_provenance']}"
            )
            fields.append(
                f"{prefix}_cost_provenance={serial[f'{prefix}_cost_provenance']}"
            )
        lines.append("\t".join(fields))
        json_rows.append(serial)
    review_security.atomic_write_text(COST_PATH, "\n".join(lines) + "\n")
    save_json(
        output_dir / "cost_accounting.json",
        {
            "schema_version": 1,
            "updated_at": now(),
            "exposure_rows": len(json_rows),
            "token_provenance": "gateway response usage as reported; no token approximation",
            "cost_provenance": (
                "x-litellm-response-cost when present; missing or interrupted-active "
                "receipts are NA, never estimated or inferred as zero"
            ),
            "unknown_interrupted_receipts": unknown_interrupted_receipts,
            "unknown_billing_receipts": unknown_billing_receipts,
            "invalid_or_unaccepted_receipts_retained_for_cost": (
                invalid_or_unaccepted_receipts
            ),
            "rows": json_rows,
        },
    )


def write_outputs_and_state(output_dir: Path, *, stopped_reason: str) -> dict[str, Any]:
    _migrate_budget_problem_classification(output_dir)
    cache_receipt_audit = review_security.audit_cache_receipt_acceptance(
        output_dir
    )
    r1 = load_json(output_dir / "r1_pass_packet.json", {})
    metadata_repair = load_json(
        output_dir / "r2_official_metadata_repair.json", {}
    )
    r2_final = load_json(output_dir / "claude_r2_results.json", {})
    r2_progress = load_json(output_dir / "claude_r2_progress.json", {})
    current_r2_source_digest = stable_digest(
        {
            "source_first_input_digest": r1.get("input_digest"),
            "r2_input_candidates_digest": metadata_repair.get(
                "r2_input_candidates_digest"
            ),
        }
    )
    use_repaired_binding = metadata_repair.get("status") == "complete"
    r2 = (
        r2_final
        if (
            r2_final.get("source_r2_input_digest") == current_r2_source_digest
            if use_repaired_binding
            else r2_final.get("source_first_input_digest") == r1.get("input_digest")
        )
        else r2_progress
        if (
            r2_progress.get("source_r2_input_digest") == current_r2_source_digest
            if use_repaired_binding
            else r2_progress.get("source_first_input_digest") == r1.get("input_digest")
        )
        else {}
    )
    r3_final = load_json(output_dir / "gemini_r3_results.json", {})
    r3_progress = load_json(output_dir / "gemini_r3_progress.json", {})
    r3 = r3_final if r3_final else r3_progress
    problem_packet = load_json(output_dir / "screening_problem_ledger.json", {})
    problems = [item for item in problem_packet.get("unresolved", []) if isinstance(item, dict)]
    budget_blocked_stages = _budget_blocked_stages(
        output_dir, packet=problem_packet
    )
    if budget_blocked_stages:
        stopped_stage = (
            "r2" if "claude_r2" in budget_blocked_stages else "r3"
        )
        stopped_reason = (
            f"{stopped_stage}_infrastructure_budget_exhausted_restart_required"
        )
    unknown_billing_receipts = [
        item
        for item in load_json(
            output_dir / "unknown_billing_receipts.json", {}
        ).get("receipts", [])
        if isinstance(item, dict)
    ]
    unknown_billing_receipts.extend(
        item
        for item in load_json(output_dir / "gateway_receipts.json", {}).get(
            "receipts", []
        )
        if isinstance(item, dict)
        and item.get("billing_status") == "unknown_after_process_interruption"
    )
    r2_results = [item for item in r2.get("results", []) if isinstance(item, dict)]
    r3_results = [item for item in r3.get("results", []) if isinstance(item, dict)]
    science_gated = [item for item in r2_results if item.get("needs_full_text")]
    gated = [item for item in science_gated if item.get("full_text_eligible")]
    gated_by_context = {str(item.get("context_id")): item for item in gated}
    terminal_source_records = _load_terminal_r3_source_deferrals(output_dir)
    terminal_source_context_ids = _terminal_r3_source_context_ids(
        output_dir,
        gated_context_ids=gated_by_context,
    )
    claude_exclusions = [
        {
            "context_id": item.get("context_id"),
            "pmid": item.get("pmid"),
            "exposure": item.get("exposure"),
            "cancer": item.get("cancer"),
            "stage": "claude_r2",
            "model": CLAUDE_R2_MODEL,
            "exclusion_code": item.get("exclusion_code"),
            "reason": item.get("reason"),
        }
        for item in r2_results
        if item.get("eligibility") == "EXCLUDE"
    ]
    gemini_exclusions = [
        {
            "context_id": item.get("context_id"),
            "pmid": item.get("pmid"),
            "exposure": item.get("exposure"),
            "cancer": item.get("cancer"),
            "stage": "gemini_r3",
            "model": GEMINI_R3_MODEL,
            "exclusion_code": item.get("exclusion_code"),
            "reason": item.get("reason"),
        }
        for item in r3_results
        if item.get("full_text_status") == "EXCLUDE"
    ]
    no_pmcid_deferrals = [
        {
            "context_id": item.get("context_id"),
            "pmid": item.get("pmid"),
            "pmcid": "",
            "exposure": item.get("exposure"),
            "cancer": item.get("cancer"),
            "stage": "gemini_r3",
            "model": GEMINI_R3_MODEL,
            "gate_reasons": item.get("parallel_r3_gate_reasons") or [],
            "disposition": "decision_free_deferred_no_pmcid",
            "reason": (
                "The context meets the scientific Gemini R3 gate but has no PMCID, "
                "so no verified PMC full-text decision can be made."
            ),
        }
        for item in r2_results
        if item.get("needs_full_text") and not item.get("pmcid")
    ]
    verified_source_failure_deferrals = []
    for record in terminal_source_records.values():
        for context_id in record["context_ids"]:
            item = gated_by_context[context_id]
            verified_source_failure_deferrals.append(
                {
                    "context_id": context_id,
                    "pmid": item.get("pmid"),
                    "pmcid": item.get("pmcid"),
                    "exposure": item.get("exposure"),
                    "cancer": item.get("cancer"),
                    "stage": "gemini_r3",
                    "model": GEMINI_R3_MODEL,
                    "gate_reasons": item.get("parallel_r3_gate_reasons") or [],
                    "disposition": R3_SOURCE_DEFERRAL_DISPOSITION,
                    "source_verification_attempts": int(record["attempts"]),
                    "source_failure_work_unit_key": record["work_unit_key"],
                    "operational_error": record.get("error"),
                    "reason": (
                        "Two strict retrieval attempts could not verify a complete "
                        "official PMC full text, so no Gemini R3 decision was made."
                    ),
                }
            )
    save_json(output_dir / "claude_r2_exclusions.json", {
        "schema_version": 1,
        "updated_at": now(),
        "exclusions": claude_exclusions,
    })
    save_json(output_dir / "gemini_r3_exclusions.json", {
        "schema_version": 1,
        "updated_at": now(),
        "exclusions": gemini_exclusions,
    })
    save_json(output_dir / "exclusions.json", {
        "schema_version": 1,
        "updated_at": now(),
        "exclusions": claude_exclusions + gemini_exclusions,
        "note": "Operational failures are unresolved work, never exclusions.",
    })
    save_json(output_dir / "r3_source_deferrals.json", {
        "schema_version": 1,
        "updated_at": now(),
        "decision_free_no_pmcid_count": len(no_pmcid_deferrals),
        "decision_free_twice_verified_source_failure_count": len(
            verified_source_failure_deferrals
        ),
        "decision_free_total_count": (
            len(no_pmcid_deferrals) + len(verified_source_failure_deferrals)
        ),
        "deferrals": sorted(
            no_pmcid_deferrals + verified_source_failure_deferrals,
            key=lambda item: str(item.get("context_id") or ""),
        ),
        "note": (
            "These contexts remain included at R2 and have no fabricated R3 "
            "decision; strict-source failures become terminal only after two "
            "verified retrieval attempts."
        ),
    })
    for stage, path in (
        ("claude_r2", output_dir / "claude_r2_errors.json"),
        ("gemini_r3", output_dir / "gemini_r3_errors.json"),
    ):
        stage_problems = [item for item in problems if item.get("stage") == stage]
        save_json(path, {
            "schema_version": 1,
            "updated_at": now(),
            "unresolved_count": len(stage_problems),
            "unresolved": stage_problems,
            "disposition": "retry_later_not_excluded",
        })
    _write_cost_files(output_dir)
    expected_r2 = int(r1.get("pass") or 0)
    r3_context_ids = {str(item.get("context_id")) for item in r3_results}
    if len(r3_context_ids) != len(r3_results):
        raise RuntimeError("Gemini R3 results contain duplicate context IDs")
    overlap = r3_context_ids.intersection(terminal_source_context_ids)
    if overlap:
        raise RuntimeError(
            "A Gemini R3 context cannot have both a result and a decision-free "
            f"source deferral: {sorted(overlap)[:5]}"
        )
    settled_r3_context_ids = r3_context_ids | terminal_source_context_ids
    complete = bool(
        expected_r2
        and len(r2_results) == expected_r2
        and settled_r3_context_ids == set(gated_by_context)
        and not problems
    )
    terminal_status = (
        "complete_with_decision_free_source_deferrals"
        if complete and (no_pmcid_deferrals or terminal_source_context_ids)
        else "complete"
        if complete
        else "in_progress"
    )
    state = {
        "schema_version": 1,
        "updated_at": now(),
        "protocol_version": PROTOCOL_VERSION,
        "publication_date_end": PUBLICATION_DATE_END,
        "models": {"r2": CLAUDE_R2_MODEL, "r3": GEMINI_R3_MODEL},
        "model_transport": {
            "api_style": "chat_completions",
            "schema_transport": "prompt_embedded_schema_with_strict_local_validation",
            "response_format_omitted": True,
        },
        "catalog_exposures": len(catalog_exposures()),
        "r1_pass_contexts": expected_r2,
        "r2_official_metadata_repair_status": metadata_repair.get("status")
        or "not_started",
        "r2_official_metadata_repaired_pmids": metadata_repair.get(
            "target_pmids", []
        ),
        "r2_official_metadata_repair_request_count": int(
            metadata_repair.get("request_count") or 0
        ),
        "claude_r2_completed_contexts": len(r2_results),
        "claude_r2_exclusions": len(claude_exclusions),
        "gemini_r3_science_gate_contexts": len(science_gated),
        "gemini_r3_pmc_eligible_contexts": len(gated),
        "gemini_r3_completed_contexts": len(r3_results),
        "gemini_r3_settled_contexts": len(settled_r3_context_ids),
        "gemini_r3_exclusions": len(gemini_exclusions),
        "gemini_r3_decision_free_no_pmcid_deferrals": len(no_pmcid_deferrals),
        "gemini_r3_decision_free_twice_verified_source_failure_deferrals": len(
            terminal_source_context_ids
        ),
        "unresolved_operational_problems": len(problems),
        "model_cache_receipt_acceptance_consistency": cache_receipt_audit,
        "interrupted_active_unknown_billing_receipts": len(
            unknown_billing_receipts
        ),
        "status": terminal_status,
        "stopped_reason": stopped_reason,
        "budget_restart_required": bool(budget_blocked_stages),
        "budget_blocked_stages": budget_blocked_stages,
        "budget_restart_guard": (
            "No new model work is dispatched for a blocked stage until the "
            "operator confirms that the Cornell budget was reset with "
            "--resume-after-budget-reset."
        ),
        "resume_command": (
            "python scripts/run_claude_gemini_parallel_review.py --stage all "
            "--batch-size 8 --workers 1"
            + (" --resume-after-budget-reset" if budget_blocked_stages else "")
        ),
        "canonical_chatgpt_artifacts_modified": False,
        "provider_route": {
            "r2": {
                "requested_provider_model": CLAUDE_R2_MODEL,
                "route": "Cornell gateway Chat Completions API",
                "direct_provider_credential": "absent",
                "direct_provider_note": "No usable direct Anthropic credential is available.",
            },
            "r3": {
                "requested_provider_model": GEMINI_R3_MODEL,
                "route": "Cornell gateway Chat Completions API",
                "direct_provider_credential": "absent",
                "direct_provider_note": "No usable direct Google Gemini credential is available.",
            },
            "authorization": "User approved Cornell gateway credentials for Claude R2 and Gemini R3.",
            "token_accounting": (
                "Provider-reported response usage through Cornell is retained when present; "
                "missing usage is unavailable and never approximated."
            ),
        },
    }
    save_json(output_dir / "run_state.json", state)
    return state


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=("prepare", "r2", "r3", "all", "status"),
        default="status",
    )
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument(
        "--r3-max-contexts-per-unit",
        type=int,
        default=R3_MAX_CONTEXTS_PER_UNIT,
        help="Hard Gemini R3 context ceiling for each split PMCID call unit.",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--max-units",
        type=int,
        default=0,
        help="Bound newly attempted work units for smoke/resume; 0 means unlimited.",
    )
    parser.add_argument(
        "--resume-after-budget-reset",
        action="store_true",
        help=(
            "Explicitly confirm that the Cornell hard budget was replenished and "
            "permit a stage blocked by a prior 429 budget_exceeded response to retry."
        ),
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be between 1 and 16")
    if args.r3_max_contexts_per_unit < 1:
        parser.error("--r3-max-contexts-per-unit must be positive")
    if args.max_units < 0:
        parser.error("--max-units cannot be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        last_stage: dict[str, Any] = {}
        _migrate_budget_problem_classification(args.output_dir)
        _recover_interrupted_active_dispatch(args.output_dir)
        if args.stage in {"prepare", "all"}:
            prepare_r1_lineage(args.output_dir)
        if args.stage in {"r2", "all"}:
            r2 = run_r2(
                args.output_dir,
                batch_size=args.batch_size,
                workers=args.workers,
                max_units=args.max_units,
                resume_after_budget_reset=args.resume_after_budget_reset,
            )
            last_stage = r2
            if args.stage == "all" and r2.get("status") != "complete":
                print(json.dumps(write_outputs_and_state(args.output_dir, stopped_reason=r2.get("stopped_reason") or "r2_partial"), indent=2))
                return 2
        if args.stage in {"r3", "all"}:
            last_stage = run_r3(
                args.output_dir,
                workers=args.workers,
                max_units=args.max_units,
                max_contexts_per_unit=args.r3_max_contexts_per_unit,
                resume_after_budget_reset=args.resume_after_budget_reset,
            )
        state = write_outputs_and_state(args.output_dir, stopped_reason=(
            "status_only"
            if args.stage == "status"
            else str(last_stage.get("stopped_reason") or f"{args.stage}_invocation_finished")
        ))
        print(json.dumps(state, indent=2))
        return 0 if str(state.get("status") or "").startswith("complete") or args.stage in {"prepare", "status"} else 2
    except KeyboardInterrupt:
        state = write_outputs_and_state(
            args.output_dir,
            stopped_reason="interrupted_by_operator_after_bounded_active_dispatch",
        )
        print(json.dumps(state, indent=2))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
