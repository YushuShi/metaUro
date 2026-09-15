"""Replay scope-specific user choices after all earlier evidence adjudications.

This excludes redundant article contexts, never rewrites retained estimates, and
keeps the earlier human/model decisions intact as historical evidence receipts.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts.human_manual_review import digest

VERSION = "curator-dataset-deduplication-2026-09-14-v1"
CHECKPOINT = "curator_dedup/current_decisions.json"
GENERAL = "Prostate cancer"


def load_checkpoint(output_root):
    path = Path(output_root) / CHECKPOINT
    if not path.exists():
        return None
    packet = json.loads(path.read_text())
    if packet.get("version") != VERSION or digest(packet.get("decisions")) != packet.get("decisions_sha256"):
        raise ValueError("Invalid curator deduplication checkpoint")
    seen = set()
    for entry in packet["decisions"]:
        cid = entry["context_id"]
        expected = "|".join((entry["excluded_pmid"], entry["exposure"], GENERAL))
        keep = "|".join((entry["retained_pmid"], entry["exposure"], GENERAL))
        if (cid in seen or cid != expected or entry["retained_context_id"] != keep
                or entry["disease"] != GENERAL or entry["retained_pmid"] == entry["excluded_pmid"]):
            raise ValueError("Invalid curator context identity: " + cid)
        seen.add(cid)
        for name in ("before", "after", "retained_record"):
            if digest(entry[name]) != entry[name + "_sha256"]:
                raise ValueError("Changed curator evidence receipt: " + cid)
        after = entry["after"]
        if (after.get("context_id") != cid or after.get("final_status") != "EXCLUDE"
                or after.get("decision_authority") != "human_curator_deduplication"):
            raise ValueError("Invalid curator exclusion: " + cid)
    return packet


def apply_checkpoint(records, output_root, tracks=None):
    packet = load_checkpoint(output_root)
    if packet is None:
        return records, []
    result = copy.deepcopy(records)
    audit = []
    for entry in packet["decisions"]:
        cid, retained = entry["context_id"], entry["retained_context_id"]
        if cid not in records or retained not in records:
            raise ValueError("Curator-selected article context is missing: " + cid)
        if tracks is not None:
            for track, expected_records in entry["source_track_sha256"].items():
                for source_cid, expected in expected_records.items():
                    if digest(tracks.get(track, {}).get(source_cid)) != expected:
                        raise ValueError("Curator-reviewed source changed; reconcile the decision: " + source_cid)
        result[cid] = copy.deepcopy(entry["after"])
        audit.append({"decision_id": entry["decision_id"], "context_id": cid,
                      "retained_context_id": retained, "applied": True,
                      "decision_authority": "human_curator_deduplication",
                      "final_status": "EXCLUDE", "after_sha256": entry["after_sha256"],
                      "superseded_human_review_id": entry.get("superseded_human_review_id")})
    return result, audit


def excluded_row(row, exposure, disease, packet):
    """Match the source exposure even when its row is inherited into a parent."""
    if packet is None or disease != GENERAL:
        return False
    direct = row.get("Direct Exposure") or row.get("direct_exposure") or exposure
    pmid = str(row.get("PMID") or row.get("pmid") or "")
    return any(pmid == entry["excluded_pmid"] and direct == entry["exposure"]
               for entry in packet["decisions"])


def filter_rows(rows, exposure, disease, packet):
    return [row for row in rows if not excluded_row(row, exposure, disease, packet)]


def filter_grouped(grouped, output_root):
    packet = load_checkpoint(output_root)
    return {exposure: filter_rows(rows, exposure, GENERAL, packet)
            for exposure, rows in grouped.items()}


def protect_ui_packet(packet, output_root):
    """A provisional projection cannot reintroduce a later rejected context."""
    decisions = load_checkpoint(output_root)
    if decisions is None:
        return packet
    excluded = {entry["context_id"] for entry in decisions["decisions"]}
    result = copy.deepcopy(packet)
    result["results"] = [row for row in result["results"]
                         if row.get("context_id") in excluded
                         or (row.get("provisional_origin_context_id") or row.get("context_id", "").split("::pending:")[0]) not in excluded]
    mapped = {row["context_id"]: row for row in result["results"]}
    for entry in decisions["decisions"]:
        mapped[entry["context_id"]] = copy.deepcopy(entry["after"])
    result["results"] = [mapped[cid] for cid in sorted(mapped)]
    result["curator_deduplication_version"] = VERSION
    return result
