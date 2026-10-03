"""Preserve MetaVir's endpoint review inside the gated monthly publication."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

from automation import monthly_ui


SCOPE_COMMANDS = (
    "scripts/aggressive_prostate_review.py",
    "scripts/review_aggressive_case_counts.py",
    "scripts/build_aggressive_prostate_results.py",
    "scripts/validate_aggressive_prostate_results.py",
)
OUTPUT_ROOTS = (
    "Cached_results", "combined", "Subexposure_results", "static", "Plot",
    "data/aggressive_prostate_review",
)


def refresh_ui(repo_root, run_dir, blocked_context_ids):
    """Apply accepted rows, review their endpoints, and seal the final derivatives."""
    repo, run = Path(repo_root).resolve(), Path(run_dir).resolve()
    # The configured canonical combined cache receives only the native apply's
    # accepted, unblocked delta. Existing adjudicated history is retained.
    report = monthly_ui.refresh_ui(repo, run, blocked_context_ids)
    if not report.get("accepted_contexts"):
        return report

    manifest_path = run / "ui_refresh_manifest.json"
    log_path = run / "metavir_scope_refresh.log"
    try:
        roots = [monthly_ui._bound(repo, relative) for relative in OUTPUT_ROOTS]
        before = monthly_ui._hashes(repo, roots)
        with log_path.open("a", encoding="utf-8") as log:
            for script in SCOPE_COMMANDS:
                log.write(f"Running {script}\n")
                log.flush()
                subprocess.run([sys.executable, script], cwd=repo, check=True,
                               stdout=log, stderr=subprocess.STDOUT)
        after = monthly_ui._hashes(repo, roots)
        outputs = dict(report.get("output_sha256", {}))
        # The endpoint projection can change files already hashed by the initial
        # refresh. Bind the actual final bytes, including both hierarchy modes.
        for relative in outputs:
            path = monthly_ui._bound(repo, relative)
            outputs[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        for relative in set(before) | set(after):
            if before.get(relative) != after.get(relative):
                outputs[relative] = after.get(relative)
        report.update(status="refreshed", endpoint_review="validated",
                      changed_paths=sorted(outputs), output_sha256=outputs,
                      endpoint_review_log=str(log_path.relative_to(repo)))
        monthly_ui._write(manifest_path, report)
        return report
    except Exception as exc:
        monthly_ui._write(manifest_path, {
            **report, "status": "failed", "endpoint_review": "failed",
            "error": str(exc), "changed_paths": [], "output_sha256": {},
        })
        raise monthly_ui.UIRefreshError(
            f"MetaVir endpoint refresh failed; do not publish derived data. See {log_path}"
        ) from exc
