#!/usr/bin/env python3
"""Discover and audit newly published MetaPee evidence.

The workflow is deliberately resumable. Discovery writes an immutable candidate
packet before any relevance screening or saved-result mutation takes place.
"""

from __future__ import annotations


import argparse
import calendar
import concurrent.futures
import glob
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from Bio import Entrez
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from scripts import dietary_only_policy as dietary_policy
import meta_analysis  # noqa: E402
from effect_measure import load_disease_registry  # noqa: E402
from scripts import cost_accounting, dual_screening, reclassification_review  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = REPO_ROOT / "Cached_results"
EXPOSURES_FILE = REPO_ROOT / "static" / "exposures.json"
SYNONYMS_FILE = DATA_DIR / "synonyms_cache.json"
ADDITIONAL_EXPOSURES_FILE = DATA_DIR / "additionalExposure.json"
ADDITIONAL_EXPOSURES_V2_FILE = DATA_DIR / "additionalExposureV2.json"
EXPOSURE_HIERARCHY_FILE = DATA_DIR / "exposure_hierarchy.json"
EXPOSURE_REVISION_FILE = DATA_DIR / "exposure_revision_manifest.json"
SUPERSEDED_GENERATION_NAME = "superseded_generation.json"
PUBMED_ESEARCH_UID_LIMIT = 10_000
ESEARCH_PAGE_SIZE = 5_000
ESEARCH_PROTOCOL_VERSION = "recursive-publication-date-splitting-v1"
DISCOVERY_CHECKPOINT_PROTOCOL_VERSION = "exact-scope-per-query-v1"
DISCOVERY_CHECKPOINT_SCHEMA_VERSION = 1
DISCOVERY_OUTCOME = "Incidence"
NCBI_KEYED_MIN_INTERVAL_SECONDS = 0.1
NCBI_PUBLIC_MIN_INTERVAL_SECONDS = 0.37
_ENTREZ_CONFIGURATION_LOCK = threading.RLock()
_ENTREZ_VALIDATED_KEY_DIGEST: str | None = None
_ENTREZ_REJECTED_KEY_DIGEST: str | None = None
_NCBI_RATE_LOCK = threading.Lock()
_NCBI_LAST_REQUEST_AT: float | None = None
_NCBI_RATE_SLEEP = time.sleep

def disease_queries(registry: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    """Build one auditable PubMed query from each registry name and alias set."""
    registry = load_disease_registry() if registry is None else registry
    queries: dict[str, str] = {}
    for scope in registry.values():
        disease = clean_search_term(scope.get("disease") or scope.get("label"))
        raw_aliases = scope.get("aliases") or []
        if isinstance(raw_aliases, str):
            raw_aliases = raw_aliases.split(";")
        terms = [disease, *[clean_search_term(value) for value in raw_aliases]]
        unique_terms: list[str] = []
        seen: set[str] = set()
        for term in terms:
            key = term.casefold()
            if term and key not in seen:
                unique_terms.append(term)
                seen.add(key)
        if not disease or not unique_terms:
            continue
        mesh = disease.replace('"', "")
        title_abstract = " OR ".join(quote_term(term) for term in unique_terms)
        queries[disease] = f'"{mesh}"[MeSH Terms] OR {title_abstract}'
    if not queries:
        raise RuntimeError("The urinary-system cancer registry produced no PubMed queries")
    return queries


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any, indent: int = 2, ensure_ascii: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=ensure_ascii, indent=indent)
    temporary.replace(path)


def require_output_not_superseded(output_dir: Path) -> None:
    """Refuse discovery or screening from an explicitly superseded generation."""
    marker_path = output_dir / SUPERSEDED_GENERATION_NAME
    if not marker_path.exists():
        return
    marker = load_json(marker_path, {})
    if not isinstance(marker, dict) or marker.get("status") != "superseded":
        raise RuntimeError("Superseded-generation marker is malformed")
    candidate_path = output_dir / "candidates.json"
    recorded = str(marker.get("candidate_sha256") or "")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", recorded)
        or not candidate_path.is_file()
        or hashlib.sha256(candidate_path.read_bytes()).hexdigest() != recorded
    ):
        raise RuntimeError("Superseded candidate packet failed its preservation digest")
    raise RuntimeError(
        "This candidate generation is superseded and unusable: "
        + str(marker.get("reason") or "replacement discovery is required")
    )


def previous_month(today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    first_this_month = today.replace(day=1)
    last_previous = first_this_month - timedelta(days=1)
    first_previous = last_previous.replace(day=1)
    return first_previous.isoformat(), last_previous.isoformat()


def wait_for_ncbi_request_slot() -> None:
    """Reserve one process-wide E-utilities request start at a safe rate."""
    global _NCBI_LAST_REQUEST_AT
    minimum_interval = (
        NCBI_KEYED_MIN_INTERVAL_SECONDS
        if Entrez.api_key
        else NCBI_PUBLIC_MIN_INTERVAL_SECONDS
    )
    with _NCBI_RATE_LOCK:
        now = time.monotonic()
        if _NCBI_LAST_REQUEST_AT is not None:
            delay = _NCBI_LAST_REQUEST_AT + minimum_interval - now
            if delay > 0:
                _NCBI_RATE_SLEEP(delay)
                now = time.monotonic()
        _NCBI_LAST_REQUEST_AT = now


def reject_invalid_ncbi_api_key(
    error: BaseException,
    attempted_api_key: str | None,
) -> bool:
    """Latch an HTTP-400 key rejection and select the public endpoint once."""
    global _ENTREZ_REJECTED_KEY_DIGEST
    if not attempted_api_key or 400 not in entrez_http_status_codes(error):
        return False
    if os.getenv("MONTHLY_REQUIRE_VALID_NCBI_KEY", "false").lower() == "true":
        return False
    digest = hashlib.sha256(attempted_api_key.encode("utf-8")).hexdigest()
    with _ENTREZ_CONFIGURATION_LOCK:
        first_rejection = _ENTREZ_REJECTED_KEY_DIGEST != digest
        _ENTREZ_REJECTED_KEY_DIGEST = digest
        current_key = str(Entrez.api_key or "")
        if current_key and hashlib.sha256(current_key.encode("utf-8")).hexdigest() == digest:
            Entrez.api_key = None
    if first_rejection:
        print(
            "WARNING: NCBI_API_KEY was rejected (HTTP 400; likely expired or invalid). "
            "Continuing through NCBI's public rate-limited endpoint.",
            flush=True,
        )
    return True


def configure_entrez(*, validate_api_key: bool = True) -> str:
    global _ENTREZ_REJECTED_KEY_DIGEST, _ENTREZ_VALIDATED_KEY_DIGEST
    # A locally edited mykey.env should take precedence over stale variables in
    # a long-lived shell. GitHub Actions has no mykey.env, so repository secrets
    # remain authoritative there.
    with _ENTREZ_CONFIGURATION_LOCK:
        load_dotenv(REPO_ROOT / "mykey.env", override=True)
        Entrez.email = os.getenv("NCBI_EMAIL") or os.getenv("PUBMED_EMAIL")
        requested_key = os.getenv("NCBI_API_KEY") or os.getenv("PUBMED_API_KEY")
        # Keep retries in the explicit paced loops below.
        Entrez.max_tries = 1
        strict_key = os.getenv("MONTHLY_REQUIRE_VALID_NCBI_KEY", "false").lower() == "true"
        if not Entrez.email:
            raise RuntimeError("NCBI_EMAIL (or PUBMED_EMAIL) is not configured")
        if not requested_key:
            Entrez.api_key = None
            if strict_key:
                raise RuntimeError("NCBI_API_KEY is missing; strict monthly validation is enabled")
            return "not_configured"
        key_digest = hashlib.sha256(requested_key.encode("utf-8")).hexdigest()
        if _ENTREZ_REJECTED_KEY_DIGEST == key_digest:
            Entrez.api_key = None
            if strict_key:
                raise RuntimeError(
                    "NCBI_API_KEY was previously rejected with HTTP 400; "
                    "strict monthly validation is enabled"
                )
            return "rejected_http_400"
        Entrez.api_key = requested_key
        if not validate_api_key:
            return "configured_not_validated"
        if _ENTREZ_VALIDATED_KEY_DIGEST == key_digest:
            return "working"

        # Validate each distinct process-local credential at most once.
        validation_term = next(iter(CANCER_QUERIES.values()))
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                wait_for_ncbi_request_slot()
                with Entrez.esearch(
                    db="pubmed", term=validation_term, retmax=0
                ) as handle:
                    Entrez.read(handle)
                _ENTREZ_VALIDATED_KEY_DIGEST = key_digest
                return "working"
            except Exception as exc:
                last_error = exc
                if 400 in entrez_http_status_codes(exc):
                    break
                if retryable_entrez_error(exc) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"NCBI API-key validation failed: {exc}") from exc
        assert last_error is not None
        if strict_key:
            raise RuntimeError(
                "NCBI_API_KEY was rejected with HTTP 400 (expired or invalid); "
                "strict monthly validation is enabled"
            ) from last_error
        reject_invalid_ncbi_api_key(last_error, requested_key)
        return "rejected_http_400"


def safe_context_part(value: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").casefold()).strip("_")


def saved_contexts() -> set[tuple[str, str, str]]:
    """Return saved PMID/exposure/cancer contexts, never PMID-only exclusions.

    One article may report several requested exposures. Saving one context must
    not prevent a later search from screening another context from the same
    PMID.
    """
    values: set[tuple[str, str, str]] = set()
    for filename in glob.glob(str(CACHE_DIR / "**" / "*.json"), recursive=True):
        path = Path(filename)
        cached = load_json(path, {})
        if not isinstance(cached, dict):
            continue
        exposure = safe_context_part(cached.get("exposure") or path.parent.name)
        cancer = safe_context_part(cached.get("cancer"))
        if not cancer:
            continue
        for study in cached.get("studies", []):
            pmid = str(study.get("PMID") or "").strip()
            if pmid and pmid.lower() not in {"none", "nan"}:
                values.add((pmid, exposure, cancer))
    return values


def included_direct_contexts(
    exposures: Iterable[str],
    cancers: Iterable[str],
) -> set[tuple[str, str, str]]:
    """Return canonical prior direct contexts for authoritative rescreening."""
    exposure_by_key = {
        safe_context_part(value): str(value) for value in exposures
    }
    cancer_by_key = {
        safe_context_part(value): str(value) for value in cancers
    }
    values: set[tuple[str, str, str]] = set()
    for path in sorted(CACHE_DIR.glob("**/*.json")):
        cached = load_json(path, {})
        if not isinstance(cached, dict):
            continue
        exposure_key = safe_context_part(
            cached.get("exposure") or path.parent.name
        )
        cancer_key = safe_context_part(cached.get("cancer"))
        exposure = exposure_by_key.get(exposure_key)
        cancer = cancer_by_key.get(cancer_key)
        if not exposure or not cancer:
            continue
        for study in cached.get("studies", []):
            if not isinstance(study, dict):
                continue
            pmid = str(study.get("PMID") or "").strip()
            direct = str(
                study.get("direct_exposure")
                or study.get("Direct Exposure")
                or exposure
            )
            if (
                pmid
                and pmid.casefold() not in {"none", "nan"}
                and safe_context_part(direct)
                == safe_context_part(exposure)
            ):
                values.add((pmid, exposure, cancer))
    return values


def clean_search_term(term: str) -> str:
    return re.sub(r"\s+", " ", str(term or "")).strip().strip('"')


def _split_catalog_scalar(value: Any, exposure: str, field: str) -> list[str]:
    """Split legacy comma-delimited aliases, rejecting atomic comma syntax.

    Chemical locants such as ``1,4-`` and ``N,N-`` use an unspaced comma.
    Treating those values as a legacy scalar alias list silently fragments the
    reviewed term.  Such aliases must be represented by a list-valued field
    (preferably ``search_terms``), where commas are preserved verbatim.
    """
    text = str(value or "")
    if re.search(r",(?=\S)", text):
        raise ValueError(
            f"Exposure {exposure!r} field {field!r} contains an atomic "
            "comma-bearing term in a scalar value; move the exact term to a "
            "list-valued search_terms field"
        )
    return [part for part in text.split(",") if part.strip()]


def _additional_exposure_entries(raw: Any) -> dict[str, Any]:
    """Normalize supported additionalExposure.json shapes without mutating them."""
    if isinstance(raw, dict) and isinstance(raw.get("exposures"), (list, dict)):
        raw = raw["exposures"]
    if isinstance(raw, list):
        entries: dict[str, Any] = {}
        for value in raw:
            if isinstance(value, str):
                name, entry = value.strip(), {}
            elif isinstance(value, dict):
                name = str(
                    value.get("exposure")
                    or value.get("name")
                    or value.get("canonical_name")
                    or ""
                ).strip()
                entry = value
            else:
                continue
            if name:
                entries[name] = entry
        return entries
    if isinstance(raw, dict):
        reserved = {"version", "schema_version", "description", "hierarchy", "edges"}
        return {
            str(name).strip(): entry
            for name, entry in raw.items()
            if str(name).strip() and str(name) not in reserved
        }
    return {}


def additional_exposure_entries() -> dict[str, Any]:
    """Load the collision-checked V1 + V2 additional-exposure union."""
    result: dict[str, Any] = {}
    canonical_by_key: dict[str, tuple[str, Path]] = {}
    for path in (ADDITIONAL_EXPOSURES_FILE, ADDITIONAL_EXPOSURES_V2_FILE):
        for name, entry in _additional_exposure_entries(load_json(path, {})).items():
            key = name.casefold()
            if key in canonical_by_key:
                prior_name, prior_path = canonical_by_key[key]
                raise ValueError(
                    "Additional exposure catalogs duplicate canonical exposure "
                    f"{name!r}: {prior_path} ({prior_name!r}) and {path}"
                )
            canonical_by_key[key] = (name, path)
            result[name] = entry
    return result


def _catalog_terms(exposure: str, entry: Any) -> list[str]:
    terms: list[Any] = (
        [exposure]
        if not isinstance(entry, dict)
        or entry.get("include_canonical_in_search", True)
        else []
    )
    if isinstance(entry, dict):
        fields = [
            "core", "aliases", "alias", "downstream", "related", "search_terms"
        ]
        if entry.get("include_guarded_in_search", True):
            fields.extend(("guarded", "guarded_aliases"))
        for field in fields:
            value = entry.get(field, "")
            if isinstance(value, list):
                terms.extend(value)
            else:
                terms.extend(_split_catalog_scalar(value, exposure, field))
    elif isinstance(entry, list):
        terms.extend(entry)
    else:
        terms.extend(_split_catalog_scalar(entry, exposure, "<entry>"))
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw_term in terms:
        term = clean_search_term(raw_term)
        key = term.casefold()
        if len(term) >= 3 and key not in seen:
            cleaned.append(term)
            seen.add(key)
    return cleaned[:60]


def exposure_terms(scope: str = "current") -> dict[str, list[str]]:
    """Return current, additional, or combined exposure discovery terms."""
    if scope not in {"current", "additional", "all"}:
        raise ValueError(f"Unknown exposure scope: {scope!r}")
    exposures = load_json(EXPOSURES_FILE, [])
    synonyms = load_json(SYNONYMS_FILE, {})
    synonyms_by_name = {str(name).casefold(): value for name, value in synonyms.items()}
    result: dict[str, list[str]] = {}
    if scope in {"current", "all"}:
        for exposure in exposures:
            name = str(exposure)
            result[name] = _catalog_terms(
                name, synonyms_by_name.get(name.casefold(), {})
            )
    if scope in {"additional", "all"}:
        for exposure, entry in additional_exposure_entries().items():
            added_terms = _catalog_terms(exposure, entry)
            existing_name = next(
                (name for name in result if name.casefold() == exposure.casefold()),
                None,
            )
            if existing_name is None:
                result[exposure] = added_terms
            else:
                result[existing_name] = list(
                    dict.fromkeys([*result[existing_name], *added_terms])
                )[:60]
    return result


def exposure_catalog_sources(scope: str = "current") -> dict[str, str]:
    """Label canonical ownership without duplicating collision search contexts."""
    if scope not in {"current", "additional", "all"}:
        raise ValueError(f"Unknown exposure scope: {scope!r}")
    current = [
        str(value)
        for value in load_json(EXPOSURES_FILE, [])
        if str(value).strip()
    ]
    additional = list(additional_exposure_entries())
    sources: dict[str, str] = {}
    canonical_by_norm: dict[str, str] = {}
    if scope in {"current", "all"}:
        for name in current:
            sources[name] = "current"
            canonical_by_norm[name.casefold()] = name
    if scope in {"additional", "all"}:
        for name in additional:
            existing = canonical_by_norm.get(name.casefold())
            if existing is None:
                sources[name] = "additional"
                canonical_by_norm[name.casefold()] = name
            else:
                sources[existing] = "current+additional"
    return sources


REVISION_NCBI_SCOPE_FIELDS = (
    "alias_expanded_existing",
    "new_in_modified_synonyms",
)


def revision_manifest(path: Path = EXPOSURE_REVISION_FILE) -> dict[str, Any]:
    packet = load_json(path, {})
    if not isinstance(packet, dict):
        raise ValueError("Exposure revision manifest must be an object")
    return packet


def _revision_group_names(
    fields: tuple[str, ...],
    path: Path = EXPOSURE_REVISION_FILE,
) -> set[str]:
    packet = revision_manifest(path)
    groups = packet.get("sets") if isinstance(packet.get("sets"), dict) else packet
    selected: set[str] = set()
    for field in fields:
        values = groups.get(field, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ValueError(f"Revision manifest field {field!r} must be a string list")
        selected.update(value.strip().casefold() for value in values if value.strip())
    return selected


def revision_scope_names(
    path: Path = EXPOSURE_REVISION_FILE,
) -> set[str]:
    selected = _revision_group_names(REVISION_NCBI_SCOPE_FIELDS, path)
    if not selected:
        raise ValueError("Revision manifest selects no current exposures for NCBI")
    return selected


def reclassification_review_names(
    path: Path = EXPOSURE_REVISION_FILE,
) -> set[str]:
    return _revision_group_names(("alias_narrowed_or_reclassified",), path)


def reclassification_targets(
    path: Path = EXPOSURE_REVISION_FILE,
) -> dict[str, list[str]]:
    packet = revision_manifest(path)
    raw = packet.get("reclassification_targets")
    if not isinstance(raw, dict):
        raise ValueError("Revision manifest reclassification_targets must be an object")
    searchable = exposure_terms("all")
    searchable_by_norm = {name.casefold(): name for name in searchable}
    current = {
        str(value).casefold(): str(value)
        for value in load_json(EXPOSURES_FILE, [])
    }
    output: dict[str, list[str]] = {}
    for source_norm in sorted(reclassification_review_names(path)):
        source = current.get(source_norm)
        if source is None:
            raise ValueError(
                f"Reclassification source {source_norm!r} is not current"
            )
        values = raw.get(source)
        if not isinstance(values, list) or not values:
            raise ValueError(
                f"Reclassification source {source!r} requires targets"
            )
        resolved = []
        for raw_target in values:
            target = searchable_by_norm.get(str(raw_target).casefold())
            if target is None:
                raise ValueError(
                    f"Reclassification target {raw_target!r} is not searchable"
                )
            if target.casefold() == source.casefold():
                raise ValueError("Reclassification source cannot target itself")
            if target not in resolved:
                resolved.append(target)
        output[source] = resolved
    return output


def exposure_catalog_digest(terms_by_exposure: dict[str, list[str]]) -> str:
    payload = json.dumps(
        terms_by_exposure,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def filter_exposure_terms(
    terms_by_exposure: dict[str, list[str]],
    requested_exposures: list[str] | None,
) -> dict[str, list[str]]:
    """Select canonical exposures case-insensitively while preserving catalog order."""
    if not requested_exposures:
        return dict(terms_by_exposure)
    canonical_by_key = {name.casefold(): name for name in terms_by_exposure}
    requested_keys: set[str] = set()
    unknown: list[str] = []
    for raw_name in requested_exposures:
        name = str(raw_name or "").strip()
        canonical = canonical_by_key.get(name.casefold())
        if canonical is None:
            unknown.append(name or "<empty>")
        else:
            requested_keys.add(canonical.casefold())
    if unknown:
        raise ValueError(
            "Unknown exposure filter(s): "
            + ", ".join(unknown)
            + ". Use canonical names from static/exposures.json."
        )
    return {
        name: terms
        for name, terms in terms_by_exposure.items()
        if name.casefold() in requested_keys
    }


VITAMIN_K_PARENT_EXPOSURE = "Vitamin K"
VITAMIN_K_CHILD_EXPOSURES = frozenset({"Vitamin K1", "Vitamin K2", "Vitamin K3"})


def _hierarchy_edges_from_packet(raw: Any) -> list[tuple[str, str]]:
    if isinstance(raw, dict) and isinstance(raw.get("edges"), list):
        raw = raw["edges"]
    if isinstance(raw, dict):
        return [
            (str(child).strip(), str(parent).strip())
            for child, parents in raw.items()
            for parent in (parents if isinstance(parents, list) else [parents])
            if str(child).strip() and str(parent).strip()
        ]
    if not isinstance(raw, list):
        return []
    edges: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            child = str(item.get("child") or "").strip()
            parent = str(item.get("parent") or "").strip()
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            child, parent = (str(value).strip() for value in item)
        else:
            raise ValueError("Hierarchy edges must contain child and parent")
        if child and parent:
            edges.append((child, parent))
    return edges


def exposure_hierarchy(
    path: Path = EXPOSURE_HIERARCHY_FILE,
) -> dict[str, tuple[str, ...]]:
    """Return a case-insensitive, cycle-checked immediate-parent mapping."""
    edges = _hierarchy_edges_from_packet(load_json(path, {}))
    canonical_by_norm: dict[str, str] = {}
    parents: dict[str, set[str]] = defaultdict(set)
    for child, parent in edges:
        child_norm, parent_norm = child.casefold(), parent.casefold()
        if child_norm == parent_norm:
            raise ValueError(f"Exposure hierarchy self-cycle: {child!r}")
        canonical_by_norm.setdefault(child_norm, child)
        canonical_by_norm.setdefault(parent_norm, parent)
        parents[child_norm].add(parent_norm)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(
                f"Exposure hierarchy cycle includes {canonical_by_norm.get(node, node)!r}"
            )
        if node in visited:
            return
        visiting.add(node)
        for parent in parents.get(node, set()):
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for node in canonical_by_norm:
        visit(node)
    return {
        canonical_by_norm[child]: tuple(
            canonical_by_norm[parent] for parent in sorted(parent_norms)
        )
        for child, parent_norms in parents.items()
    }


def hierarchy_ancestors(
    exposure: str,
    hierarchy: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    hierarchy = hierarchy or exposure_hierarchy()
    parents = {
        child.casefold(): tuple(values) for child, values in hierarchy.items()
    }
    lipid_classes = {
        "Monounsaturated fat (MUFA)".casefold(),
        "Polyunsaturated fat (PUFA)".casefold(),
    }
    explicit_root_classes = {
        parent.casefold()
        for parent in parents.get(str(exposure).casefold(), ())
        if parent.casefold() in lipid_classes
    }
    output: list[str] = []
    seen: set[str] = set()

    def add(child_norm: str) -> None:
        for parent in parents.get(child_norm, ()):
            parent_norm = parent.casefold()
            if (
                parent_norm in lipid_classes
                and explicit_root_classes
                and parent_norm not in explicit_root_classes
            ):
                continue
            if parent_norm in seen:
                continue
            seen.add(parent_norm)
            output.append(parent)
            add(parent_norm)

    add(str(exposure).casefold())
    return output


def dedupe_parent_search_contexts(
    contexts_by_pmid: dict[str, set[tuple[str, str]]],
    hierarchy: dict[str, tuple[str, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Omit duplicate parent contexts and return per-context audit records."""
    hierarchy = hierarchy or exposure_hierarchy()
    omissions: list[dict[str, Any]] = []
    for pmid, contexts in contexts_by_pmid.items():
        by_cancer: dict[str, set[str]] = defaultdict(set)
        for exposure, cancer in contexts:
            by_cancer[cancer].add(exposure)
        for cancer, exposures in by_cancer.items():
            actual_by_norm = {value.casefold(): value for value in exposures}
            children_by_parent: dict[str, set[str]] = defaultdict(set)
            for child in exposures:
                child_norm = child.casefold()
                for ancestor in hierarchy_ancestors(child, hierarchy):
                    parent = actual_by_norm.get(ancestor.casefold())
                    if parent is not None and parent.casefold() != child_norm:
                        children_by_parent[parent].add(child)
            for parent, children in sorted(children_by_parent.items()):
                contexts.discard((parent, cancer))
                omissions.append(
                    {
                        "pmid": str(pmid),
                        "omitted_parent_exposure": parent,
                        "omitted_context": {
                            "exposure": parent,
                            "cancer": cancer,
                            "outcome": "Incidence",
                        },
                        "child_contexts": [
                            {
                                "exposure": child,
                                "cancer": cancer,
                                "outcome": "Incidence",
                            }
                            for child in sorted(children)
                        ],
                        "reason": (
                            "Omitted the redundant parent screening context because "
                            "this PMID also matched a child exposure in the same "
                            "disease/outcome scope; accepted child evidence will roll "
                            "up to the parent after screening."
                        ),
                    }
                )
    return sorted(
        omissions,
        key=lambda item: (
            item["pmid"],
            item["omitted_parent_exposure"].casefold(),
            item["omitted_context"]["cancer"].casefold(),
        ),
    )


def add_candidate_context(
    contexts_by_pmid: dict[str, set[tuple[str, str]]],
    known_contexts: set[tuple[str, str, str]],
    pmid: str,
    exposure: str,
    cancer: str,
    *,
    rollup_vitamin_k: bool = False,
) -> None:
    """Add the child context and, when requested, a non-destructive K parent union."""
    pmid = str(pmid)
    cancer_key = safe_context_part(cancer)
    direct_key = (pmid, safe_context_part(exposure), cancer_key)
    if direct_key not in known_contexts:
        contexts_by_pmid[pmid].add((exposure, cancer))

    child_names = {name.casefold() for name in VITAMIN_K_CHILD_EXPOSURES}
    if rollup_vitamin_k and exposure.casefold() in child_names:
        parent_key = (pmid, safe_context_part(VITAMIN_K_PARENT_EXPOSURE), cancer_key)
        if parent_key not in known_contexts:
            contexts_by_pmid[pmid].add((VITAMIN_K_PARENT_EXPOSURE, cancer))


def quote_term(term: str) -> str:
    escaped = term.replace('"', "")
    return f'"{escaped}"[Title/Abstract]'


CANCER_QUERIES = disease_queries()


def build_query(terms: list[str], cancer_query: str) -> str:
    exposure_query = " OR ".join(quote_term(term) for term in terms)
    return f"(({exposure_query}) AND ({cancer_query}))"


def build_search_jobs(
    terms_by_exposure: dict[str, list[str]],
    cancer_queries: dict[str, str],
) -> list[tuple[str, str, str]]:
    return [
        (exposure, cancer, build_query(terms, cancer_query))
        for exposure, terms in terms_by_exposure.items()
        for cancer, cancer_query in cancer_queries.items()
    ]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def discovery_job_spec(
    exposure: str,
    cancer: str,
    query: str,
) -> dict[str, str]:
    """Return the immutable identity for one exposure/disease ESearch."""
    identity = {
        "exposure": str(exposure),
        "cancer": str(cancer),
        "outcome": DISCOVERY_OUTCOME,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
    }
    return {"job_id": _sha256_json(identity), **identity, "query": query}


def discovery_scope_binding(
    *,
    start_date: str | None,
    end_date: str,
    exposure_scope: str,
    revision_aware: bool,
    rollup_vitamin_k: bool,
    selected_exposures: list[str],
    catalog_digest: str,
    search_jobs: list[tuple[str, str, str]],
    cancer_queries: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Bind resumable ESearch work to the exact reviewed discovery scope."""
    job_specs = [discovery_job_spec(*job) for job in search_jobs]
    job_ids = [job["job_id"] for job in job_specs]
    if len(job_ids) != len(set(job_ids)):
        raise RuntimeError("Discovery scope produced duplicate ESearch job identities")
    binding = {
        "schema_version": DISCOVERY_CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_protocol_version": DISCOVERY_CHECKPOINT_PROTOCOL_VERSION,
        "esearch_protocol_version": ESEARCH_PROTOCOL_VERSION,
        "publication_date_start": start_date,
        "publication_date_end": end_date,
        "exposure_scope": exposure_scope,
        "revision_aware": bool(revision_aware),
        "rollup_vitamin_k": bool(rollup_vitamin_k),
        "selected_exposures": selected_exposures,
        "exposure_catalog_digest": catalog_digest,
        "outcomes": [DISCOVERY_OUTCOME],
        "cancer_queries": [
            {
                "cancer": cancer,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            }
            for cancer, query in cancer_queries.items()
        ],
        "jobs": [
            {key: value for key, value in job.items() if key != "query"}
            for job in job_specs
        ],
    }
    return binding, job_specs


def _validate_discovery_result(
    payload: Any,
    expected_job: dict[str, str],
    *,
    start_date: str | None,
    end_date: str,
) -> dict[str, Any]:
    """Reject corrupt, stale, or scope-swapped per-query checkpoints."""
    if not isinstance(payload, dict):
        raise RuntimeError("Discovery checkpoint contains a non-object result")
    expected_identity = {
        key: expected_job[key]
        for key in ("job_id", "exposure", "cancer", "outcome", "query_sha256")
    }
    actual_identity = {key: payload.get(key) for key in expected_identity}
    if actual_identity != expected_identity:
        raise RuntimeError(
            "Discovery checkpoint result identity does not match the exact current job"
        )
    pmids = payload.get("pmids")
    if (
        not isinstance(pmids, list)
        or any(not str(pmid).isdigit() for pmid in pmids)
        or len(pmids) != len(set(str(pmid) for pmid in pmids))
    ):
        raise RuntimeError("Discovery checkpoint contains invalid or duplicate PMIDs")
    pmids = [str(pmid) for pmid in pmids]
    try:
        request_count = int(payload.get("request_count"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Discovery checkpoint has an invalid request count") from exc
    if request_count < 1:
        raise RuntimeError("Discovery checkpoint request count must be positive")
    audit = payload.get("audit")
    expected_start = start_date or "0001-01-01"
    if not isinstance(audit, dict) or any(
        (
            audit.get("esearch_protocol_version") != ESEARCH_PROTOCOL_VERSION,
            audit.get("requested_start_date") != start_date,
            audit.get("effective_start_date") != expected_start,
            audit.get("requested_end_date") != end_date,
            int(audit.get("reported_count", -1)) != len(pmids),
            int(audit.get("retrieved_count", -1)) != len(pmids),
            int(audit.get("unique_pmid_count", -1)) != len(pmids),
            int(audit.get("esearch_requests", -1)) != request_count,
            not isinstance(audit.get("recursive_windows"), list),
        )
    ):
        raise RuntimeError("Discovery checkpoint contains an invalid ESearch audit")
    return {
        **expected_identity,
        "pmids": pmids,
        "request_count": request_count,
        "audit": audit,
    }


def open_discovery_checkpoint(
    path: Path,
    binding: dict[str, Any],
    job_specs: list[dict[str, str]],
    *,
    start_date: str | None,
    end_date: str,
    force: bool = False,
) -> tuple[sqlite3.Connection, dict[str, dict[str, Any]]]:
    """Open and fully verify the transactional per-query discovery ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("Discovery checkpoint database failed integrity_check")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS results ("
            "job_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL)"
        )
        if force:
            connection.execute("DELETE FROM results")
            connection.execute("DELETE FROM metadata")
            connection.commit()

        expected_scope_json = _canonical_json(binding)
        expected_scope_digest = hashlib.sha256(
            expected_scope_json.encode("utf-8")
        ).hexdigest()
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if not metadata:
            metadata = {
                "schema_version": str(DISCOVERY_CHECKPOINT_SCHEMA_VERSION),
                "checkpoint_protocol_version": DISCOVERY_CHECKPOINT_PROTOCOL_VERSION,
                "scope_digest": expected_scope_digest,
                "scope_json": expected_scope_json,
            }
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
            )
            connection.commit()
        elif metadata != {
            "schema_version": str(DISCOVERY_CHECKPOINT_SCHEMA_VERSION),
            "checkpoint_protocol_version": DISCOVERY_CHECKPOINT_PROTOCOL_VERSION,
            "scope_digest": expected_scope_digest,
            "scope_json": expected_scope_json,
        }:
            raise RuntimeError(
                "Discovery checkpoint scope is stale or tampered; refusing to mix "
                "cutoffs, vocabularies, outcomes, or cancer queries"
            )

        expected_by_id = {job["job_id"]: job for job in job_specs}
        completed: dict[str, dict[str, Any]] = {}
        for job_id, payload_json, payload_sha256 in connection.execute(
            "SELECT job_id, payload_json, payload_sha256 FROM results"
        ):
            expected_job = expected_by_id.get(str(job_id))
            if expected_job is None:
                raise RuntimeError(
                    "Discovery checkpoint contains a job outside the exact current scope"
                )
            if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha256:
                raise RuntimeError("Discovery checkpoint result digest mismatch")
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Discovery checkpoint result is not valid JSON") from exc
            completed[str(job_id)] = _validate_discovery_result(
                payload,
                expected_job,
                start_date=start_date,
                end_date=end_date,
            )
        return connection, completed
    except Exception:
        connection.close()
        raise


def save_discovery_result(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
) -> None:
    """Commit one successful ESearch before advancing to later work."""
    payload_json = _canonical_json(payload)
    payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    try:
        connection.execute(
            "INSERT INTO results(job_id, payload_json, payload_sha256) VALUES (?, ?, ?)",
            (payload["job_id"], payload_json, payload_digest),
        )
        connection.commit()
    except sqlite3.IntegrityError as exc:
        raise RuntimeError(
            f"Discovery checkpoint already contains job {payload.get('job_id')}"
        ) from exc


def seal_discovery_checkpoint(connection: sqlite3.Connection, path: Path) -> str:
    """Flush the SQLite WAL and return the sealed database byte digest."""
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sealed_discovery_checkpoint_matches(
    candidate_packet: dict[str, Any],
    output_dir: Path,
    *,
    expected_scope_digest: str,
    expected_jobs: int,
    required: bool,
) -> bool:
    """Verify the sealed ESearch ledger before reusing a candidate packet."""
    checkpoint = candidate_packet.get("discovery_query_checkpoint")
    if checkpoint is None:
        return not required
    if not isinstance(checkpoint, dict):
        raise RuntimeError("Candidate discovery checkpoint metadata is invalid")
    expected_metadata = {
        "schema_version": DISCOVERY_CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_protocol_version": DISCOVERY_CHECKPOINT_PROTOCOL_VERSION,
        "scope_digest": expected_scope_digest,
        "completed_jobs": expected_jobs,
        "path": "discovery_query_checkpoint.sqlite3",
    }
    for key, value in expected_metadata.items():
        if checkpoint.get(key) != value:
            raise RuntimeError(
                "Candidate discovery checkpoint metadata is stale or tampered"
            )
    recorded_digest = str(checkpoint.get("sha256") or "")
    checkpoint_path = output_dir / expected_metadata["path"]
    if not re.fullmatch(r"[0-9a-f]{64}", recorded_digest):
        raise RuntimeError("Candidate discovery checkpoint digest is invalid")
    if not checkpoint_path.is_file():
        raise RuntimeError("Candidate discovery checkpoint database is missing")
    actual_digest = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    if actual_digest != recorded_digest:
        raise RuntimeError("Candidate discovery checkpoint database was tampered")
    return True


def entrez_http_status_codes(error: BaseException) -> list[int]:
    """Extract HTTP statuses even when Bio.Entrez wraps them in RuntimeError."""
    statuses: list[int] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if isinstance(code, int):
            statuses.append(code)
        statuses.extend(
            int(value)
            for value in re.findall(r"(?<!\d)([45]\d{2})(?!\d)", str(current))
        )
        current = current.__cause__ or current.__context__
    return statuses


def retryable_entrez_error(error: Exception) -> bool:
    """Classify transient NCBI failures without retrying deterministic invariants."""
    statuses = entrez_http_status_codes(error)
    if statuses:
        # 400-series request errors are deterministic except for explicit rate
        # limiting. Bio.Entrez may wrap 429/5xx HTTP errors as RuntimeError.
        return any(code == 429 or 500 <= code <= 599 for code in statuses)
    if isinstance(error, RuntimeError):
        return False
    message = str(error).casefold()
    return isinstance(error, (TimeoutError, ConnectionError, OSError)) or any(
        marker in message
        for marker in (
            "timed out",
            "connection reset",
            "temporarily unavailable",
            "remote end closed",
            "incomplete read",
        )
    )


def _entrez_search_page(
    query: str,
    start_date: date,
    end_date: date,
    *,
    retstart: int,
    retmax: int,
) -> tuple[dict[str, Any], int]:
    """Fetch one ESearch page and report every attempted request."""
    request_count = 0
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request_count += 1
            wait_for_ncbi_request_slot()
            with Entrez.esearch(
                db="pubmed",
                term=query,
                retstart=retstart,
                retmax=retmax,
                datetype="pdat",
                mindate=start_date.isoformat().replace("-", "/"),
                maxdate=end_date.isoformat().replace("-", "/"),
            ) as handle:
                record = Entrez.read(handle)
            if not isinstance(record, dict):
                raise RuntimeError("NCBI ESearch returned a non-object payload")
            return record, request_count
        except Exception as exc:  # pragma: no cover - network retry path
            last_error = exc
            if not retryable_entrez_error(exc) or attempt == 3:
                break
            time.sleep(2 ** attempt)
    raise RuntimeError(
        "NCBI ESearch failed after "
        f"{request_count} request(s) for {start_date} through {end_date}, "
        f"retstart={retstart}: {last_error}"
    )


def _parse_esearch_page(
    record: dict[str, Any], *, expected_count: int | None = None
) -> tuple[int, list[str]]:
    raw_count = record.get("Count")
    try:
        count = int(str(raw_count))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"NCBI ESearch returned invalid Count={raw_count!r}") from exc
    if count < 0:
        raise RuntimeError(f"NCBI ESearch returned negative Count={count}")
    if expected_count is not None and count != expected_count:
        raise RuntimeError(
            "NCBI ESearch Count changed during pagination "
            f"({expected_count} to {count}); refusing an unstable result"
        )
    raw_ids = record.get("IdList")
    if not isinstance(raw_ids, (list, tuple)):
        raise RuntimeError("NCBI ESearch returned a non-list IdList")
    ids = [str(value).strip() for value in raw_ids]
    if any(not value.isdigit() for value in ids):
        raise RuntimeError("NCBI ESearch returned a non-numeric PubMed ID")
    return count, ids


def _entrez_search_date_slice(
    query: str,
    start_date: date,
    end_date: date,
    *,
    audit_windows: list[dict[str, Any]],
) -> tuple[list[str], int]:
    """Retrieve a complete date slice without crossing PubMed's 10k UID cap."""
    first, request_count = _entrez_search_page(
        query,
        start_date,
        end_date,
        retstart=0,
        retmax=ESEARCH_PAGE_SIZE,
    )
    reported_count, first_ids = _parse_esearch_page(first)
    audit_entry: dict[str, Any] = {
        "window_start": start_date.isoformat(),
        "window_end": end_date.isoformat(),
        "reported_count": reported_count,
        "retrieved_count": 0,
        "direct_esearch_requests": request_count,
        "split_required": reported_count > PUBMED_ESEARCH_UID_LIMIT,
    }
    audit_windows.append(audit_entry)

    if reported_count > PUBMED_ESEARCH_UID_LIMIT:
        if start_date >= end_date:
            raise RuntimeError(
                "PubMed ESearch reported "
                f"{reported_count:,} results on {start_date}, exceeding the "
                f"{PUBMED_ESEARCH_UID_LIMIT:,}-UID single-day limit"
            )
        span_days = (end_date - start_date).days
        left_end = start_date + timedelta(days=span_days // 2)
        right_start = left_end + timedelta(days=1)
        left_ids, left_requests = _entrez_search_date_slice(
            query, start_date, left_end, audit_windows=audit_windows
        )
        right_ids, right_requests = _entrez_search_date_slice(
            query, right_start, end_date, audit_windows=audit_windows
        )
        combined = left_ids + right_ids
        occurrences = Counter(combined)
        unique_ids = list(dict.fromkeys(combined))
        duplicate_counts = {
            pmid: count
            for pmid, count in sorted(occurrences.items(), key=lambda item: int(item[0]))
            if count > 1
        }
        audit_entry.update(
            {
                "child_window_reported_matches": len(combined),
                "cross_child_duplicate_occurrences": sum(
                    count - 1 for count in duplicate_counts.values()
                ),
                "cross_child_duplicate_pmids": list(duplicate_counts),
                "cross_child_duplicate_counts": duplicate_counts,
                "retrieved_count": len(unique_ids),
            }
        )
        if not set(first_ids).issubset(unique_ids) or len(unique_ids) != reported_count:
            raise RuntimeError(
                "PubMed Count/results changed while recursively splitting a date "
                f"window ({reported_count:,} reported; {len(unique_ids):,} unique returned)"
            )
        return unique_ids, request_count + left_requests + right_requests

    all_ids = list(first_ids)
    for retstart in range(ESEARCH_PAGE_SIZE, reported_count, ESEARCH_PAGE_SIZE):
        page, page_requests = _entrez_search_page(
            query,
            start_date,
            end_date,
            retstart=retstart,
            retmax=min(ESEARCH_PAGE_SIZE, reported_count - retstart),
        )
        request_count += page_requests
        _, page_ids = _parse_esearch_page(page, expected_count=reported_count)
        all_ids.extend(page_ids)
    unique_ids = list(dict.fromkeys(all_ids))
    if len(unique_ids) != reported_count:
        raise RuntimeError(
            "Paginated PubMed ESearch returned "
            f"{len(unique_ids):,} unique PMIDs but Count reported {reported_count:,}"
        )
    audit_entry["direct_esearch_requests"] = request_count
    audit_entry["retrieved_count"] = len(unique_ids)
    return unique_ids, request_count


def entrez_search(
    query: str,
    start_date: str | None,
    end_date: str,
    *,
    return_audit: bool = False,
) -> list[str] | tuple[list[str], int, dict[str, Any]]:
    """Search a date window, recursively splitting every result set over 10k."""
    effective_start = date.fromisoformat(start_date or "0001-01-01")
    effective_end = date.fromisoformat(end_date)
    if effective_start > effective_end:
        raise ValueError("PubMed search start_date must not be after end_date")
    windows: list[dict[str, Any]] = []
    identifiers, request_count = _entrez_search_date_slice(
        query, effective_start, effective_end, audit_windows=windows
    )
    audit = {
        "esearch_protocol_version": ESEARCH_PROTOCOL_VERSION,
        "requested_start_date": start_date,
        "effective_start_date": effective_start.isoformat(),
        "requested_end_date": end_date,
        "reported_count": windows[0]["reported_count"],
        "retrieved_count": len(identifiers),
        "unique_pmid_count": len(identifiers),
        "esearch_requests": request_count,
        "recursive_split_required": any(
            bool(window["split_required"]) for window in windows
        ),
        "recursive_windows": windows,
    }
    return (identifiers, request_count, audit) if return_audit else identifiers


def scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(scalar_text(item) for item in value).strip()
    return str(value).strip()


def journal_publication_date(article: dict[str, Any]) -> str:
    citation = article.get("MedlineCitation", {})
    article_data = citation.get("Article", {})
    # PubMed's PDAT filter follows the issue/publication date. Prefer that over
    # ArticleDate, which is commonly the earlier electronic-publication date.
    pub_date = article_data.get("Journal", {}).get("JournalIssue", {}).get("PubDate", {})
    year = scalar_text(pub_date.get("Year"))
    month_raw = scalar_text(pub_date.get("Month"))
    day = scalar_text(pub_date.get("Day")) or "01"
    if year:
        if not month_raw:
            return year
        try:
            month = str(list(calendar.month_abbr).index(month_raw[:3].title())).zfill(2)
        except ValueError:
            month = month_raw.zfill(2) if month_raw.isdigit() else "01"
        return f"{year}-{month}-{day.zfill(2)}"
    if scalar_text(pub_date.get("MedlineDate")):
        match = re.search(r"\b(19|20)\d{2}\b", scalar_text(pub_date.get("MedlineDate")))
        return match.group(0) if match else "Unknown"

    return "Unknown"


def electronic_publication_date(article: dict[str, Any]) -> str:
    article_data = article.get("MedlineCitation", {}).get("Article", {})
    article_dates = article_data.get("ArticleDate", [])
    if article_dates:
        value = article_dates[0]
        year = scalar_text(value.get("Year"))
        month = scalar_text(value.get("Month")) or "01"
        day = scalar_text(value.get("Day")) or "01"
        if year:
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return "Unknown"


def article_record(article: dict[str, Any]) -> dict[str, Any]:
    citation = article.get("MedlineCitation", {})
    article_data = citation.get("Article", {})
    pmid = scalar_text(citation.get("PMID"))
    abstract_parts = article_data.get("Abstract", {}).get("AbstractText", [])
    abstract = " ".join(scalar_text(part) for part in abstract_parts).strip()
    mesh = []
    for heading in citation.get("MeshHeadingList", []):
        descriptor = scalar_text(heading.get("DescriptorName"))
        qualifiers = [scalar_text(value) for value in heading.get("QualifierName", [])]
        mesh.append(descriptor + (" / " + ", ".join(qualifiers) if qualifiers else ""))
    identifiers = {}
    for item in article.get("PubmedData", {}).get("ArticleIdList", []):
        id_type = str(getattr(item, "attributes", {}).get("IdType", "")).lower()
        identifiers[id_type] = scalar_text(item)
    authors = []
    for author in article_data.get("AuthorList", []):
        collective = scalar_text(author.get("CollectiveName"))
        if collective:
            authors.append(collective)
        else:
            name = " ".join(filter(None, [scalar_text(author.get("LastName")), scalar_text(author.get("Initials"))]))
            if name:
                authors.append(name)
    return {
        "pmid": pmid,
        "pmcid": identifiers.get("pmc", ""),
        "doi": identifiers.get("doi", ""),
        "published_date": journal_publication_date(article),
        "journal_publication_date": journal_publication_date(article),
        "electronic_publication_date": electronic_publication_date(article),
        "title": scalar_text(article_data.get("ArticleTitle")),
        "abstract": abstract,
        "mesh": mesh,
        "publication_types": [scalar_text(value) for value in article_data.get("PublicationTypeList", [])],
        "authors": authors,
        "journal": scalar_text(article_data.get("Journal", {}).get("Title")),
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
    }


def book_article_record(article: dict[str, Any]) -> dict[str, Any]:
    """Parse PubmedBookArticle records, which are absent from PubmedArticle."""
    document = article.get("BookDocument", {})
    book = document.get("Book", {})
    pmid = scalar_text(document.get("PMID"))
    abstract = " ".join(
        scalar_text(part) for part in document.get("Abstract", {}).get("AbstractText", [])
    ).strip()
    authors = []
    for author_group in document.get("AuthorList", []):
        for author in author_group if isinstance(author_group, (list, tuple)) else [author_group]:
            collective = scalar_text(author.get("CollectiveName"))
            name = collective or " ".join(
                filter(None, [scalar_text(author.get("LastName")), scalar_text(author.get("Initials"))])
            )
            if name:
                authors.append(name)
    identifiers = {}
    for item in article.get("PubmedBookData", {}).get("ArticleIdList", []):
        id_type = str(getattr(item, "attributes", {}).get("IdType", "")).lower()
        identifiers[id_type] = scalar_text(item)
    pub_date = book.get("PubDate", {})
    year = scalar_text(pub_date.get("Year"))
    month = scalar_text(pub_date.get("Month"))
    day = scalar_text(pub_date.get("Day"))
    published = year or "Unknown"
    if year and month:
        published = f"{year}-{month.zfill(2)}-{(day or '01').zfill(2)}"
    return {
        "pmid": pmid,
        "pmcid": identifiers.get("pmc", ""),
        "doi": identifiers.get("doi", ""),
        "published_date": published,
        "title": (
            scalar_text(document.get("ArticleTitle"))
            or scalar_text(book.get("BookTitle"))
        ),
        "abstract": abstract,
        "mesh": [],
        "publication_types": [scalar_text(value) for value in document.get("PublicationType", [])],
        "authors": authors,
        "journal": scalar_text(book.get("BookTitle")),
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
    }


def date_overlaps_window(value: str, start_date: str | None, end_date: str) -> bool:
    """Return whether a PubMed date, including year/month precision, overlaps."""
    match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", str(value or ""))
    if not match:
        # Preserve records with unknown dates for sensitive downstream screening.
        return True
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    first = date(year, month, int(match.group(3) or 1))
    if match.group(3):
        last = first
    elif match.group(2):
        last = date(year, month, calendar.monthrange(year, month)[1])
    else:
        last = date(year, 12, 31)
    lower_bound_matches = start_date is None or last >= date.fromisoformat(start_date)
    return lower_bound_matches and first <= date.fromisoformat(end_date)


def publication_window_text(start_date: str | None, end_date: str) -> str:
    if start_date is None:
        return f"no lower publication-date limit through {end_date}"
    return f"{start_date} through {end_date}"


def select_in_window_publication_date(
    record: dict[str, Any],
    start_date: str | None,
    end_date: str,
) -> str | None:
    """Select the PubMed online or issue publication date falling in-window."""
    issue_date = record.get("journal_publication_date") or record.get("published_date")
    electronic_date = record.get("electronic_publication_date")
    if issue_date and issue_date != "Unknown" and date_overlaps_window(str(issue_date), start_date, end_date):
        return str(issue_date)
    if electronic_date and electronic_date != "Unknown" and date_overlaps_window(str(electronic_date), start_date, end_date):
        return str(electronic_date)
    # Unknown dates remain eligible for sensitive screening; known out-of-range
    # dates do not. PubmedBookArticle has no ArticleDate, preventing a 2026
    # contribution/update date from turning a 2012 book chapter into a new paper.
    if str(issue_date or "Unknown") == "Unknown" and str(electronic_date or "Unknown") == "Unknown":
        return "Unknown"
    return None


def entrez_fetch(pmids: list[str]) -> tuple[dict[str, dict[str, Any]], int]:
    records: dict[str, dict[str, Any]] = {}
    request_count = 0
    for offset in range(0, len(pmids), 200):
        batch = pmids[offset : offset + 200]
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                wait_for_ncbi_request_slot()
                with Entrez.efetch(db="pubmed", id=",".join(batch), retmode="xml") as handle:
                    payload = Entrez.read(handle)
                request_count += 1
                for article in payload.get("PubmedArticle", []):
                    parsed = article_record(article)
                    if parsed["pmid"]:
                        records[parsed["pmid"]] = parsed
                for article in payload.get("PubmedBookArticle", []):
                    parsed = book_article_record(article)
                    if parsed["pmid"]:
                        records[parsed["pmid"]] = parsed
                last_error = None
                break
            except Exception as exc:  # pragma: no cover - network retry path
                last_error = exc
                if not retryable_entrez_error(exc) or attempt == 3:
                    break
                time.sleep(2 ** attempt)
        if last_error:
            raise RuntimeError(f"NCBI EFetch failed after retries: {last_error}")
    requested = {str(pmid) for pmid in pmids}
    unexpected = set(records) - requested
    if unexpected:
        raise RuntimeError(
            "NCBI EFetch returned unexpected PMID(s): "
            + ", ".join(sorted(unexpected, key=int))
        )
    missing = requested - set(records)
    if missing:
        raise RuntimeError(
            "NCBI EFetch omitted requested PMID(s): "
            + ", ".join(sorted(missing, key=int))
        )
    return records, request_count


def discover(
    start_date: str | None,
    end_date: str,
    output_dir: Path,
    force: bool = False,
    requested_exposures: list[str] | None = None,
    rollup_vitamin_k: bool = False,
    exposure_scope: str = "current",
    revision_aware: bool = False,
) -> dict[str, Any]:
    require_output_not_superseded(output_dir)
    effective_scope = "all" if revision_aware else exposure_scope
    available_terms = exposure_terms(effective_scope)
    if revision_aware:
        revised = revision_scope_names()
        additional = {
            name.casefold() for name in additional_exposure_entries()
        }
        available_terms = {
            name: terms
            for name, terms in available_terms.items()
            if name.casefold() in revised or name.casefold() in additional
        }
    terms_by_exposure = filter_exposure_terms(
        available_terms, requested_exposures
    )
    all_catalog_sources = exposure_catalog_sources(effective_scope)
    catalog_source_by_exposure = {
        exposure: all_catalog_sources.get(exposure, "current")
        for exposure in terms_by_exposure
    }
    if not terms_by_exposure:
        raise ValueError(
            f"Exposure scope {exposure_scope!r} contains no searchable entries"
        )
    selected_exposures = list(terms_by_exposure)
    catalog_digest = exposure_catalog_digest(terms_by_exposure)
    search_jobs = build_search_jobs(terms_by_exposure, CANCER_QUERIES)
    checkpoint_binding, checkpoint_job_specs = discovery_scope_binding(
        start_date=start_date,
        end_date=end_date,
        exposure_scope=exposure_scope,
        revision_aware=revision_aware,
        rollup_vitamin_k=rollup_vitamin_k,
        selected_exposures=selected_exposures,
        catalog_digest=catalog_digest,
        search_jobs=search_jobs,
        cancer_queries=CANCER_QUERIES,
    )
    checkpoint_scope_digest = _sha256_json(checkpoint_binding)
    checkpoint_path = output_dir / "discovery_query_checkpoint.sqlite3"
    candidate_path = output_dir / "candidates.json"
    existing = load_json(candidate_path, {})
    selection_matches = (
        existing.get("selected_exposures") == selected_exposures
        if "selected_exposures" in existing
        else not requested_exposures
    )
    rollup_matches = (
        existing.get("rollup_vitamin_k") == bool(rollup_vitamin_k)
        if "rollup_vitamin_k" in existing
        else not rollup_vitamin_k
    )
    scope_matches = existing.get("exposure_scope", "current") == exposure_scope
    revision_matches = existing.get("revision_aware", False) == bool(revision_aware)
    catalog_matches = (
        existing.get("exposure_catalog_digest") == catalog_digest
        if "exposure_catalog_digest" in existing
        else exposure_scope == "current" and not requested_exposures
    )
    if (
        not force
        and isinstance(existing.get("articles"), list)
        and "publication_date_start" in existing
        and existing.get("publication_date_start") == start_date
        and existing.get("publication_date_end") == end_date
        and selection_matches
        and rollup_matches
        and scope_matches
        and revision_matches
        and catalog_matches
        and isinstance(existing.get("redundant_parent_context_omissions"), list)
        and existing.get("esearch_protocol_version") == ESEARCH_PROTOCOL_VERSION
        and isinstance(existing.get("esearch_audit"), list)
        and sealed_discovery_checkpoint_matches(
            existing,
            output_dir,
            expected_scope_digest=checkpoint_scope_digest,
            expected_jobs=len(checkpoint_job_specs),
            required=revision_aware,
        )
    ):
        window = publication_window_text(start_date, end_date)
        print(f"Reusing immutable candidate packet for {window}; use --force to rediscover.", flush=True)
        return existing
    api_key_status = configure_entrez()
    known_contexts = set() if revision_aware else saved_contexts()
    prior_direct_contexts = (
        included_direct_contexts(
            terms_by_exposure, CANCER_QUERIES
        )
        if revision_aware
        else set()
    )
    contexts_by_pmid: dict[str, set[tuple[str, str]]] = defaultdict(set)
    queries = 0
    esearch_requests = 0
    esearch_audit: list[dict[str, Any]] = []
    total_contexts = len(terms_by_exposure) * len(CANCER_QUERIES)
    started = time.time()

    checkpoint, completed_searches = open_discovery_checkpoint(
        checkpoint_path,
        checkpoint_binding,
        checkpoint_job_specs,
        start_date=start_date,
        end_date=end_date,
        force=force,
    )
    checkpoint_jobs_by_id = {
        job["job_id"]: job for job in checkpoint_job_specs
    }

    def add_search_result(result: dict[str, Any]) -> None:
        nonlocal queries, esearch_requests
        exposure = result["exposure"]
        cancer = result["cancer"]
        for pmid in result["pmids"]:
            add_candidate_context(
                contexts_by_pmid,
                known_contexts,
                pmid,
                exposure,
                cancer,
                rollup_vitamin_k=rollup_vitamin_k,
            )
        queries += 1
        esearch_requests += result["request_count"]
        esearch_audit.append(
            {"exposure": exposure, "cancer": cancer, **result["audit"]}
        )

    for job in checkpoint_job_specs:
        completed = completed_searches.get(job["job_id"])
        if completed is not None:
            add_search_result(completed)
    if completed_searches:
        print(
            f"Resuming discovery from {len(completed_searches)}/{total_contexts} "
            "durably verified exposure-cancer searches.",
            flush=True,
        )

    def run_search(
        job: dict[str, str],
    ) -> dict[str, Any]:
        found, request_count, audit = entrez_search(
            job["query"], start_date, end_date, return_audit=True
        )
        return {
            **{key: job[key] for key in (
                "job_id", "exposure", "cancer", "outcome", "query_sha256"
            )},
            "pmids": [str(pmid) for pmid in found],
            "request_count": request_count,
            "audit": audit,
        }

    # Five workers stay below NCBI's keyed 10-request/second allowance while
    # avoiding hundreds of sequential network round trips.
    remaining_jobs = [
        job for job in checkpoint_job_specs
        if job["job_id"] not in completed_searches
    ]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(run_search, job) for job in remaining_jobs]
            for future in concurrent.futures.as_completed(futures):
                raw_result = future.result()
                expected_job = checkpoint_jobs_by_id.get(raw_result.get("job_id"))
                if expected_job is None:
                    raise RuntimeError(
                        "ESearch worker returned a job outside the exact current scope"
                    )
                result = _validate_discovery_result(
                    raw_result,
                    expected_job,
                    start_date=start_date,
                    end_date=end_date,
                )
                save_discovery_result(checkpoint, result)
                add_search_result(result)
                if queries % 75 == 0 or queries == total_contexts:
                    print(
                        f"Discovery progress: {queries}/{total_contexts} exposure-cancer searches; "
                        f"{len(contexts_by_pmid)} unique unsaved PMIDs",
                        flush=True,
                    )
    except BaseException:
        checkpoint.close()
        raise
    checkpoint_sha256 = seal_discovery_checkpoint(checkpoint, checkpoint_path)

    for pmid, exposure, cancer in prior_direct_contexts:
        contexts_by_pmid[pmid].add((exposure, cancer))

    redundant_parent_context_omissions = dedupe_parent_search_contexts(
        contexts_by_pmid
    )
    save_json(
        output_dir / "redundant_parent_context_omissions.json",
        {
            "schema_version": 1,
            "created_at": datetime.now().astimezone().isoformat(),
            "redundant_parent_contexts_omitted": len(
                redundant_parent_context_omissions
            ),
            "records": redundant_parent_context_omissions,
        },
    )
    pmids = sorted(contexts_by_pmid, key=lambda value: int(value))
    esearch_audit.sort(key=lambda item: (item["exposure"].casefold(), item["cancer"].casefold()))
    save_json(
        output_dir / "esearch_audit.json",
        {
            "schema_version": 1,
            "esearch_protocol_version": ESEARCH_PROTOCOL_VERSION,
            "search_jobs": queries,
            "esearch_requests": esearch_requests,
            "records": esearch_audit,
        },
    )
    articles, fetch_requests = entrez_fetch(pmids)
    candidate_records = []
    out_of_window = []
    for pmid in pmids:
        record = articles.get(pmid, {"pmid": pmid})
        record["candidate_contexts"] = [
            {
                "exposure": exposure,
                "cancer": cancer,
                "outcome": "Incidence",
                "catalog_source": catalog_source_by_exposure.get(
                    exposure, "current"
                ),
                "included_source_context": (
                    pmid, exposure, cancer
                ) in prior_direct_contexts,
            }
            for exposure, cancer in sorted(contexts_by_pmid[pmid])
        ]
        selected_date = select_in_window_publication_date(record, start_date, end_date)
        has_prior_direct_context = any(
            (
                pmid,
                context["exposure"],
                context["cancer"],
            )
            in prior_direct_contexts
            for context in record["candidate_contexts"]
        )
        if selected_date is None and not has_prior_direct_context:
            out_of_window.append(record)
            continue
        record["published_date"] = (
            selected_date
            or record.get("journal_publication_date")
            or record.get("electronic_publication_date")
            or "Unknown"
        )
        record["prior_direct_context_outside_window"] = bool(
            selected_date is None and has_prior_direct_context
        )
        candidate_records.append(record)

    packet = {
        "schema_version": 1,
        "esearch_protocol_version": ESEARCH_PROTOCOL_VERSION,
        "discovery_query_checkpoint": {
            "schema_version": DISCOVERY_CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_protocol_version": DISCOVERY_CHECKPOINT_PROTOCOL_VERSION,
            "scope_digest": checkpoint_scope_digest,
            "completed_jobs": queries,
            "path": checkpoint_path.name,
            "sha256": checkpoint_sha256,
        },
        "created_at": datetime.now().astimezone().isoformat(),
        "publication_date_start": start_date,
        "publication_date_end": end_date,
        "listed_exposures": len(terms_by_exposure),
        "selected_exposures": selected_exposures,
        "catalog_sources": catalog_source_by_exposure,
        "exposure_scope": exposure_scope,
        "revision_aware": bool(revision_aware),
        "exposure_catalog_digest": catalog_digest,
        "rollup_vitamin_k": bool(rollup_vitamin_k),
        "cancer_types": list(CANCER_QUERIES),
        "saved_contexts_omitted": len(known_contexts),
        "included_direct_contexts_seeded": len(
            prior_direct_contexts
        ),
        "redundant_parent_contexts_omitted": len(
            redundant_parent_context_omissions
        ),
        "redundant_parent_context_omissions": redundant_parent_context_omissions,
        "ncbi_api_key_status": api_key_status,
        "esearch_audit": esearch_audit,
        "ncbi_requests": {
            "esearch_jobs": queries,
            "esearch": esearch_requests,
            "efetch": fetch_requests,
            "total": esearch_requests + fetch_requests,
        },
        "candidate_pmids": len(candidate_records),
        "candidate_contexts": sum(len(item["candidate_contexts"]) for item in candidate_records),
        "postfetch_out_of_window_omitted": len(out_of_window),
        "elapsed_seconds": round(time.time() - started, 1),
        "articles": candidate_records,
    }
    save_json(output_dir / "candidates.json", packet)
    save_json(
        output_dir / "out_of_window_records.json",
        {"count": len(out_of_window), "records": out_of_window},
    )
    print(json.dumps({key: value for key, value in packet.items() if key != "articles"}, indent=2))
    return packet


def refresh_candidate_metadata(output_dir: Path) -> dict[str, Any]:
    """Refresh cached PubMed metadata without repeating discovery or LLM calls."""
    require_output_not_superseded(output_dir)
    path = output_dir / "candidates.json"
    packet = load_json(path, {})
    configure_entrez(
        validate_api_key=packet.get("run_mode") != "included_reclassification"
    )
    articles = list(packet.get("articles", []))
    # A refresh may be correcting an earlier date-selection rule, so include
    # records previously parked as out-of-window rather than losing them.
    prior_out = load_json(output_dir / "out_of_window_records.json", {}).get("records", [])
    by_pmid = {str(item.get("pmid")): item for item in articles + list(prior_out)}
    articles = list(by_pmid.values())
    if not articles:
        raise RuntimeError(f"No candidates found in {path}")
    contexts = {
        str(item.get("pmid")): item.get("candidate_contexts", []) for item in articles
    }
    refreshed, requests = entrez_fetch(sorted(contexts, key=int))
    retained = []
    out_of_window = []
    start_date = packet["publication_date_start"]
    end_date = packet["publication_date_end"]
    for pmid, candidate_contexts in contexts.items():
        record = refreshed.get(pmid, next(item for item in articles if str(item.get("pmid")) == pmid))
        record["candidate_contexts"] = candidate_contexts
        selected_date = select_in_window_publication_date(record, start_date, end_date)
        if selected_date is not None:
            record["published_date"] = selected_date
            retained.append(record)
        else:
            out_of_window.append(record)
    retained.sort(key=lambda item: int(item.get("pmid") or 0))
    packet["articles"] = retained
    packet["candidate_pmids"] = len(retained)
    packet["candidate_contexts"] = sum(len(item.get("candidate_contexts", [])) for item in retained)
    packet["postfetch_out_of_window_omitted"] = len(out_of_window)
    packet["metadata_refreshed_at"] = datetime.now().astimezone().isoformat()
    packet.setdefault("ncbi_requests", {})["metadata_refresh_efetch"] = requests
    packet["ncbi_requests"]["total"] = sum(
        int(value) for key, value in packet["ncbi_requests"].items() if key != "total"
    )
    save_json(path, packet)
    save_json(
        output_dir / "out_of_window_records.json",
        {"count": len(out_of_window), "records": out_of_window},
    )

    valid_pmids = {str(item.get("pmid")) for item in retained}
    article_by_pmid = {str(item.get("pmid")): item for item in retained}
    for filename in (
        "first_screen.json",
        "second_screen.json",
        "chatgpt_first_screen.json",
        "chatgpt_second_screen.json",
        "deepseek_first_screen.json",
        "deepseek_second_screen.json",
    ):
        packet_path = output_dir / filename
        screen = load_json(packet_path, {})
        if not screen:
            continue
        screen["results"] = [
            item for item in screen.get("results", [])
            if str(item.get("pmid") or str(item.get("context_id", "")).split("|", 1)[0]) in valid_pmids
        ]
        is_first = filename.endswith("first_screen.json")
        track_prefix = filename.split("_first_screen", 1)[0] if is_first else filename.split("_second_screen", 1)[0]
        if not is_first:
            for item in screen["results"]:
                article = article_by_pmid.get(str(item.get("pmid")), {})
                for key in ("pmcid", "published_date", "title", "authors", "journal", "pubmed_url"):
                    item[key] = article.get(key) or ("" if key != "authors" else [])
        if is_first:
            screen["source_contexts"] = packet["candidate_contexts"]
            screen["pass"] = sum(item.get("decision") == "PASS" for item in screen["results"])
            screen["fail"] = sum(item.get("decision") == "FAIL" for item in screen["results"])
        else:
            screen["source_first_screen_pass"] = sum(
                item.get("decision") == "PASS"
                for item in load_json(
                    output_dir / (
                        f"{track_prefix}_first_screen.json"
                        if track_prefix in dual_screening.TRACK_NAMES
                        else "first_screen.json"
                    ),
                    {},
                ).get("results", [])
            )
            screen["include"] = sum(item.get("eligibility") == "INCLUDE" for item in screen["results"])
            screen["exclude"] = sum(item.get("eligibility") == "EXCLUDE" for item in screen["results"])
            screen["needs_full_text"] = sum(bool(item.get("needs_full_text")) for item in screen["results"])
            screen["pmcid_full_text_candidates"] = sum(
                bool(item.get("needs_full_text")) and bool(item.get("pmcid")) for item in screen["results"]
            )
        save_json(packet_path, screen)
    print(f"Refreshed {len(retained)} candidate records with {requests} NCBI EFetch calls; omitted {len(out_of_window)} truly out-of-window records.")
    return packet


def context_id(pmid: str, exposure: str, cancer: str) -> str:
    return f"{pmid}|{exposure}|{cancer}"


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def write_reports(output_dir: Path, report_dir: Path) -> dict[str, Any]:
    require_output_not_superseded(output_dir)
    candidates = load_json(output_dir / "candidates.json", {})
    first_packet = load_json(output_dir / "first_screen.json", {})
    second_packet = load_json(output_dir / "second_screen.json", {})
    if not candidates or not first_packet or not second_packet:
        raise RuntimeError("Candidate, first-screen, and second-screen packets are required")

    articles = candidates.get("articles", [])
    article_by_pmid = {str(article.get("pmid")): article for article in articles}
    first_failures = [item for item in first_packet.get("results", []) if item.get("decision") == "FAIL"]
    second_exclusions = [
        item for item in second_packet.get("results", []) if item.get("eligibility") == "EXCLUDE"
    ]
    included = [item for item in second_packet.get("results", []) if item.get("eligibility") == "INCLUDE"]

    exclusions = []
    for item in first_failures:
        pmid = item["context_id"].split("|", 1)[0]
        article = article_by_pmid.get(pmid, {})
        exclusions.append(
            {
                "pmid": pmid,
                "published_date": article.get("published_date") or "Unknown",
                "title": article.get("title") or "",
                "exposure": item.get("exposure"),
                "cancer": item.get("cancer"),
                "screen_layer": "first",
                "exclusion_code": "S1-NOT-DIRECT",
                "reason": item.get("reason") or "Not a direct exposure-cancer incidence match.",
            }
        )
    for item in second_exclusions:
        exclusions.append(
            {
                "pmid": item.get("pmid"),
                "published_date": item.get("published_date") or "Unknown",
                "title": item.get("title") or "",
                "exposure": item.get("exposure"),
                "cancer": item.get("cancer"),
                "screen_layer": "second",
                "exclusion_code": item.get("exclusion_code"),
                "reason": item.get("reason") or "Excluded by the detailed screen.",
            }
        )
    exclusions.sort(key=lambda item: (str(item["pmid"]), str(item["exposure"]), str(item["cancer"])))
    save_json(output_dir / "exclusions.json", {"count": len(exclusions), "records": exclusions})

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "newStudies.txt"
    candidate_path = report_dir / "newCandidatePMIDs.txt"
    exclusion_path = report_dir / "newStudyExclusions.txt"

    contexts_by_pmid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in included:
        contexts_by_pmid[str(item["pmid"])].append(item)

    report_lines = [
        "MetaPee new-study report",
        "===========================",
        "Publication-date search window: "
        + publication_window_text(
            candidates.get("publication_date_start"),
            candidates.get("publication_date_end"),
        ),
        "Urinary-system cancer types: "
        + ", ".join(candidates.get("cancer_types") or CANCER_QUERIES),
        "Outcome: Incidence",
        f"Listed exposures searched: {candidates.get('listed_exposures')}",
        f"Previously saved PMID/exposure/cancer contexts omitted: {candidates.get('saved_contexts_omitted')}",
        f"Unsaved candidate PMIDs indexed: {candidates.get('candidate_pmids')}",
        f"First-screen passing contexts: {first_packet.get('pass')}",
        f"Second-screen eligible contexts: {len(included)}",
        f"Second-screen eligible unique PMIDs: {len(contexts_by_pmid)}",
        f"Eligible contexts needing full text: {sum(item.get('needs_full_text', False) for item in included)}",
        f"Eligible contexts with PMCID: {sum(bool(item.get('pmcid')) for item in included)}",
        "",
        "IMPORTANT STATUS",
        "----------------",
        "This file was written after both relevance screens and before any PMC full-text retrieval, saved-result mutation, or diagnostic regeneration.",
        "Missing effect sizes or p-values were not used as exclusion reasons.",
        "",
        "PRELIMINARILY ELIGIBLE NEW STUDIES",
        "-----------------------------------",
    ]
    for pmid in sorted(contexts_by_pmid, key=int):
        contexts = contexts_by_pmid[pmid]
        first = contexts[0]
        report_lines.extend(
            [
                "",
                f"PMID: {pmid}",
                f"Published date: {first.get('published_date') or 'Unknown'}",
                f"PMCID: {first.get('pmcid') or 'None found'}",
                f"Title: {one_line(first.get('title')) or 'Not available from PubMedArticle XML'}",
                f"Authors: {', '.join(first.get('authors') or []) or 'Not available'}",
                f"Journal: {one_line(first.get('journal')) or 'Not available'}",
                f"PubMed: {first.get('pubmed_url') or ''}",
            ]
        )
        for context in contexts:
            report_lines.extend(
                [
                    f"  Context: {context.get('exposure')} | {context.get('cancer')} | Incidence",
                    f"  Second-screen reason: {one_line(context.get('reason'))}",
                    f"  Key evidence complete in title/abstract/MeSH: {'Yes' if not context.get('missing_key_fields') else 'No'}",
                    f"  Preliminary JBI: {context.get('jbi', {}).get('grade')} ({context.get('jbi', {}).get('score_percent')}%)",
                    f"  Full text required: {'Yes' if context.get('needs_full_text') else 'No'}",
                    f"  Missing key fields: {', '.join(map(str, context.get('missing_key_fields') or [])) or 'None'}",
                ]
            )

    report_lines.extend(
        [
            "",
            "AUDIT FILES",
            "-----------",
            f"All unsaved candidate PMIDs: {candidate_path.name}",
            f"All first- and second-screen exclusions: {exclusion_path.name}",
            f"Machine-readable audit packet: {output_dir}",
        ]
    )
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    candidate_lines = [
        "Unsaved PubMed candidates before relevance screening",
        "====================================================",
        "PMID | Published date | Candidate contexts | Title",
    ]
    for article in sorted(articles, key=lambda item: int(item.get("pmid") or 0)):
        contexts = "; ".join(
            f"{item['exposure']} / {item['cancer']} / Incidence"
            for item in article.get("candidate_contexts", [])
        )
        candidate_lines.append(
            f"{article.get('pmid')} | {article.get('published_date') or 'Unknown'} | {contexts} | {one_line(article.get('title'))}"
        )
    candidate_path.write_text("\n".join(candidate_lines) + "\n", encoding="utf-8")

    exclusion_lines = [
        "MetaPee new-study exclusion ledger",
        "=======================================",
        "PMID | Published date | Screen | Code | Exposure | Cancer | Reason",
    ]
    exclusion_lines.extend(
        f"{item['pmid']} | {item['published_date']} | {item['screen_layer']} | {item['exclusion_code']} | "
        f"{item['exposure']} | {item['cancer']} | {one_line(item['reason'])}"
        for item in exclusions
    )
    exclusion_path.write_text("\n".join(exclusion_lines) + "\n", encoding="utf-8")

    summary = {
        "new_studies_report": str(report_path),
        "candidate_pmids_report": str(candidate_path),
        "exclusion_report": str(exclusion_path),
        "eligible_contexts": len(included),
        "eligible_pmids": len(contexts_by_pmid),
        "exclusion_contexts": len(exclusions),
    }
    print(json.dumps(summary, indent=2))
    return summary


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def jats_to_text(xml_bytes: bytes) -> str:
    root = ET.fromstring(xml_bytes)
    blocks = []
    allowed = {"article-title", "title", "p", "th", "td", "caption"}
    for element in root.iter():
        if local_name(element.tag) not in allowed:
            continue
        value = one_line(" ".join(element.itertext()))
        if value and (not blocks or value != blocks[-1]):
            blocks.append(value)
    return "\n".join(blocks)


def fetch_pmc_full_text(pmcid: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(4):
        attempted_api_key = str(Entrez.api_key or "") or None
        try:
            wait_for_ncbi_request_slot()
            with Entrez.efetch(db="pmc", id=pmcid, retmode="xml") as handle:
                return handle.read()
        except Exception as exc:  # pragma: no cover - network retry path
            last_error = exc
            if reject_invalid_ncbi_api_key(exc, attempted_api_key):
                continue
            if not retryable_entrez_error(exc):
                break
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"PMC EFetch failed for {pmcid}: {last_error}")


# ChatGPT is the only active track while Cornell DeepSeek V4 access is pending.
# The dormant validation track remains represented in durable audit metadata;
# provider-specific calls and validation live in dual_screening.py.


def _runtime_stub(name: str) -> dual_screening.TrackRuntime:
    # Standalone resume/finalize invocations must resolve the same model
    # overrides as configure_runtime(), even when no API client is needed.
    load_dotenv(REPO_ROOT / "mykey.env", override=True)
    if name == "chatgpt":
        expected = {
            "CHATGPT_FIRST_SCREEN_MODEL": dual_screening.CHATGPT_FIRST_MODEL,
            "CHATGPT_SECOND_SCREEN_MODEL": dual_screening.CHATGPT_SECOND_MODEL,
            "CHATGPT_FULL_TEXT_MODEL": dual_screening.CHATGPT_FULL_TEXT_MODEL,
        }
        for env_name, approved_model in expected.items():
            configured = os.getenv(env_name)
            if configured and configured != approved_model:
                raise RuntimeError(
                    f"{env_name}={configured!r} violates the approved exact "
                    f"screening model {approved_model}"
                )
        return dual_screening.TrackRuntime(
            name=name,
            client=None,
            first_model=dual_screening.CHATGPT_FIRST_MODEL,
            second_model=dual_screening.CHATGPT_SECOND_MODEL,
            full_text_model=dual_screening.CHATGPT_FULL_TEXT_MODEL,
            api_style="responses",
        )
    return dual_screening.TrackRuntime(
        name=name,
        client=None,
        first_model=os.getenv(
            "DEEPSEEK_FIRST_SCREEN_MODEL", dual_screening.DEEPSEEK_FIRST_MODEL
        ),
        second_model=os.getenv(
            "DEEPSEEK_SECOND_SCREEN_MODEL", dual_screening.DEEPSEEK_SECOND_MODEL
        ),
        full_text_model=os.getenv(
            "DEEPSEEK_FULL_TEXT_MODEL", dual_screening.DEEPSEEK_FULL_TEXT_MODEL
        ),
        api_style="chat_completions",
    )


def _screening_runtimes(
    runtimes: dict[str, dual_screening.TrackRuntime] | None,
    calls_required: bool,
    track_names: tuple[str, ...] | None = None,
) -> dict[str, dual_screening.TrackRuntime]:
    requested = tuple(track_names or dual_screening.DEFAULT_ACTIVE_TRACK_NAMES)
    if runtimes is not None:
        runtime_names = tuple(
            name for name in dual_screening.TRACK_NAMES if name in runtimes
        )
        if (
            "chatgpt" not in runtime_names
            or set(runtime_names) != set(runtimes)
            or (track_names is not None and set(runtime_names) != set(requested))
        ):
            raise ValueError(
                "runtimes must include chatgpt and match the run's active tracks"
            )
        return {name: runtimes[name] for name in runtime_names}
    unknown = set(requested) - set(dual_screening.TRACK_NAMES)
    if unknown or "chatgpt" not in requested:
        raise ValueError("active tracks must include chatgpt and contain only known tracks")
    if calls_required:
        return {
            name: dual_screening.configure_runtime(name, REPO_ROOT)
            for name in requested
        }
    return {name: _runtime_stub(name) for name in requested}


def _screening_track_names(
    output_dir: Path,
    runtimes: dict[str, dual_screening.TrackRuntime] | None = None,
) -> tuple[str, ...]:
    """Resolve and pin active tracks so every resumable stage uses one mode."""
    if runtimes is not None:
        proposed = tuple(
            name for name in dual_screening.TRACK_NAMES if name in runtimes
        )
        if "chatgpt" not in proposed or set(proposed) != set(runtimes):
            raise ValueError("runtimes must include chatgpt and contain only known tracks")
    else:
        proposed = ()

    status = load_json(output_dir / "validation_status.json", {})
    persisted = tuple(status.get("active_tracks") or ())
    if persisted:
        if (
            "chatgpt" not in persisted
            or set(persisted) - set(dual_screening.TRACK_NAMES)
        ):
            raise RuntimeError("validation_status.json contains invalid active tracks")
        if proposed and set(proposed) != set(persisted):
            raise RuntimeError("Provided runtimes do not match this run's persisted mode")
        return tuple(name for name in dual_screening.TRACK_NAMES if name in persisted)
    if proposed:
        return proposed

    # A run without a persisted mode always starts in the safe current default;
    # stale DeepSeek artifacts must never opt a run into validation implicitly.
    return dual_screening.DEFAULT_ACTIVE_TRACK_NAMES


def first_screen(
    output_dir: Path,
    batch_size: int = 20,
    force: bool = False,
    *,
    runtimes: dict[str, dual_screening.TrackRuntime] | None = None,
) -> dict[str, Any]:
    require_output_not_superseded(output_dir)
    candidates = load_json(output_dir / "candidates.json", {})
    track_names = _screening_track_names(output_dir, runtimes)
    configured = _screening_runtimes(
        runtimes, bool(candidates.get("articles")), track_names
    )
    dual_screening.write_validation_status(output_dir, candidates, track_names)
    packets = {
        name: dual_screening.run_first_track(
            output_dir, candidates, configured[name], batch_size=batch_size, force=force
        )
        for name in track_names
    }
    canonical = packets["chatgpt"]
    save_json(output_dir / "first_screen.json", canonical)
    return canonical


def second_screen(
    output_dir: Path,
    batch_size: int = 8,
    force: bool = False,
    *,
    runtimes: dict[str, dual_screening.TrackRuntime] | None = None,
) -> dict[str, Any]:
    require_output_not_superseded(output_dir)
    candidates = load_json(output_dir / "candidates.json", {})
    track_names = _screening_track_names(output_dir, runtimes)
    first_packets = {
        name: load_json(output_dir / f"{name}_first_screen.json", {})
        for name in track_names
    }
    if any(not packet for packet in first_packets.values()):
        raise RuntimeError("Every active first-screen packet is required")
    calls_required = any(
        item.get("decision") == "PASS"
        for packet in first_packets.values()
        for item in packet.get("results", [])
    )
    configured = _screening_runtimes(runtimes, calls_required, track_names)
    packets = {
        name: dual_screening.run_second_track(
            output_dir,
            candidates,
            first_packets[name],
            configured[name],
            batch_size=batch_size,
            force=force,
        )
        for name in track_names
    }
    canonical = packets["chatgpt"]
    save_json(output_dir / "second_screen.json", canonical)
    return canonical


def full_text_review(
    output_dir: Path,
    force: bool = False,
    *,
    runtimes: dict[str, dual_screening.TrackRuntime] | None = None,
    fetcher: Any = None,
    parser: Any = None,
) -> dict[str, Any]:
    require_output_not_superseded(output_dir)
    candidates = load_json(output_dir / "candidates.json", {})
    track_names = _screening_track_names(output_dir, runtimes)
    second_packets = {
        name: load_json(output_dir / f"{name}_second_screen.json", {})
        for name in track_names
    }
    if any(not packet for packet in second_packets.values()):
        raise RuntimeError("Every active second-screen packet is required")

    expected_runtimes = _screening_runtimes(runtimes, False, track_names)
    packets: dict[str, dict[str, Any]] = {}
    if not force:
        for name in track_names:
            existing = load_json(output_dir / f"{name}_full_text_review.json", {})
            if not existing:
                continue
            try:
                dual_screening.validate_full_text_packet_lineage(
                    second_packets[name],
                    existing,
                    name,
                    expected_runtimes[name].full_text_model,
                )
            except dual_screening.ScreeningValidationError:
                continue
            packets[name] = existing

    missing_tracks = [
        name for name in track_names if name not in packets
    ]
    if not missing_tracks:
        canonical = dict(packets["chatgpt"])
        canonical["ncbi_requests"] = 0
        canonical["reused_tracks"] = list(track_names)
        save_json(output_dir / "full_text_review.json", canonical)
        return canonical

    pmc_required = any(
        dual_screening.full_text_gate(item)
        for name, packet in second_packets.items()
        if name in missing_tracks
        for item in packet.get("results", [])
    )
    if pmc_required and fetcher is None:
        try:
            configure_entrez(
                validate_api_key=(
                    candidates.get("run_mode") != "included_reclassification"
                )
            )
        except Exception as exc:
            configuration_error = f"NCBI configuration failed: {type(exc).__name__}: {exc}"

            def unavailable_fetcher(_pmcid: str) -> bytes:
                raise RuntimeError(configuration_error)

            fetcher = unavailable_fetcher
    sources, ncbi_requests = dual_screening.prepare_full_text_sources(
        output_dir,
        [second_packets[name] for name in missing_tracks],
        fetcher or fetch_pmc_full_text,
        parser or jats_to_text,
        force=force,
    )

    configured: dict[str, dual_screening.TrackRuntime] = dict(expected_runtimes)
    if runtimes is None:
        for name in missing_tracks:
            track_needs_model_call = any(
                dual_screening.full_text_gate(item)
                and sources.get(str(item.get("pmcid") or ""), {}).get("status")
                == "full_text"
                for item in second_packets[name].get("results", [])
            )
            if track_needs_model_call:
                configured[name] = dual_screening.configure_runtime(name, REPO_ROOT)

    for name in missing_tracks:
        packets[name] = dual_screening.run_full_text_track(
            output_dir,
            second_packets[name],
            sources,
            configured[name],
            force=force,
        )
    canonical = dict(packets["chatgpt"])
    canonical["ncbi_requests"] = ncbi_requests
    canonical["reused_tracks"] = [
        name for name in track_names if name not in missing_tracks
    ]
    save_json(output_dir / "full_text_review.json", canonical)
    # Raw PMC text is a transient model input. The durable packets retain only
    # PMCID, source status, and the short field-specific supporting excerpts.
    full_text_dir = output_dir / "full_text"
    if full_text_dir.exists():
        for text_path in full_text_dir.glob("*.txt"):
            text_path.unlink(missing_ok=True)
        try:
            full_text_dir.rmdir()
        except OSError:
            pass
    return canonical


def _write_dual_final_reports(
    output_dir: Path,
    report_dir: Path,
    canonical: dict[str, Any],
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "MetaPee monthly evidence update — canonical ChatGPT disposition",
        "================================================================",
        "Window: "
        + publication_window_text(
            canonical.get("publication_date_start"),
            canonical.get("publication_date_end"),
        ),
        "Only ChatGPT INCLUDE_META records may update saved web-platform caches.",
        "Moderate and Good JBI records are poolable when all key evidence is supported.",
        "Fair records require PMC full-text review and remain unpooled unless upgraded.",
        "Mendelian-randomization and genotype-interaction papers are exclusions.",
    ]
    for item in canonical.get("results", []):
        extraction = item.get("extraction") or {}
        jbi = item.get("jbi") or {}
        lines.extend(
            [
                "",
                f"PMID: {item.get('pmid')}",
                f"PMCID: {item.get('pmcid') or 'None found'}",
                f"Context: {item.get('exposure')} | {item.get('cancer')} | Incidence",
                f"Title: {one_line(item.get('title'))}",
                f"Final status: {item.get('final_status')}",
                f"Code: {item.get('exclusion_code') or 'None'}",
                f"Reason: {one_line(item.get('reason'))}",
                f"JBI: {jbi.get('grade') or 'Not applicable'} ({jbi.get('score_percent', 'NA')}%)",
                f"Exposure subtype: {extraction.get('exposure_subtype') or 'Not applicable'}",
                f"Ethnicity: {extraction.get('ethnicity') or 'Not applicable'}",
            ]
        )
    content = "\n".join(lines).rstrip() + "\n"
    (report_dir / "newStudies.txt").write_text(content, encoding="utf-8")
    (report_dir / "summary.txt").write_text(content, encoding="utf-8")

    exclusions = load_json(output_dir / "exclusions.json", {}).get("records", [])
    exclusion_lines = [
        "MetaPee canonical exclusion ledger",
        "==================================",
        "Every excluded context retains its PMID and one-sentence reason.",
        "",
    ]
    exclusion_lines.extend(
        f"{item['pmid']} | {item['screen_layer']} | {item['exclusion_code']} | "
        f"{item['exposure']} | {item['cancer']} | {item['reason']}"
        for item in exclusions
    )
    (report_dir / "newStudyExclusions.txt").write_text(
        "\n".join(exclusion_lines).rstrip() + "\n", encoding="utf-8"
    )


def annotate_hierarchy_memberships(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach parent memberships only after each direct context is screened."""
    hierarchy = exposure_hierarchy()
    records: list[dict[str, Any]] = []
    for item in results:
        direct = str(item.get("exposure") or "")
        item["direct_exposure"] = direct
        excluded = bool(item.get("exclusion_code")) or str(
            item.get("full_text_status") or item.get("final_status") or ""
        ).upper() == "EXCLUDE"
        ancestors = [] if excluded else hierarchy_ancestors(direct, hierarchy)
        item["inherited_exposure_memberships"] = ancestors
        item["hierarchy_applied_after_direct_screening"] = True
        if ancestors:
            records.append(
                {
                    "context_id": item.get("context_id"),
                    "pmid": str(item.get("pmid") or ""),
                    "direct_exposure": direct,
                    "inherited_exposures": ancestors,
                    "cancer": item.get("cancer"),
                    "outcome": "Incidence",
                    "pool_in_parent_meta_analysis": False,
                }
            )
    return records


def finalize_evidence(output_dir: Path, report_dir: Path) -> dict[str, Any]:
    require_output_not_superseded(output_dir)
    candidates = load_json(output_dir / "candidates.json", {})
    track_names = _screening_track_names(output_dir)
    final_packets = {}
    for name in track_names:
        first = load_json(output_dir / f"{name}_first_screen.json", {})
        second = load_json(output_dir / f"{name}_second_screen.json", {})
        full_text = load_json(output_dir / f"{name}_full_text_review.json", {})
        if not first or not second or not full_text:
            raise RuntimeError(f"Incomplete {name} screening packets")
        runtime = _runtime_stub(name)
        lineage = dual_screening.validate_track_lineage(
            candidates,
            first,
            second,
            full_text,
            name,
            {
                "first": runtime.first_model,
                "second": runtime.second_model,
                "full_text": runtime.full_text_model,
            },
        )
        final_packets[name] = dual_screening.finalize_track(
            candidates, first, second, full_text, name
        )
        final_packets[name].update(lineage)
        hierarchy_records = annotate_hierarchy_memberships(
            final_packets[name].get("results", [])
        )
        final_packets[name]["hierarchy_membership_records"] = hierarchy_records
        save_json(output_dir / f"{name}_final_screening.json", final_packets[name])

    canonical = dict(final_packets["chatgpt"])
    canonical["canonical_track"] = "chatgpt"
    canonical["validation_status"] = (
        "completed"
        if "deepseek" in final_packets
        else dual_screening.DEEPSEEK_VALIDATION_STATUS
    )
    save_json(output_dir / "final_screening.json", canonical)
    save_json(
        output_dir / "hierarchy_memberships.json",
        {
            "schema_version": 1,
            "created_at": datetime.now().astimezone().isoformat(),
            "rule": (
                "Hierarchy is applied after direct screening without duplicate "
                "parent model contexts"
            ),
            "records": canonical.get("hierarchy_membership_records", []),
        },
    )
    dual_screening.write_canonical_exclusions(output_dir, canonical)
    _write_dual_final_reports(output_dir, report_dir, canonical)
    dual_screening.write_discrepancies(
        output_dir, report_dir, canonical, final_packets.get("deepseek")
    )
    dual_screening.write_validation_status(
        output_dir,
        candidates,
        track_names,
        completed="deepseek" in final_packets,
    )
    return canonical


def json_compatible(value: Any) -> Any:
    """Convert pandas/numpy scalar values returned by meta_analysis to JSON."""
    if isinstance(value, dict):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_compatible(item) for item in value]
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def study_row(item: dict[str, Any]) -> dict[str, Any]:
    from scripts.display_eligibility import eligible_fair, display_row
    if eligible_fair(item):
        return dietary_policy.apply_row(display_row(item), item.get("exposure"))
    dual_screening.add_wald_derivations(item.get("extraction"))
    extraction = item.get("extraction") or {}
    evidence = extraction.get("evidence") or {}
    jbi = item.get("jbi") or {}
    authors = item.get("authors") or []
    first_author = authors[0] if authors else "Unknown"
    year_match = re.search(r"\b(19|20)\d{2}\b", str(item.get("published_date") or ""))
    effect = extraction.get("effect_size")
    lower = extraction.get("ci_lower")
    upper = extraction.get("ci_upper")
    original_effect_type = (
        extraction.get("reported_effect_type")
        if "reported_effect_type" in extraction
        else extraction.get("effect_type")
    )
    original_effect_size = (
        extraction.get("reported_effect_size")
        if "reported_effect_size" in extraction
        else effect
    )
    original_lower = (
        extraction.get("reported_ci_lower")
        if "reported_ci_lower" in extraction
        else lower
    )
    original_upper = (
        extraction.get("reported_ci_upper")
        if "reported_ci_upper" in extraction
        else upper
    )
    original_p_value = (
        extraction.get("reported_p_value")
        if "reported_p_value" in extraction
        else extraction.get("p_value")
    )
    was_inverted = bool(extraction.get("needs_inversion"))
    if was_inverted and effect is not None and lower is not None and upper is not None:
        effect, lower, upper = 1 / float(effect), 1 / float(upper), 1 / float(lower)
    se = (
        round(
            (math.log(float(upper)) - math.log(float(lower)))
            / (2.0 * dual_screening.WALD_95_Z),
            16,
        )
        if lower is not None and upper is not None and float(lower) > 0
        else None
    )
    p_value = extraction.get("p_value")
    p_value_support = str(extraction.get("p_value") or "")
    if extraction.get("p_value_method") == dual_screening.WALD_P_VALUE_METHOD:
        p_value_support = (
            "Computed from the effect and 95% CI using a two-sided Wald test "
            "on the log scale."
        )
    def excerpt(field: str) -> str:
        value = evidence.get(field) or {}
        return one_line(value.get("excerpt"))

    def evidence_url(field: str) -> str:
        value = evidence.get(field) or {}
        return one_line(value.get("url"))

    ci_support = excerpt("confidence_interval")
    if extraction.get("ci_method") == dual_screening.WALD_CONFIDENCE_INTERVAL_METHOD:
        ci_support = (
            "Computed from the reported ratio effect and two-sided p value using "
            "a 95% Wald interval on the log scale."
        )

    measurement_support = excerpt("exposure_measurement")
    raw_measurement = one_line(extraction.get("exposure_measurement_type")).lower()
    if any(term in raw_measurement for term in ("diet", "questionnaire", "self-report", "recall")):
        measurement_type = "dietary_intake"
    elif any(term in raw_measurement for term in ("biospecimen", "blood", "serum", "plasma", "urine")):
        measurement_type = "human_biospecimen"
    else:
        measurement_type = "unclear"
    total_n = extraction.get("total_n")
    cases = extraction.get("cases")
    estimated_cases = (
        int(round(float(total_n) * meta_analysis.baseline_risk_for_disease(item.get("cancer"))))
        if cases is None and total_n is not None
        else None
    )
    row = {
        "Study": f"{first_author} et al. ({year_match.group(0) if year_match else 'Unknown'}) [PMID: {item.get('pmid')}]",
        "PMID": str(item.get("pmid")),
        "PMCID": str(item.get("pmcid") or ""),
        "direct_exposure": item.get("direct_exposure") or item.get("exposure"),
        "inherited_exposure_memberships": item.get(
            "inherited_exposure_memberships"
        ) or [],
        "Displayed Exposure": item.get("exposure"),
        "Hierarchy Inherited": bool(item.get("hierarchy_inherited")),
        "pool_in_parent_meta_analysis": bool(
            item.get(
                "pool_in_parent_meta_analysis",
                not bool(item.get("hierarchy_inherited")),
            )
        ),
        "Screening Record ID": item.get("record_id") or item.get("context_id"),
        "Original Effect Type": original_effect_type,
        "Original Effect Type Reported Label": extraction.get(
            "reported_effect_type_label"
        ),
        "Original Effect Size": original_effect_size,
        "Original Lower CI": original_lower,
        "Original Upper CI": original_upper,
        "Original P Value": original_p_value,
        "direction_imputed": extraction.get("direction_imputed", False),
        "direction_imputation_reason": extraction.get("direction_imputation_reason"),
        "effect_size_imputed": extraction.get("effect_size_imputed", False),
        "effect_size_imputation_method": extraction.get("effect_size_imputation_method"),
        "ci_level_assumed": extraction.get("ci_level_assumed", False),
        "effect_is_adjusted": extraction.get("effect_is_adjusted", False),
        "reported_effect_type": extraction.get("reported_effect_type"),
        "poolability_policy_version": extraction.get("poolability_policy_version"),
        "Effect Size": effect,
        "Lower CI": lower,
        "Upper CI": upper,
        "P Value": p_value,
        "Population": extraction.get("population") or "Human study population described in the cited article",
        "Gender": extraction.get("gender") or "NA",
        "Smoking Status": extraction.get("smoking_status") or "NA",
        "Ethnicity": extraction.get("ethnicity") or "Mixed",
        "Ethnicity Basis": extraction.get("ethnicity_basis"),
        "Ethnicity Supporting Text": extraction.get("ethnicity_supporting_text"),
        "Reference": one_line(item.get("title")),
        "Authors": ", ".join(authors),
        "Journal": one_line(item.get("journal")),
        "Year": year_match.group(0) if year_match else "Unknown",
        "Link": item.get("pubmed_url") or f"https://pubmed.ncbi.nlm.nih.gov/{item.get('pmid')}/",
        "Effect Type": extraction.get("effect_type"),
        "SE": se,
        "Sample Size": total_n,
        "Cases": cases,
        "Estimated Cases": estimated_cases,
        "Design": extraction.get("design"),
        "Timing": extraction.get("timing"),
        "Continent": extraction.get("continent"),
        "Stage": None,
        "Quality %": jbi.get("score_percent"),
        "Quality Score": jbi.get("grade"),
        "comparison_type": extraction.get("comparison_type"),
        "effect_inverted": was_inverted,
        "exposure_subtype": extraction.get("exposure_subtype") or "mixed_or_unclear",
        "pooling_stratum": extraction.get("exposure_subtype") or "mixed_or_unclear",
        "JBI": jbi.get("answers") or {},
        "exposure_measurement_type": measurement_type,
        "exposure_measurement_supporting_text": measurement_support,
        "extraction_evidence": evidence,
        "evidence_excerpt_source": (
            evidence_url("effect_size")
            or evidence_url("confidence_interval")
            or evidence_url("sample_size")
            or item.get("pubmed_url")
        ),
        "extraction_supporting_text": {
            "sample_size": excerpt("sample_size"),
            "cases": excerpt("cases"),
            "effect_size": excerpt("effect_size"),
            "effect_direction": excerpt("comparison"),
            "p_value": excerpt("p_value") or p_value_support,
            "confidence_interval": ci_support,
            "outcome_definition": f"Incident {item.get('cancer')}",
            "exposure_definition": excerpt("comparison") or one_line(extraction.get("comparison_type")),
            "design": excerpt("design"),
            "timing": excerpt("timing"),
            "continent": excerpt("continent"),
            "ethnicity": excerpt("ethnicity"),
            "gender": excerpt("gender"),
            "smoking_status": excerpt("smoking_status"),
        },
    }

    for field in dietary_policy.AUDIT_FIELDS:
        if field in extraction:
            row[field] = extraction[field]
    return dietary_policy.apply_row(row, item.get("exposure"))


def _screening_record_id(study: dict[str, Any]) -> str:
    return str(
        study.get("Screening Record ID")
        or f"legacy-pmid:{study.get('PMID')}"
    )


def _same_reported_effect_for_reclassification_refresh(
    existing: dict[str, Any],
    current: dict[str, Any],
    exposure: str,
) -> bool:
    """Recognize one legacy/current rendering of the same reported estimate.

    Included-only screening can normalize ``All participants`` to ``Mixed`` and
    lightly rephrase a comparison.  Screening-record IDs therefore cannot be
    the only replay identity.  Exact PMID/exposure/type/effect/CI agreement plus
    compatible comparison wording is narrow enough to refresh that estimate
    without collapsing a distinct stratum with different quantitative values.
    """
    if str(existing.get("PMID") or "") != str(current.get("PMID") or ""):
        return False
    if _direct_exposure(existing, exposure).casefold() != _direct_exposure(
        current, exposure
    ).casefold():
        return False
    if str(existing.get("Effect Type") or "").upper() != str(
        current.get("Effect Type") or ""
    ).upper():
        return False
    if not all(
        _close_reported_number(existing.get(field), current.get(field))
        for field in ("Effect Size", "Lower CI", "Upper CI")
    ):
        return False
    first = _normalised_comparison(existing.get("comparison_type"))
    second = _normalised_comparison(current.get("comparison_type"))
    return bool(
        first
        and second
        and (
            first == second
            or f" {first} " in f" {second} "
            or f" {second} " in f" {first} "
        )
    )


def _merge_screened_cache_row(
    studies: list[dict[str, Any]],
    new_row: dict[str, Any],
    exposure: str,
    *,
    included_reclassification: bool,
) -> tuple[str, list[str]]:
    """Merge one screened row and return action plus superseded record IDs."""
    record_id = _screening_record_id(new_row)
    exact_index = next(
        (
            index
            for index, study in enumerate(studies)
            if _screening_record_id(study) == record_id
        ),
        None,
    )
    matching_indices = (
        [
            index
            for index, study in enumerate(studies)
            if _same_reported_effect_for_reclassification_refresh(
                study, new_row, exposure
            )
        ]
        if included_reclassification
        else []
    )
    superseded: list[str] = []
    if exact_index is not None:
        changed = studies[exact_index] != new_row
        studies[exact_index] = new_row
        duplicate_indices = [
            index for index in matching_indices if index != exact_index
        ]
        for index in sorted(duplicate_indices, reverse=True):
            superseded.append(_screening_record_id(studies[index]))
            studies.pop(index)
        return (
            "refreshed" if changed or duplicate_indices else "unchanged",
            sorted(set(superseded)),
        )

    if included_reclassification and len(matching_indices) > 1:
        raise RuntimeError(
            "Included-reclassification refresh found multiple legacy rows for "
            f"one exact reported effect ({exposure}/PMID {new_row.get('PMID')})"
        )
    if included_reclassification and matching_indices:
        index = matching_indices[0]
        superseded.append(_screening_record_id(studies[index]))
        studies[index] = new_row
        return "refreshed", superseded

    studies.append(new_row)
    return "appended", superseded


def apply_to_saved_results(output_dir: Path) -> dict[str, Any]:
    """Add only ChatGPT-canonical, poolable studies and rebuild diagnostics."""
    import pandas as pd

    final_packet = load_json(output_dir / "final_screening.json", {})
    if final_packet.get("canonical_track") != "chatgpt":
        raise RuntimeError("Saved-result mutation requires the canonical ChatGPT packet")
    candidates = load_json(output_dir / "candidates.json", {})
    articles_by_pmid = {
        str(article.get("pmid") or ""): article
        for article in candidates.get("articles", [])
    }
    protocol_guarded = []
    for item in final_packet.get("results", []):
        if item.get("full_text_status") == "INCLUDE_META":
            source = articles_by_pmid.get(str(item.get("pmid") or ""), item)
            protocol_block = dual_screening.protocol_exclusion(source)
            if protocol_block:
                code, reason = protocol_block
                protocol_guarded.append((str(item.get("pmid") or "unknown"), code, reason))
    if protocol_guarded:
        detail = "; ".join(
            f"PMID {pmid} ({code}: {reason})"
            for pmid, code, reason in protocol_guarded
        )
        raise RuntimeError(
            "Canonical screening invariant failed; rerun --stage finalize before apply. "
            + detail
        )

    stale_subtypes = [
        str(item.get("pmid") or "unknown")
        for item in final_packet.get("results", [])
        if item.get("full_text_status") == "INCLUDE_META"
        and dual_screening.apply_whole_food_subtype_disposition(item).get("final_status")
        != item.get("final_status")
    ]
    if stale_subtypes:
        raise RuntimeError(
            "Canonical subtype invariant failed; rerun --stage finalize before apply. "
            f"Affected PMIDs: {', '.join(stale_subtypes)}"
        )
    from scripts.display_eligibility import eligible_fair
    included_contexts = [
        item for item in final_packet.get("results", [])
        if (item.get("full_text_status") == "INCLUDE_META" or eligible_fair(item))
        and item.get("track") == "chatgpt"
        and not meta_analysis.is_curated_meta_analysis_exclusion(
            item.get("pmid"), item.get("cancer"), item.get("exposure"), "Incidence"
        )
    ]
    included: list[dict[str, Any]] = []
    subtype_review: list[dict[str, Any]] = [
        item
        for item in final_packet.get("results", [])
        if item.get("final_status") == "ELIGIBLE_SUBTYPE_REVIEW"
    ]
    for item in included_contexts:
        if eligible_fair(item):
            included.append(item)
            continue
        for record in dual_screening.split_study_records(item):
            if not dual_screening.is_poolable(record.get("extraction"), record.get("jbi")):
                continue
            disposition = dual_screening.apply_whole_food_subtype_disposition(record)
            if disposition.get("final_status") == "ELIGIBLE_SUBTYPE_REVIEW":
                subtype_review.append(disposition)
            else:
                included.append(disposition)
    screening_models = final_packet.get("screening_models")
    screening_input_digests = final_packet.get("screening_input_digests")
    expected_stages = {"first", "second", "full_text"}
    if included and (
        not isinstance(screening_models, dict)
        or set(screening_models) != expected_stages
        or not all(isinstance(value, str) and value for value in screening_models.values())
        or not isinstance(screening_input_digests, dict)
        or set(screening_input_digests) != expected_stages
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in screening_input_digests.values()
        )
        or final_packet.get("protocol_version")
        != dual_screening.SCREENING_PROTOCOL_VERSION
    ):
        raise RuntimeError(
            "Canonical screening lineage metadata is missing or stale; rerun --stage finalize before apply."
        )
    save_json(
        output_dir / "exposure_subtype_review.json",
        {
            "created_at": datetime.now().astimezone().isoformat(),
            "reason": (
                "Related Tomato and Pepper (Capsicum) evidence was retained but not automatically pooled "
                "across food, dietary-component, biomarker, and supplement subtypes."
            ),
            "records": subtype_review,
        },
    )
    changes = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in included:
        direct = str(item.get("direct_exposure") or item.get("exposure") or "")
        targets = [
            direct,
            *[
                str(value)
                for value in item.get("inherited_exposure_memberships") or []
            ],
        ]
        seen_targets: set[str] = set()
        for target in targets:
            target_key = target.casefold()
            if not target or target_key in seen_targets:
                continue
            seen_targets.add(target_key)
            grouped[(target, item["cancer"])].append(
                {
                    **item,
                    "exposure": target,
                    "direct_exposure": direct,
                    "hierarchy_inherited": target_key != direct.casefold(),
                    "pool_in_parent_meta_analysis": (
                        target_key == direct.casefold()
                    ),
                }
            )

    for (exposure, cancer), items in grouped.items():
        safe_exposure = re.sub(r"[^a-zA-Z0-9]+", "_", exposure.lower()).strip("_")
        safe_cancer = re.sub(r"[^a-zA-Z0-9]+", "_", cancer.lower()).strip("_")
        cache_paths = [
            CACHE_DIR / safe_exposure / f"{safe_cancer}_incidence_true_{suffix}.json"
            for suffix in ("all", "core")
        ]
        existing_path = next((path for path in cache_paths if path.exists()), None)
        # A clean MetaPee repository intentionally starts with no inherited
        # caches. The first accepted urinary-cancer record must therefore bootstrap
        # both canonical cache variants instead of requiring a legacy seed.
        source_cache = load_json(existing_path, {}) if existing_path else {}
        studies = list(source_cache.get("studies", []))
        window_start = final_packet.get("publication_date_start")
        window_end = final_packet.get("publication_date_end")
        recorded_added = {
            str(pmid)
            for update in source_cache.get("monthly_updates", [])
            if update.get("publication_date_start") == window_start
            and update.get("publication_date_end") == window_end
            for pmid in update.get("added_pmids", [])
        }
        appended = []
        appended_records = []
        refreshed = []
        refreshed_records = []
        superseded_record_ids = []
        for item in items:
            pmid = str(item.get("pmid"))
            new_row = study_row(item)
            # Cache rows returned by meta_analysis contain deterministic RR
            # conversion columns.  Add the same fields before replay comparison
            # so an unchanged source record is not spuriously refreshed forever.
            try:
                normalized_frame = meta_analysis.add_effect_measure_conversion_columns(
                    pd.DataFrame([new_row]),
                    cancer,
                )
                new_row = json_compatible(normalized_frame.iloc[0].to_dict())
            except ValueError:
                # Supports replaying a legacy out-of-registry packet without
                # weakening the registry validation in a new urinary-cancer run.
                new_row = json_compatible(new_row)
            record_id = _screening_record_id(new_row)
            action, superseded = _merge_screened_cache_row(
                studies,
                new_row,
                exposure,
                included_reclassification=(
                    candidates.get("run_mode") == "included_reclassification"
                ),
            )
            superseded_record_ids.extend(superseded)
            if action == "appended":
                appended.append(pmid)
                appended_records.append(record_id)
            elif action == "refreshed":
                refreshed.append(pmid)
                refreshed_records.append(record_id)
        if not appended and not refreshed:
            # Consolidate duplicate audit entries from an interrupted/replayed
            # run without recomputing an unchanged meta-analysis.
            for path in cache_paths:
                cache = load_json(path, dict(source_cache))
                cache["exposure"] = exposure
                cache["cancer"] = cancer
                cache["outcome"] = "Incidence"
                cache["canonical_track"] = "chatgpt"
                cache["screening_models"] = dict(screening_models)
                cache["screening_input_digests"] = dict(screening_input_digests)
                cache["screening_protocol_version"] = final_packet["protocol_version"]
                other_updates = [
                    update for update in cache.get("monthly_updates", [])
                    if not (
                        update.get("publication_date_start") == window_start
                        and update.get("publication_date_end") == window_end
                    )
                ]
                if recorded_added:
                    other_updates.append(
                        {
                            "publication_date_start": window_start,
                            "publication_date_end": window_end,
                            "added_pmids": sorted(recorded_added),
                        }
                    )
                cache["monthly_updates"] = other_updates
                save_json(path, json_compatible(cache), indent=4, ensure_ascii=True)
            changes.append(
                {
                    "exposure": exposure,
                    "cancer": cancer,
                    "status": "previously_applied" if recorded_added else "already_present",
                    "added_pmids": sorted(recorded_added),
                }
            )
            continue

        frame = pd.DataFrame(studies)
        for column in (
            "Effect Size",
            "Lower CI",
            "Upper CI",
            "Original Effect Size",
            "Original Lower CI",
            "Original Upper CI",
            "Original P Value",
            "SE",
            "Cases",
            "Sample Size",
            "Estimated Cases",
        ):
            if column in frame:
                frame[column] = pd.to_numeric(
                    frame[column].astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce"
                )
        screening_stats = source_cache.get("screening_stats") or {
            "total_fetched": len(final_packet.get("results", [])),
            "after_prefilter": len(final_packet.get("results", [])),
            "llm_screened_in": sum(
                item.get("final_status")
                in {
                    "INCLUDE_META",
                    "ELIGIBLE_NOT_POOLABLE",
                    "ELIGIBLE_SUBTYPE_REVIEW",
                }
                for item in final_packet.get("results", [])
            ),
            "llm_screened_out": sum(
                item.get("final_status") == "EXCLUDE"
                for item in final_packet.get("results", [])
            ),
            "extracted": len(studies),
            "canonical_track": "chatgpt",
        }
        parent_pooling = (
            frame["pool_in_parent_meta_analysis"].map(
                meta_analysis.is_parent_pooling_eligible
            )
            if "pool_in_parent_meta_analysis" in frame
            else pd.Series(True, index=frame.index)
        )
        if parent_pooling.any():
            result = meta_analysis.perform_meta_analysis(
                frame,
                cancer,
                exposure,
                outcome="Incidence",
                exclude_meta=True,
                df_all=frame,
                screening_stats=screening_stats,
            )
        else:
            result = {
                "success": True,
                "studies": json_compatible(studies),
                "summary_html": (
                    "<p>Screened child-exposure evidence is listed for this "
                    "parent category but is not pooled in the parent "
                    "meta-analysis.</p>"
                ),
                "headline": None,
                "screening_stats": screening_stats,
                "plot_url": None,
                "funnel_plot_url": None,
                "baujat_plot_url": None,
            }
        if not result.get("success"):
            raise RuntimeError(f"Meta-analysis rebuild failed for {exposure}/{cancer}: {result}")
        analyzed_studies = result.get("studies")
        if not isinstance(analyzed_studies, list):
            analyzed_studies = studies
        for path in cache_paths:
            cache = load_json(path, dict(source_cache))
            for key in (
                "studies", "summary_html", "headline", "screening_stats",
                "plot_url", "funnel_plot_url", "baujat_plot_url",
            ):
                cache[key] = analyzed_studies if key == "studies" else result.get(key)
            cache["exposure"] = exposure
            cache["cancer"] = cancer
            cache["outcome"] = "Incidence"
            cache["canonical_track"] = "chatgpt"
            cache["screening_models"] = dict(screening_models)
            cache["screening_input_digests"] = dict(screening_input_digests)
            cache["screening_protocol_version"] = final_packet["protocol_version"]
            cache["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            all_added = sorted(recorded_added | set(appended))
            updates = [
                update for update in cache.get("monthly_updates", [])
                if not (
                    update.get("publication_date_start") == window_start
                    and update.get("publication_date_end") == window_end
                )
            ]
            updates.append(
                {
                    "publication_date_start": window_start,
                    "publication_date_end": window_end,
                    "added_pmids": all_added,
                }
            )
            cache["monthly_updates"] = updates
            save_json(path, json_compatible(cache), indent=4, ensure_ascii=True)
        changes.append(
            {
                "exposure": exposure,
                "cancer": cancer,
                "status": "updated",
                "added_pmids": sorted(recorded_added | set(appended)),
                "added_record_ids": sorted(set(appended_records)),
                "refreshed_pmids": sorted(set(refreshed)),
                "refreshed_record_ids": sorted(set(refreshed_records)),
                "superseded_record_ids": sorted(set(superseded_record_ids)),
                "studies_after": len(analyzed_studies),
                "headline": result.get("headline"),
            }
        )
    packet = {"created_at": datetime.now().astimezone().isoformat(), "changes": changes}
    save_json(output_dir / "applied_changes.json", json_compatible(packet))
    print(json.dumps(packet, indent=2, default=str))
    return packet


def _canonical_cache_paths(exposure: str, cancer: str) -> list[Path]:
    safe_exposure = re.sub(r"[^a-zA-Z0-9]+", "_", exposure.lower()).strip("_")
    safe_cancer = re.sub(r"[^a-zA-Z0-9]+", "_", cancer.lower()).strip("_")
    return [
        CACHE_DIR / safe_exposure / f"{safe_cancer}_incidence_true_{suffix}.json"
        for suffix in ("all", "core")
    ]


def _replace_unsearched_parent_rows(
    path: Path,
    exposure: str,
    cancer: str,
    retained: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    final_packet: dict[str, Any],
) -> None:
    """Preserve parent-direct rows while replacing revised child projections."""
    import pandas as pd

    cached = load_json(path, {}) if path.exists() else {}
    if retained:
        frame = pd.DataFrame(retained)
        for column in (
            "Effect Size", "Lower CI", "Upper CI", "Original Effect Size",
            "Original Lower CI", "Original Upper CI", "Original P Value",
            "SE", "Cases", "Sample Size", "Estimated Cases",
        ):
            if column in frame:
                frame[column] = pd.to_numeric(
                    frame[column].astype(str).str.replace(",", "", regex=False).str.strip(),
                    errors="coerce",
                )
        result = meta_analysis.perform_meta_analysis(
            frame,
            cancer,
            exposure,
            outcome="Incidence",
            exclude_meta=True,
            df_all=frame,
            screening_stats=cached.get("screening_stats"),
        )
        if not result.get("success"):
            raise RuntimeError(
                f"Unsearched parent rebuild failed for {exposure}/{cancer}: {result}"
            )
        studies = result.get("studies")
        if not isinstance(studies, list):
            studies = retained
    else:
        studies = []
        result = {
            "summary_html": (
                "<p>No studies remain after revised child memberships were rebuilt.</p>"
            ),
            "headline": None,
            "screening_stats": cached.get("screening_stats") or {},
            "plot_url": None,
            "funnel_plot_url": None,
            "baujat_plot_url": None,
        }
    cached.update(
        {
            "studies": studies,
            "exposure": exposure,
            "cancer": cancer,
            "outcome": "Incidence",
            "canonical_track": "chatgpt",
            "screening_models": dict(final_packet["screening_models"]),
            "screening_input_digests": dict(
                final_packet["screening_input_digests"]
            ),
            "screening_protocol_version": final_packet["protocol_version"],
            "authoritative_child_rollup_revision": {
                "mode": "preserve_unsearched_parent_direct_rows",
                "removed_inherited_rows": removed,
            },
        }
    )
    for key in (
        "summary_html", "headline", "screening_stats", "plot_url",
        "funnel_plot_url", "baujat_plot_url",
    ):
        cached[key] = result.get(key)
    save_json(path, json_compatible(cached), indent=4, ensure_ascii=True)


def _display_repo_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _normalised_comparison(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _direct_exposure(study: dict[str, Any], fallback: str) -> str:
    explicit = study.get("direct_exposure") or study.get("Direct Exposure")
    if explicit:
        return str(explicit).strip()
    # Older meta-analysis serialization omitted the explicit hierarchy fields,
    # but the exact direct context remains embedded in the immutable screening
    # record ID.  Recover it only from the validated `PMID|exposure|cancer`
    # prefix so a replay can replace prior projections without duplication.
    record_id = str(study.get("Screening Record ID") or "")
    context_prefix = record_id.split("#", 1)[0]
    parts = context_prefix.split("|", 2)
    if len(parts) == 3 and parts[1].strip():
        return parts[1].strip()
    return str(fallback).strip()


def _source_record_id(
    source: str,
    cancer: str,
    study: dict[str, Any],
) -> str:
    payload = {
        "source": source,
        "cancer": cancer,
        "pmid": str(study.get("PMID") or ""),
        "direct_exposure": _direct_exposure(study, source),
        "effect_type": str(study.get("Effect Type") or "").upper(),
        "effect_size": study.get("Effect Size"),
        "ci_lower": study.get("Lower CI"),
        "ci_upper": study.get("Upper CI"),
        "comparison": _normalised_comparison(
            study.get("comparison_type")
            or (study.get("extraction_supporting_text") or {}).get(
                "exposure_definition"
            )
        ),
    }
    material = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def included_reclassification_records() -> list[dict[str, Any]]:
    """Use legacy included caches only to define the immutable PMID universe."""
    records: dict[str, dict[str, Any]] = {}
    for source in reclassification_targets():
        for cancer in CANCER_QUERIES:
            cache_path = next(
                (
                    path
                    for path in _canonical_cache_paths(source, cancer)
                    if path.exists()
                ),
                None,
            )
            if cache_path is None:
                continue
            cached = load_json(cache_path, {})
            if not isinstance(cached, dict):
                raise ValueError(f"Included cache {cache_path} must be an object")
            legacy_lineage = {
                "screening_protocol_version": cached.get(
                    "screening_protocol_version"
                ),
                "screening_models": cached.get("screening_models"),
                "canonical_track": cached.get("canonical_track"),
            }
            for study in cached.get("studies", []):
                if not isinstance(study, dict):
                    continue
                pmid = str(study.get("PMID") or "").strip()
                if not pmid or pmid.casefold() in {"none", "nan"}:
                    continue
                if _direct_exposure(study, source).casefold() != source.casefold():
                    continue
                record_id = _source_record_id(source, cancer, study)
                records[record_id] = {
                    "source_record_id": record_id,
                    "source_exposure": source,
                    "cancer": cancer,
                    "outcome": "Incidence",
                    "pmid": pmid,
                    "source_study": study,
                    "legacy_source_lineage": legacy_lineage,
                }
    return sorted(
        records.values(),
        key=lambda item: (
            item["source_exposure"].casefold(),
            item["cancer"].casefold(),
            not item["pmid"].isdigit(),
            int(item["pmid"]) if item["pmid"].isdigit() else item["pmid"],
            item["source_record_id"],
        ),
    )


def _reclassification_context_metadata(
    exposure: str,
    source_exposures: set[str],
    targets: dict[str, list[str]],
) -> dict[str, Any]:
    """Describe binding source/target roles for the screening prompts."""
    target_for = sorted(
        source
        for source in source_exposures
        if exposure in targets.get(source, [])
    )
    roles = []
    if exposure in source_exposures:
        roles.append("source")
    if target_for:
        roles.append("approved_target")
    wording = {
        "Fish oil": (
            "Include this direct Fish oil context only when the article explicitly "
            "uses fish-oil wording for the exposure; DHA/EPA-only wording is not Fish oil."
        ),
        "Omega-3 fatty acids": (
            "Include this direct Omega-3 context only with explicit omega-3/n-3 family "
            "wording; DHA-, EPA-, or other atomic-only wording belongs to the atomic "
            "context and reaches Omega-3 only through post-screen hierarchy roll-up."
        ),
        "Tomato": (
            "Include this direct Tomato context only with explicit tomato wording; "
            "lycopene-only wording is not Tomato."
        ),
        "Lycopene": "Assess explicit lycopene wording as Lycopene, not Tomato.",
        "Eicosapentaenoic acid": (
            "Explicit EPA/eicosapentaenoic-acid wording may support this atomic target "
            "without fish-oil or omega-3 family wording."
        ),
        "Docosahexaenoic acid": (
            "Explicit DHA/docosahexaenoic-acid wording may support this atomic target "
            "without fish-oil or omega-3 family wording."
        ),
    }.get(
        exposure,
        "Require article wording that directly supports this requested atomic exposure.",
    )
    return {
        "reclassification_roles": roles,
        "approved_target_for_sources": target_for,
        "classification_instruction": wording,
    }


def discover_reclassification(
    output_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    """EFetch and screen only included source PMIDs; never run PubMed ESearch."""
    source_records = included_reclassification_records()
    targets = reclassification_targets()
    source_digest = hashlib.sha256(
        json.dumps(
            {"records": source_records, "targets": targets},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    candidate_path = output_dir / "candidates.json"
    existing = load_json(candidate_path, {})
    if (
        not force
        and existing.get("run_mode") == "included_reclassification"
        and existing.get("source_records_digest") == source_digest
        and isinstance(existing.get("articles"), list)
    ):
        return existing

    contexts_by_pmid: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for record in source_records:
        source = record["source_exposure"]
        cancer = record["cancer"]
        contexts_by_pmid[record["pmid"]].add((source, cancer))
        for target in targets[source]:
            contexts_by_pmid[record["pmid"]].add((target, cancer))
    pmids = sorted(
        contexts_by_pmid,
        key=lambda value: (
            not value.isdigit(),
            int(value) if value.isdigit() else value,
        ),
    )
    started = time.time()
    if pmids:
        api_key_status = configure_entrez(validate_api_key=False)
        articles, fetch_requests = entrez_fetch(pmids)
        missing = sorted(set(pmids).difference(articles))
        if missing:
            raise RuntimeError(
                "NCBI EFetch omitted included PMIDs; reclassification stopped: "
                + ", ".join(missing)
            )
    else:
        api_key_status, articles, fetch_requests = "not_required", {}, 0

    source_exposures_by_scope: dict[tuple[str, str], set[str]] = defaultdict(set)
    for source_record in source_records:
        source_exposures_by_scope[
            (source_record["pmid"], source_record["cancer"])
        ].add(source_record["source_exposure"])

    candidate_records = []
    sources = exposure_catalog_sources("all")
    source_by_norm = {name.casefold(): value for name, value in sources.items()}
    for pmid in pmids:
        record = dict(articles[pmid])
        record["published_date"] = (
            record.get("journal_publication_date")
            or record.get("electronic_publication_date")
            or record.get("published_date")
            or "Unknown"
        )
        record["candidate_contexts"] = [
            {
                "exposure": exposure,
                "cancer": cancer,
                "outcome": "Incidence",
                "catalog_source": source_by_norm.get(
                    exposure.casefold(), "current"
                ),
                **_reclassification_context_metadata(
                    exposure,
                    source_exposures_by_scope[(pmid, cancer)],
                    targets,
                ),
            }
            for exposure, cancer in sorted(
                contexts_by_pmid[pmid],
                key=lambda item: (item[0].casefold(), item[1].casefold()),
            )
        ]
        candidate_records.append(record)
    revision_date = str(revision_manifest().get("revision_date") or date.today())
    listed = sorted(
        {
            context["exposure"]
            for article in candidate_records
            for context in article["candidate_contexts"]
        },
        key=str.casefold,
    )
    packet = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "run_mode": "included_reclassification",
        "publication_date_start": None,
        "publication_date_end": revision_date,
        "selected_exposures": listed,
        "exposure_scope": "all",
        "revision_aware": True,
        "source_records_digest": source_digest,
        "listed_exposures": len(listed),
        "cancer_types": list(CANCER_QUERIES),
        "saved_contexts_omitted": 0,
        "redundant_parent_contexts_omitted": 0,
        "redundant_parent_context_omissions": [],
        "ncbi_api_key_status": api_key_status,
        "ncbi_requests": {
            "esearch": 0,
            "efetch": fetch_requests,
            "total": fetch_requests,
        },
        "candidate_pmids": len(candidate_records),
        "candidate_contexts": sum(
            len(item["candidate_contexts"]) for item in candidate_records
        ),
        "included_source_effects": len(source_records),
        "elapsed_seconds": round(time.time() - started, 1),
        "reclassification_targets": targets,
        "reclassification_source_records": source_records,
        "articles": candidate_records,
    }
    save_json(candidate_path, packet)
    return packet


def _finite_float(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _close_reported_number(left: Any, right: Any) -> bool:
    first, second = _finite_float(left), _finite_float(right)
    return (
        first is not None
        and second is not None
        and math.isclose(first, second, rel_tol=1e-6, abs_tol=1e-8)
    )


def _comparison_agrees(left: Any, right: Any) -> bool:
    first, second = _normalised_comparison(left), _normalised_comparison(right)
    if not first or not second:
        return False
    if first in second or second in first:
        return True
    first_tokens, second_tokens = set(first.split()), set(second.split())
    return (
        len(first_tokens & second_tokens)
        / max(1, len(first_tokens | second_tokens))
        >= 0.5
    )


def _same_reported_effect(
    source_study: dict[str, Any],
    target_row: dict[str, Any],
) -> bool:
    source_type = str(source_study.get("Effect Type") or "").strip().upper()
    target_type = str(target_row.get("Effect Type") or "").strip().upper()
    if not source_type or source_type != target_type:
        return False
    if not all(
        _close_reported_number(source_study.get(field), target_row.get(field))
        for field in ("Effect Size", "Lower CI", "Upper CI")
    ):
        return False
    source_comparison = source_study.get("comparison_type") or (
        source_study.get("extraction_supporting_text") or {}
    ).get("exposure_definition")
    target_comparison = target_row.get("comparison_type") or (
        target_row.get("extraction_supporting_text") or {}
    ).get("exposure_definition")
    return _comparison_agrees(source_comparison, target_comparison)


def _target_effect_is_persisted(
    target: str,
    cancer: str,
    target_item: dict[str, Any],
) -> bool:
    expected = study_row(target_item)
    pmid = str(expected.get("PMID") or "")
    for path in _canonical_cache_paths(target, cancer):
        if not path.exists():
            continue
        for study in load_json(path, {}).get("studies", []):
            if (
                str(study.get("PMID") or "") == pmid
                and _direct_exposure(study, target).casefold()
                == target.casefold()
                and _same_reported_effect(expected, study)
            ):
                return True
    return False


def _rebuild_reclassified_source_cache(
    path: Path,
    source: str,
    cancer: str,
    retained: list[dict[str, Any]],
    removed_record_ids: list[str],
    final_packet: dict[str, Any],
) -> None:
    import pandas as pd

    cache = load_json(path, {})
    cache["studies"] = retained
    cache["exposure"] = source
    cache["cancer"] = cancer
    cache["outcome"] = "Incidence"
    cache["canonical_track"] = "chatgpt"
    cache["screening_models"] = dict(final_packet["screening_models"])
    cache["screening_input_digests"] = dict(
        final_packet["screening_input_digests"]
    )
    cache["screening_protocol_version"] = final_packet["protocol_version"]
    cache.setdefault("reclassification_updates", []).append(
        {
            "revision_date": revision_manifest().get("revision_date"),
            "removed_source_record_ids": sorted(removed_record_ids),
            "rule": "target_persisted_before_source_removal",
        }
    )
    if retained:
        frame = pd.DataFrame(retained)
        for column in (
            "Effect Size", "Lower CI", "Upper CI", "SE", "Cases",
            "Sample Size", "Estimated Cases",
        ):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        result = meta_analysis.perform_meta_analysis(
            frame,
            cancer,
            source,
            outcome="Incidence",
            exclude_meta=True,
            df_all=frame,
            screening_stats=cache.get("screening_stats"),
        )
        if not result.get("success"):
            raise RuntimeError(
                f"Source-cache rebuild failed for {source}/{cancer}: {result}"
            )
        for key in (
            "summary_html", "headline", "screening_stats", "plot_url",
            "funnel_plot_url", "baujat_plot_url",
        ):
            cache[key] = result.get(key)
    else:
        cache.update(
            {
                "summary_html": (
                    "<p>No directly included studies remain after audited "
                    "exposure reclassification.</p>"
                ),
                "headline": None,
                "plot_url": None,
                "funnel_plot_url": None,
                "baujat_plot_url": None,
            }
        )
    save_json(path, json_compatible(cache), indent=4, ensure_ascii=True)


def _validate_reclassification_final_lineage(final_packet: dict[str, Any]) -> None:
    expected_models = {
        "first": dual_screening.CHATGPT_FIRST_MODEL,
        "second": dual_screening.CHATGPT_SECOND_MODEL,
        "full_text": dual_screening.CHATGPT_FULL_TEXT_MODEL,
    }
    digests = final_packet.get("screening_input_digests")
    if (
        final_packet.get("canonical_track") != "chatgpt"
        or final_packet.get("protocol_version")
        != dual_screening.SCREENING_PROTOCOL_VERSION
        or final_packet.get("screening_models") != expected_models
        or not isinstance(digests, dict)
        or set(digests) != {"first", "second", "full_text"}
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in digests.values()
        )
    ):
        raise RuntimeError(
            "Reclassification final packet has stale model/protocol lineage"
        )


def apply_reclassification_results(output_dir: Path) -> dict[str, Any]:
    """Persist accepted targets first; remove only the verified same effect."""
    candidates = load_json(output_dir / "candidates.json", {})
    if candidates.get("run_mode") != "included_reclassification":
        raise RuntimeError(
            "Reclassification apply requires an included-only candidate packet"
        )
    final_packet = load_json(output_dir / "final_screening.json", {})
    _validate_reclassification_final_lineage(final_packet)

    target_apply = apply_to_saved_results(output_dir)
    result_by_context = {
        str(item.get("context_id") or ""): item
        for item in final_packet.get("results", [])
    }
    removal_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    audit_records = []
    for record in candidates.get("reclassification_source_records") or []:
        source = str(record["source_exposure"])
        cancer = str(record["cancer"])
        pmid = str(record["pmid"])
        source_result = result_by_context.get(context_id(pmid, source, cancer))
        base = {
            "source_record_id": record["source_record_id"],
            "pmid": pmid,
            "source_exposure": source,
            "cancer": cancer,
            "source_final_status": (
                source_result.get("final_status") if source_result else "MISSING"
            ),
            "source_screen_reason": (
                source_result.get("reason")
                if source_result
                else "The source screening context is missing."
            ),
        }
        if source_result and source_result.get("full_text_status") == "INCLUDE_META":
            audit_records.append(
                {
                    **base,
                    "action": "retained_source_reaffirmed",
                    "accepted_targets": [],
                    "reason": (
                        "Fresh Luna-Sol-Luna screening reaffirmed explicit source "
                        "wording."
                    ),
                }
            )
            continue
        matched = []
        for target in candidates["reclassification_targets"].get(source, []):
            target_result = result_by_context.get(
                context_id(pmid, target, cancer)
            )
            if (
                target_result
                and target_result.get("full_text_status") == "INCLUDE_META"
                and _same_reported_effect(
                    record["source_study"], study_row(target_result)
                )
            ):
                matched.append((target, target_result))
        if not matched:
            audit_records.append(
                {
                    **base,
                    "action": "retained_for_adjudication",
                    "accepted_targets": [],
                    "reason": (
                        "No accepted target matched the same PMID, effect, CI, "
                        "effect type, and comparison."
                    ),
                }
            )
            continue
        persisted = [
            target
            for target, target_result in matched
            if _target_effect_is_persisted(target, cancer, target_result)
        ]
        if not persisted:
            audit_records.append(
                {
                    **base,
                    "action": "retained_for_adjudication",
                    "accepted_targets": [target for target, _ in matched],
                    "reason": (
                        "Matching target screening passed, but the same target "
                        "effect was not verified in cache; source retained."
                    ),
                }
            )
            continue
        removal_ids[(source, cancer)].add(record["source_record_id"])
        audit_records.append(
            {
                **base,
                "action": "moved_to_verified_targets",
                "accepted_targets": persisted,
                "reason": (
                    "Same PMID/effect was accepted and verified in target before "
                    "source removal."
                ),
            }
        )

    originals: dict[Path, dict[str, Any]] = {}
    planned = []
    for (source, cancer), identifiers in sorted(removal_ids.items()):
        for path in _canonical_cache_paths(source, cancer):
            if not path.exists():
                continue
            cached = load_json(path, {})
            retained, removed = [], []
            for study in cached.get("studies", []):
                record_id = _source_record_id(source, cancer, study)
                if (
                    _direct_exposure(study, source).casefold()
                    == source.casefold()
                    and record_id in identifiers
                ):
                    removed.append(record_id)
                else:
                    retained.append(study)
            if removed:
                originals[path] = cached
                planned.append((path, source, cancer, retained, removed))
    cache_changes = []
    try:
        for path, source, cancer, retained, removed in planned:
            _rebuild_reclassified_source_cache(
                path, source, cancer, retained, removed, final_packet
            )
            cache_changes.append(
                {
                    "cache": _display_repo_path(path),
                    "removed_source_record_ids": sorted(removed),
                    "studies_after": len(retained),
                }
            )
    except Exception:
        for path, original in originals.items():
            save_json(path, original, indent=4, ensure_ascii=True)
        raise

    packet = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "run_mode": "included_reclassification",
        "transaction_rule": "target_persisted_before_source_removal",
        "target_apply": target_apply,
        "records": audit_records,
        "cache_changes": cache_changes,
        "moved_source_effects": sum(
            item["action"] == "moved_to_verified_targets"
            for item in audit_records
        ),
        "retained_source_effects": sum(
            item["action"] != "moved_to_verified_targets"
            for item in audit_records
        ),
    }
    save_json(
        output_dir / "reclassification_applied_changes.json",
        json_compatible(packet),
    )
    return packet


def apply_revision_authoritative_results(
    output_dir: Path,
) -> dict[str, Any]:
    """Replace every searched direct cache, including zero-inclusion scopes."""
    candidates = load_json(output_dir / "candidates.json", {})
    final_packet = load_json(output_dir / "final_screening.json", {})
    if not candidates.get("revision_aware"):
        raise RuntimeError(
            "Authoritative revision apply requires a revision-aware packet"
        )
    required_models = {
        "first": dual_screening.CHATGPT_FIRST_MODEL,
        "second": dual_screening.CHATGPT_SECOND_MODEL,
        "full_text": dual_screening.CHATGPT_FULL_TEXT_MODEL,
    }
    digests = final_packet.get("screening_input_digests")
    if (
        final_packet.get("canonical_track") != "chatgpt"
        or final_packet.get("protocol_version")
        != dual_screening.SCREENING_PROTOCOL_VERSION
        or final_packet.get("screening_models") != required_models
        or not isinstance(digests, dict)
        or set(digests) != set(required_models)
        or not all(
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in digests.values()
        )
    ):
        raise RuntimeError(
            "Authoritative revision apply rejected stale model/protocol packets"
        )
    expected = dual_screening.expected_contexts(candidates)
    final_by_context = {
        str(item.get("context_id") or ""): item
        for item in final_packet.get("results", [])
    }
    if set(final_by_context) != set(expected):
        missing = sorted(set(expected).difference(final_by_context))
        unexpected = sorted(set(final_by_context).difference(expected))
        raise RuntimeError(
            "Authoritative revision apply requires one final disposition per "
            f"candidate context; missing={missing}, unexpected={unexpected}"
        )

    selected_by_norm = {
        name.casefold(): name
        for name in candidates.get("selected_exposures", [])
    }
    selected_norms = set(selected_by_norm)
    cancers = [str(value) for value in candidates.get("cancer_types", [])]
    hierarchy = exposure_hierarchy()
    unsearched_parent_scopes = {
        (parent, cancer)
        for child in selected_by_norm.values()
        for parent in hierarchy_ancestors(child, hierarchy)
        if parent.casefold() not in selected_norms
        for cancer in cancers
    }
    omissions = candidates.get("redundant_parent_context_omissions") or []
    omission_by_context = {
        (
            str(item.get("pmid") or ""),
            str(
                (item.get("omitted_context") or {}).get("exposure")
                or item.get("omitted_parent_exposure")
                or ""
            ).casefold(),
            str(
                (item.get("omitted_context") or {}).get("cancer")
                or ""
            ).casefold(),
        ): item
        for item in omissions
    }
    removal_audit = []
    mutation_scopes = {
        (exposure, cancer)
        for exposure in selected_by_norm.values()
        for cancer in cancers
    }
    mutation_scopes.update(unsearched_parent_scopes)
    for item in final_packet.get("results", []):
        if item.get("full_text_status") != "INCLUDE_META":
            continue
        direct = str(
            item.get("direct_exposure")
            or item.get("exposure")
            or ""
        )
        mutation_scopes.add((direct, str(item.get("cancer") or "")))
        mutation_scopes.update(
            (str(parent), str(item.get("cancer") or ""))
            for parent in item.get(
                "inherited_exposure_memberships"
            )
            or []
        )

    mutation_paths = {
        path
        for exposure, cancer in mutation_scopes
        if exposure and cancer
        for path in _canonical_cache_paths(exposure, cancer)
    }
    originals = {
        path: load_json(path, {}) if path.exists() else None
        for path in mutation_paths
    }

    for exposure in selected_by_norm.values():
        for cancer in cancers:
            for path in _canonical_cache_paths(exposure, cancer):
                cached = load_json(path, {}) if path.exists() else {}
                for study in cached.get("studies", []):
                    if not isinstance(study, dict):
                        continue
                    pmid = str(study.get("PMID") or "").strip()
                    direct = _direct_exposure(study, exposure)
                    record = {
                        "cache": _display_repo_path(path),
                        "pmid": pmid,
                        "source_exposure": exposure,
                        "direct_exposure": direct,
                        "cancer": cancer,
                        "source_record_id": _source_record_id(
                            exposure, cancer, study
                        ),
                    }
                    if direct.casefold() != exposure.casefold():
                        removal_audit.append(
                            {
                                **record,
                                "disposition": (
                                    "prior_hierarchy_projection_rebuilt"
                                ),
                                "reason": (
                                    "The prior inherited parent projection is "
                                    "recreated only from freshly accepted child "
                                    "evidence after direct screening."
                                ),
                            }
                        )
                        continue
                    cid = context_id(pmid, exposure, cancer)
                    disposition = final_by_context.get(cid)
                    if disposition is not None:
                        status = str(
                            disposition.get("final_status") or ""
                        )
                        reason = str(
                            disposition.get("reason")
                            or "Fresh screening supplied a final disposition."
                        )
                        removal_audit.append(
                            {
                                **record,
                                "disposition": status,
                                "reason": reason,
                            }
                        )
                        continue
                    omission = omission_by_context.get(
                        (
                            pmid,
                            exposure.casefold(),
                            cancer.casefold(),
                        )
                    )
                    if omission is not None:
                        child_ids = [
                            context_id(
                                pmid,
                                child["exposure"],
                                child["cancer"],
                            )
                            for child in omission.get(
                                "child_contexts", []
                            )
                        ]
                        if child_ids and all(
                            child_id in final_by_context
                            for child_id in child_ids
                        ):
                            removal_audit.append(
                                {
                                    **record,
                                    "disposition": (
                                        "redundant_parent_context_omitted"
                                    ),
                                    "reason": str(
                                        omission.get("reason") or ""
                                    ),
                                    "child_context_ids": child_ids,
                                }
                            )
                            continue
                    raise RuntimeError(
                        "Fail-closed authoritative revision: saved row lacks "
                        f"a final disposition ({exposure}/{cancer}/PMID {pmid})"
                    )

    revision_date = revision_manifest().get("revision_date")
    parent_rollup_audit: list[dict[str, Any]] = []
    accepted_child_replacements: dict[
        tuple[str, str, str], set[str]
    ] = defaultdict(set)
    for item in final_packet.get("results", []):
        if item.get("full_text_status") != "INCLUDE_META":
            continue
        direct = str(
            item.get("direct_exposure")
            or item.get("exposure")
            or ""
        ).strip()
        cancer = str(item.get("cancer") or "").strip()
        pmid = str(item.get("pmid") or "").strip()
        for parent in item.get("inherited_exposure_memberships") or []:
            if str(parent).casefold() in selected_norms:
                continue
            accepted_child_replacements[
                (str(parent).casefold(), cancer.casefold(), pmid)
            ].add(direct)
    try:
        # A parent not searched in this revision keeps its own direct evidence
        # and other children's evidence.  Only projections whose direct child
        # was authoritatively rescreened are replaced.
        for parent, cancer in sorted(unsearched_parent_scopes):
            for path in _canonical_cache_paths(parent, cancer):
                if not path.exists():
                    continue
                cached = load_json(path, {})
                retained, removed_audit = [], []
                for study in cached.get("studies", []):
                    direct = _direct_exposure(study, parent)
                    pmid = str(study.get("PMID") or "").strip()
                    replacement_children = accepted_child_replacements.get(
                        (parent.casefold(), cancer.casefold(), pmid), set()
                    )
                    if direct.casefold() in selected_norms:
                        removed_audit.append(
                            {
                                "pmid": pmid,
                                "parent_exposure": parent,
                                "direct_exposure": direct,
                                "cancer": cancer,
                                "reason": (
                                    "Replaced an inherited row from an "
                                    "authoritatively rescreened child while "
                                    "preserving unsearched parent-direct rows."
                                ),
                            }
                        )
                    elif (
                        direct.casefold() == parent.casefold()
                        and replacement_children
                    ):
                        removed_audit.append(
                            {
                                "pmid": pmid,
                                "parent_exposure": parent,
                                "direct_exposure": direct,
                                "cancer": cancer,
                                "replacement_direct_exposures": sorted(
                                    replacement_children,
                                    key=str.casefold,
                                ),
                                "reason": (
                                    "Replaced a redundant parent-direct row "
                                    "for the same PMID with its freshly "
                                    "screened, more-specific child evidence."
                                ),
                            }
                        )
                    else:
                        retained.append(study)
                if not removed_audit:
                    continue
                _replace_unsearched_parent_rows(
                    path,
                    parent,
                    cancer,
                    retained,
                    removed_audit,
                    final_packet,
                )
                parent_rollup_audit.extend(removed_audit)
        for exposure in selected_by_norm.values():
            for cancer in cancers:
                scoped_audit = [
                    item
                    for item in removal_audit
                    if item["source_exposure"].casefold()
                    == exposure.casefold()
                    and item["cancer"].casefold()
                    == cancer.casefold()
                ]
                for path in _canonical_cache_paths(exposure, cancer):
                    cached = load_json(path, {}) if path.exists() else {}
                    cached.update(
                        {
                            "studies": [],
                            "exposure": exposure,
                            "cancer": cancer,
                            "outcome": "Incidence",
                            "summary_html": (
                                "<p>No studies were retained by the "
                                "authoritative revision rescreen.</p>"
                            ),
                            "headline": None,
                            "plot_url": None,
                            "funnel_plot_url": None,
                            "baujat_plot_url": None,
                            "canonical_track": "chatgpt",
                            "screening_models": dict(required_models),
                            "screening_input_digests": dict(digests),
                            "screening_protocol_version": final_packet[
                                "protocol_version"
                            ],
                            "authoritative_revision": {
                                "revision_date": revision_date,
                                "mode": "all_history_replace",
                                "zero_inclusion_cache_created": True,
                                "prior_row_dispositions": scoped_audit,
                            },
                        }
                    )
                    save_json(
                        path,
                        json_compatible(cached),
                        indent=4,
                        ensure_ascii=True,
                    )
        merged = apply_to_saved_results(output_dir)
    except Exception:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                save_json(
                    path,
                    original,
                    indent=4,
                    ensure_ascii=True,
                )
        raise

    packet = {
        **merged,
        "run_mode": "authoritative_revision_rebuild",
        "revision_date": revision_date,
        "transaction_rule": (
            "prevalidate_all_old_rows_then_replace_or_rollback"
        ),
        "authoritatively_replaced_scopes": len(
            selected_by_norm
        )
        * len(cancers),
        "prior_row_dispositions": removal_audit,
        "unsearched_parent_inherited_row_replacements": parent_rollup_audit,
    }
    save_json(
        output_dir / "revision_authoritative_rebuild.json",
        json_compatible(packet),
    )
    save_json(
        output_dir / "applied_changes.json",
        json_compatible(packet),
    )
    return packet


def apply_screened_results(output_dir: Path) -> dict[str, Any]:
    require_output_not_superseded(output_dir)
    candidates = load_json(output_dir / "candidates.json", {})
    if candidates.get("run_mode") == "included_reclassification":
        return apply_reclassification_results(output_dir)
    if (
        candidates.get("revision_aware")
        and candidates.get("run_mode") != "included_reclassification"
    ):
        return apply_revision_authoritative_results(output_dir)
    return apply_to_saved_results(output_dir)

def write_run_cost_report(output_dir: Path, report_dir: Path) -> Path:
    """Update both the all-exposure and additional-only cost ledgers."""
    root_path = REPO_ROOT / "cost.txt"
    destinations = (
        (root_path, "all"),
        (report_dir / "cost.txt", "all"),
        (REPO_ROOT / "additionalExposureCost.txt", "additional"),
        (report_dir / "additionalExposureCost.txt", "additional"),
    )
    written: set[Path] = set()
    for destination, scope in destinations:
        resolved = destination.resolve()
        if resolved in written:
            continue
        cost_accounting.write_cost_report(
            output_dir,
            REPO_ROOT,
            report_path=destination,
            exposure_scope=scope,
        )
        written.add(resolved)
    return root_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument(
        "--all-history-through",
        metavar="YYYY-MM-DD",
        help=(
            "search all indexed publication history through this inclusive end date; "
            "artifacts retain no lower limit while PubMed receives its earliest-date "
            "sentinel solely to make the upper bound effective"
        ),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--exposure",
        dest="exposures",
        action="append",
        help=(
            "limit discovery to one canonical exposure; repeat for multiple "
            "exposures (matching is case-insensitive)"
        ),
    )
    parser.add_argument(
        "--exposure-scope",
        choices=("current", "additional", "all"),
        default="current",
        help=(
            "choose static/synonyms exposures, the collision-checked "
            "additionalExposure.json + additionalExposureV2.json union, or all"
        ),
    )
    parser.add_argument(
        "--revision-aware",
        action="store_true",
        help=(
            "search alias-expanded/new current exposures plus every V1/V2 "
            "additional exposure; narrowed sources remain included-record review only"
        ),
    )
    parser.add_argument(
        "--revision-reclassification",
        action="store_true",
        help=(
            "screen only already-included narrowed-source PMIDs against declared "
            "targets, with zero PubMed ESearch"
        ),
    )
    parser.add_argument(
        "--rollup-vitamin-k",
        action="store_true",
        help=(
            "add a parent Vitamin K candidate context for every Vitamin K1, "
            "Vitamin K2, or Vitamin K3 search hit while retaining the child context"
        ),
    )
    parser.add_argument(
        "--stage",
        choices=(
            "discover", "refresh-metadata", "first-screen", "second-screen",
            "report", "full-text", "finalize", "apply", "cost-report",
            "reclassification-review", "all",
        ),
        default="discover",
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--report-dir", type=Path, default=REPO_ROOT.parent)
    parser.add_argument(
        "--monthly-report-subdir",
        action="store_true",
        help="write human-readable reports under REPORT_DIR/YYYY-MM-DD_YYYY-MM-DD",
    )
    args = parser.parse_args()
    if args.stage == "reclassification-review":
        if args.output_dir is None:
            args.output_dir = DATA_DIR / "exposure_reclassification_review"
        return args
    if args.revision_reclassification:
        conflicts = (
            args.start_date
            or args.end_date
            or args.previous_month
            or args.all_history_through
            or args.revision_aware
            or args.exposures
            or args.exposure_scope != "current"
        )
        if conflicts:
            parser.error(
                "--revision-reclassification cannot be combined with date, "
                "catalog, exposure, or --revision-aware options"
            )
        revision_date = str(revision_manifest().get("revision_date") or "")
        try:
            date.fromisoformat(revision_date)
        except ValueError:
            parser.error("revision manifest requires an ISO revision_date")
        args.start_date = None
        args.end_date = revision_date
    if (args.exposures or args.rollup_vitamin_k or args.exposure_scope != "current") and args.stage not in {"discover", "all"}:
        parser.error(
            "--exposure, --exposure-scope, and --rollup-vitamin-k apply only to discover/all stages"
        )
    if args.all_history_through and (
        args.previous_month or args.start_date or args.end_date
    ):
        parser.error(
            "--all-history-through cannot be combined with --previous-month, "
            "--start-date, or --end-date"
        )
    if args.all_history_through:
        try:
            date.fromisoformat(args.all_history_through)
        except ValueError:
            parser.error("--all-history-through must be an ISO date (YYYY-MM-DD)")
        if args.force:
            parser.error(
                "--force is disabled for all-history runs; resume the immutable checkpoints instead"
            )
        args.start_date = None
        args.end_date = args.all_history_through
    if args.revision_aware and not args.all_history_through:
        parser.error(
            "--revision-aware requires --all-history-through; partial-date "
            "revision discovery is forbidden"
        )
    if args.previous_month:
        args.start_date, args.end_date = previous_month()
    if (
        not args.revision_reclassification
        and not args.all_history_through
        and (not args.start_date or not args.end_date)
    ):
        parser.error(
            "provide --start-date and --end-date, use --previous-month, "
            "or use --all-history-through"
        )
    if args.output_dir is None:
        subdir = (
            f"included_reclassification_{args.end_date}"
            if args.revision_reclassification
            else (
                f"all_history_through_{args.end_date}"
                if args.all_history_through
                else f"{args.start_date}_{args.end_date}"
            )
        )
        args.output_dir = DATA_DIR / "monthly_updates" / subdir
    if args.monthly_report_subdir:
        subdir = (
            f"all_history_through_{args.end_date}"
            if args.all_history_through
            else f"{args.start_date}_{args.end_date}"
        )
        args.report_dir = args.report_dir / subdir
    return args


def rebuild_summary_assets() -> dict[str, Any]:
    """Synchronize cross-exposure workbooks and plots after cache application."""
    from scripts import build_summary_plots

    return build_summary_plots.build(CACHE_DIR, REPO_ROOT / "Plot")


def main() -> int:
    args = parse_args()
    if args.stage == "all":
        if args.revision_reclassification:
            discover_reclassification(args.output_dir, force=args.force)
        else:
            discover(
                args.start_date,
                args.end_date,
                args.output_dir,
                force=args.force,
                requested_exposures=args.exposures,
                rollup_vitamin_k=args.rollup_vitamin_k,
                exposure_scope=args.exposure_scope,
                revision_aware=args.revision_aware,
            )
        first_screen(args.output_dir, batch_size=args.batch_size, force=args.force)
        second_screen(args.output_dir, batch_size=args.batch_size, force=args.force)
        # Preserve the requested pre-retrieval report before any full-text or
        # saved-result work changes the evidence state.
        write_reports(args.output_dir, args.report_dir)
        full_text_review(args.output_dir, force=args.force)
        finalize_evidence(args.output_dir, args.report_dir)
        apply_screened_results(args.output_dir)
        rebuild_summary_assets()
        write_run_cost_report(args.output_dir, args.report_dir)
    elif args.stage == "discover":
        if args.revision_reclassification:
            discover_reclassification(args.output_dir, force=args.force)
        else:
            discover(
                args.start_date,
                args.end_date,
                args.output_dir,
                force=args.force,
                requested_exposures=args.exposures,
                rollup_vitamin_k=args.rollup_vitamin_k,
                exposure_scope=args.exposure_scope,
                revision_aware=args.revision_aware,
            )
    elif args.stage == "refresh-metadata":
        refresh_candidate_metadata(args.output_dir)
    elif args.stage == "first-screen":
        first_screen(args.output_dir, batch_size=args.batch_size, force=args.force)
    elif args.stage == "second-screen":
        second_screen(args.output_dir, batch_size=args.batch_size, force=args.force)
    elif args.stage == "report":
        write_reports(args.output_dir, args.report_dir)
    elif args.stage == "full-text":
        full_text_review(args.output_dir, force=args.force)
    elif args.stage == "finalize":
        finalize_evidence(args.output_dir, args.report_dir)
    elif args.stage == "apply":
        apply_screened_results(args.output_dir)
        rebuild_summary_assets()
    elif args.stage == "cost-report":
        write_run_cost_report(args.output_dir, args.report_dir)
    elif args.stage == "reclassification-review":
        reclassification_review.prepare_review(
            repo_root=REPO_ROOT,
            cache_dir=CACHE_DIR,
            manifest_path=EXPOSURE_REVISION_FILE,
            output_path=args.output_dir / "included_reclassification_review.json",
            expected_protocol=dual_screening.SCREENING_PROTOCOL_VERSION,
            expected_models={
                "first": dual_screening.CHATGPT_FIRST_MODEL,
                "second": dual_screening.CHATGPT_SECOND_MODEL,
                "full_text": dual_screening.CHATGPT_FULL_TEXT_MODEL,
            },
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Monthly evidence update failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
