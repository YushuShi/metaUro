"""Durable GPT token-usage accounting for MetaPee screening runs.

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
LONG_CONTEXT_INPUT_TOKEN_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULTIPLIER = 2.0
LONG_CONTEXT_OUTPUT_MULTIPLIER = 1.5

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
    long_input_tokens = min(
        input_tokens,
        max(0, int(usage.get("long_context_input_tokens") or 0)),
    )
    long_cached_tokens = min(
        cached_tokens,
        long_input_tokens,
        max(0, int(usage.get("long_context_cached_input_tokens") or 0)),
    )
    long_uncached_tokens = long_input_tokens - long_cached_tokens
    regular_cached_tokens = cached_tokens - long_cached_tokens
    regular_uncached_tokens = uncached_tokens - long_uncached_tokens
    long_output_tokens = min(
        output_tokens,
        max(0, int(usage.get("long_context_output_tokens") or 0)),
    )
    regular_output_tokens = output_tokens - long_output_tokens
    return (
        regular_uncached_tokens * rates["input"]
        + regular_cached_tokens * rates["cached_input"]
        + regular_output_tokens * rates["output"]
        + long_uncached_tokens * rates["input"] * LONG_CONTEXT_INPUT_MULTIPLIER
        + long_cached_tokens * rates["cached_input"] * LONG_CONTEXT_INPUT_MULTIPLIER
        + long_output_tokens * rates["output"] * LONG_CONTEXT_OUTPUT_MULTIPLIER
    ) / 1_000_000


def _additional_exposure_names(repo_root: Path) -> list[str]:
    names: dict[str, tuple[str, Path]] = {}
    reserved = {"version", "schema_version", "description", "hierarchy", "edges"}
    for path in (
        repo_root / "data" / "additionalExposure.json",
        repo_root / "data" / "additionalExposureV2.json",
    ):
        raw = load_json(path, {})
        if isinstance(raw, dict) and isinstance(raw.get("exposures"), (list, dict)):
            raw = raw["exposures"]
        candidates: list[str] = []
        if isinstance(raw, dict):
            candidates.extend(
                str(name).strip() for name in raw if str(name) not in reserved
            )
        elif isinstance(raw, list):
            for value in raw:
                if isinstance(value, str):
                    candidates.append(value.strip())
                elif isinstance(value, dict):
                    candidates.append(
                        str(
                            value.get("exposure")
                            or value.get("name")
                            or value.get("canonical_name")
                            or ""
                        ).strip()
                    )
        for name in candidates:
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
    return [name for name, _ in names.values()]


def run_exposure_scope(output_dir: Path) -> str:
    scope = str(
        load_json(output_dir / "candidates.json", {}).get("exposure_scope")
        or "current"
    )
    return scope if scope in {"current", "additional", "all"} else "current"


def cost_report_filename(output_dir: Path) -> str:
    return (
        "additionalExposureCost.txt"
        if run_exposure_scope(output_dir) == "additional"
        else "cost.txt"
    )


def configured_exposures(repo_root: Path, scope: str = "current") -> list[str]:
    values = load_json(repo_root / "static" / "exposures.json", [])
    if not isinstance(values, list):
        values = []
    current = {str(value).strip() for value in values if str(value).strip()}
    additional = set(_additional_exposure_names(repo_root))
    selected = {
        "current": current,
        "additional": additional,
        "all": current | additional,
    }.get(scope, current)
    return sorted(
        selected,
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
                "long_context_calls": int(raw.get("long_context_calls") or 0),
                "long_context_input_tokens": int(
                    raw.get("long_context_input_tokens") or 0
                ),
                "long_context_cached_input_tokens": int(
                    raw.get("long_context_cached_input_tokens") or 0
                ),
                "long_context_output_tokens": int(
                    raw.get("long_context_output_tokens") or 0
                ),
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
    if not candidates:
        return "not started"
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
    exposure_scope: str | None = None,
) -> str:
    """Render all configured exposures, including zero-cost/no-candidate rows."""
    candidates = load_json(output_dir / "candidates.json", {})
    candidate_set = set(candidate_exposures(output_dir))
    rows = collect_usage_rows(output_dir)
    selected_scope = exposure_scope or run_exposure_scope(output_dir)
    configured = configured_exposures(repo_root, selected_scope)
    if selected_scope == "additional":
        allowed = {name.casefold() for name in configured}
        candidate_set = {
            name for name in candidate_set if name.casefold() in allowed
        }
        rows = [
            row for row in rows
            if str(row.get("exposure") or "").casefold() in allowed
        ]
    exposures = configured
    exposures = sorted(
        set(exposures) | candidate_set | {row["exposure"] for row in rows},
        key=str.casefold,
    )

    costs: dict[tuple[str, str], float] = {}
    tokens: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        key = (row["exposure"], row["model"])
        costs[key] = costs.get(key, 0.0) + row["estimated_cost_usd"]
        token_totals = tokens.setdefault(
            key,
            {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
            },
        )
        for field in token_totals:
            token_totals[field] += int(row.get(field) or 0)

    timestamp = updated_at or datetime.now().astimezone().isoformat()
    complete = (output_dir / "final_screening.json").is_file() and (
        output_dir / "applied_changes.json"
    ).is_file()
    started = (output_dir / "candidates.json").is_file() or bool(rows)
    if complete:
        status = "Status: complete; final screening and cache application are recorded."
    elif started:
        status = (
            "Status: in progress; this file is rewritten after each retained API response."
        )
    else:
        status = "Status: not started; no GPT API responses have been recorded."
    lines = [
        "MetaPee GPT screening cost by exposure",
        "=======================================",
        f"Run: {_publication_window_label(candidates)}",
        f"Updated: {timestamp}",
        status,
        "",
        "ESTIMATE CAVEAT: Dollar amounts use the supplied public list prices and may "
        "differ from the amount billed by the Cornell gateway. Token counts come from "
        "successful API response usage metadata. For a shared multi-exposure call, "
        "tokens are allocated by the number of contexts for each exposure; allocations "
        "preserve the call's exact total tokens.",
        (
            "Only usage present in retained screening checkpoints can be reconstructed; "
            "usage that existed solely in a superseded checkpoint is not included."
        ),
        (
            "Cache writes are priced at 1.25x uncached input, but the retained gateway "
            "usage schema does not report cache-write tokens; that unreported charge "
            "is therefore not included in these estimates."
        ),
        "",
        "Rates (USD per 1 million tokens)",
        "--------------------------------",
        "Model | Input | Cached input | Output",
        "gpt-5.6-luna | $0.20 | $0.02 | $1.20",
        "gpt-5.6-sol | $5.00 | $0.50 | $30.00",
        (
            "For a call with more than 272,000 input tokens, retained usage is "
            "costed at 2x input/cached-input and 1.5x output rates."
        ),
        "",
        "Per-exposure estimated cost",
        "---------------------------",
        "Exposure | Candidate this run | Luna cost | Sol cost | Total cost | "
        "Luna input tokens | Luna cached input tokens | Luna output tokens | "
        "Luna total tokens | Sol input tokens | Sol cached input tokens | "
        "Sol output tokens | Sol total tokens",
    ]
    for exposure in exposures:
        luna = costs.get((exposure, "gpt-5.6-luna"), 0.0)
        sol = costs.get((exposure, "gpt-5.6-sol"), 0.0)
        luna_tokens = tokens.get((exposure, "gpt-5.6-luna"), {})
        sol_tokens = tokens.get((exposure, "gpt-5.6-sol"), {})
        luna_input = int(luna_tokens.get("input_tokens") or 0)
        luna_cached = int(luna_tokens.get("cached_input_tokens") or 0)
        luna_output = int(luna_tokens.get("output_tokens") or 0)
        sol_input = int(sol_tokens.get("input_tokens") or 0)
        sol_cached = int(sol_tokens.get("cached_input_tokens") or 0)
        sol_output = int(sol_tokens.get("output_tokens") or 0)
        lines.append(
            f"{exposure} | {'yes' if exposure in candidate_set else 'no'} | "
            f"{_fmt_usd(luna)} | {_fmt_usd(sol)} | {_fmt_usd(luna + sol)} | "
            f"{luna_input} | {luna_cached} | {luna_output} | "
            f"{luna_input + luna_output} | {sol_input} | {sol_cached} | "
            f"{sol_output} | {sol_input + sol_output}"
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
            "Cached input tokens | Output tokens | Reasoning tokens | "
            "Long-context calls | Estimated cost",
        ]
    )
    if rows:
        for row in rows:
            lines.append(
                f"{row['exposure']} | {row['stage_label']} | {row['model']} | "
                f"{row['calls_touching_exposure']} | {row['input_tokens']} | "
                f"{row['cached_input_tokens']} | {row['output_tokens']} | "
                f"{row['reasoning_tokens']} | {row['long_context_calls']} | "
                f"{_fmt_usd(row['estimated_cost_usd'])}"
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


def write_cost_report(
    output_dir: Path,
    repo_root: Path,
    report_path: Path | None = None,
    *,
    exposure_scope: str | None = None,
) -> Path:
    """Atomically rewrite a run's cost report and return its path."""
    path = report_path or repo_root / cost_report_filename(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        render_cost_report(
            output_dir,
            repo_root,
            exposure_scope=exposure_scope,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def find_repo_root(output_dir: Path) -> Path | None:
    """Find MetaPee's root without making temp-directory tests modify the repo."""
    for candidate in (output_dir, *output_dir.parents):
        if (candidate / "static" / "exposures.json").is_file() and (
            candidate / "scripts" / "dual_screening.py"
        ).is_file():
            return candidate
    return None


def update_cost_report_for_run(output_dir: Path) -> Path:
    """Update the current- or additional-catalog cost ledger for this run."""
    repo_root = find_repo_root(output_dir)
    if repo_root is None:
        # A standalone run still receives a durable report beside its packets.
        return write_cost_report(
            output_dir,
            output_dir,
            output_dir / cost_report_filename(output_dir),
        )
    return write_cost_report(output_dir, repo_root)
