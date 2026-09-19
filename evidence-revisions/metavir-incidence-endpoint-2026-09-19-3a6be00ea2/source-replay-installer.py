#!/usr/bin/env python3
"""Install or replay the sealed MetaVir incidence-endpoint release.

Exact mode copies only receipt-listed files after a complete preflight.  The
endpoint-only mode is intended for older embedded MetaVir snapshots: it installs
the durable decision checkpoint and policy code, performs three small semantic
merges that preserve known snapshot-specific additions, and rebuilds artifacts
from the destination's own evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
RECEIPT_PATH = HERE / "receipt.json"
STAGE = HERE / "repo"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_hash(path: Path) -> str | None:
    return sha256(path) if path.is_file() else None


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_text(destination: Path, text: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(text, encoding="utf-8")
        if destination.exists():
            shutil.copymode(destination, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def replace_once(text: str, before: str, after: str, path: str) -> str:
    if after in text:
        return text
    count = text.count(before)
    if count != 1:
        raise RuntimeError(f"Expected one merge anchor in {path}, found {count}: {before!r}")
    return text.replace(before, after, 1)


def merge_readme(text: str) -> str:
    policy = (
        "Incidence means time to first prostate-cancer diagnosis. For every HR (and any\n"
        "other follow-up estimate), the modeled failure event must be identified.\n"
        "Mortality, fatal/lethal disease determined by later death or metastasis,\n"
        "postdiagnosis progression, recurrence, and survival are excluded even when the\n"
        "cohort was cancer-free at entry. Separately reported total, advanced-stage-at-\n"
        "diagnosis, and high-grade diagnosis estimates remain eligible. Reviewed legacy\n"
        "exceptions are recorded in `data/incidence_endpoint_review/` and replayed by\n"
        "the disease-scope projection without changing historical model responses.\n\n"
    )
    anchor = "Moderate/Good JBI and disease/design eligibility rules still apply.\n\n"
    if policy not in text:
        text = replace_once(text, anchor, anchor + policy, "README.md")
    old = "`static/chatgpt/`; combined plots use `static/<exposure>/`."
    new = (
        "`static/chatgpt/`; combined plots use `static/<exposure>/`. Endpoint-policy\n"
        "rebuilds use the isolated `incidence_endpoint_v1` namespaces so an audited\n"
        "release never overwrites prior figure bytes."
    )
    return replace_once(text, old, new, "README.md")


def merge_plot_localization(text: str) -> str:
    before = (
        "    if repo_root.name.casefold() == \"metavir\":\n"
        "        return \"metavir\""
    )
    after = (
        "    if (repo_root.name.casefold() == \"metavir\"\n"
        "            or ((repo_root / \"data/aggressive_prostate_review\").is_dir()\n"
        "                and (repo_root / \"combined\").is_dir())):\n"
        "        return \"metavir\""
    )
    return replace_once(text, before, after, "scripts/plot_localization.py")


def merge_subexposure_builder(text: str) -> str:
    source_before = "    source_root=ROOT/('combined' if ROOT.name=='MetaVir' else 'Cached_results')"
    source_after = (
        "    # Prefer the authoritative combined projection when it is present.  The\n"
        "    # prior directory-name check made isolated release builds silently fall\n"
        "    # back to the legacy cache merely because the checkout had been staged\n"
        "    # under a temporary directory name.\n"
        "    combined_root=ROOT/'combined'\n"
        "    source_root=combined_root if (combined_root/'_merge_manifest.json').exists() else ROOT/'Cached_results'"
    )
    text = replace_once(
        text, source_before, source_after, "scripts/rebuild_subexposure_results.py"
    )
    namespace_before = "                                os.chdir(plot_root);kwargs['generate_plots']=True\n"
    namespace_after = (
        namespace_before
        + "                                # The temporary plot root is copied beneath the\n"
        + "                                # final subexposure-mode directory below, so the\n"
        + "                                # namespace here is relative to that directory.\n"
        + "                                kwargs['plot_namespace']='incidence_endpoint_v1'\n"
    )
    text = replace_once(
        text, namespace_before, namespace_after, "scripts/rebuild_subexposure_results.py"
    )
    url_before = "                                result[k]=str(url).replace('static/',f'static/subexposures/{mode}/',1) if url else None"
    url_after = "                                result[k]=str(url).replace('static/',f'static/subexposures/{mode}/',1) if url else None"
    return replace_once(
        text, url_before, url_after, "scripts/rebuild_subexposure_results.py"
    )


MERGERS = {
    "README.md": merge_readme,
    "scripts/plot_localization.py": merge_plot_localization,
    "scripts/rebuild_subexposure_results.py": merge_subexposure_builder,
}


def load_receipt() -> dict:
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    if receipt.get("release_id") != "metavir-incidence-endpoint-2026-09-19-v1":
        raise RuntimeError("Unexpected release receipt")
    return receipt


def exact_install(destination: Path, receipt: dict, check_only: bool) -> dict:
    states = []
    conflicts = []
    for entry in receipt["files"]:
        relative = entry["path"]
        source = STAGE / relative
        target = destination / relative
        if current_hash(source) != entry["after_sha256"]:
            raise RuntimeError(f"Staged payload failed its checksum: {relative}")
        found = current_hash(target)
        if found == entry["after_sha256"]:
            state = "already-installed"
        elif found == entry["before_sha256"] or found in entry.get("intermediate_sha256", []):
            state = "ready"
        else:
            state = "conflict"
            conflicts.append(
                {"path": relative, "expected": entry["before_sha256"], "found": found}
            )
        states.append({"path": relative, "state": state})
    if conflicts:
        preview = "\n".join(
            f"  {item['path']}: expected {item['expected']}, found {item['found']}"
            for item in conflicts[:20]
        )
        raise RuntimeError(f"Exact-install preflight found {len(conflicts)} conflict(s):\n{preview}")
    if not check_only:
        for entry, state in zip(receipt["files"], states, strict=True):
            if state["state"] == "ready":
                atomic_copy(STAGE / entry["path"], destination / entry["path"])
        bad = [
            entry["path"]
            for entry in receipt["files"]
            if current_hash(destination / entry["path"]) != entry["after_sha256"]
        ]
        if bad:
            raise RuntimeError(f"Post-install checksum mismatch: {bad[:20]}")
    return {
        "mode": "exact",
        "checked": len(states),
        "ready": sum(item["state"] == "ready" for item in states),
        "already_installed": sum(item["state"] == "already-installed" for item in states),
        "installed": 0 if check_only else sum(item["state"] == "ready" for item in states),
    }


def endpoint_source_plan(destination: Path, receipt: dict) -> list[dict]:
    plan = []
    conflicts = []
    for entry in receipt["endpoint_only_replay"]["sources"]:
        relative = entry["path"]
        source = STAGE / relative
        target = destination / relative
        if current_hash(source) != entry["after_sha256"]:
            raise RuntimeError(f"Staged endpoint source failed its checksum: {relative}")
        found = current_hash(target)
        if entry["kind"] == "copy":
            if found == entry["after_sha256"]:
                state = "already-installed"
            elif found in entry["allowed_before_sha256"]:
                state = "ready"
            else:
                state = "conflict"
        else:
            if not target.is_file():
                state = "conflict"
            else:
                original = target.read_text(encoding="utf-8")
                try:
                    merged = MERGERS[relative](original)
                except RuntimeError as exc:
                    state = "conflict"
                    conflicts.append({"path": relative, "error": str(exc), "found": found})
                    plan.append({"entry": entry, "state": state})
                    continue
                if merged == original:
                    state = "already-installed"
                elif found in entry["allowed_before_sha256"]:
                    state = "ready"
                else:
                    state = "conflict"
        if state == "conflict":
            conflicts.append(
                {
                    "path": relative,
                    "expected": entry["allowed_before_sha256"],
                    "found": found,
                }
            )
        plan.append({"entry": entry, "state": state})
    if conflicts:
        raise RuntimeError(
            "Endpoint-only preflight conflicts:\n" + json.dumps(conflicts[:20], indent=2)
        )
    return plan


def endpoint_install(
    destination: Path,
    receipt: dict,
    check_only: bool,
    skip_build: bool,
) -> dict:
    plan = endpoint_source_plan(destination, receipt)
    if check_only:
        return {
            "mode": "endpoint-only",
            "checked": len(plan),
            "ready": sum(item["state"] == "ready" for item in plan),
            "already_installed": sum(item["state"] == "already-installed" for item in plan),
            "built": False,
        }

    for item in plan:
        if item["state"] != "ready":
            continue
        entry = item["entry"]
        relative = entry["path"]
        target = destination / relative
        if entry["kind"] == "copy":
            atomic_copy(STAGE / relative, target)
        else:
            atomic_text(target, MERGERS[relative](target.read_text(encoding="utf-8")))

    build_commands = []
    if not skip_build:
        environment = os.environ.copy()
        environment.setdefault("MPLCONFIGDIR", "/tmp/metavir-incidence-endpoint-matplotlib")
        commands = (
            [sys.executable, "-B", "scripts/replay_incidence_endpoint_decisions.py"],
            [sys.executable, "-B", "scripts/validate_incidence_endpoint_results.py"],
        )
        for command in commands:
            subprocess.run(command, cwd=destination, env=environment, check=True)
            build_commands.append(command)
    return {
        "mode": "endpoint-only",
        "checked": len(plan),
        "installed_sources": sum(item["state"] == "ready" for item in plan),
        "already_installed_sources": sum(
            item["state"] == "already-installed" for item in plan
        ),
        "built": not skip_build,
        "commands": build_commands,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--endpoint-only", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Endpoint-only mode: install sources without rebuilding destination artifacts.",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    destination = args.destination.resolve()
    if not destination.is_dir():
        raise SystemExit(f"Destination is not a directory: {destination}")
    receipt = load_receipt()
    if args.endpoint_only:
        result = endpoint_install(destination, receipt, args.check, args.skip_build)
    else:
        if args.skip_build:
            raise SystemExit("--skip-build is only valid with --endpoint-only")
        result = exact_install(destination, receipt, args.check)
    report = {
        "release_id": receipt["release_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "destination": str(destination),
        "check_only": args.check,
        **result,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
