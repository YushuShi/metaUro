#!/usr/bin/env python3
"""Discover and audit newly published MetaVir evidence.

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
from scripts import audit_blocked_pmc_sources, cost_accounting, dual_screening  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = REPO_ROOT / "Cached_results"
EXPOSURES_FILE = REPO_ROOT / "static" / "exposures.json"
SYNONYMS_FILE = DATA_DIR / "synonyms_cache.json"
ADDITIONAL_EXPOSURES_FILE = DATA_DIR / "additionalExposure.json"
ADDITIONAL_EXPOSURES_V2_FILE = DATA_DIR / "additionalExposureV2.json"
EXPOSURE_HIERARCHY_FILE = DATA_DIR / "exposure_hierarchy.json"
EXPOSURE_REVISION_FILE = DATA_DIR / "exposure_revision_manifest.json"
CANDIDATE_GENERATION_STATE_NAME = "candidate_generation_state.json"
NCBI_KEYED_MIN_INTERVAL_SECONDS = 0.11
NCBI_PUBLIC_MIN_INTERVAL_SECONDS = 0.37
_ENTREZ_CONFIGURATION_LOCK = threading.RLock()
_ENTREZ_REJECTED_KEY_DIGEST: str | None = None
_NCBI_RATE_LOCK = threading.Lock()
_NCBI_LAST_REQUEST_AT: float | None = None
_NCBI_RATE_SLEEP = time.sleep

# Narrowed/reclassified current exposures are deliberately absent here: their
# already-included records receive a local classification review and must not
# trigger a fresh PubMed search.  The separate manifest field remains the
# durable audit list for that review.
REVISION_NCBI_SCOPE_FIELDS = (
    "alias_expanded_existing",
    "new_in_modified_synonyms",
    "modified_synonyms",
)

CANCER_QUERIES = {
    "Prostate cancer": (
        '"Prostatic Neoplasms"[MeSH Terms] OR "prostate cancer"[Title/Abstract] '
        'OR "prostatic cancer"[Title/Abstract] OR "prostate carcinoma"[Title/Abstract] '
        'OR "prostatic carcinoma"[Title/Abstract]'
    ),
}


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


def file_sha256(path: Path) -> str:
    """Return a byte-exact artifact digest."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_candidate_generation_ready(output_dir: Path) -> dict[str, Any] | None:
    """Fail closed while a superseded candidate generation is being replaced."""
    state_path = output_dir / CANDIDATE_GENERATION_STATE_NAME
    if not state_path.exists():
        return None
    state = load_json(state_path, {})
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise RuntimeError("Candidate-generation state is missing or malformed")
    if state.get("status") != "resealed":
        raise RuntimeError(
            "Candidate generation is superseded and its corrective migration is incomplete"
        )
    candidate_path = output_dir / "candidates.json"
    expected_sha = str(state.get("candidate_sha256") or "")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
        or not candidate_path.is_file()
        or file_sha256(candidate_path) != expected_sha
    ):
        raise RuntimeError("Resealed candidate generation does not match its audit digest")
    candidates = load_json(candidate_path, {})
    if candidates.get("vocabulary_digest") != state.get("vocabulary_digest"):
        raise RuntimeError("Resealed candidate vocabulary does not match its migration state")
    return state


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
            target = _NCBI_LAST_REQUEST_AT + minimum_interval
            delay = target - now
            while delay > 0:
                _NCBI_RATE_SLEEP(delay)
                now = time.monotonic()
                delay = target - now
        _NCBI_LAST_REQUEST_AT = now


def reject_invalid_ncbi_api_key(
    error: BaseException,
    attempted_api_key: str | None,
) -> bool:
    """Latch one key's HTTP-400 rejection and select the public endpoint."""
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
        if (
            current_key
            and hashlib.sha256(current_key.encode("utf-8")).hexdigest() == digest
        ):
            Entrez.api_key = None
    if first_rejection:
        print(
            "WARNING: NCBI_API_KEY was rejected (HTTP 400; likely expired or invalid). "
            "Continuing through NCBI's public rate-limited endpoint.",
            flush=True,
        )
    return True


def configure_entrez(*, validate_api_key: bool = True) -> str:
    global _ENTREZ_REJECTED_KEY_DIGEST
    # A locally edited mykey.env should take precedence over stale variables in
    # a long-lived shell. GitHub Actions has no mykey.env, so repository secrets
    # remain authoritative there.
    with _ENTREZ_CONFIGURATION_LOCK:
        load_dotenv(REPO_ROOT / "mykey.env", override=True)
        Entrez.email = os.getenv("NCBI_EMAIL") or os.getenv("PUBMED_EMAIL")
        requested_key = os.getenv("NCBI_API_KEY") or os.getenv("PUBMED_API_KEY")
        # All retries belong to the explicit, paced loops in this module.
        Entrez.max_tries = 1
        strict_key = (
            os.getenv("MONTHLY_REQUIRE_VALID_NCBI_KEY", "false").lower()
            == "true"
        )
        if not Entrez.email:
            raise RuntimeError("NCBI_EMAIL (or PUBMED_EMAIL) is not configured")
        if not requested_key:
            Entrez.api_key = None
            if strict_key:
                raise RuntimeError(
                    "NCBI_API_KEY is missing; strict monthly validation is enabled"
                )
            return "not_configured"

        key_digest = hashlib.sha256(requested_key.encode("utf-8")).hexdigest()
        if _ENTREZ_REJECTED_KEY_DIGEST == key_digest:
            # load_dotenv(override=True) may restore the rejected secret in the
            # environment, but the process-local digest latch keeps it off the
            # wire for every subsequent configure call.
            Entrez.api_key = None
            if strict_key:
                raise RuntimeError(
                    "NCBI_API_KEY was previously rejected with HTTP 400; "
                    "strict monthly validation is enabled"
                )
            return "rejected_http_400"

        Entrez.api_key = requested_key
        # Included-only reclassification is prohibited from issuing ESearch.
        # Direct EFetch performs the same fail-closed key detection instead.
        if not validate_api_key:
            return "configured_not_validated"
        # Validate a configured credential before ordinary ESearch. A rejected
        # key is the state that is latched: valid keys may be rechecked because
        # they can expire during a long resumable run.
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                wait_for_ncbi_request_slot()
                with Entrez.esearch(
                    db="pubmed", term="prostate cancer", retmax=0
                ) as handle:
                    Entrez.read(handle)
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
        cancer = safe_context_part(cached.get("cancer") or "prostate_cancer")
        for study in cached.get("studies", []):
            pmid = str(study.get("PMID") or "").strip()
            if pmid and pmid.lower() not in {"none", "nan"}:
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


def current_exposure_names() -> list[str]:
    values = load_json(EXPOSURES_FILE, [])
    if not isinstance(values, list):
        raise ValueError("static/exposures.json must be a JSON array")
    return [str(value) for value in values if str(value).strip()]


def additional_exposure_catalog() -> dict[str, dict[str, Any]]:
    """Load the collision-checked V1 + V2 additional-exposure union."""
    result: dict[str, dict[str, Any]] = {}
    canonical_by_key: dict[str, tuple[str, Path]] = {}
    for path in (ADDITIONAL_EXPOSURES_FILE, ADDITIONAL_EXPOSURES_V2_FILE):
        raw = load_json(path, {})
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be a JSON object")
        for raw_name, entry in raw.items():
            name = str(raw_name).strip()
            if not name:
                continue
            key = name.casefold()
            if key in canonical_by_key:
                prior_name, prior_path = canonical_by_key[key]
                raise ValueError(
                    "Additional exposure catalogs duplicate canonical exposure "
                    f"{name!r}: {prior_path} ({prior_name!r}) and {path}"
                )
            canonical_by_key[key] = (name, path)
            result[name] = (
                dict(entry) if isinstance(entry, dict) else {"core": entry}
            )
    return result


def revision_manifest() -> dict[str, Any]:
    raw = load_json(EXPOSURE_REVISION_FILE, {})
    if not isinstance(raw, dict):
        raise ValueError("data/exposure_revision_manifest.json must be a JSON object")
    return raw


def _revision_group_names(field_names: tuple[str, ...]) -> set[str]:
    raw = revision_manifest()
    groups = raw.get("sets") if isinstance(raw.get("sets"), dict) else raw
    selected: set[str] = set()
    for field in field_names:
        values = groups.get(field, [])
        if values is None:
            continue
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"Revision manifest field {field!r} must be a string list")
        selected.update(value.strip().casefold() for value in values if value.strip())
    return selected


def revision_ncbi_scope_names() -> set[str]:
    """Return only changed current exposures authorized for a new NCBI search."""
    selected = _revision_group_names(REVISION_NCBI_SCOPE_FIELDS)
    if not selected:
        raise ValueError("Revision manifest selects no current exposures for NCBI search")
    return selected


def reclassification_scope_names() -> set[str]:
    """Return narrowed/reclassified current exposures for included-only review."""
    return _revision_group_names(("alias_narrowed_or_reclassified",))


def reclassification_targets() -> dict[str, list[str]]:
    """Return the approved source-to-target classification map."""
    raw = revision_manifest().get("reclassification_targets", {})
    if not isinstance(raw, dict):
        raise ValueError("Revision manifest reclassification_targets must be an object")
    current_by_key = {name.casefold(): name for name in current_exposure_names()}
    additional_by_key = {
        name.casefold(): name for name in additional_exposure_catalog()
    }
    searchable_by_key = {**current_by_key, **additional_by_key}
    scope = reclassification_scope_names()
    targets: dict[str, list[str]] = {}
    for source_key in sorted(scope):
        source = current_by_key.get(source_key)
        if not source:
            raise ValueError(
                f"Reclassification source {source_key!r} is not a current exposure"
            )
        values = raw.get(source)
        if not isinstance(values, list) or not values:
            raise ValueError(
                f"Reclassification source {source!r} requires at least one target"
            )
        resolved: list[str] = []
        for value in values:
            target = searchable_by_key.get(str(value).strip().casefold())
            if not target:
                raise ValueError(
                    f"Reclassification target {value!r} for {source!r} is not searchable"
                )
            if target.casefold() == source.casefold():
                raise ValueError(f"Reclassification source {source!r} cannot target itself")
            if target not in resolved:
                resolved.append(target)
        targets[source] = resolved
    return targets


def _canonical_cache_paths(exposure: str, cancer: str) -> list[Path]:
    safe_exposure = re.sub(r"[^a-zA-Z0-9]+", "_", exposure.lower()).strip("_")
    safe_cancer = re.sub(r"[^a-zA-Z0-9]+", "_", cancer.lower()).strip("_")
    return [
        CACHE_DIR / safe_exposure / f"{safe_cancer}_incidence_true_{suffix}.json"
        for suffix in ("all", "core")
    ]


def _load_required_json_object(path: Path, label: str = "JSON file") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} {path} is unreadable or invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} {path} must be a JSON object")
    return value


def _normalised_comparison(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _source_record_id(source: str, cancer: str, study: dict[str, Any]) -> str:
    """Identify one already-included direct effect in both all/core caches."""
    payload = {
        "source": source,
        "cancer": cancer,
        "pmid": str(study.get("PMID") or ""),
        "direct_exposure": str(study.get("Direct Exposure") or source),
        "effect_type": str(study.get("Effect Type") or "").upper(),
        "effect_size": study.get("Effect Size"),
        "ci_lower": study.get("Lower CI"),
        "ci_upper": study.get("Upper CI"),
        "comparison": _normalised_comparison(
            study.get("comparison_type")
            or (study.get("extraction_supporting_text") or {}).get("exposure_definition")
        ),
    }
    material = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _canonical_digest(value: Any) -> str:
    """Return a stable digest for durable scope and lineage comparisons."""
    material = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _reclassification_source_digest(
    records: list[dict[str, Any]],
    targets: dict[str, list[str]],
) -> str:
    return _canonical_digest({"records": records, "targets": targets})


def included_reclassification_records() -> list[dict[str, Any]]:
    """Load the validated union of direct effects in both all/core caches.

    The two canonical variants occasionally diverge after an interrupted legacy
    run.  Choosing the first file would silently omit a source effect from the
    included-only review, so membership is unioned by the same-effect record ID
    and the contributing cache variants are retained in the immutable packet.
    """
    records: dict[str, dict[str, Any]] = {}
    for source in reclassification_targets():
        for cancer in CANCER_QUERIES:
            for cache_path in _canonical_cache_paths(source, cancer):
                if not cache_path.exists():
                    continue
                cached = _load_required_json_object(cache_path, "Included cache")
                variant = cache_path.stem.rsplit("_", 1)[-1]
                for study in cached.get("studies", []):
                    if not isinstance(study, dict):
                        continue
                    pmid = str(study.get("PMID") or "").strip()
                    if not pmid or pmid.casefold() in {"none", "nan"}:
                        continue
                    direct = str(study.get("Direct Exposure") or source).strip()
                    # Child evidence already rolled into a parent is not a direct
                    # source record and must never be reclassified a second time.
                    if direct.casefold() != source.casefold():
                        continue
                    record_id = _source_record_id(source, cancer, study)
                    if record_id not in records:
                        records[record_id] = {
                            "source_record_id": record_id,
                            "source_exposure": source,
                            "cancer": cancer,
                            "outcome": "Incidence",
                            "pmid": pmid,
                            "source_study": study,
                            "source_cache_variants": [],
                        }
                    variants = records[record_id]["source_cache_variants"]
                    if variant not in variants:
                        variants.append(variant)
                        variants.sort()
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


def _reclassification_context_roles(
    source_records: list[dict[str, Any]],
    targets: dict[str, list[str]],
) -> dict[str, dict[tuple[str, str], dict[str, set[str]]]]:
    """Describe why each included PMID/exposure context is being screened."""
    roles: dict[str, dict[tuple[str, str], dict[str, set[str]]]] = defaultdict(dict)

    def add(
        pmid: str,
        exposure: str,
        cancer: str,
        role: str,
        source: str,
    ) -> None:
        detail = roles[pmid].setdefault(
            (exposure, cancer),
            {"roles": set(), "sources": set()},
        )
        detail["roles"].add(role)
        detail["sources"].add(source)

    for record in source_records:
        source = str(record["source_exposure"])
        cancer = str(record["cancer"])
        pmid = str(record["pmid"])
        add(pmid, source, cancer, "source", source)
        for target in targets[source]:
            add(pmid, target, cancer, "approved_target", source)
    return roles


def _serialise_reclassification_context(
    exposure: str,
    cancer: str,
    detail: dict[str, set[str]],
) -> dict[str, Any]:
    role_order = {"source": 0, "approved_target": 1}
    return {
        "exposure": exposure,
        "cancer": cancer,
        "outcome": "Incidence",
        "reclassification_roles": sorted(
            detail["roles"], key=lambda role: (role_order.get(role, 99), role)
        ),
        "reclassification_sources": sorted(detail["sources"], key=str.casefold),
    }


def exposure_terms(
    catalog: str = "current",
    *,
    revision_aware: bool = False,
) -> dict[str, list[str]]:
    if catalog not in {"current", "additional", "combined"}:
        raise ValueError(f"Unsupported exposure catalog: {catalog!r}")
    current = current_exposure_names()
    synonyms = load_json(SYNONYMS_FILE, {})
    additional = (
        additional_exposure_catalog()
        if catalog in {"additional", "combined"} or revision_aware
        else {}
    )
    current_casefold = {name.casefold(): name for name in current}
    collisions = sorted(name for name in additional if name.casefold() in current_casefold)
    if collisions:
        raise ValueError(
            "Additional exposure catalogs duplicate current canonical exposures: "
            + ", ".join(collisions)
        )
    exposures = []
    if catalog in {"current", "combined"}:
        exposures.extend(current)
    if catalog in {"additional", "combined"}:
        exposures.extend(additional)
    if revision_aware:
        selected_current = revision_ncbi_scope_names()
        unknown = sorted(selected_current.difference(current_casefold))
        if unknown:
            raise ValueError(
                "Revision manifest names are not current canonical exposures: "
                + ", ".join(unknown)
            )
        # A revision run searches expanded/new current entries plus the entire
        # approved additional catalog. Narrowed entries are intentionally local.
        exposures = [
            name for name in current if name.casefold() in selected_current
        ] + list(additional)
    synonyms_by_name = {
        str(name).casefold(): value for name, value in synonyms.items()
    }
    additional_by_name = {
        str(name).casefold(): value for name, value in additional.items()
    }
    result: dict[str, list[str]] = {}
    for exposure in exposures:
        key = str(exposure).casefold()
        cache_entry = additional_by_name.get(key, synonyms_by_name.get(key, {}))
        terms = (
            [str(exposure)]
            if not isinstance(cache_entry, dict)
            or cache_entry.get("include_canonical_in_search", True)
            else []
        )
        if isinstance(cache_entry, dict):
            # ``guarded`` terms are deliberately broad discovery hooks that
            # must survive Luna/Sol classification; they are not promoted to
            # interchangeable UI aliases. ``downstream``/``related`` remain
            # explicit discovery terms rather than canonical aliases.
            fields = ["core", "downstream", "related", "search_terms"]
            if cache_entry.get("include_guarded_in_search", True):
                fields.insert(1, "guarded")
            for field in fields:
                value = cache_entry.get(field, "")
                if isinstance(value, list):
                    terms.extend(value)
                else:
                    terms.extend(_split_catalog_scalar(value, str(exposure), field))
        else:
            terms.extend(
                _split_catalog_scalar(cache_entry, str(exposure), "<entry>")
            )
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw_term in terms:
            term = clean_search_term(raw_term)
            key = term.casefold()
            if len(term) >= 3 and key not in seen:
                cleaned.append(term)
                seen.add(key)
        # Keep the explicitly reviewed vocabulary intact. The longest current
        # lists remain comfortably within PubMed's query-size limits.
        result[str(exposure)] = cleaned[:60]
    return result


def exposure_vocabulary_digest(terms: dict[str, list[str]]) -> str:
    """Bind reusable discovery packets to the exact reviewed vocabulary."""
    payload = json.dumps(terms, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def exposure_hierarchy() -> dict[str, list[str]]:
    """Return direct child-to-parent memberships without transitive expansion."""
    expected_digest = str(
        revision_manifest().get("exposure_hierarchy_sha256") or ""
    )
    actual_digest = hashlib.sha256(EXPOSURE_HIERARCHY_FILE.read_bytes()).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest) or (
        actual_digest != expected_digest
    ):
        raise ValueError(
            "data/exposure_hierarchy.json does not match the reviewed hierarchy "
            "checksum in data/exposure_revision_manifest.json"
        )
    raw = load_json(EXPOSURE_HIERARCHY_FILE, {})
    if not isinstance(raw, dict):
        raise ValueError("data/exposure_hierarchy.json must be a JSON object")
    return {
        str(child): [str(parent) for parent in parents if str(parent).strip()]
        for child, parents in raw.items()
        if isinstance(parents, list)
    }


def inherited_exposures(exposure: str) -> list[str]:
    """Compute acyclic transitive parent membership for final UI roll-up."""
    hierarchy = exposure_hierarchy()
    inherited: list[str] = []
    visiting: set[str] = set()
    lipid_classes = {"Monounsaturated fat (MUFA)", "Polyunsaturated fat (PUFA)"}
    explicit_root_classes = set(hierarchy.get(exposure, [])).intersection(lipid_classes)

    def visit(child: str) -> None:
        if child in visiting:
            raise ValueError(f"Cycle in exposure hierarchy at {child!r}")
        visiting.add(child)
        for parent in hierarchy.get(child, []):
            # A chemically explicit leaf classification overrides a generic
            # family-to-class edge. Mead acid is omega-9 but polyunsaturated;
            # it must not inherit MUFA merely through the omega-9 parent.
            if (
                parent in lipid_classes
                and explicit_root_classes
                and parent not in explicit_root_classes
            ):
                continue
            if parent not in inherited:
                inherited.append(parent)
                visit(parent)
        visiting.remove(child)

    visit(exposure)
    return inherited


def dedupe_parent_search_contexts(
    contexts_by_pmid: dict[str, set[tuple[str, str]]],
) -> list[dict[str, Any]]:
    """Drop a parent context when a directly found child will roll up to it."""
    removed: list[dict[str, Any]] = []
    for pmid, contexts in contexts_by_pmid.items():
        exposures_by_cancer: dict[str, set[str]] = defaultdict(set)
        for exposure, cancer in contexts:
            exposures_by_cancer[cancer].add(exposure)
        redundant: set[tuple[str, str]] = set()
        for cancer, exposures in exposures_by_cancer.items():
            for child in exposures:
                for parent in inherited_exposures(child):
                    if parent in exposures:
                        redundant.add((parent, cancer))
        if redundant:
            contexts.difference_update(redundant)
            for parent, cancer in sorted(redundant):
                children = sorted(
                    child
                    for child in exposures_by_cancer[cancer]
                    if child != parent and parent in inherited_exposures(child)
                )
                removed.append(
                    {
                        "pmid": str(pmid),
                        "exposure": parent,
                        "cancer": cancer,
                        "child_contexts": children,
                        "reason": (
                            "The directly discovered child context(s) will be screened "
                            "once and rolled up after screening, so this duplicate parent "
                            "model context was omitted."
                        ),
                    }
                )
    return removed


def selected_exposure_terms(
    selected_exposures: list[str] | None = None,
    *,
    catalog: str = "current",
    revision_aware: bool = False,
) -> dict[str, list[str]]:
    """Return the exact requested exposure subset in canonical catalog order."""
    terms = exposure_terms(catalog, revision_aware=revision_aware)
    if not selected_exposures:
        return terms
    requested = set(selected_exposures)
    unknown = sorted(requested.difference(terms))
    if unknown:
        raise ValueError(
            "Unknown --exposure value(s): "
            + ", ".join(repr(value) for value in unknown)
            + ". Values must exactly match the selected exposure catalog."
        )
    return {name: values for name, values in terms.items() if name in requested}


def quote_term(term: str) -> str:
    escaped = term.replace('"', "")
    return f'"{escaped}"[Title/Abstract]'


def build_query(terms: list[str], cancer_query: str) -> str:
    exposure_query = " OR ".join(quote_term(term) for term in terms)
    return f"(({exposure_query}) AND ({cancer_query}))"


class EntrezSearchIds(list[str]):
    """PMID list with a durable completeness audit for its ESearch query."""

    def __init__(self, values: list[str], audit: dict[str, Any]):
        super().__init__(values)
        self.audit = audit


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


def entrez_search(query: str, start_date: str | None, end_date: str) -> EntrezSearchIds:
    """Retrieve a complete PubMed set without relying on >10k pagination.

    PubMed cannot reliably page arbitrary ESearch sets beyond 10,000 records.
    Large intervals are therefore split into disjoint publication-date ranges
    until every leaf Count is at most 10,000. A single day above the limit is a
    hard failure because there is no date-only split that can prove completeness.
    """
    page_size = 10000
    try:
        lower = date.fromisoformat(start_date) if start_date else date.min
        upper = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("ESearch dates must be ISO YYYY-MM-DD values") from exc
    if lower > upper:
        raise ValueError("ESearch start_date cannot be after end_date")

    def date_parameter(value: date) -> str:
        return f"{value.year:04d}/{value.month:02d}/{value.day:02d}"

    request_count = 0

    def fetch_interval(interval_start: date, interval_end: date) -> dict[str, Any]:
        nonlocal request_count
        parameters = {
            "db": "pubmed",
            "term": query,
            "retmax": page_size,
            "retstart": 0,
            "datetype": "pdat",
            "mindate": date_parameter(interval_start),
            "maxdate": date_parameter(interval_end),
        }
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                request_count += 1
                wait_for_ncbi_request_slot()
                with Entrez.esearch(**parameters) as handle:
                    record = Entrez.read(handle)
                if not isinstance(record, dict):
                    raise RuntimeError("NCBI ESearch returned a non-object record")
                return record
            except Exception as exc:  # pragma: no cover - network retry path
                last_error = exc
                if not retryable_entrez_error(exc) or attempt >= 3:
                    break
                time.sleep(2 ** attempt)
        raise RuntimeError(
            "NCBI ESearch failed for publication interval "
            f"{interval_start.isoformat()} through {interval_end.isoformat()}: {last_error}"
        )

    def exact_count(record: dict[str, Any], interval_start: date, interval_end: date) -> int:
        if "Count" not in record:
            raise RuntimeError(
                "NCBI ESearch omitted Count for publication interval "
                f"{interval_start.isoformat()} through {interval_end.isoformat()}"
            )
        try:
            count = int(str(record["Count"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("NCBI ESearch returned an invalid Count") from exc
        if count < 0:
            raise RuntimeError("NCBI ESearch returned a negative Count")
        return count

    leaves: list[dict[str, Any]] = []

    def collect(
        interval_start: date,
        interval_end: date,
        record: dict[str, Any] | None = None,
    ) -> tuple[list[str], int]:
        if record is None:
            record = fetch_interval(interval_start, interval_end)
        count = exact_count(record, interval_start, interval_end)
        if count > page_size:
            if interval_start == interval_end:
                raise RuntimeError(
                    "NCBI ESearch returned more than 10,000 records for the single "
                    f"publication day {interval_start.isoformat()}; completeness "
                    "cannot be established by date splitting."
                )
            midpoint = interval_start + (interval_end - interval_start) // 2
            left_ids, left_count = collect(interval_start, midpoint)
            right_ids, right_count = collect(midpoint + timedelta(days=1), interval_end)
            if left_count + right_count != count:
                raise RuntimeError(
                    "NCBI ESearch child interval Counts do not equal their parent "
                    f"Count ({left_count} + {right_count} != {count})."
                )
            return left_ids + right_ids, count

        raw_ids = record.get("IdList")
        if not isinstance(raw_ids, (list, tuple)):
            raise RuntimeError("NCBI ESearch leaf omitted IdList")
        identifiers = [str(value) for value in raw_ids]
        unique_identifiers = list(dict.fromkeys(identifiers))
        if len(identifiers) != count or len(unique_identifiers) != count:
            raise RuntimeError(
                "NCBI ESearch leaf Count/IdList mismatch for publication interval "
                f"{interval_start.isoformat()} through {interval_end.isoformat()}: "
                f"Count={count}, returned={len(identifiers)}, unique={len(unique_identifiers)}."
            )
        leaves.append(
            {
                "publication_date_start": interval_start.isoformat(),
                "publication_date_end": interval_end.isoformat(),
                "reported_count": count,
                "returned_unique_pmids": len(unique_identifiers),
                "complete": True,
            }
        )
        return unique_identifiers, count

    root_record = fetch_interval(lower, upper)
    identifiers, reported_count = collect(lower, upper, root_record)
    unique_identifiers = list(dict.fromkeys(identifiers))
    if len(identifiers) != reported_count or len(unique_identifiers) != reported_count:
        raise RuntimeError(
            "NCBI ESearch root Count/unique PMID mismatch after date splitting: "
            f"Count={reported_count}, returned={len(identifiers)}, "
            f"unique={len(unique_identifiers)}."
        )
    audit = {
        "complete": True,
        "reported_count": reported_count,
        "returned_unique_pmids": len(unique_identifiers),
        "request_count": request_count,
        "leaf_intervals": leaves,
    }
    return EntrezSearchIds(unique_identifiers, audit)


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
        # NCBI evidence-review book records commonly omit a chapter-level
        # ArticleTitle while supplying the exact indexed title as BookTitle.
        # Preserve that official title instead of emitting a blank R2 record.
        "title": scalar_text(document.get("ArticleTitle"))
        or scalar_text(book.get("BookTitle")),
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
        public_retry_after_key_rejection = False
        # Four ordinary attempts plus one reserved slot for the sole public
        # retry that may follow a keyed HTTP 400.
        for attempt in range(5):
            attempted_api_key = str(Entrez.api_key or "") or None
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
                if reject_invalid_ncbi_api_key(exc, attempted_api_key):
                    # A rejected keyed request is followed by exactly one
                    # public request. That public attempt is never expanded by
                    # the ordinary transient-retry loop.
                    public_retry_after_key_rejection = True
                    continue
                if public_retry_after_key_rejection:
                    break
                if not retryable_entrez_error(exc) or attempt >= 3:
                    break
                time.sleep(2 ** attempt)
        if last_error:
            raise RuntimeError(f"NCBI EFetch failed after retries: {last_error}")
    return records, request_count


def discover_reclassification(
    output_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    """Build an immutable packet from included source records without ESearch.

    Each PubMed article is fetched only because its PMID is already present in
    a narrowed/reclassified source cache.  Source and approved target contexts
    are screened together, so this path can never expand the article universe.
    """
    source_records = included_reclassification_records()
    targets = reclassification_targets()
    source_digest = _reclassification_source_digest(source_records, targets)
    candidate_path = output_dir / "candidates.json"
    existing = load_json(candidate_path, {})
    if (
        not force
        and existing.get("run_mode") == "included_reclassification"
        and existing.get("source_records_digest") == source_digest
        and isinstance(existing.get("articles"), list)
    ):
        print(
            "Reusing immutable included-only reclassification packet; "
            "use --force to refetch metadata.",
            flush=True,
        )
        return existing

    contexts_by_pmid = _reclassification_context_roles(source_records, targets)
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
        missing_pmids = sorted(set(pmids).difference(articles))
        if missing_pmids:
            raise RuntimeError(
                "NCBI EFetch omitted included PMIDs; no reclassification screening "
                "packet was written: " + ", ".join(missing_pmids)
            )
    else:
        api_key_status = "not_required"
        articles = {}
        fetch_requests = 0

    candidate_records = []
    for pmid in pmids:
        record = dict(articles[pmid])
        record["published_date"] = (
            record.get("journal_publication_date")
            or record.get("electronic_publication_date")
            or record.get("published_date")
            or "Unknown"
        )
        record["candidate_contexts"] = [
            _serialise_reclassification_context(exposure, cancer, detail)
            for (exposure, cancer), detail in sorted(
                contexts_by_pmid[pmid].items(),
                key=lambda item: (
                    item[0][0].casefold(),
                    item[0][1].casefold(),
                ),
            )
        ]
        candidate_records.append(record)

    revision_date = str(revision_manifest().get("revision_date") or date.today())
    listed_exposures = sorted(
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
        "selected_exposures": listed_exposures,
        "catalog": "combined",
        "revision_aware": True,
        "rescreen_saved_contexts": True,
        "source_records_digest": source_digest,
        "listed_exposures": len(listed_exposures),
        "cancer_types": list(CANCER_QUERIES),
        "saved_contexts_omitted": 0,
        "redundant_parent_contexts_omitted": 0,
        "redundant_parent_contexts": [],
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
    print(json.dumps({key: value for key, value in packet.items() if key not in {
        "articles", "reclassification_source_records"
    }}, indent=2))
    return packet


def discover(
    start_date: str | None,
    end_date: str,
    output_dir: Path,
    force: bool = False,
    selected_exposures: list[str] | None = None,
    rescreen_saved_contexts: bool = False,
    catalog: str = "current",
    revision_aware: bool = False,
) -> dict[str, Any]:
    require_candidate_generation_ready(output_dir)
    terms_by_exposure = selected_exposure_terms(
        selected_exposures,
        catalog=catalog,
        revision_aware=revision_aware,
    )
    if revision_aware:
        # Expanded aliases and new atomic exposures require an all-history
        # rescreen even when the PMID/exposure context was saved previously.
        rescreen_saved_contexts = True
    vocabulary_digest = exposure_vocabulary_digest(terms_by_exposure)
    exposure_filter = list(terms_by_exposure) if selected_exposures else None
    searched_exposures = list(terms_by_exposure)
    searched_context_manifest = [
        {"exposure": exposure, "cancer": cancer, "outcome": "Incidence"}
        for exposure in searched_exposures
        for cancer in CANCER_QUERIES
    ]
    candidate_path = output_dir / "candidates.json"
    existing = load_json(candidate_path, {})
    if (
        not force
        and isinstance(existing.get("articles"), list)
        and "publication_date_start" in existing
        and existing.get("publication_date_start") == start_date
        and existing.get("publication_date_end") == end_date
        and existing.get("selected_exposures") == exposure_filter
        and existing.get("catalog", "current") == catalog
        and bool(existing.get("revision_aware", False)) == bool(revision_aware)
        and existing.get("vocabulary_digest") == vocabulary_digest
        and bool(existing.get("rescreen_saved_contexts", False))
        == bool(rescreen_saved_contexts)
        and (
            not revision_aware
            or (
                existing.get("searched_exposures") == searched_exposures
                and existing.get("searched_contexts") == searched_context_manifest
                and existing.get("discovery_complete") is True
            )
        )
    ):
        window = publication_window_text(start_date, end_date)
        print(f"Reusing immutable candidate packet for {window}; use --force to rediscover.", flush=True)
        return existing
    api_key_status = configure_entrez()
    known_contexts = set() if rescreen_saved_contexts else saved_contexts()
    contexts_by_pmid: dict[str, set[tuple[str, str]]] = defaultdict(set)
    queries = 0
    esearch_requests = 0
    search_completion: list[dict[str, Any]] = []
    total_contexts = len(terms_by_exposure) * len(CANCER_QUERIES)
    started = time.time()

    search_jobs = [
        (exposure, cancer, build_query(terms, cancer_query))
        for exposure, terms in terms_by_exposure.items()
        for cancer, cancer_query in CANCER_QUERIES.items()
    ]

    def run_search(
        job: tuple[str, str, str]
    ) -> tuple[str, str, str, EntrezSearchIds]:
        exposure, cancer, query = job
        return exposure, cancer, query, entrez_search(query, start_date, end_date)

    # Five workers stay below NCBI's keyed 10-request/second allowance while
    # avoiding hundreds of sequential network round trips.
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(run_search, job) for job in search_jobs]
        for future in concurrent.futures.as_completed(futures):
            exposure, cancer, query, found_pmids = future.result()
            audit = getattr(found_pmids, "audit", None)
            if not isinstance(audit, dict) or audit.get("complete") is not True:
                raise RuntimeError(
                    f"NCBI ESearch completeness audit is missing for {exposure}/{cancer}"
                )
            esearch_requests += int(audit.get("request_count") or 0)
            search_completion.append(
                {
                    "exposure": exposure,
                    "cancer": cancer,
                    "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                    **audit,
                }
            )
            for pmid in found_pmids:
                context = (
                    str(pmid),
                    safe_context_part(exposure),
                    safe_context_part(cancer),
                )
                if context not in known_contexts:
                    contexts_by_pmid[pmid].add((exposure, cancer))
            queries += 1
            if queries % 75 == 0 or queries == total_contexts:
                print(
                    f"Discovery progress: {queries}/{total_contexts} exposure-cancer searches; "
                    f"{len(contexts_by_pmid)} unique unsaved PMIDs",
                    flush=True,
                )

    redundant_parent_contexts = dedupe_parent_search_contexts(contexts_by_pmid)
    pmids = sorted(
        (pmid for pmid, contexts in contexts_by_pmid.items() if contexts),
        key=lambda value: int(value),
    )
    articles, fetch_requests = entrez_fetch(pmids)
    missing_pmids = sorted(set(pmids).difference(articles))
    if missing_pmids:
        raise RuntimeError(
            "NCBI EFetch omitted requested PMIDs; no discovery packet was written: "
            + ", ".join(missing_pmids)
        )
    candidate_records = []
    out_of_window = []
    for pmid in pmids:
        record = dict(articles[pmid])
        record["candidate_contexts"] = [
            {"exposure": exposure, "cancer": cancer, "outcome": "Incidence"}
            for exposure, cancer in sorted(contexts_by_pmid[pmid])
        ]
        selected_date = select_in_window_publication_date(record, start_date, end_date)
        if selected_date is None:
            out_of_window.append(record)
            continue
        record["published_date"] = selected_date
        candidate_records.append(record)

    packet = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "publication_date_start": start_date,
        "publication_date_end": end_date,
        "selected_exposures": exposure_filter,
        "catalog": catalog,
        "revision_aware": bool(revision_aware),
        "vocabulary_digest": vocabulary_digest,
        "rescreen_saved_contexts": bool(rescreen_saved_contexts),
        "searched_exposures": searched_exposures,
        "searched_contexts": searched_context_manifest,
        "discovery_complete": queries == total_contexts,
        "search_completion": sorted(
            search_completion,
            key=lambda item: (item["exposure"].casefold(), item["cancer"].casefold()),
        ),
        "listed_exposures": len(terms_by_exposure),
        "cancer_types": list(CANCER_QUERIES),
        "saved_contexts_omitted": len(known_contexts),
        "redundant_parent_contexts_omitted": len(redundant_parent_contexts),
        "redundant_parent_contexts": redundant_parent_contexts,
        "ncbi_api_key_status": api_key_status,
        "ncbi_requests": {
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
    require_candidate_generation_ready(output_dir)
    configure_entrez()
    path = output_dir / "candidates.json"
    packet = load_json(path, {})
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
    require_candidate_generation_ready(output_dir)
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
        "MetaVir new-study report",
        "===========================",
        "Publication-date search window: "
        + publication_window_text(
            candidates.get("publication_date_start"),
            candidates.get("publication_date_end"),
        ),
        "Cancer type: Prostate cancer",
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
        "MetaVir new-study exclusion ledger",
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
    # Older PMC backfiles can contain a complete OCR transcript in one JATS
    # ``preformat`` node instead of paragraph tags.  Preserve that official
    # body text; the strict receipt validator still enforces article identity,
    # direct-body structure, and the fixed completeness floor.
    allowed = {
        "article-title",
        "title",
        "p",
        "th",
        "td",
        "caption",
        "preformat",
    }
    for element in root.iter():
        if local_name(element.tag) not in allowed:
            continue
        value = one_line(" ".join(element.itertext()))
        if value and (not blocks or value != blocks[-1]):
            blocks.append(value)
    return "\n".join(blocks)


def fetch_pmc_full_text(pmcid: str) -> bytes:
    last_error: Exception | None = None
    public_retry_after_key_rejection = False
    # Four ordinary attempts plus one reserved slot for the sole public retry
    # that may follow a keyed HTTP 400.
    for attempt in range(5):
        attempted_api_key = str(Entrez.api_key or "") or None
        try:
            wait_for_ncbi_request_slot()
            with Entrez.efetch(db="pmc", id=pmcid, retmode="xml") as handle:
                return handle.read()
        except Exception as exc:  # pragma: no cover - network retry path
            last_error = exc
            if reject_invalid_ncbi_api_key(exc, attempted_api_key):
                public_retry_after_key_rejection = True
                continue
            if public_retry_after_key_rejection:
                break
            if not retryable_entrez_error(exc) or attempt >= 3:
                break
            time.sleep(2 ** attempt)
    raise RuntimeError(f"PMC EFetch failed for {pmcid}: {last_error}")


def is_exact_pmc_repair_scope(output_dir: Path) -> bool:
    """Identify the one immutable MetaVir generation that requires PMC repair."""
    candidate_path = output_dir / "candidates.json"
    second_path = output_dir / "chatgpt_second_screen.json"
    return (
        candidate_path.is_file()
        and second_path.is_file()
        and file_sha256(candidate_path)
        == audit_blocked_pmc_sources.EXPECTED_CANDIDATE_SHA256
        and file_sha256(second_path)
        == audit_blocked_pmc_sources.EXPECTED_SECOND_SHA256
    )


def validated_pmc_repair_sources(output_dir: Path) -> dict[str, dict[str, Any]]:
    """Load the exact all-or-none repair staging after revalidating every receipt."""
    staging = output_dir / audit_blocked_pmc_sources.STAGING_DIRNAME
    exact_scope = is_exact_pmc_repair_scope(output_dir)
    if not staging.exists():
        if exact_scope:
            raise RuntimeError(
                "Exact MetaVir R3 repair scope lacks its all-or-none PMC staging seal"
            )
        return {}
    if not exact_scope:
        raise RuntimeError("PMC repair staging exists outside its exact sealed scope")
    if not staging.is_dir() or staging.is_symlink():
        raise RuntimeError("PMC repair staging is not a regular directory")
    ledger_path = output_dir / audit_blocked_pmc_sources.LEDGER_FILENAME
    if not ledger_path.is_file():
        raise RuntimeError("PMC repair staging exists without its retrieval ledger")
    ledger = load_json(ledger_path, {})
    validation_path = (
        output_dir / audit_blocked_pmc_sources.LEDGER_VALIDATION_FILENAME
    )
    validation = load_json(validation_path, {})
    ledger_sha256 = file_sha256(ledger_path)
    if (
        validation.get("status") != "validated"
        or validation.get("all_sources_complete") is not True
        or validation.get("production_block_checked") is not True
        or validation.get("validated_source_count")
        != len(audit_blocked_pmc_sources.EXPECTED_BLOCKERS)
        or validation.get("unresolved_source_count") != 0
        or validation.get("decision") != "all_sources_validated_stage_ready"
        or validation.get("ledger_sha256") != ledger_sha256
    ):
        raise RuntimeError("PMC repair ledger lacks its exact completed validation seal")
    audit_blocked_pmc_sources.validate_audit_ledger(
        output_dir,
        ledger,
        require_blocked_production=False,
    )
    if ledger.get("all_sources_complete") is not True:
        raise RuntimeError("PMC repair staging is not an exact complete source set")
    manifest = load_json(staging / "manifest.json", {})
    manifest_sources = manifest.get("sources") or {}
    sources: dict[str, dict[str, Any]] = {}
    for pmcid, entry in sorted(manifest_sources.items()):
        text_path = staging / f"{pmcid}.txt"
        text = text_path.read_text(encoding="utf-8").rstrip("\n")
        sources[pmcid] = {
            "text": text,
            "source_format": str(entry.get("source_format") or ""),
            "source_route": str(entry.get("route") or ""),
            "raw_sha256": str(entry.get("raw_sha256") or ""),
            "validation_digest": audit_blocked_pmc_sources.stable_json_digest(
                entry.get("validation") or {}
            ),
            "receipt_ledger_sha256": ledger_sha256,
        }
    if set(sources) != set(audit_blocked_pmc_sources.EXPECTED_BLOCKERS):
        raise RuntimeError("PMC repair source set changed after ledger validation")
    return sources


def require_exact_pmc_repair_manifest(
    manifest: dict[str, Any],
    repair_sources: dict[str, dict[str, Any]],
) -> None:
    """Require every bounded blocker to retain its sealed receipt provenance."""
    expected_sources = {
        pmcid: {
            **source,
            "status": "full_text",
            "error": "",
            "prevalidated": True,
        }
        for pmcid, source in repair_sources.items()
    }
    expected_manifest = dual_screening.full_text_source_manifest(
        expected_sources,
        audit_blocked_pmc_sources.EXPECTED_BLOCKERS,
    )
    for pmcid in audit_blocked_pmc_sources.EXPECTED_BLOCKERS:
        if manifest.get(pmcid) != expected_manifest.get(pmcid):
            raise dual_screening.ScreeningValidationError(
                f"Exact MetaVir R3 packet lacks sealed PMC repair provenance: {pmcid}"
            )


def require_exact_pmc_repair_packet(
    output_dir: Path,
    full_text_packet: dict[str, Any],
) -> None:
    """Enforce the bounded repair seal at every independently callable stage."""
    if not is_exact_pmc_repair_scope(output_dir):
        return
    repair_sources = validated_pmc_repair_sources(output_dir)
    require_exact_pmc_repair_manifest(
        full_text_packet.get("source_manifest") or {},
        repair_sources,
    )


# ChatGPT is the only active track while Cornell DeepSeek V4 access is pending.
# The dormant validation track remains represented in durable audit metadata;
# provider-specific calls and validation live in dual_screening.py.


def _runtime_stub(name: str) -> dual_screening.TrackRuntime:
    # Standalone resume/finalize invocations must use the exact production
    # contract even when no API client is needed.
    load_dotenv(REPO_ROOT / "mykey.env", override=True)
    if name == "chatgpt":
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
    require_candidate_generation_ready(output_dir)
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
    require_candidate_generation_ready(output_dir)
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
    require_candidate_generation_ready(output_dir)
    exact_repair_scope = is_exact_pmc_repair_scope(output_dir)
    if exact_repair_scope and (fetcher is not None or parser is not None):
        raise RuntimeError(
            "Exact MetaVir R3 repair scope forbids custom PMC fetchers or parsers"
        )
    repair_sources = (
        validated_pmc_repair_sources(output_dir)
        if fetcher is None and parser is None
        else {}
    )
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
            contexts = {
                str(item.get("context_id") or ""): item
                for item in second_packets[name].get("results", [])
                if dual_screening.is_full_text_candidate(item)
            }
            manual_adjudications = (
                dual_screening.load_full_text_manual_adjudications(
                    output_dir,
                    contexts,
                )
            )
            try:
                dual_screening.validate_full_text_packet_lineage(
                    second_packets[name],
                    existing,
                    name,
                    expected_runtimes[name].full_text_model,
                    manual_adjudications=manual_adjudications,
                )
                if exact_repair_scope:
                    require_exact_pmc_repair_manifest(
                        existing.get("source_manifest") or {},
                        repair_sources,
                    )
            except dual_screening.ScreeningValidationError:
                continue
            existing_results = {
                item["context_id"]: item for item in existing.get("results", [])
            }
            dual_screening.reconcile_completed_problem_ledger(
                output_dir,
                existing_results,
                track=name,
                stage="full_text",
            )
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
        dual_screening.is_full_text_candidate(item)
        for name, packet in second_packets.items()
        if name in missing_tracks
        for item in packet.get("results", [])
    )
    if pmc_required and fetcher is None:
        try:
            configure_entrez()
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
        prevalidated_sources=repair_sources,
        required_prevalidated_pmcids=(
            set(audit_blocked_pmc_sources.EXPECTED_BLOCKERS)
            if exact_repair_scope
            else None
        ),
    )

    configured: dict[str, dual_screening.TrackRuntime] = dict(expected_runtimes)
    if runtimes is None:
        for name in missing_tracks:
            track_needs_model_call = any(
                dual_screening.is_full_text_candidate(item)
                and sources.get(str(item.get("pmcid") or ""), {}).get("status")
                == "full_text"
                for item in second_packets[name].get("results", [])
            )
            if track_needs_model_call:
                configured[name] = dual_screening.configure_runtime(name, REPO_ROOT)

    try:
        for name in missing_tracks:
            packets[name] = dual_screening.run_full_text_track(
                output_dir,
                second_packets[name],
                sources,
                configured[name],
                force=force,
            )
    except dual_screening.ScreeningValidationError:
        # Never leave an older convenience/canonical packet looking current when
        # the exact gated PMC retrieval or Luna stage is blocked.
        (output_dir / "full_text_review.json").unlink(missing_ok=True)
        raise
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
        "MetaVir monthly evidence update — canonical ChatGPT disposition",
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
        "MetaVir canonical exclusion ledger",
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


RECLASSIFICATION_WORDING_BOUNDARIES = {
    "fish oil": (
        re.compile(r"\bfish[\s-]+oils?\b", flags=re.IGNORECASE),
        {"source"},
        (
            "The article does not use explicit fish-oil wording; DHA-, EPA-, or "
            "omega-3-only evidence cannot remain classified as Fish oil."
        ),
    ),
    "tomato": (
        re.compile(r"\btomato(?:es)?\b", flags=re.IGNORECASE),
        {"source"},
        (
            "The article does not mention tomato wording; lycopene-only evidence "
            "cannot remain classified as Tomato."
        ),
    ),
    "omega-3 fatty acids": (
        re.compile(r"\b(?:omega[\s-]*3|n[\s-]*3)\b", flags=re.IGNORECASE),
        {"source", "approved_target"},
        (
            "The article does not use omega-3 or n-3 family wording; an EPA- or "
            "DHA-only record belongs to its atomic exposure and reaches Omega-3 "
            "fatty acids only through post-screen hierarchy rollup."
        ),
    ),
    "eicosapentaenoic acid": (
        re.compile(r"\beicosapentaenoic\b|\bEPA\b", flags=re.IGNORECASE),
        {"approved_target"},
        "The article does not mention EPA or eicosapentaenoic acid.",
    ),
    "docosahexaenoic acid": (
        re.compile(r"\bdocosahexaenoic\b|\bDHA\b", flags=re.IGNORECASE),
        {"approved_target"},
        "The article does not mention DHA or docosahexaenoic acid.",
    ),
    "lycopene": (
        re.compile(r"\blycopene\b|\blyc-o-mato\b", flags=re.IGNORECASE),
        {"approved_target"},
        "The article does not mention lycopene.",
    ),
}


def _article_wording_text(article: dict[str, Any]) -> str:
    mesh = article.get("mesh") or []
    return " ".join(
        [
            str(article.get("title") or ""),
            str(article.get("abstract") or ""),
            " ".join(
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else str(value)
                for value in mesh
            ),
        ]
    ).translate(str.maketrans({"–": "-", "—": "-", "−": "-"}))


def _screened_wording_text(
    article: dict[str, Any], result: dict[str, Any]
) -> str:
    """Combine PubMed text with source-validated extraction excerpts."""
    extraction = result.get("extraction") or {}
    evidence = extraction.get("evidence") or {}
    excerpts = [
        str(entry.get("excerpt") or "")
        for entry in evidence.values()
        if isinstance(entry, dict)
    ]
    return " ".join(
        [
            _article_wording_text(article),
            str(extraction.get("comparison_type") or ""),
            str(extraction.get("exposure_subtype") or ""),
            *excerpts,
        ]
    ).translate(str.maketrans({"–": "-", "—": "-", "−": "-"}))


def apply_reclassification_wording_boundaries(
    candidates: dict[str, Any],
    final_packet: dict[str, Any],
) -> dict[str, Any]:
    """Deterministically enforce the narrowed source definitions.

    Luna and Sol still screen every approved source and target context.  This
    final guard prevents a model from reaffirming the exact legacy leakage the
    revision was created to repair: EPA/DHA/omega-3 wording is not Fish oil,
    and lycopene wording is not Tomato unless the article actually says tomato.
    """
    if candidates.get("run_mode") != "included_reclassification":
        return final_packet
    article_by_pmid = {
        str(article.get("pmid") or ""): article
        for article in candidates.get("articles", [])
    }
    roles_by_context = {
        dual_screening.context_id(
            str(article.get("pmid") or ""),
            str(context.get("exposure") or ""),
            str(context.get("cancer") or ""),
        ): set(context.get("reclassification_roles") or [])
        for article in candidates.get("articles", [])
        for context in article.get("candidate_contexts", [])
    }
    guarded = dict(final_packet)
    guarded_results = []
    for result in final_packet.get("results", []):
        item = dict(result)
        cid = str(item.get("context_id") or "")
        exposure_key = str(item.get("exposure") or "").casefold()
        boundary = RECLASSIFICATION_WORDING_BOUNDARIES.get(exposure_key)
        if boundary:
            pattern, guarded_roles, reason = boundary
            article = article_by_pmid.get(str(item.get("pmid") or ""), {})
            if roles_by_context.get(cid, set()).intersection(
                guarded_roles
            ) and not pattern.search(_screened_wording_text(article, item)):
                item["status_before_reclassification_wording_guard"] = item.get(
                    "final_status"
                )
                item["reason_before_reclassification_wording_guard"] = item.get(
                    "reason"
                )
                item.update(
                    {
                        "final_status": "EXCLUDE",
                        "full_text_status": "EXCLUDE",
                        "screen_layer": "deterministic_reclassification_wording",
                        "exclusion_code": "S2-EXPOSURE-MISMATCH",
                        "reason": reason,
                        "extraction": None,
                        "jbi": None,
                    }
                )
        guarded_results.append(item)
    guarded["results"] = guarded_results
    guarded["counts"] = dict(
        Counter(item.get("final_status") for item in guarded_results)
    )
    return guarded


def finalize_evidence(output_dir: Path, report_dir: Path) -> dict[str, Any]:
    require_candidate_generation_ready(output_dir)
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
        if name == "chatgpt":
            require_exact_pmc_repair_packet(output_dir, full_text)
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
        final_packets[name] = apply_reclassification_wording_boundaries(
            candidates, final_packets[name]
        )
        final_packets[name].update(lineage)
        save_json(output_dir / f"{name}_final_screening.json", final_packets[name])

    canonical = dict(final_packets["chatgpt"])
    for item in canonical.get("results", []):
        item["direct_exposure"] = item.get("exposure")
        item["inherited_exposures"] = inherited_exposures(str(item.get("exposure") or ""))
    canonical["canonical_track"] = "chatgpt"
    canonical["validation_status"] = (
        "completed"
        if "deepseek" in final_packets
        else dual_screening.DEEPSEEK_VALIDATION_STATUS
    )
    save_json(output_dir / "final_screening.json", canonical)
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


def stable_effect_id(item: dict[str, Any]) -> str:
    """Identify one direct screened estimate independently of parent roll-ups."""
    extraction = item.get("extraction") or {}
    payload = {
        "pmid": str(item.get("pmid") or ""),
        "direct_exposure": str(item.get("direct_exposure") or item.get("exposure") or ""),
        "cancer": str(item.get("cancer") or ""),
        "outcome": str(item.get("outcome") or "Incidence"),
        "effect_type": extraction.get("effect_type"),
        "effect_size": extraction.get("effect_size"),
        "comparison_type": extraction.get("comparison_type"),
        "exposure_subtype": extraction.get("exposure_subtype"),
        "exposure_measurement_type": extraction.get("exposure_measurement_type_original", extraction.get("exposure_measurement_type")),
    }
    material = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def study_row(item: dict[str, Any]) -> dict[str, Any]:
    from scripts.display_eligibility import eligible_fair, eligible_fulltext_display, display_row
    if eligible_fair(item) or eligible_fulltext_display(item) or item.get('provisional_ui_display_only') is True:
        return dietary_policy.apply_row(display_row(item), item.get("exposure"))
    extraction = dual_screening.apply_poolability_policy(dict(item.get("extraction") or {}))
    evidence = extraction.get("evidence") or {}
    jbi = item.get("jbi") or {}
    authors = item.get("authors") or []
    first_author = authors[0] if authors else "Unknown"
    year_match = re.search(r"\b(19|20)\d{2}\b", str(item.get("published_date") or ""))
    effect = extraction.get("effect_size")
    lower = extraction.get("ci_lower")
    upper = extraction.get("ci_upper")
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
    baseline_risk = (
        meta_analysis.baseline_risk_for_disease(item.get("subcategory") or item.get("cancer"))
        if cases is None and total_n is not None
        else None
    )
    estimated_cases = (
        int(round(float(total_n) * baseline_risk))
        if baseline_risk is not None
        else None
    )
    row = {
        "Effect ID": stable_effect_id(item),
        "Direct Exposure": item.get("direct_exposure") or item.get("exposure"),
        "Displayed Exposure": item.get("exposure"),
        "Hierarchy Inherited": bool(item.get("hierarchy_inherited")),
        "pool_in_parent_meta_analysis": bool(
            item.get(
                "pool_in_parent_meta_analysis",
                not bool(item.get("hierarchy_inherited")),
            )
        ),
        "Study": f"{first_author} et al. ({year_match.group(0) if year_match else 'Unknown'}) [PMID: {item.get('pmid')}]",
        "PMID": str(item.get("pmid")),
        "PMCID": str(item.get("pmcid") or ""),
        "Effect Size": effect,
        "Lower CI": lower,
        "Upper CI": upper,
        "P Value": p_value,
        "Population": extraction.get("population") or "Human study population described in the cited article",
        "Location": extraction.get("location") or "NA",
        "Ethnicity": extraction.get("ethnicity") or "NA",
        "Ethnicity Basis": extraction.get("ethnicity_basis") or "NA",
        "Ethnicity Supporting Text": extraction.get("ethnicity_supporting_text") or "NA",
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
        "effect_size_imputed": extraction.get("effect_size_imputed", False),
        "effect_size_imputation_method": extraction.get("effect_size_imputation_method"),
        "ci_level_assumed": extraction.get("ci_level_assumed", False),
        "effect_is_adjusted": extraction.get("effect_is_adjusted", False),
        "reported_effect_type": extraction.get("reported_effect_type"),
        "poolability_policy_version": extraction.get("poolability_policy_version"),
        "direction_imputed": extraction.get("direction_imputed", False),
        "direction_imputation_reason": extraction.get("direction_imputation_reason", ""),
        "direction_original_comparison": extraction.get("direction_original_comparison"),
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
            "effect_direction": (
                extraction.get("direction_imputation_reason")
                if extraction.get("direction_imputed") else excerpt("comparison")
            ),
            "p_value": excerpt("p_value") or p_value_support,
            "confidence_interval": ci_support,
            "outcome_definition": f"Incident {item.get('cancer')}",
            "exposure_definition": excerpt("comparison") or one_line(extraction.get("comparison_type")),
            "design": excerpt("design"),
            "timing": excerpt("timing"),
            "continent": excerpt("continent"),
            "ethnicity": excerpt("ethnicity"),
            "location": excerpt("location"),
        },
    }

    for field in ("key_comparison", "key_comparison_p_resolution"):
        if field in item:
            row[field] = item[field]
    from scripts.provisional_ui_overlay import decorate_row
    row = decorate_row(row,item)
    if extraction.get("astra_fulltext_provenance"):
        source = extraction["astra_fulltext_provenance"].get("source") or {}
        row["PMCID"] = source.get("pmcid") or row.get("PMCID")
        row["evidence_excerpt_source"] = source.get("url") or row.get("evidence_excerpt_source")
        row["evidence_source_scope"] = "PMC_FULL_TEXT_TEXT_ONLY"
    for field in dietary_policy.AUDIT_FIELDS:
        if field in extraction:
            row[field] = extraction[field]
    return dietary_policy.apply_row(row, item.get("exposure"))


def _validate_current_chatgpt_lineage(
    output_dir: Path,
    candidates: dict[str, Any],
    final_packet: dict[str, Any],
) -> dict[str, Any]:
    """Require exact current Luna -> Sol -> conditional Luna provenance."""
    first = load_json(output_dir / "chatgpt_first_screen.json", {})
    second = load_json(output_dir / "chatgpt_second_screen.json", {})
    full_text = load_json(output_dir / "chatgpt_full_text_review.json", {})
    if not first or not second or not full_text:
        raise RuntimeError(
            "Apply requires complete chatgpt_first_screen, chatgpt_second_screen, "
            "and chatgpt_full_text_review packets."
        )
    expected_models = {
        "first": dual_screening.CHATGPT_FIRST_MODEL,
        "second": dual_screening.CHATGPT_SECOND_MODEL,
        "full_text": dual_screening.CHATGPT_FULL_TEXT_MODEL,
    }
    try:
        require_exact_pmc_repair_packet(output_dir, full_text)
        lineage = dual_screening.validate_track_lineage(
            candidates,
            first,
            second,
            full_text,
            "chatgpt",
            expected_models,
        )
        expected_final = dual_screening.finalize_track(
            candidates,
            first,
            second,
            full_text,
            "chatgpt",
        )
    except (ValueError, RuntimeError) as exc:
        raise RuntimeError(f"ChatGPT screening lineage validation failed: {exc}") from exc
    expected_final = apply_reclassification_wording_boundaries(
        candidates, expected_final
    )
    for item in expected_final.get("results", []):
        item["direct_exposure"] = item.get("exposure")
        item["inherited_exposures"] = inherited_exposures(
            str(item.get("exposure") or "")
        )
    if final_packet.get("canonical_track") != "chatgpt":
        raise RuntimeError("Apply requires the canonical ChatGPT final packet")
    if final_packet.get("protocol_version") != lineage["protocol_version"]:
        raise RuntimeError("Final screening protocol does not match current stage lineage")
    if final_packet.get("screening_models") != lineage["screening_models"]:
        raise RuntimeError("Final screening models do not match current stage lineage")
    if final_packet.get("screening_input_digests") != lineage[
        "screening_input_digests"
    ]:
        raise RuntimeError("Final screening digests do not match current stage lineage")
    if _canonical_digest(final_packet.get("results", [])) != _canonical_digest(
        expected_final.get("results", [])
    ):
        raise RuntimeError(
            "Final screening results are stale or differ from the exact current "
            "candidate-to-Luna-to-Sol-to-conditional-Luna lineage."
        )
    return lineage


def _validate_current_reclassification_scope(candidates: dict[str, Any]) -> None:
    """Bind included-only apply to the source union and approved targets now on disk."""
    if candidates.get("run_mode") != "included_reclassification":
        raise RuntimeError("Candidate packet is not an included-only reclassification run")
    current_records = included_reclassification_records()
    current_targets = reclassification_targets()
    expected_digest = _reclassification_source_digest(current_records, current_targets)
    if candidates.get("source_records_digest") != expected_digest:
        raise RuntimeError(
            "Reclassification source caches or approved targets changed after discovery; "
            "rediscover and rescreen before apply."
        )
    if _canonical_digest(candidates.get("reclassification_source_records")) != (
        _canonical_digest(current_records)
    ):
        raise RuntimeError("Reclassification source-record scope is stale")
    if candidates.get("reclassification_targets") != current_targets:
        raise RuntimeError("Reclassification approved-target scope is stale")
    if candidates.get("revision_aware") is not True or candidates.get(
        "rescreen_saved_contexts"
    ) is not True:
        raise RuntimeError("Reclassification candidates must request a complete rescreen")
    if candidates.get("publication_date_start") is not None or candidates.get(
        "publication_date_end"
    ) != str(revision_manifest().get("revision_date") or ""):
        raise RuntimeError("Reclassification revision-date scope is stale")
    if candidates.get("catalog") != "combined" or candidates.get(
        "cancer_types"
    ) != list(CANCER_QUERIES):
        raise RuntimeError("Reclassification catalog/cancer scope is stale")
    if candidates.get("saved_contexts_omitted") != 0:
        raise RuntimeError("Reclassification candidates may not omit saved source contexts")
    requests = candidates.get("ncbi_requests") or {}
    if requests.get("esearch") != 0:
        raise RuntimeError("Included-only reclassification must never issue NCBI ESearch")
    if requests.get("total") != requests.get("efetch"):
        raise RuntimeError("Reclassification NCBI request accounting is inconsistent")

    expected_roles = _reclassification_context_roles(current_records, current_targets)
    expected_pmids = set(expected_roles)
    articles = candidates.get("articles")
    if not isinstance(articles, list):
        raise RuntimeError("Reclassification candidate articles must be an array")
    actual_pmids = [str(article.get("pmid") or "") for article in articles]
    if len(actual_pmids) != len(set(actual_pmids)) or set(actual_pmids) != expected_pmids:
        raise RuntimeError("Reclassification candidate PMIDs do not match current source caches")
    for article in articles:
        pmid = str(article.get("pmid") or "")
        expected_contexts = [
            _serialise_reclassification_context(exposure, cancer, detail)
            for (exposure, cancer), detail in sorted(
                expected_roles[pmid].items(),
                key=lambda item: (item[0][0].casefold(), item[0][1].casefold()),
            )
        ]
        if article.get("candidate_contexts") != expected_contexts:
            raise RuntimeError(
                f"Reclassification roles/contexts are stale for PMID {pmid}"
            )
    expected_exposures = sorted(
        {
            exposure
            for contexts in expected_roles.values()
            for exposure, _cancer in contexts
        },
        key=str.casefold,
    )
    if candidates.get("selected_exposures") != expected_exposures:
        raise RuntimeError("Reclassification selected exposure scope is stale")
    if candidates.get("listed_exposures") != len(expected_exposures):
        raise RuntimeError("Reclassification listed exposure count is inconsistent")
    if candidates.get("included_source_effects") != len(current_records):
        raise RuntimeError("Reclassification source-effect count is inconsistent")
    if candidates.get("candidate_pmids") != len(expected_pmids):
        raise RuntimeError("Reclassification candidate PMID count is inconsistent")
    expected_context_count = sum(len(contexts) for contexts in expected_roles.values())
    if candidates.get("candidate_contexts") != expected_context_count:
        raise RuntimeError("Reclassification candidate context count is inconsistent")


def require_current_reclassification_apply(output_dir: Path) -> dict[str, Any]:
    """Validate every durable input before any reclassification mutation."""
    candidates = load_json(output_dir / "candidates.json", {})
    final_packet = load_json(output_dir / "final_screening.json", {})
    lineage = _validate_current_chatgpt_lineage(
        output_dir, candidates, final_packet
    )
    _validate_current_reclassification_scope(candidates)
    return lineage


def _validate_current_revision_search_scope(
    candidates: dict[str, Any],
    *,
    exact_terms: dict[str, list[str]] | None = None,
) -> set[tuple[str, str]]:
    """Return searched contexts after validating an exact vocabulary generation."""
    if candidates.get("run_mode") == "included_reclassification":
        raise RuntimeError("Included-only reclassification is not an NCBI revision search")
    if candidates.get("revision_aware") is not True:
        raise RuntimeError("Authoritative apply requires revision_aware candidates")
    if candidates.get("publication_date_start") is not None:
        raise RuntimeError("Revision-aware apply requires an all-history candidate packet")
    if candidates.get("publication_date_end") != str(
        revision_manifest().get("revision_date") or ""
    ):
        raise RuntimeError("Revision-aware all-history cutoff is stale")
    if candidates.get("rescreen_saved_contexts") is not True:
        raise RuntimeError("Revision-aware apply may not omit saved contexts")
    if candidates.get("saved_contexts_omitted") != 0:
        raise RuntimeError("Revision-aware discovery reported omitted saved contexts")
    if candidates.get("discovery_complete") is not True:
        raise RuntimeError("Revision-aware NCBI discovery is not marked complete")
    selected = candidates.get("selected_exposures")
    if selected is not None and not isinstance(selected, list):
        raise RuntimeError("Revision-aware selected_exposures must be null or an array")
    if exact_terms is None:
        try:
            terms = selected_exposure_terms(
                selected,
                catalog=str(candidates.get("catalog") or "current"),
                revision_aware=True,
            )
        except ValueError as exc:
            raise RuntimeError(f"Revision-aware exposure scope is stale: {exc}") from exc
    else:
        terms = {
            str(exposure): [str(term) for term in values]
            for exposure, values in exact_terms.items()
        }
        if selected is not None and selected != list(terms):
            raise RuntimeError(
                "Revision-aware selected exposures do not match the supplied exact vocabulary"
            )
    expected_exposures = list(terms)
    expected_manifest = [
        {"exposure": exposure, "cancer": cancer, "outcome": "Incidence"}
        for exposure in expected_exposures
        for cancer in CANCER_QUERIES
    ]
    if candidates.get("searched_exposures") != expected_exposures:
        raise RuntimeError("Revision-aware searched exposure scope is stale")
    if candidates.get("searched_contexts") != expected_manifest:
        raise RuntimeError("Revision-aware searched exposure/cancer scope is stale")
    if candidates.get("vocabulary_digest") != exposure_vocabulary_digest(terms):
        raise RuntimeError("Revision-aware search vocabulary changed after discovery")
    if candidates.get("listed_exposures") != len(expected_exposures):
        raise RuntimeError("Revision-aware listed exposure count is inconsistent")
    requests = candidates.get("ncbi_requests") or {}
    allowed = {
        (item["exposure"], item["cancer"])
        for item in expected_manifest
    }
    completion = candidates.get("search_completion")
    if not isinstance(completion, list) or len(completion) != len(allowed):
        raise RuntimeError("Revision-aware ESearch completion audit is incomplete")
    completion_by_context: dict[tuple[str, str], dict[str, Any]] = {}
    audited_requests = 0
    expected_interval_start = str(
        candidates.get("publication_date_start") or date.min.isoformat()
    )
    expected_interval_end = str(candidates.get("publication_date_end") or "")
    for audit in completion:
        if not isinstance(audit, dict):
            raise RuntimeError("Revision-aware ESearch audit entries must be objects")
        key = (str(audit.get("exposure") or ""), str(audit.get("cancer") or ""))
        if key not in allowed or key in completion_by_context:
            raise RuntimeError("Revision-aware ESearch audit scope is duplicated or stale")
        exposure, cancer = key
        expected_query = build_query(terms[exposure], CANCER_QUERIES[cancer])
        if audit.get("query_sha256") != hashlib.sha256(
            expected_query.encode("utf-8")
        ).hexdigest():
            raise RuntimeError("Revision-aware ESearch query audit is stale")
        reported = audit.get("reported_count")
        returned = audit.get("returned_unique_pmids")
        request_count = audit.get("request_count")
        if (
            audit.get("complete") is not True
            or isinstance(reported, bool)
            or not isinstance(reported, int)
            or reported < 0
            or returned != reported
            or isinstance(request_count, bool)
            or not isinstance(request_count, int)
            or request_count < 1
        ):
            raise RuntimeError("Revision-aware ESearch Count/unique audit is invalid")
        leaves = audit.get("leaf_intervals")
        if not isinstance(leaves, list) or not leaves:
            raise RuntimeError("Revision-aware ESearch leaf audit is missing")
        ordered_leaves = sorted(
            leaves, key=lambda item: str(item.get("publication_date_start") or "")
        )
        next_start = date.fromisoformat(expected_interval_start)
        leaf_total = 0
        for leaf in ordered_leaves:
            if not isinstance(leaf, dict):
                raise RuntimeError("Revision-aware ESearch leaf audit is invalid")
            leaf_start = date.fromisoformat(str(leaf.get("publication_date_start") or ""))
            leaf_end = date.fromisoformat(str(leaf.get("publication_date_end") or ""))
            leaf_count = leaf.get("reported_count")
            if (
                leaf_start != next_start
                or leaf_end < leaf_start
                or leaf.get("complete") is not True
                or isinstance(leaf_count, bool)
                or not isinstance(leaf_count, int)
                or not 0 <= leaf_count <= 10000
                or leaf.get("returned_unique_pmids") != leaf_count
            ):
                raise RuntimeError("Revision-aware ESearch leaf coverage/count is invalid")
            next_start = leaf_end + timedelta(days=1)
            leaf_total += leaf_count
        if (
            ordered_leaves[-1].get("publication_date_end") != expected_interval_end
            or leaf_total != reported
            or request_count < len(ordered_leaves)
        ):
            raise RuntimeError("Revision-aware ESearch leaf totals or coverage are invalid")
        audited_requests += request_count
        completion_by_context[key] = audit
    if set(completion_by_context) != allowed:
        raise RuntimeError("Revision-aware ESearch audit does not cover every search")
    if requests.get("esearch") != audited_requests or requests.get("total") != (
        audited_requests + int(requests.get("efetch") or 0)
    ):
        raise RuntimeError("Revision-aware NCBI request accounting is incomplete")
    articles = candidates.get("articles")
    if not isinstance(articles, list):
        raise RuntimeError("Revision-aware candidate articles must be an array")
    pmids = [str(article.get("pmid") or "") for article in articles]
    if any(not pmid for pmid in pmids) or len(pmids) != len(set(pmids)):
        raise RuntimeError("Revision-aware candidate PMIDs are empty or duplicated")
    article_count = 0
    for article in articles:
        seen_article_contexts: set[tuple[str, str]] = set()
        for context in article.get("candidate_contexts", []):
            key = (str(context.get("exposure") or ""), str(context.get("cancer") or ""))
            if key not in allowed:
                raise RuntimeError(
                    f"Revision candidate includes an unsearched context: {key!r}"
                )
            if key in seen_article_contexts:
                raise RuntimeError(
                    f"Revision candidate duplicates a context for PMID {article.get('pmid')}"
                )
            seen_article_contexts.add(key)
            article_count += 1
    if candidates.get("candidate_contexts") != article_count:
        raise RuntimeError("Revision-aware candidate context count is inconsistent")
    if candidates.get("candidate_pmids") != len(articles):
        raise RuntimeError("Revision-aware candidate PMID count is inconsistent")
    return allowed


def require_current_revision_apply(output_dir: Path) -> set[tuple[str, str]]:
    """Validate exact current screening lineage and all-history search scope."""
    candidates = load_json(output_dir / "candidates.json", {})
    final_packet = load_json(output_dir / "final_screening.json", {})
    _validate_current_chatgpt_lineage(output_dir, candidates, final_packet)
    return _validate_current_revision_search_scope(candidates)


def _numeric_study_frame(studies: list[dict[str, Any]]):
    import pandas as pd

    frame = pd.DataFrame(studies)
    for column in (
        "Effect Size",
        "Lower CI",
        "Upper CI",
        "SE",
        "Cases",
        "Sample Size",
        "Estimated Cases",
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


def _default_screening_stats(final_packet: dict[str, Any], extracted: int) -> dict[str, Any]:
    return {
        "total_fetched": len(final_packet.get("results", [])),
        "after_prefilter": len(final_packet.get("results", [])),
        "llm_screened_in": sum(
            item.get("final_status")
            in {"INCLUDE_META", "ELIGIBLE_NOT_POOLABLE", "ELIGIBLE_SUBTYPE_REVIEW"}
            for item in final_packet.get("results", [])
        ),
        "llm_screened_out": sum(
            item.get("final_status") == "EXCLUDE"
            for item in final_packet.get("results", [])
        ),
        "extracted": extracted,
        "canonical_track": "chatgpt",
    }


def _empty_cache_result(
    final_packet: dict[str, Any],
    source_cache: dict[str, Any],
) -> dict[str, Any]:
    stats = dict(
        source_cache.get("screening_stats")
        or _default_screening_stats(final_packet, 0)
    )
    stats["extracted"] = 0
    stats["canonical_track"] = "chatgpt"
    return {
        "studies": [],
        "summary_html": (
            "<p>No studies remain after the authoritative all-history revision "
            "search and Luna-Sol-Luna screening.</p>"
        ),
        "headline": None,
        "screening_stats": stats,
        "plot_url": None,
        "funnel_plot_url": None,
        "baujat_plot_url": None,
    }


def _transactional_json_writes(
    writes: list[tuple[Path, dict[str, Any], int, bool]],
) -> None:
    """Atomically write a prepared set and restore exact bytes on any failure."""
    originals: dict[Path, bytes | None] = {
        path: path.read_bytes() if path.exists() else None
        for path, _payload, _indent, _ascii in writes
    }
    written: list[Path] = []
    try:
        for path, payload, indent, ensure_ascii in writes:
            # Register before the call so even a failure after atomic replace
            # but before return restores the prior bytes.
            written.append(path)
            save_json(
                path,
                json_compatible(payload),
                indent=indent,
                ensure_ascii=ensure_ascii,
            )
    except Exception:
        for path in reversed(written):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
                continue
            rollback = path.with_suffix(path.suffix + ".rollback.tmp")
            rollback.parent.mkdir(parents=True, exist_ok=True)
            rollback.write_bytes(original)
            rollback.replace(path)
        raise


def _authoritative_removal_record(
    study: dict[str, Any],
    exposure: str,
    cancer: str,
    cache_path: Path,
    new_rows: list[dict[str, Any]],
    final_by_context: dict[str, dict[str, Any]],
    parent_deduplications: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    old_effect_id = str(study.get("Effect ID") or "")
    if old_effect_id and any(
        str(row.get("Effect ID") or "") == old_effect_id for row in new_rows
    ):
        return None
    pmid = str(study.get("PMID") or "")
    direct = str(study.get("Direct Exposure") or exposure)
    replacement = next(
        (
            row
            for row in new_rows
            if str(row.get("PMID") or "") == pmid
            and str(row.get("Direct Exposure") or exposure).casefold()
            == direct.casefold()
        ),
        None,
    )
    cid = dual_screening.context_id(pmid, direct, cancer)
    screened = final_by_context.get(cid)
    deduped = parent_deduplications.get((pmid, exposure, cancer))
    if replacement is not None:
        status = "REPLACED_INCLUDED"
        code = None
        reason = (
            "The prior saved effect row was replaced by the newly screened-in "
            "all-history extraction for the same PMID and direct exposure."
        )
    elif screened is not None:
        status = str(screened.get("final_status") or "NOT_INCLUDED")
        code = screened.get("exclusion_code")
        reason = str(
            screened.get("reason")
            or "The current all-history screening did not retain this saved record."
        )
    elif deduped is not None:
        status = "PARENT_CONTEXT_DEDUPLICATED"
        code = "REVISION-PARENT-DEDUPLICATED"
        reason = str(deduped.get("reason") or "The parent context was deduplicated.")
    else:
        status = "NOT_INCLUDED_AFTER_CURRENT_SEARCH"
        code = "REVISION-NOT-INCLUDED"
        reason = (
            "The PMID/effect was not retained by the current all-history search "
            "and exact Luna-Sol-conditional-Luna screening lineage."
        )
    try:
        cache_label = str(cache_path.relative_to(REPO_ROOT))
    except ValueError:
        cache_label = str(cache_path)
    return {
        "pmid": pmid,
        "cached_exposure": exposure,
        "direct_exposure": direct,
        "cancer": cancer,
        "effect_id": old_effect_id or None,
        "cache": cache_label,
        "status": status,
        "exclusion_code": code,
        "reason": reason,
    }


def _apply_revision_authoritative(
    output_dir: Path,
    candidates: dict[str, Any],
    final_packet: dict[str, Any],
    included: list[dict[str, Any]],
    grouped: dict[tuple[str, str], list[dict[str, Any]]],
    subtype_review: list[dict[str, Any]],
    searched_contexts: set[tuple[str, str]],
) -> dict[str, Any]:
    """Precompute and transactionally apply an all-history revision run.

    Directly searched exposure/cancer caches are authoritative replacements.
    Inherited parents outside the revision search remain merge-only so newly
    screened child evidence cannot erase older, unchanged direct-parent evidence.
    """
    import pandas as pd

    screening_models = dict(final_packet["screening_models"])
    screening_input_digests = dict(final_packet["screening_input_digests"])
    final_by_context = {
        str(item.get("context_id") or ""): item
        for item in final_packet.get("results", [])
    }
    parent_deduplications = {
        (
            str(item.get("pmid") or ""),
            str(item.get("exposure") or ""),
            str(item.get("cancer") or ""),
        ): item
        for item in candidates.get("redundant_parent_contexts", [])
    }
    keys = set(grouped)
    for key in searched_contexts:
        if any(path.exists() for path in _canonical_cache_paths(*key)):
            keys.add(key)

    prepared_cache_writes: list[tuple[Path, dict[str, Any], int, bool]] = []
    changes: list[dict[str, Any]] = []
    all_removed: list[dict[str, Any]] = []
    window_start = final_packet.get("publication_date_start")
    window_end = final_packet.get("publication_date_end")

    for exposure, cancer in sorted(keys, key=lambda item: (item[0].casefold(), item[1].casefold())):
        items = grouped.get((exposure, cancer), [])
        cache_paths = _canonical_cache_paths(exposure, cancer)
        existing_path = next((path for path in cache_paths if path.exists()), None)
        source_cache = (
            _load_required_json_object(existing_path, "Saved cache")
            if existing_path
            else {}
        )
        authoritative = (exposure, cancer) in searched_contexts
        appended: list[str] = []
        refreshed: list[str] = []
        affected_revised_children: set[str] = set()
        replacement_children_by_pmid: dict[str, set[str]] = defaultdict(set)
        for item in items:
            if not item.get("hierarchy_inherited"):
                continue
            replacement_children_by_pmid[
                str(item.get("pmid") or "")
            ].add(str(item.get("direct_exposure") or ""))
        if authoritative:
            by_effect: dict[str, dict[str, Any]] = {}
            for item in items:
                row = study_row(item)
                by_effect[str(row["Effect ID"])] = row
            studies = list(by_effect.values())
        else:
            affected_revised_children = {
                child.casefold()
                for child, child_cancer in searched_contexts
                if child_cancer == cancer and exposure in inherited_exposures(child)
            }
            # Preserve the union of unchanged parent rows from all/core, while
            # replacing only inherited rows whose direct child was in this
            # revision search. This is what protects unchanged Vitamin K direct
            # evidence when K1/K2/K3 are refreshed.
            existing_union: dict[str, dict[str, Any]] = {}
            for path in cache_paths:
                cached_variant = (
                    _load_required_json_object(path, "Saved cache")
                    if path.exists()
                    else {}
                )
                for study in cached_variant.get("studies", []):
                    key = str(study.get("Effect ID") or _canonical_digest(study))
                    existing_union[key] = study
            studies = []
            for study in existing_union.values():
                direct = str(study.get("Direct Exposure") or exposure)
                pmid = str(study.get("PMID") or "")
                if direct.casefold() in affected_revised_children:
                    continue
                if (
                    direct.casefold() == exposure.casefold()
                    and replacement_children_by_pmid.get(pmid)
                ):
                    # The candidate builder uses PMID-level parent/child
                    # deduplication. Apply the same rule to an unchanged parent
                    # cache so a freshly screened child replaces, rather than
                    # duplicates, its old parent-direct row.
                    continue
                studies.append(study)
            index_by_effect = {
                str(study.get("Effect ID") or f"legacy:{study.get('PMID')}"): index
                for index, study in enumerate(studies)
            }
            legacy_direct_by_pmid = {
                str(study.get("PMID")): index
                for index, study in enumerate(studies)
                if not study.get("Effect ID")
            }
            for item in items:
                row = study_row(item)
                effect_id = str(row["Effect ID"])
                pmid = str(item.get("pmid") or "")
                if (
                    effect_id not in index_by_effect
                    and not item.get("hierarchy_inherited")
                    and pmid in legacy_direct_by_pmid
                ):
                    index = legacy_direct_by_pmid.pop(pmid)
                    studies[index] = row
                    index_by_effect[effect_id] = index
                    refreshed.append(pmid)
                elif effect_id in index_by_effect:
                    index = index_by_effect[effect_id]
                    if studies[index] != row:
                        studies[index] = row
                        refreshed.append(pmid)
                else:
                    studies.append(row)
                    index_by_effect[effect_id] = len(studies) - 1
                    appended.append(pmid)

        if studies:
            frame = _numeric_study_frame(studies)
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
                    plot_namespace="chatgpt",
                    outcome="Incidence",
                    exclude_meta=True,
                    df_all=frame,
                    screening_stats=source_cache.get("screening_stats")
                    or _default_screening_stats(final_packet, len(studies)),
                )
            else:
                result = {
                    "success": True,
                    "summary_html": (
                        "<p>Screened child-exposure evidence is listed for "
                        "this parent category but is not pooled in the parent "
                        "meta-analysis.</p>"
                    ),
                    "headline": None,
                    "screening_stats": source_cache.get("screening_stats")
                    or _default_screening_stats(final_packet, len(studies)),
                    "plot_url": None,
                    "funnel_plot_url": None,
                    "baujat_plot_url": None,
                }
            if not result.get("success"):
                raise RuntimeError(
                    f"Meta-analysis rebuild failed for {exposure}/{cancer}: {result}"
                )
            result_fields = {
                "studies": studies,
                **{
                    key: result.get(key)
                    for key in (
                        "summary_html",
                        "headline",
                        "screening_stats",
                        "plot_url",
                        "funnel_plot_url",
                        "baujat_plot_url",
                    )
                },
            }
        else:
            result_fields = _empty_cache_result(final_packet, source_cache)

        paths_to_write = cache_paths if items else [path for path in cache_paths if path.exists()]
        context_removed: list[dict[str, Any]] = []
        for path in paths_to_write:
            path_cache = (
                _load_required_json_object(path, "Saved cache")
                if path.exists()
                else dict(source_cache)
            )
            removed = []
            if authoritative or affected_revised_children:
                for old_study in path_cache.get("studies", []):
                    old_direct = str(
                        old_study.get("Direct Exposure") or exposure
                    )
                    old_pmid = str(old_study.get("PMID") or "")
                    replaced_parent_direct = (
                        not authoritative
                        and old_direct.casefold() == exposure.casefold()
                        and bool(replacement_children_by_pmid.get(old_pmid))
                    )
                    if (
                        not authoritative
                        and old_direct.casefold()
                        not in affected_revised_children
                        and not replaced_parent_direct
                    ):
                        continue
                    record = _authoritative_removal_record(
                        old_study,
                        exposure,
                        cancer,
                        path,
                        studies,
                        final_by_context,
                        parent_deduplications,
                    )
                    if record is not None:
                        if replaced_parent_direct:
                            record.update(
                                {
                                    "status": "PARENT_DIRECT_REPLACED_BY_CHILD",
                                    "exclusion_code": (
                                        "REVISION-PARENT-CHILD-DEDUPLICATED"
                                    ),
                                    "replacement_direct_exposures": sorted(
                                        replacement_children_by_pmid[old_pmid],
                                        key=str.casefold,
                                    ),
                                    "reason": (
                                        "Replaced a redundant parent-direct row "
                                        "for the same PMID with its freshly "
                                        "screened, more-specific child evidence."
                                    ),
                                }
                            )
                        removed.append(record)
                updates = list(path_cache.get("revision_updates") or [])
                updates.append(
                    {
                        "publication_date_start": window_start,
                        "publication_date_end": window_end,
                        "mode": (
                            "all_history_authoritative"
                            if authoritative
                            else "revised_child_rollup_scoped_replace"
                        ),
                        "vocabulary_digest": candidates.get("vocabulary_digest"),
                        "affected_revised_children": sorted(
                            affected_revised_children, key=str.casefold
                        ),
                        "removed_records": removed,
                        "studies_after": len(studies),
                    }
                )
                path_cache["revision_updates"] = updates
            path_cache.update(result_fields)
            path_cache.update(
                {
                    "exposure": exposure,
                    "cancer": cancer,
                    "outcome": "Incidence",
                    "canonical_track": "chatgpt",
                    "screening_models": screening_models,
                    "screening_input_digests": screening_input_digests,
                    "screening_protocol_version": final_packet["protocol_version"],
                    "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            prepared_cache_writes.append((path, path_cache, 4, True))
            context_removed.extend(removed)
        all_removed.extend(context_removed)
        changes.append(
            {
                "exposure": exposure,
                "cancer": cancer,
                "status": (
                    "authoritatively_replaced"
                    if authoritative
                    else "revised_child_rollup_scoped_replaced"
                ),
                "added_pmids": sorted({str(item.get("pmid") or "") for item in items}),
                "refreshed_pmids": sorted(set(refreshed)),
                "removed_pmids": sorted(
                    {record["pmid"] for record in context_removed if record.get("pmid")}
                ),
                "removed_records": context_removed,
                "studies_after": len(studies),
                "headline": result_fields.get("headline"),
            }
        )

    removal_packet = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "mode": "all_history_authoritative",
        "records": all_removed,
    }
    subtype_packet = {
        "created_at": datetime.now().astimezone().isoformat(),
        "reason": (
            "Related Tomato and Pepper (Capsicum) evidence was retained but not "
            "automatically pooled across food, dietary-component, biomarker, and "
            "supplement subtypes."
        ),
        "records": subtype_review,
    }
    packet = {
        "created_at": datetime.now().astimezone().isoformat(),
        "mode": "all_history_authoritative",
        "searched_contexts": len(searched_contexts),
        "included_contexts": len(included),
        "removed_records": len(all_removed),
        "changes": changes,
    }
    writes = [
        *prepared_cache_writes,
        (output_dir / "revision_removed_records.json", removal_packet, 2, False),
        (output_dir / "exposure_subtype_review.json", subtype_packet, 2, False),
        (output_dir / "applied_changes.json", packet, 2, False),
    ]
    _transactional_json_writes(writes)
    print(json.dumps(packet, indent=2, default=str))
    return packet


def apply_to_saved_results(output_dir: Path) -> dict[str, Any]:
    """Add only ChatGPT-canonical, poolable studies and rebuild diagnostics."""
    import pandas as pd

    final_packet = load_json(output_dir / "final_screening.json", {})
    if final_packet.get("canonical_track") != "chatgpt":
        raise RuntimeError("Saved-result mutation requires the canonical ChatGPT packet")
    candidates = load_json(output_dir / "candidates.json", {})
    searched_revision_contexts: set[tuple[str, str]] | None = None
    if (
        candidates.get("revision_aware") is True
        and candidates.get("run_mode") != "included_reclassification"
    ):
        # Exact lineage and current all-history search scope are checked before
        # even audit artifacts are written.
        searched_revision_contexts = require_current_revision_apply(output_dir)
    else:
        # Ordinary monthly and included-reclassification applies must prove the
        # same exact conditional-R3 lineage before any mutation.
        _validate_current_chatgpt_lineage(output_dir, candidates, final_packet)
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
    subtype_review = [
        item
        for item in final_packet.get("results", [])
        if item.get("final_status") == "ELIGIBLE_SUBTYPE_REVIEW"
    ]
    from scripts.display_eligibility import eligible_fair
    included = [
        item for item in final_packet.get("results", [])
        if (item.get("full_text_status") == "INCLUDE_META" or eligible_fair(item))
        and item.get("track") == "chatgpt"
        and not meta_analysis.is_curated_meta_analysis_exclusion(
            item.get("pmid"), item.get("cancer"), item.get("exposure"), "Incidence"
        )
    ]
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
    changes = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in included:
        direct = str(item.get("direct_exposure") or item.get("exposure") or "")
        targets = [direct, *[str(value) for value in item.get("inherited_exposures") or []]]
        seen_targets: set[str] = set()
        for target in targets:
            if not target or target in seen_targets:
                continue
            seen_targets.add(target)
            grouped[(target, item["cancer"])].append(
                {
                    **item,
                    "exposure": target,
                    "direct_exposure": direct,
                    "hierarchy_inherited": target != direct,
                    "pool_in_parent_meta_analysis": target == direct,
                }
            )

    if searched_revision_contexts is not None:
        return _apply_revision_authoritative(
            output_dir,
            candidates,
            final_packet,
            included,
            grouped,
            subtype_review,
            searched_revision_contexts,
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

    for (exposure, cancer), items in grouped.items():
        safe_exposure = re.sub(r"[^a-zA-Z0-9]+", "_", exposure.lower()).strip("_")
        safe_cancer = re.sub(r"[^a-zA-Z0-9]+", "_", cancer.lower()).strip("_")
        cache_paths = [
            CACHE_DIR / safe_exposure / f"{safe_cancer}_incidence_true_{suffix}.json"
            for suffix in ("all", "core")
        ]
        existing_path = next((path for path in cache_paths if path.exists()), None)
        # A clean MetaVir repository intentionally starts with no inherited
        # caches. The first accepted prostate record must therefore bootstrap
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
        index_by_effect = {
            str(study.get("Effect ID") or f"legacy:{study.get('PMID')}"): index
            for index, study in enumerate(studies)
        }
        legacy_direct_by_pmid = {
            str(study.get("PMID")): index
            for index, study in enumerate(studies)
            if not study.get("Effect ID")
        }
        appended = []
        refreshed = []
        for item in items:
            new_row = study_row(item)
            effect_id = str(new_row["Effect ID"])
            pmid = str(item.get("pmid"))
            if (
                effect_id not in index_by_effect
                and not item.get("hierarchy_inherited")
                and pmid in legacy_direct_by_pmid
            ):
                # Migrate one legacy direct row in place. Inherited child rows
                # remain separate estimates and never overwrite a parent row.
                index = legacy_direct_by_pmid.pop(pmid)
                studies[index] = new_row
                index_by_effect[effect_id] = index
                refreshed.append(pmid)
                continue
            if effect_id in index_by_effect:
                index = index_by_effect[effect_id]
                if studies[index] != new_row:
                    studies[index] = new_row
                    refreshed.append(pmid)
                continue
            studies.append(new_row)
            index_by_effect[effect_id] = len(studies) - 1
            appended.append(pmid)
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
        for column in ("Effect Size", "Lower CI", "Upper CI", "SE", "Cases", "Sample Size", "Estimated Cases"):
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
                plot_namespace="chatgpt",
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
        for path in cache_paths:
            cache = load_json(path, dict(source_cache))
            for key in (
                "studies", "summary_html", "headline", "screening_stats",
                "plot_url", "funnel_plot_url", "baujat_plot_url",
            ):
                cache[key] = studies if key == "studies" else result.get(key)
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
                "refreshed_pmids": refreshed,
                "studies_after": len(result.get("studies", [])),
                "headline": result.get("headline"),
            }
        )
    packet = {"created_at": datetime.now().astimezone().isoformat(), "changes": changes}
    save_json(output_dir / "applied_changes.json", json_compatible(packet))
    print(json.dumps(packet, indent=2, default=str))
    return packet


def _finite_float(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _close_reported_number(left: Any, right: Any) -> bool:
    first = _finite_float(left)
    second = _finite_float(right)
    if first is None or second is None:
        return False
    return math.isclose(first, second, rel_tol=1e-6, abs_tol=1e-8)


def _comparison_agrees(left: Any, right: Any) -> bool:
    first = _normalised_comparison(left)
    second = _normalised_comparison(right)
    if not first or not second:
        return False
    if first in second or second in first:
        return True
    first_tokens = set(first.split())
    second_tokens = set(second.split())
    return (
        len(first_tokens & second_tokens) / max(1, len(first_tokens | second_tokens))
        >= 0.5
    )


def _same_reported_effect(source_study: dict[str, Any], target_row: dict[str, Any]) -> bool:
    """Require a strong same-estimate match before deleting a source effect."""
    source_type = str(source_study.get("Effect Type") or "").strip().upper()
    target_type = str(target_row.get("Effect Type") or "").strip().upper()
    if not source_type or source_type != target_type:
        return False
    if not all(
        _close_reported_number(source_study.get(source_key), target_row.get(target_key))
        for source_key, target_key in (
            ("Effect Size", "Effect Size"),
            ("Lower CI", "Lower CI"),
            ("Upper CI", "Upper CI"),
        )
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
    expected_row = study_row(target_item)
    pmid = str(expected_row.get("PMID") or "")
    for path in _canonical_cache_paths(target, cancer):
        if not path.exists():
            continue
        cached = load_json(path, {})
        for study in cached.get("studies", []):
            direct = str(study.get("Direct Exposure") or target).strip()
            if (
                str(study.get("PMID") or "") == pmid
                and direct.casefold() == target.casefold()
                and _same_reported_effect(expected_row, study)
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
    """Persist one source-cache removal after its targets are safely present."""
    import pandas as pd

    cache = load_json(path, {})
    cache["studies"] = retained
    cache["exposure"] = source
    cache["cancer"] = cancer
    cache["outcome"] = "Incidence"
    cache["canonical_track"] = "chatgpt"
    cache["screening_models"] = dict(final_packet.get("screening_models") or {})
    cache["screening_input_digests"] = dict(
        final_packet.get("screening_input_digests") or {}
    )
    cache["screening_protocol_version"] = final_packet.get("protocol_version")
    cache["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates = list(cache.get("reclassification_updates") or [])
    updates.append(
        {
            "revision_date": revision_manifest().get("revision_date"),
            "removed_source_record_ids": sorted(removed_record_ids),
            "rule": "target_persisted_before_source_removal",
        }
    )
    cache["reclassification_updates"] = updates

    if retained:
        frame = pd.DataFrame(retained)
        for column in (
            "Effect Size", "Lower CI", "Upper CI", "SE", "Cases",
            "Sample Size", "Estimated Cases",
        ):
            if column in frame:
                frame[column] = pd.to_numeric(
                    frame[column]
                    .astype(str)
                    .str.replace(",", "", regex=False)
                    .str.strip(),
                    errors="coerce",
                )
        result = meta_analysis.perform_meta_analysis(
            frame,
            cancer,
            source,
            plot_namespace="chatgpt",
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
        cache["summary_html"] = (
            "<p>No directly included studies remain after the audited exposure "
            "reclassification.</p>"
        )
        cache["headline"] = None
        cache["plot_url"] = None
        cache["funnel_plot_url"] = None
        cache["baujat_plot_url"] = None
        stats = dict(cache.get("screening_stats") or {})
        stats["extracted"] = 0
        stats["canonical_track"] = "chatgpt"
        cache["screening_stats"] = stats
    save_json(path, json_compatible(cache), indent=4, ensure_ascii=True)


def apply_reclassification_results(output_dir: Path) -> dict[str, Any]:
    """Apply targets first, then transactionally remove matched source effects."""
    candidates = load_json(output_dir / "candidates.json", {})
    if candidates.get("run_mode") != "included_reclassification":
        raise RuntimeError(
            "Reclassification apply requires an included_reclassification candidate packet"
        )
    final_packet = load_json(output_dir / "final_screening.json", {})
    if final_packet.get("canonical_track") != "chatgpt":
        raise RuntimeError("Reclassification requires canonical ChatGPT final screening")
    # Fail closed before target or source cache mutation. This validates the
    # exact current source union, approved target roles, protocol, models,
    # candidate digest, and every Luna -> Sol -> conditional Luna stage packet.
    require_current_reclassification_apply(output_dir)

    # This mutation is intentionally ordered: accepted target rows (including
    # hierarchy roll-ups) must exist before any direct source row is considered.
    target_apply = apply_to_saved_results(output_dir)
    result_by_context = {
        str(item.get("context_id") or ""): item
        for item in final_packet.get("results", [])
    }
    targets = candidates.get("reclassification_targets") or {}
    source_records = candidates.get("reclassification_source_records") or []
    removal_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    audit_records: list[dict[str, Any]] = []

    for record in source_records:
        source = str(record.get("source_exposure") or "")
        cancer = str(record.get("cancer") or "")
        pmid = str(record.get("pmid") or "")
        source_study = record.get("source_study") or {}
        source_cid = dual_screening.context_id(pmid, source, cancer)
        source_result = result_by_context.get(source_cid)
        base_audit = {
            "source_record_id": record.get("source_record_id"),
            "pmid": pmid,
            "source_exposure": source,
            "cancer": cancer,
            "source_final_status": (
                source_result.get("final_status") if source_result else "MISSING"
            ),
            "source_exclusion_code": (
                source_result.get("exclusion_code") if source_result else None
            ),
            "source_screen_reason": (
                source_result.get("reason")
                if source_result
                else "The source screening context is missing from the final packet."
            ),
        }
        if source_result and source_result.get("full_text_status") == "INCLUDE_META":
            audit_records.append(
                {
                    **base_audit,
                    "action": "retained_source_reaffirmed",
                    "accepted_targets": [],
                    "reason": (
                        "The exact Luna-Sol-Luna pipeline reaffirmed the direct "
                        "source exposure classification."
                    ),
                }
            )
            continue

        effect_matched: list[tuple[str, dict[str, Any]]] = []
        for target in targets.get(source, []):
            target_result = result_by_context.get(
                dual_screening.context_id(pmid, target, cancer)
            )
            if not target_result or target_result.get("full_text_status") != "INCLUDE_META":
                continue
            target_row = study_row(target_result)
            if _same_reported_effect(source_study, target_row):
                effect_matched.append((target, target_result))

        if not effect_matched:
            audit_records.append(
                {
                    **base_audit,
                    "action": "retained_for_adjudication",
                    "accepted_targets": [],
                    "reason": (
                        "No accepted target context matched the same PMID, effect, "
                        "95% confidence interval, effect type, and comparison."
                    ),
                }
            )
            continue

        persisted = [
            target
            for target, target_result in effect_matched
            if _target_effect_is_persisted(target, cancer, target_result)
        ]
        if not persisted:
            audit_records.append(
                {
                    **base_audit,
                    "action": "retained_for_adjudication",
                    "accepted_targets": [target for target, _item in effect_matched],
                    "reason": (
                        "A matching target was screened in, but its same-effect cache "
                        "record was not verified, so the source was not removed."
                    ),
                }
            )
            continue

        removal_ids[(source, cancer)].add(str(record.get("source_record_id") or ""))
        audit_records.append(
            {
                **base_audit,
                "action": "moved_to_verified_targets",
                "accepted_targets": persisted,
                "reason": (
                    "The source classification was not retained and the same "
                    "PMID/effect was verified in the listed accepted target cache(s)."
                ),
            }
        )

    cache_changes = []
    for (source, cancer), identifiers in sorted(removal_ids.items()):
        originals: dict[Path, dict[str, Any]] = {}
        pending_cache_changes: list[dict[str, Any]] = []
        try:
            for path in _canonical_cache_paths(source, cancer):
                if not path.exists():
                    continue
                cached = load_json(path, {})
                originals[path] = cached
                retained = []
                removed = []
                for study in cached.get("studies", []):
                    direct = str(study.get("Direct Exposure") or source).strip()
                    record_id = _source_record_id(source, cancer, study)
                    if (
                        direct.casefold() == source.casefold()
                        and record_id in identifiers
                    ):
                        removed.append(record_id)
                    else:
                        retained.append(study)
                if not removed:
                    continue
                _rebuild_reclassified_source_cache(
                    path,
                    source,
                    cancer,
                    retained,
                    removed,
                    final_packet,
                )
                try:
                    cache_label = str(path.relative_to(REPO_ROOT))
                except ValueError:
                    cache_label = str(path)
                pending_cache_changes.append(
                    {
                        "cache": cache_label,
                        "removed_source_record_ids": sorted(removed),
                        "studies_after": len(retained),
                    }
                )
        except Exception:
            # Accepted targets remain safely written, but every source cache in
            # this source/cancer unit is restored so no partial source removal
            # can escape an interrupted multi-file update.
            for written_path, original in originals.items():
                save_json(
                    written_path,
                    json_compatible(original),
                    indent=4,
                    ensure_ascii=True,
                )
            raise
        cache_changes.extend(pending_cache_changes)

    packet = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "run_mode": "included_reclassification",
        "transaction_rule": "target_persisted_before_source_removal",
        "target_apply": target_apply,
        "records": audit_records,
        "cache_changes": cache_changes,
        "moved_source_effects": sum(
            item.get("action") == "moved_to_verified_targets"
            for item in audit_records
        ),
        "retained_source_effects": sum(
            item.get("action") != "moved_to_verified_targets"
            for item in audit_records
        ),
    }
    save_json(
        output_dir / "reclassification_applied_changes.json",
        json_compatible(packet),
    )
    print(json.dumps(packet, indent=2, default=str))
    return packet


def apply_screened_results(output_dir: Path) -> dict[str, Any]:
    require_candidate_generation_ready(output_dir)
    candidates = load_json(output_dir / "candidates.json", {})
    if candidates.get("run_mode") == "included_reclassification":
        return apply_reclassification_results(output_dir)
    return apply_to_saved_results(output_dir)


def write_run_cost_report(output_dir: Path, report_dir: Path) -> Path:
    """Write per-exposure GPT list-price estimates from actual usage metadata."""
    root_path = cost_accounting.write_cost_report(output_dir, REPO_ROOT)
    cost_accounting.write_additional_exposure_cost_report(output_dir, REPO_ROOT)
    report_path = report_dir / "cost.txt"
    if report_path.resolve() == root_path.resolve():
        return root_path
    return cost_accounting.write_cost_report(
        output_dir,
        REPO_ROOT,
        report_path=report_path,
    )


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
        "--catalog",
        choices=("current", "additional", "combined"),
        default="current",
        help=(
            "choose current, the collision-checked additionalExposure.json + "
            "additionalExposureV2.json union, or all catalogs"
        ),
    )
    parser.add_argument(
        "--revision-aware",
        action="store_true",
        help=(
            "search alias-expanded/new current exposures from the revision manifest "
            "plus every V1/V2 additional exposure; narrowed exposures are excluded "
            "from NCBI"
        ),
    )
    parser.add_argument(
        "--revision-reclassification",
        action="store_true",
        help=(
            "screen only already-included Fish oil/Omega-3/Tomato PMIDs against "
            "their approved targets; performs no PubMed ESearch and removes a "
            "source effect only after the same target effect is persisted"
        ),
    )
    parser.add_argument(
        "--exposure",
        action="append",
        help=(
            "limit discovery to an exact synonyms/additional catalog name; repeat "
            "for additional exposures"
        ),
    )
    parser.add_argument(
        "--rescreen-saved-contexts",
        action="store_true",
        help=(
            "include already saved PMID/exposure contexts; required for an "
            "alias-expanded historical rescreen and MetaFemina-style clean rerun"
        ),
    )
    parser.add_argument(
        "--stage",
        choices=(
            "discover", "refresh-metadata", "first-screen", "second-screen",
            "report", "full-text", "finalize", "apply", "cost-report", "all",
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
    if (args.exposure or args.rescreen_saved_contexts or args.revision_aware) and (
        args.stage not in {"discover", "all"}
    ):
        parser.error(
            "--exposure, --rescreen-saved-contexts, and --revision-aware apply only "
            "to discover/all stages"
        )
    if args.revision_reclassification:
        conflicts = (
            args.start_date
            or args.end_date
            or args.previous_month
            or args.all_history_through
            or args.revision_aware
            or args.exposure
            or args.rescreen_saved_contexts
            or args.catalog != "current"
        )
        if conflicts:
            parser.error(
                "--revision-reclassification cannot be combined with date-window, "
                "catalog, exposure, rescreen, or --revision-aware options"
            )
        revision_date = str(revision_manifest().get("revision_date") or "")
        try:
            date.fromisoformat(revision_date)
        except ValueError:
            parser.error("the revision manifest requires an ISO revision_date")
        args.start_date = None
        args.end_date = revision_date
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
            "--revision-aware requires --all-history-through so expanded/new "
            "exposures cannot accidentally receive a partial-date search"
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
            "use --all-history-through, or use --revision-reclassification"
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
            f"included_reclassification_{args.end_date}"
            if args.revision_reclassification
            else (
                f"all_history_through_{args.end_date}"
                if args.all_history_through
                else f"{args.start_date}_{args.end_date}"
            )
        )
        args.report_dir = args.report_dir / subdir
    return args


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
                selected_exposures=args.exposure,
                rescreen_saved_contexts=args.rescreen_saved_contexts,
                catalog=args.catalog,
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
                selected_exposures=args.exposure,
                rescreen_saved_contexts=args.rescreen_saved_contexts,
                catalog=args.catalog,
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
    elif args.stage == "cost-report":
        write_run_cost_report(args.output_dir, args.report_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Monthly evidence update failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
