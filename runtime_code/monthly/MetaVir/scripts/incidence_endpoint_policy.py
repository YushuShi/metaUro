"""Fail-closed endpoint contract for MetaVir incidence estimates.

Time-to-event methods do not establish an incidence outcome by themselves.
Only time to a first prostate-cancer diagnosis is in scope.  The human
checkpoint records reviewed legacy estimates whose modeled event was death or
postdiagnosis progression; projection code uses it without rewriting the
historical model review.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VERSION = "incident-diagnosis-endpoint-2026-09-19-v1"
DEFAULT_CHECKPOINT = (
    ROOT / "data" / "incidence_endpoint_review" / "current_exclusions.json"
)

PROMPT_RULE = """
INCIDENCE ENDPOINT RULE FOR TIME-TO-EVENT ESTIMATES:
For every HR, and for any other estimate defined by follow-up time, identify
the modeled failure event rather than inferring it from the effect label or
from the participants being cancer-free at baseline. Include an estimate only
when follow-up ends at the first diagnosis of prostate cancer. Advanced,
high-grade, high-risk, or metastatic disease is eligible only when it is
defined at that first diagnosis. Exclude time from diagnosis to death,
prostate-cancer or all-cause mortality, fatal/lethal disease defined by a later
death, overall or disease-specific survival, recurrence, progression, and
metastasis that develops after the initial diagnosis, using S2-WRONG-OUTCOME
or S2-POSTDIAGNOSIS as appropriate. A paper may report both diagnosis and
death endpoints; retain only its separately reported diagnosis estimate.
"""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_checkpoint(path: Path = DEFAULT_CHECKPOINT) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or document.get("version") != VERSION:
        raise ValueError(f"Unsupported incidence-endpoint checkpoint: {path}")
    records = document.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("Incidence-endpoint checkpoint must contain records")
    identities: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Incidence-endpoint records must be objects")
        kind = record.get("match_kind")
        identifier = str(record.get("match_id") or "")
        if kind not in {"source_row_id", "derived_effect_id"} or not identifier:
            raise ValueError(f"Invalid incidence-endpoint match: {record!r}")
        identity = (kind, identifier)
        if identity in identities:
            raise ValueError(f"Duplicate incidence-endpoint match: {identity}")
        identities.add(identity)
        if not str(record.get("pmid") or "") or not str(record.get("reason") or ""):
            raise ValueError(f"Incomplete incidence-endpoint record: {identity}")
        if record.get("decision") != "exclude_non_diagnosis_endpoint":
            raise ValueError(f"Unsupported incidence-endpoint decision: {identity}")
    return document


def record_id(record: dict[str, Any]) -> str:
    return f"{record['match_kind']}:{record['match_id']}"


def matching_record(
    row: dict[str, Any],
    checkpoint: dict[str, Any] | None,
    *,
    source_row_id: str | None = None,
) -> dict[str, Any] | None:
    if not checkpoint:
        return None
    review = row.get("disease_scope_review") or {}
    source_ids = {
        str(value)
        for value in (source_row_id, review.get("source_row_id"))
        if value
    }
    effect_id = str(row.get("Effect ID") or "")
    for record in checkpoint["records"]:
        if record["match_kind"] == "source_row_id" and record["match_id"] in source_ids:
            return record
        if record["match_kind"] == "derived_effect_id" and record["match_id"] == effect_id:
            return record
    return None


def audit_action(
    record: dict[str, Any],
    row: dict[str, Any],
    *,
    source_row_id: str | None,
    origin: str,
) -> dict[str, Any]:
    return {
        "action": "excluded_non_diagnosis_endpoint",
        "incidence_endpoint_policy_version": VERSION,
        "record_id": record_id(record),
        "match_kind": record["match_kind"],
        "match_id": record["match_id"],
        "pmid": str(row.get("PMID") or record["pmid"]),
        "exposure": row.get("Direct Exposure"),
        "effect_id": row.get("Effect ID"),
        "source_row_id": source_row_id,
        "origin": origin,
        "endpoint_class": record.get("endpoint_class"),
        "reason": record["reason"],
    }
