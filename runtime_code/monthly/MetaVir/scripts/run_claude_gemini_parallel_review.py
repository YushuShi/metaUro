#!/usr/bin/env python3
"""Run the isolated Claude-R2 / Gemini-R3 validation track for MetaVir.

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
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

from dotenv import dotenv_values
from openai import OpenAI


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import audit_blocked_pmc_sources  # noqa: E402
from scripts import dual_screening  # noqa: E402
from scripts import monthly_evidence_update  # noqa: E402


DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "claude_gemini_screening" / "all_history_through_2026-08-29"
COST_PATH = REPO_ROOT / "ClaudeGeminiCost.txt"
AUTHORIZED_CREDENTIAL_FILE = REPO_ROOT / "mykey.env"

CLAUDE_R2_MODEL = "claude-opus-5"
GEMINI_R3_MODEL = "gemini-3.7-flash"
PROTOCOL_VERSION = "metavir-claude-gemini-parallel-v1"
PUBLICATION_DATE_END = "2026-08-29"
R2_MAX_CONTEXTS_PER_UNIT = 12
R3_MAX_CONTEXTS_PER_UNIT = 8
CORNELL_GATEWAY_HOST = "api.ai.it.cornell.edu"
CORNELL_GATEWAY_BASE_PATHS = frozenset({"", "/", "/v1", "/v1/"})
GATEWAY_KEY_NAMES = ("CORNELL_API_KEY", "Cornell_API_Key")
GATEWAY_BASE_URL_NAMES = (
    "CORNELL_API_BASE_URL",
    "Cornell_API_Base_URL",
    "API_Base_URI",
    "API_BASE_URI",
)
NCBI_EMAIL_NAMES = ("NCBI_EMAIL", "PUBMED_EMAIL")
NCBI_KEY_NAMES = ("NCBI_API_KEY", "PUBMED_API_KEY")
ALLOWED_CREDENTIAL_NAMES = frozenset(
    GATEWAY_KEY_NAMES + GATEWAY_BASE_URL_NAMES + NCBI_EMAIL_NAMES + NCBI_KEY_NAMES
)

R3_SOURCE_BINDING_FIELDS = {
    "full_text_characters_reviewed": "characters",
    "pmc_source_format": "source_format",
    "pmc_source_sha256": "sha256",
    "pmc_source_receipt_digest": "receipt_digest",
    "pmc_source_raw_sha256": "raw_sha256",
}

# These repairs are deliberately narrower than ordinary model-output cleanup.
# They recover already-paid Gemini responses only when the model used an exact
# legacy spelling whose scientific meaning is unambiguous.  Every applied
# repair is persisted next to the untouched raw response.
R3_CACHE_RECOVERY_CONTRACT_VERSION = "metavir-gemini-r3-cache-recovery-v1"
MODEL_CACHE_ACCEPTANCE_RECONCILIATION_VERSION = (
    "metavir-model-cache-acceptance-reconciliation-v1"
)
R3_EXPOSURE_MISMATCH_CODE_ALIASES = {
    "EXCLUDE_EXPOSURE": "S2-EXPOSURE-MISMATCH",
    "WRONG_EXPOSURE": "S2-EXPOSURE-MISMATCH",
    "E3_wrong_exposure": "S2-EXPOSURE-MISMATCH",
    "EXCLUDE_WRONG_EXPOSURE": "S2-EXPOSURE-MISMATCH",
}
R3_HR_EFFECT_TYPE_ALIASES = frozenset(
    {
        "shr",
        "sub-hazard",
        "subhazard",
        "subdistribution hazard",
        "sub-hazard ratio",
        "subhazard ratio",
        "subdistribution hazard ratio",
    }
)
R3_FORCE_FRESH_PMIDS = frozenset({"21518693"})
PMC_HTML_TRANSIENT_RETRY_REASONS = frozenset(
    {
        # PMC can transiently serve its anti-automation shell at the canonical
        # article URL with HTTP 200.  The shell does not contain the article's
        # exact head-scoped canonical identity, so it remains invalid; a later
        # independent response must pass the unchanged strict HTML validator.
        "canonical_url_identity_mismatch",
    }
)
PMC_HTML_TRANSIENT_MAX_ATTEMPTS = 3
PMC_HTML_TRANSIENT_RETRY_DELAYS_SECONDS = (1.0, 2.0)
_LEGACY_PMC_URL_RE = re.compile(
    r"https://www\.ncbi\.nlm\.nih\.gov/pmc/articles/(PMC[0-9]+)/"
)

CATALOG_PATHS = (
    DATA_DIR / "synonyms_cache.json",
    DATA_DIR / "additionalExposure.json",
    DATA_DIR / "additionalExposureV2.json",
)

# The order is part of the scientific lineage contract.  An all-history packet
# replaces older contexts for every exposure it actually searched.  A delta or
# reclassification packet only replaces matching context IDs.
R1_SOURCES = (
    {
        "name": "all_history_through_2026-06-30",
        "coverage": "baseline",
    },
    {
        "name": "2026-07-01_2026-07-31",
        "coverage": "context_overlay",
    },
    {
        "name": "all_history_through_2026-08-29",
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
    {
        "name": "vitamin_b6_plp_diet_v2_all_history_through_2026-08-29",
        "coverage": "all_history_exposure_replacement",
    },
)

_WRITE_LOCK = threading.RLock()
_SOURCE_LOCK_GUARD = threading.Lock()
_SOURCE_LOCKS: dict[str, threading.Lock] = {}
_SEALED_REPAIR_SOURCES: dict[str, dict[str, Any]] | None = None
_REDACTION_MARKER = "[REDACTED]"
_SENSITIVE_LABEL = (
    r"(?:token|x-api-key|[A-Za-z0-9_.-]+cookie|"
    r"[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|auth[_-]?token|session[_-]?token|"
    r"client[_-]?secret|secret|password|passwd|passphrase)"
    r"[A-Za-z0-9_.-]*)"
)
_QUOTED_SECRET = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])([\"']?{_SENSITIVE_LABEL}[\"']?\s*[:=]\s*)"
    r"([\"'])([^\"'\r\n]*)([\"'])"
)
_UNQUOTED_SECRET = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])([\"']?{_SENSITIVE_LABEL}[\"']?\s*[:=]\s*)"
    r"(?![\"'])(\[REDACTED\]|[^\s,;}}\]\)&]+)"
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"([\"']?(?:authorization|proxy[-_ ]authorization)[\"']?\s*[:=]\s*)"
    r"(?:\[REDACTED\]|\"[^\"\r\n]*\"|'[^'\r\n]*'|"
    r"(?:bearer|basic)\s+[^\s,;}}\]]+|[^\s,;}}\]]+)"
)
_BEARER_CREDENTIAL = re.compile(
    r"(?i)(\b(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]+)"
)
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"(?:-----END [^-\r\n]*PRIVATE KEY-----|$)",
    re.DOTALL,
)
_COOKIE_CREDENTIAL = re.compile(
    r"(?im)([\"']?(?:set[-_ ]?cookie|cookie)[\"']?\s*[:=]\s*)"
    r"(?:\[REDACTED\]|[^\r\n]*)"
)
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


def now() -> str:
    return datetime.now().astimezone().isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _durable_atomic_write(
    path: Path,
    writer: Callable[[Any], None],
    *,
    binary: bool,
) -> None:
    """Publish one complete file without following a predictable temp symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        os.fchmod(descriptor, 0o644)
        handle = (
            os.fdopen(descriptor, "wb")
            if binary
            else os.fdopen(descriptor, "w", encoding="utf-8")
        )
        descriptor_open = False
        with handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
            finally:
                os.close(directory_descriptor)
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)


def _durable_atomic_write_bytes(path: Path, payload: bytes) -> None:
    _durable_atomic_write(path, lambda handle: handle.write(payload), binary=True)


def _durable_atomic_write_text(path: Path, payload: str) -> None:
    _durable_atomic_write(path, lambda handle: handle.write(payload), binary=False)


def save_json(path: Path, payload: Any) -> None:
    def write(handle: Any) -> None:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    _durable_atomic_write(path, write, binary=False)


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
    """Return one persistable error string with common credentials removed."""
    value = str(error)
    value = _PRIVATE_KEY.sub(_REDACTION_MARKER, value)
    value = _COOKIE_CREDENTIAL.sub(rf"\1{_REDACTION_MARKER}", value)
    value = _AUTHORIZATION_VALUE.sub(rf"\1{_REDACTION_MARKER}", value)
    value = _BEARER_CREDENTIAL.sub(rf"\1{_REDACTION_MARKER}", value)
    value = _QUOTED_SECRET.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}"
            f"{_REDACTION_MARKER}{match.group(4)}"
        ),
        value,
    )
    value = _UNQUOTED_SECRET.sub(rf"\1{_REDACTION_MARKER}", value)
    value = _KNOWN_TOKEN.sub(_REDACTION_MARKER, value)
    return re.sub(r"\s+", " ", value).strip()[:1000]


def _canonical_pmc_url(pmcid: str) -> str:
    return f"https://pmc.ncbi.nlm.nih.gov/articles/{_normalise_pmcid(pmcid)}/"


def _normalization_event(
    *,
    event_type: str,
    context_id: str,
    field: str,
    before: Any,
    after: Any,
    rule: str,
) -> dict[str, Any]:
    """Return a deterministic audit event for one exact cache repair."""
    return {
        "contract_version": R3_CACHE_RECOVERY_CONTRACT_VERSION,
        "event_type": event_type,
        "context_id": str(context_id or ""),
        "field": field,
        "before": before,
        "after": after,
        "rule": rule,
    }


def _normalise_gemini_r3_payload(
    payload: dict[str, Any],
    *,
    expected_pmcid: str,
    source_text: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Deep-copy and narrowly canonicalize one Gemini R3 response.

    The raw provider output is never modified.  This function only repairs
    exact, scientifically lossless aliases and source metadata that can be
    proven against the verified full text supplied to the model.
    """
    normalized = copy.deepcopy(payload)
    events: list[dict[str, Any]] = []
    canonical_pmcid = _normalise_pmcid(expected_pmcid)
    canonical_url = _canonical_pmc_url(canonical_pmcid)
    compact_source_text = dual_screening.one_line(source_text)
    results = normalized.get("results")
    if not isinstance(results, list):
        return normalized, events

    for row_index, row in enumerate(results):
        if not isinstance(row, dict):
            continue
        context_id = str(row.get("context_id") or "")
        code = row.get("exclusion_code")
        mapped_code = R3_EXPOSURE_MISMATCH_CODE_ALIASES.get(code)
        if mapped_code:
            row["exclusion_code"] = mapped_code
            events.append(
                _normalization_event(
                    event_type="exclusion_code_alias",
                    context_id=context_id,
                    field=f"results[{row_index}].exclusion_code",
                    before=code,
                    after=mapped_code,
                    rule="exact_exposure_mismatch_alias",
                )
            )

        extraction = row.get("extraction")
        if isinstance(extraction, dict):
            raw_effect_type = extraction.get("effect_type")
            compact_effect_type = dual_screening.one_line(raw_effect_type).casefold()
            if compact_effect_type in R3_HR_EFFECT_TYPE_ALIASES:
                extraction["effect_type"] = "HR"
                events.append(
                    _normalization_event(
                        event_type="effect_type_alias",
                        context_id=context_id,
                        field=f"results[{row_index}].extraction.effect_type",
                        before=raw_effect_type,
                        after="HR",
                        rule="exact_subdistribution_hazard_alias",
                    )
                )

            evidence = extraction.get("evidence")
            if isinstance(evidence, dict):
                for evidence_field, entry in evidence.items():
                    if not isinstance(entry, dict):
                        continue
                    raw_url = entry.get("url")
                    match = (
                        _LEGACY_PMC_URL_RE.fullmatch(str(raw_url or ""))
                        if raw_url is not None
                        else None
                    )
                    if match and _normalise_pmcid(match.group(1)) == canonical_pmcid:
                        entry["url"] = canonical_url
                        events.append(
                            _normalization_event(
                                event_type="legacy_pmc_url",
                                context_id=context_id,
                                field=(
                                    f"results[{row_index}].extraction.evidence."
                                    f"{evidence_field}.url"
                                ),
                                before=raw_url,
                                after=canonical_url,
                                rule="exact_legacy_ncbi_pmc_url_for_expected_pmcid",
                            )
                        )

                    excerpt = dual_screening.one_line(entry.get("excerpt"))
                    location = dual_screening.one_line(entry.get("location"))
                    if (
                        entry.get("source") == "abstract"
                        and entry.get("url") == canonical_url
                        and excerpt
                        and excerpt in compact_source_text
                        and re.search(r"\babstract\b", location, re.IGNORECASE)
                    ):
                        entry["source"] = "full_text"
                        events.append(
                            _normalization_event(
                                event_type="verified_source_label",
                                context_id=context_id,
                                field=(
                                    f"results[{row_index}].extraction.evidence."
                                    f"{evidence_field}.source"
                                ),
                                before="abstract",
                                after="full_text",
                                rule=(
                                    "exact_excerpt_in_verified_full_text_with_exact_"
                                    "pmcid_url_and_abstract_location"
                                ),
                            )
                        )

        jbi = row.get("jbi")
        if isinstance(jbi, dict):
            checklist = str(jbi.get("checklist_type") or "").casefold()
            answers = jbi.get("answers")
            checklist_total = dual_screening.JBI_TOTALS.get(checklist)
            if isinstance(answers, dict) and checklist_total is not None:
                for question_number in range(checklist_total + 1, 14):
                    question = f"q{question_number}"
                    raw_answer = answers.get(question)
                    if raw_answer in {"Yes", "No", "Unclear"}:
                        answers[question] = "NA"
                        events.append(
                            _normalization_event(
                                event_type="jbi_beyond_checklist",
                                context_id=context_id,
                                field=(
                                    f"results[{row_index}].jbi.answers.{question}"
                                ),
                                before=raw_answer,
                                after="NA",
                                rule="question_is_beyond_declared_jbi_checklist",
                            )
                        )
    return normalized, events


def _gateway_response_schema(stage: str) -> dict[str, Any]:
    """Return the model schema, with an exact R3 exclusion-code enum."""
    schema = copy.deepcopy(dual_screening.response_schema(
        "second" if stage == "claude_r2" else "full_text"
    ))
    if stage == "gemini_r3":
        code_schema = schema["schema"]["properties"]["results"]["items"][
            "properties"
        ]["exclusion_code"]
        code_schema["enum"] = [None, *sorted(dual_screening.EXCLUSION_CODES)]
    return schema


def _requires_fresh_r3_adjudication(context_ids: Iterable[str]) -> bool:
    return any(
        str(context_id).split("|", 1)[0] in R3_FORCE_FRESH_PMIDS
        for context_id in context_ids
    )


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
            if name in ownership:
                raise RuntimeError(
                    f"Exposure {name!r} appears in both {ownership[name]} and {path.name}"
                )
            ownership[name] = path.name
            exposures.append(name)
    exposures = sorted(exposures, key=lambda value: (value.casefold(), value))
    forbidden = {
        name for name in exposures if name.casefold() in {"diet pill", "diet pills"}
    }
    if forbidden:
        raise RuntimeError(
            f"Removed Diet pills exposure leaked into current catalogs: {sorted(forbidden)}"
        )
    if len(exposures) != 446:
        raise RuntimeError(
            f"The current three-catalog union must contain exactly 446 exposures, got {len(exposures)}"
        )
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
        first_path = source_dir / "chatgpt_first_screen.json"
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
        if spec["name"] in {
            "all_history_through_2026-06-30",
            "2026-07-01_2026-07-31",
        }:
            # The legacy packets recorded only the count (228), not the names.
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
    if len(conflicts) != 10:
        raise RuntimeError(f"Expected exactly 10 deliberate R1 conflicts, got {len(conflicts)}")

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
                "cancer": str(result.get("cancer") or "Prostate cancer"),
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
    superseded_source_decisions = [
        {
            "context_id": context_id,
            "source_decision_lineage": records,
            "selected": False,
            "disposition": (
                "superseded_by_later_all_history_exposure_replacement_without_a_current_candidate"
            ),
        }
        for context_id, records in sorted(lineage.items())
        if context_id not in selected
    ]
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
        "superseded_source_decision_contexts": len(superseded_source_decisions),
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
    save_json(output_dir / "r1_superseded_source_decisions.json", {
        "schema_version": 1,
        "created_at": deterministic_created_at,
        "results": superseded_source_decisions,
        "note": (
            "These earlier decisions remain auditable but are not Claude R2 inputs because "
            "a later all-history exposure packet did not retain the context."
        ),
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
    issues = dual_screening.required_quantitative_issues(item.get("extraction"))
    if issues:
        reasons.append("important quantitative evidence remains missing or invalid: " + ", ".join(issues))
    return reasons


def apply_parallel_r3_gate(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    if isinstance(item.get("extraction"), dict):
        result["extraction"] = copy.deepcopy(item["extraction"])
        dual_screening.apply_poolability_policy(result["extraction"])
        result["missing_key_fields"] = dual_screening.missing_key_fields(result["extraction"])
        result["invalid_key_fields"] = dual_screening.invalid_key_fields(result["extraction"])
    result["poolability_policy_version"] = dual_screening.POOLABILITY_POLICY_VERSION
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


def _authorized_credentials_file() -> Path:
    """Return the one local credential file authorized for this runner."""
    candidate = AUTHORIZED_CREDENTIAL_FILE
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise RuntimeError("Authorized MetaVir credential file is not readable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("Authorized MetaVir credential file must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("Authorized MetaVir credential path is not a regular file")
    # Return the lexical authorized path. The O_NOFOLLOW open plus fstat below
    # performs the race-safe final-component validation; resolving here could
    # follow a symlink introduced after lstat().
    return candidate.absolute()


def _allowed_credential_values(
    credentials_file: Path | None = None,
    *,
    names: Iterable[str] = ALLOWED_CREDENTIAL_NAMES,
) -> dict[str, str]:
    """Read only allowlisted values from local mykey.env without exporting them."""
    requested_names = frozenset(names)
    if not requested_names <= ALLOWED_CREDENTIAL_NAMES:
        raise RuntimeError("Credential loader requested a non-allowlisted name")
    # Monthly Actions explicitly supplies repository/environment credentials;
    # there is no local secret file on a fresh runner. Keep the standalone
    # historical review's file-only contract outside this authorized mode.
    if os.getenv("MONTHLY_AUTOMATION_REQUIRE_GATE", "").strip().lower() == "true":
        return {name: str(os.environ[name]).strip() for name in requested_names
                if str(os.environ.get(name) or "").strip()}
    credential_path = credentials_file or _authorized_credentials_file()
    open_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(credential_path, open_flags)
    except OSError as exc:
        raise RuntimeError("Authorized credential file could not be opened safely") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("Authorized MetaVir credential path is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            parsed = dotenv_values(stream=handle, interpolate=False)
        values = {
            name: str(parsed[name]).strip()
            for name in requested_names
            if parsed.get(name) is not None and str(parsed[name]).strip()
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if "parsed" in locals():
            parsed.clear()
    return values


def _first_credential_value(
    values: dict[str, str], names: Iterable[str]
) -> str:
    for name in names:
        value = str(values.get(name) or "").strip()
        if value:
            return value
    return ""


def _validated_cornell_base_url(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("Cornell gateway base URL is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or str(parsed.hostname or "").casefold() != CORNELL_GATEWAY_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or "?" in value
        or "#" in value
        or parsed.path not in CORNELL_GATEWAY_BASE_PATHS
    ):
        raise RuntimeError(
            "Cornell gateway base URL must use the approved HTTPS host and base path"
        )
    normalized_path = "/v1" if parsed.path.rstrip("/") == "/v1" else ""
    return f"https://{CORNELL_GATEWAY_HOST}{normalized_path}"


@contextlib.contextmanager
def _configured_local_entrez() -> Iterable[None]:
    """Scope PubMed/PMC credentials without copying dotenv values to os.environ."""
    values = _allowed_credential_values(names=NCBI_EMAIL_NAMES + NCBI_KEY_NAMES)
    email = _first_credential_value(values, NCBI_EMAIL_NAMES)
    requested_key = _first_credential_value(values, NCBI_KEY_NAMES)
    if not email:
        raise RuntimeError("NCBI email is not configured in MetaVir/mykey.env")
    entrez = monthly_evidence_update.Entrez
    with monthly_evidence_update._ENTREZ_CONFIGURATION_LOCK:
        previous = (
            getattr(entrez, "email", None),
            getattr(entrez, "api_key", None),
            getattr(entrez, "tool", None),
            getattr(entrez, "max_tries", None),
        )
        rejected_digest = monthly_evidence_update._ENTREZ_REJECTED_KEY_DIGEST
        if (
            requested_key
            and rejected_digest
            and hashlib.sha256(requested_key.encode("utf-8")).hexdigest()
            == rejected_digest
        ):
            requested_key = ""
        if monthly_evidence_update.ncbi_resilience.is_throttled(requested_key):
            requested_key = ""
        entrez.email = email
        monthly_evidence_update.ncbi_resilience.configure_key(entrez, requested_key)
        entrez.tool = "MetaVir-Claude-Gemini-PMC"
        entrez.max_tries = 1
        try:
            yield
        finally:
            entrez.email, previous_key, entrez.tool, entrez.max_tries = previous
            monthly_evidence_update.ncbi_resilience.configure_key(entrez, previous_key)
            values.clear()


def _configure_client() -> OpenAI:
    credentials_file = _authorized_credentials_file()
    values = _allowed_credential_values(
        credentials_file,
        names=GATEWAY_KEY_NAMES + GATEWAY_BASE_URL_NAMES,
    )
    key = _first_credential_value(values, GATEWAY_KEY_NAMES)
    base_url = _first_credential_value(values, GATEWAY_BASE_URL_NAMES)
    if not key or not base_url:
        raise RuntimeError(
            "Cornell gateway key/base URL are not configured in MetaVir/mykey.env"
        )
    try:
        return OpenAI(
            api_key=key,
            base_url=_validated_cornell_base_url(base_url),
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


def _validation_history_snapshot(
    record: dict[str, Any],
    *,
    transition: str,
) -> dict[str, Any]:
    results = record.get("validated_results")
    return {
        "history_sequence": len(record.get("validation_history") or []) + 1,
        "superseded_at": now(),
        "transition": transition,
        "validation_status": str(record.get("validation_status") or ""),
        "validation_error": str(record.get("validation_error") or ""),
        "scientific_result_accepted": bool(
            record.get("scientific_result_accepted")
        ),
        "validation_contract_version": str(
            record.get("validation_contract_version") or "legacy"
        ),
        "normalization_events": copy.deepcopy(
            record.get("normalization_events") or []
        ),
        "validated_results_digest": (
            stable_digest(results) if isinstance(results, list) else ""
        ),
        "output_sha256": str(record.get("output_sha256") or ""),
    }


def _transition_cached_validation(
    record: dict[str, Any],
    *,
    validation_status: str,
    validation_error: str = "",
    validated_results: list[dict[str, Any]] | None = None,
    normalization_events: list[dict[str, Any]] | None = None,
    transition: str,
) -> None:
    """Change validation disposition while retaining immutable prior state."""
    history = [
        copy.deepcopy(item)
        for item in record.get("validation_history", [])
        if isinstance(item, dict)
    ]
    history.append(_validation_history_snapshot(record, transition=transition))
    record["validation_history"] = history
    record["validation_status"] = validation_status
    record["validation_error"] = safe_error(validation_error) if validation_error else ""
    record["scientific_result_accepted"] = validation_status == "valid"
    record["validation_contract_version"] = R3_CACHE_RECOVERY_CONTRACT_VERSION
    record["normalization_events"] = copy.deepcopy(normalization_events or [])
    if validated_results is None:
        record.pop("validated_results", None)
    else:
        record["validated_results"] = validated_results


def _verify_cached_raw_output(record: dict[str, Any]) -> str:
    output_text = record.get("output_text")
    if not isinstance(output_text, str) or not output_text:
        raise dual_screening.ScreeningValidationError(
            "Cached model response has no raw output text"
        )
    expected_sha256 = str(record.get("output_sha256") or "")
    actual_sha256 = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    if expected_sha256 != actual_sha256:
        raise dual_screening.ScreeningValidationError(
            "Cached raw output SHA-256 does not match its immutable receipt"
        )
    return output_text


def _record_forced_fresh_cache_bypass(record: dict[str, Any]) -> None:
    history = [
        copy.deepcopy(item)
        for item in record.get("cache_revalidation_history", [])
        if isinstance(item, dict)
    ]
    if not any(
        item.get("contract_version") == R3_CACHE_RECOVERY_CONTRACT_VERSION
        and item.get("disposition") == "forced_fresh_adjudication_required"
        for item in history
    ):
        history.append(
            {
                "recorded_at": now(),
                "contract_version": R3_CACHE_RECOVERY_CONTRACT_VERSION,
                "disposition": "forced_fresh_adjudication_required",
                "reason": (
                    "PMID 21518693 reports period prevalence in the verified full "
                    "text and must receive a fresh model adjudication."
                ),
                "raw_output_sha256": str(record.get("output_sha256") or ""),
            }
        )
    record["cache_revalidation_history"] = history


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
    payload_normalizer: Callable[
        [dict[str, Any]], tuple[dict[str, Any], list[dict[str, Any]]]
    ]
    | None = None,
    force_fresh_cached_adjudication: bool = False,
) -> list[dict[str, Any]]:
    """Call Cornell Chat Completions with local strict JSON validation.

    Claude Opus 5 on Cornell Bedrock rejects the Responses ``text.format``
    field.  Both reviewer models therefore receive the schema in the prompt;
    ``response_format`` is intentionally omitted and the existing strict local
    validators remain authoritative.
    """
    schema = _gateway_response_schema(stage)
    base_unit_key = _work_unit_key(stage, context_ids, source_sha256)
    response_dir = output_dir / "model_response_cache" / stage
    response_dir.mkdir(parents=True, exist_ok=True)
    existing_paths = sorted(
        response_dir.glob(f"{base_unit_key}_*.json"),
        key=lambda path: (_cache_attempt(path), path.name),
        reverse=True,
    )

    def parse_and_normalize_output_text(
        output_text: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        payload = dual_screening.json_from_text(output_text)
        normalization_events: list[dict[str, Any]] = []
        if payload_normalizer is not None:
            payload, normalization_events = payload_normalizer(payload)
        return payload, normalization_events

    last_feedback = ""
    for path in existing_paths:
        cached = load_json(path, {})
        if (
            force_fresh_cached_adjudication
            and cached.get("validation_contract_version")
            != R3_CACHE_RECOVERY_CONTRACT_VERSION
        ):
            _record_forced_fresh_cache_bypass(cached)
            with _WRITE_LOCK:
                save_json(path, cached)
            last_feedback = (
                "Prior cached outputs are ineligible for local recovery because "
                "this PMID requires fresh full-text adjudication."
            )
            continue
        if cached.get("validation_status") == "valid" and isinstance(
            cached.get("validated_results"), list
        ):
            return cached["validated_results"]
        if cached.get("validation_status") in {"received", "invalid"}:
            normalization_events: list[dict[str, Any]] = []
            try:
                output_text = _verify_cached_raw_output(cached)
                payload, normalization_events = parse_and_normalize_output_text(
                    output_text
                )
                results = validator(payload)
            except Exception as exc:
                _transition_cached_validation(
                    cached,
                    validation_status="invalid",
                    validation_error=safe_error(exc),
                    normalization_events=normalization_events,
                    transition="cached_attempt_revalidated",
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
                    normalization_events=normalization_events,
                    transition="cached_attempt_revalidated",
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

    for _repair in range(2):
        attempt = max(
            [_cache_attempt(path) for path in existing_paths]
            + [
                _cache_attempt(path)
                for path in response_dir.glob(f"{base_unit_key}_*.json")
            ]
            + [0]
        ) + 1
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
            if raw_response is not None:
                # Cornell may return a billable raw response whose SDK parse
                # fails.  Preserve the recognized cost header and mark the
                # attempt invalid instead of silently losing its accounting.
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
                            "validation_status": "invalid",
                            "failure_type": "raw_response_parse_failed",
                            "output_text": "",
                        },
                    )
                    _append_gateway_receipt(output_dir, receipt)
                    _write_cost_files(output_dir)
                last_feedback = safe_error(exc)
                continue
            raise OperationalProblem(f"{stage} gateway call failed: {safe_error(exc)}") from exc

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
        normalization_events = []
        try:
            payload, normalization_events = parse_and_normalize_output_text(
                output_text
            )
            results = validator(payload)
        except Exception as exc:
            _transition_cached_validation(
                record,
                validation_status="invalid",
                validation_error=safe_error(exc),
                normalization_events=normalization_events,
                transition="new_gateway_attempt_validated",
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
        _transition_cached_validation(
            record,
            validation_status="valid",
            validated_results=results,
            normalization_events=normalization_events,
            transition="new_gateway_attempt_validated",
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
    """Annotate one billed/received attempt without ever dropping its cost.

    Gateway usage belongs to the call even when its JSON or scientific schema
    is rejected.  The receipt is therefore retained and only its validation
    disposition is updated in place.
    """
    path = output_dir / "gateway_receipts.json"
    packet = load_json(path, {})
    changed = False
    for receipt in packet.get("receipts", []):
        if not isinstance(receipt, dict) or receipt.get("receipt_id") != receipt_id:
            continue
        history = [
            copy.deepcopy(item)
            for item in receipt.get("validation_history", [])
            if isinstance(item, dict)
        ]
        history.append(
            {
                "history_sequence": len(history) + 1,
                "superseded_at": now(),
                "validation_status": str(receipt.get("validation_status") or ""),
                "validation_error": str(receipt.get("validation_error") or ""),
                "scientific_result_accepted": bool(
                    receipt.get("scientific_result_accepted")
                ),
                "validation_contract_version": str(
                    receipt.get("validation_contract_version") or "legacy"
                ),
                "response_cost_usd": receipt.get("response_cost_usd"),
                "output_sha256": str(receipt.get("output_sha256") or ""),
            }
        )
        receipt["validation_history"] = history
        receipt["validation_status"] = validation_status
        receipt["scientific_result_accepted"] = scientific_result_accepted
        receipt["validation_error"] = safe_error(validation_error) if validation_error else ""
        receipt["validation_contract_version"] = R3_CACHE_RECOVERY_CONTRACT_VERSION
        changed = True
        break
    if not changed:
        raise RuntimeError(f"Gateway receipt disappeared before validation: {receipt_id}")
    packet["updated_at"] = now()
    save_json(path, packet)


def _model_cache_protected_digest(record: dict[str, Any]) -> str:
    """Digest every cache field except the one legacy acceptance boolean."""
    protected = copy.deepcopy(record)
    protected.pop("scientific_result_accepted", None)
    return stable_digest(protected)


def _load_required_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError(f"{label} must not be a symlink: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is not a JSON object: {path}")
    return value


def reconcile_model_cache_acceptance(
    output_dir: Path = OUTPUT_DIR,
    *,
    write: bool = True,
) -> dict[str, Any]:
    """Reconcile one exact legacy cache flag without changing science or raw data.

    Older success branches marked the gateway receipt accepted but left the raw
    cache record's initially-false ``scientific_result_accepted`` flag behind.
    Only that proven false-to-true metadata delta is permitted here. Any
    identity, status, output, result-set, or opposite-direction discrepancy
    fails closed before a file is changed.
    """
    with _WRITE_LOCK:
        receipts_path = output_dir / "gateway_receipts.json"
        receipts_packet = (
            _load_required_json_object(receipts_path, "Gateway receipt ledger")
            if receipts_path.exists()
            else {"receipts": []}
        )
        receipts: dict[str, dict[str, Any]] = {}
        for raw_receipt in receipts_packet.get("receipts") or []:
            if not isinstance(raw_receipt, dict):
                raise RuntimeError("Gateway receipt ledger contains a non-object row")
            receipt_id = str(raw_receipt.get("receipt_id") or "")
            if not receipt_id or receipt_id in receipts:
                raise RuntimeError(
                    "Gateway receipt ledger has a missing or duplicate receipt_id"
                )
            receipts[receipt_id] = raw_receipt

        cache_root = output_dir / "model_response_cache"
        cache_paths = sorted(cache_root.glob("*/*.json")) if cache_root.exists() else []
        seen_receipt_ids: set[str] = set()
        planned: list[dict[str, Any]] = []
        consistent_by_stage: Counter[str] = Counter()
        scanned_by_stage: Counter[str] = Counter()
        unexpected: list[str] = []
        identity_fields = (
            "stage",
            "model_requested",
            "work_unit_key",
            "attempt",
            "context_ids",
            "input_digest",
            "output_sha256",
            "response_cost_usd",
        )

        for path in cache_paths:
            record = _load_required_json_object(path, "Model response cache")
            stage = path.parent.name
            if stage not in {"claude_r2", "gemini_r3"}:
                unexpected.append(f"{path}: unexpected cache stage {stage!r}")
                continue
            scanned_by_stage[stage] += 1
            receipt_id = str(record.get("receipt_id") or "")
            receipt = receipts.get(receipt_id)
            if receipt is None:
                unexpected.append(f"{path}: no matching gateway receipt")
                continue
            if receipt_id in seen_receipt_ids:
                unexpected.append(f"{path}: receipt_id is used by multiple cache files")
                continue
            seen_receipt_ids.add(receipt_id)
            differing_identity = [
                field
                for field in identity_fields
                if record.get(field) != receipt.get(field)
            ]
            if differing_identity:
                unexpected.append(
                    f"{path}: cache/receipt identity differs for "
                    + ",".join(differing_identity)
                )
                continue

            validation_status = str(record.get("validation_status") or "")
            receipt_validation_status = str(receipt.get("validation_status") or "")
            if validation_status != receipt_validation_status:
                unexpected.append(f"{path}: cache/receipt validation_status differs")
                continue
            receipt_accepted = receipt.get("scientific_result_accepted")
            cache_accepted = record.get("scientific_result_accepted")
            if not isinstance(receipt_accepted, bool) or not isinstance(
                cache_accepted, bool
            ):
                unexpected.append(f"{path}: acceptance field is not boolean")
                continue
            if receipt_accepted != (receipt_validation_status == "valid"):
                unexpected.append(
                    f"{path}: receipt acceptance contradicts validation_status"
                )
                continue

            output_text = record.get("output_text")
            output_sha256 = str(record.get("output_sha256") or "")
            if isinstance(output_text, str) and output_text:
                if (
                    hashlib.sha256(output_text.encode("utf-8")).hexdigest()
                    != output_sha256
                ):
                    unexpected.append(f"{path}: raw output SHA-256 mismatch")
                    continue
            if validation_status == "valid":
                if not isinstance(output_text, str) or not output_text:
                    unexpected.append(f"{path}: valid cache lacks raw output text")
                    continue
                results = record.get("validated_results")
                if not isinstance(results, list):
                    unexpected.append(f"{path}: valid cache lacks validated_results")
                    continue
                result_context_ids = sorted(
                    str(item.get("context_id") or "")
                    for item in results
                    if isinstance(item, dict)
                )
                expected_context_ids = sorted(
                    str(value) for value in (record.get("context_ids") or [])
                )
                if (
                    len(result_context_ids) != len(results)
                    or result_context_ids != expected_context_ids
                ):
                    unexpected.append(
                        f"{path}: validated_results context coverage differs"
                    )
                    continue

            if cache_accepted == receipt_accepted:
                consistent_by_stage[stage] += 1
                continue
            if not (
                validation_status == "valid"
                and cache_accepted is False
                and receipt_accepted is True
            ):
                unexpected.append(
                    f"{path}: acceptance mismatch is not the approved legacy false-to-true pattern"
                )
                continue
            planned.append(
                {
                    "path": path,
                    "record": record,
                    "stage": stage,
                    "receipt_id": receipt_id,
                    "protected_digest": _model_cache_protected_digest(record),
                    "validated_results_digest": stable_digest(
                        record.get("validated_results")
                    ),
                    "cache_file_sha256_before": file_digest(path),
                    "output_sha256": output_sha256,
                }
            )

        if unexpected:
            raise RuntimeError(
                "Model-cache acceptance reconciliation failed closed: "
                + " | ".join(unexpected[:10])
            )

        unmatched_receipts = sorted(set(receipts) - seen_receipt_ids)
        stage_plan = Counter(item["stage"] for item in planned)
        reconciliation_records: list[dict[str, Any]] = []
        if write:
            for item in planned:
                path = item["path"]
                updated = copy.deepcopy(item["record"])
                updated["scientific_result_accepted"] = True
                if _model_cache_protected_digest(updated) != item["protected_digest"]:
                    raise RuntimeError(
                        f"Protected cache content changed before reconciliation: {path}"
                    )
                save_json(path, updated)
                reloaded = _load_required_json_object(
                    path, "Reconciled model response cache"
                )
                if (
                    reloaded.get("scientific_result_accepted") is not True
                    or _model_cache_protected_digest(reloaded)
                    != item["protected_digest"]
                ):
                    raise RuntimeError(
                        f"Atomic cache reconciliation verification failed: {path}"
                    )
                reconciliation_records.append(
                    {
                        "cache_path": str(path.relative_to(output_dir)),
                        "receipt_id": item["receipt_id"],
                        "stage": item["stage"],
                        "before_scientific_result_accepted": False,
                        "after_scientific_result_accepted": True,
                        "output_sha256": item["output_sha256"],
                        "validated_results_digest": item[
                            "validated_results_digest"
                        ],
                        "protected_record_digest_before_and_after": item[
                            "protected_digest"
                        ],
                        "cache_file_sha256_before": item[
                            "cache_file_sha256_before"
                        ],
                        "cache_file_sha256_after": file_digest(path),
                    }
                )

        result = {
            "contract_version": MODEL_CACHE_ACCEPTANCE_RECONCILIATION_VERSION,
            "audited_at": now(),
            "write_requested": write,
            "cache_files_scanned": len(cache_paths),
            "gateway_receipts_scanned": len(receipts),
            "cache_files_by_stage": dict(sorted(scanned_by_stage.items())),
            "already_consistent_by_stage": dict(
                sorted(consistent_by_stage.items())
            ),
            "reconciliation_required_by_stage": dict(sorted(stage_plan.items())),
            "reconciled_by_stage": (
                dict(sorted(stage_plan.items())) if write else {}
            ),
            "reconciled_cache_files": len(planned) if write else 0,
            "remaining_acceptance_mismatches": 0 if write else len(planned),
            "unmatched_gateway_receipts_without_cache": len(unmatched_receipts),
            "unexpected_mismatches": 0,
            "raw_outputs_changed": False,
            "output_hashes_changed": False,
            "validated_results_changed": False,
            "cost_or_usage_changed": False,
            "validation_history_changed": False,
            "scientific_decisions_changed": False,
            "reconciliation_records": reconciliation_records,
        }
        if write and planned:
            audit_path = output_dir / "model_cache_acceptance_reconciliation.json"
            prior = load_json(audit_path, {})
            prior_runs = [
                copy.deepcopy(item)
                for item in prior.get("runs", [])
                if isinstance(item, dict)
            ] if isinstance(prior, dict) else []
            runs = prior_runs + [result]
            save_json(
                audit_path,
                {
                    "schema_version": 1,
                    "contract_version": (
                        MODEL_CACHE_ACCEPTANCE_RECONCILIATION_VERSION
                    ),
                    "updated_at": now(),
                    "run_count": len(runs),
                    "cumulative_reconciled_cache_files": sum(
                        int(item.get("reconciled_cache_files") or 0)
                        for item in runs
                    ),
                    "latest_run": result,
                    "runs": runs,
                },
            )
        return result


def _record_problem(
    output_dir: Path,
    *,
    stage: str,
    model: str,
    work_unit_key: str,
    context_ids: list[str],
    error: BaseException | str,
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


def _completed_context_ids_for_stage(output_dir: Path, stage: str) -> set[str]:
    """Return only contexts with a durable scientific result checkpoint."""
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
    except (OSError, ValueError):
        return False
    return True


def _recover_interrupted_active_dispatch(output_dir: Path) -> int:
    """Preserve stale active calls as decision-free, billing-unknown work.

    A process can stop after the gateway accepted a request but before a response
    receipt or scientific result was durably checkpointed. Such a call is never
    treated as an exclusion and never assigned zero cost. A later successful
    context checkpoint resolves the problem while history remains auditable.
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
        problem_key = f"interrupted-{recovery_id}"
        receipt_status = "unknown_after_process_interruption"
        billing_status = "unknown_after_process_interruption"
        if recovery_id not in known_recovery_ids:
            _record_problem(
                output_dir,
                stage=stage,
                model=model,
                work_unit_key=problem_key,
                context_ids=context_ids,
                error=(
                    "The process stopped while this unit was active; no scientific "
                    "disposition was inferred."
                ),
                metadata={
                    "failure_type": "interrupted_active_call",
                    "original_work_unit_key": work_unit_key,
                    "response_receipt_status": receipt_status,
                    "billing_status": billing_status,
                    "recovery_id": recovery_id,
                    "recovered_at": recovered_at,
                },
            )
            known_recovery_ids.add(recovery_id)
        # Even if an earlier attempt for this logical unit has a durable
        # receipt, the active attempt itself cannot be identified after a hard
        # stop. Preserve a separate unknown receipt instead of attributing a
        # prior attempt's known billing to the interrupted request.
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
                "exposure_weights": dict(sorted(weights.items(), key=lambda item: item[0].casefold())),
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
                "status": receipt_status,
                "recovery_id": recovery_id,
            }
        )

    history = [item for item in state.get("recovery_history", []) if isinstance(item, dict)]
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


def _run_units(
    units: list[Any],
    process: Callable[[Any], tuple[str, list[dict[str, Any]]]],
    on_success: Callable[[str, list[dict[str, Any]]], None],
    on_error: Callable[[Any, Exception], None],
    workers: int,
    on_active: Callable[[list[Any]], None] | None = None,
) -> None:
    """Run a bounded rolling queue with a pre-submit active-call journal.

    At most ``workers`` futures exist.  A unit is made durable through
    ``on_active`` before it can reach the executor, so an interruption between
    scheduling and response checkpointing remains conservatively billable and
    decision-free on resume.
    """
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
                else:
                    on_success(key, results)
            normal_completion = True
        finally:
            # Preserve the last active snapshot on BaseException. The next
            # process records response/billing as unknown rather than silently
            # asserting that no gateway call survived the interruption.
            if normal_completion and on_active is not None:
                on_active([])
        return
    iterator = iter(enumerate(units))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures: dict[concurrent.futures.Future[Any], tuple[int, Any]] = {}
    active: dict[int, Any] = {}
    normal_completion = False

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
        # Do not roll back the journal if submit itself is interrupted: the
        # callable may already have entered the executor before submit returns.
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
                else:
                    on_success(key, results)
                futures.pop(future)
                active.pop(index, None)
                if on_active is not None:
                    on_active(active_in_source_order())
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
            item for item in previous.get("recovery_history", []) if isinstance(item, dict)
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


def _make_r2_units(
    expected: dict[str, dict[str, Any]],
    article_by_pmid: dict[str, dict[str, Any]],
    completed_context_ids: set[str],
    *,
    batch_size: int,
    max_contexts_per_unit: int = R2_MAX_CONTEXTS_PER_UNIT,
) -> list[dict[str, Any]]:
    """Pack R2 deterministically by articles while capping exact contexts.

    One dense article may be repeated across multiple units, but each context
    appears exactly once.  ``batch_size`` retains its user-facing meaning as
    the maximum article group considered together; the hard context ceiling
    prevents an unusually dense article from overflowing a model response.
    """
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
    if set(ordered_pmids) - set(article_by_pmid):
        raise RuntimeError("R2 context metadata lacks its source article")

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
    return units


def _r2_candidates_with_official_metadata(
    output_dir: Path,
    candidates: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Repair blank R2 titles from exact official PubMed records.

    The immutable R1 packet is never rewritten.  A separately sealed R2 input
    packet records the exact PMIDs fetched, request count, official fields,
    source digest, and repaired-input digest.  PubMed may genuinely omit an
    abstract or MeSH headings; those empty values are retained and reported,
    not treated as an operational failure.
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
        with _configured_local_entrez():
            records, request_count = monthly_evidence_update.entrez_fetch(target_pmids)
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
            raise RuntimeError(
                f"Official PubMed record still has no title for PMID {pmid}"
            )
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
) -> dict[str, Any]:
    _recover_interrupted_active_dispatch(output_dir)
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
        (output_dir / "claude_r2_results.json").unlink(missing_ok=True)
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
    gateway = client or (_configure_client() if units else None)

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
        context_ids = ids_for(unit)
        key = _work_unit_key("claude_r2", context_ids)
        _record_problem(
            output_dir,
            stage="claude_r2",
            model=CLAUDE_R2_MODEL,
            work_unit_key=key,
            context_ids=context_ids,
            error=exc,
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

    _run_units(units, process, success, error, workers, on_active=active)
    stopped = (
        "r2_complete"
        if len(progress) == len(expected)
        else "r2_bounded_max_units" if max_units > 0 and total_pending_units > len(units)
        else "r2_unresolved_operational_problems"
    )
    with _WRITE_LOCK:
        persist(stopped)
    return load_json(progress_path, {})


def _normalise_pmcid(pmcid: str) -> str:
    value = audit_blocked_pmc_sources.normalize_pmcid(pmcid)
    if not value:
        raise ValueError(f"Invalid PMCID: {pmcid!r}")
    return value


def _source_lock(pmcid: str) -> threading.Lock:
    with _SOURCE_LOCK_GUARD:
        return _SOURCE_LOCKS.setdefault(pmcid, threading.Lock())


def _finalize_source_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    result = dict(receipt)
    result.pop("receipt_digest", None)
    result["receipt_digest"] = stable_digest(result)
    return result


def _sanitized_transport_receipt(transport: Any) -> dict[str, Any]:
    """Retain source provenance without persisting an HTTP/PoW cookie value."""
    if not isinstance(transport, dict):
        return {}
    result = {
        field: transport.get(field)
        for field in (
            "url",
            "final_url",
            "redirected",
            "http_status",
            "content_type",
            "request_accept",
        )
        if field in transport
    }
    attempts: list[dict[str, Any]] = []
    for raw_attempt in transport.get("attempts") or []:
        if not isinstance(raw_attempt, dict):
            continue
        attempt = {
            field: raw_attempt.get(field)
            for field in (
                "attempt",
                "http_status",
                "bytes",
                "sha256",
                "redirected",
                "content_type",
            )
            if field in raw_attempt
        }
        if "error" in raw_attempt:
            attempt["error"] = safe_error(str(raw_attempt.get("error") or ""))
        attempts.append(attempt)
    result["attempts"] = attempts
    cookie = str(transport.get("request_cookie") or "")
    result["request_cookie_present"] = bool(cookie)
    if cookie:
        result["request_cookie_sha256"] = hashlib.sha256(
            cookie.encode("utf-8")
        ).hexdigest()
    return result


def _probe_pmc_html_with_bounded_transient_retry(
    pmcid: str,
    expected_pmid: str,
    *,
    user_agent: str,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], str, bytes, list[dict[str, Any]]]:
    """Retry only a known transient PMC HTML shell, never its validation.

    Each request remains cookie-free and must independently pass the existing
    exact URL, PMCID, PMID, article-container, and substantive-body checks.
    Failed receipts are retained only as sanitized transport/validation audit
    records; their response bodies and any credential material are not saved.
    """
    attempt_records: list[dict[str, Any]] = []
    for attempt_number in range(1, PMC_HTML_TRANSIENT_MAX_ATTEMPTS + 1):
        route, text, raw = audit_blocked_pmc_sources.probe_pmc_html(
            pmcid,
            expected_pmid,
            user_agent=user_agent,
        )
        validation = route.get("validation") or {}
        transport = route.get("transport") or {}
        if (
            route.get("route") != "pmc_canonical_article_html"
            or str(transport.get("request_cookie") or "")
        ):
            raise OperationalProblem(
                "Canonical PMC HTML retry route violated its cookie-free identity scope"
            )
        attempt_records.append(
            {
                "attempt": attempt_number,
                "validation": copy.deepcopy(validation),
                "transport": _sanitized_transport_receipt(transport),
            }
        )
        reason = str(validation.get("reason") or "")
        if (
            validation.get("valid") is True
            or reason not in PMC_HTML_TRANSIENT_RETRY_REASONS
            or attempt_number == PMC_HTML_TRANSIENT_MAX_ATTEMPTS
        ):
            return route, text, raw, attempt_records
        sleep(PMC_HTML_TRANSIENT_RETRY_DELAYS_SECONDS[attempt_number - 1])
    raise AssertionError("bounded PMC HTML transient retry did not return")


def _sealed_repair_source(pmcid: str, expected_pmid: str) -> tuple[str, dict[str, Any]] | None:
    """Reuse MetaVir's separately sealed official PMC source when available.

    This includes the existing exact official-PDF route.  The legacy repair
    loader revalidates its all-or-none ledger, exact PMCID/PMID bindings, raw
    hashes, and strict PDF proof before any text is admitted here.
    """
    if audit_blocked_pmc_sources.EXPECTED_BLOCKERS.get(pmcid) != expected_pmid:
        return None
    global _SEALED_REPAIR_SOURCES
    with _WRITE_LOCK:
        if _SEALED_REPAIR_SOURCES is None:
            source_dir = (
                DATA_DIR
                / "monthly_updates"
                / "all_history_through_2026-08-29"
            )
            _SEALED_REPAIR_SOURCES = (
                monthly_evidence_update.validated_pmc_repair_sources(source_dir)
            )
        source = (_SEALED_REPAIR_SOURCES or {}).get(pmcid)
    if not source:
        raise OperationalProblem(
            f"The sealed MetaVir PMC repair set lacks {pmcid}/{expected_pmid}"
        )
    text = str(source.get("text") or "").rstrip() + "\n"
    if len(text) < 5000:
        raise OperationalProblem(f"The sealed PMC source is too short: {pmcid}")
    receipt = _finalize_source_receipt(
        {
            "schema_version": 1,
            "retrieved_at": "sealed_existing_metavir_repair",
            "pmcid": pmcid,
            "pmid": expected_pmid,
            "status": "verified_full_text",
            "source": "MetaVir sealed official PMC repair ledger",
            "source_route": str(source.get("source_route") or ""),
            "source_format": str(source.get("source_format") or ""),
            "identity_verified": True,
            "direct_article_body_verified": True,
            "minimum_characters_required": 5000,
            "characters": len(text),
            "raw_sha256": str(source.get("raw_sha256") or ""),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "sealed_validation_digest": str(
                source.get("validation_digest") or ""
            ),
            "sealed_ledger_sha256": str(
                source.get("receipt_ledger_sha256") or ""
            ),
        }
    )
    return text, receipt


def _canonical_source_manifest_entry(
    pmcid: str,
    expected_pmid: str,
    text: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Validate and normalize the exact proof used by a Gemini unit."""
    canonical_pmcid = _normalise_pmcid(pmcid)
    canonical_pmid = audit_blocked_pmc_sources.normalize_pmid(expected_pmid)
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source_format = str(receipt.get("source_format") or "")
    receipt_digest = str(receipt.get("receipt_digest") or "")
    raw_sha256 = str(receipt.get("raw_sha256") or "")
    minimum_characters = receipt.get("minimum_characters_required")
    if not isinstance(minimum_characters, int) or isinstance(
        minimum_characters, bool
    ):
        minimum_characters = 5000
    short_authorized = bool(receipt.get("genuine_short_record_authorization"))
    if not raw_sha256:
        raw_sha256 = str(
            (receipt.get("strict_jats_validation") or {}).get("sha256") or ""
        )
    if (
        canonical_pmcid != str(receipt.get("pmcid") or "")
        or canonical_pmid != str(receipt.get("pmid") or "")
        or receipt.get("status") != "verified_full_text"
        or receipt.get("identity_verified") is not True
        or receipt.get("direct_article_body_verified") is not True
        or source_format not in {"jats_xml", "pmc_jats", "pmc_html", "pmc_pdf"}
        or int(receipt.get("characters") or 0) != len(text)
        or len(text) < minimum_characters
        or minimum_characters < 5000
        and not short_authorized
        or str(receipt.get("text_sha256") or "") != text_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", raw_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt_digest)
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


def _register_r3_source(
    output_dir: Path,
    pmcid: str,
    expected_pmid: str,
    text: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Install one canonical proof per PMCID, including split-context units.

    Two dynamic official receipts may differ while their normalized review text
    is byte-identical.  The first exact proof remains canonical.  A different
    normalized source is rejected and must be explicitly refreshed instead of
    silently rebinding already completed scientific results.
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


def _genuine_short_minimum(
    output_dir: Path,
    *,
    pmcid: str,
    pmid: str,
) -> tuple[int, dict[str, Any] | None]:
    """Return a documented user-approved short-record threshold, if present."""
    path = output_dir / "genuine_short_pmc_records.json"
    packet = load_json(path, {})
    for record in packet.get("records", []) if isinstance(packet, dict) else []:
        if not isinstance(record, dict):
            continue
        if (
            audit_blocked_pmc_sources.normalize_pmcid(record.get("pmcid")) == pmcid
            and audit_blocked_pmc_sources.normalize_pmid(record.get("pmid")) == pmid
        ):
            minimum = record.get("minimum_characters")
            if (
                packet.get("schema_version") != 1
                or record.get("authorization") != "user_approved_genuine_short_record"
                or not isinstance(minimum, int)
                or isinstance(minimum, bool)
                or not 2000 <= minimum < 5000
                or not str(record.get("reason") or "").strip()
            ):
                raise OperationalProblem(
                    f"Invalid genuine-short-record authorization for {pmcid}/{pmid}"
                )
            return minimum, record
    return 5000, None


def _verified_pmc_text_unlocked(
    output_dir: Path,
    pmcid: str,
    expected_pmid: str,
) -> tuple[str, dict[str, Any]]:
    pmcid = _normalise_pmcid(pmcid)
    expected_pmid = audit_blocked_pmc_sources.normalize_pmid(expected_pmid)
    if not expected_pmid:
        raise OperationalProblem(f"Invalid expected PMID for {pmcid}: {expected_pmid!r}")
    minimum_characters, short_authorization = _genuine_short_minimum(
        output_dir,
        pmcid=pmcid,
        pmid=expected_pmid,
    )
    cache_dir = output_dir / "full_text_cache"
    text_path = cache_dir / f"{pmcid}.txt"
    receipt_path = cache_dir / f"{pmcid}.source.json"
    receipt = load_json(receipt_path, {})
    if text_path.exists() and receipt.get("status") == "verified_full_text":
        text = text_path.read_text(encoding="utf-8")
        if (
            receipt.get("pmcid") == pmcid
            and receipt.get("pmid") == expected_pmid
            and receipt.get("identity_verified") is True
            and receipt.get("direct_article_body_verified") is True
            and len(text) >= minimum_characters
            and receipt.get("text_sha256") == hashlib.sha256(text.encode("utf-8")).hexdigest()
        ):
            if not receipt.get("receipt_digest"):
                receipt = _finalize_source_receipt(receipt)
                save_json(receipt_path, receipt)
            return text, receipt

    sealed = _sealed_repair_source(pmcid, expected_pmid)
    if sealed is not None:
        text, receipt = sealed
        cache_dir.mkdir(parents=True, exist_ok=True)
        _durable_atomic_write_text(text_path, text)
        save_json(receipt_path, receipt)
        return text, receipt

    try:
        with _configured_local_entrez():
            raw = monthly_evidence_update.fetch_pmc_full_text(pmcid)
        validation, extracted = audit_blocked_pmc_sources.validate_jats_receipt(
            raw,
            expected_pmcid=pmcid,
            expected_pmid=expected_pmid,
        )
        source = "NCBI PMC EFetch JATS XML"
        source_route = "efetch"
        source_format = "jats_xml"
        source_validation = validation
        source_raw = raw
        extra_receipt: dict[str, Any] = {}
        if validation.get("valid"):
            text = extracted.rstrip() + "\n"
        else:
            credential_values = _allowed_credential_values(names=NCBI_EMAIL_NAMES)
            email = _first_credential_value(credential_values, NCBI_EMAIL_NAMES)
            credential_values.clear()
            user_agent = "MetaVir-Claude-Gemini-PMC/1.0"
            if email:
                user_agent += f" (mailto:{email})"
            html_route, html_text, html_raw, html_attempt_records = (
                _probe_pmc_html_with_bounded_transient_retry(
                    pmcid, expected_pmid, user_agent=user_agent
                )
            )
            html_validation = html_route.get("validation") or {}
            if html_validation.get("valid"):
                text = html_text.rstrip() + "\n"
                source = "Canonical official PMC article HTML"
                source_route = "pmc_canonical_article_html"
                source_format = "pmc_html"
                source_validation = html_validation
                source_raw = html_raw
                extra_receipt = {
                    "efetch_validation": validation,
                    "canonical_html_probe_attempts": html_attempt_records,
                    "official_transport_receipt": _sanitized_transport_receipt(
                        html_route.get("transport")
                    ),
                }
            else:
                try:
                    pdf_route, pdf_text, pdf_raw = (
                        audit_blocked_pmc_sources.probe_pmc_pdf(
                            pmcid,
                            expected_pmid,
                            link_html_route=html_route,
                            link_html_raw=html_raw,
                            user_agent=user_agent,
                        )
                    )
                except Exception as exc:
                    raise OperationalProblem(
                        f"No strict complete official PMC source for {pmcid}/{expected_pmid}; "
                        f"JATS={validation.get('reason')}; HTML={html_validation.get('reason')}; "
                        f"PDF={safe_error(exc)}"
                    ) from exc
                pdf_validation = pdf_route.get("validation") or {}
                if not pdf_validation.get("valid"):
                    raise OperationalProblem(
                        f"Official PMC PDF failed strict validation for {pmcid}/{expected_pmid}: "
                        f"{pdf_validation.get('reason') or 'unknown validation failure'}"
                    )
                text = pdf_text.rstrip() + "\n"
                source = "Exact official PMC PDF"
                source_route = "pmc_official_pdf"
                source_format = "pmc_pdf"
                source_validation = pdf_validation
                source_raw = pdf_raw
                extra_receipt = {
                    "efetch_validation": validation,
                    "canonical_html_probe_attempts": html_attempt_records,
                    "canonical_html_validation": html_validation,
                    "canonical_html_sha256": hashlib.sha256(html_raw).hexdigest(),
                    "official_pdf_transport_receipt": _sanitized_transport_receipt(
                        pdf_route.get("pdf_transport")
                    ),
                }
        if len(text) < minimum_characters:
            raise OperationalProblem(
                f"Verified PMC record {pmcid}/{expected_pmid} has {len(text)} characters; "
                f"the required minimum is {minimum_characters}"
            )
        cache_dir.mkdir(parents=True, exist_ok=True)
        _durable_atomic_write_text(text_path, text)
        receipt = _finalize_source_receipt(
            {
                "schema_version": 1,
                "retrieved_at": now(),
                "pmcid": pmcid,
                "pmid": expected_pmid,
                "status": "verified_full_text",
                "source": source,
                "source_route": source_route,
                "source_format": source_format,
                "identity_verified": True,
                "direct_article_body_verified": True,
                "strict_source_validation": source_validation,
                "strict_jats_validation": validation,
                "minimum_characters_required": minimum_characters,
                "genuine_short_record_authorization": short_authorization,
                "characters": len(text),
                "raw_sha256": hashlib.sha256(source_raw).hexdigest(),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                **extra_receipt,
            }
        )
        save_json(receipt_path, receipt)
        return text, receipt
    except OperationalProblem:
        raise
    except Exception as exc:
        raise OperationalProblem(f"PMC full-text retrieval failed for {pmcid}: {safe_error(exc)}") from exc


def _verified_pmc_text(
    output_dir: Path,
    pmcid: str,
    expected_pmid: str,
) -> tuple[str, dict[str, Any]]:
    canonical = _normalise_pmcid(pmcid)
    with _source_lock(canonical):
        return _verified_pmc_text_unlocked(
            output_dir, canonical, expected_pmid
        )


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
    pmcids = {_normalise_pmcid(item["pmcid"]) for item in contexts.values()}
    if len(pmcids) != 1:
        raise OperationalProblem(
            f"A Gemini R3 prompt must contain exactly one PMCID: {sorted(pmcids)}"
        )
    canonical_url = _canonical_pmc_url(next(iter(pmcids)))
    allowed_codes = ", ".join(sorted(dual_screening.EXCLUSION_CODES))
    pmids = {str(item.get("pmid") or "") for item in contexts.values()}
    forced_fresh_reminder = ""
    if pmids == {"21518693"}:
        forced_fresh_reminder = (
            "\n\nEXACT ADJUDICATION REMINDER FOR PMID 21518693:\n"
            "The verified article describes a '7-year-period prevalence' endpoint. "
            "Reassess that endpoint against the incidence-only protocol and do not "
            "treat prevalence as incidence. This reminder does not preassign an "
            "inclusion or exclusion decision."
        )
    return (
        dual_screening.FULL_TEXT_INSTRUCTIONS
        + "\n\nSTRICT SOURCE AND CODE CONTRACT:\n"
        + f"For every non-null evidence item, use source full_text and exactly this URL: {canonical_url}\n"
        + "For INCLUDE use exclusion_code null. For EXCLUDE use exactly one of: "
        + allowed_codes
        + ". Do not invent, abbreviate, underscore, or prefix an exclusion code. "
        + "Every context requires a non-empty one-sentence reason."
        + forced_fresh_reminder
        + "\n\nCONTEXTS:\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        + "\n\nFULL ARTICLE TEXT:\n"
        + full_text
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
                    "context_ids": contexts[
                        offset : offset + max_contexts_per_unit
                    ],
                }
            )
    return units


def run_r3(
    output_dir: Path = OUTPUT_DIR,
    *,
    workers: int = 1,
    max_units: int = 0,
    max_contexts_per_unit: int = R3_MAX_CONTEXTS_PER_UNIT,
    client: OpenAI | None = None,
    source_loader: Callable[[Path, str, str], tuple[str, dict[str, Any]]] = _verified_pmc_text,
) -> dict[str, Any]:
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
    units = _make_r3_units(
        gated,
        set(progress),
        max_contexts_per_unit=max_contexts_per_unit,
    )
    total_pending_units = len(units)
    if max_units > 0:
        units = units[:max_units]
    gateway = client or (_configure_client() if units else None)

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
            rows = dual_screening.validate_full_text(
                payload,
                expected,
                source_text=text,
                source_identifier=pmcid,
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
            payload_normalizer=lambda payload: _normalise_gemini_r3_payload(
                payload,
                expected_pmcid=pmcid,
                source_text=text,
            ),
            force_fresh_cached_adjudication=(
                _requires_fresh_r3_adjudication(context_ids)
            ),
        )
        _update_pmc_receipt_index(output_dir)
        # Problem identity intentionally omits the source digest, so a later
        # successful verified retrieval resolves an earlier retrieval failure.
        return _work_unit_key("gemini_r3", context_ids), results

    def persist(stopped_reason: str) -> None:
        manifest = load_json(output_dir / "r3_source_manifest.json", {})
        if not isinstance(manifest, dict):
            raise RuntimeError("Gemini R3 source manifest is malformed")
        complete = len(progress) == len(gated)
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
            "status": "complete" if complete else "partial",
            "stopped_reason": stopped_reason,
            "source_manifest_digest": stable_digest(manifest),
            "results": sorted(progress.values(), key=lambda item: item["context_id"]),
        }
        save_json(progress_path, packet)
        r2_complete = (output_dir / "claude_r2_results.json").exists()
        if r2_complete and len(progress) == len(gated):
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
        context_ids = list(unit["context_ids"])
        # Retrieval failures lack a source digest, so their durable key remains stable.
        key = _work_unit_key("gemini_r3", context_ids)
        _record_problem(
            output_dir,
            stage="gemini_r3",
            model=GEMINI_R3_MODEL,
            work_unit_key=key,
            context_ids=context_ids,
            error=exc,
        )
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

    _run_units(units, process, success, error, workers, on_active=active)
    stopped = (
        "r3_complete"
        if len(progress) == len(gated) and (output_dir / "claude_r2_results.json").exists()
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
        for path in sorted((output_dir / "full_text_cache").glob("*.source.json"))
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
    unknown_interrupted_receipts = sum(
        item.get("billing_status") == "unknown_after_process_interruption"
        for item in receipts
    )
    invalid_or_unaccepted_receipts = sum(
        item.get("scientific_result_accepted") is False
        and item.get("billing_status") != "unknown_after_process_interruption"
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
            if receipt.get("billing_status") == "unknown_after_process_interruption":
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
                "unavailable_after_process_interruption"
                if unknown and int(row[f"{prefix}_calls"]) == unknown
                else "partial_provider_reported_with_interrupted_unknown"
                if unknown
                else "provider_reported_via_cornell_gateway_response_usage"
                if int(row[f"{prefix}_calls"])
                else "no_calls"
            )
            serial[f"{prefix}_cost_provenance"] = (
                "unknown_after_process_interruption"
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
            fields.append(f"{prefix}_token_provenance={serial[f'{prefix}_token_provenance']}")
            fields.append(f"{prefix}_cost_provenance={serial[f'{prefix}_cost_provenance']}")
        lines.append("\t".join(fields))
        json_rows.append(serial)
    _durable_atomic_write_text(COST_PATH, "\n".join(lines) + "\n")
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
            "invalid_or_unaccepted_receipts_retained_for_cost": (
                invalid_or_unaccepted_receipts
            ),
            "rows": json_rows,
        },
    )


def write_outputs_and_state(output_dir: Path, *, stopped_reason: str) -> dict[str, Any]:
    acceptance_consistency = reconcile_model_cache_acceptance(
        output_dir, write=True
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
    r2 = (
        r2_final
        if r2_final.get("source_r2_input_digest") == current_r2_source_digest
        else r2_progress
        if r2_progress.get("source_r2_input_digest") == current_r2_source_digest
        else {}
    )
    r3_final = load_json(output_dir / "gemini_r3_results.json", {})
    r3_progress = load_json(output_dir / "gemini_r3_progress.json", {})
    r3 = r3_final if r3_final else r3_progress
    problem_packet = load_json(output_dir / "screening_problem_ledger.json", {})
    problems = [item for item in problem_packet.get("unresolved", []) if isinstance(item, dict)]
    r2_results = [item for item in r2.get("results", []) if isinstance(item, dict)]
    r3_results = [item for item in r3.get("results", []) if isinstance(item, dict)]
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
        "deferrals": no_pmcid_deferrals,
        "note": "These contexts remain included at R2 and have no fabricated R3 decision.",
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
    science_gated = [item for item in r2_results if item.get("needs_full_text")]
    gated = [item for item in science_gated if item.get("full_text_eligible")]
    complete = bool(
        expected_r2
        and len(r2_results) == expected_r2
        and len(r3_results) == len(gated)
        and not problems
    )
    terminal_status = (
        "complete_with_decision_free_source_deferrals"
        if complete and no_pmcid_deferrals
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
        "gemini_r3_exclusions": len(gemini_exclusions),
        "gemini_r3_decision_free_no_pmcid_deferrals": len(no_pmcid_deferrals),
        "unresolved_operational_problems": len(problems),
        "model_cache_acceptance_consistency": {
            "contract_version": acceptance_consistency["contract_version"],
            "cache_files_scanned": acceptance_consistency[
                "cache_files_scanned"
            ],
            "gateway_receipts_scanned": acceptance_consistency[
                "gateway_receipts_scanned"
            ],
            "reconciled_cache_files_this_invocation": (
                acceptance_consistency["reconciled_cache_files"]
            ),
            "remaining_acceptance_mismatches": acceptance_consistency[
                "remaining_acceptance_mismatches"
            ],
            "scientific_decisions_changed": False,
        },
        "status": terminal_status,
        "stopped_reason": stopped_reason,
        "resume_command": (
            "python scripts/run_claude_gemini_parallel_review.py --stage all "
            "--batch-size 8 --workers 1"
        ),
        "canonical_chatgpt_artifacts_modified": False,
        "cornell_credential_source": "local:MetaVir/mykey.env",
        "credential_handling": (
            "Only allowlisted Cornell and NCBI names are read from the local "
            "regular non-symlink MetaVir/mykey.env file; values are not exported "
            "to the process environment or persisted in review artifacts."
        ),
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
                "direct_provider_credential": "invalid",
                "direct_provider_note": "The available direct Gemini credential failed prior validation and is not used.",
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
        _recover_interrupted_active_dispatch(args.output_dir)
        if args.stage in {"prepare", "all"}:
            prepare_r1_lineage(args.output_dir)
        if args.stage in {"r2", "all"}:
            r2 = run_r2(
                args.output_dir,
                batch_size=args.batch_size,
                workers=args.workers,
                max_units=args.max_units,
            )
            if args.stage == "all" and r2.get("status") != "complete":
                print(json.dumps(write_outputs_and_state(args.output_dir, stopped_reason=r2.get("stopped_reason") or "r2_partial"), indent=2))
                return 2
        if args.stage in {"r3", "all"}:
            run_r3(
                args.output_dir,
                workers=args.workers,
                max_units=args.max_units,
                max_contexts_per_unit=args.r3_max_contexts_per_unit,
            )
        state = write_outputs_and_state(args.output_dir, stopped_reason=(
            "status_only" if args.stage == "status" else f"{args.stage}_invocation_finished"
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
