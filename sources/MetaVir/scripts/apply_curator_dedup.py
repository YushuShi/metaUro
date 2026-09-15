#!/usr/bin/env python3
"""Apply the user's five prostate dataset choices and rebuild saved results offline."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import curator_dedup as curator

USER_INSTRUCTION = "keep only\n\nPMID: 15514967\n\nPMID: 25866035\n\nPMID: 39417685\n\nPMID: 19066370\n\nfor those exposure/disease combinations"
AUDIT_REPORT = "reports/duplicate_sample_size_effect_2026-09-14/possible_shared_datasets.md"
CHOICES = [("Lycopene", "15514967", "17392149"),
           ("Omega-3 fatty acids", "39417685", "38343844"),
           ("Omega-6 fatty acids", "39417685", "38343844"),
           ("Selenium", "19066370", "20924966"),
           ("Vitamin E", "19066370", "20924966")]


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_map(packet):
    return {row["context_id"]: row for row in packet["results"]}


def scope_packets():
    return {str(path.relative_to(ROOT)): read(path)
            for path in (ROOT / "combined").glob("*/*_incidence_true_all.json")}


def prepare():
    current = curator.load_checkpoint(ROOT / "combined")
    if current is not None:
        return current
    from scripts import current_comparison_adapter as adapter
    tracks, _, _, paths = adapter.current_tracks()
    records = record_map(read(ROOT / "combined/final_screening.json"))
    report = ROOT.parents[1] / AUDIT_REPORT
    decisions = []
    for index, (exposure, keep, reject) in enumerate(CHOICES, 1):
        cid = "|".join((reject, exposure, curator.GENERAL))
        retained_cid = "|".join((keep, exposure, curator.GENERAL))
        before, retained = records[cid], records[retained_cid]
        if before.get("final_status") == "EXCLUDE" or retained.get("final_status") == "EXCLUDE":
            raise ValueError("Expected both articles in the reviewed baseline: " + cid)
        after = copy.deepcopy(before)
        decision_id = f"metavir-dataset-dedup-2026-09-14-{index:02d}"
        note = f"User selected PMID {keep} instead of PMID {reject} for {exposure} / {curator.GENERAL}; exclude the redundant article from display and pooling, including inherited exposure views."
        after.update(final_status="EXCLUDE", full_text_status="EXCLUDE", eligibility="EXCLUDE",
                     reason=note, exclusion_code="CURATOR-REDUNDANT-DATASET",
                     decision_authority="human_curator_deduplication",
                     manual_review_pending=False, key_comparison_pending=False,
                     provisional_ui_display_only=False, policy_poolable=False,
                     manual_poolable=False, pool_in_parent_meta_analysis=False,
                     curator_deduplication={"version": curator.VERSION, "decision_id": decision_id,
                         "retained_pmid": keep, "retained_context_id": retained_cid,
                         "excluded_pmid": reject, "exposure": exposure, "disease": curator.GENERAL,
                         "action": "exclude_redundant_article_context", "user_instruction": USER_INSTRUCTION,
                         "supersedes_previous_decisions": True})
        decisions.append({"decision_id": decision_id, "context_id": cid,
                          "exposure": exposure, "disease": curator.GENERAL,
                          "retained_pmid": keep, "excluded_pmid": reject,
                          "retained_context_id": retained_cid,
                          "superseded_human_review_id": (before.get("human_adjudication") or {}).get("review_id"),
                          "source_track_sha256": {track.lower(): {key: curator.digest(values.get(key))
                              for key in (cid, retained_cid)} for track, values in tracks.items()},
                          "before": before, "before_sha256": curator.digest(before),
                          "after": after, "after_sha256": curator.digest(after),
                          "retained_record": retained, "retained_record_sha256": curator.digest(retained)})
    return {"version": curator.VERSION, "repository": "MetaVir", "decision_authority": "user",
            "user_instruction": USER_INSTRUCTION, "preceding_audit_report": AUDIT_REPORT,
            "preceding_audit_report_sha256": sha(report),
            "scope_note": "The PMID 25866035 choice belongs to the urinary source and is applied separately. These five decisions cover general prostate incidence only; unrelated exposure and aggressive-cancer scopes are preserved.",
            "original_final_sha256": sha(ROOT / "combined/final_screening.json"),
            "effective_track_sha256": {track: sha(path) for track, path in paths.items()},
            "decisions": decisions, "decisions_sha256": curator.digest(decisions)}


def apply(work_dir, rebuild=True):
    from scripts import build_combined_results as builder
    from scripts import current_comparison_adapter as adapter
    work_dir.mkdir(parents=True, exist_ok=True)
    packet = prepare()
    before = record_map(read(ROOT / "combined/final_screening.json"))
    scopes_before = scope_packets()
    expected_contexts = {entry["context_id"] for entry in packet["decisions"]}
    protected = {}
    for relative in ("data", "Cached_results", "combined/human_review"):
        for path in (ROOT / relative).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                protected[str(path.relative_to(ROOT))] = sha(path)
    write(work_dir / "protected_sha256.json", protected)
    write(work_dir / "baseline_scope_packets.json", scopes_before)
    baseline = work_dir / "baseline_final_screening.json"
    baseline.write_bytes((ROOT / "combined/final_screening.json").read_bytes())
    write(ROOT / "combined" / curator.CHECKPOINT, packet)
    if rebuild:
        args = ["--preserve-canonical-from", str(baseline)]
        for cid in sorted(expected_contexts):
            args.extend(["--refresh-context", cid])
        builder.build(builder.parse_args(args))
        from scripts import build_summary_plots, rebuild_subexposure_results
        build_summary_plots.build(ROOT / "combined", ROOT / "Plot")
        rebuild_subexposure_results.build(skip_summary=False)
    else:
        final = read(ROOT / "combined/final_screening.json")
        final["results"] = list(curator.apply_checkpoint(before, ROOT / "combined")[0].values())
        write(ROOT / "combined/final_screening.json", final)
    installed = record_map(read(ROOT / "combined/final_screening.json"))
    if set(installed) != set(before) or any(installed[cid] != row for cid, row in before.items() if cid not in expected_contexts):
        raise AssertionError("An unrelated canonical context changed")
    for entry in packet["decisions"]:
        if installed[entry["context_id"]] != entry["after"]:
            raise AssertionError("Installed exclusion differs from curator receipt")
        if installed[entry["retained_context_id"]] != entry["retained_record"]:
            raise AssertionError("Retained article evidence changed")
    if curator.apply_checkpoint(installed, ROOT / "combined")[0] != installed:
        raise AssertionError("Curator replay is not idempotent")
    tracks, _, _, paths = adapter.current_tracks()
    if {track: sha(path) for track, path in paths.items()} != packet["effective_track_sha256"]:
        raise AssertionError("Original source screening packets changed")
    curator.apply_checkpoint(installed, ROOT / "combined", {key.lower(): values for key, values in tracks.items()})
    changed = [relative for relative, expected in protected.items() if sha(ROOT / relative) != expected]
    if changed:
        raise AssertionError("Protected source or earlier human receipt changed: " + repr(changed))
    audit = {"version": curator.VERSION, "decisions": len(expected_contexts),
             "user_instruction": USER_INSTRUCTION, "checkpoint_sha256": sha(ROOT / "combined" / curator.CHECKPOINT),
             "original_source_and_human_receipt_files_verified": len(protected),
             "protected_files_unchanged": True, "checkpoint_replay_idempotent": True,
             "unrelated_canonical_contexts_unchanged": len(before) - len(expected_contexts),
             "retained_canonical_contexts_unchanged": len(packet["decisions"]),
             "new_model_calls": 0, "scopes": [], "mode_scopes": []}
    if rebuild:
        scopes_after = scope_packets()
        if set(scopes_before) != set(scopes_after):
            raise AssertionError("Scientific scope inventory changed")
        for relative, old in scopes_before.items():
            exposure, disease = old["exposure"], old.get("disease") or old.get("cancer")
            expected = curator.filter_rows(old["studies"], exposure, disease, packet)
            if scopes_after[relative]["studies"] != expected:
                raise AssertionError("Unrelated or retained scientific scope rows changed: " + relative)
            removed = [row for row in old["studies"] if curator.excluded_row(row, exposure, disease, packet)]
            if removed:
                audit["scopes"].append({"path": relative, "exposure": exposure, "disease": disease,
                    "before_rows": len(old["studies"]), "after_rows": len(expected),
                    "removed_rows": removed, "before_rows_sha256": curator.digest(old["studies"]),
                    "after_rows_sha256": curator.digest(expected)})
        for mode in ("yes", "no"):
            for path in (ROOT / "Subexposure_results" / mode).glob("*/*_incidence_true_all.json"):
                payload = read(path)
                exposure, disease = payload["exposure"], payload.get("disease") or payload.get("cancer")
                if any(curator.excluded_row(row, exposure, disease, packet) for row in payload["studies"]):
                    raise AssertionError("An excluded article reappeared through inheritance")
                if disease == curator.GENERAL and exposure in {e[0] for e in CHOICES} | {"Polyunsaturated fat (PUFA)"}:
                    audit["mode_scopes"].append({"mode": mode, "exposure": exposure, "disease": disease,
                        "rows": len(payload["studies"]), "rows_sha256": curator.digest(payload["studies"])})
        audit.update(canonical_scopes_verified=len(scopes_before),
                     aggressive_scope_rows_unchanged=sum("aggressive_prostate" in p for p in scopes_before),
                     canonical_removed_row_occurrences=sum(len(s["removed_rows"]) for s in audit["scopes"]))
    write(ROOT / "combined/curator_dedup/application_audit.json", audit)
    print(json.dumps({key: value for key, value in audit.items() if key not in {"scopes", "mode_scopes"}}, indent=2))
    return audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    if args.work_dir:
        apply(args.work_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="metavir-curator-dedup-") as temporary:
            apply(Path(temporary))
