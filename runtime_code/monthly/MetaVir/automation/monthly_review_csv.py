"""Durable, spreadsheet-safe comparison queue; annotations never approve evidence."""
from __future__ import annotations
import csv
import json
from pathlib import Path

HUMAN_FIELDS = ("human_decision", "reviewer", "comment")
SIDE_FIELDS = ("pipeline", "stage", "model", "decision", "reason", "effect_size", "effect_type", "ci_lower", "ci_upper", "total_n", "cases", "quality_grade", "quality_percent")


def safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def queue_path(run_dir, reports_dir):
    root = run_dir.parents[2] if run_dir.parent.name == "monthly_updates" and run_dir.parent.parent.name == "data" else reports_dir.parent
    return root / "monthlyHumanReview" / (run_dir.name + ".csv")


def metadata(run_dir):
    result = {"run_id": run_dir.name, "source_scope": run_dir.parents[2].name if run_dir.parent.name == "monthly_updates" else "", "stages": {}}
    for stage, names in (("r2", ("second_screen.json", "chatgpt_second_screen.json")), ("r3", ("full_text_review.json", "chatgpt_full_text_review.json"))):
        path = next((run_dir/name for name in names if (run_dir/name).is_file()), None)
        if path:
            packet = json.loads(path.read_text())
            result["stages"][stage] = {row["context_id"]: row.get("model") or packet.get("model") or "" for row in packet.get("results", [])}
    return result


def side(claim, pipeline, stage, model):
    extraction = claim.get("extraction") or claim
    jbi = claim.get("jbi") or {}
    result = {"pipeline": pipeline, "stage": claim.get("stage") or stage, "model": claim.get("model") or model,
              "decision": claim.get("eligibility") or claim.get("final_status") or claim.get("full_text_status"), "reason": claim.get("reason"),
              "quality_grade": jbi.get("grade") or claim.get("jbi_grade"), "quality_percent": jbi.get("score_percent", claim.get("jbi_score_percent"))}
    result.update({key: extraction.get(key) for key in ("effect_size", "effect_type", "ci_lower", "ci_upper", "total_n", "cases")})
    return result


def write_review(path, records, *, context=None, legacy_path=None):
    context = context or {}
    previous = {}
    # Copy annotations forward once; historical reports remain byte-for-byte intact.
    prior = path if path.exists() else legacy_path
    if prior is not None and prior.exists():
        with prior.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                rid = row.get("review_id")
                if rid in previous:
                    raise ValueError("Duplicate human review ID")
                previous[rid] = row
    columns = ["review_id", "run_id", "source_scope", "context_id", "pmid", "pubmed_url", "evidence_url", "disease", "exposure", "outcome", "title", "comparison", "stage"]
    for i in (1, 2):
        columns += [f"pipeline_{i}" if field == "pipeline" else f"pipeline_{i}_{field}" for field in SIDE_FIELDS]
    columns += ["disagreement_fields", "human_review_reason", "adjudication_status", "different_fields", "r1_decision", "r2_decision", "r3_decision", "r2_claims", "r3_claims", "primary_claims", "validation_claims", "astra_model", "astra_status", "astra_recommendation", "astra_confidence", "astra_rationale", "astra_evidence_source", "astra_evidence_quote", "human_review_status", *HUMAN_FIELDS]
    # Preserve optional reviewer-created annotation columns as well.
    extra = sorted({key for row in previous.values() for key in row if key not in columns})
    columns += extra
    rows = []
    for record in records:
        source, decision = record["request"]["input"], record.get("result") or {}
        cid = source["context_id"]
        row = {key: source.get(key) for key in ("context_id", "pmid", "exposure", "disease", "title", "comparison", "r1_decision")}
        fields = "; ".join(source["different_fields"])
        row.update(review_id=record["request_digest"], run_id=context.get("run_id", ""), source_scope=context.get("source_scope", ""), outcome=source.get("endpoint") or source.get("outcome") or "",
                   pubmed_url=f"https://pubmed.ncbi.nlm.nih.gov/{source['pmid']}/", evidence_url=f"https://pmc.ncbi.nlm.nih.gov/articles/{source['pmcid']}/" if source.get("pmcid") else f"https://pubmed.ncbi.nlm.nih.gov/{source['pmid']}/",
                   different_fields=fields, disagreement_fields=fields, human_review_reason=("Explicit native human-review flag remains unresolved" if fields == "human_review_required" else "Scientific claims requiring review: " + fields),
                   r2_decision=source["r2"].get("eligibility"), r3_decision=source["r3"].get("eligibility"), astra_model=record.get("model") or record["request"].get("model"), astra_status=record["status"], adjudication_status=record["status"], human_review_status="pending",
                   **{"astra_" + key: decision.get(key) for key in ("recommendation", "confidence", "rationale", "evidence_source", "evidence_quote")})
        if source["comparison"] == "R2_R3":
            one = side(source["r2"], "Primary GPT (sequential stages)", "R2", context.get("stages", {}).get("r2", {}).get(cid, ""))
            two = side(source["r3"], "Primary GPT (sequential stages)", "R3", context.get("stages", {}).get("r3", {}).get(cid, ""))
        else:
            one = side(source.get("primary", {}), "Primary GPT", "", "")
            two = side(source.get("validation", {}), "Independent validation", "", "")
            if str(two.get("model") or "").startswith(("claude", "gemini")):
                two["pipeline"] = "Independent Claude/Gemini"
        row["stage"] = str(one["stage"] or "") + " / " + str(two["stage"] or "")
        for i, values in enumerate((one, two), 1):
            for key, value in values.items():
                row[f"pipeline_{i}" if key == "pipeline" else f"pipeline_{i}_{key}"] = value
        for key in ("r2", "r3", "primary", "validation"):
            row[key + "_claims"] = json.dumps(source.get(key, {}), ensure_ascii=False, sort_keys=True)
        for key in (*HUMAN_FIELDS, *extra):
            row[key] = previous.get(row["review_id"], {}).get(key, "")
        rows.append(row)
    current = {row["review_id"] for row in rows}
    for rid, row in previous.items():
        if rid not in current and any(row.get(k) for k in (*HUMAN_FIELDS, *extra)):
            rows.append({**row, "human_review_status": "superseded_input_preserved"})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: safe(value) for key, value in row.items()} for row in rows)
    temporary.replace(path)
