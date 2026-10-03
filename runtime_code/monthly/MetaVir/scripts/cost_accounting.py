"""Durable GPT token-usage accounting for MetaVir screening runs.

The Cornell gateway returns token counts on each successful model response.
Screening packets retain those counts, including an allocation to exposures
when one API call contains contexts for more than one exposure.  This module
turns the retained counts into a human-readable, resumable ``cost.txt``.

The dollar values are list-price estimates, not Cornell invoices.  In
particular, a gateway may apply institutional pricing that differs from the
rates supplied for this project.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


MODEL_RATES_USD_PER_MILLION: dict[str, dict[str, float]] = {
    "gpt-5.6-luna": {
        "input": 0.20,
        "cached_input": 0.02,
        "output": 1.20,
    },
    "gpt-5.6-sol": {
        "input": 5.00,
        "cached_input": 0.50,
        "output": 30.00,
    },
}

STAGES = (
    ("first_screen", "First title/abstract/MeSH screen"),
    ("second_screen", "Detailed title/abstract/MeSH screen"),
    ("full_text_review", "PMC full-text review"),
)


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def canonical_model(model: Any) -> str | None:
    """Resolve gateway-qualified model names to a supported priced model."""
    value = str(model or "").strip().casefold()
    for priced_model in MODEL_RATES_USD_PER_MILLION:
        if value == priced_model or value.endswith("/" + priced_model):
            return priced_model
    return None


def estimated_cost_usd(usage: dict[str, Any], model: Any = None) -> float:
    """Estimate list-price cost from actual response usage-token counts.

    ``cached_input_tokens`` is treated as a subset of ``input_tokens``, which
    matches the OpenAI/LiteLLM usage schema.  Reasoning tokens are already part
    of output tokens and are therefore not charged a second time.
    """
    priced_model = canonical_model(model or usage.get("model"))
    if priced_model is None:
        return 0.0
    rates = MODEL_RATES_USD_PER_MILLION[priced_model]
    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    cached_tokens = min(
        input_tokens,
        max(0, int(usage.get("cached_input_tokens") or 0)),
    )
    uncached_tokens = input_tokens - cached_tokens
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    return (
        uncached_tokens * rates["input"]
        + cached_tokens * rates["cached_input"]
        + output_tokens * rates["output"]
    ) / 1_000_000


def configured_exposures(repo_root: Path) -> list[str]:
    values = load_json(repo_root / "static" / "exposures.json", [])
    if not isinstance(values, list):
        return []
    return sorted(
        {str(value).strip() for value in values if str(value).strip()},
        key=str.casefold,
    )


def additional_configured_exposures(repo_root: Path) -> list[str]:
    """Return the collision-checked V1 + V2 additional catalog."""
    names: dict[str, tuple[str, Path]] = {}
    for path in (
        repo_root / "data" / "additionalExposure.json",
        repo_root / "data" / "additionalExposureV2.json",
    ):
        values = load_json(path, {})
        if isinstance(values, dict):
            candidates = values
        elif isinstance(values, list):
            candidates = {
                str(item.get("exposure") or item.get("name") or "").strip()
                for item in values
                if isinstance(item, dict)
            }
        else:
            candidates = set()
        for raw_name in candidates:
            name = str(raw_name).strip()
            if not name:
                continue
            key = name.casefold()
            if key in names:
                prior_name, prior_path = names[key]
                raise ValueError(
                    "Additional exposure catalogs duplicate canonical exposure "
                    f"{name!r}: {prior_path} ({prior_name!r}) and {path}"
                )
            names[key] = (name, path)
    return sorted(
        (name for name, _ in names.values()),
        key=str.casefold,
    )


def candidate_exposures(output_dir: Path) -> list[str]:
    packet = load_json(output_dir / "candidates.json", {})
    values = {
        str(context.get("exposure") or "").strip()
        for article in packet.get("articles", [])
        for context in article.get("candidate_contexts", [])
        if str(context.get("exposure") or "").strip()
    }
    return sorted(values, key=str.casefold)


def _stage_usage(output_dir: Path, stage: str) -> dict[str, Any]:
    """Prefer an in-progress checkpoint so the report updates during a run."""
    progress = output_dir / f"chatgpt_{stage}_progress.json"
    completed = output_dir / f"chatgpt_{stage}.json"
    if progress.is_file():
        return load_json(progress, {}).get("usage") or {}
    return load_json(completed, {}).get("usage") or {}


def collect_usage_rows(output_dir: Path) -> list[dict[str, Any]]:
    """Collect stage/model/exposure usage from durable screening checkpoints."""
    rows: list[dict[str, Any]] = []
    for stage, stage_label in STAGES:
        usage = _stage_usage(output_dir, stage)
        model = canonical_model(usage.get("model"))
        if model is None:
            continue
        by_exposure = usage.get("by_exposure") or {}
        if not isinstance(by_exposure, dict):
            continue
        for exposure, raw in by_exposure.items():
            if not isinstance(raw, dict):
                continue
            row = {
                "exposure": str(exposure),
                "stage": stage,
                "stage_label": stage_label,
                "model": model,
                "calls_touching_exposure": int(raw.get("calls") or 0),
                "input_tokens": int(raw.get("input_tokens") or 0),
                "cached_input_tokens": int(raw.get("cached_input_tokens") or 0),
                "output_tokens": int(raw.get("output_tokens") or 0),
                "reasoning_tokens": int(raw.get("reasoning_tokens") or 0),
            }
            row["estimated_cost_usd"] = estimated_cost_usd(row, model)
            rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            row["exposure"].casefold(),
            row["model"],
            row["stage"],
        ),
    )


def _fmt_usd(value: float) -> str:
    return f"${value:.6f}"


def _publication_window_label(candidates: dict[str, Any]) -> str:
    """Describe bounded and all-history candidate-packet windows clearly."""
    if "publication_date_start" not in candidates:
        start = "not started"
    else:
        raw_start = candidates.get("publication_date_start")
        start = (
            str(raw_start).strip()
            if raw_start is not None and str(raw_start).strip()
            else "no lower publication-date limit"
        )
    end = str(candidates.get("publication_date_end") or "not started").strip()
    return f"{start} through {end}"


def render_cost_report(
    output_dir: Path,
    repo_root: Path,
    *,
    updated_at: str | None = None,
) -> str:
    """Render all configured exposures, including zero-cost/no-candidate rows."""
    candidates = load_json(output_dir / "candidates.json", {})
    candidate_set = set(candidate_exposures(output_dir))
    rows = collect_usage_rows(output_dir)
    exposures = configured_exposures(repo_root)
    exposures = sorted(
        set(exposures) | candidate_set | {row["exposure"] for row in rows},
        key=str.casefold,
    )

    costs: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (row["exposure"], row["model"])
        costs[key] = costs.get(key, 0.0) + row["estimated_cost_usd"]

    timestamp = updated_at or datetime.now().astimezone().isoformat()
    complete = (output_dir / "final_screening.json").is_file() and (
        output_dir / "applied_changes.json"
    ).is_file()
    lines = [
        "MetaVir GPT screening cost by exposure",
        "=======================================",
        f"Run: {_publication_window_label(candidates)}",
        f"Updated: {timestamp}",
        (
            "Status: complete; final screening and cache application are recorded."
            if complete
            else "Status: in progress; this file is rewritten after each retained API response."
        ),
        "",
        "ESTIMATE CAVEAT: Dollar amounts use the supplied public list prices and may "
        "differ from the amount billed by the Cornell gateway. Token counts come from "
        "successful API response usage metadata. For a shared multi-exposure call, "
        "tokens are allocated by the number of contexts for each exposure; allocations "
        "preserve the call's exact total tokens.",
        "",
        "Rates (USD per 1 million tokens)",
        "--------------------------------",
        "Model | Input | Cached input | Output",
        "gpt-5.6-luna | $0.20 | $0.02 | $1.20",
        "gpt-5.6-sol | $5.00 | $0.50 | $30.00",
        "",
        "Per-exposure estimated cost",
        "---------------------------",
        "Exposure | Candidate this run | Luna | Sol | Total",
    ]
    for exposure in exposures:
        luna = costs.get((exposure, "gpt-5.6-luna"), 0.0)
        sol = costs.get((exposure, "gpt-5.6-sol"), 0.0)
        lines.append(
            f"{exposure} | {'yes' if exposure in candidate_set else 'no'} | "
            f"{_fmt_usd(luna)} | {_fmt_usd(sol)} | {_fmt_usd(luna + sol)}"
        )

    total_luna = sum(
        value for (exposure, model), value in costs.items() if model == "gpt-5.6-luna"
    )
    total_sol = sum(
        value for (exposure, model), value in costs.items() if model == "gpt-5.6-sol"
    )
    lines.extend(
        [
            "",
            "Run totals",
            "----------",
            f"Luna: {_fmt_usd(total_luna)}",
            f"Sol: {_fmt_usd(total_sol)}",
            f"Combined: {_fmt_usd(total_luna + total_sol)}",
            "",
            "Detailed actual-token allocation",
            "--------------------------------",
            "Exposure | Stage | Model | Calls touching exposure | Input tokens | "
            "Cached input tokens | Output tokens | Reasoning tokens | Estimated cost",
        ]
    )
    if rows:
        for row in rows:
            lines.append(
                f"{row['exposure']} | {row['stage_label']} | {row['model']} | "
                f"{row['calls_touching_exposure']} | {row['input_tokens']} | "
                f"{row['cached_input_tokens']} | {row['output_tokens']} | "
                f"{row['reasoning_tokens']} | {_fmt_usd(row['estimated_cost_usd'])}"
            )
    else:
        lines.append("No GPT API responses have been recorded for this run yet.")

    lines.extend(
        [
            "",
            "Cached input tokens are a subset of input tokens and are charged at the "
            "cached-input rate; reasoning tokens are included in output tokens and are "
            "not charged twice.",
            "No API keys or secret values are written to this file.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_additional_exposure_cost_report(
    output_dir: Path,
    repo_root: Path,
    *,
    updated_at: str | None = None,
) -> str:
    """Render exactly one cost row for every additional atomic exposure."""
    rows = collect_usage_rows(output_dir)
    exposures = additional_configured_exposures(repo_root)
    costs: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (row["exposure"], row["model"])
        costs[key] = costs.get(key, 0.0) + row["estimated_cost_usd"]
    timestamp = updated_at or datetime.now().astimezone().isoformat()
    lines = [
        "MetaVir additional-exposure GPT cost",
        "====================================",
        f"Run: {_publication_window_label(load_json(output_dir / 'candidates.json', {}))}",
        f"Updated: {timestamp}",
        "Exposure | Luna | Sol | Total",
    ]
    for exposure in exposures:
        luna = costs.get((exposure, "gpt-5.6-luna"), 0.0)
        sol = costs.get((exposure, "gpt-5.6-sol"), 0.0)
        lines.append(
            f"{exposure} | {_fmt_usd(luna)} | {_fmt_usd(sol)} | "
            f"{_fmt_usd(luna + sol)}"
        )
    return "\n".join(lines) + "\n"


def write_additional_exposure_cost_report(
    output_dir: Path,
    repo_root: Path,
    report_path: Path | None = None,
) -> Path:
    """Atomically rewrite the dedicated additional-exposure ledger."""
    path = report_path or repo_root / "additionalExposureCost.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        render_additional_exposure_cost_report(output_dir, repo_root),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def write_cost_report(
    output_dir: Path,
    repo_root: Path,
    report_path: Path | None = None,
) -> Path:
    """Atomically rewrite a run's cost report and return its path."""
    path = report_path or repo_root / "cost.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(render_cost_report(output_dir, repo_root), encoding="utf-8")
    temporary.replace(path)
    return path


def find_repo_root(output_dir: Path) -> Path | None:
    """Find MetaVir's root without making temp-directory tests modify the repo."""
    for candidate in (output_dir, *output_dir.parents):
        if (candidate / "static" / "exposures.json").is_file() and (
            candidate / "scripts" / "dual_screening.py"
        ).is_file():
            return candidate
    return None


def update_cost_report_for_run(output_dir: Path) -> Path:
    """Update root ``cost.txt`` in production or a local report in test runs."""
    repo_root = find_repo_root(output_dir)
    if repo_root is None:
        # A standalone run still receives a durable report beside its packets.
        return write_cost_report(output_dir, output_dir, output_dir / "cost.txt")
    path = write_cost_report(output_dir, repo_root)
    write_additional_exposure_cost_report(output_dir, repo_root)
    return path
