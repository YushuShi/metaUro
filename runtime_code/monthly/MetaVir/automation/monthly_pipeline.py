"""Incremental monthly screening orchestration; existing scientific engines stay authoritative.

The scheduler commits the validated run, review CSV, UI products and successful
watermark together. A failed search, screening call, UI build or validation never
advances that watermark. Human-review holds are separate from operational errors.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

VERSION = "monthly-incremental-v1"
STAGES = ("discover", "first-screen", "second-screen", "full-text", "finalize", "report")
STATE_PATH = "data/monthly_automation/state.json"
ACTIVE_PATH = "data/monthly_automation/active_run.json"
GATE_FILENAME = "monthly_application_gate.json"
MANIFEST_FILENAME = "monthly_run_manifest.json"
RAW_FILENAMES = (
    "monthly_parallel_comparison.json",
    "candidates.json", "first_screen.json", "second_screen.json", "full_text_review.json",
    "chatgpt_first_screen.json", "chatgpt_second_screen.json", "chatgpt_full_text_review.json",
    "final_screening.json", "exclusions.json", "full_text_sources.json",
    "full_text_source_manifest.json",
    "chatgpt_second.json", "claude_second.json", "chatgpt_full_text.json", "gemini_full_text.json",
    "chatgpt/final_screening.json", "claude_gemini/final_screening.json",
    "monthly_survival_baseline.json", "monthly_survival_ncbi_discovery.json",
    "monthly_survival_initial_comparison.json",
)
REQUIRED_SECRETS = ("NCBI_EMAIL", "CORNELL_API_KEY", "CORNELL_API_BASE_URL")


class PipelineError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"Cannot read a valid JSON object: {path.name}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"Expected a JSON object: {path.name}")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def within_repo(root: Path, value: str | Path) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise PipelineError("Automation paths must stay within this repository")
    return path


def iso_date(value: Any, label: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise PipelineError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PipelineError(f"{label} is not a real calendar date") from exc


def validate_config(config: dict[str, Any]) -> None:
    if not str(config.get("repository") or "").strip():
        raise PipelineError("Configuration requires repository")
    bootstrap = config.get("bootstrap")
    if not isinstance(bootstrap, dict):
        raise PipelineError("Configuration requires explicit bootstrap date and provenance")
    iso_date(bootstrap.get("last_successful_publication_date"), "bootstrap last successful publication date")
    if not str(bootstrap.get("provenance") or "").strip():
        raise PipelineError("Bootstrap search cutoff requires recorded provenance")
    discovery = config.get("discovery_args", [])
    if not isinstance(discovery, list) or not all(isinstance(value, str) for value in discovery):
        raise PipelineError("discovery_args must be an argv list")
    forbidden = {"--force", "--previous-month", "--start-date", "--end-date", "--all-history-through", "--stage", "--output-dir", "--report-dir", "--exposure", "--full-rescreen", "--revision-aware", "--revision-rescreen", "--revision-reclassification"}
    if forbidden.intersection(discovery):
        raise PipelineError("Discovery arguments may select full catalogs, not replace the incremental window or rescreen saved records")
    for key in ("validation_commands", "ui_commands"):
        commands = config.get(key, [])
        if not isinstance(commands, list) or not all(isinstance(command, list) and command and all(isinstance(arg, str) and arg for arg in command) for command in commands):
            raise PipelineError(f"{key} must contain nonempty argv lists")
    if not config.get("validation_commands"):
        raise PipelineError("At least one meaningful validation command is required before release")


def load_config(root: Path, path: str | Path) -> dict[str, Any]:
    config = read_json(within_repo(root, path))
    validate_config(config)
    return config


def publication_window(root: Path, config: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Search complete UTC publication days, resuming any interrupted frozen window.

    On the 28th at 06:23 UTC, publications later that same day have not appeared
    yet. Ending on the 27th keeps the full 28th in the following search interval.
    """
    today = today or datetime.now(timezone.utc).date()
    last_complete_day = today - timedelta(days=1)
    state_path = within_repo(root, config.get("state_file", STATE_PATH))
    state = read_json(state_path) if state_path.exists() else None
    if state is not None:
        if state.get("repository") != config["repository"] or state.get("status") != "completed":
            raise PipelineError("Successful-search state has wrong repository or incomplete status")
        cutoff = iso_date(state.get("last_successful_publication_date"), "successful publication watermark")
        provenance = {"kind": "successful_release", "run_id": state.get("run_id"), "manifest_sha256": state.get("manifest_sha256")}
    else:
        cutoff = iso_date(config["bootstrap"]["last_successful_publication_date"], "bootstrap publication cutoff")
        provenance = {"kind": "bootstrap", "provenance": config["bootstrap"]["provenance"]}
    if cutoff > last_complete_day:
        raise PipelineError("The successful publication watermark is in the future or covers an incomplete UTC day")
    start = cutoff + timedelta(days=1)
    active_path = within_repo(root, config.get("active_file", ACTIVE_PATH))
    if active_path.exists():
        active = read_json(active_path)
        active_start = iso_date(active.get("start_date"), "active start date")
        active_end = iso_date(active.get("end_date"), "active end date")
        if active.get("repository") != config["repository"]:
            raise PipelineError("Active run belongs to a different repository")
        if active_end > cutoff:
            if active_start != start or active_end < active_start or active_end > last_complete_day:
                raise PipelineError("Interrupted run does not continue the successful search watermark")
            return {"start_date": str(active_start), "end_date": str(active_end), "run_id": f"{active_start}_{active_end}", "resumed": True, "watermark_source": provenance, "publication_cutoff_policy": "last_complete_utc_day", "no_work": False}
    return {"start_date": str(start), "end_date": str(last_complete_day), "run_id": f"{start}_{last_complete_day}", "resumed": False, "watermark_source": provenance, "publication_cutoff_policy": "last_complete_utc_day", "no_work": start > last_complete_day}


def run_paths(root: Path, window: dict[str, Any]) -> tuple[Path, Path]:
    return (within_repo(root, Path("data/monthly_updates") / window["run_id"]), within_repo(root, Path("data/monthly_reports") / window["run_id"]))


def context_id(record: dict[str, Any]) -> str:
    """Native context identifiers are carried unchanged through every application."""
    value = record.get("context_id")
    if value is not None and str(value).strip():
        return str(value)
    # This is deliberately the same compatibility shape used by adjudication.
    return "|".join(str(record.get(key) or "") for key in ("pmid", "exposure")) + "|" + str(record.get("cancer") or record.get("disease") or "")


def filter_application_records(records: list[dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
    """An application-only veto, called AFTER native raw-packet validation.

    No canonical results, model seals, human decisions or existing cache rows are
    rewritten. Without a monthly gate the repository's existing CLI is unchanged.
    """
    path = Path(output_dir) / GATE_FILENAME
    if not path.exists():
        if os.environ.get("MONTHLY_AUTOMATION_REQUIRE_GATE") == "true":
            raise PipelineError("Monthly application requires an adjudicated application gate")
        return records
    gate = read_json(path)
    if gate.get("schema_version") != 1 or gate.get("policy") != "human_review_before_disputed_application":
        raise PipelineError("Unknown monthly application gate policy")
    source = Path(output_dir) / "final_screening.json"
    if not source.exists() or gate.get("source_final_screening_sha256") != sha256(source):
        raise PipelineError("Application gate is stale: final screening packet changed")
    blocked = gate.get("blocked_context_ids")
    if not isinstance(blocked, list) or not all(isinstance(value, str) and value for value in blocked) or len(blocked) != len(set(blocked)):
        raise PipelineError("Malformed blocked context identifiers")
    blocked_ids = set(blocked)
    kept = [record for record in records if context_id(record) not in blocked_ids]
    # A separate receipt makes deferred application explicit without altering the
    # pipeline's canonical applied_changes or screening-artifact seals.
    atomic_json(Path(output_dir) / "monthly_application_filter.json", {
        "schema_version": 1, "source_final_screening_sha256": gate["source_final_screening_sha256"],
        "gate_sha256": sha256(path), "candidate_application_records": len(records),
        "accepted_application_records": len(kept),
        "deferred_context_ids": sorted({context_id(record) for record in records if context_id(record) in blocked_ids}),
        "held_context_ids": sorted(blocked_ids), "raw_screening_packets_unchanged": True,
    })
    return kept


def packet_hashes(run_dir: Path) -> dict[str, str]:
    return {name: sha256(run_dir / name) for name in RAW_FILENAMES if (run_dir / name).is_file()}


def verify_packets(run_dir: Path, expected: dict[str, str]) -> None:
    for name, expected_sha in expected.items():
        path = run_dir / name
        if not path.exists() or sha256(path) != expected_sha:
            raise PipelineError(f"Raw screening artifact changed after finalization: {name}")


def render_command(command: list[str], root: Path, window: dict[str, Any], run_dir: Path, reports_dir: Path) -> list[str]:
    values = {"python": sys.executable, "repo_root": str(root), "run_dir": str(run_dir), "reports_dir": str(reports_dir), **{key: window[key] for key in ("start_date", "end_date", "run_id")}}
    try:
        return [argument.format_map(values) for argument in command]
    except (KeyError, ValueError) as exc:
        raise PipelineError("Unknown or malformed command template placeholder") from exc


def native_command(root: Path, config: dict[str, Any], window: dict[str, Any], stage: str) -> list[str]:
    run_dir, reports_dir = run_paths(root, window)
    result = [sys.executable, str(within_repo(root, config.get("monthly_script", "scripts/monthly_evidence_update.py"))), "--start-date", window["start_date"], "--end-date", window["end_date"], "--output-dir", str(run_dir), "--report-dir", str(reports_dir), "--stage", stage]
    if stage == "discover":
        result.extend(config.get("discovery_args", []))
    return result


def normalized_environment(environ: dict[str, str]) -> dict[str, str]:
    """Resolve credential aliases without requiring the optional NCBI key."""
    environment = dict(environ)
    aliases = {
        "NCBI_EMAIL": ("NCBI_EMAIL", "PUBMED_EMAIL", "ENTREZ_EMAIL"),
        "NCBI_API_KEY": ("NCBI_API_KEY", "PUBMED_API_KEY", "ENTREZ_API_KEY"),
        "CORNELL_API_KEY": ("CORNELL_API_KEY",),
        "CORNELL_API_BASE_URL": ("CORNELL_API_BASE_URL", "API_BASE_URI"),
    }
    for canonical, names in aliases.items():
        values = [str(environment.get(name) or "").strip() for name in names]
        environment[canonical] = next((value for value in values if value), "")
    # Older shells/workflows must not turn an optional throughput key into a
    # prerequisite for discovering evidence through the public endpoint.
    environment.pop("MONTHLY_REQUIRE_VALID_NCBI_KEY", None)
    return environment


def preflight(root: Path, config: dict[str, Any], *, environ: dict[str, str] | None = None, today: date | None = None) -> dict[str, Any]:
    """Read-only structural/credential-presence check; never sends a model request."""
    validate_config(config)
    environment = normalized_environment(os.environ if environ is None else environ)
    missing = [name for name in REQUIRED_SECRETS if not environment.get(name, "").strip()]
    script = within_repo(root, config.get("monthly_script", "scripts/monthly_evidence_update.py"))
    problems = []
    if not script.is_file():
        problems.append(f"Missing screening engine: {script.relative_to(root)}")
    else:
        source = script.read_text(encoding="utf-8")
        if "filter_application_records" not in source:
            problems.append("Screening engine lacks the monthly application-only review gate")
        if not config.get("discovery_args") and ('"--catalog"' in source or '"--exposure-scope"' in source):
            problems.append("Full-catalog discovery arguments are required")
    window = publication_window(root, config, today)
    run_dir, reports_dir = run_paths(root, window)
    for commands in (config.get("ui_commands", []), config["validation_commands"]):
        for command in commands:
            render_command(command, root, window, run_dir, reports_dir)
    return {"repository": config["repository"], "window": window, "ready": not missing and not problems, "missing_secret_names": missing, "problems": problems, "network_calls": 0, "preprint_recovery_models": ["gpt-6-luna", "gemini-3.8-flash", "gpt-6-astra"]}


def default_runner(argv: list[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(argv, cwd=cwd, env=env, check=True)


def write_status(root: Path, config: dict[str, Any], manifest: dict[str, Any]) -> None:
    status = {key: manifest.get(key) for key in ("repository", "run_id", "start_date", "end_date", "status", "created_at", "validated_at", "completed_at", "review")}
    status["schema_version"] = 1
    status["reports_path"] = f"data/monthly_reports/{manifest['run_id']}"
    atomic_json(within_repo(root, config.get("status_file", "static/monthly_update_status.json")), status)


def run_pipeline(root: Path, config: dict[str, Any], *, runner: Callable[..., Any] = default_runner, adjudicator: Callable[..., Any] | None = None, parallel_reviewer: Callable[..., Any] | None = None, ui_refresher: Callable[..., Any] | None = None, today: date | None = None, check_credentials: bool = True) -> dict[str, Any]:
    """Prepare a fully validated release. --finalize-state commits its watermark locally."""
    root = root.resolve()
    validate_config(config)
    check = preflight(root, config, today=today)
    if check["problems"] or check_credentials and check["missing_secret_names"]:
        problems = list(check["problems"])
        if check_credentials and check["missing_secret_names"]:
            problems.append("Missing secret names: " + ", ".join(check["missing_secret_names"]))
        raise PipelineError("Monthly preflight failed: " + "; ".join(problems))
    window = check["window"]
    if window["no_work"]:
        return {"repository": config["repository"], "status": "current", **window}
    run_dir, reports_dir = run_paths(root, window)
    if config.get("adapter") == "neuro":
        from automation.monthly_archives import hydrate_run
        hydrate_run(root, config, window["run_id"])
    manifest_path = run_dir / MANIFEST_FILENAME
    config_digest = digest_json(config)
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest.get("config_sha256") != config_digest or manifest.get("run_id") != window["run_id"]:
            raise PipelineError("Interrupted run configuration changed; inspect the saved manifest before resuming")
    else:
        if (run_dir / "candidates.json").exists():
            raise PipelineError("Run directory already contains historical screening without this automation manifest")
        manifest = {"schema_version": 1, "version": VERSION, "repository": config["repository"], **window, "config_sha256": config_digest, "created_at": utc_now(), "steps": [], "status": "running"}
    atomic_json(within_repo(root, config.get("active_file", ACTIVE_PATH)), {"repository": config["repository"], "start_date": window["start_date"], "end_date": window["end_date"], "run_id": window["run_id"]})
    atomic_json(manifest_path, manifest)
    reports_dir.mkdir(parents=True, exist_ok=True)
    environment = normalized_environment(os.environ)
    environment.update(MONTHLY_AUTOMATION_REQUIRE_GATE="true")

    def execute(label: str, argv: list[str]) -> None:
        manifest["current_step"] = label
        manifest["status"] = "running"
        atomic_json(manifest_path, manifest)
        runner(argv, root, environment)
        manifest["steps"].append({"step": label, "command": argv, "completed_at": utc_now()})
        atomic_json(manifest_path, manifest)

    try:
        # Native stages own immutable digest-based reuse. Reinvoking them resumes
        # interrupted batches and validates saved artifacts instead of trusting a
        # shallow orchestrator "stage complete" flag.
        for stage in STAGES:
            execute(stage, native_command(root, config, window, stage))
        if not (run_dir / "final_screening.json").is_file() or not (run_dir / "candidates.json").is_file():
            raise PipelineError("Screening completed without required final/candidate packets")
        manifest["current_step"] = "independent-review"
        atomic_json(manifest_path, manifest)
        if parallel_reviewer is None:
            from automation.monthly_parallel import run_parallel
            parallel_reviewer = run_parallel
        parallel = parallel_reviewer(root, run_dir, adapter=config.get("adapter"))
        manifest["independent_review"] = copy.deepcopy(parallel)
        if parallel.get("complete") is True:
            manifest["steps"].append({"step": "independent-review", "completed_at": utc_now()})
        atomic_json(manifest_path, manifest)
        raw_hashes = packet_hashes(run_dir)
        manifest["raw_packet_sha256"] = raw_hashes
        atomic_json(manifest_path, manifest)
        if adjudicator is None:
            if config.get("adapter") == "survival":
                from automation.monthly_survival import adjudicate_run
            else:
                from automation.monthly_adjudication import adjudicate_run
            adjudicator = adjudicate_run
        adjudication = adjudicator(run_dir, reports_dir, execute=parallel.get("complete") is True)
        manifest["review"] = copy.deepcopy(adjudication)
        atomic_json(manifest_path, manifest)
        verify_packets(run_dir, raw_hashes)
        if parallel.get("complete") is not True:
            raise PipelineError("Independent Claude/Gemini review is operationally incomplete; current decisions and human-review CSV are retained")
        if adjudication.get("complete") is not True:
            raise PipelineError("Astra adjudication has operationally incomplete requests; review artifacts are retained")
        blocked = adjudication.get("blocked_context_ids")
        if not isinstance(blocked, list) or not all(isinstance(value, str) and value for value in blocked):
            raise PipelineError("Astra returned an invalid application hold list")
        review_csv = within_repo(root, str(adjudication.get("csv_path") or ""))
        adjudication_path = within_repo(root, str(adjudication.get("adjudication_path") or ""))
        if not review_csv.is_file() or not adjudication_path.is_file():
            raise PipelineError("Astra did not preserve its review CSV and adjudication receipt")
        gate = {"schema_version": 1, "policy": "human_review_before_disputed_application", "source_final_screening_sha256": raw_hashes["final_screening.json"], "blocked_context_ids": sorted(set(blocked)), "review_csv": str(review_csv.relative_to(root)), "review_csv_sha256": sha256(review_csv), "adjudication_path": str(adjudication_path.relative_to(root)), "adjudication_sha256": sha256(adjudication_path)}
        atomic_json(run_dir / GATE_FILENAME, gate)
        execute("apply", native_command(root, config, window, "apply"))
        if not (run_dir / "monthly_application_filter.json").is_file():
            raise PipelineError("The native application did not execute the review gate")
        verify_packets(run_dir, raw_hashes)
        if ui_refresher is None:
            if config.get("adapter") == "survival":
                from automation.monthly_survival import refresh_ui
            elif config.get("adapter") == "neuro":
                from automation.monthly_neuro import refresh_ui
            elif config.get("adapter") == "metavir":
                from automation.monthly_metavir import refresh_ui
            else:
                from automation.monthly_ui import refresh_ui
            ui_refresher = refresh_ui
        manifest["ui_refresh"] = ui_refresher(root, run_dir, blocked)
        for index, command in enumerate(config.get("ui_commands", [])):
            execute(f"ui:{index}", render_command(command, root, window, run_dir, reports_dir))
        # Separate preprint R3 never enters native PMC screening packets.
        manifest["current_step"] = "preprint-recovery"
        atomic_json(manifest_path, manifest)
        from automation.monthly_preprint_recovery import recover_run
        manifest["preprint_recovery"] = recover_run(root, run_dir, reports_dir)
        manifest["steps"].append({"step": "preprint-recovery", "completed_at": utc_now()})
        atomic_json(manifest_path, manifest)
        execute("cost-report", native_command(root, config, window, "cost-report"))
        for index, command in enumerate(config["validation_commands"]):
            execute(f"validation:{index}", render_command(command, root, window, run_dir, reports_dir))
        verify_packets(run_dir, raw_hashes)
        manifest.update(status="validated", validated_at=utc_now(), current_step=None, review_artifacts_sha256={str(review_csv.relative_to(root)): sha256(review_csv), str(adjudication_path.relative_to(root)): sha256(adjudication_path)}, application_gate_sha256=sha256(run_dir / GATE_FILENAME))
        atomic_json(manifest_path, manifest)
        write_status(root, config, manifest)
        return manifest
    except Exception as exc:
        # Exception strings from remote clients can include credentials/content;
        # retain only the exception class in the public status artifact.
        manifest.update(status="failed", failed_at=utc_now(), error_type=type(exc).__name__)
        atomic_json(manifest_path, manifest)
        write_status(root, config, manifest)
        raise


def finalize_state(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Advance only after validated UI/tests; workflow commits state and outputs together."""
    root = root.resolve()
    validate_config(config)
    active = read_json(within_repo(root, config.get("active_file", ACTIVE_PATH)))
    run_id = active.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}", run_id):
        raise PipelineError("Invalid active run identifier")
    if config.get("adapter") == "neuro":
        from automation.monthly_archives import hydrate_run
        hydrate_run(root, config, run_id)
    run_dir = within_repo(root, Path("data/monthly_updates") / run_id)
    manifest_path = run_dir / MANIFEST_FILENAME
    manifest = read_json(manifest_path)
    if manifest.get("repository") != config["repository"] or manifest.get("config_sha256") != digest_json(config):
        raise PipelineError("Release manifest does not match repository/configuration")
    if manifest.get("status") not in {"validated", "completed"}:
        raise PipelineError("Cannot advance search watermark before screening, UI refresh and validation pass")
    expected = manifest.get("raw_packet_sha256")
    if not isinstance(expected, dict) or "final_screening.json" not in expected or "candidates.json" not in expected:
        raise PipelineError("Validated manifest lacks raw screening hashes")
    verify_packets(run_dir, expected)
    for relative, checksum in manifest.get("review_artifacts_sha256", {}).items():
        path = within_repo(root, relative)
        if not path.is_file() or sha256(path) != checksum:
            raise PipelineError("Human review artifacts changed before release")
    if sha256(run_dir / GATE_FILENAME) != manifest.get("application_gate_sha256"):
        raise PipelineError("Application hold gate changed before release")
    parallel = manifest.get("independent_review") or {}
    if parallel.get("complete") is not True:
        raise PipelineError("Release requires completed independent Claude/Gemini review")
    for relative, checksum in parallel.get("artifact_sha256", {}).items():
        path = within_repo(root, run_dir / relative)
        if not path.is_file() or sha256(path) != checksum:
            raise PipelineError("Independent review receipt changed before release")
    completed = {entry["step"] for entry in manifest.get("steps", [])}
    if not set(STAGES + ("independent-review", "apply", "cost-report")).issubset(completed) or not all(f"validation:{i}" in completed for i in range(len(config["validation_commands"]))):
        raise PipelineError("Release manifest lacks completed screening/application/validation steps")
    # Verify continuity again to prevent replaying an older run over newer state.
    state_path = within_repo(root, config.get("state_file", STATE_PATH))
    previous = read_json(state_path) if state_path.exists() else None
    cutoff = iso_date(previous["last_successful_publication_date"] if previous else config["bootstrap"]["last_successful_publication_date"], "previous successful watermark")
    end = iso_date(manifest.get("end_date"), "run end")
    start = iso_date(manifest.get("start_date"), "run start")
    if end < cutoff or start != cutoff + timedelta(days=1) and not (previous and previous.get("run_id") == run_id and end == cutoff):
        raise PipelineError("Release would skip or rewind a publication search interval")
    manifest.update(status="completed", completed_at=manifest.get("completed_at") or utc_now())
    atomic_json(manifest_path, manifest)
    state = {"schema_version": 1, "version": VERSION, "repository": config["repository"], "status": "completed", "last_successful_publication_date": str(end), "run_id": run_id, "manifest_path": str(manifest_path.relative_to(root)), "manifest_sha256": sha256(manifest_path), "completed_at": manifest["completed_at"], "human_review_pending": len(manifest["review"]["blocked_context_ids"])}
    atomic_json(state_path, state)
    write_status(root, config, manifest)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", default="automation/monthly_config.json")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--run", action="store_true")
    group.add_argument("--finalize-state", action="store_true")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    config = load_config(root, args.config)
    if args.preflight:
        result = preflight(root, config)
        print(json.dumps(result, indent=2))
        return 0 if result["ready"] else 1
    if args.run:
        result = run_pipeline(root, config)
        print(json.dumps({key: result.get(key) for key in ("repository", "status", "run_id", "start_date", "end_date")}, indent=2))
    else:
        print(json.dumps(finalize_state(root, config), indent=2))
    return 0


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        raise SystemExit(main())
    except PipelineError as error:
        print(f"Monthly automation stopped: {error}", file=sys.stderr)
        raise SystemExit(1)
