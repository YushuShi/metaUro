"""Independent current-month Claude/Gemini review, without historical reseeding.

The primary R1 screen is shared; R2 and source-gated R3 are independent. Native
reviewers own scientific validation, response receipts, costs and resumability.
This adapter never applies their estimates or modifies primary screening packets.
"""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path

VERSION = "monthly-parallel-v1"
MODELS = {"r2": "claude-opus-5", "r3": "gemini-3.7-flash"}
COMPARISON = "monthly_parallel_comparison.json"


def read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def save(path, value, immutable=False):
    if immutable and path.exists():
        if read(path) != value:
            raise RuntimeError("Current monthly parallel input changed; refusing historical or stale reuse")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def stage(run_dir, names):
    path = next((run_dir / name for name in names if (run_dir / name).is_file()), None)
    if path is None:
        raise RuntimeError("Missing current monthly screening stage")
    return path, read(path)


def index(packet):
    rows = {}
    for row in packet.get("results", list((packet.get("results_by_context") or {}).values())):
        cid = row.get("context_id")
        if not cid or cid in rows:
            raise RuntimeError("Missing or duplicate independent-review context")
        rows[cid] = row
    return rows


def candidate_id(article, context, decisions):
    """Resolve an existing R1 identity; preserve scoped and endpoint IDs exactly."""
    if context.get("context_id"):
        return str(context["context_id"])
    base = "|".join((str(article["pmid"]), context["exposure"], context.get("scope_id") or context.get("cancer") or context["disease"]))
    possible = {base}
    for key in ("endpoint", "outcome"):
        if context.get(key):
            possible.add(base + "|" + str(context[key]))
    matches = possible & set(decisions)
    if len(matches) != 1:
        raise RuntimeError("Candidate lacks one exact current monthly R1 identity")
    return matches.pop()


def seed_current_run(run_dir, *, neuro=False):
    candidates_path = run_dir / "candidates.json"
    candidates = read(candidates_path)
    first_path, first = stage(run_dir, ("first_screen.json", "chatgpt_first_screen.json"))
    decisions = index(first)
    native_path = run_dir / "native/discovery.json"
    native_contexts = read(native_path, {}).get("contexts", {}) if neuro else {}
    if neuro and not native_path.is_file():
        raise RuntimeError("Neuro monthly review requires its sealed native discovery contexts")
    contexts = {}
    selected_articles = []
    for article in candidates.get("articles", []):
        selected = []
        for context in article.get("candidate_contexts", []):
            cid = candidate_id(article, context, decisions)
            if cid in contexts:
                raise RuntimeError("Duplicate monthly candidate context")
            contexts[cid] = {**context, "context_id": cid, "pmid": str(article["pmid"])}
            if cid not in decisions or decisions[cid].get("decision") not in {"PASS", "FAIL"}:
                raise RuntimeError("Current monthly R1 decision is missing or incomplete")
            if decisions[cid]["decision"] == "PASS":
                selected.append(copy.deepcopy(context))
        if selected:
            row = copy.deepcopy(article)
            if neuro:
                row.update({"PMID": str(article["pmid"]), "PMCID": article.get("pmcid"), "Title": article.get("title"), "Abstract": article.get("abstract"), "MeSH": article.get("mesh") or []})
                native_selected = []
                for c in selected:
                    native = native_contexts.get(c["context_id"])
                    if not isinstance(native, dict) or native.get("PMID") != str(article["pmid"]) or native.get("Exposure") != c["exposure"] or native.get("Outcome Key") != c["outcome"]:
                        raise RuntimeError("Neuro native context differs from current monthly discovery")
                    native_selected.append(copy.deepcopy(native))
                selected = native_selected
            row["candidate_contexts"] = selected
            selected_articles.append(row)
    if set(decisions) != set(contexts):
        raise RuntimeError("Monthly R1 and discovery context sets differ")
    passed = [row for row in decisions.values() if row["decision"] == "PASS"]
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (candidates_path, first_path)}
    if neuro:
        source_hashes["native/discovery.json"] = hashlib.sha256(native_path.read_bytes()).hexdigest()
    packet = {**candidates, "articles": selected_articles, "candidate_contexts": len(passed), "candidate_pmids": len(selected_articles), "generation": VERSION}
    source_digest = digest({"source_sha256": source_hashes, "candidates": packet, "decisions": first})
    output = run_dir / "monthly_parallel"
    save(output / "source_receipt.json", {"version": VERSION, "source_sha256": source_hashes, "models": MODELS, "input_digest": source_digest, "shared_stage": "primary R1", "independent_stages": ["Claude R2", "source-gated Gemini R3"]}, immutable=True)
    save(output / "r1_pass_candidates.json", packet, immutable=True)
    save(output / "r1_pass_packet.json", {**first, "input_digest": source_digest, "pass": len(passed), "fail": len(decisions)-len(passed), "results": passed}, immutable=True)
    save(output / "r1_selected_decisions.json", {"results": list(decisions.values())}, immutable=True)
    return output, contexts, {row["context_id"] for row in passed}, source_hashes


def load_driver(root):
    path = root / "scripts/run_claude_gemini_parallel_review.py"
    if not path.is_file() and (root / "automation/monthly_parallel_heart.py").is_file():
        path = root / "automation/monthly_parallel_heart.py"
    spec = importlib.util.spec_from_file_location("monthly_parallel_native_driver", path)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    return driver


def client_from_environment():
    from openai import OpenAI
    key = os.environ.get("CORNELL_API_KEY", "").strip()
    base = next((os.environ[name].strip() for name in ("CORNELL_API_BASE_URL", "API_BASE_URI", "API_Base_URI", "CORNELL_BASE_URL") if os.environ.get(name, "").strip()), "")
    if not key or not base:
        raise RuntimeError("Independent monthly review requires Cornell gateway credentials")
    client = OpenAI(api_key=key, base_url=base, timeout=180, max_retries=0)
    available = {item.id for item in client.models.list()}
    if set(MODELS.values()) - available:
        raise RuntimeError("Cornell gateway lacks a required independent-review model")
    return client


def claim(row, model, stage_name):
    extraction = copy.deepcopy(row.get("extraction") or row.get("study") or {})
    if "total_n" not in extraction and "sample_size" in extraction:
        extraction["total_n"] = extraction["sample_size"]
    jbi = copy.deepcopy(row.get("jbi") or {})
    for target, key in (("grade", "jbi_quality"), ("score_percent", "jbi_quality_percent"), ("answers", "jbi_answers")):
        if target not in jbi and key in row:
            jbi[target] = row[key]
    status = str(row.get("full_text_status") or row.get("eligibility") or row.get("decision") or row.get("final_status") or "").upper()
    if status in {"INCLUDE_META", "ELIGIBLE_NOT_POOLABLE"}:
        status = "INCLUDE"
    return {"eligibility": status, "extraction": extraction, "jbi": jbi,
            "reason": row.get("reason") or row.get("exclusion_reason"), "model": row.get("model") or row.get("third_round_model") or row.get("full_text_model") or row.get("parallel_review_model") or model,
            "stage": stage_name, "additional_extractions": row.get("additional_extractions") or []}


def differences(left, right):
    # Reasons/model names differ naturally. Compare scientific claims, preserving
    # all original claims for review and treating missing numeric values honestly.
    fields = []
    if left["eligibility"] != right["eligibility"]:
        fields.append("eligibility")
    if left["eligibility"] == right["eligibility"] == "EXCLUDE":
        return fields
    for section, keys in (("extraction", ("effect_size", "effect_type", "ci_lower", "ci_upper", "total_n", "cases", "comparison_direction", "comparison_group", "reference_group", "gender", "smoking_status", "exposure_measurement_type", "subgroup", "stratum", "population", "adjusted_model", "additional_records", "additional_dietary_records")), ("jbi", ("grade", "score_percent", "answers"))):
        for key in keys:
            a, b = left[section].get(key), right[section].get(key)
            if a in (None, "", [], {}) and b in (None, "", [], {}):
                continue
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                equal = abs(a-b) <= 1e-10 * max(1, abs(a), abs(b))
            else:
                equal = a == b
            if not equal:
                fields.append(section + "." + key)
    if left["additional_extractions"] != right["additional_extractions"]:
        fields.append("additional_extractions")
    return fields


def compare_current(run_dir, contexts, r2, r3, complete):
    _, primary2 = stage(run_dir, ("second_screen.json", "chatgpt_second_screen.json"))
    _, primary3 = stage(run_dir, ("full_text_review.json", "chatgpt_full_text_review.json"))
    p2, p3 = index(primary2), index(primary3)
    final = index(read(run_dir / "final_screening.json", {"results": []}))
    records = []
    for cid in sorted(r2):
        if cid not in p2:
            raise RuntimeError("Independent context has no current primary R2 decision")
        # Compare each pipeline's latest actual scientific review. Missing source
        # is not a negative vote; native R2 remains the last reviewed decision.
        full = p3.get(cid) or {}
        reviewed = str(full.get("pmc_source_status") or full.get("full_text_source_status") or "").lower() in {"full_text", "complete", "available", "manual_full_text", "authorized_manual"}
        reviewed = (reviewed or bool(full.get("full_text_characters_reviewed", 0) and full.get("full_text_source_type") in {"pmc", "manual", "authorized_manual"})) and not full.get("full_text_validation_fallback")
        p = full if reviewed else p2[cid]
        v = r3.get(cid) or r2[cid]
        primary_stage = "R3" if reviewed else "R2"
        validation_stage = "R3" if cid in r3 else "R2"
        primary = claim(p, (primary3 if reviewed else primary2).get("model"), primary_stage)
        validation = claim(v, MODELS["r3" if cid in r3 else "r2"], validation_stage)
        fields = differences(primary, validation)
        # Agreement cannot silently clear an explicit native manual-review flag.
        for reviewed_row in (p, v, final.get(cid, {})):
            manual = any(reviewed_row.get(key) is True for key in ("human_review_required", "requires_human_review", "manual_review_required", "needs_human_review"))
            manual = manual or str(reviewed_row.get("human_review_status") or "").lower() in {"pending", "required", "unresolved"}
            manual = manual or bool(reviewed_row.get("human_review_reasons"))
            if manual and "human_review_required" not in fields:
                fields.append("human_review_required")
        if fields:
            context = contexts[cid]
            records.append({**context, "cancer": context.get("cancer") or context.get("disease"), "different_fields": fields,
                            "chatgpt": primary, "claude_gemini": validation, "primary_pipeline": "Primary GPT", "validation_pipeline": "Independent Claude/Gemini", "canonical_resolution": "human_review_required"})
    packet = {"version": VERSION, "validation_status": "complete" if complete else "incomplete_actual_reviews", "shared_stage": "primary R1", "validation_track": "claude_gemini", "compared_contexts": len(r2), "records": records}
    save(run_dir / COMPARISON, packet)
    return packet


def run_prepared_driver(driver, output, gateway):
    """Femina/Cutis retain their native prepared-context and output-path APIs."""
    old_output = driver.OUTPUT_DIR
    for name, value in list(vars(driver).items()):
        if isinstance(value, Path) and value.is_relative_to(old_output):
            setattr(driver, name, output / value.relative_to(old_output))
    driver.OUTPUT_DIR = output
    driver.COST_PATH = output / "ClaudeGeminiCost.txt"
    candidates = read(output / "r1_pass_candidates.json")
    first = read(output / "r1_pass_packet.json")
    expected = driver.monthly.candidate_context_index(candidates)
    if set(expected) != set(index(first)):
        raise RuntimeError("Prepared native contexts differ from monthly R1 PASS identities")
    contexts = []
    first_rows = index(first)
    for cid, value in sorted(expected.items()):
        contexts.append({**value, "first_screen_reason": first_rows[cid].get("reason") or ""})
    articles = copy.deepcopy(candidates["articles"])
    for article in articles:
        article["candidate_contexts"] = [row for row in contexts if row["pmid"] == str(article["pmid"])]
    exposures = sorted({row["exposure"] for row in read(output / "r1_selected_decisions.json")["results"]})
    coverage = {}
    selected = read(output / "r1_selected_decisions.json")["results"]
    for exposure in exposures:
        rows = [row for row in selected if row["exposure"] == exposure]
        coverage[exposure] = {"r1_candidate_contexts": len(rows), "r1_pass_contexts": sum(row["decision"] == "PASS" for row in rows), "r1_fail_contexts": sum(row["decision"] == "FAIL" for row in rows), "r1_no_candidate": not rows}
    prepared = {"pipeline_id": driver.PIPELINE_ID, "cutoff": candidates.get("publication_date_end"), "catalog_exposures": exposures,
                "catalog_search_coverage": {"all_current_exposures_covered": False, "scope": "current monthly R1 candidates; discovery coverage remains in its original packet"},
                "exposure_r1_coverage": coverage, "source_receipt": read(output / "source_receipt.json"), "articles": articles, "contexts": contexts}
    prepared["input_digest"] = driver.monthly.stable_json_digest(prepared)
    save(output / "prepared_candidates.json", prepared, immutable=True)
    driver.CUTOFF = candidates.get("publication_date_end")
    original_client = driver.configure_client
    driver.configure_client = lambda: gateway
    try:
        driver.run_r2(prepared, batch_size=4, workers=1, max_units=None)
        driver.run_r3(prepared, workers=1, max_units=None)
        driver.update_outputs(prepared)
    finally:
        driver.configure_client = original_client


def run_parallel(root, run_dir, *, adapter=None, driver=None, client=None):
    root, run_dir = Path(root), Path(run_dir)
    output, contexts, expected, source_hashes = seed_current_run(run_dir, neuro=adapter == "neuro")
    error_type = None
    if expected:
        previous_gate = os.environ.get("MONTHLY_AUTOMATION_REQUIRE_GATE")
        os.environ["MONTHLY_AUTOMATION_REQUIRE_GATE"] = "true"
        try:
            driver = driver or load_driver(root)
            if getattr(driver, "CLAUDE_R2_MODEL", getattr(driver, "CLAUDE_MODEL", None)) != MODELS["r2"] or getattr(driver, "GEMINI_R3_MODEL", getattr(driver, "GEMINI_MODEL", None)) != MODELS["r3"]:
                raise RuntimeError("Independent reviewer model contract changed")
            # Keep current costs local; no historical all-history packet is touched.
            gateway = client if client is not None else client_from_environment()
            if hasattr(driver, "run_native"):
                driver.run_native(root, run_dir, output, gateway)
            elif hasattr(driver, "PREPARED_PATH"):
                run_prepared_driver(driver, output, gateway)
            else:
                driver.OUTPUT_DIR = output
                driver.COST_PATH = output / "ClaudeGeminiCost.txt"
                driver.PUBLICATION_DATE_END = read(run_dir / "candidates.json").get("publication_date_end")
                driver.run_r2(output, client=gateway)
                driver.run_r3(output, client=gateway)
                driver.write_outputs_and_state(output, stopped_reason="monthly_current_window")
        except Exception as exc:
            # Persist failures without provider headers/credentials. Available
            # decisions still produce a review CSV before the pipeline stops.
            error_type = type(exc).__name__
        finally:
            if previous_gate is None:
                os.environ.pop("MONTHLY_AUTOMATION_REQUIRE_GATE", None)
            else:
                os.environ["MONTHLY_AUTOMATION_REQUIRE_GATE"] = previous_gate
    r2 = index(read(output / "claude_r2_results.json", None) or read(output / "claude_r2_progress.json", {"results": []}))
    r3 = index(read(output / "gemini_r3_results.json", None) or read(output / "gemini_r3_progress.json", {"results": []}))
    if set(r2) - expected or set(r3) - set(r2):
        raise RuntimeError("Independent results escaped the current monthly R1 scope")
    required_r3 = {cid for cid, row in r2.items() if row.get("full_text_eligible")}
    problems = read(output / "screening_problem_ledger.json", {}).get("unresolved", [])
    for name in ("claude_r2_failures.json", "gemini_r3_failures.json"):
        problems += [event for event in read(output / name, {}).get("events", []) if event.get("status") == "unresolved"]
    complete = not error_type and set(r2) == expected and set(r3) == required_r3 and not problems
    compare_current(run_dir, contexts, r2, r3, complete)
    for name, checksum in source_hashes.items():
        if hashlib.sha256((run_dir / name).read_bytes()).hexdigest() != checksum:
            raise RuntimeError("Primary monthly source changed during independent review")
    result = {"version": VERSION, "complete": complete, "models": MODELS, "shared_stage": "primary R1", "expected_r2": len(expected), "completed_r2": len(r2), "required_r3": len(required_r3), "completed_r3": len(r3), "operational_error_type": error_type, "comparison_path": str(run_dir / COMPARISON), "missing_context_ids": sorted((expected-set(r2)) | (required_r3-set(r3))), "source_sha256": source_hashes}
    save(output / "monthly_status.json", result)
    result["artifact_sha256"] = {str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(output.rglob("*")) if path.is_file() and not path.is_symlink() and path.suffix in {".json", ".jsonl", ".csv", ".txt", ".xml", ".html", ".htm", ".pdf"}}
    return result
