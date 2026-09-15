"""Later article choices override older decisions only within their named scope."""
import copy
import json

import pytest

from scripts import curator_dedup as curator


def checkpoint(tmp_path):
    cid, keep = "2|Omega-6 fatty acids|Prostate cancer", "1|Omega-6 fatty acids|Prostate cancer"
    before = {"context_id": cid, "final_status": "INCLUDE_META",
              "human_adjudication": {"review_id": "earlier-review"}, "extraction": {"effect_size": 1.01}}
    retained = {"context_id": keep, "extraction": {"effect_size": 1.01}}
    after = dict(copy.deepcopy(before), final_status="EXCLUDE", decision_authority="human_curator_deduplication")
    entry = {"decision_id": "choice-1", "context_id": cid, "retained_context_id": keep,
             "excluded_pmid": "2", "retained_pmid": "1", "exposure": "Omega-6 fatty acids",
             "disease": "Prostate cancer", "before": before, "after": after, "retained_record": retained,
             "source_track_sha256": {"chatgpt": {cid: curator.digest(before), keep: curator.digest(retained)}}}
    for name in ("before", "after", "retained_record"):
        entry[name + "_sha256"] = curator.digest(entry[name])
    packet = {"version": curator.VERSION, "decisions": [entry], "decisions_sha256": curator.digest([entry])}
    path = tmp_path / curator.CHECKPOINT
    path.parent.mkdir()
    path.write_text(json.dumps(packet))
    return {cid: before, keep: retained}, packet


def test_later_user_choice_preserves_kept_article_and_earlier_receipt(tmp_path):
    records, packet = checkpoint(tmp_path)
    original = copy.deepcopy(records)
    result, audit = curator.apply_checkpoint(records, tmp_path, {"chatgpt": records})
    entry = packet["decisions"][0]
    assert result[entry["context_id"]] == entry["after"]
    assert result[entry["context_id"]]["human_adjudication"] == {"review_id": "earlier-review"}
    assert result[entry["retained_context_id"]] == original[entry["retained_context_id"]]
    assert records == original
    assert curator.apply_checkpoint(result, tmp_path)[0] == result
    assert audit[0]["final_status"] == "EXCLUDE"


def test_scope_filter_removes_parent_inheritance_and_preserves_other_contexts(tmp_path):
    _, packet = checkpoint(tmp_path)
    rejected = {"PMID": "2", "Direct Exposure": "Omega-6 fatty acids"}
    other_exposure = {"PMID": "2", "Direct Exposure": "Vitamin E"}
    kept = {"PMID": "1", "Direct Exposure": "Omega-6 fatty acids"}
    rows = [rejected, other_exposure, kept]
    assert curator.filter_rows(rows, "Polyunsaturated fat (PUFA)", curator.GENERAL, packet) == [other_exposure, kept]
    assert curator.filter_rows(rows, "Omega-6 fatty acids", "Aggressive prostate cancer", packet) == rows


def test_provisional_clone_cannot_restore_rejected_article(tmp_path):
    records, packet = checkpoint(tmp_path)
    entry = packet["decisions"][0]
    clone = {"context_id": entry["context_id"] + "::pending:claude_gemini",
             "provisional_origin_context_id": entry["context_id"], "final_status": "INCLUDE_META"}
    projected = curator.protect_ui_packet({"results": [*records.values(), clone]}, tmp_path)
    assert len(projected["results"]) == 2
    assert next(row for row in projected["results"] if row["context_id"] == entry["context_id"])["final_status"] == "EXCLUDE"


def test_changed_source_requires_reconciliation(tmp_path):
    records, _ = checkpoint(tmp_path)
    changed = copy.deepcopy(records)
    next(iter(changed.values()))["extraction"]["effect_size"] = 8
    with pytest.raises(ValueError, match="source changed"):
        curator.apply_checkpoint(records, tmp_path, {"chatgpt": changed})


def test_modified_decision_receipt_is_rejected(tmp_path):
    _, packet = checkpoint(tmp_path)
    packet["decisions"][0]["excluded_pmid"] = "99"
    (tmp_path / curator.CHECKPOINT).write_text(json.dumps(packet))
    with pytest.raises(ValueError, match="checkpoint"):
        curator.load_checkpoint(tmp_path)
