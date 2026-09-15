"""Replay explicit human decisions after automated evidence reconciliation.

The checkpoint owns complete reviewed records and immutable before/source receipts.
No model packets are changed. A later automated rebuild cannot replace human values.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

CHECKPOINT = "human_review/current_decisions.json"
VERSION = "human-manual-review-2026-09-14-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, default=str).encode()).hexdigest()


def apply_checkpoint(records, output_root, tracks=None):
    path = Path(output_root) / CHECKPOINT
    if not path.exists():
        return records, []
    packet = json.loads(path.read_text())
    if packet.get("version") != VERSION:
        raise ValueError("Unsupported human manual review checkpoint")
    result = copy.deepcopy(records)
    audit = []
    seen = set()
    for entry in packet["decisions"]:
        cid = entry["context_id"]
        if cid in seen or cid not in records:
            raise ValueError("Duplicate or unknown human-reviewed context: " + cid)
        seen.add(cid)
        if tracks is not None:
            for track, expected in entry["source_track_sha256"].items():
                if digest(tracks.get(track, {}).get(cid)) != expected:
                    raise ValueError("Reviewed model evidence changed; reconcile its human decision before rebuilding: " + cid)
        after = entry["after"]
        if (after.get("context_id") != cid or not after.get("human_adjudication")
                or digest(after) != entry["after_sha256"]):
            raise ValueError("Invalid human-reviewed record: " + cid)
        result[cid] = copy.deepcopy(after)
        audit.append({"review_id": entry["review_id"], "context_id": cid,
                      "decision_authority": "human_manual_review", "applied": True,
                      "follow_up": entry.get("follow_up", []),
                      "final_status": after["final_status"],
                      "after_sha256": entry["after_sha256"]})
    return result, audit


def display_records(item):
    """Show each accepted contrast, with only one same-cohort primary estimate."""
    estimates = [item.get("extraction") or {}, *(item.get("additional_extractions") or [])]
    result = []
    for index, extraction in enumerate(estimates):
        row = copy.deepcopy(item)
        row.pop("additional_extractions", None)
        row.pop("additional_records", None)
        row["extraction"] = copy.deepcopy(extraction)
        row["human_estimate_index"] = index + 1
        if index:
            row["human_nonprimary_same_cohort"] = True
            row["pool_in_parent_meta_analysis"] = False
        result.append(row)
    return result


def context_of_study(row):
    return (row.get("provisional_origin_context_id") or row.get("original_screening_context_id")
            or row.get("Original Screening Context ID") or row.get("context_id")
            or "|".join((str(row.get("PMID") or ""), str(row.get("Direct Exposure") or ""), "Prostate cancer")))


def merge_saved_scope_rows(grouped, output_root, reviewed):
    """Replace reviewed contexts while retaining unrelated disease-scope decisions."""
    saved = {}
    for path in Path(output_root).glob("*/prostate_cancer_incidence_true_all.json"):
        packet = json.loads(path.read_text())
        saved[packet["exposure"]] = packet.get("studies", [])
    result = {}
    for exposure in set(saved) | set(grouped):
        unchanged = [copy.deepcopy(row) for row in saved.get(exposure, [])
                     if context_of_study(row) not in reviewed]
        replacements = [row for row in grouped.get(exposure, [])
                        if context_of_study(row) in reviewed]
        result[exposure] = unchanged + replacements
    return result


def decorate_study(row, item):
    if not item.get("human_adjudication"):
        return row
    row.update(context_id=item["context_id"],
               decision_authority="human_manual_review",
               human_adjudication=copy.deepcopy(item["human_adjudication"]),
               manual_review_pending=False, key_comparison_pending=False,
               final_status=item["final_status"],
               human_review_follow_up=copy.deepcopy(item.get("human_review_follow_up", [])))
    if item.get("human_nonprimary_same_cohort"):
        row.update(manual_poolable=False, policy_poolable=False, **{"Primary Estimate": False},
                   pool_in_parent_meta_analysis=False,
                   manual_pooling_restriction="Another accepted contrast from the same cohort is primary; shared participants/reference must not be counted twice.")
    if item.get("final_status") == "ELIGIBLE_NOT_POOLABLE":
        row.update(display_only=True, policy_poolable=False, manual_poolable=False,
                   pool_in_parent_meta_analysis=False, **{"Primary Estimate": False},
                   display_only_reason=item.get("reason"))
    return row
