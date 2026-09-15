#!/usr/bin/env python3
"""Import the completed MetaVir review CSV and rebuild saved evidence offline.

Only the explicit Manual review results column provides new decisions. Original
pipeline packets stay immutable. Re-running the same input reuses the checkpoint.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

EXPECTED_CSV_SHA256 = "ea0df907d0a6d148fdf471cda5300e46f867db94cd2a45ccdd1de098f8a7706e"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import human_manual_review as human

# Each non-track instruction is reviewed explicitly; free text is never executed.
NUMBERS = {
    "metaVir-0142": (1.12, 1.01, 1.25, "RR", 47750),
    "metaVir-0143": (1.28, 1.02, 1.60, "RR", 47750),
    "metaVir-0145": (.80, .68, .93, "RR", 47750),
    "metaVir-0213": (1.52, .92, 2.51, "OR", None),
    "metaVir-0252": (1.05, .84, 1.32, "OR", None),
    "metaVir-0254": (1.38, .85, 2.25, "OR", None),
    "metaVir-0256": (1.10, .83, 1.44, "OR", None),
    "metaVir-0257": (.85, .65, 1.12, "OR", None),
    "metaVir-0331": (1.53, .70, 3.45, "OR", None),
    "metaVir-0335": (1.89, 1.06, 3.36, "HR", 310),
    "metaVir-0437": (.99, .87, 1.11, None, None),
    "metaVir-0438": (1.25, 1.11, 1.41, None, None),
    "metaVir-0440": (.90, .80, 1.02, "HR", None),
    "metaVir-0456": (4.42, 1.68, 11.59, "OR", None),
}
SAMPLE_SIZES = {"metaVir-0147": 952, "metaVir-0148": 952, "metaVir-0149": 952,
                "metaVir-0285": 2074, "metaVir-0383": 953}
NO_DIRECT = {"metaVir-0346", "metaVir-0392", "metaVir-0421"}
FOLLOW_UP = {
    "metaVir-0160": ["Provide the replacement calcium effect and matching CI, or confirm that no numerical total/nonadvanced prostate-cancer estimate is available. 'Unrelated' alone does not specify an effect size."],
    "metaVir-0254": ["Confirm the sample size and exposure time window for OR 1.38 (0.85–2.25): saved tracks give N=2268 and N=2267 and earlier effects concern a different outcome/contrast."],
    "metaVir-0256": ["Provide the analyzed N and exposure time window for corrected oatmeal OR 1.10 (0.83–1.44); the saved full-text review leaves N missing and earlier estimates refer to different life periods."],
    "metaVir-0257": ["Provide the analyzed N and grain/time-window contrast for corrected whole-grain OR 0.85 (0.65–1.12); the saved full-text review leaves N missing."],
    "metaVir-0331": ["Confirm the analyzed N for the Table 1 vitamin-D OR 1.53 (0.70–3.45): overall N=152, but vitamin-D categories total 150; the prior interaction-model estimate is replaced."],
    "metaVir-0456": ["Confirm the comparison/reference group and analyzed N for selenium OR 4.42 (1.68–11.59): prior tracks differ in reference tertile and N=285 versus 188."],
}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False,
                               default=str) + "\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_evidence(row, source_sha):
    return {"source": "human_manual_review", "excerpt": row["Manual review results"],
            "source_sha256": source_sha, "location": row["Review ID"] + ", Manual review results",
            "url": row["PubMed URL"]}


def clear_derived(e):
    # A corrected effect/CI cannot retain a p-value, SE, or receipt from another model.
    for key in list(e):
        if (key.startswith(("p_value", "ci_source", "wald_", "effect_size_imput", "ci_imput",
                            "effect_original", "ci_original", "astra_fulltext"))
                or key in {"standard_error", "variance", "se", "derivations", "reported_extraction",
                           "reported_effect_size", "reported_ci_lower", "reported_ci_upper", "reported_p_value",
                           "inversion_applied", "inversion_provenance", "direction_original_comparison",
                           "direction_imputation_reason", "direction_imputed", "reported_comparison_direction"}):
            e.pop(key, None)
    e.update(p_value=None, ci_scale="ratio", effect_scale="ratio", needs_inversion=False)
    evidence = e.setdefault("evidence", {})
    for key in ("p_value", "effect_size", "confidence_interval", "ci_lower", "ci_upper", "comparison"):
        evidence.pop(key, None)


def clean_record(row):
    for key in list(row):
        if (key.startswith(("provisional_", "key_comparison", "nonpoolable_status", "astra_"))
                or key in {"additional_extractions", "additional_records", "manual_review_pending",
                           "policy_poolable", "manual_poolable", "scientific_pooling_eligible", "poolability_policy"}):
            row.pop(key, None)
    return row


def prepare(csv_path):
    from scripts import current_comparison_adapter as adapter
    from scripts import build_combined_results as builder
    from scripts import dual_screening
    raw = csv_path.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    if source_sha != EXPECTED_CSV_SHA256:
        raise ValueError("CSV differs from the explicitly mapped completed review; review new instructions before importing")
    rows = list(csv.DictReader(io.StringIO(raw.decode("cp1252"))))
    if len(rows) != 96 or len({r["Context ID"] for r in rows}) != 96:
        raise ValueError("This importer expects the 96 unique completed MetaVir contexts")
    current_path = ROOT / "combined" / human.CHECKPOINT
    if current_path.exists():
        existing = json.loads(current_path.read_text())
        if existing.get("source_sha256") == source_sha:
            return existing
        raise ValueError("A different completed review already exists; merge its human decisions explicitly")
    canonical = json.loads((ROOT / "combined/final_screening.json").read_text())
    prior = {r["context_id"]: r for r in canonical["results"]}
    selected, _, _, effective_paths = adapter.current_tracks()
    tracks = {k.lower(): v for k, v in selected.items()}
    pending = json.loads((ROOT / "combined/provisional_ui/pending_manifest.json").read_text())
    manifest = {r["id"]: r for r in pending["rows"]}
    decisions = []
    for row in rows:
        rid, cid, note = row["Review ID"], row["Context ID"], row["Manual review results"].strip()
        if (row["Repository"] != "metaVir" or row["Endpoint"] != "Incidence"
                or row["Estimate pair"] != "1" or cid != "|".join((row["PMID"], row["Exposure"], row["Disease"]))
                or cid not in prior or rid not in manifest or not note
                or manifest[rid]["source_row"]["Context ID"] != cid):
            raise ValueError("Review identity does not match saved evidence: " + rid)
        sources = {track: values[cid] for track, values in tracks.items()}
        # The CSV's displayed numeric columns bind its pair to these exact tracks.
        from scripts.astra_fulltext_common import source_value_equal
        from scripts.key_comparison_policy import missing
        for track, prefix in (("chatgpt", "ChatGPT"), ("claude_gemini", "Claude+Gemini")):
            e = sources[track].get("extraction") or {}
            for suffix, field in (("sample size", "total_n"), ("effect size", "effect_size"),
                                  ("CI lower", "ci_lower"), ("CI upper", "ci_upper")):
                value = row[prefix + " " + suffix]
                if not source_value_equal(value, e.get(field), "count" if field == "total_n" else "ratio"):
                    raise ValueError("Reviewed source numbers changed: " + rid + " " + prefix + " " + field)
            claimed_type, saved_type = row[prefix + " effect type"], e.get("effect_type")
            if not (missing(claimed_type) and missing(saved_type)) and str(claimed_type).upper() != str(saved_type).upper():
                raise ValueError("Reviewed source effect type changed: " + rid + " " + prefix)
        text = note.casefold()
        selection = ("claude_gemini" if "claude" in text and ("use" in text or "recommend" in text)
                     else "chatgpt" if ("chatgpt" in text or "chagpt" in text) and "use" in text else None)
        before = prior[cid]
        after = clean_record(copy.deepcopy(sources[selection] if selection else before))
        evidence = source_evidence(row, source_sha)
        e = after.get("extraction") or {}
        after["extraction"] = e
        action = "selected_" + selection if selection else "custom"
        status = builder.normalized_status(sources[selection], claude_gemini=selection == "claude_gemini") if selection else "ELIGIBLE_NOT_POOLABLE"
        follow_up = copy.deepcopy(FOLLOW_UP.get(rid, []))
        if rid in SAMPLE_SIZES:
            e["total_n"] = SAMPLE_SIZES[rid]
            e.setdefault("evidence", {})["sample_size"] = evidence
            action = "corrected_sample_size"
        elif rid in {"metaVir-0301", "metaVir-0307"}:
            # Keep the reported low/high bundle; existing study-row projection inverts once.
            e.update(total_n=300, needs_inversion=True, direction_imputed=False)
            e.setdefault("evidence", {}).update(sample_size=evidence, comparison=evidence)
            action = "corrected_sample_size_and_reciprocal_direction"
        elif rid in NUMBERS:
            effect, lo, hi, measure, n = NUMBERS[rid]
            clear_derived(e)
            e.update(effect_size=effect, ci_lower=lo, ci_upper=hi, ci_confidence_level=95,
                     effect_type=measure or e.get("effect_type"), reported_effect_type=measure or e.get("effect_type"))
            if n is not None:
                e["total_n"] = n
                e["evidence"]["sample_size"] = evidence
            e["evidence"].update(effect_size=evidence, confidence_interval=evidence)
            # Remove the rejected estimate's optional model/contrast descriptions.
            for key in ("adjusted_model", "adjustment_model", "effect_is_adjusted", "comparison_group", "reference_group", "comparison_direction"):
                e.pop(key, None)
            e["comparison_type"] = None
            if rid in {"metaVir-0142", "metaVir-0143"}:
                e["comparison_type"] = "High versus low intake over overall follow-up"
            elif rid == "metaVir-0145":
                e["comparison_type"] = ">2 versus <0.25 servings/week tomato sauce over overall follow-up"
            elif rid == "metaVir-0213":
                # The exact human-selected value is also quoted in the saved abstract evidence.
                e["comparison_type"] = "Higher plasma 25(OH)D concentration (50 ng/ml category) versus 30–<50 ng/ml"
            elif rid == "metaVir-0331":
                e["comparison_type"] = "Normal serum vitamin D (>30 ng/ml) versus deficiency (≤30 ng/ml); Table 1"
            elif rid == "metaVir-0335":
                e["comparison_type"] = "Quartile 4 versus quartile 1"
            if e["comparison_type"]:
                e["evidence"]["comparison"] = evidence
                old_evidence = (before.get("extraction") or {}).get("evidence") or {}
                if rid == "metaVir-0213":
                    e["evidence"]["comparison"] = copy.deepcopy(old_evidence.get("effect_size"))
                elif rid == "metaVir-0331":
                    e["evidence"]["comparison"] = copy.deepcopy(old_evidence.get("comparison_group"))
                elif rid == "metaVir-0335":
                    e["evidence"]["comparison"] = copy.deepcopy(old_evidence.get("comparison") or old_evidence.get("comparison_group"))
            if rid in {"metaVir-0254", "metaVir-0456"}:
                # Their existing N conflict was not resolved by an effect-only correction.
                e["total_n"] = None
                e["evidence"].pop("sample_size", None)
            action = "corrected_effect_bundle"
        elif rid == "metaVir-0055":
            city = copy.deepcopy(sources["chatgpt"].get("extraction") or {})
            rural = copy.deepcopy(sources["claude_gemini"].get("extraction") or {})
            for estimate in (city, rural):
                estimate.setdefault("evidence", {}).update(effect_size=evidence, confidence_interval=evidence, comparison=evidence)
            after["extraction"] = e = city
            after["additional_extractions"] = [rural]
            action = "retained_two_same_cohort_contrasts"
        elif rid in NO_DIRECT:
            status = "EXCLUDE"
            after.update(eligibility="EXCLUDE", exclusion_code="HUMAN-NO-DIRECT-EXPOSURE-ESTIMATE")
            after["extraction"] = None
            action = "excluded_direct_exposure_context"
        elif rid == "metaVir-0160":
            clear_derived(e)
            e.update(effect_size=None, ci_lower=None, ci_upper=None, comparison_type=None)
            e["evidence"]["effect_size"] = evidence
            action = "rejected_values_qualitative_only"
        elif selection is None:
            raise ValueError("Unmapped human instruction: " + rid)
        if status != "EXCLUDE":
            after["eligibility"] = "INCLUDE"
            e = dual_screening.apply_poolability_policy(after.get("extraction"))
            after["extraction"] = e
            status = "INCLUDE_META" if dual_screening.is_poolable(e, after.get("jbi")) else "ELIGIBLE_NOT_POOLABLE"
            # A selected source's actual clinical restrictions remain in effect.
            independent_hold = any(after.get(k) for k in ("full_text_pooling_block", "manual_review_hold", "is_mendelian_randomization"))
            if independent_hold:
                status = "ELIGIBLE_NOT_POOLABLE"
            if status == "INCLUDE_META":
                guarded = dual_screening.apply_whole_food_subtype_disposition(dict(after, final_status=status))
                status = guarded["final_status"]
        issues = dual_screening.required_quantitative_issues(after.get("extraction")) if status != "EXCLUDE" else []
        if status != "EXCLUDE" and (after.get("jbi") or {}).get("grade") not in {"Moderate", "Good"}:
            issues.append("JBI grade must be Moderate or Good")
        after["poolability_policy"] = {"version": human.VERSION, "remaining_issues": issues,
                                       "human_decision_authority": True}
        after.update(final_status=status, full_text_status=status, reason=note,
                     decision_authority="human_manual_review", key_comparison_pending=False,
                     manual_review_pending=False, human_review_follow_up=follow_up,
                     human_adjudication={"version": human.VERSION, "review_id": rid,
                                         "source_sha256": source_sha, "manual_review_results": row["Manual review results"],
                                         "action": action, "selected_pipeline": selection,
                                         "estimate_pair": 1, "follow_up_required": bool(follow_up)},
                     combined_adjudication={"resolution": "HUMAN_MANUAL_REVIEW", "selected_pipeline": selection,
                                           "explanation": note})
        decisions.append({"review_id": rid, "context_id": cid, "action": action,
                          "source_row": row, "source_track_sha256": {k: human.digest(v) for k, v in sources.items()},
                          "source_records": sources, "before": before, "before_sha256": human.digest(before),
                          "after": after, "after_sha256": human.digest(after), "follow_up": follow_up,
                          "original_pending_entry": manifest[rid]})
    return {"version": human.VERSION, "repository": "MetaVir", "source_filename": csv_path.name,
            "source_sha256": source_sha, "source_encoding": "cp1252", "source_rows": len(rows),
            "original_final_sha256": sha(ROOT / "combined/final_screening.json"),
            "effective_track_sha256": {k: sha(v) for k, v in effective_paths.items()},
            "decisions": decisions}


def apply(csv_path, rebuild=True):
    from scripts import build_combined_results as builder
    from scripts import curator_dedup as curator
    packet = prepare(csv_path)
    combined = ROOT / "combined"
    reviewed = {d["context_id"] for d in packet["decisions"]}
    before = json.loads((combined / "final_screening.json").read_text())
    prior = {r["context_id"]: r for r in before["results"]}
    with tempfile.TemporaryDirectory(prefix="metavir-human-baseline-") as td:
        baseline = Path(td) / "final_screening.json"
        baseline.write_bytes((combined / "final_screening.json").read_bytes())
        write(combined / human.CHECKPOINT, packet)
        (combined / "human_review/source_review.csv").write_bytes(csv_path.read_bytes())
        manifest_path = combined / "provisional_ui/pending_manifest.json"
        pending = json.loads(manifest_path.read_text())
        pending["rows"] = [r for r in pending["rows"] if r["source_row"]["Context ID"] not in reviewed]
        write(manifest_path, pending)
        if rebuild:
            builder.build(builder.parse_args(["--preserve-canonical-from", str(baseline)]))
            from scripts import build_summary_plots, rebuild_subexposure_results
            build_summary_plots.build(combined, ROOT / "Plot")
            rebuild_subexposure_results.build(skip_summary=False)
        else:
            records, _ = human.apply_checkpoint(prior, combined)
            records, _ = curator.apply_checkpoint(records, combined)
            before["results"] = list(records.values())
            write(combined / "final_screening.json", before)
    installed = {r["context_id"]: r for r in json.loads((combined / "final_screening.json").read_text())["results"]}
    if set(installed) != set(prior) or any(installed[cid] != row for cid, row in prior.items() if cid not in reviewed):
        raise AssertionError("An unrelated canonical context changed during the human update")
    expected = dict(prior)
    expected.update({d["context_id"]: d["after"] for d in packet["decisions"]})
    expected, curator_audit = curator.apply_checkpoint(expected, combined)
    if any(installed[d["context_id"]] != expected[d["context_id"]] for d in packet["decisions"]):
        raise AssertionError("An installed human decision differs from its checkpoint")
    replayed, _ = human.apply_checkpoint(installed, combined)
    replayed, _ = curator.apply_checkpoint(replayed, combined)
    if replayed != installed:
        raise AssertionError("Human checkpoint replay is not idempotent")
    from scripts import current_comparison_adapter as adapter
    replay_tracks, _, _, effective_paths = adapter.current_tracks()
    if {k: sha(v) for k, v in effective_paths.items()} != packet["effective_track_sha256"]:
        raise AssertionError("Effective source evidence file changed")
    for decision in packet["decisions"]:
        for track, expected in decision["source_track_sha256"].items():
            if human.digest(replay_tracks[track.upper()][decision["context_id"]]) != expected:
                raise AssertionError("Original reviewed model record changed")
    audit = {"version": human.VERSION, "source_sha256": packet["source_sha256"],
             "reviewed_contexts": len(reviewed), "articles": len({d["source_row"]["PMID"] for d in packet["decisions"]}),
             "unrelated_canonical_contexts_unchanged": len(prior) - len(reviewed),
             "checkpoint_replay_idempotent": True, "original_tracks_unchanged": True,
             "new_model_calls": 0, "records": [{k: d[k] for k in ("review_id", "context_id", "action", "follow_up", "before_sha256", "after_sha256")} | {"final_status": installed[d["context_id"]]["final_status"]} for d in packet["decisions"]]}
    if curator_audit:
        superseded = {entry["context_id"] for entry in curator_audit}
        audit["later_curator_decisions_preserved"] = len(superseded)
        for entry in audit["records"]:
            if entry["context_id"] in superseded:
                entry["superseded_by"] = curator.VERSION
    contextual_note = ("The mineral-supplement row selects Claude's exclusion; the following separate Multivitamin row selects ChatGPT. "
                       "The incidental Zinc 2.1 (1.1–4.1) and Selenium 1.3 (0.3–5.7) numbers do not identify their contrasts, "
                       "so unrelated Zinc/Selenium contexts retain their saved estimates. Clarification of the cross-context wording is pending.")
    for entry in audit["records"]:
        if entry["review_id"] == "metaVir-0183":
            entry["contextual_note"] = contextual_note
    checkpoint_path = combined / human.CHECKPOINT
    saved_checkpoint = json.loads(checkpoint_path.read_text())
    for entry in saved_checkpoint["decisions"]:
        if entry["review_id"] == "metaVir-0183":
            entry["contextual_note"] = contextual_note
    write(checkpoint_path, saved_checkpoint)
    write(combined / "human_review/application_audit.json", audit)
    from collections import Counter
    write(combined / "human_review/review_status.json", {
        "version": human.VERSION, "source_sha256": packet["source_sha256"],
        "reviewed_contexts": len(reviewed), "reviewed_articles": audit["articles"],
        "human_decisions_applied": len(reviewed), "pending_model_discrepancy_reviews": 0,
        "quantitative_follow_up_contexts": sum(bool(d["follow_up"]) for d in packet["decisions"]),
        "final_status_counts": dict(Counter(installed[d["context_id"]]["final_status"] for d in packet["decisions"])),
        "temporary_ui_choices_remaining": 0,
    })
    write(combined / "human_review/follow_up.json", {"records": [d for d in audit["records"] if d["follow_up"]]})
    # Remove stale pending-review UI receipts after the reviewed rows leave the manifest.
    for name in ("application_audit.json", "review_dispositions.json"):
        path = combined / "provisional_ui" / name
        existing = json.loads(path.read_text()) if path.exists() else []
        if isinstance(existing, list):
            write(path, [r for r in existing if r.get("context_id") not in reviewed])
    print(json.dumps({k: v for k, v in audit.items() if k != "records"}, indent=2))
    return audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if args.prepare_only:
        packet = prepare(args.csv)
        write(ROOT / "combined" / human.CHECKPOINT, packet)
        print("Prepared", len(packet["decisions"]), "human decisions")
    else:
        apply(args.csv)
