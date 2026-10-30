import copy
import hashlib
import json
from pathlib import Path

import pytest

from automation import preprint_recovery_bridge as bridge
from automation import autonomous_monthly as monthly


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, *, jobs=True):
    work = tmp_path / "native/MetaVir"
    run = work / "data/monthly_updates/2026-10-01_2026-10-25"
    result = run / "preprint_recovery/results/job.json"
    hold = work / bridge.REVIEW_ROOT / (run.name + "_holds.csv")
    hold.parent.mkdir(parents=True)
    hold.write_text("context_id,reason\n123|Fiber|Prostate cancer,discrepancy\n")
    files = {str(result.relative_to(work)): put(result, {"pmid": "123", "final_decision": "INCLUDE_META"}),
             str(hold.relative_to(work)): hashlib.sha256(hold.read_bytes()).hexdigest()}
    manifest = {"schema_version": 1, "repository": "MetaVir", "run_id": run.name, "status": "complete",
        "cost": {"gateway_reported_cost_usd": 1.25, "unknown_cost_calls": 2, "dispatches": 3},
        "holds": [{"pmid": "123", "context_id": "123|Fiber|Prostate cancer", "reason": "scientific_discrepancy"}],
        "jobs": [{"job_id": "job", "pmid": "123", "preprint_id": "PPR456", "context_ids": ["123|Fiber|Prostate cancer"],
                  "result_path": str(result.relative_to(work)), "result_sha256": files[str(result.relative_to(work))]}] if jobs else [],
        "owned_public_files": files}
    path = run / "preprint_recovery/recovery_manifest.json"
    binding = {"schema_version": 1, "status": "complete", "manifest_path": str(path.relative_to(work)),
               "manifest_sha256": put(path, manifest), "owned_public_files": files}
    return work, run, binding


def test_new_source_prerequisites_leave_existing_native_models_available():
    assert set(bridge.FALLBACK_MODELS).issubset(monthly.MODELS)
    assert {"gpt-5.6-luna", "gpt-5.6-sol", "claude-opus-5", "gemini-3.7-flash"}.issubset(monthly.MODELS)


def test_native_scientific_receipt_is_preserved_without_another_review(tmp_path):
    work, run, binding = fixture(tmp_path)
    original = copy.deepcopy(binding)
    value = bridge.capture_scope_recovery(work, run, binding)
    assert binding == original
    assert value["cost"]["unknown_cost_calls"] == 2
    assert value["independent_reviews_performed_by_bridge"] == 0
    assert value["projection_status"] == "held_pending_verified_native_aliases"


def test_public_wrapper_is_self_contained_and_preserves_root_queues(tmp_path):
    work, run, binding = fixture(tmp_path)
    root = tmp_path / "uro"
    baseline = root / "sources/MetaVir/Cached_results/existing.json"
    put(baseline, {"studies": [{"PMID": "prior", "Effect Size": 0.8}]})
    original = baseline.read_bytes()
    audit = root / "updates/monthly/release"
    public = bridge.publish_scope_recovery(root, work, audit, "MetaVir", "release", binding)
    wrapper = json.loads((root / public["manifest_path"]).read_text())
    assert wrapper["repository"] == "metaUro" and wrapper["run_id"] == "release"
    assert wrapper["source_manifest_sha256"] == binding["manifest_sha256"]
    assert wrapper["jobs"][0]["result_path"] in wrapper["owned_public_files"]
    assert wrapper["jobs"][0]["native_result_path"] in wrapper["source_path_aliases"]
    assert all(path.startswith(("updates/monthly/", "manualReview/preprint_recovery/")) for path in wrapper["owned_public_files"])
    assert any(h["reason"] == "verified_Uro_native_selector_alias_required" for h in wrapper["holds"])
    assert any("lineage_holds" in path for path in public["root_manual_review_files"])
    assert bridge.validate_published_recovery(root, public)["status"] == "complete"
    # A downstream consumer needs no private packet files or external source tree.
    import shutil
    shutil.rmtree(work)
    assert bridge.validate_published_recovery(root, public)["cost"]["gateway_reported_cost_usd"] == 1.25
    assert baseline.read_bytes() == original


def test_sealed_artifact_or_source_scope_mismatch_blocks_publication(tmp_path):
    work, run, binding = fixture(tmp_path)
    public_file = next(p for p in binding["owned_public_files"] if p.endswith("job.json"))
    (work / public_file).write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        bridge.capture_scope_recovery(work, run, binding)
    work, run, binding = fixture(tmp_path / "second")
    with pytest.raises(ValueError, match="scope"):
        bridge.publish_scope_recovery(tmp_path / "uro", work, tmp_path / "uro/updates/monthly/release", "metaPee", "release", binding)
    work, run, binding = fixture(tmp_path / "third")
    path = work / binding["manifest_path"]
    document = json.loads(path.read_text())
    document["jobs"][0]["result_sha256"] = "0" * 64
    binding["manifest_sha256"] = put(path, document)
    with pytest.raises(ValueError, match="sealed"):
        bridge.capture_scope_recovery(work, run, binding)


def test_private_raw_sources_cannot_enter_public_inventory(tmp_path):
    work, run, binding = fixture(tmp_path)
    private = run / "preprint_recovery/source_cache/response.json"
    checksum = put(private, {"full_article": "private"})
    binding["owned_public_files"][str(private.relative_to(work))] = checksum
    with pytest.raises(ValueError, match="Private"):
        bridge.capture_scope_recovery(work, run, binding)


def test_private_replay_preserved_separately_without_replacing_human_edits(tmp_path):
    work, run, binding = fixture(tmp_path)
    private = work / bridge.PRIVATE_ROOT / run.name / "jobs/paid.json"
    put(private, {"paid_response": "retained"})
    root = tmp_path / "uro"
    bridge.checkpoint_private(root, work, "MetaVir", run.name)
    old_private = private.read_bytes()
    old_hash = hashlib.sha256(old_private).hexdigest()
    put(private, {"paid_response": "new retained snapshot"})
    bridge.checkpoint_private(root, work, "MetaVir", run.name)
    historical = root / bridge.PRIVATE_ROOT / "MetaVir" / run.name / ".history" / old_hash / "jobs/paid.json"
    assert historical.read_bytes() == old_private
    queues = bridge.checkpoint_queues(root, work, "MetaVir", run.name, "release")
    human = root / queues[0]
    human.write_text("human_decision,accepted\n")
    later = bridge.checkpoint_queues(root, work, "MetaVir", run.name, "release")
    assert human.read_text() == "human_decision,accepted\n"
    assert "_snapshot_" in later[0]
    restored = tmp_path / "restored"
    bridge.restore_private(root, restored, "MetaVir", run.name)
    assert (restored / bridge.PRIVATE_ROOT / run.name / "jobs/paid.json").read_bytes() == private.read_bytes()
    assert not (root / "updates").exists()


def test_cost_unknowns_and_manual_holds_survive_scope_aggregation(tmp_path):
    work, run, binding = fixture(tmp_path)
    native = bridge.capture_scope_recovery(work, run, binding)
    total = bridge.aggregate_recovery({"MetaVir": {"preprint_recovery": native}, "metaPee": {"preprint_recovery": native}})
    assert total["cost"] == {"gateway_reported_cost_usd": 2.5, "unknown_cost_calls": 4, "dispatches": 6}
    assert total["manual_hold_count"] == 2
    assert total["independent_reviews_performed_by_bridge"] == 0


def test_empty_or_historical_runs_do_not_invent_projection_holds(tmp_path):
    assert bridge.capture_scope_recovery(tmp_path, tmp_path, None) is None
    work, run, binding = fixture(tmp_path, jobs=False)
    assert bridge.capture_scope_recovery(work, run, binding)["projection_status"] == "not_applicable_no_native_jobs"


def test_uploaded_private_checkpoint_restores_only_pinned_window_and_rejects_conflicts(tmp_path):
    span = {"run_id": "2026-10-01_2026-10-25", "end_date": "2026-10-25",
            "start_dates": {"MetaVir": "2026-10-01", "metaPee": "2026-10-03"}}
    artifact = tmp_path / "artifact"
    native = "2026-10-01_2026-10-25"
    private = artifact / bridge.PRIVATE_ROOT / "MetaVir" / native / "blobs/raw_no_extension"
    put(private, {"paid_dispatch": "unchanged"})
    ignored = artifact / bridge.PRIVATE_ROOT / "MetaVir/2025-01-01_2025-01-31/private.json"
    put(ignored, {"other_window": True})
    root = tmp_path / "uro"
    assert bridge.restore_uploaded_recovery(root, artifact, span) == 1
    restored = root / private.relative_to(artifact)
    assert restored.read_bytes() == private.read_bytes()
    assert not (root / ignored.relative_to(artifact)).exists()
    restored.write_text("preserve local paid bytes")
    with pytest.raises(ValueError, match="conflicts"):
        bridge.restore_uploaded_recovery(root, artifact, span)
    assert restored.read_text() == "preserve local paid bytes"


def test_sealed_native_packages_are_self_contained_and_pmc_success_never_fetches():
    import importlib.util
    import sys
    from unittest.mock import Mock
    root = Path(__file__).resolve().parents[1]
    manifest = monthly.runtime_manifest(root)
    for scope in ("MetaVir", "metaPee"):
        folder = root / "runtime_code/monthly" / scope / "automation/preprint_recovery"
        name = "_uro_preprint_package_" + scope
        spec = importlib.util.spec_from_file_location(name, folder / "__init__.py", submodule_search_locations=[str(folder)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
        fallback = __import__(name + ".europepmc_fallback", fromlist=["recover_preprint"])
        gate, http = Mock(), Mock()
        result = fallback.recover_preprint({"pmid": "123"}, pmc_availability="available",
            authorize_retrieval=gate, http_get=http, cache_dir="unused")
        assert result["status"] == "not_needed"
        gate.assert_not_called(); http.assert_not_called()
        assert scope + "/automation/preprint_recovery/europepmc_fallback.py" in manifest["files"]
        for module_name in list(sys.modules):
            if module_name == name or module_name.startswith(name + "."):
                sys.modules.pop(module_name)
