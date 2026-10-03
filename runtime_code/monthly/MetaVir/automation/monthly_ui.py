"""Append an approved monthly delta to the caches actually served by each UI.

Historical screening packets and human-review decisions are never build inputs
or write targets. Only accepted rows from the native, gated apply are projected.
A refresh error is fatal to publication: callers must not push derived data when
this function raises. The separate worker keeps each repository's Python modules
and calculation policies isolated from the other repositories.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from collections import defaultdict
from typing import Any


class UIRefreshError(RuntimeError):
    """The saved UI cannot safely represent the approved monthly delta."""


def _read(path: Path, default=None):
    if not path.exists() and default is not None:
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if hasattr(value, "item"):
        return _clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(_clean(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == data:
        return
    temporary = path.with_name(path.name + ".monthly-tmp")
    temporary.write_text(data, encoding="utf-8")
    temporary.replace(path)


def _key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _bound(root: Path, value: str) -> Path:
    candidate = (root / value).resolve()
    if not candidate.is_relative_to(root.resolve()) or Path(value).is_absolute():
        raise UIRefreshError(f"UI refresh path escapes repository: {value}")
    if "sources" in Path(value).parts or "_history" in Path(value).parts:
        raise UIRefreshError(f"An immutable source is not a UI refresh target: {value}")
    return candidate


def _identity(row):
    stable = row.get("Record ID") or row.get("Effect ID") or row.get("record_id")
    return (str(row.get("PMID") or row.get("pmid") or ""),
            _key(row.get("Direct Exposure") or row.get("direct_exposure")),
            str(stable or ""))


def _scientific_values(row):
    return tuple(str(row.get(key)) for key in (
        "Effect Type", "Effect Size", "Lower CI", "Upper CI", "Sample Size", "Cases"))


def append_rows(existing, incoming):
    """Preserve all historical rows; append IDs once and reject changed estimates."""
    result = copy.deepcopy(existing)
    indexed = {_identity(row): row for row in existing}
    added = []
    for row in incoming:
        identity = _identity(row)
        if not identity[0] or not identity[2]:
            raise UIRefreshError("An applied monthly row lacks a PMID or stable effect ID")
        old = indexed.get(identity)
        if old is not None:
            if _scientific_values(old) != _scientific_values(row):
                raise UIRefreshError(
                    f"Existing study {identity[0]} has a changed estimate; human review is required")
            continue
        # A legacy row without an effect ID must not be duplicated by an ID upgrade.
        legacy = [old for old in existing if str(old.get("PMID")) == identity[0]
                  and not (old.get("Record ID") or old.get("Effect ID"))]
        if legacy:
            if any(_scientific_values(old) == _scientific_values(row) for old in legacy):
                continue
            raise UIRefreshError(f"Legacy study {identity[0]} conflicts with a monthly estimate")
        new = copy.deepcopy(row)
        result.append(new)
        indexed[identity] = new
        added.append(new)
    return result, added


def _context(item):
    return str(item.get("context_id") or "|".join(str(item.get(key) or "")
               for key in ("pmid", "exposure", "cancer")))


def _accepted_delta(repo, run, blocked):
    final = _read(run / "final_screening.json")
    changes = _read(run / "applied_changes.json").get("changes", [])
    blocked = set(map(str, blocked))
    allowed = {}
    for item in final.get("results", []):
        if (_context(item) in blocked or item.get("final_status") not in
                {"INCLUDE_META", "ELIGIBLE_NOT_POOLABLE", "ELIGIBLE_SUBTYPE_REVIEW"}):
            continue
        key = (str(item.get("pmid")), _key(item.get("direct_exposure") or item.get("exposure")),
               _key(item.get("cancer") or item.get("disease")))
        allowed[key] = _context(item)
    delta = defaultdict(list)
    for change in changes:
        ids = set(map(str, change.get("added_pmids", []) + change.get("refreshed_pmids", [])))
        if not ids:
            continue
        exposure = change.get("exposure")
        disease = change.get("cancer") or change.get("disease")
        folder = repo / "Cached_results" / _key(exposure)
        for path in sorted(folder.glob("*_true_all.json")):
            payload = _read(path)
            if _key(payload.get("cancer") or payload.get("disease")) != _key(disease):
                continue
            for row in payload.get("studies", []):
                direct = row.get("Direct Exposure") or exposure
                key = (str(row.get("PMID")), _key(direct), _key(disease))
                if (str(row.get("PMID")) not in ids or key not in allowed
                        or _key(direct) != _key(exposure)):
                    continue
                new = copy.deepcopy(row)
                new["Direct Exposure"] = direct
                new["Monthly Context ID"] = allowed[key]
                new["monthly_run"] = str(run.relative_to(repo))
                # Rebuild only the exposure relationship in the receiving cache.
                new["Displayed Exposure"] = direct
                new["Hierarchy Inherited"] = False
                delta[(str(direct), str(disease), path.name)].append(new)
    for context, rows in list(delta.items()):
        delta[context] = append_rows([], rows)[0]
    expected = {(str(pmid), _key(change.get("exposure")),
                 _key(change.get("cancer") or change.get("disease")))
                for change in changes for pmid in change.get("added_pmids", [])}
    missing = {key for key in expected if key in allowed} - {
        (str(row["PMID"]), _key(exposure), _key(disease))
        for (exposure, disease, _), rows in delta.items() for row in rows}
    if missing:
        raise UIRefreshError(f"Native apply rows could not be located for {len(missing)} contexts")
    return delta


def _profiles(repo):
    config = _read(repo / "automation/monthly_config.json", {})
    custom = config.get("ui_refresh") or {}
    if custom.get("cache_roots"):
        pairs = {item["cache_root"]: item["plot_root"] for item in custom.get("summary_pairs", [])}
        return [{"cache": value, "mode": next((part for part in Path(value).parts
                 if part in {"yes", "no"}), "no"), "plot": pairs.get(value),
                 "builder": custom.get("summary_builder", "scripts.build_summary_plots")}
                for value in custom["cache_roots"]]
    if (repo / "survival_app.py").exists():
        raise UIRefreshError("Survival UI requires its native saved-survival publish stage; incidence caches are not its source")
    provisional = repo / "combined/provisional_ui"
    if (provisional / "subexposure_views/yes/Cached_results").is_dir():
        return [{"cache": f"combined/provisional_ui/subexposure_views/{mode}/Cached_results",
                 "mode": mode, "plot": f"combined/provisional_ui/subexposure_views/{mode}/Plot",
                 "builder": "scripts.build_summary_plots"} for mode in ("yes", "no")]
    if (provisional / "Cached_results").is_dir():
        return [{"cache": "combined/provisional_ui/Cached_results", "mode": "no",
                 "plot": "combined/provisional_ui/Plot", "builder": "scripts.build_summary_plots",
                 "native_views": (repo / "scripts/subexposure_views.py").exists()}]
    if (repo / "data/subexposure_views/yes/Cached_results").is_dir():
        return [{"cache": f"data/subexposure_views/{mode}/Cached_results", "mode": mode,
                 "plot": f"data/subexposure_views/{mode}/Plot",
                 "builder": "scripts.build_subexposure_outputs"} for mode in ("yes", "no")]
    if (repo / "Subexposure_results/yes").is_dir():
        return [{"cache": f"Subexposure_results/{mode}", "mode": mode,
                 "plot": f"Plot/subexposures/{mode}", "builder": "scripts.build_summary_plots"}
                for mode in ("yes", "no")]
    app = (repo / "app.py").read_text()
    if "METANEURO_DATA_DIR" in app:
        raise UIRefreshError("MetaNeuro requires its outcome-specific native apply and summary refresh")
    if "CACHE_DIR = os.path.join(BASE_DIR, 'Cached_results')" in app:
        return [{"cache": "Cached_results", "mode": "no", "plot": "Plot",
                 "builder": "scripts.build_summary_plots",
                 "native_views": (repo / "scripts/subexposure_views.py").exists()}]
    raise UIRefreshError("The active UI cache layout is not recognized; configure ui_refresh explicitly")


def _project_additions(rows, exposure, target, mode, hierarchy, policy):
    if policy is None:
        return copy.deepcopy(rows) if _key(exposure) == _key(target) else []
    return policy.project_rows({exposure: rows}, target, hierarchy, include=mode)


def _plan(repo, profiles, delta, policy):
    hierarchy = _read(repo / "data/exposure_hierarchy.json", {})
    result = {}
    for profile in profiles:
        root = _bound(repo, profile["cache"])
        if not root.is_dir():
            raise UIRefreshError(f"Active UI cache is absent: {profile['cache']}")
        cache_paths = sorted(root.glob("*/*_true_all.json"))
        templates = [_read(cache_paths[0])] if cache_paths else []
        available_names = {path.name for path in cache_paths}
        if not templates:
            raise UIRefreshError(f"No baseline UI data found in {profile['cache']}")
        for (exposure, disease, filename), rows in delta.items():
            if filename not in available_names:
                raise UIRefreshError(f"No active UI scope for {disease} in {profile['cache']}; configure its campaign projection before publishing")
            targets = {exposure}
            if profile["mode"] == "yes" and policy is not None:
                for parent in {parent for parents in hierarchy.values() for parent in parents}:
                    if _key(exposure) in {_key(child) for child in policy.descendants(parent, hierarchy)}:
                        targets.add(parent)
            for target in sorted(targets):
                incoming = _project_additions(rows, exposure, target, profile["mode"], hierarchy, policy)
                for suffix in ("all", "core"):
                    path = root / _key(target) / filename.replace("_true_all.json", f"_true_{suffix}.json")
                    displayed = _read(path, {})
                    if displayed.get("disease_hierarchy_prebuilt") is True:
                        path = root / "_disease_scope_sources" / path.relative_to(root)
                    relative = str(path.relative_to(repo))
                    original = result.get(relative, {}).get("payload") or _read(path, {})
                    if not original:
                        original = {key: copy.deepcopy(value) for key, value in templates[0].items()
                                    if key not in {"studies", "headline", "summary_html", "screening_stats",
                                                   "plot_url", "funnel_plot_url", "baujat_plot_url",
                                                   "disease_hierarchy_prebuilt", "disease_hierarchy_projection"}}
                        original.update(studies=[], exposure=target, cancer=disease, disease=disease,
                                        endpoint="Incidence", outcome="Incidence")
                    merged, added = append_rows(original.get("studies", []), incoming)
                    if not added:
                        continue
                    payload = copy.deepcopy(original)
                    payload["studies"] = merged
                    payload["monthly_ui_updates"] = sorted(set(payload.get("monthly_ui_updates", []) +
                                                              [row["monthly_run"] for row in added]))
                    result[relative] = {"payload": payload, "mode": profile["mode"],
                                        "added": len(added) + result.get(relative, {}).get("added", 0)}
    return result


def _calculate(repo, item, backend, policy):
    import pandas as pd
    payload = item["payload"]
    rows = payload["studies"]
    target = payload["exposure"]
    # The native selector recomputes only overlap-selection fields. Existing
    # original/manual decisions and all historical study fields remain intact.
    pool_rows = rows
    if policy:
        sources = defaultdict(list)
        for row in rows:
            sources[row.get("Direct Exposure") or target].append(row)
        selected = policy.project_rows(sources, target,
                    _read(repo / "data/exposure_hierarchy.json", {}), include=item["mode"])
        selected_ids = {_identity(row) for row in policy.pooling_rows(selected)}
        pool_rows = [row for row in rows if _identity(row) in selected_ids]
        for row in rows:
            row["subexposure_pooling_selected"] = _identity(row) in selected_ids
    frame = pd.DataFrame(pool_rows)
    disease = payload.get("disease") or payload.get("cancer")
    endpoint = payload.get("endpoint") or payload.get("outcome") or "Incidence"
    if not frame.empty and hasattr(backend, "filter_curated_meta_analysis_exclusions"):
        frame = backend.filter_curated_meta_analysis_exclusions(frame, disease, target, endpoint)
    if not frame.empty and hasattr(backend, "human_pooling_rows"):
        frame = backend.human_pooling_rows(frame)
    result = {"success": True, "headline": None, "summary_html": "<p>No studies meet the pooling criteria.</p>",
              "plot_url": None, "funnel_plot_url": None, "baujat_plot_url": None}
    if not frame.empty:
        parameters = inspect.signature(backend.perform_meta_analysis).parameters
        kwargs = {"outcome": endpoint, "exclude_meta": True, "df_all": pd.DataFrame(rows),
                  "generate_plots": True}
        if "plot_namespace" in parameters:
            kwargs["plot_namespace"] = "monthly/" + item["mode"]
        if "plot_output_root" in parameters:
            kwargs["plot_output_root"] = str(repo / "static/monthly" / item["mode"])
            kwargs["plot_url_prefix"] = "static/monthly/" + item["mode"]
        elif "plot_root" in parameters:
            kwargs["plot_root"] = str(repo / "static/monthly" / item["mode"])
        result = backend.perform_meta_analysis(frame, disease, target, **kwargs)
        if "plot_root" in kwargs and "plot_output_root" not in kwargs:
            for key in ("plot_url", "funnel_plot_url", "baujat_plot_url"):
                if str(result.get(key) or "").startswith("static/"):
                    result[key] = str(result[key]).replace("static/", "static/monthly/" + item["mode"] + "/", 1)
        if result.get("error") or result.get("success") is False:
            raise UIRefreshError(f"Failed to calculate {disease}/{target}: {result.get('error')}")
    for key in ("headline", "summary_html", "plot_url", "funnel_plot_url", "baujat_plot_url"):
        payload[key] = result.get(key)
    if isinstance(payload.get("forest_plot"), dict):
        payload["forest_plot"]["url"] = payload.get("plot_url")
    if isinstance(payload.get("screening_stats"), dict):
        payload["screening_stats"]["extracted"] = len(rows)
    if isinstance(payload.get("subexposure_projection"), dict):
        payload["subexposure_projection"].update(table_rows=len(rows), pooling_rows=len(frame))
    return payload


def _hashes(repo, roots):
    return {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
            for root in roots if root.exists() for path in root.rglob("*") if path.is_file()}


def _worker(repo, run, blocked):
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    delta = _accepted_delta(repo, run, blocked)
    report = {"schema_version": 1, "status": "no_new_accepted_rows", "changed_paths": [],
              "accepted_contexts": len(delta), "blocked_context_ids": sorted(blocked), "output_sha256": {}}
    if not delta:
        return report
    profiles = _profiles(repo)
    try:
        policy = importlib.import_module("scripts.subexposure_policy")
    except ModuleNotFoundError as exc:
        if exc.name != "scripts.subexposure_policy":
            raise
        policy = None
    plan = _plan(repo, profiles, delta, policy)
    if not plan and not any(item["cache"] == "Cached_results" for item in profiles):
        report["status"] = "already_current"
        return report
    backend = importlib.import_module("meta_analysis")
    roots = [_bound(repo, item["cache"]) for item in profiles]
    roots += [_bound(repo, item["plot"]) for item in profiles if item.get("plot")]
    roots += [repo / "static/monthly"]
    roots += [_bound(repo, item["cache"]).parent / "Subexposure_views"
              for item in profiles if item.get("native_views")]
    before = _hashes(repo, roots)
    # Provisional manifests bind historical source packets. Capture exactly those
    # declared inputs and verify they remain unchanged after the refresh.
    protected = {}
    for manifest in (repo / "combined/provisional_ui/manifest.json",):
        for relative, digest in _read(manifest, {}).get("protected_source_hashes", {}).items():
            path = _bound(repo, relative)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != digest:
                raise UIRefreshError(f"Historical UI input is already stale: {relative}")
            protected[relative] = actual
    # Validate every changed scope before committing any cache JSON.
    calculated = {path: _calculate(repo, item, backend, policy) for path, item in plan.items()}
    for relative, payload in calculated.items():
        _write(_bound(repo, relative), payload)
    summary_reports = []
    for profile in profiles:
        cache = _bound(repo, profile["cache"])
        # Parent disease views are a local derivative of the updated mode caches.
        if (repo / "scripts/provisional_disease_scopes.py").exists():
            module = importlib.import_module("scripts.provisional_disease_scopes")
            module.materialize(cache)
        if not profile.get("plot"):
            continue
        builder = importlib.import_module(profile["builder"])
        plot = _bound(repo, profile["plot"])
        if profile.get("native_views"):
            views = importlib.import_module("scripts.subexposure_views")
            views.build_views(cache, generate_plots=True)
            for mode in ("yes", "no"):
                destination = plot / ("subexposures_" + mode)
                manifest = builder.build(views.view_root(cache, mode == "yes"), destination)
                manifest["include_subexposures"] = mode
                _write(destination / "summary_manifest.json", manifest)
        if profile["builder"].endswith("build_subexposure_outputs"):
            manifest = builder.build_summaries(cache.parent, profile["mode"])
        elif hasattr(builder, "build"):
            old = _read(plot / "summary_manifest.json", {})
            manifest = builder.build(cache, plot)
            for key in ("provisional_ui_version", "manual_review_pending", "include_subexposures"):
                if key in old:
                    manifest[key] = old[key]
            _write(plot / "summary_manifest.json", manifest)
        else:
            raise UIRefreshError(f"No saved-cache summary builder: {profile['builder']}")
        summary_reports.append({"cache_root": profile["cache"], "plot_root": profile["plot"]})
    for relative, expected in protected.items():
        if hashlib.sha256(_bound(repo, relative).read_bytes()).hexdigest() != expected:
            raise UIRefreshError(f"Historical source changed during UI refresh: {relative}")
    after = _hashes(repo, roots)
    report.update(status="refreshed", changed_paths=sorted(path for path in set(before) | set(after)
                  if before.get(path) != after.get(path)), refreshed_scopes=len(plan),
                  added_cache_rows=sum(item["added"] for item in plan.values()), summaries=summary_reports,
                  historical_source_hashes_verified=len(protected),
                  output_sha256={path: after.get(path) for path in set(before) | set(after)
                                 if before.get(path) != after.get(path)})
    return report


def refresh_ui(repo_root, run_dir, blocked_context_ids):
    """Refresh the active UI in an isolated process; raise on incomplete refresh."""
    repo, run = Path(repo_root).resolve(), Path(run_dir).resolve()
    if not run.is_relative_to(repo):
        raise UIRefreshError("Monthly run directory must be inside its repository")
    request = run / "ui_refresh_request.json"
    report_path = run / "ui_refresh_manifest.json"
    _write(request, {"blocked_context_ids": sorted(set(map(str, blocked_context_ids)))})
    process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker",
                              str(repo), str(run)], cwd=repo, text=True, capture_output=True)
    (run / "ui_refresh.log").write_text(process.stdout + process.stderr, encoding="utf-8")
    if process.returncode:
        raise UIRefreshError(f"UI refresh failed; do not publish derived data. See {run / 'ui_refresh.log'}")
    return _read(report_path)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--worker":
        raise SystemExit("Use refresh_ui(repo_root, run_dir, blocked_context_ids)")
    repository, run_directory = Path(sys.argv[2]), Path(sys.argv[3])
    blocked_ids = _read(run_directory / "ui_refresh_request.json")["blocked_context_ids"]
    try:
        manifest = _worker(repository, run_directory, blocked_ids)
        _write(run_directory / "ui_refresh_manifest.json", manifest)
    except Exception as error:
        _write(run_directory / "ui_refresh_manifest.json", {"schema_version": 1,
               "status": "failed", "error": str(error), "changed_paths": []})
        raise
