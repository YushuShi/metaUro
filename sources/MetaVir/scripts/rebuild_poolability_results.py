#!/usr/bin/env python3
"""Reapply incidence poolability offline, preserving original model evidence.

Rebuild ChatGPT caches, the Fable-adjudicated combined projection, all exposure
diagnostics and localized summary plots. No PubMed or model requests are made.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from scripts import build_combined_results as combined


def write_audit(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["pipeline", "PMID", "first_author_last_name", "exposure", "context_id",
              "previous_status", "final_status", "direction_imputed", "one_sentence_explanation"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def project(rows, pipeline, monthly):
    projected = {cid: combined.normalized_pipeline_row(row, pipeline)
                 for cid, row in rows.items()}
    audit = combined.reclassify_poolability(projected, monthly)
    for row in audit:
        row["pipeline"] = pipeline
    return projected, audit


def build_chatgpt(args, rows, selected, monthly, meta_analysis):
    inventory = combined.base_cache_inventory(args.base_cache_root)
    grouped, direct_count, supplement_count = combined.build_grouped_studies(
        rows, inventory, set(selected), monthly
    )
    build_id = combined.canonical_digest({
        "policy_version": 1, "rows": rows, "studies": grouped,
        "generators": {key: combined.sha256_file(path)
                       for key, path in combined.GENERATOR_FILES.items()},
    })
    counts = Counter(row["final_status"] for row in rows.values())
    stats = {
        "total_fetched": len({row.get("pmid") for row in rows.values()}),
        "after_prefilter": len(rows),
        "llm_screened_in": counts["INCLUDE_META"] + counts["ELIGIBLE_NOT_POOLABLE"],
        "llm_screened_out": counts["EXCLUDE"],
        "canonical_track": "chatgpt",
    }
    exposures = sorted(set(inventory) | set(grouped), key=str.casefold)
    with tempfile.TemporaryDirectory(prefix="metavir-poolability-") as temporary:
        stage = Path(temporary)
        cache_root = stage / "Cached_results"
        static_root = stage / "static" / "chatgpt"
        cache_root.mkdir()
        static_root.mkdir(parents=True)
        with combined.working_directory(stage):
            for exposure in exposures:
                entry = inventory.get(exposure)
                slug = entry["slug"] if entry else combined.safe_component(exposure)
                (static_root / slug).mkdir(parents=True, exist_ok=True)
                source = copy.deepcopy(entry["core"] if entry else {})
                cancer = str(source.get("cancer") or "Prostate cancer")
                rebuilt = combined.analysis_fields(
                    grouped.get(exposure, []), cancer, exposure, stats, meta_analysis,
                    generate_plots=True, build_id=build_id, canonical_track="chatgpt",
                )
                for suffix in ("all", "core"):
                    cache = copy.deepcopy(entry[suffix] if entry else {})
                    cache.update(rebuilt)
                    cache.update({
                        "exposure": exposure, "cancer": cancer, "outcome": "Incidence",
                        "canonical_track": "chatgpt",
                        "poolability_projection": {"policy_version": 1, "build_id": build_id,
                            "source": "selected original ChatGPT final packets", "new_model_calls": 0},
                    })
                    combined.write_json(cache_root / slug / f"prostate_cancer_incidence_true_{suffix}.json",
                                        monthly.json_compatible(cache), indent=4)
        manifest = {
            "policy_version": 1, "build_id": build_id, "canonical_track": "chatgpt",
            "statuses": dict(counts), "direct_ui_studies": direct_count,
            "base_supplemental_direct_studies": supplement_count,
            "displayed_ui_studies": sum(map(len, grouped.values())),
            "cache_exposures": len(exposures), "new_model_calls": 0,
        }
        combined.write_json(cache_root / "_poolability_manifest.json", manifest)
        combined.transactionally_replace_directories([
            (cache_root, args.base_cache_root),
            (static_root, args.static_root / "chatgpt"),
        ])
    return manifest


def finish_summary(args, options):
    with (options.audit_root / "poolability_audit.csv").open(encoding="utf-8-sig", newline="") as handle:
        audit = list(csv.DictReader(handle))
    summary = {"policy_version": 1, "new_model_calls": 0,
               "source_packets_unchanged": True,
               "chatgpt": combined.load_json_object(args.base_cache_root / "_poolability_manifest.json"),
               "combined": combined.load_json_object(args.output_root / "_merge_manifest.json")["counts"],
               "transitions": dict(Counter(f'{row["pipeline"]}: {row["previous_status"]} -> {row["final_status"]}'
                                           for row in audit)),
               "summary_plots_status": "pending"}
    combined.write_json(options.audit_root / "rebuild_summary.json", summary)
    if not options.skip_summary:
        from scripts import build_summary_plots
        plot_manifest = build_summary_plots.build(args.output_root, REPO_ROOT / "Plot")
        summary["summary_scopes"] = len(plot_manifest["scopes"])
        summary["summary_plots_status"] = "complete"
        combined.write_json(options.audit_root / "rebuild_summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path,
                        default=REPO_ROOT / "outputs" / "poolability_policy_2026-09-05")
    parser.add_argument("--skip-summary", action="store_true", help="For targeted development only.")
    parser.add_argument("--summary-only", action="store_true", help="Resume summary generation after a completed cache rebuild.")
    options = parser.parse_args(argv)
    args = combined.parse_args([])
    if options.summary_only:
        finish_summary(args, options)
        return 0
    meta_analysis, monthly = combined._load_project_modules()
    chatgpt, selected, _ = combined.selected_chatgpt_lineage(args.monthly_root, args.parallel_root)
    other = combined.claude_gemini_lineage(args.parallel_root)
    audit = []
    projections = {}
    for pipeline, raw in (("CHATGPT", chatgpt), ("CLAUDE_GEMINI", other)):
        projected, changes = project(raw, pipeline, monthly)
        projections[pipeline] = projected
        audit.extend(changes)
        combined.write_json(options.audit_root / f"{pipeline.lower()}_poolability.json", {
            "policy_version": 1, "new_model_calls": 0,
            "source_packets_unchanged": True,
            "statuses": dict(Counter(row["final_status"] for row in projected.values())),
            "results": [row for row in projected.values() if row["final_status"] != "EXCLUDE"],
        })
    chat_manifest = build_chatgpt(args, projections["CHATGPT"], selected, monthly, meta_analysis)
    print(json.dumps({"chatgpt": chat_manifest}, indent=2), flush=True)
    manifest = combined.build(args)
    combined_audit = combined.load_json_object(args.output_root / "_poolability_audit.json")["records"]
    audit.extend(dict(row, pipeline="COMBINED") for row in combined_audit)
    write_audit(options.audit_root / "poolability_audit.csv", audit)
    write_audit(options.audit_root / "status_changes.csv",
                [row for row in audit if row["previous_status"] != row["final_status"]])
    finish_summary(args, options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
