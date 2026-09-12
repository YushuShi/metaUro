#!/usr/bin/env python3
"""Validate one exact MetaPee screening run before it is committed."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from scripts import dual_screening as screening  # noqa: E402


# Follow the screening implementation's release contract instead of duplicating a
# version literal that can silently drift when prompts or packet lineage change.
EXPECTED_PROTOCOL_VERSION = screening.SCREENING_PROTOCOL_VERSION
EXPECTED_MODELS = {
    "chatgpt": {
        "first": "gpt-5.6-luna",
        "second": "gpt-5.6-sol",
        "full_text": "gpt-5.6-luna",
    },
}
REQUIRED_REPORTS = (
    "summary.txt",
    "newStudies.txt",
    "newStudyExclusions.txt",
    "modelDiscrepancies.txt",
    "cost.txt",
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class ArtifactValidationError(ValueError):
    """A durable monthly artifact violated the release contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ArtifactValidationError(message)


def _load_object(path: Path) -> dict[str, Any]:
    _require(path.is_file() and path.stat().st_size > 0, f"Missing artifact: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(f"Invalid JSON in {path}: {exc}") from exc
    _require(isinstance(value, dict), f"Artifact must contain a JSON object: {path}")
    return value


def _results(packet: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    rows = packet.get("results")
    _require(isinstance(rows, list), f"{label}.results must be an array")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        _require(isinstance(row, dict), f"Every {label} result must be an object")
        context_id = str(row.get("context_id") or "")
        _require(bool(context_id), f"{label} contains a blank context_id")
        _require(context_id not in indexed, f"{label} duplicates context_id {context_id}")
        indexed[context_id] = row
    return indexed


def _require_contexts(
    actual: dict[str, dict[str, Any]], expected: set[str], label: str
) -> None:
    actual_ids = set(actual)
    missing = sorted(expected - actual_ids)
    unexpected = sorted(actual_ids - expected)
    _require(
        not missing and not unexpected,
        f"{label} context mismatch; missing={missing}, unexpected={unexpected}",
    )


def _require_count(packet: dict[str, Any], key: str, expected: int, label: str) -> None:
    _require(packet.get(key) == expected, f"{label}.{key} must equal {expected}")


def _validate_usage(packet: dict[str, Any], model: str, label: str) -> None:
    usage = packet.get("usage")
    _require(isinstance(usage, dict), f"{label}.usage must be an object")
    _require(usage.get("model") == model, f"{label}.usage.model must be {model}")
    for key in ("calls", "input_tokens", "output_tokens", "reasoning_tokens"):
        value = usage.get(key)
        _require(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0,
            f"{label}.usage.{key} must be a non-negative integer",
        )


def _validate_stage_header(
    packet: dict[str, Any], track: str, stage: str, label: str
) -> str:
    model = EXPECTED_MODELS[track][stage]
    _require(packet.get("schema_version") == 2, f"{label}.schema_version must be 2")
    _require(packet.get("track") == track, f"{label}.track must be {track}")
    _require(packet.get("model") == model, f"{label}.model must be {model}")
    _require(
        packet.get("protocol_version") == EXPECTED_PROTOCOL_VERSION,
        f"{label}.protocol_version must be {EXPECTED_PROTOCOL_VERSION}",
    )
    digest = packet.get("input_digest")
    _require(
        isinstance(digest, str) and SHA256_PATTERN.fullmatch(digest) is not None,
        f"{label}.input_digest must be a lowercase SHA-256 digest",
    )
    _validate_usage(packet, model, label)
    return model


def _validate_first(
    packet: dict[str, Any], candidates: dict[str, Any], expected: set[str], track: str
) -> dict[str, dict[str, Any]]:
    label = f"{track}_first_screen"
    model = _validate_stage_header(packet, track, "first", label)
    expected_digest = screening.first_input_digest(candidates, model)
    _require(packet["input_digest"] == expected_digest, f"{label} input digest is stale")
    rows = _results(packet, label)
    _require_contexts(rows, expected, label)
    decisions = Counter(row.get("decision") for row in rows.values())
    _require(set(decisions) <= {"PASS", "FAIL"}, f"{label} has an invalid decision")
    _require_count(packet, "source_candidates", len(candidates["articles"]), label)
    _require_count(packet, "source_contexts", len(expected), label)
    _require_count(packet, "pass", decisions["PASS"], label)
    _require_count(packet, "fail", decisions["FAIL"], label)
    return rows


def _validate_second(
    packet: dict[str, Any],
    candidates: dict[str, Any],
    first_packet: dict[str, Any],
    first_rows: dict[str, dict[str, Any]],
    track: str,
) -> dict[str, dict[str, Any]]:
    label = f"{track}_second_screen"
    model = _validate_stage_header(packet, track, "second", label)
    _require(
        packet.get("source_first_model") == first_packet.get("model"),
        f"{label} is not linked to the current first-screen model",
    )
    _require(
        packet.get("source_first_input_digest") == first_packet.get("input_digest"),
        f"{label} is not linked to the current first-screen digest",
    )
    expected_digest = screening.second_input_digest(candidates, first_packet, model)
    _require(packet["input_digest"] == expected_digest, f"{label} input digest is stale")
    expected = {
        context_id
        for context_id, row in first_rows.items()
        if row.get("decision") == "PASS"
    }
    rows = _results(packet, label)
    _require_contexts(rows, expected, label)
    eligibility = Counter(row.get("eligibility") for row in rows.values())
    _require(
        set(eligibility) <= {"INCLUDE", "EXCLUDE"},
        f"{label} has an invalid eligibility value",
    )
    _require_count(packet, "source_first_screen_pass", len(expected), label)
    _require_count(packet, "include", eligibility["INCLUDE"], label)
    _require_count(packet, "exclude", eligibility["EXCLUDE"], label)
    _require_count(
        packet,
        "needs_full_text",
        sum(bool(row.get("needs_full_text")) for row in rows.values()),
        label,
    )
    _require_count(
        packet,
        "pmcid_full_text_candidates",
        sum(bool(row.get("full_text_eligible")) for row in rows.values()),
        label,
    )
    return rows


def _validate_full_text(
    packet: dict[str, Any], second_rows: dict[str, dict[str, Any]], track: str
) -> dict[str, dict[str, Any]]:
    label = f"{track}_full_text_review"
    _validate_stage_header(packet, track, "full_text", label)
    expected = {
        context_id
        for context_id, row in second_rows.items()
        if screening.full_text_gate(row)
    }
    rows = _results(packet, label)
    _require_contexts(rows, expected, label)
    _require_count(packet, "contexts", len(expected), label)
    statuses = Counter(row.get("full_text_status") for row in rows.values())
    _require(
        all(isinstance(status, str) and status for status in statuses),
        f"{label} has an invalid full_text_status",
    )
    _require(packet.get("status_counts") == dict(statuses), f"{label}.status_counts is stale")
    return rows


def _validate_lineage(
    candidates: dict[str, Any],
    first_packet: dict[str, Any],
    second_packet: dict[str, Any],
    full_text_packet: dict[str, Any],
    track: str,
) -> dict[str, Any]:
    """Delegate the content-bound chain validation to the screening contract."""
    try:
        return screening.validate_track_lineage(
            candidates,
            first_packet,
            second_packet,
            full_text_packet,
            track,
            EXPECTED_MODELS[track],
        )
    except screening.ScreeningValidationError as exc:
        raise ArtifactValidationError(str(exc)) from exc


def _validate_final(
    packet: dict[str, Any],
    expected: set[str],
    track: str,
    start: str | None,
    end: str,
    expected_lineage: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    label = f"{track}_final_screening"
    _require(packet.get("schema_version") == 2, f"{label}.schema_version must be 2")
    _require(packet.get("track") == track, f"{label}.track must be {track}")
    _require(
        "publication_date_start" in packet,
        f"{label} must explicitly record its publication-date start",
    )
    _require(packet.get("publication_date_start") == start, f"{label} start date mismatch")
    _require(packet.get("publication_date_end") == end, f"{label} end date mismatch")
    _require(
        packet.get("protocol_version") == expected_lineage["protocol_version"],
        f"{label}.protocol_version does not match its screening lineage",
    )
    _require(
        packet.get("screening_models") == expected_lineage["screening_models"],
        f"{label}.screening_models does not match its screening lineage",
    )
    _require(
        packet.get("screening_input_digests")
        == expected_lineage["screening_input_digests"],
        f"{label}.screening_input_digests does not match its screening lineage",
    )
    rows = _results(packet, label)
    _require_contexts(rows, expected, label)
    statuses = Counter(row.get("final_status") for row in rows.values())
    _require(
        all(isinstance(status, str) and status for status in statuses),
        f"{label} has an invalid final_status",
    )
    _require(packet.get("counts") == dict(statuses), f"{label}.counts is stale")
    return rows


def _validate_validation_status(
    packet: dict[str, Any], start: str | None, end: str
) -> None:
    _require(packet.get("schema_version") == 1, "validation_status.schema_version must be 1")
    _require(
        "publication_date_start" in packet,
        "Validation status must explicitly record its publication-date start",
    )
    _require(packet.get("publication_date_start") == start, "Validation status start date mismatch")
    _require(packet.get("publication_date_end") == end, "Validation status end date mismatch")
    _require(packet.get("screening_mode") == "chatgpt_only", "Screening mode must be chatgpt_only")
    _require(packet.get("canonical_track") == "chatgpt", "Validation status must be ChatGPT-canonical")
    _require(packet.get("active_tracks") == ["chatgpt"], "Only ChatGPT may be active")
    validation = packet.get("validation")
    _require(isinstance(validation, dict), "validation_status.validation must be an object")
    _require(validation.get("track") == "deepseek", "Validation track must be DeepSeek")
    _require(validation.get("role") == "validation_only", "DeepSeek must be validation-only")
    _require(
        validation.get("status") == screening.DEEPSEEK_VALIDATION_STATUS,
        "DeepSeek validation must be pending gateway enablement",
    )
    _require(validation.get("results_available") is False, "DeepSeek results must be unavailable")
    _require(
        validation.get("required_models")
        == {
            "first": screening.DEEPSEEK_FIRST_MODEL,
            "second": screening.DEEPSEEK_SECOND_MODEL,
            "full_text": screening.DEEPSEEK_FULL_TEXT_MODEL,
        },
        "Pending DeepSeek model contract is incorrect",
    )
    _require(bool(validation.get("reason")), "Pending DeepSeek validation needs a reason")


def previous_month_window(today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    first_this_month = today.replace(day=1)
    end = first_this_month - timedelta(days=1)
    start = end.replace(day=1)
    return start.isoformat(), end.isoformat()


def validate_run(
    updates_root: Path,
    reports_root: Path,
    start: str | None,
    end: str,
) -> dict[str, Any]:
    """Validate one bounded or all-history directory and its artifact graph."""
    if start is not None:
        date.fromisoformat(start)
    date.fromisoformat(end)
    run_name = (
        f"all_history_through_{end}"
        if start is None
        else f"{start}_{end}"
    )
    update_dir = updates_root / run_name
    report_dir = reports_root / run_name
    _require(update_dir.is_dir(), f"Missing exact update directory: {update_dir}")
    _require(report_dir.is_dir(), f"Missing exact report directory: {report_dir}")

    candidates = _load_object(update_dir / "candidates.json")
    _require(candidates.get("schema_version") == 1, "candidates.schema_version must be 1")
    _require(
        "publication_date_start" in candidates,
        "Candidates must explicitly record their publication-date start",
    )
    _require(candidates.get("publication_date_start") == start, "Candidate start date mismatch")
    _require(candidates.get("publication_date_end") == end, "Candidate end date mismatch")
    articles = candidates.get("articles")
    _require(isinstance(articles, list), "candidates.articles must be an array")
    expected_map = screening.expected_contexts(candidates)
    expected = set(expected_map)
    raw_context_count = sum(
        len(article.get("candidate_contexts", []))
        for article in articles
        if isinstance(article, dict)
    )
    _require_count(candidates, "candidate_pmids", len(articles), "candidates")
    _require_count(candidates, "candidate_contexts", raw_context_count, "candidates")
    _require(
        len(expected) == raw_context_count,
        "Candidate packet contains duplicate or malformed context identifiers",
    )

    validation_status = _load_object(update_dir / "validation_status.json")
    _validate_validation_status(validation_status, start, end)
    for stage in ("first_screen", "second_screen", "full_text_review", "final_screening"):
        _require(
            not (update_dir / f"deepseek_{stage}.json").exists(),
            f"DeepSeek {stage} artifact must not exist while validation is pending",
        )

    packets: dict[str, dict[str, Any]] = {}
    final_rows: dict[str, dict[str, dict[str, Any]]] = {}
    for track in EXPECTED_MODELS:
        first = _load_object(update_dir / f"{track}_first_screen.json")
        first_rows = _validate_first(first, candidates, expected, track)
        second = _load_object(update_dir / f"{track}_second_screen.json")
        second_rows = _validate_second(second, candidates, first, first_rows, track)
        full_text = _load_object(update_dir / f"{track}_full_text_review.json")
        _validate_full_text(full_text, second_rows, track)
        lineage = _validate_lineage(candidates, first, second, full_text, track)
        final = _load_object(update_dir / f"{track}_final_screening.json")
        final_rows[track] = _validate_final(
            final, expected, track, start, end, lineage
        )
        packets.update(
            {
                f"{track}_first": first,
                f"{track}_second": second,
                f"{track}_full_text": full_text,
                f"{track}_final": final,
            }
        )

    first_alias = _load_object(update_dir / "first_screen.json")
    second_alias = _load_object(update_dir / "second_screen.json")
    full_alias = _load_object(update_dir / "full_text_review.json")
    _require(first_alias == packets["chatgpt_first"], "first_screen alias is not ChatGPT")
    _require(second_alias == packets["chatgpt_second"], "second_screen alias is not ChatGPT")

    pmc_status = _load_object(update_dir / "pmc_source_status.json")
    _require(pmc_status.get("schema_version") == 1, "pmc_source_status.schema_version must be 1")
    _require(
        isinstance(pmc_status.get("ncbi_requests"), int)
        and not isinstance(pmc_status.get("ncbi_requests"), bool)
        and pmc_status["ncbi_requests"] >= 0,
        "pmc_source_status.ncbi_requests must be a non-negative integer",
    )
    _require(isinstance(pmc_status.get("sources"), dict), "pmc_source_status.sources must be an object")
    reused_tracks = full_alias.get("reused_tracks")
    _require(
        isinstance(reused_tracks, list)
        and reused_tracks
        == [track for track in EXPECTED_MODELS if track in reused_tracks],
        "full_text_review.reused_tracks must be an ordered subset of the screening tracks",
    )
    alias_ncbi_requests = full_alias.get("ncbi_requests")
    _require(
        isinstance(alias_ncbi_requests, int)
        and not isinstance(alias_ncbi_requests, bool)
        and alias_ncbi_requests >= 0,
        "full_text_review.ncbi_requests must be a non-negative integer",
    )
    if reused_tracks == list(EXPECTED_MODELS):
        _require(
            alias_ncbi_requests == 0,
            "A fully reused full-text run must make zero NCBI requests",
        )
    else:
        _require(
            alias_ncbi_requests == pmc_status["ncbi_requests"],
            "full_text_review.ncbi_requests differs from pmc_source_status",
        )
    provider_alias = dict(full_alias)
    provider_alias.pop("ncbi_requests", None)
    provider_alias.pop("reused_tracks", None)
    _require(
        provider_alias == packets["chatgpt_full_text"],
        "full_text_review alias is not ChatGPT",
    )

    canonical = _load_object(update_dir / "final_screening.json")
    _require(canonical.get("canonical_track") == "chatgpt", "final_screening must be ChatGPT-canonical")
    canonical_without_marker = dict(canonical)
    canonical_without_marker.pop("canonical_track", None)
    _require(
        canonical_without_marker.pop("validation_status", None)
        == screening.DEEPSEEK_VALIDATION_STATUS,
        "final_screening must record pending DeepSeek validation",
    )
    _require(
        canonical_without_marker == packets["chatgpt_final"],
        "final_screening differs from the ChatGPT final packet",
    )

    exclusions = _load_object(update_dir / "exclusions.json")
    _require(exclusions.get("canonical_track") == "chatgpt", "exclusions must be ChatGPT-canonical")
    exclusion_records = exclusions.get("records")
    _require(isinstance(exclusion_records, list), "exclusions.records must be an array")
    _require_count(exclusions, "count", len(exclusion_records), "exclusions")
    exclusion_ids = [str(row.get("context_id") or "") for row in exclusion_records if isinstance(row, dict)]
    expected_exclusions = {
        context_id
        for context_id, row in final_rows["chatgpt"].items()
        if row.get("final_status") == "EXCLUDE"
    }
    _require(
        len(exclusion_ids) == len(set(exclusion_ids))
        and set(exclusion_ids) == expected_exclusions,
        "Exclusion ledger does not match the canonical final packet",
    )

    discrepancies = _load_object(update_dir / "discrepancies.json")
    _require(
        discrepancies.get("canonical_track") == "chatgpt",
        "discrepancies must resolve to ChatGPT",
    )
    _require(
        discrepancies.get("validation_track") == "deepseek"
        and discrepancies.get("validation_status")
        == screening.DEEPSEEK_VALIDATION_STATUS,
        "discrepancies must record pending DeepSeek validation",
    )
    discrepancy_records = discrepancies.get("records")
    _require(isinstance(discrepancy_records, list), "discrepancies.records must be an array")
    _require_count(discrepancies, "compared_contexts", 0, "discrepancies")
    _require_count(discrepancies, "pending_contexts", len(expected), "discrepancies")
    _require_count(discrepancies, "discrepancy_count", len(discrepancy_records), "discrepancies")
    discrepancy_ids = []
    for row in discrepancy_records:
        _require(isinstance(row, dict), "Every discrepancy must be an object")
        context_id = str(row.get("context_id") or "")
        _require(context_id in expected, f"Unexpected discrepancy context: {context_id}")
        _require(row.get("canonical_resolution") == "chatgpt", "Discrepancy resolution must be ChatGPT")
        _require(isinstance(row.get("different_fields"), list) and row["different_fields"], "Discrepancy must list changed fields")
        discrepancy_ids.append(context_id)
    _require(len(discrepancy_ids) == len(set(discrepancy_ids)), "Duplicate discrepancy context")

    out_of_window = _load_object(update_dir / "out_of_window_records.json")
    _require(isinstance(out_of_window.get("records"), list), "out_of_window_records.records must be an array")
    _require_count(out_of_window, "count", len(out_of_window["records"]), "out_of_window_records")
    subtype_review = _load_object(update_dir / "exposure_subtype_review.json")
    _require(isinstance(subtype_review.get("records"), list), "exposure_subtype_review.records must be an array")
    applied = _load_object(update_dir / "applied_changes.json")
    _require(isinstance(applied.get("changes"), list), "applied_changes.changes must be an array")

    for filename in REQUIRED_REPORTS:
        path = report_dir / filename
        _require(path.is_file() and path.stat().st_size > 0, f"Missing report: {path}")

    return {
        "run": run_name,
        "update_dir": str(update_dir),
        "report_dir": str(report_dir),
        "candidate_pmids": len(articles),
        "candidate_contexts": len(expected),
        "models": EXPECTED_MODELS,
        "protocol_version": EXPECTED_PROTOCOL_VERSION,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--updates-root", type=Path, default=Path("data/monthly_updates"))
    parser.add_argument("--reports-root", type=Path, default=Path("data/monthly_reports"))
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument(
        "--all-history-through",
        metavar="YYYY-MM-DD",
        help=(
            "validate the all_history_through_YYYY-MM-DD run whose durable "
            "publication-date start is null"
        ),
    )
    args = parser.parse_args()
    if args.all_history_through and (args.start_date or args.end_date):
        parser.error(
            "--all-history-through cannot be combined with --start-date or --end-date"
        )
    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be provided together")
    if args.all_history_through:
        try:
            date.fromisoformat(args.all_history_through)
        except ValueError:
            parser.error("--all-history-through must be an ISO date (YYYY-MM-DD)")
        args.start_date = None
        args.end_date = args.all_history_through
    elif not args.start_date:
        args.start_date, args.end_date = previous_month_window()
    return args


def main() -> int:
    args = parse_args()
    summary = validate_run(
        args.updates_root,
        args.reports_root,
        args.start_date,
        args.end_date,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ArtifactValidationError as exc:
        raise SystemExit(f"Monthly artifact validation failed: {exc}") from exc
