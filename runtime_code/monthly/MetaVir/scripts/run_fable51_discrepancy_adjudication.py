#!/usr/bin/env python3
"""Adjudicate MetaVir pipeline discrepancies with Claude Fable 5.1.

Scientific evidence sent to the model is strictly limited to each record's
title, abstract, and MeSH terms.  The competing pipeline values are supplied as
untrusted claims so that Fable can say which claim the allowed record supports.
No full text, PMC content, evidence excerpts, prior explanations, or article
URLs are loaded into a model request.

The run is resumable and uses exactly one PMID per gateway work unit, allowing
gateway-reported token usage and cost to be attributed directly to an article.
Use ``--run-only`` for paid calls, then ``--export-only`` to build the CSVs from
validated checkpoints.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
SCREENING_CSV = (
    REPO_ROOT
    / "outputs"
    / "chatgpt_claude_gemini_comparison_2026-09-03"
    / "screening_discrepancies.csv"
)
EXTRACTION_CSV = (
    REPO_ROOT
    / "outputs"
    / "chatgpt_claude_gemini_comparison_2026-09-03"
    / "extraction_discrepancies.csv"
)
ARTICLE_JSON = (
    REPO_ROOT
    / "data"
    / "claude_gemini_screening"
    / "all_history_through_2026-08-29"
    / "r2_input_candidates.json"
)
CHECKPOINT_DIR = (
    REPO_ROOT
    / "data"
    / "fable51_discrepancy_adjudication"
    / "title_abstract_mesh_2026-09-03"
)
OUTPUT_DIR = REPO_ROOT / "outputs" / "fable51_discrepancy_adjudication_2026-09-03"

MODEL = "claude-fable-5-1"
PROTOCOL_VERSION = "metavir-fable51-title-abstract-mesh-v1"
SCHEMA_VERSION = "metavir-fable51-adjudication-v1"
SOURCE_SCOPE = "TITLE_ABSTRACT_MESH_ONLY"

FIELD_MAP = {
    "sample_size": "SAMPLE_SIZE",
    "effect_size": "EFFECT_SIZE",
    "confidence_interval_or_p_value": "CI_OR_P_VALUE",
}
# EFFECT_MEASURE_TYPE is accepted only while validating authenticated historical
# supersets. It is absent from FIELD_MAP, so current discrepancy units can never
# request or export that now-nonreportable field.
VALID_FIELDS = frozenset(
    {"SCREENING_DECISION", "EFFECT_MEASURE_TYPE", *FIELD_MAP.values()}
)
VALID_VERDICTS = frozenset(
    {
        "CHATGPT_CORRECT",
        "CLAUDE_GEMINI_CORRECT",
        "BOTH_CORRECT",
        "NEITHER_CORRECT",
        "NO_REPORTABLE_DISCREPANCY",
        "INSUFFICIENT_TITLE_ABSTRACT_MESH",
        "UNSURE",
    }
)
VALID_TAM_ASSESSMENTS = frozenset(
    {"SUFFICIENT", "INSUFFICIENT", "AMBIGUOUS", "NOT_ASSESSED_PROVIDER_FILTER"}
)
VALID_EVIDENCE_SOURCES = frozenset({"TITLE", "ABSTRACT", "MESH", "NONE"})
VALID_EVIDENCE_TYPES = frozenset({"BIOSPECIMEN", "DIETARY_INTAKE", "UNCLEAR"})
EXCLUSION_CODES = frozenset(
    {
        "S2-REVIEW",
        "S2-NONPRIMARY",
        "S2-ANIMAL",
        "S2-EXPOSURE-MISMATCH",
        "S2-COMPOUND",
        "S2-CANCER-MISMATCH",
        "S2-WRONG-OUTCOME",
        "S2-POSTDIAGNOSIS",
        "S2-BENIGN-ONLY",
        "S2-MENDELIAN-RANDOMIZATION",
        "S2-GENOTYPE-INTERACTION",
        "S2-GENETIC-FOCUS",
        "S2-DIAGNOSTIC-MECHANISTIC",
        "S2-NO-TARGET-ESTIMATE",
        "S2-DUPLICATE",
    }
)

SYSTEM_PROMPT = """You are an independent scientific adjudicator for MetaVir, a systematic review of nutritional exposures and incident prostate cancer. For scientific evidence, use ONLY the TITLE, ABSTRACT, and MESH supplied in SOURCE_EVIDENCE. Pipeline claims are candidate claims, not evidence. Never request, retrieve, quote, infer from, or use full text, PMC/DOI/PubMed pages, references, external knowledge, web searches, or memorized article content. You have not reviewed full text and must never imply that you have. If title+abstract+MeSH cannot resolve a claim, report insufficiency or uncertainty instead of guessing. Return only one valid JSON object matching the contract."""

INSTRUCTIONS = """Independently adjudicate every requested discrepancy field for this one PMID.

SCREENING RULE
INCLUDE an original human study when the specified nutritional exposure and incident prostate-cancer risk are evaluated, even if the result is null or title+abstract+MeSH omit sample size, effect estimate, CI, or p value. EXCLUDE only when the allowed evidence clearly establishes one of these codes: S2-REVIEW (review/meta-analysis); S2-NONPRIMARY (editorial/commentary/guideline/protocol/case report/correction/retraction); S2-ANIMAL (nonhuman/preclinical only); S2-EXPOSURE-MISMATCH (target exposure absent/incidental/wrong concept/medical treatment); S2-COMPOUND (target inseparable from an index, mixture, combined exposure, or multi-nutrient intervention); S2-CANCER-MISMATCH (incident prostate-cancer result absent or inseparable); S2-WRONG-OUTCOME (only survival, mortality, recurrence, progression, treatment response, prognosis, prevalence, or another non-incidence outcome); S2-POSTDIAGNOSIS (exposure only after diagnosis/treatment); S2-BENIGN-ONLY; S2-MENDELIAN-RANDOMIZATION; S2-GENOTYPE-INTERACTION; S2-GENETIC-FOCUS; S2-DIAGNOSTIC-MECHANISTIC; S2-NO-TARGET-ESTIMATE (the target incidence relationship itself is inseparable, not merely numerically unreported); or S2-DUPLICATE (only if title+abstract+MeSH explicitly establishes overlap). A missing abstract is not evidence of nonprimary status. Treat INCLUDE_META and ELIGIBLE_NOT_POOLABLE as INCLUDE when comparing pipeline claims.

EXTRACTION RULE
Adjudicate only requested_fields. SAMPLE_SIZE is the total participants analyzed for the target exposure-incidence estimate, never the number of cases/events. Match each number to the specified exposure form, incident prostate-cancer outcome, population/stratum, comparison, model, and estimate. Do not combine values from different estimates. For CI_OR_P_VALUE, either a CI or a p value can resolve the field, but it must belong to the same target estimate. Do not derive a CI from a p value or vice versa. If a pipeline value is absent from title+abstract+MeSH, it may have come from full text: mark INSUFFICIENT_TITLE_ABSTRACT_MESH rather than preferring or rejecting it. If several plausible values or strata cannot be mapped uniquely, mark UNSURE. EFFECT_MEASURE_TYPE by itself is not a reportable discrepancy under the requested comparison; return NO_REPORTABLE_DISCREPANCY for that field unless the type difference makes a numeric claim impossible to align, in which case return UNSURE.

NON-ERRORS
Treat formatting, trailing zeros, and obvious display rounding as equivalent (for example 1, 1.0, and 1.00). Do not report a missing verbal evidence label as an error; annotate evidence_type independently as BIOSPECIMEN, DIETARY_INTAKE, or UNCLEAR.

VERDICTS
Use CHATGPT_CORRECT or CLAUDE_GEMINI_CORRECT only when the allowed evidence unambiguously supports that pipeline claim over the other. Use BOTH_CORRECT for equivalent/simultaneously supported claims, NEITHER_CORRECT when the allowed evidence clearly supplies another answer, NO_REPORTABLE_DISCREPANCY for a non-error, INSUFFICIENT_TITLE_ABSTRACT_MESH when needed information is absent, and UNSURE when relevant information exists but cannot be mapped uniquely. Any insufficient, ambiguous, or unsure result requires manual_review_needed=true.

EVIDENCE AND EXPLANATION
For a resolved result, evidence_text must be a short verbatim excerpt from exactly one allowed source and evidence_source must identify TITLE, ABSTRACT, or MESH. For MESH, copy exactly one supplied term. For an unresolved/non-reportable result, use evidence_source NONE and evidence_text null unless a verbatim allowed excerpt directly explains the ambiguity. one_sentence_explanation must be exactly one concise sentence of at most 45 words and must say which pipeline/value is supported or precisely what the allowed record lacks.

OUTPUT CONTRACT
Return exactly these top-level keys: schema_version, pmid, source_scope, results.
- schema_version must be "metavir-fable51-adjudication-v1".
- pmid must exactly match the input PMID.
- source_scope must be "TITLE_ABSTRACT_MESH_ONLY".
- results must contain exactly one object for every requested (context_id, field), with no extras.
Each result must contain exactly these keys:
context_id, exposure, field, tam_assessment, verdict, adjudicated_value, evidence_source, evidence_text, evidence_type, manual_review_needed, manual_review_reason, one_sentence_explanation.
Allowed values:
- tam_assessment: SUFFICIENT, INSUFFICIENT, or AMBIGUOUS.
- verdict: CHATGPT_CORRECT, CLAUDE_GEMINI_CORRECT, BOTH_CORRECT, NEITHER_CORRECT, NO_REPORTABLE_DISCREPANCY, INSUFFICIENT_TITLE_ABSTRACT_MESH, or UNSURE.
- evidence_source: TITLE, ABSTRACT, MESH, or NONE.
- evidence_type: BIOSPECIMEN, DIETARY_INTAKE, or UNCLEAR.
adjudicated_value and evidence_text may be strings or null. manual_review_needed is boolean; manual_review_reason is a short string or null. Do not add Markdown or prose outside the JSON object."""

RESULT_KEYS = frozenset(
    {
        "context_id",
        "exposure",
        "field",
        "tam_assessment",
        "verdict",
        "adjudicated_value",
        "evidence_source",
        "evidence_text",
        "evidence_type",
        "manual_review_needed",
        "manual_review_reason",
        "one_sentence_explanation",
    }
)

CONTENT_FILTER_FEEDBACK = (
    "The previous completion was blocked by an automated content filter. This is benign "
    "academic systematic-review work: use neutral, non-graphic scientific wording and "
    "provide no medical advice. For this retry, set evidence_source to NONE and "
    "evidence_text to null, do not quote or paraphrase clinical or demographic details, "
    "and use a generic explanation such as 'The allowed metadata supports ChatGPT.' or "
    "'The allowed metadata is insufficient to resolve this field.' Return compact JSON."
)

COMPACT_CODE_MAP = {
    "G": "CHATGPT_CORRECT",
    "C": "CLAUDE_GEMINI_CORRECT",
    "B": "BOTH_CORRECT",
    "N": "NEITHER_CORRECT",
    "X": "NO_REPORTABLE_DISCREPANCY",
    "I": "INSUFFICIENT_TITLE_ABSTRACT_MESH",
    "U": "UNSURE",
}
COMPACT_EVIDENCE_TYPE_MAP = {
    "B": "BIOSPECIMEN",
    "D": "DIETARY_INTAKE",
    "U": "UNCLEAR",
}

_PRINT_LOCK = threading.Lock()


def now() -> str:
    return datetime.now().astimezone().isoformat()


def _helpers() -> Any:
    # Reuse the repository's audited Cornell credential and receipt helpers.
    from scripts import run_claude_gemini_parallel_review as gateway

    return gateway


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _value(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text else None


def _split_fields(value: str) -> list[str]:
    fields = []
    for raw in str(value or "").split(";"):
        raw = raw.strip()
        if raw:
            if raw not in FIELD_MAP:
                raise RuntimeError(f"Unexpected discrepancy field: {raw}")
            fields.append(FIELD_MAP[raw])
    if not fields:
        raise RuntimeError("Extraction discrepancy has no fields")
    return fields


def _pipeline_claim(row: dict[str, str], prefix: str, *, screening: bool) -> dict[str, Any]:
    if screening:
        status = _value(row[f"{prefix}_status"])
        return {
            "screen_status": status,
            "exclusion_code": _value(row["exclusion_code"]) if status == "EXCLUDE" else None,
        }
    return {
        "sample_size": _value(row[f"{prefix}_sample_size"]),
        "effect_measure_type": _value(row[f"{prefix}_effect_type"]),
        "effect_size": _value(row[f"{prefix}_effect_size"]),
        "ci_lower": _value(row[f"{prefix}_ci_lower"]),
        "ci_upper": _value(row[f"{prefix}_ci_upper"]),
        "p_value": _value(row[f"{prefix}_p_value"]),
        "comparison": _value(row[f"{prefix}_comparison"]),
    }


def _load_units() -> list[dict[str, Any]]:
    screening_rows = _read_csv(SCREENING_CSV)
    extraction_rows = _read_csv(EXTRACTION_CSV)
    article_packet = json.loads(ARTICLE_JSON.read_text(encoding="utf-8"))
    articles = {
        str(article.get("pmid") or "").strip(): article
        for article in article_packet.get("articles", [])
    }
    contexts_by_pmid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    author_by_pmid: dict[str, str] = {}

    for row in screening_rows:
        pmid = row["PMID"].strip()
        author_by_pmid.setdefault(pmid, row["first_author_last_name"].strip())
        contexts_by_pmid[pmid].append(
            {
                "context_id": row["context_id"],
                "exposure": row["exposure"],
                "cancer": row["cancer"],
                "discrepancy_type": "SCREENING",
                "requested_fields": ["SCREENING_DECISION"],
                "claims": {
                    "ChatGPT": _pipeline_claim(row, "chatgpt", screening=True),
                    "Claude+Gemini": _pipeline_claim(
                        row, "claude_gemini", screening=True
                    ),
                },
            }
        )

    for row in extraction_rows:
        pmid = row["PMID"].strip()
        author_by_pmid.setdefault(pmid, row["first_author_last_name"].strip())
        contexts_by_pmid[pmid].append(
            {
                "context_id": row["context_id"],
                "exposure": row["exposure"],
                "cancer": row["cancer"],
                "discrepancy_type": "EXTRACTION",
                "requested_fields": _split_fields(row["discrepant_fields"]),
                "claims": {
                    "ChatGPT": _pipeline_claim(row, "chatgpt", screening=False),
                    "Claude+Gemini": _pipeline_claim(
                        row, "claude_gemini", screening=False
                    ),
                },
            }
        )

    units = []
    for pmid in sorted(contexts_by_pmid, key=lambda value: int(value)):
        article = articles.get(pmid)
        if not article:
            raise RuntimeError(f"No local title/abstract/MeSH record for PMID {pmid}")
        source_evidence = {
            "pmid": pmid,
            "title": str(article.get("title") or "").strip(),
            "abstract": str(article.get("abstract") or "").strip(),
            "mesh": [str(term).strip() for term in article.get("mesh", []) if str(term).strip()],
        }
        if set(source_evidence) != {"pmid", "title", "abstract", "mesh"}:
            raise AssertionError("The source-evidence allowlist changed")
        contexts = sorted(
            contexts_by_pmid[pmid],
            key=lambda row: (row["context_id"].casefold(), row["discrepancy_type"]),
        )
        expected = []
        for context in contexts:
            for field in context["requested_fields"]:
                expected.append((context["context_id"], field))
        if len(expected) != len(set(expected)):
            raise RuntimeError(f"Duplicate requested field for PMID {pmid}")
        units.append(
            {
                "pmid": pmid,
                "first_author_last_name": author_by_pmid.get(pmid, ""),
                "source_evidence": source_evidence,
                "contexts": contexts,
                "expected_keys": expected,
            }
        )

    all_contexts = {
        context["context_id"] for unit in units for context in unit["contexts"]
    }
    if len(units) != 303 or len(all_contexts) != 497:
        raise RuntimeError(
            f"Unexpected discrepancy scope: {len(units)} PMIDs / {len(all_contexts)} contexts"
        )
    return units


def _input_payload(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "SOURCE_EVIDENCE": unit["source_evidence"],
        "PIPELINE_CLAIMS_NOT_EVIDENCE": {
            "pmid": unit["pmid"],
            "contexts": unit["contexts"],
        },
    }


def _prompt(unit: dict[str, Any], feedback: str = "") -> str:
    payload = json.dumps(
        _input_payload(unit), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    correction = ""
    if feedback:
        correction = (
            "\n\nYour previous response failed local validation: "
            + feedback
            + " Return a complete corrected JSON object."
        )
    return INSTRUCTIONS + correction + "\n\nINPUT:\n" + payload


def _compact_prompt(unit: dict[str, Any]) -> str:
    """Ask for opaque codes when prose is repeatedly suppressed by the provider."""
    tasks = []
    task_number = 0
    for context in unit["contexts"]:
        for field in context["requested_fields"]:
            task_number += 1
            tasks.append(
                {
                    "n": task_number,
                    "exposure": context["exposure"],
                    "field": field,
                    "chatgpt_claim": context["claims"]["ChatGPT"],
                    "claude_gemini_claim": context["claims"]["Claude+Gemini"],
                }
            )
    compact_input = {
        "source_evidence": unit["source_evidence"],
        "tasks": tasks,
    }
    return (
        "This is a benign scholarly-record classification. Use only the supplied title, "
        "abstract, and MeSH as evidence; the two claims are not evidence. For screening, "
        "include an original human study of the named nutritional exposure and incident "
        "prostate-cancer risk; missing numbers alone are not an exclusion. Exclude reviews, "
        "nonprimary/animal/mechanistic studies, wrong exposure/outcome, postdiagnosis-only "
        "studies, inseparable compounds/results, genetic-only studies, or duplicates only "
        "when the supplied metadata makes that clear. For extraction, sample size is total "
        "participants, never cases; align effect and CI/p to the same exposure, population, "
        "comparison, and model. Treat rounding/trailing zeros as equal. Effect-measure-type "
        "wording alone is X. If required information is absent use I; if present but "
        "ambiguous use U.\n\n"
        "Return ONLY compact JSON with no source quotations, medical prose, explanation, or "
        "Markdown: {\"pmid\":\"exact PMID\",\"answers\":[{\"n\":1,\"code\":\"G\","
        "\"evidence_type\":\"D\"}]}. Include every task number exactly once. code is G "
        "(ChatGPT), C (Claude+Gemini), B (both), N (neither), X (no reportable "
        "discrepancy), I (title/abstract/MeSH insufficient), or U (ambiguous/unsure). "
        "evidence_type is B (biospecimen), D (dietary intake), or U (unclear).\n\nINPUT:\n"
        + json.dumps(
            compact_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _work_key(unit: dict[str, Any], model: str) -> str:
    gateway = _helpers()
    return gateway.stable_digest(
        {
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "model": model,
            "system_prompt": SYSTEM_PROMPT,
            "instructions": INSTRUCTIONS,
            "input": _input_payload(unit),
        }
    )


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _parse_json_output(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("response is not a JSON object")
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("top-level response must be an object")
    return payload


def _validate_evidence(
    result: dict[str, Any], source: dict[str, Any], *, pmid: str
) -> None:
    evidence_source = result["evidence_source"]
    evidence_text = result["evidence_text"]
    if evidence_text is not None and not isinstance(evidence_text, str):
        raise ValueError(f"{pmid}: evidence_text must be a string or null")
    excerpt = _collapse_ws(evidence_text or "")
    if len(excerpt) > 500:
        raise ValueError(f"{pmid}: evidence_text exceeds 500 characters")
    if evidence_source == "NONE":
        if excerpt:
            raise ValueError(f"{pmid}: NONE evidence_source requires null/blank evidence_text")
        return
    if not excerpt:
        raise ValueError(f"{pmid}: non-NONE evidence_source requires evidence_text")
    excerpt_folded = excerpt.casefold()
    if evidence_source in {"TITLE", "ABSTRACT"}:
        allowed = _collapse_ws(source[evidence_source.casefold()]).casefold()
        if excerpt_folded not in allowed:
            raise ValueError(
                f"{pmid}: evidence_text is not a verbatim {evidence_source} excerpt"
            )
    elif evidence_source == "MESH":
        allowed_terms = {_collapse_ws(term).casefold() for term in source["mesh"]}
        if excerpt_folded not in allowed_terms:
            raise ValueError(f"{pmid}: evidence_text is not exactly one supplied MeSH term")


def _validate_payload(
    payload: dict[str, Any], unit: dict[str, Any]
) -> list[dict[str, Any]]:
    if set(payload) != {"schema_version", "pmid", "source_scope", "results"}:
        raise ValueError("top-level keys do not exactly match the contract")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch")
    if str(payload["pmid"]) != unit["pmid"]:
        raise ValueError("PMID mismatch")
    if payload["source_scope"] != SOURCE_SCOPE:
        raise ValueError("source_scope mismatch")
    results = payload["results"]
    if not isinstance(results, list):
        raise ValueError("results must be a list")
    expected = set(tuple(item) for item in unit["expected_keys"])
    observed: set[tuple[str, str]] = set()
    context_map = {context["context_id"]: context for context in unit["contexts"]}
    for result in results:
        if not isinstance(result, dict) or set(result) != RESULT_KEYS:
            raise ValueError("every result must contain exactly the contracted keys")
        context_id = str(result["context_id"])
        field = str(result["field"])
        key = (context_id, field)
        if key not in expected or key in observed:
            raise ValueError(f"unexpected or duplicate result key: {key}")
        observed.add(key)
        context = context_map[context_id]
        if result["exposure"] != context["exposure"]:
            raise ValueError(f"{context_id}: exposure mismatch")
        if field not in VALID_FIELDS:
            raise ValueError(f"{context_id}: invalid field")
        if result["tam_assessment"] not in VALID_TAM_ASSESSMENTS:
            raise ValueError(f"{context_id}/{field}: invalid tam_assessment")
        if result["verdict"] not in VALID_VERDICTS:
            raise ValueError(f"{context_id}/{field}: invalid verdict")
        if result["evidence_source"] not in VALID_EVIDENCE_SOURCES:
            raise ValueError(f"{context_id}/{field}: invalid evidence_source")
        if result["evidence_type"] not in VALID_EVIDENCE_TYPES:
            raise ValueError(f"{context_id}/{field}: invalid evidence_type")
        if result["adjudicated_value"] is not None and not isinstance(
            result["adjudicated_value"], str
        ):
            raise ValueError(f"{context_id}/{field}: adjudicated_value must be string/null")
        if not isinstance(result["manual_review_needed"], bool):
            raise ValueError(f"{context_id}/{field}: manual_review_needed must be boolean")
        reason = result["manual_review_reason"]
        if reason is not None and not isinstance(reason, str):
            raise ValueError(f"{context_id}/{field}: manual_review_reason must be string/null")
        if (
            result["verdict"] in {"INSUFFICIENT_TITLE_ABSTRACT_MESH", "UNSURE"}
            or result["tam_assessment"] != "SUFFICIENT"
        ) and not result["manual_review_needed"]:
            raise ValueError(f"{context_id}/{field}: unresolved result requires manual review")
        explanation = result["one_sentence_explanation"]
        if not isinstance(explanation, str):
            raise ValueError(f"{context_id}/{field}: explanation must be a string")
        explanation = _collapse_ws(explanation)
        if not explanation or len(explanation) > 500 or "\n" in explanation:
            raise ValueError(f"{context_id}/{field}: invalid explanation length/format")
        if explanation[-1] not in ".!?":
            raise ValueError(f"{context_id}/{field}: explanation must end as a sentence")
        result["one_sentence_explanation"] = explanation
        _validate_evidence(result, unit["source_evidence"], pmid=unit["pmid"])
    if observed != expected:
        missing = sorted(expected - observed)
        raise ValueError(f"missing expected result keys: {missing[:5]}")
    return sorted(results, key=lambda row: (row["context_id"].casefold(), row["field"]))


def _standard_payload_from_compact(
    payload: dict[str, Any], unit: dict[str, Any]
) -> dict[str, Any]:
    if set(payload) != {"pmid", "answers"}:
        raise ValueError("compact response keys do not exactly match the contract")
    if str(payload["pmid"]) != unit["pmid"]:
        raise ValueError("compact response PMID mismatch")
    answers = payload["answers"]
    if not isinstance(answers, list):
        raise ValueError("compact answers must be a list")
    by_number: dict[int, dict[str, Any]] = {}
    for answer in answers:
        if not isinstance(answer, dict) or set(answer) != {"n", "code", "evidence_type"}:
            raise ValueError("compact answer keys do not exactly match the contract")
        try:
            number = int(answer["n"])
        except (TypeError, ValueError) as exc:
            raise ValueError("compact answer number must be an integer") from exc
        if number in by_number:
            raise ValueError("duplicate compact answer number")
        if answer["code"] not in COMPACT_CODE_MAP:
            raise ValueError("invalid compact verdict code")
        if answer["evidence_type"] not in COMPACT_EVIDENCE_TYPE_MAP:
            raise ValueError("invalid compact evidence-type code")
        by_number[number] = answer
    expected_numbers = set(range(1, len(unit["expected_keys"]) + 1))
    if set(by_number) != expected_numbers:
        raise ValueError("compact response does not cover every task exactly once")

    contexts = {context["context_id"]: context for context in unit["contexts"]}
    explanations = {
        "CHATGPT_CORRECT": (
            "Fable 5.1 found that the title, abstract, and MeSH support ChatGPT's claim for this field."
        ),
        "CLAUDE_GEMINI_CORRECT": (
            "Fable 5.1 found that the title, abstract, and MeSH support Claude+Gemini's claim for this field."
        ),
        "BOTH_CORRECT": (
            "Fable 5.1 found that the title, abstract, and MeSH support both pipeline claims for this field."
        ),
        "NEITHER_CORRECT": (
            "Fable 5.1 found that the title, abstract, and MeSH support neither pipeline claim for this field."
        ),
        "NO_REPORTABLE_DISCREPANCY": (
            "Fable 5.1 found no reportable discrepancy for this field under the requested comparison rules."
        ),
        "INSUFFICIENT_TITLE_ABSTRACT_MESH": (
            "Fable 5.1 found that the title, abstract, and MeSH do not contain enough information to resolve this field."
        ),
        "UNSURE": (
            "Fable 5.1 found relevant title, abstract, or MeSH information but could not map the competing claims unambiguously."
        ),
    }
    results = []
    for number, (context_id, field) in enumerate(unit["expected_keys"], start=1):
        answer = by_number[number]
        verdict = COMPACT_CODE_MAP[answer["code"]]
        if verdict == "INSUFFICIENT_TITLE_ABSTRACT_MESH":
            assessment = "INSUFFICIENT"
            reason = "Required information is absent from title, abstract, and MeSH."
        elif verdict == "UNSURE":
            assessment = "AMBIGUOUS"
            reason = "The allowed metadata does not map the competing claims unambiguously."
        else:
            assessment = "SUFFICIENT"
            reason = None
        results.append(
            {
                "context_id": context_id,
                "exposure": contexts[context_id]["exposure"],
                "field": field,
                "tam_assessment": assessment,
                "verdict": verdict,
                "adjudicated_value": None,
                "evidence_source": "NONE",
                "evidence_text": None,
                "evidence_type": COMPACT_EVIDENCE_TYPE_MAP[answer["evidence_type"]],
                "manual_review_needed": verdict
                in {"INSUFFICIENT_TITLE_ABSTRACT_MESH", "UNSURE"},
                "manual_review_reason": reason,
                "one_sentence_explanation": explanations[verdict],
            }
        )
    standard = {
        "schema_version": SCHEMA_VERSION,
        "pmid": unit["pmid"],
        "source_scope": SOURCE_SCOPE,
        "results": results,
    }
    standard["results"] = _validate_payload(standard, unit)
    return standard


def _attempt_number(pmid: str) -> int:
    attempt_dir = CHECKPOINT_DIR / "attempts"
    values = []
    for path in attempt_dir.glob(f"{pmid}_*.json"):
        match = re.search(r"_([0-9]+)\.json$", path.name)
        if match:
            values.append(int(match.group(1)))
    for path in (CHECKPOINT_DIR / "dispatch").glob(f"{pmid}_*.json"):
        match = re.search(r"_([0-9]+)\.json$", path.name)
        if match:
            values.append(int(match.group(1)))
    return max(values, default=0) + 1


def _compatible_cached_subset(
    record: dict[str, Any], unit: dict[str, Any], model: str
) -> dict[str, Any] | None:
    """Reuse a cached response only when the current request is an exact subset."""
    if (
        str(record.get("pmid") or "") != unit["pmid"]
        or record.get("model_requested") != model
    ):
        return None
    input_path = CHECKPOINT_DIR / "inputs" / f"{unit['pmid']}.json"
    prior_input = json.loads(input_path.read_text(encoding="utf-8"))
    if (
        prior_input.get("work_key") != record.get("work_key")
        or prior_input.get("protocol_version") != PROTOCOL_VERSION
        or prior_input.get("model_requested") != model
    ):
        return None
    prior_request = prior_input.get("request_payload") or {}
    current_request = _input_payload(unit)
    if prior_request.get("SOURCE_EVIDENCE") != current_request["SOURCE_EVIDENCE"]:
        return None
    prior_claims = prior_request.get("PIPELINE_CLAIMS_NOT_EVIDENCE") or {}
    if str(prior_claims.get("pmid") or "") != unit["pmid"]:
        return None
    prior_context_list = prior_claims.get("contexts", [])
    if not isinstance(prior_context_list, list) or any(
        not isinstance(row, dict) or not str(row.get("context_id") or "")
        for row in prior_context_list
    ):
        return None
    prior_contexts = {row["context_id"]: row for row in prior_context_list}
    if len(prior_contexts) != len(prior_context_list):
        return None
    historical_expected = [
        (context["context_id"], field)
        for context in prior_context_list
        for field in context.get("requested_fields", [])
    ]
    if len(historical_expected) != len(set(historical_expected)):
        return None
    historical_unit = {
        "pmid": unit["pmid"],
        "source_evidence": prior_request["SOURCE_EVIDENCE"],
        "contexts": prior_context_list,
        "expected_keys": historical_expected,
    }
    if _work_key(historical_unit, model) != record.get("work_key"):
        return None
    _validate_payload(record.get("payload"), historical_unit)
    for current in unit["contexts"]:
        prior = prior_contexts.get(current["context_id"])
        if prior is None:
            return None
        for key in ("context_id", "exposure", "cancer", "discrepancy_type", "claims"):
            if prior.get(key) != current.get(key):
                return None
        if not set(current["requested_fields"]).issubset(prior.get("requested_fields", [])):
            return None

    expected = {tuple(item) for item in unit["expected_keys"]}
    payload = dict(record.get("payload") or {})
    payload["results"] = [
        row for row in payload.get("results", [])
        if (str(row.get("context_id")), str(row.get("field"))) in expected
    ]
    candidate = dict(record)
    candidate["work_key"] = _work_key(unit, model)
    candidate["payload"] = payload
    candidate["cost_scope"] = "MIXED_CURRENT_AND_HISTORICAL_NONREPORTABLE_REVIEW"
    candidate["cost_exposures"] = "; ".join(
        sorted(
            {
                str(context.get("exposure") or "")
                for context in prior_contexts.values()
                if str(context.get("exposure") or "")
            },
            key=str.casefold,
        )
    )
    candidate["cost_context_count"] = len(prior_contexts)
    candidate["cost_field_count"] = sum(
        len(context.get("requested_fields", [])) for context in prior_contexts.values()
    )
    _validate_payload(payload, unit)
    return candidate


def _load_valid_cache(
    unit: dict[str, Any],
    model: str,
    *,
    allow_compatible_superset: bool = False,
) -> dict[str, Any] | None:
    path = CHECKPOINT_DIR / "results" / f"{unit['pmid']}.json"
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("work_key") != _work_key(unit, model):
            return (
                _compatible_cached_subset(record, unit, model)
                if allow_compatible_superset
                else None
            )
        payload = record.get("payload")
        _validate_payload(payload, unit)
        return record
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _prior_content_filter_count(pmid: str) -> int:
    count = 0
    for path in (CHECKPOINT_DIR / "attempts").glob(f"{pmid}_*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status = str(record.get("validation_status") or "")
        if (
            "content_filter" in status
            or str(record.get("finish_reason") or "") == "content_filter"
        ):
            count += 1
        elif (
            not str(record.get("output_text") or "").strip()
            and int((record.get("usage") or {}).get("output_tokens") or 0) > 0
            and status == "invalid"
        ):
            # Earlier runner versions did not persist finish_reason, but this
            # exact empty-content/provider-token pattern was diagnostically
            # confirmed to be finish_reason=content_filter.
            count += 1
    return count


def _operational_filter_payload(unit: dict[str, Any]) -> dict[str, Any]:
    contexts = {context["context_id"]: context for context in unit["contexts"]}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "pmid": unit["pmid"],
        "source_scope": SOURCE_SCOPE,
        "results": [
            {
                "context_id": context_id,
                "exposure": contexts[context_id]["exposure"],
                "field": field,
                "tam_assessment": "NOT_ASSESSED_PROVIDER_FILTER",
                "verdict": "UNSURE",
                "adjudicated_value": None,
                "evidence_source": "NONE",
                "evidence_text": None,
                "evidence_type": "UNCLEAR",
                "manual_review_needed": True,
                "manual_review_reason": (
                    "Cornell's provider content filter suppressed every Fable 5.1 response."
                ),
                "one_sentence_explanation": (
                    "Cornell's provider content filter suppressed every Fable 5.1 response, so this field could not be adjudicated."
                ),
            }
            for context_id, field in unit["expected_keys"]
        ],
    }
    payload["results"] = _validate_payload(payload, unit)
    return payload


def _run_unit(
    client: Any,
    unit: dict[str, Any],
    *,
    model: str,
    max_attempts: int,
    max_tokens: int,
    force: bool,
) -> dict[str, Any]:
    gateway = _helpers()
    cached = None if force else _load_valid_cache(unit, model)
    if cached is not None:
        return {"pmid": unit["pmid"], "status": "cached", "cost": ""}

    work_key = _work_key(unit, model)
    input_path = CHECKPOINT_DIR / "inputs" / f"{unit['pmid']}.json"
    gateway.save_json(
        input_path,
        {
            "schema_version": 1,
            "created_at": now(),
            "protocol_version": PROTOCOL_VERSION,
            "model_requested": model,
            "work_key": work_key,
            "allowed_scientific_source_fields": ["title", "abstract", "mesh"],
            "explicitly_excluded_source_types": [
                "full_text",
                "PMC",
                "DOI_or_article_URL",
                "prior_evidence_excerpt",
                "prior_exclusion_reason",
                "prior_brief_explanation",
            ],
            "request_payload": _input_payload(unit),
        },
    )

    prior_content_filters = _prior_content_filter_count(unit["pmid"])
    if prior_content_filters >= 3:
        attempts, _ = _load_attempts(unit["pmid"])
        last_attempt = max(attempts, key=lambda row: int(row.get("attempt") or 0))
        payload = _operational_filter_payload(unit)
        gateway.save_json(
            CHECKPOINT_DIR / "results" / f"{unit['pmid']}.json",
            {
                "schema_version": 1,
                "accepted_at": now(),
                "pmid": unit["pmid"],
                "work_key": work_key,
                "model_requested": model,
                "model_reported": last_attempt.get("model_reported", ""),
                "accepted_attempt": None,
                "last_attempt": last_attempt.get("attempt"),
                "attempt_path": "",
                "adjudication_status": "PROVIDER_CONTENT_FILTERED",
                "response_mode": "operational_unresolved",
                "payload": payload,
            },
        )
        return {
            "pmid": unit["pmid"],
            "status": "provider_content_filtered",
            "cost": "no additional call",
        }
    content_filter_failures = min(prior_content_filters, 2)
    compact_mode = content_filter_failures >= 2
    feedback = CONTENT_FILTER_FEEDBACK if content_filter_failures == 1 else ""
    validation_failures = 0
    last_error = ""
    for local_attempt in range(1, max_attempts + 1):
        attempt = _attempt_number(unit["pmid"])
        prompt = _compact_prompt(unit) if compact_mode else _prompt(unit, feedback)
        input_digest = gateway.stable_digest(
            {
                "model": model,
                "system": SYSTEM_PROMPT,
                "prompt": prompt,
                "work_key": work_key,
            }
        )
        dispatch_path = CHECKPOINT_DIR / "dispatch" / f"{unit['pmid']}_{attempt:04d}.json"
        attempt_path = CHECKPOINT_DIR / "attempts" / f"{unit['pmid']}_{attempt:04d}.json"
        requested_at = now()
        gateway.save_json(
            dispatch_path,
            {
                "schema_version": 1,
                "requested_at": requested_at,
                "pmid": unit["pmid"],
                "work_key": work_key,
                "attempt": attempt,
                "model_requested": model,
                "input_digest": input_digest,
                "response_mode": "compact_code_only" if compact_mode else "standard",
                "status": "dispatch_journaled_before_gateway_call",
            },
        )
        raw_response = None
        response = None
        headers: dict[str, str] = {}
        try:
            completions = client.chat.completions
            raw_api = getattr(completions, "with_raw_response", None)
            if raw_api is not None:
                raw_response = raw_api.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=max_tokens,
                )
                headers = gateway._safe_headers(getattr(raw_response, "headers", None))
                response = raw_response.parse()
            else:
                response = completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=max_tokens,
                )
            output_text = gateway._chat_completion_text(response)
            usage = gateway._usage_dict(response)
            cost, cost_header_status = gateway._validated_gateway_cost(headers)
            finish_reason = str(
                getattr((getattr(response, "choices", None) or [None])[0], "finish_reason", "")
                or ""
            )
            record = {
                "schema_version": 1,
                "requested_at": requested_at,
                "received_at": now(),
                "pmid": unit["pmid"],
                "context_ids": [context["context_id"] for context in unit["contexts"]],
                "work_key": work_key,
                "attempt": attempt,
                "model_requested": model,
                "model_reported": str(getattr(response, "model", "") or ""),
                "input_digest": input_digest,
                "response_mode": "compact_code_only" if compact_mode else "standard",
                "request_id": str(
                    getattr(response, "_request_id", "")
                    or headers.get("x-request-id")
                    or ""
                ),
                "finish_reason": finish_reason,
                "usage": usage,
                "response_cost_usd": cost,
                "cost_header_status": cost_header_status,
                "cost_provenance": (
                    "cornell_gateway_x-litellm-response-cost"
                    if cost is not None
                    else "unavailable_no_local_estimate"
                ),
                "safe_gateway_headers": headers,
                "output_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
                "output_text": output_text,
                "validation_status": "received",
                "scientific_result_accepted": False,
                "validation_error": "",
            }
            # Persist the raw response and billable receipt before parsing JSON.
            gateway.save_json(attempt_path, record)
            if finish_reason == "content_filter":
                content_filter_failures += 1
                last_error = (
                    "gateway finish_reason=content_filter returned no standard content"
                )
                record["validation_status"] = "content_filter"
                record["validation_error"] = last_error
                gateway.save_json(attempt_path, record)
                if content_filter_failures == 1:
                    feedback = CONTENT_FILTER_FEEDBACK
                    compact_mode = False
                elif content_filter_failures == 2:
                    compact_mode = True
                else:
                    payload = _operational_filter_payload(unit)
                    gateway.save_json(
                        CHECKPOINT_DIR / "results" / f"{unit['pmid']}.json",
                        {
                            "schema_version": 1,
                            "accepted_at": now(),
                            "pmid": unit["pmid"],
                            "work_key": work_key,
                            "model_requested": model,
                            "model_reported": record["model_reported"],
                            "accepted_attempt": None,
                            "last_attempt": attempt,
                            "attempt_path": str(attempt_path.relative_to(REPO_ROOT)),
                            "adjudication_status": "PROVIDER_CONTENT_FILTERED",
                            "response_mode": "operational_unresolved",
                            "payload": payload,
                        },
                    )
                    return {
                        "pmid": unit["pmid"],
                        "status": "provider_content_filtered",
                        "cost": cost or "unavailable",
                    }
                if local_attempt < max_attempts:
                    time.sleep(min(4.0, 1.0 * local_attempt))
                    continue
                break
            try:
                raw_payload = _parse_json_output(output_text)
                payload = (
                    _standard_payload_from_compact(raw_payload, unit)
                    if compact_mode
                    else raw_payload
                )
                results = _validate_payload(payload, unit)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                validation_failures += 1
                last_error = gateway.safe_error(exc)
                record["validation_status"] = "invalid"
                record["validation_error"] = last_error
                gateway.save_json(attempt_path, record)
                feedback = last_error
                if validation_failures >= 2:
                    break
                time.sleep(min(4.0, 1.0 * local_attempt))
                continue
            payload["results"] = results
            record["validation_status"] = "valid"
            record["scientific_result_accepted"] = True
            gateway.save_json(attempt_path, record)
            gateway.save_json(
                CHECKPOINT_DIR / "results" / f"{unit['pmid']}.json",
                {
                    "schema_version": 1,
                    "accepted_at": now(),
                    "pmid": unit["pmid"],
                    "work_key": work_key,
                    "model_requested": model,
                    "model_reported": record["model_reported"],
                    "accepted_attempt": attempt,
                    "attempt_path": str(attempt_path.relative_to(REPO_ROOT)),
                    "adjudication_status": "MODEL_ADJUDICATED",
                    "response_mode": "compact_code_only" if compact_mode else "standard",
                    "payload": payload,
                },
            )
            return {
                "pmid": unit["pmid"],
                "status": "completed",
                "cost": cost or "unavailable",
            }
        except Exception as exc:  # transport/SDK failure; never a scientific verdict
            last_error = gateway.safe_error(exc)
            if raw_response is not None:
                headers = gateway._safe_headers(getattr(raw_response, "headers", None))
            cost, cost_header_status = gateway._validated_gateway_cost(headers)
            gateway.save_json(
                attempt_path,
                {
                    "schema_version": 1,
                    "requested_at": requested_at,
                    "received_at": now(),
                    "pmid": unit["pmid"],
                    "context_ids": [context["context_id"] for context in unit["contexts"]],
                    "work_key": work_key,
                    "attempt": attempt,
                    "model_requested": model,
                    "model_reported": "",
                    "input_digest": input_digest,
                    "response_mode": "compact_code_only" if compact_mode else "standard",
                    "request_id": str(headers.get("x-request-id") or ""),
                    "finish_reason": "",
                    "usage": {
                        "input_tokens": 0,
                        "cached_input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                    },
                    "response_cost_usd": cost,
                    "cost_header_status": cost_header_status,
                    "cost_provenance": (
                        "cornell_gateway_x-litellm-response-cost"
                        if cost is not None
                        else "unavailable_no_local_estimate"
                    ),
                    "safe_gateway_headers": headers,
                    "output_sha256": "",
                    "output_text": "",
                    "validation_status": "transport_error",
                    "scientific_result_accepted": False,
                    "validation_error": last_error,
                },
            )
            if local_attempt < max_attempts:
                time.sleep(min(8.0, 2.0 * local_attempt))
                continue
    return {"pmid": unit["pmid"], "status": "failed", "error": last_error}


def _verify_model(client: Any, model: str) -> list[str]:
    page = client.models.list()
    model_ids = sorted(
        str(getattr(item, "id", "") or "") for item in getattr(page, "data", [])
    )
    if model not in model_ids:
        matches = [
            item
            for item in model_ids
            if "fable" in item.casefold() and re.search(r"(?:^|[-_.])5[-_.]?1(?:$|[-_.])", item)
        ]
        raise RuntimeError(
            f"Exact requested model {model!r} is unavailable; possible matches: {matches}"
        )
    return model_ids


def _atomic_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _claim_for_field(context: dict[str, Any], pipeline: str, field: str) -> str:
    claim = context["claims"][pipeline]
    if field == "SCREENING_DECISION":
        return json.dumps(claim, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if field == "SAMPLE_SIZE":
        return str(claim.get("sample_size") or "")
    if field == "EFFECT_MEASURE_TYPE":
        return str(claim.get("effect_measure_type") or "")
    if field == "EFFECT_SIZE":
        selected = {
            "effect_measure_type": claim.get("effect_measure_type"),
            "effect_size": claim.get("effect_size"),
            "comparison": claim.get("comparison"),
        }
    elif field == "CI_OR_P_VALUE":
        selected = {
            "effect_measure_type": claim.get("effect_measure_type"),
            "effect_size": claim.get("effect_size"),
            "ci_lower": claim.get("ci_lower"),
            "ci_upper": claim.get("ci_upper"),
            "p_value": claim.get("p_value"),
            "comparison": claim.get("comparison"),
        }
    else:
        raise RuntimeError(f"Unexpected field {field}")
    return json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _correct_pipeline(verdict: str) -> str:
    return {
        "CHATGPT_CORRECT": "ChatGPT",
        "CLAUDE_GEMINI_CORRECT": "Claude+Gemini",
        "BOTH_CORRECT": "Both",
        "NEITHER_CORRECT": "Neither",
        "NO_REPORTABLE_DISCREPANCY": "No reportable discrepancy",
        "INSUFFICIENT_TITLE_ABSTRACT_MESH": "Undetermined",
        "UNSURE": "Undetermined",
    }[verdict]


def _missing_metadata(source: dict[str, Any]) -> str:
    missing = []
    if not source["title"]:
        missing.append("title")
    if not source["abstract"]:
        missing.append("abstract")
    if not source["mesh"]:
        missing.append("MeSH")
    return "; ".join(missing)


def _load_attempts(pmid: str) -> tuple[list[dict[str, Any]], int]:
    attempts = []
    attempt_numbers = set()
    for path in sorted((CHECKPOINT_DIR / "attempts").glob(f"{pmid}_*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        attempts.append(row)
        attempt_numbers.add(int(row.get("attempt") or 0))
    orphan_dispatches = 0
    for path in (CHECKPOINT_DIR / "dispatch").glob(f"{pmid}_*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            attempt = int(row.get("attempt") or 0)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if attempt not in attempt_numbers:
            orphan_dispatches += 1
    return attempts, orphan_dispatches


def _automatic_merge_ready(
    *,
    adjudication_status: str,
    verdict: str,
    assessment: str,
    manual_review_needed: bool,
    evidence_source: str,
    evidence_text: Any,
) -> bool:
    if (
        adjudication_status != "MODEL_ADJUDICATED"
        or assessment != "SUFFICIENT"
        or manual_review_needed
    ):
        return False
    if verdict == "NO_REPORTABLE_DISCREPANCY":
        return True
    return (
        verdict in {"CHATGPT_CORRECT", "CLAUDE_GEMINI_CORRECT"}
        and evidence_source in {"TITLE", "ABSTRACT", "MESH"}
        and bool(str(evidence_text or "").strip())
    )


def _manual_review_reason(result: dict[str, Any], automatic_ready: bool) -> str:
    existing = str(result.get("manual_review_reason") or "").strip()
    if existing or automatic_ready:
        return existing
    verdict = result["verdict"]
    if verdict in {"CHATGPT_CORRECT", "CLAUDE_GEMINI_CORRECT"}:
        return (
            "Fable selected a pipeline but its saved response lacks a supporting "
            "title, abstract, or MeSH excerpt."
        )
    if verdict == "BOTH_CORRECT":
        return "Fable supported both pipeline claims, so a curator must choose the combined value."
    if verdict == "NEITHER_CORRECT":
        return "Fable supported neither pipeline claim, so a curator must supply or choose the combined value."
    return "This field does not satisfy the requirements for automatic combination."


def _cost_provenance(cost_status: str, attempts_with_cost: int, attempt_count: int) -> str:
    if cost_status == "complete":
        return "Cornell gateway x-litellm-response-cost; all attempts included"
    if attempts_with_cost:
        return (
            "Cornell gateway x-litellm-response-cost; known-cost attempts only "
            f"({attempts_with_cost}/{attempt_count}); total is incomplete"
        )
    if cost_status == "unknown_interrupted":
        return (
            "No gateway cost was recorded; interrupted dispatches may have incurred "
            "unrecorded cost and no local estimate was substituted"
        )
    return "unavailable; no local price estimate substituted"


def export_csvs(units: list[dict[str, Any]], *, model: str) -> list[Path]:
    main_path = OUTPUT_DIR / "fable51Adjudication.csv"
    unsure_path = OUTPUT_DIR / "fable51InsufficientOrUnsure.csv"
    cost_path = OUTPUT_DIR / "fable51Cost.csv"
    previous_cost_rows: list[dict[str, str]] = []
    if cost_path.exists():
        with cost_path.open(newline="", encoding="utf-8-sig") as handle:
            previous_cost_rows = list(csv.DictReader(handle))
    main_rows: list[dict[str, Any]] = []
    unsure_rows: list[dict[str, Any]] = []
    cost_rows: list[dict[str, Any]] = []

    for unit in units:
        cached = _load_valid_cache(unit, model, allow_compatible_superset=True)
        if cached is None:
            raise RuntimeError(f"No valid Fable adjudication cache for PMID {unit['pmid']}")
        result_map = {
            (row["context_id"], row["field"]): row
            for row in cached["payload"]["results"]
        }
        context_map = {context["context_id"]: context for context in unit["contexts"]}
        missing_fields = _missing_metadata(unit["source_evidence"])
        for context_id, field in unit["expected_keys"]:
            context = context_map[context_id]
            result = result_map[(context_id, field)]
            adjudication_status = cached.get("adjudication_status", "MODEL_ADJUDICATED")
            automatic_ready = _automatic_merge_ready(
                adjudication_status=adjudication_status,
                verdict=result["verdict"],
                assessment=result["tam_assessment"],
                manual_review_needed=result["manual_review_needed"],
                evidence_source=result["evidence_source"],
                evidence_text=result["evidence_text"],
            )
            effective_manual_review = result["manual_review_needed"] or not automatic_ready
            manual_reason = _manual_review_reason(result, automatic_ready)
            main_rows.append(
                {
                    "PMID": unit["pmid"],
                    "first_author_last_name": unit["first_author_last_name"],
                    "exposure": context["exposure"],
                    "cancer": context["cancer"],
                    "context_id": context_id,
                    "discrepancy_type": context["discrepancy_type"],
                    "discrepant_field": field,
                    "chatgpt_claim": _claim_for_field(context, "ChatGPT", field),
                    "claude_gemini_claim": _claim_for_field(context, "Claude+Gemini", field),
                    "fable51_verdict": result["verdict"],
                    "correct_pipeline": _correct_pipeline(result["verdict"]),
                    "fable51_adjudicated_value": result["adjudicated_value"] or "",
                    "title_abstract_mesh_assessment": result["tam_assessment"],
                    "fable51_evidence_type": result["evidence_type"],
                    "fable51_evidence_source": result["evidence_source"],
                    "fable51_supporting_excerpt": result["evidence_text"] or "",
                    "fable51_manual_review_needed": str(effective_manual_review).lower(),
                    "fable51_manual_review_reason": manual_reason,
                    "one_sentence_explanation": result["one_sentence_explanation"],
                    "fable51_adjudication_status": adjudication_status,
                    "fable51_response_mode": cached.get("response_mode", "standard"),
                    "model_requested": model,
                    "model_reported": cached.get("model_reported", ""),
                    "source_scope": SOURCE_SCOPE,
                    "missing_allowed_metadata_fields": missing_fields,
                    "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{unit['pmid']}/",
                }
            )
            if effective_manual_review:
                unsure_rows.append(
                    {
                        "PMID": unit["pmid"],
                        "first_author_last_name": unit["first_author_last_name"],
                        "exposure": context["exposure"],
                        "cancer": context["cancer"],
                        "context_id": context_id,
                        "discrepant_field": field,
                        "fable51_verdict": result["verdict"],
                        "title_abstract_mesh_assessment": result["tam_assessment"],
                        "missing_allowed_metadata_fields": missing_fields,
                        "manual_review_reason": manual_reason,
                        "one_sentence_explanation": result["one_sentence_explanation"],
                        "fable51_adjudication_status": cached.get(
                            "adjudication_status", "MODEL_ADJUDICATED"
                        ),
                        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{unit['pmid']}/",
                    }
                )

        attempts, orphan_dispatches = _load_attempts(unit["pmid"])
        known_cost = Decimal("0")
        attempts_with_cost = 0
        usage = defaultdict(int)
        reported_models = set()
        errors = []
        for attempt in attempts:
            raw_cost = attempt.get("response_cost_usd")
            if raw_cost not in (None, ""):
                known_cost += Decimal(str(raw_cost))
                attempts_with_cost += 1
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
            ):
                usage[key] += int((attempt.get("usage") or {}).get(key) or 0)
            if attempt.get("model_reported"):
                reported_models.add(str(attempt["model_reported"]))
            if attempt.get("validation_error"):
                errors.append(str(attempt["validation_error"]))
        attempt_count = len(attempts) + orphan_dispatches
        if orphan_dispatches:
            cost_status = "unknown_interrupted"
            total_cost = ""
        elif attempt_count and attempts_with_cost == attempt_count:
            cost_status = "complete"
            total_cost = format(known_cost, "f")
        elif attempts_with_cost:
            cost_status = "partial"
            total_cost = ""
        else:
            cost_status = "unavailable"
            total_cost = ""
        cost_rows.append(
            {
                "PMID": unit["pmid"],
                "first_author_last_name": unit["first_author_last_name"],
                "exposures": cached.get("cost_exposures")
                or "; ".join(
                    sorted(
                        {context["exposure"] for context in unit["contexts"]},
                        key=str.casefold,
                    )
                ),
                "context_count": cached.get("cost_context_count", len(unit["contexts"])),
                "adjudicated_field_count": cached.get(
                    "cost_field_count", len(unit["expected_keys"])
                ),
                "model_requested": model,
                "model_reported": "; ".join(sorted(reported_models)),
                "attempt_count": attempt_count,
                "attempts_with_cost": attempts_with_cost,
                "input_tokens": usage["input_tokens"],
                "cached_input_tokens": usage["cached_input_tokens"],
                "output_tokens": usage["output_tokens"],
                "reasoning_tokens": usage["reasoning_tokens"],
                "total_tokens": usage["input_tokens"] + usage["output_tokens"],
                "known_cost_usd": format(known_cost, "f") if attempts_with_cost else "",
                "total_cost_usd": total_cost,
                "cost_status": cost_status,
                "cost_provenance": (
                    _cost_provenance(cost_status, attempts_with_cost, attempt_count)
                ),
                "cost_scope": cached.get(
                    "cost_scope", "CURRENT_REPORTABLE_DISCREPANCY_SCOPE"
                ),
                "adjudication_status": cached.get(
                    "adjudication_status", "MODEL_ADJUDICATED"
                ),
                "error": " | ".join(dict.fromkeys(errors)),
            }
        )

    current_pmids = {unit["pmid"] for unit in units}
    for previous in previous_cost_rows:
        if previous.get("PMID") in current_pmids:
            continue
        legacy = dict(previous)
        legacy["cost_scope"] = "HISTORICAL_NONREPORTABLE_REVIEW"
        legacy["cost_provenance"] = _cost_provenance(
            legacy.get("cost_status", ""),
            int(legacy.get("attempts_with_cost") or 0),
            int(legacy.get("attempt_count") or 0),
        )
        cost_rows.append(legacy)

    main_rows.sort(
        key=lambda row: (int(row["PMID"]), row["exposure"].casefold(), row["discrepant_field"])
    )
    unsure_rows.sort(
        key=lambda row: (int(row["PMID"]), row["exposure"].casefold(), row["discrepant_field"])
    )
    cost_rows.sort(key=lambda row: int(row["PMID"]))

    _atomic_csv(main_path, list(main_rows[0]), main_rows)
    _atomic_csv(
        unsure_path,
        [
            "PMID",
            "first_author_last_name",
            "exposure",
            "cancer",
            "context_id",
            "discrepant_field",
            "fable51_verdict",
            "title_abstract_mesh_assessment",
            "missing_allowed_metadata_fields",
            "manual_review_reason",
            "one_sentence_explanation",
            "fable51_adjudication_status",
            "pubmed_url",
        ],
        unsure_rows,
    )
    _atomic_csv(cost_path, list(cost_rows[0]), cost_rows)

    # Re-open every deliverable and enforce structural completeness.
    with main_path.open(newline="", encoding="utf-8") as handle:
        checked_main = list(csv.DictReader(handle))
    with unsure_path.open(newline="", encoding="utf-8") as handle:
        checked_unsure = list(csv.DictReader(handle))
    with cost_path.open(newline="", encoding="utf-8") as handle:
        checked_cost = list(csv.DictReader(handle))
    expected_field_count = sum(len(unit["expected_keys"]) for unit in units)
    if len(checked_main) != expected_field_count:
        raise RuntimeError("Main adjudication CSV failed completeness validation")
    if (
        len(checked_cost) != len(cost_rows)
        or len({row["PMID"] for row in checked_cost}) != len(cost_rows)
        or not {unit["pmid"] for unit in units}.issubset(
            {row["PMID"] for row in checked_cost}
        )
    ):
        raise RuntimeError("Cost CSV failed one-row-per-PMID validation")
    if any(not row["one_sentence_explanation"] for row in checked_main):
        raise RuntimeError("Main adjudication CSV has a blank explanation")
    expected_unsure = sum(
        row["fable51_manual_review_needed"] == "true" for row in checked_main
    )
    if len(checked_unsure) != expected_unsure:
        raise RuntimeError("Insufficient/unsure CSV failed subset validation")
    return [main_path, unsure_path, cost_path]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-only", action="store_true")
    mode.add_argument("--export-only", action="store_true")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-pmids", type=int)
    parser.add_argument("--pmid", action="append", default=[])
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-model-check", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    units = _load_units()
    if args.pmid:
        requested = set(args.pmid)
        units = [unit for unit in units if unit["pmid"] in requested]
        missing = sorted(requested - {unit["pmid"] for unit in units})
        if missing:
            raise RuntimeError(f"Requested PMID(s) are outside discrepancy scope: {missing}")
    if args.max_pmids is not None:
        if args.max_pmids < 1:
            raise RuntimeError("--max-pmids must be positive")
        units = units[: args.max_pmids]
    if not 1 <= args.workers <= 16:
        raise RuntimeError("--workers must be between 1 and 16")
    if not 1 <= args.max_attempts <= 5:
        raise RuntimeError("--max-attempts must be between 1 and 5")

    run_calls = not args.export_only
    export = not args.run_only
    if run_calls:
        gateway = _helpers()
        client = gateway._configure_client()
        if not args.skip_model_check:
            _verify_model(client, args.model)
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        gateway.save_json(
            CHECKPOINT_DIR / "run_manifest.json",
            {
                "schema_version": 1,
                "updated_at": now(),
                "protocol_version": PROTOCOL_VERSION,
                "model_requested": args.model,
                "source_scope": SOURCE_SCOPE,
                "article_units_selected": len(units),
                "context_count_selected": sum(len(unit["contexts"]) for unit in units),
                "field_count_selected": sum(len(unit["expected_keys"]) for unit in units),
                "source_files": [
                    str(SCREENING_CSV.relative_to(REPO_ROOT)),
                    str(EXTRACTION_CSV.relative_to(REPO_ROOT)),
                    str(ARTICLE_JSON.relative_to(REPO_ROOT)),
                ],
            },
        )
        failures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    _run_unit,
                    client,
                    unit,
                    model=args.model,
                    max_attempts=args.max_attempts,
                    max_tokens=args.max_tokens,
                    force=args.force,
                ): unit["pmid"]
                for unit in units
            }
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                pmid = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"pmid": pmid, "status": "failed", "error": gateway.safe_error(exc)}
                completed += 1
                if result["status"] == "failed":
                    failures.append(result)
                with _PRINT_LOCK:
                    detail = result.get("cost") or result.get("error") or ""
                    print(
                        f"[{completed}/{len(units)}] PMID {pmid}: {result['status']} {detail}",
                        flush=True,
                    )
        if failures:
            gateway.save_json(CHECKPOINT_DIR / "failures.json", failures)
            raise RuntimeError(
                f"{len(failures)} PMID work unit(s) failed; rerun to resume before export"
            )

    if export:
        paths = export_csvs(units, model=args.model)
        for path in paths:
            print(path)


if __name__ == "__main__":
    main()
