"""Regression checks for durable reviewed evidence and cohort-safe projection."""
import copy
import json
from pathlib import Path

import pytest

from scripts import human_manual_review as human


def checkpoint(tmp_path):
    sources = {"chatgpt": {"extraction": {"effect_size": 2}},
               "claude_gemini": {"extraction": {"effect_size": 3}}}
    row = {"context_id": "1|Tea|Prostate cancer", "final_status": "INCLUDE_META",
           "extraction": {"effect_size": 1.5}, "human_adjudication": {"review_id": "human-1"}}
    decision = {"review_id": "human-1", "context_id": row["context_id"], "after": row,
                "after_sha256": human.digest(row),
                "source_track_sha256": {k: human.digest(v) for k, v in sources.items()}}
    path = tmp_path / human.CHECKPOINT
    path.parent.mkdir()
    path.write_text(json.dumps({"version": human.VERSION, "decisions": [decision]}))
    tracks = {k: {row["context_id"]: v} for k, v in sources.items()}
    return row, tracks


def test_replay_overrides_automation_without_changing_unrelated_records(tmp_path):
    human_row, tracks = checkpoint(tmp_path)
    automated = {human_row["context_id"]: {"context_id": human_row["context_id"], "extraction": {"effect_size": 99}},
                 "unrelated": {"extraction": {"effect_size": .7}}}
    original = copy.deepcopy(automated)
    result, audit = human.apply_checkpoint(automated, tmp_path, tracks)
    assert result[human_row["context_id"]] == human_row
    assert result["unrelated"] == original["unrelated"]
    assert automated == original
    assert human.apply_checkpoint(result, tmp_path, tracks)[0] == result
    assert audit[0]["decision_authority"] == "human_manual_review"


def test_changed_model_input_requires_reconciliation(tmp_path):
    row, tracks = checkpoint(tmp_path)
    tracks["chatgpt"][row["context_id"]]["extraction"]["effect_size"] = 4
    with pytest.raises(ValueError, match="Reviewed model evidence changed"):
        human.apply_checkpoint({row["context_id"]: row}, tmp_path, tracks)


def test_modified_checkpoint_record_is_rejected(tmp_path):
    row, tracks = checkpoint(tmp_path)
    path = tmp_path / human.CHECKPOINT
    packet = json.loads(path.read_text())
    packet["decisions"][0]["after"]["extraction"]["effect_size"] = 77
    path.write_text(json.dumps(packet))
    with pytest.raises(ValueError, match="Invalid human-reviewed record"):
        human.apply_checkpoint({row["context_id"]: row}, tmp_path, tracks)


def test_multiple_accepted_contrasts_cannot_count_cohort_twice():
    item = {"context_id": "1|Diet|Prostate cancer", "human_adjudication": {"review_id": "human-1"},
            "final_status": "INCLUDE_META", "extraction": {"effect_size": 1.15},
            "additional_extractions": [{"effect_size": 1.30}], "pool_in_parent_meta_analysis": True}
    displayed = human.display_records(item)
    assert [r["extraction"]["effect_size"] for r in displayed] == [1.15, 1.30]
    assert displayed[0]["pool_in_parent_meta_analysis"] is True
    second = human.decorate_study({}, displayed[1])
    assert second["manual_poolable"] is False and second["Primary Estimate"] is False
    assert item["pool_in_parent_meta_analysis"] is True


def test_targeted_refresh_keeps_unrelated_scope_annotations(tmp_path):
    old = {"PMID": "1", "Direct Exposure": "Tea", "Effect Size": .5}
    untouched = {"PMID": "2", "Direct Exposure": "Tea", "Effect Size": .8,
                 "Disease Endpoint": "aggressive", "disease_scope_pooling_selected": False}
    path = tmp_path / "tea/prostate_cancer_incidence_true_all.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"exposure": "Tea", "studies": [old, untouched]}))
    new = {"context_id": "1|Tea|Prostate cancer", "Effect Size": 1.2}
    result = human.merge_saved_scope_rows({"Tea": [new, {"PMID": "3"}]}, tmp_path, {new["context_id"]})
    assert result["Tea"] == [untouched, new]
