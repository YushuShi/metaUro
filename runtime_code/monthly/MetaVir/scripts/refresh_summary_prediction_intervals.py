"""Redraw active summary forests from saved workbooks; never re-pool evidence."""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_summary_plots as plots
from scripts.rebuild_summary_translations import read_rows, render

def _no_recalculation(*args, **kwargs):
    raise RuntimeError("This presentation refresh must only read saved summary values")


VERSION = "separate-prediction-interval-2026-09-27"
AUDIT = ROOT / "data/summary_pi_audit/2026-09-27"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def active_manifests():
    if ROOT.name == "MetaVir":
        return [ROOT / "Plot/subexposures" / mode / "summary_manifest.json" for mode in ("yes", "no")]
    paths = [ROOT / "combined/provisional_ui/subexposure_views" / mode / "Plot/summary_manifest.json"
             for mode in ("yes", "no")]
    if ROOT.name == "metaBreath":
        pointer = read(ROOT / "combined/nonmalignant_active_view.json")
        base = ROOT / "combined" / pointer["namespace"]
    elif ROOT.name == "metaGut":
        base = ROOT / "combined/nonmalignant_extension_2026-09-10"
        pointer = read(base / "active_interim_view.json")
        base = base / pointer["view_path"]
    else:
        raise ValueError("Unsupported repository")
    return paths + [base / "subexposure_views" / mode / "Plot/summary_manifest.json" for mode in ("yes", "no")]


def active_bundle():
    if ROOT.name == "metaBreath":
        pointer_path = ROOT / "combined/nonmalignant_active_view.json"
        pointer = read(pointer_path)
        manifest_path = ROOT / "combined" / pointer["namespace"] / "interim_manifest.json"
        key = "files_sha256"
    elif ROOT.name == "metaGut":
        pointer_path = ROOT / "combined/nonmalignant_extension_2026-09-10/active_interim_view.json"
        pointer = read(pointer_path)
        manifest_path = pointer_path.parent / pointer["view_path"] / "interim_manifest.json"
        key = "asset_sha256"
    else:
        return None
    if sha(manifest_path) != pointer["manifest_sha256"]:
        raise ValueError("Active bundle checksum differs")
    manifest = read(manifest_path)
    for relative, expected in manifest[key].items():
        if sha(ROOT / relative) != expected:
            raise ValueError(f"Active asset differs: {relative}")
    return pointer_path, pointer, manifest_path, manifest, key


def plan():
    jobs, sources = [], {}
    manifests = active_manifests()
    for path in manifests:
        manifest = read(path)
        sources[str(path.relative_to(ROOT))] = sha(path)
        mode = next(part for part in path.parts if part in ("yes", "no"))
        for scope_key, scope in manifest["scopes"].items():
            datasets = {}
            for dataset, entry in scope.get("workbooks", {}).items():
                source = path.parent / entry["filename"]
                if entry.get("available"):
                    sources[str(source.relative_to(ROOT))] = sha(source)
                    datasets[dataset] = read_rows(source)
            for name, entry in scope.get("plots", {}).items():
                if not name.startswith("forest-") or not entry.get("available"):
                    continue
                rows = datasets["dietary" if name.endswith("-dietary") else "combined"]
                direction = "protective" if "protective" in name else "harmful"
                selected = plots._ordered_plot_rows(rows, direction)
                if len(selected) != entry["exposure_count"]:
                    raise ValueError(f"Saved row count differs: {path} {scope_key} {name}")
                if set(entry["locales"]) != set(manifest["languages"]):
                    raise ValueError(f"Incomplete localized forest declaration: {path} {name}")
                for locale, filename in entry["locales"].items():
                    target = path.parent / filename
                    if not target.resolve().is_relative_to(path.parent.resolve()):
                        raise ValueError("Plot path escapes release")
                    jobs.append(dict(target=target, name=name, locale=locale, scope=scope,
                        manifest=manifest, datasets=datasets, mode=mode,
                        expected=entry["exposure_count"],
                        pi_count=sum(plots.prediction_interval_bounds(row) is not None for row in selected),
                        manifest_path=str(path.relative_to(ROOT)), scope_key=scope_key,
                        before_sha256=sha(target)))
    return manifests, jobs, sources


def public_job(job):
    return {key: (str(value.relative_to(ROOT)) if isinstance(value, Path) else value)
            for key, value in job.items() if key not in ("scope", "manifest", "datasets", "staged")}


def load_checkpoint(directory, jobs, sources):
    """Reuse only complete staged PDFs bound to this exact saved-input recipe."""
    if directory is None:
        return {}
    directory = Path(directory).resolve()
    saved = read(directory / "checkpoint.json")
    expected = {"source_files": sources,
                "renderer_sha256": sha(Path(plots.__file__)),
                "localization_sha256": sha(ROOT / "scripts/plot_localization.py")}
    if any(saved.get(key) != value for key, value in expected.items()):
        raise ValueError("Checkpoint input or presentation recipe differs")
    result = {}
    for entry in saved["figures"]:
        index = entry["index"]
        if not isinstance(index, int) or index < 0 or index >= len(jobs) or index in result:
            raise ValueError("Invalid checkpoint job index")
        if entry["target"] != str(jobs[index]["target"].relative_to(ROOT)):
            raise ValueError("Checkpoint target differs")
        path = (directory / entry["file"]).resolve()
        if not path.is_relative_to(directory) or sha(path) != entry["sha256"]:
            raise ValueError("Checkpoint PDF checksum differs")
        content = path.read_bytes()
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
            raise ValueError("Checkpoint PDF is incomplete")
        result[index] = path
    return result


def refresh(check=False, preview=False, checkpoint=None):
    plots.summarize_cache = _no_recalculation
    plots.meta_analysis.perform_meta_analysis = _no_recalculation
    bundle = active_bundle()
    manifests, jobs, sources = plan()
    report = {"version": VERSION, "repository": ROOT.name, "scientific_recalculation": False,
              "source_files": sources, "jobs": [public_job(job) for job in jobs]}
    if not (AUDIT / "refresh.json").exists():
        write(AUDIT / "plan.json", report)
    print(f"{ROOT.name}: {len(jobs)} active localized forests; {len(sources)} saved inputs", flush=True)
    if check:
        return report
    if preview:
        # Include every supported language in a substantial, multi-exposure forest.
        chosen = {}
        for job in sorted(jobs, key=lambda item: item["expected"], reverse=True):
            chosen.setdefault(job["locale"], job)
        for locale, job in chosen.items():
            target = AUDIT / "previews" / locale / job["target"].name
            target.parent.mkdir(parents=True, exist_ok=True)
            assert render(job, target) == job["expected"]
        return report
    if (AUDIT / "refresh.json").exists():
        previous = read(AUDIT / "refresh.json")
        if (previous.get("renderer_sha256") != sha(Path(plots.__file__)) or
                previous.get("localization_sha256") != sha(ROOT / "scripts/plot_localization.py")):
            raise ValueError("Use a new audit release directory for a changed presentation recipe")
        for relative, expected in previous["source_files"].items():
            if not relative.endswith("summary_manifest.json") and sha(ROOT / relative) != expected:
                raise ValueError(f"Previously refreshed saved input differs: {relative}")
        for entry in previous["artifacts"]:
            if sha(ROOT / entry["path"]) != entry["after_sha256"]:
                raise ValueError(f"Previously refreshed artifact differs: {entry['path']}")
        print("Already refreshed; final artifact and input hashes verified", flush=True)
        return previous
    checkpoint_files = load_checkpoint(checkpoint, jobs, sources)
    print(f"Verified {len(checkpoint_files)} checkpoint PDFs", flush=True)
    changed = []
    def record(path, before):
        changed.append({"path": str(path.relative_to(ROOT)), "before_sha256": before,
                        "after_sha256": sha(path)})
    with tempfile.TemporaryDirectory(prefix="summary-pi-") as directory:
        reused = {}
        for index, job in enumerate(jobs):
            identity = json.dumps({k: job[k] for k in ("name", "locale", "scope", "datasets", "mode")}, sort_keys=True)
            # Coverage notes are part of the visible saved scope.
            identity += json.dumps(job["manifest"].get("screening_coverage"), sort_keys=True)
            if index in checkpoint_files:
                job["staged"] = checkpoint_files[index]
                reused.setdefault(identity, job["staged"])
                continue
            if identity in reused:
                job["staged"] = reused[identity]
                continue
            target = Path(directory) / str(index) / job["target"].name
            target.parent.mkdir(parents=True)
            if render(job, target) != job["expected"]:
                raise ValueError("Forest rendering changed the saved row count")
            job["staged"] = target
            reused[identity] = target
            if (index + 1) % 20 == 0:
                print(f"Rendered {index + 1}/{len(jobs)}", flush=True)
        for relative, expected in sources.items():
            if sha(ROOT / relative) != expected:
                raise ValueError(f"A saved input changed during rendering: {relative}")
        for job in jobs:
            shutil.copyfile(job["staged"], job["target"])
            record(job["target"], job["before_sha256"])
        for path in manifests:
            manifest = read(path)
            manifest["forest_presentation"] = {"version": VERSION,
                "prediction_interval": "Saved finite positive ordered bounds; dashed line below CI with end caps",
                "numerical_results_changed": False}
            before = sha(path);write(path, manifest);record(path, before)
    if bundle:
        pointer_path, pointer, manifest_path, manifest, key = bundle
        for entry in changed:
            if entry["path"] in manifest[key]:
                if manifest[key][entry["path"]] != entry["before_sha256"]:
                    raise ValueError("Bundle preimage differs")
                manifest[key][entry["path"]] = entry["after_sha256"]
        manifest["forest_presentation"] = {"version": VERSION, "numerical_results_changed": False}
        before = sha(manifest_path);write(manifest_path, manifest);record(manifest_path, before)
        before = sha(pointer_path);pointer["manifest_sha256"] = sha(manifest_path)
        write(pointer_path, pointer);record(pointer_path, before)
        active_bundle()
    report = {**report, "renderer_sha256": sha(Path(plots.__file__)),
              "localization_sha256": sha(ROOT / "scripts/plot_localization.py"),
              "artifacts": changed, "unique_renders": len(reused),
              "checkpoint_pdfs_reused": len(checkpoint_files)}
    write(AUDIT / "refresh.json", report)
    print(f"Published {len(jobs)} forests; {len(changed)} total plot/manifest changes; no numerical writes", flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--checkpoint", type=Path, help="Verified local staging checkpoint to resume")
    args = parser.parse_args()
    refresh(check=args.check, preview=args.preview, checkpoint=args.checkpoint)
