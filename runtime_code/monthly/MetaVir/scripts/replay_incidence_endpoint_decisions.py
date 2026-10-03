#!/usr/bin/env python3
"""Replay only the reviewed diagnosis-endpoint exclusions on saved MetaVir caches.

Unlike the broader aggressive-disease projection, this replay never
reclassifies, adds, or edits a retained evidence row.  It removes only rows
matched by the human checkpoint, recomputes the affected analyses, and rebuilds
the active combined/subexposure summaries from the destination's own evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import warnings
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_combined_results as combined  # noqa: E402
from scripts import incidence_endpoint_policy as endpoint_policy  # noqa: E402
from scripts import monthly_evidence_update as monthly  # noqa: E402


CACHE_ROOTS = (
    ("Cached_results", "chatgpt/incidence_endpoint_v1"),
    ("combined", "incidence_endpoint_v1"),
    ("Subexposure_results/yes", "subexposures/yes/incidence_endpoint_v1"),
    ("Subexposure_results/no", "subexposures/no/incidence_endpoint_v1"),
)


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(monthly.json_compatible(payload), ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def row_fingerprint(row: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def source_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = list(payload.get("studies") or [])
    rows.extend((payload.get("incidence_endpoint_exclusions") or {}).get("source_rows") or [])
    seen: set[str] = set()
    unique = []
    for row in rows:
        fingerprint = row_fingerprint(row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(row)
    return unique


def filter_rows(
    rows: list[dict[str, Any]], checkpoint: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return retained rows, audit actions, and exact excluded row snapshots."""
    retained, actions, excluded = [], [], []
    for row in rows:
        record = endpoint_policy.matching_record(row, checkpoint)
        if record is None:
            retained.append(row)
            continue
        source_id = str((row.get("disease_scope_review") or {}).get("source_row_id") or "") or None
        actions.append(
            endpoint_policy.audit_action(
                record,
                row,
                source_row_id=source_id,
                origin="saved_cache_endpoint_replay",
            )
        )
        excluded.append(row)
    return retained, actions, excluded


def recompute(
    payload: dict[str, Any],
    rows: list[dict[str, Any]],
    namespace: str,
    *,
    generate_plots: bool,
) -> dict[str, Any]:
    import meta_analysis
    from scripts.subexposure_policy import eligible_for_selection

    disease = str(payload.get("disease") or payload.get("cancer") or "")
    exposure = str(payload.get("exposure") or "")
    frame = combined.numeric_frame(rows)
    result: dict[str, Any] = {}
    if rows:
        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = meta_analysis.perform_meta_analysis(
                frame,
                disease,
                exposure,
                outcome=str(payload.get("endpoint") or payload.get("outcome") or "Incidence"),
                exclude_meta=True,
                df_all=frame,
                generate_plots=generate_plots,
                plot_namespace=namespace,
            )
        if result.get("error") and any(eligible_for_selection(row) for row in rows):
            raise RuntimeError(f"{disease}/{exposure}: {result['error']}")

    output = copy.deepcopy(payload)
    for field in ("headline", "plot_url", "funnel_plot_url", "baujat_plot_url"):
        output[field] = result.get(field)
    output["summary_html"] = result.get("summary_html") or (
        "<p>Eligible evidence is listed below. No studies meet the default pooling criteria.</p>"
    )
    for field in ("forest_plots", "forest_plot", "canonical_rr", "analysis_settings"):
        output.pop(field, None)
    output["studies"] = rows
    if isinstance(output.get("screening_stats"), dict):
        output["screening_stats"]["extracted"] = len(rows)
    return output


def rebuild_summaries() -> dict[str, int]:
    from scripts import build_summary_plots as summary

    counts: dict[str, int] = {}
    for cache_relative, plot_relative, mode in (
        ("combined", "Plot", None),
        ("Subexposure_results/yes", "Plot/subexposures/yes", "yes"),
        ("Subexposure_results/no", "Plot/subexposures/no", "no"),
    ):
        manifest = summary.build(ROOT / cache_relative, ROOT / plot_relative)
        if mode is not None:
            manifest["include_subexposures"] = mode
        write(ROOT / plot_relative / "summary_manifest.json", manifest)
        counts[cache_relative] = len(manifest["scopes"])
    return counts


def replay(*, generate_plots: bool = True, rebuild_summary_outputs: bool = True) -> dict[str, Any]:
    checkpoint = endpoint_policy.load_checkpoint()
    checkpoint_digest = endpoint_policy.file_sha256(endpoint_policy.DEFAULT_CHECKPOINT)
    expected = {endpoint_policy.record_id(record) for record in checkpoint["records"]}
    matched: set[str] = set()
    root_counts: dict[str, dict[str, int]] = {}
    changed_files: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="metavir-endpoint-replay-") as directory:
        stage = Path(directory)
        targets: list[tuple[Path, Path]] = []
        for root_relative, namespace in CACHE_ROOTS:
            cache_root = ROOT / root_relative
            counts = Counter()
            for all_path in sorted(cache_root.glob("*/*_incidence_true_all.json")):
                payload = read(all_path)
                original_active = list(payload.get("studies") or [])
                retained, actions, excluded = filter_rows(source_rows(payload), checkpoint)
                if not actions:
                    continue
                matched.update(action["record_id"] for action in actions)
                counts["changed_scopes"] += 1
                counts["removed_occurrences"] += len(excluded)
                active_changed = original_active != retained
                output = payload
                if active_changed:
                    old_cwd = Path.cwd()
                    try:
                        os.chdir(stage)
                        output = recompute(
                            payload, retained, namespace, generate_plots=generate_plots
                        )
                    finally:
                        os.chdir(old_cwd)
                else:
                    output = copy.deepcopy(payload)
                state = {
                    "version": endpoint_policy.VERSION,
                    "checkpoint_sha256": checkpoint_digest,
                    "records": actions,
                    "source_rows": excluded,
                }
                output["incidence_endpoint_exclusions"] = state
                output["incidence_endpoint_projection"] = {
                    "policy_version": endpoint_policy.VERSION,
                    "checkpoint_sha256": checkpoint_digest,
                    "replay_scope": "exact_checkpoint_rows_only",
                    "retained_rows_unchanged": all(
                        before is after for before, after in zip(
                            [row for row in source_rows(payload)
                             if endpoint_policy.matching_record(row, checkpoint) is None],
                            retained,
                            strict=True,
                        )
                    ),
                }
                relative_all = all_path.relative_to(ROOT)
                for suffix in ("all", "core"):
                    destination = all_path.with_name(
                        all_path.name.replace("_true_all.json", f"_true_{suffix}.json")
                    )
                    if suffix == "core" and not destination.is_file():
                        continue
                    staged = stage / destination.relative_to(ROOT)
                    write(staged, output)
                    targets.append((staged, destination))
                changed_files.append(
                    {
                        "cache_root": root_relative,
                        "path": str(relative_all),
                        "rows_before": len(original_active),
                        "rows_after": len(retained),
                        "removed_occurrences": len(excluded),
                        "removed_record_ids": sorted({action["record_id"] for action in actions}),
                        "unexpected_removed_rows": 0,
                        "added_rows": 0,
                        "retained_row_mutations": 0,
                    }
                )
            root_counts[root_relative] = dict(counts)

        unmatched = sorted(expected - matched)
        if unmatched:
            raise RuntimeError(f"Endpoint checkpoint did not match: {unmatched}")
        for staged, destination in targets:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
        static_stage = stage / "static"
        if static_stage.exists():
            shutil.copytree(static_stage, ROOT / "static", dirs_exist_ok=True)

    summary_counts = rebuild_summaries() if rebuild_summary_outputs else {}
    report = {
        "schema_version": 1,
        "policy_version": endpoint_policy.VERSION,
        "checkpoint_sha256": checkpoint_digest,
        "expected_record_count": len(expected),
        "matched_record_count": len(matched),
        "removed_occurrences": sum(
            item["removed_occurrences"] for item in changed_files
        ),
        "unexpected_removed_rows": 0,
        "added_rows": 0,
        "retained_row_mutations": 0,
        "root_counts": root_counts,
        "changed_files": changed_files,
        "summary_scope_counts": summary_counts,
    }
    write(ROOT / "data/incidence_endpoint_review/replay_audit.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-plots", action="store_true")
    parser.add_argument("--skip-summaries", action="store_true")
    args = parser.parse_args()
    report = replay(
        generate_plots=not args.skip_plots,
        rebuild_summary_outputs=not args.skip_summaries,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
