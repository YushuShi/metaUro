"""Auditable ChatGPT screening with a dormant DeepSeek validation track.

This module deliberately contains no PubMed discovery or cache mutation code.
It consumes the immutable ``candidates.json`` packet, writes model-specific
screening packets, and creates a ChatGPT-canonical final packet. DeepSeek V4 is
retained as a validation contract, but is inactive until Cornell enables those
models. Keeping the model runner here prevents provider-specific patches from
leaking through the rest of the application.
"""

from __future__ import annotations

from scripts import dietary_only_policy as dietary_policy

from scripts import relaxed_poolability as relaxed_policy

import json
import math
import hashlib
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI
from effect_measure import normalize_effect_type


CHATGPT_FIRST_MODEL = "gpt-5.6-luna"
CHATGPT_SECOND_MODEL = "gpt-5.6-sol"
CHATGPT_FULL_TEXT_MODEL = "gpt-5.6-luna"
DEEPSEEK_FIRST_MODEL = "deepseek-v4-flash"
DEEPSEEK_SECOND_MODEL = "deepseek-v4-pro"
DEEPSEEK_FULL_TEXT_MODEL = "deepseek-v4-flash"

TRACK_NAMES = ("chatgpt", "deepseek")
DEFAULT_ACTIVE_TRACK_NAMES = ("chatgpt",)
DEEPSEEK_VALIDATION_STATUS = "pending_gateway_enablement"
DEEPSEEK_VALIDATION_REASON = (
    "DeepSeek V4 validation is pending because Cornell has not enabled "
    "deepseek-v4-flash and deepseek-v4-pro on the gateway."
)
KEY_EVIDENCE_FIELDS = ("effect_size", "effect_type", "ci_lower", "ci_upper", "total_n")
EXTRACTION_FIELDS = (
    "effect_size",
    "effect_type",
    "ci_lower",
    "ci_upper",
    "p_value",
    "ci_confidence_level",
    "total_n",
    "cases",
    "comparison_type",
    "design",
    "timing",
    "continent",
    "country",
    "location",
    "ethnicity",
    "population",
    "exposure_subtype",
    "exposure_measurement_type",
    "exposure_classification_ambiguous",
    "wald_inputs_same_estimate",
    "needs_inversion",
)
EVIDENCE_FIELDS = (
    "sample_size",
    "cases",
    "effect_size",
    "confidence_interval",
    "p_value",
    "comparison",
    "design",
    "timing",
    "continent",
    "ethnicity",
    "location",
    "exposure_measurement",
)
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
JBI_TOTALS = {"cohort": 11, "case_control": 10, "cross_sectional": 8, "rct": 13}
SCREENING_PROTOCOL_VERSION = 9
POOLABILITY_POLICY_VERSION = "relaxed-incidence-v1"
WALD_RATIO_EFFECT_TYPES = frozenset({"OR", "RR", "IRR", "HR"})
WALD_95_Z = NormalDist().inv_cdf(0.975)
WALD_CONFIDENCE_INTERVAL_METHOD = (
    "95% Wald CI from effect and two-sided p value on the log scale"
)
WALD_P_VALUE_METHOD = "two-sided Wald test from effect and 95% CI on the log scale"
FULL_TEXT_MANUAL_ADJUDICATIONS_FILENAME = "full_text_manual_adjudications.json"


FIRST_SYSTEM = (
    "You are the sensitive first screening layer for a systematic review of "
    "nutritional exposures and incident prostate cancer. Return only the "
    "requested JSON."
)
FIRST_INSTRUCTIONS = """
Judge every context using ONLY the supplied article title, abstract, and MeSH
terms. PASS when the requested nutritional exposure and incident prostate-cancer
risk are plausibly connected. A null result or missing quantitative detail is
not a reason to fail. When uncertain, PASS to the detailed screen.

FAIL only for a clear lexical false match, incidental exposure, wrong cancer,
or treatment/survival/prognosis/recurrence/mechanism without incident-cancer
risk. Potential Mendelian-randomization and genotype-interaction reports must
PASS here so the detailed screen can assign their explicit protocol code.

Return one result per context_id. Each reason must be one complete sentence.
"""

SECOND_SYSTEM = (
    "You are the detailed abstract screening, extraction, and JBI layer for an "
    "incident prostate-cancer systematic review. Return only the requested JSON."
)
SECOND_INSTRUCTIONS = """
Re-read ONLY the supplied title, abstract, and MeSH terms. Include original human
studies of the requested nutritional exposure and incident prostate cancer.
Apply exactly one of these codes to every exclusion:

S2-REVIEW: systematic, scoping, narrative, umbrella, or meta-analysis review.
S2-NONPRIMARY: editorial, commentary, guideline, protocol, non-comparative case
report/series, correction without original results, or retracted publication.
S2-ANIMAL: exclusively animal, cell-line, organoid, xenograft, or preclinical.
S2-EXPOSURE-MISMATCH: requested exposure is absent, incidental, another concept,
or a medical treatment instead of the nutritional exposure.
S2-COMPOUND: target is inseparable from an index, mixture, multi-nutrient
intervention, or combined exposure with no target-specific estimate.
S2-CANCER-MISMATCH: prostate-cancer incidence results are absent or inseparable.
S2-WRONG-OUTCOME: only survival, mortality, recurrence, progression, treatment
response, prognosis, prevalence, or another non-incidence endpoint.
S2-POSTDIAGNOSIS: exposure is measured/administered only after diagnosis/treatment.
S2-BENIGN-ONLY: only benign or nonmalignant disease.
S2-MENDELIAN-RANDOMIZATION: Mendelian randomization or genetic-instrument analysis.
S2-GENOTYPE-INTERACTION: genotype-stratified exposure effect or gene/SNP-exposure
interaction rather than a population-wide exposure effect.
S2-GENETIC-FOCUS: other gene, mutation, SNP, expression, or genetic focus without
a population-wide exposure estimate.
S2-DIAGNOSTIC-MECHANISTIC: diagnostic, tumor-tissue, molecular mechanism, imaging,
or biomarker discrimination without evidence about developing prostate cancer.
S2-NO-TARGET-ESTIMATE: target incidence result cannot be separated.
S2-DUPLICATE: supplied metadata establishes a duplicate/overlapping analysis.

Do not exclude because an effect, CI, p value, sample size, or case count is
missing. For included contexts, extract only explicit evidence. Classify
exposure_subtype as whole_food, dietary_component, human_biomarker, supplement,
or mixed_or_unclear; in particular, do not treat tomato food intake, dietary
lycopene, circulating lycopene, and lycopene supplements as interchangeable.
Provide a field-specific verbatim excerpt (short, never an invented paraphrase), source
(abstract), PubMed URL, and location for every non-null value. Use null when
absent. Report country, study location, population, and ethnicity only when
explicitly stated; use NA when ethnicity or location cannot be found. Set
exposure_classification_ambiguous true whenever the abstract does not establish
the requested exposure class/form unambiguously. Set wald_inputs_same_estimate
true only when the effect, p value, CI, outcome, comparison, model, and stratum
that are available clearly refer to the same estimate; an unreported model or
stratum alone does not make them inconsistent. Report ci_confidence_level only
when stated. Total sample size is required for pooling, but exact case counts
are optional: leave cases null when unavailable and never invent a case count.
Exposure unit/scale, adjustment model/subgroup/stratum, and direct exposure
measurement may be NA and their absence does not prevent pooling. A usable CI
OR an exact two-sided p value for the selected effect is sufficient; both are
not required. Retain p-value inequalities as strings, never as exact values.
Record the comparison direction only when explicit; leave comparison_type null
when unclear. Code will assume high versus low (or yes versus no for binary
exposure) and mark that direction as imputed without inventing source evidence.

Appraise JBI with cohort q1-q11, case_control q1-q10, cross_sectional q1-q8, or
rct q1-q13. Use Yes, No, Unclear, or NA; unreported methods are Unclear. Supply
q1-q13 and mark questions beyond the selected checklist NA.

Full-text escalation is determined in code after deterministic Wald completion.
It occurs only for an included PMCID context when the JBI grade remains Fair
or a required quantitative field remains missing/invalid.
Missing exposure measurement or subtype alone does not require escalation.
Each reason must be one sentence.
"""

FULL_TEXT_SYSTEM = (
    "You are a full-text evidence extractor and JBI appraiser for an incident "
    "prostate-cancer systematic review. Return only the requested JSON."
)
FULL_TEXT_INSTRUCTIONS = """
Use ONLY the supplied PMC full text. Re-evaluate each context, recover missing
sample size/case/effect/CI/p-value information, and reperform JBI. Preserve the
second-round ethnicity and location values; if Sol recorded NA, keep NA rather
than inferring either field in this round. Exclude only when the
full text establishes one of the detailed-screen S2 codes. Mendelian
randomization and genotype-interaction papers use their explicit exclusion codes.

For every value recovered or revised here, provide a short field-specific
original-article excerpt, source full_text, the matching PMC URL, and a
section/table/paragraph location. Return null for unchanged abstract values;
the code merges them from the prior extraction. Prefer the most fully adjusted
target-context estimate; never invent values. Supply q1-q13, with
non-applicable questions marked NA. Each reason must be one sentence. The code
will determine poolability: total sample size, ratio effect, and a usable CI OR
exact two-sided p value for that effect plus Moderate or Good JBI is poolable;
Fair evidence is not. Exact cases, exposure unit/scale, adjustment model,
subgroup/stratum, and direct exposure measurement are optional and may be NA.
Do not invent missing case counts. Retain p inequalities as strings. Leave an
unclear comparison_type null; code marks the default high-versus-low or binary
yes-versus-no direction as imputed. Never fabricate supporting excerpts.
"""


class ScreeningValidationError(ValueError):
    """A model response violated the screening packet contract."""


class ScreeningGatewayError(RuntimeError):
    """A transient model-gateway failure exhausted its immediate retries."""


class ScreeningResponseError(RuntimeError):
    """A model response could not be parsed after its repair attempts."""


class UnresolvedScreeningProblems(RuntimeError):
    """Recoverable work-unit problems remained after the deferred revisit."""


RECOVERABLE_SCREENING_PROBLEMS = (
    ScreeningGatewayError,
    ScreeningResponseError,
    ScreeningValidationError,
)


def _wald_p_value_input(value: Any) -> float | None:
    """Return an exact invertible two-sided p value; reject inequalities."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        p_value = float(value)
    else:
        text = str(value).strip().lower().replace("−", "-")
        if re.search(r"[<>≤≥]", text):
            return None
        match = re.search(r"(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", text)
        if not match:
            return None
        p_value = float(match.group(0))
    return p_value if math.isfinite(p_value) and 0 < p_value <= 1 else None


def wald_ratio_ci_from_effect_p(
    effect_size: Any,
    p_value: Any,
    effect_type: Any,
) -> tuple[float, float] | None:
    """Derive a 95% Wald CI for a ratio effect from a two-sided p value."""
    if str(effect_type or "").strip().upper() not in WALD_RATIO_EFFECT_TYPES:
        return None
    try:
        effect = float(effect_size)
    except (TypeError, ValueError):
        return None
    p_numeric = _wald_p_value_input(p_value)
    if (
        p_numeric is None
        or not math.isfinite(effect)
        or effect <= 0
        or math.isclose(effect, 1.0, rel_tol=0.0, abs_tol=1e-15)
    ):
        return None
    z_score = -NormalDist().inv_cdf(max(p_numeric / 2.0, 5e-324))
    if not math.isfinite(z_score) or z_score <= 0:
        return None
    log_effect = math.log(effect)
    standard_error = abs(log_effect) / z_score
    if not math.isfinite(standard_error) or standard_error <= 0:
        return None
    margin = WALD_95_Z * standard_error
    try:
        interval = math.exp(log_effect - margin), math.exp(log_effect + margin)
    except OverflowError:
        return None
    return interval if all(math.isfinite(value) and value > 0 for value in interval) else None


def wald_p_value_from_ratio_ci(
    effect_size: Any,
    ci_lower: Any,
    ci_upper: Any,
    effect_type: Any,
) -> float | None:
    """Derive a two-sided Wald p value from a coherent ratio estimate and CI."""
    if str(effect_type or "").strip().upper() not in WALD_RATIO_EFFECT_TYPES:
        return None
    try:
        effect = float(effect_size)
        lower = float(ci_lower)
        upper = float(ci_upper)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (effect, lower, upper)):
        return None
    if not lower < upper or not lower <= effect <= upper:
        return None
    standard_error = (math.log(upper) - math.log(lower)) / (2.0 * WALD_95_Z)
    if not math.isfinite(standard_error) or standard_error <= 0:
        return None
    z_score = abs(math.log(effect)) / standard_error
    return math.erfc(z_score / math.sqrt(2.0))


def _p_value_supports_exact_two_sided_target(extraction: dict[str, Any]) -> bool:
    """Reject bounds and tests that do not identify the target coefficient."""
    excerpt = one_line(
        (((extraction.get("evidence") or {}).get("p_value") or {}).get("excerpt"))
    ).casefold()
    reported = one_line(extraction.get("p_value_reported_as")).casefold()
    if not extraction.get("p_value_is_exact"):
        return False
    if re.search(r"[<>≤≥]", f"{reported} {excerpt}"):
        return False
    return not re.search(
        r"\b(?:p\s*(?:for|-)?\s*trend|trend\s+p|interaction|heterogeneity|"
        r"global\s+(?:test|p)|overall\s+(?:test|p)|one[- ]sided|one[- ]tailed)\b",
        excerpt,
    )


def _ci_supports_two_sided_wald(extraction: dict[str, Any]) -> bool:
    """Require an explicitly 95% non-specialized CI for reverse Wald completion."""
    if extraction.get("ci_confidence_level") != 95.0:
        return False
    excerpt = one_line(
        (((extraction.get("evidence") or {}).get("confidence_interval") or {}).get("excerpt"))
    ).casefold()
    if re.search(
        r"\b(?:profile[- ]likelihood|bootstrap(?:ped)?|credible|bayesian|exact)\b",
        excerpt,
    ):
        return False
    return extraction.get("wald_inputs_same_estimate") is True


def add_wald_derivations(extraction):
    if not isinstance(extraction, dict):
        return False, False
    before_ci = extraction.get("ci_lower") is None and extraction.get("ci_upper") is None
    before_p = extraction.get("p_value") is None
    relaxed_policy.complete(extraction, direction=False)
    return before_ci and extraction.get("ci_lower") is not None, before_p and extraction.get("p_value") is not None



def _explicit_comparison_direction(value: Any) -> bool:
    text = one_line(value).casefold().replace("_", " ")
    reference_missing = bool(
        re.search(
            r"\b(?:unspecified|unstated|unreported|unidentified|undefined)\s+"
            r"(?:\w+\s+){0,3}(?:reference|referent|comparator|direction)\b",
            text,
        )
        or re.search(
            r"\b(?:reference|referent|comparator|direction)\b"
            r"[^;,.]{0,40}\b(?:not (?:specified|provided|defined)|unspecified|unstated|undefined)\b",
            text,
        )
        or re.search(
            r"\b(?:vs\.?|versus|compared (?:with|to)|relative to)\s+"
            r"(?:(?:an?|the)\s+)?(?:unspecified|unstated|unreported|unidentified|undefined)\b",
            text,
        )
    )
    if not text or reference_missing or re.search(
        r"\b(?:unclear|unknown|not (?:reported|stated|clear)|cannot determine|indeterminate)\b",
        text,
    ):
        return False
    return bool(
        # Named categories, numeric doses, and inequalities establish the
        # reported order too; they must not be replaced by a default direction.
        re.search(r"\S.*\s(?:vs\.?|versus|compared (?:with|to)|relative to)\s+\S", text)
        or re.search(r"\b(?:high(?:er|est)?|low(?:er|est)?|top|bottom|yes|no|positive|negative)\b", text)
        and re.search(r"\b(?:vs\.?|versus|compared|relative)\b|/", text)
        or re.search(r"\bper\b|\b(?:increase|decrease|increment|doubling)\b", text)
        or re.search(r"\b(?:q|t)[1-9]\s*(?:vs\.?|versus|/)\s*(?:q|t)[1-9]\b", text)
        or re.search(r"\b(?:users?|use|consumers?|exposed|present)\b.*\b(?:non[- ]?(?:users?|use|consumers?)|unexposed|absent|never)\b", text)
        or re.search(r"\b(?:non[- ]?(?:users?|use|consumers?)|unexposed|absent|never)\b.*\b(?:users?|use|consumers?|exposed|present)\b", text)
    )


def apply_poolability_policy(
    extraction: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Idempotently apply the shared policy to new or previously saved evidence.

    Mutates and returns the supplied extraction. Reported evidence and optional
    missing fields are preserved; assumptions are separate provenance fields.
    An explicit false same-estimate flag is never overridden for Wald recovery.
    """
    if not isinstance(extraction, dict):
        return extraction
    add_wald_derivations(extraction)
    extraction["poolability_policy_version"] = POOLABILITY_POLICY_VERSION
    comparison = extraction.get("comparison_type")
    if extraction.get("direction_imputed") is True:
        return extraction
    if extraction.get("astra_fulltext_provenance") and extraction.get("comparison_direction") in {"higher_vs_lower", "lower_vs_higher", "per_unit_increase"}:
        # The validated PMC extraction has an explicit direction even when the
        # optional comparison label is absent. Keep its reciprocal requirement.
        extraction["direction_imputed"] = False
        return extraction
    if _explicit_comparison_direction(comparison):
        extraction["direction_imputed"] = False
        return extraction
    context = " ".join(
        one_line(value)
        for value in (
            comparison,
            ((extraction.get("evidence") or {}).get("comparison") or {}).get("excerpt"),
        )
    ).casefold()
    binary = bool(
        re.search(
            r"\b(?:binary|dichotomous|yes|no|positive|negative|seropositiv\w*|"
            r"seronegativ\w*|users?|non[- ]?users?|use|non[- ]?use|ever|never|"
            r"presence|absence|present|absent|exposed|unexposed)\b",
            context,
        )
    )
    extraction.setdefault("direction_original_comparison", comparison)
    extraction["comparison_type"] = "Yes versus no" if binary else "High versus low"
    extraction["needs_inversion"] = False
    extraction["direction_imputed"] = True
    extraction["direction_imputation_reason"] = (
        "Comparison direction was not explicit; the protocol assumes "
        + ("yes versus no for binary exposure." if binary else "high versus low exposure.")
    )
    return extraction


@dataclass(frozen=True)
class TrackRuntime:
    name: str
    client: Any
    first_model: str
    second_model: str
    full_text_model: str
    api_style: str

    def model_for(self, stage: str) -> str:
        return {
            "first": self.first_model,
            "second": self.second_model,
            "full_text": self.full_text_model,
        }[stage]


def configure_runtime(
    name: str,
    repo_root: Path,
    client_factory: Callable[..., Any] = OpenAI,
) -> TrackRuntime:
    """Create an isolated track client on the shared Cornell LiteLLM gateway."""
    if name not in TRACK_NAMES:
        raise ValueError(f"Unknown screening track: {name}")
    load_dotenv(repo_root / "mykey.env", override=True)
    key = os.getenv("CORNELL_API_KEY") or os.getenv("Cornell_API_Key")
    base_url = os.getenv("CORNELL_API_BASE_URL") or os.getenv("API_Base_URI")
    if not key:
        raise RuntimeError(
            "CORNELL_API_KEY (or Cornell_API_Key in mykey.env) is not configured"
        )
    if not base_url:
        raise RuntimeError(
            "CORNELL_API_BASE_URL (or API_Base_URI in mykey.env) is not configured"
        )
    client = client_factory(api_key=key, base_url=base_url, timeout=180)

    if name == "chatgpt":
        return TrackRuntime(
            name="chatgpt",
            client=client,
            first_model=CHATGPT_FIRST_MODEL,
            second_model=CHATGPT_SECOND_MODEL,
            full_text_model=CHATGPT_FULL_TEXT_MODEL,
            api_style="responses",
        )
    if name == "deepseek":
        return TrackRuntime(
            name="deepseek",
            client=client,
            first_model=os.getenv("DEEPSEEK_FIRST_SCREEN_MODEL", DEEPSEEK_FIRST_MODEL),
            second_model=os.getenv("DEEPSEEK_SECOND_SCREEN_MODEL", DEEPSEEK_SECOND_MODEL),
            full_text_model=os.getenv("DEEPSEEK_FULL_TEXT_MODEL", DEEPSEEK_FULL_TEXT_MODEL),
            api_style="chat_completions",
        )
    raise AssertionError("validated screening track was not configured")


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    temporary.replace(path)


def validation_status_packet(
    candidates: dict[str, Any],
    active_tracks: Iterable[str] = DEFAULT_ACTIVE_TRACK_NAMES,
    *,
    completed: bool = False,
) -> dict[str, Any]:
    """Describe the primary/validation state without fabricating model results."""
    tracks = tuple(dict.fromkeys(str(name) for name in active_tracks))
    unknown = sorted(set(tracks) - set(TRACK_NAMES))
    if unknown or "chatgpt" not in tracks:
        raise ValueError(
            "Active screening tracks must include chatgpt and contain only known tracks"
        )
    deepseek_active = "deepseek" in tracks
    status = "completed" if deepseek_active and completed else (
        "active" if deepseek_active else DEEPSEEK_VALIDATION_STATUS
    )
    return {
        "schema_version": 1,
        "created_at": utc_now(),
        "publication_date_start": candidates.get("publication_date_start"),
        "publication_date_end": candidates.get("publication_date_end"),
        "screening_mode": "dual" if deepseek_active else "chatgpt_only",
        "canonical_track": "chatgpt",
        "active_tracks": list(tracks),
        "validation": {
            "track": "deepseek",
            "role": "validation_only",
            "status": status,
            "reason": "" if deepseek_active else DEEPSEEK_VALIDATION_REASON,
            "results_available": bool(deepseek_active and completed),
            "required_models": {
                "first": DEEPSEEK_FIRST_MODEL,
                "second": DEEPSEEK_SECOND_MODEL,
                "full_text": DEEPSEEK_FULL_TEXT_MODEL,
            },
        },
    }


def write_validation_status(
    output_dir: Path,
    candidates: dict[str, Any],
    active_tracks: Iterable[str] = DEFAULT_ACTIVE_TRACK_NAMES,
    *,
    completed: bool = False,
) -> dict[str, Any]:
    packet = validation_status_packet(
        candidates, active_tracks, completed=completed
    )
    save_json(output_dir / "validation_status.json", packet)
    return packet


def screening_input_digest(stage: str, model: str, payload: Any) -> str:
    """Bind resumable packets to their exact source data and screening contract."""
    contracts = {
        "first": (FIRST_SYSTEM, FIRST_INSTRUCTIONS),
        "second": (SECOND_SYSTEM, SECOND_INSTRUCTIONS),
        "full_text": (FULL_TEXT_SYSTEM, FULL_TEXT_INSTRUCTIONS),
    }
    system, instructions = contracts[stage]
    canonical = {
        "protocol_version": SCREENING_PROTOCOL_VERSION,
        "stage": stage,
        "model": model,
        "system": system,
        "instructions": instructions,
        "response_schema": response_schema(stage),
        "payload": payload,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def first_input_digest(candidates: dict[str, Any], model: str) -> str:
    """Return the canonical first-screen digest for an immutable candidate packet."""
    return screening_input_digest(
        "first",
        model,
        {"candidates": candidates},
    )


def first_context_input_digests(
    candidates: dict[str, Any],
    model: str,
) -> dict[str, str]:
    """Bind each first-round result to its complete semantic context input.

    Transport batching is deliberately excluded: a context may be safely carried
    between candidate generations only when its article record, exact context,
    validation metadata, model, prompt contract, and response schema are unchanged.
    Sibling candidate contexts are excluded so an added/removed Vitamin K3 context
    cannot invalidate an otherwise identical atomic context for the same PMID.
    """
    expected = expected_contexts(candidates)
    articles = _article_lookup(candidates)
    digests: dict[str, str] = {}
    for cid, base in expected.items():
        article = articles.get(base["pmid"], {})
        article_input = {
            key: value
            for key, value in article.items()
            if key != "candidate_contexts"
        }
        validation_context = {
            key: value
            for key, value in base.items()
            if not key.startswith("_")
        }
        digests[cid] = screening_input_digest(
            "first",
            model,
            {
                "article": article_input,
                "context": validation_context,
            },
        )
    return digests


def second_input_digest(
    candidates: dict[str, Any],
    first_packet: dict[str, Any],
    model: str,
) -> str:
    """Bind a second screen to the exact candidate and first-screen packets."""
    return screening_input_digest(
        "second",
        model,
        {
            "candidates": candidates,
            "first_model": first_packet.get("model"),
            "first_input_digest": first_packet.get("input_digest"),
            "first_results": first_packet.get("results", []),
        },
    )


def full_text_source_manifest(
    sources: dict[str, dict[str, Any]],
    relevant_pmcids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Create a durable, content-bound manifest without retaining PMC article text."""
    manifest: dict[str, dict[str, Any]] = {}
    for pmcid in sorted({str(value or "") for value in relevant_pmcids}):
        source = sources.get(pmcid) or {
            "status": "no_pmcid" if not pmcid else "unavailable",
            "text": "",
            "error": "",
        }
        text = str(source.get("text") or "")
        manifest[pmcid] = {
            "status": str(source.get("status") or "unavailable"),
            "characters": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "error": one_line(source.get("error") or "")[:500],
            "prevalidated": bool(source.get("prevalidated")),
            "source_format": one_line(source.get("source_format") or ""),
            "source_route": one_line(source.get("source_route") or ""),
            "raw_sha256": one_line(source.get("raw_sha256") or ""),
            "validation_digest": one_line(
                source.get("validation_digest") or ""
            ),
            "receipt_ledger_sha256": one_line(
                source.get("receipt_ledger_sha256") or ""
            ),
        }
    return manifest


def stable_json_digest(payload: Any) -> str:
    """Hash a durable JSON object using the packet canonicalization contract."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def full_text_input_digest(
    second_packet: dict[str, Any],
    model: str,
    source_manifest: dict[str, dict[str, Any]],
    manual_adjudications: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Bind a full-text screen to its second screen and hashed PMC inputs."""
    payload: dict[str, Any] = {
        "second_model": second_packet.get("model"),
        "second_input_digest": second_packet.get("input_digest"),
        "second_results": second_packet.get("results", []),
        "source_manifest": source_manifest,
    }
    manual_digest = manual_full_text_adjudications_digest(manual_adjudications)
    if manual_digest is not None:
        payload["manual_full_text_adjudications_digest"] = manual_digest
    return screening_input_digest(
        "full_text",
        model,
        payload,
    )


def manual_full_text_adjudications_digest(
    adjudications: dict[str, dict[str, Any]] | None,
) -> str | None:
    """Return a stable digest for the operational manual-adjudication content."""
    if not adjudications:
        return None
    records = [
        {
            "context_id": context_id,
            "full_text_status": adjudications[context_id].get("full_text_status"),
            "exclusion_code": adjudications[context_id].get("exclusion_code"),
            "reason": adjudications[context_id].get("reason"),
            "recorded_at": adjudications[context_id].get("recorded_at"),
        }
        for context_id in sorted(adjudications)
    ]
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def empty_usage(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "calls": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "by_exposure": {},
    }


_USAGE_LOCK = threading.Lock()


def _usage_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _allocated_tokens(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Allocate integer tokens by context count while preserving the exact total."""
    positive = {
        str(name): int(weight)
        for name, weight in weights.items()
        if str(name) and int(weight) > 0
    }
    denominator = sum(positive.values())
    if total <= 0 or denominator <= 0:
        return {name: 0 for name in positive}
    allocated = {
        name: total * weight // denominator for name, weight in positive.items()
    }
    remainder = total - sum(allocated.values())
    order = sorted(
        positive,
        key=lambda name: (-(total * positive[name] % denominator), name.casefold()),
    )
    for name in order[:remainder]:
        allocated[name] += 1
    return allocated


def _add_usage(
    usage: dict[str, Any],
    response: Any,
    exposure_weights: dict[str, int] | None = None,
) -> None:
    raw = getattr(response, "usage", None)
    input_tokens = int(
        _usage_value(raw, "input_tokens")
        or _usage_value(raw, "prompt_tokens")
        or 0
    )
    output_tokens = int(
        _usage_value(raw, "output_tokens")
        or _usage_value(raw, "completion_tokens")
        or 0
    )
    input_details = (
        _usage_value(raw, "input_tokens_details")
        or _usage_value(raw, "prompt_tokens_details")
    )
    cached_input = int(
        _usage_value(input_details, "cached_tokens")
        or _usage_value(raw, "cache_read_input_tokens")
        or _usage_value(raw, "prompt_cache_hit_tokens")
        or 0
    )
    cached_input = min(max(0, cached_input), max(0, input_tokens))
    output_details = (
        _usage_value(raw, "output_tokens_details")
        or _usage_value(raw, "completion_tokens_details")
    )
    reasoning = int(_usage_value(output_details, "reasoning_tokens", 0) or 0)
    weights = exposure_weights or {}
    cached_allocated = _allocated_tokens(cached_input, weights)
    uncached_allocated = _allocated_tokens(input_tokens - cached_input, weights)
    output_allocated = _allocated_tokens(output_tokens, weights)
    reasoning_allocated = _allocated_tokens(reasoning, weights)
    with _USAGE_LOCK:
        usage["calls"] = int(usage.get("calls") or 0) + 1
        usage["input_tokens"] = int(usage.get("input_tokens") or 0) + input_tokens
        usage["cached_input_tokens"] = (
            int(usage.get("cached_input_tokens") or 0) + cached_input
        )
        usage["output_tokens"] = int(usage.get("output_tokens") or 0) + output_tokens
        usage["reasoning_tokens"] = int(usage.get("reasoning_tokens") or 0) + reasoning
        by_exposure = usage.setdefault("by_exposure", {})
        for exposure in sorted(weights, key=str.casefold):
            entry = by_exposure.setdefault(
                exposure,
                {
                    "calls": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                },
            )
            entry["calls"] = int(entry.get("calls") or 0) + 1
            entry["input_tokens"] = int(entry.get("input_tokens") or 0) + (
                cached_allocated.get(exposure, 0)
                + uncached_allocated.get(exposure, 0)
            )
            entry["cached_input_tokens"] = int(
                entry.get("cached_input_tokens") or 0
            ) + cached_allocated.get(exposure, 0)
            entry["output_tokens"] = int(entry.get("output_tokens") or 0) + (
                output_allocated.get(exposure, 0)
            )
            entry["reasoning_tokens"] = int(
                entry.get("reasoning_tokens") or 0
            ) + reasoning_allocated.get(exposure, 0)


def json_from_text(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ScreeningValidationError("Model response must be a JSON object")
    return payload


def _nullable(*types: str) -> dict[str, Any]:
    return {"type": list(types) + ["null"]}


def _evidence_schema() -> dict[str, Any]:
    entry = {
        "type": ["object", "null"],
        "properties": {
            "excerpt": {"type": "string"},
            "source": {"type": "string", "enum": ["abstract", "full_text"]},
            "url": {"type": "string"},
            "location": {"type": "string"},
        },
        "required": ["excerpt", "source", "url", "location"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {field: entry for field in EVIDENCE_FIELDS},
        "required": list(EVIDENCE_FIELDS),
        "additionalProperties": False,
    }


def _extraction_schema() -> dict[str, Any]:
    properties = {
        "effect_size": _nullable("number"),
        "effect_type": _nullable("string"),
        "ci_lower": _nullable("number"),
        "ci_upper": _nullable("number"),
        "p_value": _nullable("number", "string"),
        "ci_confidence_level": _nullable("number"),
        "total_n": _nullable("number"),
        "cases": _nullable("number"),
        "comparison_type": _nullable("string"),
        "design": _nullable("string"),
        "timing": _nullable("string"),
        "continent": _nullable("string"),
        "country": _nullable("string"),
        "location": _nullable("string"),
        "ethnicity": _nullable("string"),
        "population": _nullable("string"),
        "exposure_subtype": {
            "type": "string",
            "enum": [
                "whole_food",
                "dietary_component",
                "human_biomarker",
                "supplement",
                "mixed_or_unclear",
            ],
        },
        "exposure_measurement_type": {
            "type": ["string", "null"],
            "enum": ["dietary_intake", "human_biospecimen", "unclear", "NA", None],
        },
        "exposure_classification_ambiguous": {"type": "boolean"},
        "wald_inputs_same_estimate": {"type": "boolean"},
        "needs_inversion": {"type": "boolean"},
        "evidence": _evidence_schema(),
    }
    return {
        "type": ["object", "null"],
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _jbi_schema() -> dict[str, Any]:
    answers = {f"q{index}": {"type": "string", "enum": ["Yes", "No", "Unclear", "NA"]} for index in range(1, 14)}
    return {
        "type": ["object", "null"],
        "properties": {
            "checklist_type": {
                "type": "string",
                "enum": list(JBI_TOTALS),
            },
            "answers": {
                "type": "object",
                "properties": answers,
                "required": list(answers),
                "additionalProperties": False,
            },
        },
        "required": ["checklist_type", "answers"],
        "additionalProperties": False,
    }


def response_schema(stage: str) -> dict[str, Any]:
    if stage == "first":
        item_properties = {
            "context_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["PASS", "FAIL"]},
            "reason": {"type": "string"},
        }
    elif stage == "second":
        item_properties = {
            "context_id": {"type": "string"},
            "eligibility": {"type": "string", "enum": ["INCLUDE", "EXCLUDE"]},
            "exclusion_code": {"type": ["string", "null"]},
            "reason": {"type": "string"},
            "extraction": _extraction_schema(),
            "jbi": _jbi_schema(),
        }
    elif stage == "full_text":
        item_properties = {
            "context_id": {"type": "string"},
            "status": {"type": "string", "enum": ["INCLUDE", "EXCLUDE"]},
            "exclusion_code": {"type": ["string", "null"]},
            "reason": {"type": "string"},
            "extraction": _extraction_schema(),
            "jbi": _jbi_schema(),
        }
    else:
        raise ValueError(stage)
    schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": item_properties,
                    "required": list(item_properties),
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "name": f"monthly_{stage}_screen",
        "strict": True,
        "schema": schema,
    }


def call_json(
    runtime: TrackRuntime,
    stage: str,
    system: str,
    prompt: str,
    usage: dict[str, Any],
    *,
    exposure_weights: dict[str, int] | None = None,
    on_usage: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Call Responses for ChatGPT and an isolated compatible client for DeepSeek."""
    model = runtime.model_for(stage)
    schema = response_schema(stage)
    contracted_prompt = (
        prompt
        + "\n\nOUTPUT CONTRACT:\nReturn exactly one JSON object matching this JSON Schema. "
        "Include every required property, use null for unavailable values, and do not add prose.\n"
        + json.dumps(schema["schema"], ensure_ascii=False, separators=(",", ":"))
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            if runtime.api_style == "responses":
                response = runtime.client.responses.create(
                    model=model,
                    instructions=system,
                    input=contracted_prompt,
                    text={"format": schema},
                    store=False,
                )
                content = response.output_text
            elif runtime.api_style == "chat_completions":
                response = runtime.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": contracted_prompt},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=8192 if stage == "first" else 32768,
                )
                content = response.choices[0].message.content
            else:
                raise RuntimeError(f"Unsupported API style: {runtime.api_style}")
            _add_usage(usage, response, exposure_weights)
            if on_usage is not None:
                try:
                    on_usage()
                except OSError as exc:  # never repeat a billed call for a local I/O error
                    print(f"WARNING: usage checkpoint could not be written: {exc}", flush=True)
            return json_from_text(content)
        except Exception as exc:  # pragma: no cover - retry timing is integration-only
            last_error = exc
            if _is_transient_gateway_error(exc):
                raise ScreeningGatewayError(
                    f"{runtime.name} {stage} gateway call failed: {exc}"
                ) from exc
            if attempt < 2:
                time.sleep(2**attempt)
    message = f"{runtime.name} {stage} call failed after retries: {last_error}"
    if _is_transient_gateway_error(last_error):
        raise ScreeningGatewayError(message) from last_error
    if isinstance(last_error, (json.JSONDecodeError, ScreeningValidationError)):
        raise ScreeningResponseError(message) from last_error
    raise RuntimeError(message) from last_error


def _is_transient_gateway_error(error: Exception | None) -> bool:
    """Distinguish retryable transport/gateway failures from bad model output."""
    if error is None:
        return False
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    module = type(error).__module__.casefold()
    name = type(error).__name__.casefold()
    message = str(error).casefold()
    if module.startswith(("httpx", "httpcore")):
        return True
    if any(
        token in name
        for token in (
            "apiconnection",
            "apitimeout",
            "ratelimit",
            "internalserver",
            "serviceunavailable",
        )
    ):
        return True
    status_code = getattr(error, "status_code", None)
    if status_code == 429 or isinstance(status_code, int) and status_code >= 500:
        return True
    return any(
        token in message
        for token in (
            "connection error",
            "connection reset",
            "connection refused",
            "timed out",
            "timeout",
            "rate limit",
            "too many requests",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "temporarily unavailable",
        )
    )


def context_id(pmid: str, exposure: str, cancer: str) -> str:
    return f"{pmid}|{exposure}|{cancer}"


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def one_sentence(value: Any) -> str:
    result = one_line(value)
    if not result:
        raise ScreeningValidationError("A non-empty one-sentence reason is required")
    # ``St. John's wort`` is one exposure name, not a sentence boundary. Keep
    # the exception deliberately narrow so genuinely separate sentences still
    # fail the strict one-sentence contract.
    sentence_scan = re.sub(
        r"\bSt\.(?=\s+John(?:['’]s)?\b)",
        "St",
        result,
        flags=re.IGNORECASE,
    )
    # Comparative labels such as "Q5 vs. Q1" occur routinely in evidence
    # rationales; ``vs.`` is an abbreviation, not a sentence boundary.
    sentence_scan = re.sub(
        r"\bvs\.(?=\s+[A-Z0-9])",
        "vs",
        sentence_scan,
        flags=re.IGNORECASE,
    )
    if re.search(r"[.!?]\s+[A-Z]", sentence_scan):
        raise ScreeningValidationError(f"Reason contains multiple sentences: {result!r}")
    if result[-1] not in ".!?":
        result += "."
    return result


def protocol_exclusion(article: dict[str, Any]) -> tuple[str, str] | None:
    text = " ".join(
        [
            str(article.get("title") or ""),
            str(article.get("abstract") or ""),
            " ".join(str(value) for value in article.get("mesh", [])),
        ]
    ).casefold()
    text = text.translate(str.maketrans({"–": "-", "—": "-", "−": "-", "×": "x"}))
    if re.search(
        r"\bmendelian randomi[sz]ation\b|"
        r"\b(?:two[- ]sample|bidirectional|multivariable)\s+mr\b|"
        r"\bmr\s+(?:analysis|study|design|approach)\b|"
        r"\bgenetically\s+(?:predicted|proxied|instrumented)\b|"
        r"\bgenetic(?:ally derived)?\s+instrument(?:al variable)?s?\b|"
        r"\b(?:genetic variants?|snps?)\b.{0,80}\binstrumental variables?\b|"
        r"\binstrumental[- ]variable(?:s| analysis)?\b.{0,80}\b(?:genetic variants?|snps?)\b",
        text,
    ):
        return (
            "S2-MENDELIAN-RANDOMIZATION",
            "The report uses Mendelian randomization or genetic instruments, which this protocol excludes.",
        )
    if re.search(
        r"\b(?:gene|genotype|snp|polymorphism|rs\d+)\s*[-x ]\s*"
        r"(?:diet|nutrient|exposure|environment)\s+interaction\b|"
        r"\b(?:diet|nutrient|exposure|environment)\s*[-x ]\s*"
        r"(?:gene|genotype|snp|polymorphism|rs\d+)\s+interaction\b|"
        r"\bgenotype[- ]stratified\b|"
        r"\bstratif(?:ied|ication)\b.{0,100}\b(?:gene|genotype|snp|polymorphism|rs\d+)\b|"
        r"\beffect\s+modification\s+by\s+(?:gene|genotype|snp|polymorphism|rs\d+)\b|"
        r"\b(?:association|effect)\b.{0,60}\b(?:differed|varied|modified)\b.{0,40}\b(?:gene|genotype|snp|polymorphism|rs\d+)\b|"
        r"\b(?:gene|genotype|snp|polymorphism|rs\d+)\b.{0,40}\bmodif(?:y|ies|ied|ication)\b.{0,60}\bassociation\b|"
        r"\b(?:gene|genotype|snp|polymorphism)\s+by\s+(?:diet|nutrient|exposure|environment)\s+interaction\b|"
        r"\binteraction\b.{0,100}\b(?:gene|genotype|snp|polymorphism|rs\d+)\b|"
        r"\bg\s*[*x]\s*e\b",
        text,
    ):
        return (
            "S2-GENOTYPE-INTERACTION",
            "The report centers on a genotype-stratified or gene-exposure interaction, which this protocol excludes.",
        )
    return None


_protocol_exclusion = protocol_exclusion


def expected_contexts(candidates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}
    for article in candidates.get("articles", []):
        pmid = str(article.get("pmid") or "")
        protocol_exclusion_value = protocol_exclusion(article)
        for context in article.get("candidate_contexts", []):
            cid = context_id(pmid, context["exposure"], context["cancer"])
            expected[cid] = {
                "context_id": cid,
                "pmid": pmid,
                "pmcid": article.get("pmcid") or "",
                "published_date": article.get("published_date") or "Unknown",
                "title": article.get("title") or "",
                "authors": article.get("authors") or [],
                "journal": article.get("journal") or "",
                "pubmed_url": article.get("pubmed_url")
                or (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""),
                "exposure": context["exposure"],
                "cancer": context["cancer"],
                "outcome": "Incidence",
                "protocol_exclusion": protocol_exclusion_value,
                "_abstract_text": " ".join(
                    [str(article.get("title") or ""), str(article.get("abstract") or "")]
                ),
            }
    return expected


def _exact_results(payload: dict[str, Any], expected: Iterable[str]) -> dict[str, dict[str, Any]]:
    expected_ids = set(expected)
    results = payload.get("results")
    if not isinstance(results, list):
        raise ScreeningValidationError("results must be an array")
    returned: dict[str, dict[str, Any]] = {}
    for raw in results:
        if not isinstance(raw, dict):
            raise ScreeningValidationError("Every result must be an object")
        cid = str(raw.get("context_id") or "")
        if cid not in expected_ids:
            raise ScreeningValidationError(f"Unexpected context_id: {cid!r}")
        if cid in returned:
            raise ScreeningValidationError(f"Duplicate context_id: {cid}")
        returned[cid] = raw
    if set(returned) != expected_ids:
        missing = sorted(expected_ids - set(returned))
        raise ScreeningValidationError(f"Model omitted contexts: {missing}")
    return returned


def _strict_jbi(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScreeningValidationError("Included records require a JBI object")
    checklist = str(raw.get("checklist_type") or "").lower()
    if checklist not in JBI_TOTALS:
        raise ScreeningValidationError(f"Invalid JBI checklist_type: {checklist!r}")
    supplied = raw.get("answers")
    if not isinstance(supplied, dict):
        raise ScreeningValidationError("JBI answers must be an object")
    answers: dict[str, str] = {}
    allowed = {"Yes", "No", "Unclear", "NA"}
    for index in range(1, 14):
        key = f"q{index}"
        answer = supplied.get(key)
        if answer not in allowed:
            raise ScreeningValidationError(f"Missing or invalid JBI answer {key}: {answer!r}")
        if index > JBI_TOTALS[checklist] and answer != "NA":
            raise ScreeningValidationError(
                f"JBI {key} is beyond the {checklist} checklist and must be NA"
            )
        answers[key] = answer
    total = JBI_TOTALS[checklist]
    applicable = [answers[f"q{index}"] for index in range(1, total + 1)]
    # NA/Unclear/No receive no credit. Removing NA from the denominator allowed
    # a response with one Yes and ten NA answers to be mislabeled Good and skip
    # the required Fair full-text escalation.
    score = round(100 * sum(answer == "Yes" for answer in applicable) / total, 1)
    grade = "Good" if score > 80 else "Moderate" if score >= 51 else "Fair"
    return {
        "checklist_type": checklist,
        "answers": answers,
        "score_percent": score,
        "grade": grade,
    }


def _number_or_none(value: Any, field: str) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ScreeningValidationError(f"Boolean is not a valid {field} value")
    cleaned = str(value).strip().replace(",", "").replace("−", "-")
    if field == "p_value" and cleaned.casefold() in {
        "ns",
        "not significant",
        "not statistically significant",
        "non-significant",
        "nonsignificant",
    }:
        return None
    if cleaned.casefold() in {
        "na",
        "n/a",
        "nr",
        "not reported",
        "not available",
        "unknown",
        "unclear",
        "none",
        "null",
    }:
        return None
    if field == "p_value":
        cleaned = re.sub(r"^[<>]=?\s*", "", cleaned)
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise ScreeningValidationError(f"{field} must be numeric or null, not {value!r}") from exc
    if not math.isfinite(number):
        raise ScreeningValidationError(f"{field} must be finite")
    if field in {"total_n", "cases"}:
        if number <= 0 or not number.is_integer():
            raise ScreeningValidationError(f"{field} must be a positive whole number")
        return int(number)
    if field == "p_value":
        if not 0 < number <= 1:
            raise ScreeningValidationError(
                "p_value must be greater than zero and at most one"
            )
    elif field in {"effect_size", "ci_lower", "ci_upper"} and number <= 0:
        # A ratio and its confidence limits cannot be non-positive.  Treat a
        # model's zero placeholder as missing so the context can be retained
        # for audit/full-text follow-up instead of failing the whole screen.
        return None
    elif number <= 0:
        raise ScreeningValidationError(f"{field} must be greater than zero")
    return int(number) if number.is_integer() else number


def _normalise_evidence(
    raw: Any,
    *,
    required_source: str | None = None,
    source_text: str | None = None,
    source_identifier: str | None = None,
) -> dict[str, dict[str, str] | None]:
    if not isinstance(raw, dict):
        raise ScreeningValidationError("Extraction evidence must be an object")
    evidence: dict[str, dict[str, str] | None] = {}
    for field in EVIDENCE_FIELDS:
        value = raw.get(field)
        if value is None:
            evidence[field] = None
            continue
        if not isinstance(value, dict):
            raise ScreeningValidationError(f"Evidence for {field} must be an object or null")
        entry = {key: one_line(value.get(key)) for key in ("excerpt", "source", "url", "location")}
        if not all(entry[key] for key in ("source", "url", "location")):
            raise ScreeningValidationError(f"Evidence for {field} is missing source/url/location")
        if entry["source"] not in {"abstract", "full_text"}:
            raise ScreeningValidationError(f"Invalid evidence source for {field}: {entry['source']}")
        if required_source and entry["source"] != required_source:
            raise ScreeningValidationError(
                f"Evidence for {field} must cite {required_source}, not {entry['source']}"
            )
        if not entry["url"].startswith("https://"):
            raise ScreeningValidationError(f"Evidence URL for {field} must be HTTPS")
        parsed = urlparse(entry["url"])
        required_host = (
            "pubmed.ncbi.nlm.nih.gov"
            if entry["source"] == "abstract"
            else "pmc.ncbi.nlm.nih.gov"
        )
        if parsed.hostname != required_host:
            raise ScreeningValidationError(
                f"Evidence URL for {field} must use {required_host}"
            )
        path_segments = {
            segment.casefold() for segment in parsed.path.split("/") if segment
        }
        if source_identifier and source_identifier.casefold() not in path_segments:
            raise ScreeningValidationError(
                f"Evidence URL for {field} does not identify {source_identifier}"
            )
        if (
            source_text is not None
            and entry["excerpt"]
            and one_line(entry["excerpt"]).casefold()
            not in one_line(source_text).casefold()
        ):
            # Extracted values drive the analysis.  Excerpts are an audit aid;
            # when a supplied quotation cannot be verified, preserve the
            # source metadata but leave the excerpt blank.
            entry["excerpt"] = ""
        evidence[field] = entry
    return evidence


NUMERIC_TOKEN_PATTERN = re.compile(
    r"(?:(?<![A-Za-z0-9.\-])[-+]|(?<![A-Za-z0-9.]))(?:"
    r"\d{1,3}(?:[, \u00a0\u2009\u202f]\d{3})+|"
    r"\d+(?:\.\d*)?|\.\d+"
    r")(?:[eE][-+]?\d+)?"
)


def _numeric_value(token: str) -> float:
    compact = re.sub(r"[, \u00a0\u2009\u202f]", "", token)
    return float(compact)


def _numbers_in_text(value: Any) -> list[float]:
    numbers = []
    text = str(value or "")
    normalized = re.sub(r"(?<=\d)[·∙](?=\d)", ".", text)
    normalized = re.sub(r"(?<=\d)\.\s+(?=\d{1,2}\b)", ".", normalized)
    for match in NUMERIC_TOKEN_PATTERN.finditer(normalized):
        try:
            number = _numeric_value(match.group(0))
        except ValueError:
            continue
        if match.group(0).startswith("-"):
            previous = normalized[: match.start()].rstrip()
            if previous and (previous[-1].isdigit() or previous[-1] == "."):
                number = abs(number)
        numbers.append(number)
    return numbers


WORD_NUMBER_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
WORD_NUMBER_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
WORD_NUMBER_PATTERN = re.compile(
    r"\b(?:"
    + "|".join((*WORD_NUMBER_ONES, *WORD_NUMBER_TENS))
    + r")(?:[- ](?:"
    + "|".join(key for key in WORD_NUMBER_ONES if WORD_NUMBER_ONES[key] < 10)
    + r"))?\b",
    flags=re.IGNORECASE,
)


def _word_numbers_in_text(value: Any) -> list[float]:
    numbers = []
    for match in WORD_NUMBER_PATTERN.finditer(str(value or "")):
        parts = re.split(r"[- ]", match.group(0).casefold())
        number = WORD_NUMBER_ONES.get(parts[0], WORD_NUMBER_TENS.get(parts[0], 0))
        if len(parts) == 2:
            number += WORD_NUMBER_ONES[parts[1]]
        numbers.append(float(number))
    return numbers


def _sample_numbers_in_text(value: Any) -> list[float]:
    return _numbers_in_text(value) + _word_numbers_in_text(value)


def _source_windows(source_text: str) -> list[str]:
    """Return one- and two-sentence verbatim windows from supplied article text."""
    text = str(source_text or "")
    if not text:
        return []
    spans = []
    start = 0
    for boundary in re.finditer(r"[.!?](?:\s+|$)|\n+", text):
        end = boundary.end()
        if one_line(text[start:end]):
            spans.append((start, end))
        start = end
    if one_line(text[start:]):
        spans.append((start, len(text)))
    windows = [one_line(text[start:end]) for start, end in spans]
    windows.extend(
        one_line(text[spans[index][0] : spans[index + 1][1]])
        for index in range(len(spans) - 1)
    )
    return windows


def _field_relevance_score(field: str, excerpt: str) -> int:
    text = excerpt.casefold()
    cue_patterns = {
        "effect_size": (
            r"\bodds ratio\b|\bor\b",
            r"\brisk ratio\b|\brelative risk\b|\brr\b",
            r"\bhazard ratio\b|\bhr\b",
            r"\brisk\b|\bassociation\b",
        ),
        "ci_lower": (r"confidence interval|\bci\b", r"95\s*%"),
        "ci_upper": (r"confidence interval|\bci\b", r"95\s*%"),
        "total_n": (
            r"\bparticipants?\b|\bsubjects?\b|\bpatients?\b|\bmen\b|\bwomen\b",
            r"\bcases?\b|\bcontrols?\b|\bcohort\b|\bsample\b",
            r"\bincluded\b|\benrolled\b|\brecruited\b|\bfollowed\b",
        ),
        "cases": (r"\bcases?\b|\bcancers?\b|\bevents?\b", r"diagnosed|developed|incident"),
        "p_value": (r"\bp(?:\s*value)?\b|p\s*\(?trend\)?",),
    }
    return sum(
        4 if index == 0 else 2
        for index, pattern in enumerate(cue_patterns.get(field, ()))
        if re.search(pattern, text, flags=re.IGNORECASE)
    )


def _reported_sample_components(excerpt: str, total_n: float) -> tuple[float, ...]:
    """Find reported group counts that exactly sum to an extracted total sample."""
    if not float(total_n).is_integer() or total_n <= 0:
        return ()
    labels = re.findall(
        r"\b(?:cases?|controls?|participants?|subjects?|patients?|men|women|groups?|arms?)\b",
        excerpt,
        flags=re.IGNORECASE,
    )
    if len(labels) < 2:
        return ()
    integers = [
        number
        for number in _sample_numbers_in_text(excerpt)
        if number > 0 and float(number).is_integer() and number < total_n
    ]
    half = total_n / 2
    if any(math.isclose(number, half, rel_tol=0, abs_tol=1e-9) for number in integers):
        if re.search(r"\bpairs?\b|\bequal number\b", excerpt, flags=re.IGNORECASE):
            return (half, half)
    ratio = re.search(
        r"\b(\d+)\s*:\s*(\d+)\s+matched\s+case-control\b",
        excerpt,
        flags=re.IGNORECASE,
    )
    if ratio:
        case_units, control_units = map(int, ratio.groups())
        denominator = case_units + control_units
        cases = total_n * case_units / denominator
        controls = total_n * control_units / denominator
        if (
            cases in integers
            and cases.is_integer()
            and controls.is_integer()
        ):
            return (cases, controls)
    for count in (2, 3):
        for values in combinations(integers, count):
            if math.isclose(sum(values), total_n, rel_tol=0, abs_tol=1e-9):
                return tuple(values)
    return ()


def _reported_case_components(excerpt: str, cases: float) -> tuple[float, ...]:
    """Find arm-specific event counts that exactly sum to extracted cases."""
    if not float(cases).is_integer() or cases <= 0:
        return ()
    if not re.search(r"\bcases?\b|\bcancers?\b", excerpt, flags=re.IGNORECASE):
        return ()
    integers = [
        number
        for number in _sample_numbers_in_text(excerpt)
        if number > 0 and float(number).is_integer() and number < cases
    ]
    for count in (2, 3):
        for values in combinations(integers, count):
            if math.isclose(sum(values), cases, rel_tol=0, abs_tol=1e-9):
                return tuple(values)
    return ()


def _source_excerpt_for_numeric_field(
    field: str,
    value: Any,
    source_text: str,
    extraction: dict[str, Any] | None = None,
) -> tuple[str, tuple[float, ...]]:
    """Find the most relevant source window for a numeric extraction."""
    target = float(value)
    extraction = extraction or {}
    companions = {
        "effect_size": (extraction.get("ci_lower"), extraction.get("ci_upper")),
        "ci_lower": (extraction.get("effect_size"), extraction.get("ci_upper")),
        "ci_upper": (extraction.get("effect_size"), extraction.get("ci_lower")),
        "total_n": (extraction.get("cases"),),
    }.get(field, ())
    direct = []
    derived = []
    percent_derived = []
    for excerpt in _source_windows(source_text):
        numbers = (
            _sample_numbers_in_text(excerpt)
            if field in {"total_n", "cases"}
            else _numbers_in_text(excerpt)
        )
        score = _field_relevance_score(field, excerpt)
        score += 2 * sum(
            companion is not None
            and any(
                math.isclose(float(companion), number, rel_tol=1e-9, abs_tol=1e-12)
                for number in numbers
            )
            for companion in companions
        )
        if any(
            math.isclose(target, number, rel_tol=1e-9, abs_tol=1e-12)
            for number in numbers
        ):
            direct.append((score, -len(excerpt), excerpt, ()))
        elif field in {"total_n", "cases"}:
            components = (
                _reported_sample_components(excerpt, target)
                if field == "total_n"
                else _reported_case_components(excerpt, target)
            )
            if components:
                if extraction.get("cases") in components:
                    score += 4
                derived.append((score, -len(excerpt), excerpt, components))
        elif field == "effect_size":
            for percent_match in re.finditer(r"(\d+(?:\.\d+)?)\s*%", excerpt):
                percent = float(percent_match.group(1))
                directions = list(
                    re.finditer(
                        r"\b(?:higher|lower|increas\w*|decreas\w*|reduc\w*|more likely|less likely)\b",
                        excerpt,
                        flags=re.IGNORECASE,
                    )
                )
                if not directions:
                    continue
                direction = min(
                    directions,
                    key=lambda match: abs(match.start() - percent_match.start()),
                ).group(0).casefold()
                increase = (
                    direction == "higher"
                    or direction == "more likely"
                    or direction.startswith("increas")
                )
                implied = 1 + percent / 100 if increase else 1 - percent / 100
                if math.isclose(target, implied, rel_tol=1e-9, abs_tol=1e-12):
                    percent_derived.append((score, -len(excerpt), excerpt, ()))
    candidates = direct or percent_derived or derived
    if not candidates:
        return "", ()
    _score, _negative_length, excerpt, components = max(candidates)
    return excerpt, components


def _percentage_statement_to_ratio(
    value: Any,
    raw_effect_type: Any,
    source_text: str | None,
) -> Any:
    """Convert a raw percentage change to its ratio when the article supports it.

    Models sometimes return ``91`` for a reported 91% risk increase and
    sometimes return the already-derived ``1.91``.  Only convert the former,
    and use the stated direction in the source sentence so decreases become
    ratios below one.
    """
    if value is None:
        return None
    label = one_line(raw_effect_type)
    if not ("%" in label or re.search(r"\b(?:percent|pct)\b", label, re.IGNORECASE)):
        return value
    target = float(value)
    for excerpt in _source_windows(source_text or ""):
        directions = list(
            re.finditer(
                r"\b(?:higher|lower|increas\w*|decreas\w*|reduc\w*|more likely|less likely)\b",
                excerpt,
                flags=re.IGNORECASE,
            )
        )
        if not directions:
            continue
        for percent_match in re.finditer(r"(\d+(?:\.\d+)?)\s*%", excerpt):
            percent = float(percent_match.group(1))
            if not math.isclose(target, percent, rel_tol=1e-9, abs_tol=1e-12):
                continue
            direction = min(
                directions,
                key=lambda match: abs(match.start() - percent_match.start()),
            ).group(0).casefold()
            increase = (
                direction == "higher"
                or direction == "more likely"
                or direction.startswith("increas")
            )
            return 1 + percent / 100 if increase else 1 - percent / 100
    return value


def _format_numeric_component(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _require_numeric_support(
    field: str,
    value: Any,
    evidence: dict[str, Any] | None,
    *,
    source_text: str | None = None,
    extraction: dict[str, Any] | None = None,
) -> None:
    if value is None:
        return
    if source_text and field in {"effect_size", "ci_lower", "ci_upper", "total_n", "cases"}:
        repaired_excerpt, components = _source_excerpt_for_numeric_field(
            field,
            value,
            source_text,
            extraction,
        )
        if repaired_excerpt and evidence is not None:
            evidence["excerpt"] = repaired_excerpt
            if components:
                equation = " + ".join(_format_numeric_component(item) for item in components)
                evidence["location"] = one_line(
                    f"{evidence.get('location')}; {field} derived from reported group counts "
                    f"{equation} = {_format_numeric_component(float(value))}"
                )
            return
    excerpt_numbers = _numbers_in_text((evidence or {}).get("excerpt"))
    if any(math.isclose(float(value), number, rel_tol=1e-9, abs_tol=1e-12) for number in excerpt_numbers):
        return
    repaired_excerpt, _components = _source_excerpt_for_numeric_field(
        field,
        value,
        source_text or "",
        extraction,
    )
    if repaired_excerpt and evidence is not None:
        evidence["excerpt"] = repaired_excerpt
        return
    if evidence is not None:
        evidence["excerpt"] = ""
    # The extracted value is the analysis input; an excerpt is optional audit
    # assistance and must not block an otherwise usable record.
    return


def _is_absolute_mean_difference(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    effect_type = one_line(raw.get("effect_type")).casefold()
    return bool(re.search(r"\b(?:absolute\s+)?mean\s+difference\b", effect_type))


def infer_ethnicity(extraction: dict[str, Any]) -> tuple[str, str, str]:
    """Return only explicit field-specific ethnicity evidence, never imputation."""
    value = one_line(extraction.get("ethnicity"))
    evidence = (extraction.get("evidence") or {}).get("ethnicity") or {}
    support = one_line(evidence.get("excerpt"))
    if (
        not value
        or value.casefold() in {"na", "n/a", "unknown", "not reported", "none"}
        or not support
        or value.casefold() not in support.casefold()
    ):
        return (
            "NA",
            "NA: Sol found no explicit field-specific ethnicity statement",
            "NA",
        )
    return value, "Explicit Sol field-specific ethnicity evidence", support


def _normalise_extraction(
    raw: Any,
    *,
    required_source: str | None = None,
    source_text: str | None = None,
    source_identifier: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScreeningValidationError("Included records require an extraction object")
    # Signed log-ratio bounds must be converted before positive-ratio parsing.
    # Keep the complete original evidence and raw p syntax available to the policy.
    if isinstance(raw, dict) and any(str(raw.get(k) or "").lower() in {"log", "ln", "log_ratio", "log scale", "log-ratio"} for k in ("ci_scale", "confidence_interval_scale", "effect_scale")):
        raw = dict(raw)
        relaxed_policy.complete(raw, direction=False)
    extraction: dict[str, Any] = {}
    numeric = {
        "effect_size", "ci_lower", "ci_upper", "p_value",
        "ci_confidence_level", "total_n", "cases",
    }
    raw_p_value = raw.get("p_value")
    for field in EXTRACTION_FIELDS:
        if field not in raw:
            raise ScreeningValidationError(f"Extraction is missing required field: {field}")
        if field in numeric:
            extraction[field] = _number_or_none(raw.get(field), field)
        elif field in {
            "needs_inversion",
            "exposure_classification_ambiguous",
            "wald_inputs_same_estimate",
        }:
            if not isinstance(raw.get(field), bool):
                raise ScreeningValidationError(f"{field} must be boolean")
            extraction[field] = raw[field]
        else:
            text = one_line(raw.get(field))
            missing_values = {
                "",
                "na",
                "n/a",
                "nr",
                "not reported",
                "not available",
                "unknown",
                "none",
                "null",
            }
            if field == "comparison_type":
                missing_values.update(
                    {
                        "unclear",
                        "not stated",
                        "not clear",
                        "cannot determine",
                        "indeterminate",
                    }
                )
            extraction[field] = (
                "NA"
                if (
                    field in {"ethnicity", "location"}
                    and required_source != "full_text"
                    and text.casefold() in missing_values
                )
                else None if text.casefold() in missing_values else text
            )
    raw_effect_type = extraction.get("effect_type")
    if raw_effect_type is not None:
        extraction["reported_effect_type"] = raw_effect_type
        extraction["effect_is_adjusted"] = relaxed_policy.is_adjusted(extraction)
    if raw_effect_type is not None:
        extraction["effect_type"] = normalize_effect_type(raw_effect_type)
        if extraction["effect_type"] is None:
            raise ScreeningValidationError(
                f"Unsupported effect_type for pooling: {raw_effect_type!r}"
            )
        extraction["effect_size"] = _percentage_statement_to_ratio(
            extraction.get("effect_size"),
            raw_effect_type,
            source_text,
        )
    effect = extraction.get("effect_size")
    lower = extraction.get("ci_lower")
    upper = extraction.get("ci_upper")
    if lower is not None and upper is not None and lower > upper:
        raise ScreeningValidationError("ci_lower cannot exceed ci_upper")
    if effect is not None and lower is not None and effect < lower:
        raise ScreeningValidationError("effect_size cannot be below ci_lower")
    if effect is not None and upper is not None and effect > upper:
        raise ScreeningValidationError("effect_size cannot exceed ci_upper")
    if (
        extraction.get("cases") is not None
        and extraction.get("total_n") is not None
        and extraction["cases"] > extraction["total_n"]
    ):
        raise ScreeningValidationError("cases cannot exceed total_n")
    measurement = extraction.get("exposure_measurement_type")
    if measurement not in {"dietary_intake", "human_biospecimen", "unclear", None}:
        raise ScreeningValidationError(f"Invalid exposure_measurement_type: {measurement!r}")
    if extraction.get("exposure_subtype") not in {
        "whole_food",
        "dietary_component",
        "human_biomarker",
        "supplement",
        "mixed_or_unclear",
    }:
        raise ScreeningValidationError(
            f"Invalid exposure_subtype: {extraction.get('exposure_subtype')!r}"
        )
    evidence = _normalise_evidence(
        raw.get("evidence"),
        required_source=required_source,
        source_text=source_text,
        source_identifier=source_identifier,
    )
    extraction["evidence"] = evidence
    for key in ("ci_scale", "ci_original_log_bounds", "ci_scale_conversion", "effect_original_log_value", "effect_scale_conversion", "effect_size_imputed", "effect_size_imputation_method", "ci_level_assumed", "ci_original_confidence_level", "ci_original_reported_bounds", "ci_level_conversion"):
        if key in raw:
            extraction[key] = raw[key]

    # MetaVir demographics are a Sol-only extraction. A country, nationality,
    # population label, or model guess cannot substitute for an explicit
    # field-specific abstract statement. Unsupported values become NA.
    if required_source != "full_text":
        for field in ("ethnicity", "location"):
            value = one_line(extraction.get(field))
            excerpt = one_line((evidence.get(field) or {}).get("excerpt"))
            if (
                not value
                or value.casefold() == "na"
                or not excerpt
                or value.casefold() not in excerpt.casefold()
            ):
                extraction[field] = "NA"
                evidence[field] = None
    p_excerpt = one_line((evidence.get("p_value") or {}).get("excerpt"))
    extraction["p_value_reported_as"] = one_line(raw_p_value)
    extraction["p_value_is_exact"] = bool(
        extraction.get("p_value") is not None
        and not re.search(r"[<>≤≥]", f"{raw_p_value} {p_excerpt}")
    )
    # Field-specific evidence is retained when available, but it is no longer
    # a prerequisite for retaining an extracted value.  Null evidence renders
    # as an empty excerpt in downstream audit/UI helpers.
    for field, evidence_field in {
        "effect_size": "effect_size",
        "ci_lower": "confidence_interval",
        "ci_upper": "confidence_interval",
        "p_value": "p_value",
        "total_n": "sample_size",
        "cases": "cases",
    }.items():
        _require_numeric_support(
            field,
            extraction.get(field),
            evidence.get(evidence_field),
            source_text=source_text,
            extraction=extraction,
        )
    if required_source == "full_text":
        # A null R3 comparison means unchanged. Impute only after merging R2.
        add_wald_derivations(extraction)
    else:
        extraction["direction_original_comparison"] = raw.get("comparison_type")
        apply_poolability_policy(extraction)
    if extraction.get("exposure_subtype") == "mixed_or_unclear":
        extraction["exposure_classification_ambiguous"] = True
    return extraction


def missing_key_fields(extraction: dict[str, Any] | None) -> list[str]:
    extraction = extraction or {}
    apply_poolability_policy(extraction)
    return [field for field in KEY_EVIDENCE_FIELDS if extraction.get(field) is None]


def invalid_key_fields(extraction: dict[str, Any] | None) -> list[str]:
    """Return present required quantitative fields that are incoherent."""
    extraction = extraction or {}
    invalid: set[str] = set()
    if extraction.get("effect_type") not in WALD_RATIO_EFFECT_TYPES:
        invalid.add("effect_type")
    coherent: dict[str, bool] = {}
    for field in ("effect_size", "ci_lower", "ci_upper"):
        value = extraction.get(field)
        coherent[field] = bool(
            value is not None
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) > 0
        )
        if value is not None and not coherent[field]:
            invalid.add(field)
    effect = extraction.get("effect_size")
    lower = extraction.get("ci_lower")
    upper = extraction.get("ci_upper")
    if coherent["ci_lower"] and coherent["ci_upper"] and not float(lower) < float(upper):
        invalid.update({"ci_lower", "ci_upper"})
    if coherent["effect_size"] and coherent["ci_lower"] and float(effect) < float(lower):
        invalid.update({"effect_size", "ci_lower"})
    if coherent["effect_size"] and coherent["ci_upper"] and float(effect) > float(upper):
        invalid.update({"effect_size", "ci_upper"})
    total_n = extraction.get("total_n")
    if total_n is not None and (
        not isinstance(total_n, (int, float))
        or isinstance(total_n, bool)
        or not math.isfinite(float(total_n))
        or total_n <= 0
        or not float(total_n).is_integer()
    ):
        invalid.add("total_n")
    return sorted(invalid)


def required_quantitative_issues(extraction: dict[str, Any] | None) -> list[str]:
    return sorted(set(missing_key_fields(extraction)) | set(invalid_key_fields(extraction)))


def needs_full_text(extraction: dict[str, Any] | None, jbi: dict[str, Any] | None) -> bool:
    """Apply the shared Fair or missing-required-quantitative third-round gate."""
    extraction = extraction or {}
    return bool(
        (jbi or {}).get("grade") == "Fair"
        or required_quantitative_issues(extraction)
    )


def is_full_text_candidate(item: dict[str, Any]) -> bool:
    """Require inclusion, a PMCID, and the exact post-Wald third-round gate."""
    return bool(
        item.get("eligibility") == "INCLUDE"
        and item.get("pmcid")
        and needs_full_text(item.get("extraction"), item.get("jbi"))
    )


def is_poolable(extraction: dict[str, Any] | None, jbi: dict[str, Any] | None) -> bool:
    extraction = extraction or {}
    return bool(
        not required_quantitative_issues(extraction)
        and (jbi or {}).get("grade") in {"Moderate", "Good"}
    )


def validate_first(payload: dict[str, Any], expected: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    returned = _exact_results(payload, expected)
    results = []
    for cid, base in expected.items():
        raw = returned[cid]
        decision = str(raw.get("decision") or "").upper()
        if decision not in {"PASS", "FAIL"}:
            raise ScreeningValidationError(f"Invalid first-screen decision for {cid}: {decision!r}")
        reason = one_sentence(raw.get("reason"))
        if base.get("protocol_exclusion"):
            decision = "PASS"
            reason = "Potential protocol exclusion requires explicit coding at the detailed screen."
        results.append(
            {
                "context_id": cid,
                "pmid": base["pmid"],
                "exposure": base["exposure"],
                "cancer": base["cancer"],
                "outcome": "Incidence",
                "decision": decision,
                "reason": reason,
            }
        )
    return sorted(results, key=lambda item: item["context_id"])


def validate_second(payload: dict[str, Any], expected: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    returned = _exact_results(payload, expected)
    results = []
    for cid, base in expected.items():
        raw = returned[cid]
        eligibility = str(raw.get("eligibility") or "").upper()
        code = raw.get("exclusion_code")
        reason = one_sentence(raw.get("reason"))
        if base.get("protocol_exclusion"):
            eligibility = "EXCLUDE"
            code, reason = base["protocol_exclusion"]
        elif eligibility == "INCLUDE" and _is_absolute_mean_difference(
            raw.get("extraction")
        ):
            eligibility = "EXCLUDE"
            code = "S2-NO-TARGET-ESTIMATE"
            reason = (
                "The article reports an absolute mean difference that cannot be "
                "incorporated into this ratio-based meta-analysis."
            )
        if eligibility not in {"INCLUDE", "EXCLUDE"}:
            raise ScreeningValidationError(f"Invalid eligibility for {cid}: {eligibility!r}")
        if eligibility == "EXCLUDE":
            if code not in EXCLUSION_CODES:
                raise ScreeningValidationError(f"Invalid exclusion code for {cid}: {code!r}")
            extraction = None
            jbi = None
            missing = []
            invalid = []
            escalate = False
        else:
            if code not in (None, ""):
                raise ScreeningValidationError(f"Included context {cid} has exclusion code {code!r}")
            extraction = _normalise_extraction(
                raw.get("extraction"),
                required_source="abstract",
                source_text=base.get("_abstract_text") or "",
                source_identifier=base["pmid"],
            )
            jbi = _strict_jbi(raw.get("jbi"))
            missing = missing_key_fields(extraction)
            invalid = invalid_key_fields(extraction)
            escalate = needs_full_text(extraction, jbi)
            code = None
        results.append(
            {
                **{
                    key: value
                    for key, value in base.items()
                    if key != "protocol_exclusion" and not key.startswith("_")
                },
                "eligibility": eligibility,
                "exclusion_code": code,
                "reason": reason,
                "missing_key_fields": missing,
                "invalid_key_fields": invalid,
                "needs_full_text": escalate,
                "full_text_eligible": bool(escalate and base.get("pmcid")),
                "extraction": extraction,
                "jbi": jbi,
            }
        )
    return sorted(results, key=lambda item: item["context_id"])


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _problem_unit_key(track: str, stage: str, context_ids: Iterable[str]) -> str:
    material = "\n".join([track, stage, *sorted(str(value) for value in context_ids)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _safe_problem_error(error: Exception) -> str:
    value = one_line(error)
    value = re.sub(
        r"(?i)(api[_ -]?key|authorization|bearer)(\s*[:=]?\s*)\S+",
        r"\1\2[REDACTED]",
        value,
    )
    return value[:500]


def _problem_type(error: Exception) -> str:
    if isinstance(error, ScreeningGatewayError):
        return "gateway"
    if isinstance(error, ScreeningValidationError):
        return "strict_validation"
    if isinstance(error, ScreeningResponseError):
        return "malformed_response"
    raise TypeError(f"Unsupported recoverable screening problem: {type(error).__name__}")


def _context_audit(context_ids: Iterable[str]) -> dict[str, Any]:
    contexts: list[dict[str, str]] = []
    for value in sorted(str(item) for item in context_ids):
        parts = value.split("|", 2)
        pmid, exposure, disease_scope = (parts + [""] * 3)[:3]
        contexts.append(
            {
                "context_id": value,
                "pmid": pmid,
                "exposure": exposure,
                "disease_scope": disease_scope,
            }
        )
    return {
        "context_ids": [item["context_id"] for item in contexts],
        "contexts": contexts,
        "pmids": sorted({item["pmid"] for item in contexts if item["pmid"]}),
        "exposures": sorted(
            {item["exposure"] for item in contexts if item["exposure"]}
        ),
        "disease_scopes": sorted(
            {
                item["disease_scope"]
                for item in contexts
                if item["disease_scope"]
            }
        ),
    }


def _write_problem_event(
    output_dir: Path,
    record: dict[str, Any],
    event: str,
) -> None:
    """Keep a durable run-level audit of deferred and later resolved work."""
    path = output_dir / "screening_problem_ledger.json"
    ledger = load_json(path, {})
    if not isinstance(ledger, dict):
        ledger = {}
    pending = {
        str(item.get("unit_key")): item
        for item in ledger.get("pending", [])
        if isinstance(item, dict) and item.get("unit_key")
    }
    event_record = {
        "event": event,
        "at": utc_now(),
        **record,
    }
    if event == "deferred":
        pending[record["unit_key"]] = record
    elif event == "resolved":
        pending.pop(record["unit_key"], None)
    else:  # pragma: no cover - internal programming guard
        raise ValueError(f"Unknown screening-problem ledger event: {event}")
    events = [item for item in ledger.get("events", []) if isinstance(item, dict)]
    events.append(event_record)
    save_json(
        path,
        {
            "schema_version": 1,
            "updated_at": utc_now(),
            "pending_count": len(pending),
            "pending": sorted(pending.values(), key=lambda item: item["unit_key"]),
            "events": events,
        },
    )


def _active_deferred_problems(
    output_dir: Path,
    checkpoint: dict[str, Any],
    progress: dict[str, dict[str, Any]],
    *,
    track: str | None = None,
    stage: str | None = None,
) -> dict[str, dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    records = checkpoint.get("deferred_screening_problems")
    if records is None:  # resume checkpoints created by the earlier gateway-only contract
        records = checkpoint.get("deferred_gateway_batches", [])
    for record in records or []:
        if not isinstance(record, dict) or not record.get("unit_key"):
            continue
        active[str(record["unit_key"])] = record
    if track is not None and stage is not None:
        ledger = load_json(output_dir / "screening_problem_ledger.json", {})
        for record in ledger.get("pending", []) if isinstance(ledger, dict) else []:
            if (
                not isinstance(record, dict)
                or not record.get("unit_key")
                or record.get("track") != track
                or record.get("stage") != stage
            ):
                continue
            active[str(record["unit_key"])] = record
    _resolve_completed_deferred_problems(output_dir, active, progress)
    return active


def reconcile_completed_problem_ledger(
    output_dir: Path,
    progress: dict[str, dict[str, Any]],
    *,
    track: str,
    stage: str,
) -> None:
    """Resolve durable pending records whose complete results already exist."""
    _active_deferred_problems(
        output_dir,
        {},
        progress,
        track=track,
        stage=stage,
    )


def _resolve_completed_deferred_problems(
    output_dir: Path,
    deferred_by_key: dict[str, dict[str, Any]],
    progress: dict[str, dict[str, Any]],
) -> None:
    """Close old grouped failures when a finer-grained resume completes them."""
    for unit_key, record in list(deferred_by_key.items()):
        context_ids = [str(value) for value in record.get("context_ids", [])]
        if not context_ids or not all(context_id in progress for context_id in context_ids):
            continue
        deferred_by_key.pop(unit_key, None)
        _write_problem_event(
            output_dir,
            {**record, "resolved_at": utc_now()},
            "resolved",
        )


def _run_units_with_problem_deferral(
    output_dir: Path,
    runtime: TrackRuntime,
    stage: str,
    units: list[Any],
    process_unit: Callable[[Any], None],
    context_ids_for: Callable[[Any], list[str]],
    label_for: Callable[[Any], str],
    deferred_by_key: dict[str, dict[str, Any]],
    persist_progress: Callable[[], None],
) -> None:
    """Finish ordinary work first, then revisit recoverable problem units."""

    def metadata(unit: Any) -> tuple[str, list[str], str]:
        context_ids = sorted(context_ids_for(unit))
        return (
            _problem_unit_key(runtime.name, stage, context_ids),
            context_ids,
            label_for(unit),
        )

    # A resumed run must continue with untouched work before revisiting the
    # locations already marked as recoverable screening problems.
    ordered_units = sorted(
        units,
        key=lambda unit: (metadata(unit)[0] in deferred_by_key,),
    )
    deferred_this_pass: list[Any] = []

    def defer(unit: Any, error: Exception) -> None:
        unit_key, context_ids, label = metadata(unit)
        previous = deferred_by_key.get(unit_key) or {}
        record = {
            "unit_key": unit_key,
            "track": runtime.name,
            "stage": stage,
            "model": runtime.model_for(stage),
            "location": label,
            **_context_audit(context_ids),
            "problem_type": _problem_type(error),
            "deferrals": int(previous.get("deferrals") or 0) + 1,
            "first_deferred_at": previous.get("first_deferred_at") or utc_now(),
            "last_deferred_at": utc_now(),
            "last_error": _safe_problem_error(error),
        }
        deferred_by_key[unit_key] = record
        _write_problem_event(output_dir, record, "deferred")
        persist_progress()
        print(
            f"{runtime.name} {stage}: deferred {record['problem_type']} problem at "
            f"{label}; "
            "continuing with later work",
            flush=True,
        )

    def resolve(unit: Any) -> None:
        unit_key, _context_ids, label = metadata(unit)
        record = deferred_by_key.pop(unit_key, None)
        if record is None:
            return
        resolved = {**record, "location": label, "resolved_at": utc_now()}
        _write_problem_event(output_dir, resolved, "resolved")
        persist_progress()
        print(
            f"{runtime.name} {stage}: resolved deferred screening problem at {label}",
            flush=True,
        )

    for unit in ordered_units:
        try:
            process_unit(unit)
        except RECOVERABLE_SCREENING_PROBLEMS as exc:
            defer(unit, exc)
            deferred_this_pass.append(unit)
        else:
            resolve(unit)

    if deferred_this_pass:
        print(
            f"{runtime.name} {stage}: retrying {len(deferred_this_pass)} deferred "
            "screening-problem work unit(s) after the main pass",
            flush=True,
        )

    unresolved: list[Any] = []
    for unit in deferred_this_pass:
        try:
            process_unit(unit)
        except RECOVERABLE_SCREENING_PROBLEMS as exc:
            defer(unit, exc)
            unresolved.append(unit)
        else:
            resolve(unit)

    if unresolved:
        locations = ", ".join(label_for(unit) for unit in unresolved[:10])
        suffix = "" if len(unresolved) <= 10 else f" and {len(unresolved) - 10} more"
        raise UnresolvedScreeningProblems(
            f"{runtime.name} {stage} has {len(unresolved)} unresolved deferred screening "
            f"work unit(s) after the end-of-stage retry: {locations}{suffix}; rerun "
            "the same stage to resume only the missing contexts"
        )


def _validated_call(
    runtime: TrackRuntime,
    stage: str,
    system: str,
    prompt: str,
    usage: dict[str, Any],
    validator: Callable[[dict[str, Any]], list[dict[str, Any]]],
    *,
    exposure_weights: dict[str, int] | None = None,
    on_usage: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    errors: list[str] = []
    retry_prompt = prompt
    for _ in range(2):
        payload = call_json(
            runtime,
            stage,
            system,
            retry_prompt,
            usage,
            exposure_weights=exposure_weights,
            on_usage=on_usage,
        )
        try:
            return validator(payload)
        except ScreeningValidationError as exc:
            errors.append(str(exc))
            retry_prompt = (
                prompt
                + "\n\nVALIDATION FEEDBACK FROM THE PREVIOUS RESPONSE:\n"
                + str(exc)
                + "\nCorrect every listed contract violation and return the complete result set again."
            )
    raise ScreeningValidationError(
        f"{runtime.name} {stage} response failed strict validation twice: {'; '.join(errors)}"
    )


def _exposure_weights(contexts: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Count contexts so shared-call token allocation is explicit and reproducible."""
    return dict(
        Counter(
            str(context.get("exposure") or "").strip()
            for context in contexts
            if str(context.get("exposure") or "").strip()
        )
    )


def _update_cost_report(output_dir: Path, runtime: TrackRuntime) -> None:
    if runtime.name != "chatgpt":
        return
    # Lazy import avoids coupling the standalone accounting helper to model code.
    from scripts import cost_accounting

    cost_accounting.update_cost_report_for_run(output_dir)


def _article_lookup(candidates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(article.get("pmid") or ""): article for article in candidates.get("articles", [])}


def _first_prompt(
    articles: list[dict[str, Any]],
    contexts: dict[str, dict[str, Any]],
) -> str:
    compact = []
    for article in articles:
        pmid = str(article.get("pmid") or "")
        article_contexts = [
            {
                "context_id": cid,
                "exposure": value["exposure"],
                "cancer": value["cancer"],
                "outcome": "Incidence",
            }
            for cid, value in contexts.items()
            if value["pmid"] == pmid
        ]
        if article_contexts:
            compact.append(
                {
                    "pmid": pmid,
                    "title": article.get("title") or "",
                    "abstract": str(article.get("abstract") or ""),
                    "mesh": article.get("mesh") or [],
                    "contexts": article_contexts,
                }
            )
    return FIRST_INSTRUCTIONS + "\n\nARTICLES:\n" + json.dumps(
        compact, ensure_ascii=False, separators=(",", ":")
    )


def run_first_track(
    output_dir: Path,
    candidates: dict[str, Any],
    runtime: TrackRuntime,
    batch_size: int = 20,
    force: bool = False,
) -> dict[str, Any]:
    output_path = output_dir / f"{runtime.name}_first_screen.json"
    progress_path = output_dir / f"{runtime.name}_first_screen_progress.json"
    expected = expected_contexts(candidates)
    input_digest = first_input_digest(candidates, runtime.first_model)
    context_input_digests = first_context_input_digests(
        candidates, runtime.first_model
    )
    existing = {} if force else load_json(output_path, {})
    if (
        existing.get("model") != runtime.first_model
        or existing.get("input_digest") != input_digest
    ):
        existing = {}
    existing_results = {
        item["context_id"]: item
        for item in existing.get("results", [])
        if item.get("context_id") in expected
    }
    if existing and set(existing_results) == set(expected):
        return existing

    checkpoint = {} if force else load_json(progress_path, {})
    if (
        checkpoint.get("model") != runtime.first_model
        or checkpoint.get("input_digest") != input_digest
    ):
        checkpoint = {}
    progress = dict(existing_results)
    checkpoint_context_digests = checkpoint.get("context_input_digests")
    if checkpoint_context_digests is not None and not isinstance(
        checkpoint_context_digests, dict
    ):
        raise ScreeningValidationError(
            f"{runtime.name} first-screen context digest map is invalid"
        )
    progress.update(
        {
            cid: item
            for cid, item in (checkpoint.get("results_by_context") or {}).items()
            if cid in expected
            and (
                checkpoint_context_digests is None
                or checkpoint_context_digests.get(cid) == context_input_digests[cid]
            )
        }
    )
    usage = checkpoint.get("usage") or existing.get("usage") or empty_usage(runtime.first_model)
    generation_migration = checkpoint.get("generation_migration")
    deferred_by_key = _active_deferred_problems(output_dir, checkpoint, progress)

    def persist_progress() -> None:
        _resolve_completed_deferred_problems(output_dir, deferred_by_key, progress)
        save_json(
            progress_path,
            {
                "updated_at": utc_now(),
                "model": runtime.first_model,
                "input_digest": input_digest,
                "usage": usage,
                "results_by_context": progress,
                "context_input_digests": {
                    cid: context_input_digests[cid]
                    for cid in progress
                },
                "deferred_screening_problems": sorted(
                    deferred_by_key.values(), key=lambda item: item["unit_key"]
                ),
                **(
                    {"generation_migration": generation_migration}
                    if isinstance(generation_migration, dict)
                    else {}
                ),
            },
        )
        _update_cost_report(output_dir, runtime)

    article_by_pmid = _article_lookup(candidates)
    pending_pmids = sorted(
        {base["pmid"] for cid, base in expected.items() if cid not in progress},
        key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
    )
    pending_articles = [article_by_pmid[pmid] for pmid in pending_pmids]
    work_units = list(enumerate(_chunks(pending_articles, batch_size), start=1))

    def context_ids_for(unit: tuple[int, list[dict[str, Any]]]) -> list[str]:
        _batch_number, article_batch = unit
        pmids = {str(article.get("pmid") or "") for article in article_batch}
        return [
            cid
            for cid, value in expected.items()
            if value["pmid"] in pmids
        ]

    def label_for(unit: tuple[int, list[dict[str, Any]]]) -> str:
        _batch_number, article_batch = unit
        pmids = [str(article.get("pmid") or "") for article in article_batch]
        return "PMIDs " + ",".join(pmids)

    def process_unit(unit: tuple[int, list[dict[str, Any]]]) -> None:
        batch_number, article_batch = unit
        batch_expected = {
            cid: expected[cid]
            for cid in context_ids_for(unit)
            if cid not in progress
        }
        if not batch_expected:
            return
        prompt = _first_prompt(article_batch, batch_expected)
        results = _validated_call(
            runtime,
            "first",
            FIRST_SYSTEM,
            prompt,
            usage,
            lambda payload, values=batch_expected: validate_first(payload, values),
            exposure_weights=_exposure_weights(batch_expected.values()),
            on_usage=persist_progress,
        )
        progress.update({item["context_id"]: item for item in results})
        persist_progress()
        print(
            f"{runtime.name} first screen: batch {batch_number}; {len(progress)}/{len(expected)} contexts",
            flush=True,
        )

    _run_units_with_problem_deferral(
        output_dir,
        runtime,
        "first",
        work_units,
        process_unit,
        context_ids_for,
        label_for,
        deferred_by_key,
        persist_progress,
    )

    results = sorted(progress.values(), key=lambda item: item["context_id"])
    if len(results) != len(expected):
        raise ScreeningValidationError(f"{runtime.name} first screen is incomplete")
    packet = {
        "schema_version": 2,
        "created_at": utc_now(),
        "track": runtime.name,
        "model": runtime.first_model,
        "protocol_version": SCREENING_PROTOCOL_VERSION,
        "input_digest": input_digest,
        "source_candidates": len(candidates.get("articles", [])),
        "source_contexts": len(expected),
        "pass": sum(item["decision"] == "PASS" for item in results),
        "fail": sum(item["decision"] == "FAIL" for item in results),
        "usage": usage,
        "results": results,
    }
    save_json(output_path, packet)
    progress_path.unlink(missing_ok=True)
    _update_cost_report(output_dir, runtime)
    return packet


def _second_prompt(
    articles: list[dict[str, Any]],
    contexts: dict[str, dict[str, Any]],
) -> str:
    compact = []
    for article in articles:
        pmid = str(article.get("pmid") or "")
        article_contexts = [
            {
                "context_id": cid,
                "exposure": value["exposure"],
                "cancer": value["cancer"],
                "outcome": "Incidence",
            }
            for cid, value in contexts.items()
            if value["pmid"] == pmid
        ]
        if article_contexts:
            compact.append(
                {
                    "pmid": pmid,
                    "title": article.get("title") or "",
                    "abstract": str(article.get("abstract") or ""),
                    "mesh": article.get("mesh") or [],
                    "contexts": article_contexts,
                }
            )
    return SECOND_INSTRUCTIONS + "\n\nARTICLES:\n" + json.dumps(
        compact, ensure_ascii=False, separators=(",", ":")
    )


def run_second_track(
    output_dir: Path,
    candidates: dict[str, Any],
    first_packet: dict[str, Any],
    runtime: TrackRuntime,
    batch_size: int = 8,
    force: bool = False,
) -> dict[str, Any]:
    output_path = output_dir / f"{runtime.name}_second_screen.json"
    progress_path = output_dir / f"{runtime.name}_second_screen_progress.json"
    all_expected = expected_contexts(candidates)
    passing = {
        item["context_id"]: item
        for item in first_packet.get("results", [])
        if item.get("decision") == "PASS"
    }
    expected = {cid: all_expected[cid] for cid in passing if cid in all_expected}
    input_digest = second_input_digest(candidates, first_packet, runtime.second_model)
    existing = {} if force else load_json(output_path, {})
    if (
        existing.get("model") != runtime.second_model
        or existing.get("input_digest") != input_digest
    ):
        existing = {}
    existing_results = {
        item["context_id"]: item
        for item in existing.get("results", [])
        if item.get("context_id") in expected
    }
    if existing and set(existing_results) == set(expected):
        return existing

    checkpoint = {} if force else load_json(progress_path, {})
    if (
        checkpoint.get("model") != runtime.second_model
        or checkpoint.get("input_digest") != input_digest
    ):
        checkpoint = {}
    progress = dict(existing_results)
    progress.update(
        {
            cid: item
            for cid, item in (checkpoint.get("results_by_context") or {}).items()
            if cid in expected
        }
    )
    usage = checkpoint.get("usage") or existing.get("usage") or empty_usage(runtime.second_model)
    deferred_by_key = _active_deferred_problems(output_dir, checkpoint, progress)

    def persist_progress() -> None:
        _resolve_completed_deferred_problems(output_dir, deferred_by_key, progress)
        save_json(
            progress_path,
            {
                "updated_at": utc_now(),
                "model": runtime.second_model,
                "input_digest": input_digest,
                "usage": usage,
                "results_by_context": progress,
                "deferred_screening_problems": sorted(
                    deferred_by_key.values(), key=lambda item: item["unit_key"]
                ),
            },
        )
        _update_cost_report(output_dir, runtime)

    article_by_pmid = _article_lookup(candidates)
    pending_pmids = sorted({base["pmid"] for cid, base in expected.items() if cid not in progress})
    pending_articles = [article_by_pmid[pmid] for pmid in pending_pmids]
    work_units = list(enumerate(_chunks(pending_articles, batch_size), start=1))

    def context_ids_for(unit: tuple[int, list[dict[str, Any]]]) -> list[str]:
        _batch_number, article_batch = unit
        pmids = {str(article.get("pmid") or "") for article in article_batch}
        return [cid for cid, value in expected.items() if value["pmid"] in pmids]

    def label_for(unit: tuple[int, list[dict[str, Any]]]) -> str:
        _batch_number, article_batch = unit
        pmids = [str(article.get("pmid") or "") for article in article_batch]
        return "PMIDs " + ",".join(pmids)

    def process_unit(unit: tuple[int, list[dict[str, Any]]]) -> None:
        batch_number, article_batch = unit
        batch_expected = {
            cid: expected[cid]
            for cid in context_ids_for(unit)
            if cid not in progress
        }
        if not batch_expected:
            return
        prompt = _second_prompt(article_batch, batch_expected)
        results = _validated_call(
            runtime,
            "second",
            SECOND_SYSTEM,
            prompt,
            usage,
            lambda payload, values=batch_expected: validate_second(payload, values),
            exposure_weights=_exposure_weights(batch_expected.values()),
            on_usage=persist_progress,
        )
        progress.update({item["context_id"]: item for item in results})
        persist_progress()
        print(
            f"{runtime.name} second screen: batch {batch_number}; {len(progress)}/{len(expected)} contexts",
            flush=True,
        )

    _run_units_with_problem_deferral(
        output_dir,
        runtime,
        "second",
        work_units,
        process_unit,
        context_ids_for,
        label_for,
        deferred_by_key,
        persist_progress,
    )

    results = sorted(progress.values(), key=lambda item: item["context_id"])
    if len(results) != len(expected):
        raise ScreeningValidationError(f"{runtime.name} second screen is incomplete")
    packet = {
        "schema_version": 2,
        "created_at": utc_now(),
        "track": runtime.name,
        "model": runtime.second_model,
        "protocol_version": SCREENING_PROTOCOL_VERSION,
        "input_digest": input_digest,
        "source_first_model": first_packet.get("model"),
        "source_first_input_digest": first_packet.get("input_digest"),
        "source_first_screen_pass": len(expected),
        "include": sum(item["eligibility"] == "INCLUDE" for item in results),
        "exclude": sum(item["eligibility"] == "EXCLUDE" for item in results),
        "needs_full_text": sum(bool(item.get("needs_full_text")) for item in results),
        "pmcid_full_text_candidates": sum(bool(item.get("full_text_eligible")) for item in results),
        "usage": usage,
        "results": results,
    }
    save_json(output_path, packet)
    progress_path.unlink(missing_ok=True)
    _update_cost_report(output_dir, runtime)
    return packet


def _merge_extractions(abstract: dict[str, Any], full_text: dict[str, Any]) -> dict[str, Any]:
    merged = dict(abstract or {})
    full_evidence = full_text.get("evidence") or {}
    for field in EXTRACTION_FIELDS:
        if field in {"ethnicity", "location"}:
            # These are owned by R2 Sol. R3 Luna may recover quantitative/JBI
            # information, but it must preserve Sol's explicit value or NA.
            continue
        value = full_text.get(field)
        if value is None:
            continue
        if field == "needs_inversion" and not (
            full_text.get("comparison_type") and full_evidence.get("comparison")
        ):
            # The full-text schema requires a boolean. Do not let a default
            # False silently undo an abstract direction decision without new
            # verbatim comparison evidence.
            continue
        if field == "exposure_subtype" and value == "mixed_or_unclear" and merged.get(field):
            continue
        if field == "exposure_measurement_type" and value == "unclear" and merged.get(field):
            continue
        if (
            field == "exposure_classification_ambiguous"
            and value is True
            and full_text.get("exposure_subtype") == "mixed_or_unclear"
            and full_text.get("exposure_measurement_type") == "unclear"
            and merged.get("exposure_subtype") != "mixed_or_unclear"
            and merged.get("exposure_measurement_type") != "unclear"
        ):
            # The full-text schema needs placeholder classification values.
            # Do not let them erase a definitive Sol classification when Luna
            # supplied no new exposure-classification evidence.
            continue
        if field == "wald_inputs_same_estimate" and value is False and not any(
            full_evidence.get(name)
            for name in ("effect_size", "confidence_interval", "p_value", "comparison")
        ):
            continue
        merged[field] = value
    merged_evidence = dict((abstract or {}).get("evidence") or {})
    for field, evidence in full_evidence.items():
        if field in {"ethnicity", "location"}:
            continue
        if evidence:
            merged_evidence[field] = evidence
    merged["evidence"] = merged_evidence
    for field in (
        "p_value_reported_as",
        "p_value_is_exact",
        "wald_derivations",
        "ci_method",
        "ci_source_p_value",
        "p_value_method",
    ):
        if full_text.get(field) is not None:
            merged[field] = full_text[field]
    if (
        _explicit_comparison_direction(full_text.get("comparison_type"))
        and full_evidence.get("comparison")
    ):
        merged["direction_imputed"] = False
        merged.pop("direction_imputation_reason", None)
        merged["direction_original_comparison"] = full_text.get("comparison_type")
    apply_poolability_policy(merged)
    return merged


def validate_full_text(
    payload: dict[str, Any],
    expected: dict[str, dict[str, Any]],
    *,
    source_text: str | None = None,
    source_identifier: str | None = None,
) -> list[dict[str, Any]]:
    returned = _exact_results(payload, expected)
    results = []
    for cid, base in expected.items():
        raw = returned[cid]
        status = str(raw.get("status") or "").upper()
        code = raw.get("exclusion_code")
        reason = one_sentence(raw.get("reason"))
        if status not in {"INCLUDE", "EXCLUDE"}:
            raise ScreeningValidationError(f"Invalid full-text status for {cid}: {status!r}")
        if status == "EXCLUDE":
            if code not in EXCLUSION_CODES:
                raise ScreeningValidationError(f"Invalid full-text exclusion code for {cid}: {code!r}")
            extraction = base.get("extraction") or {}
            jbi = base.get("jbi")
            final_status = "EXCLUDE"
        else:
            if code not in (None, ""):
                raise ScreeningValidationError(f"Included full-text context {cid} has exclusion code")
            full_extraction = _normalise_extraction(
                raw.get("extraction"),
                required_source="full_text",
                source_text=source_text,
                source_identifier=source_identifier,
            )
            extraction = _merge_extractions(base.get("extraction") or {}, full_extraction)
            jbi = _strict_jbi(raw.get("jbi"))
            code = None
            final_status = "INCLUDE_META" if is_poolable(extraction, jbi) else "ELIGIBLE_NOT_POOLABLE"
        results.append(
            {
                **base,
                "final_status": final_status,
                "full_text_status": final_status,
                "exclusion_code": code,
                "reason": reason,
                "missing_key_fields": missing_key_fields(extraction),
                "invalid_key_fields": invalid_key_fields(extraction),
                "extraction": extraction,
                "jbi": jbi,
                "pmc_source_status": "full_text",
            }
        )
    return sorted(results, key=lambda item: item["context_id"])


def load_full_text_manual_adjudications(
    output_dir: Path,
    contexts: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str | None]]:
    """Load conservative, run-specific resolutions for repeatedly deferred contexts."""
    path = output_dir / FULL_TEXT_MANUAL_ADJUDICATIONS_FILENAME
    if not path.exists():
        return {}
    packet = load_json(path, {})
    records = packet.get("adjudications")
    if packet.get("schema_version") != 1 or not isinstance(records, list):
        raise ScreeningValidationError(f"Invalid manual adjudication packet: {path}")
    recorded_at = one_line(packet.get("recorded_at"))
    if not recorded_at:
        raise ScreeningValidationError(f"Manual adjudication packet lacks recorded_at: {path}")
    adjudications: dict[str, dict[str, str | None]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ScreeningValidationError(f"Invalid manual adjudication record: {record!r}")
        context_id = one_line(record.get("context_id"))
        status = one_line(record.get("full_text_status")).upper()
        code = one_line(record.get("exclusion_code")) or None
        reason = one_sentence(record.get("reason"))
        if context_id not in contexts or context_id in adjudications:
            raise ScreeningValidationError(
                f"Unknown or duplicate manual adjudication context: {context_id!r}"
            )
        if status not in {"ELIGIBLE_NOT_POOLABLE", "EXCLUDE"}:
            raise ScreeningValidationError(
                f"Manual adjudication {context_id} has invalid status: {status!r}"
            )
        if status == "EXCLUDE" and code not in EXCLUSION_CODES:
            raise ScreeningValidationError(
                f"Manual adjudication {context_id} has invalid exclusion code: {code!r}"
            )
        if status != "EXCLUDE" and code is not None:
            raise ScreeningValidationError(
                f"Non-excluded manual adjudication {context_id} cannot have an exclusion code"
            )
        adjudications[context_id] = {
            "full_text_status": status,
            "exclusion_code": code,
            "reason": reason,
            "recorded_at": recorded_at,
        }
    return adjudications


def run_full_text_track(
    output_dir: Path,
    second_packet: dict[str, Any],
    sources: dict[str, dict[str, str]],
    runtime: TrackRuntime,
    force: bool = False,
) -> dict[str, Any]:
    output_path = output_dir / f"{runtime.name}_full_text_review.json"
    progress_path = output_dir / f"{runtime.name}_full_text_review_progress.json"
    contexts = {
        item["context_id"]: item
        for item in second_packet.get("results", [])
        if is_full_text_candidate(item)
    }
    relevant_pmcids = {str(item.get("pmcid") or "") for item in contexts.values()}
    source_manifest = full_text_source_manifest(sources, relevant_pmcids)
    manual_adjudications = load_full_text_manual_adjudications(output_dir, contexts)
    input_digest = full_text_input_digest(
        second_packet,
        runtime.full_text_model,
        source_manifest,
        manual_adjudications,
    )
    source_failures = [
        {
            "pmcid": pmcid,
            "status": entry.get("status"),
            "characters": entry.get("characters"),
            "sha256": entry.get("sha256"),
            "error": entry.get("error") or "",
            "context_ids": sorted(
                cid
                for cid, context in contexts.items()
                if str(context.get("pmcid") or "") == pmcid
            ),
        }
        for pmcid, entry in sorted(source_manifest.items())
        if (
            entry.get("status") != "full_text"
            or isinstance(entry.get("characters"), bool)
            or not isinstance(entry.get("characters"), int)
            or int(entry.get("characters") or 0) < 2000
            or bool(entry.get("error"))
        )
    ]
    if source_failures:
        # A gated PMCID without complete source text is an operationally blocked
        # R3 unit, never a terminal screening disposition. Preserve the exact
        # source audit for retry, but remove any stale packet that could otherwise
        # be mistaken for a valid conditional-Luna result.
        output_path.unlink(missing_ok=True)
        save_json(
            progress_path,
            {
                "schema_version": 2,
                "updated_at": utc_now(),
                "status": "blocked_incomplete_pmc_source",
                "track": runtime.name,
                "model": runtime.full_text_model,
                "protocol_version": SCREENING_PROTOCOL_VERSION,
                "input_digest": input_digest,
                "source_second_model": second_packet.get("model"),
                "source_second_input_digest": second_packet.get("input_digest"),
                "source_manifest": source_manifest,
                "source_manifest_digest": stable_json_digest(source_manifest),
                "source_failures": source_failures,
                "results_by_context": {},
            },
        )
        failed = ", ".join(
            f"{item['pmcid']} ({item['status']})" for item in source_failures
        )
        raise ScreeningValidationError(
            "Conditional Luna screening requires complete verified PMC full text; "
            f"retry retrieval before finalization: {failed}"
        )
    existing = {}
    existing_matches_manual = False
    if not force:
        candidate = load_json(output_path, {})
        if candidate and candidate.get("source_manifest") == source_manifest:
            try:
                validate_full_text_packet_lineage(
                    second_packet,
                    candidate,
                    runtime.name,
                    runtime.full_text_model,
                )
            except ScreeningValidationError:
                pass
            else:
                existing = candidate
                try:
                    validate_full_text_packet_lineage(
                        second_packet,
                        candidate,
                        runtime.name,
                        runtime.full_text_model,
                        manual_adjudications=manual_adjudications,
                    )
                except ScreeningValidationError:
                    pass
                else:
                    existing_matches_manual = True
    existing_results = {
        item["context_id"]: item
        for item in existing.get("results", [])
        if item.get("context_id") in contexts
    }
    if (
        existing_matches_manual
        and set(existing_results) == set(contexts)
    ):
        reconcile_completed_problem_ledger(
            output_dir,
            existing_results,
            track=runtime.name,
            stage="full_text",
        )
        return existing

    # A new packet is required.  Remove any stale/invalid prior packet before a
    # retrieval or model failure can interrupt this replacement run.
    output_path.unlink(missing_ok=True)
    checkpoint = {} if force else load_json(progress_path, {})
    if (
        checkpoint.get("model") != runtime.full_text_model
        or checkpoint.get("protocol_version") != SCREENING_PROTOCOL_VERSION
        or checkpoint.get("input_digest") != input_digest
        or checkpoint.get("source_second_model") != second_packet.get("model")
        or checkpoint.get("source_second_input_digest")
        != second_packet.get("input_digest")
        or checkpoint.get("source_manifest") != source_manifest
        or checkpoint.get("source_manifest_digest")
        != stable_json_digest(source_manifest)
    ):
        checkpoint = {}
    progress = dict(existing_results)
    for cid, item in (checkpoint.get("results_by_context") or {}).items():
        context = contexts.get(cid)
        if not isinstance(context, dict) or not isinstance(item, dict):
            continue
        pmcid = str(context.get("pmcid") or "")
        source = source_manifest.get(pmcid) or {}
        if (
            str(item.get("pmcid") or "") == pmcid
            and item.get("pmc_source_status") == "full_text"
            and item.get("pmc_source_sha256") == source.get("sha256")
            and item.get("full_text_characters_reviewed")
            == source.get("characters")
            and item.get("third_round_status") == "COMPLETED"
            and item.get("third_round_model") == runtime.full_text_model
        ):
            progress[cid] = item
    existing_adjudications = _manual_full_text_adjudications_from_packet(
        existing,
        track=runtime.name,
    ) if existing else {}
    for cid in set(existing_adjudications) | set(manual_adjudications):
        if existing_adjudications.get(cid) != manual_adjudications.get(cid):
            # Manual adjudication can refine disposition only after a current
            # actual Luna result; additions, changes, and removals all require
            # that context to be screened again.
            progress.pop(cid, None)
    usage = checkpoint.get("usage") or existing.get("usage") or empty_usage(runtime.full_text_model)
    deferred_by_key = _active_deferred_problems(
        output_dir,
        checkpoint,
        progress,
        track=runtime.name,
        stage="full_text",
    )

    def persist_progress() -> None:
        _resolve_completed_deferred_problems(output_dir, deferred_by_key, progress)
        save_json(
            progress_path,
            {
                "updated_at": utc_now(),
                "model": runtime.full_text_model,
                "protocol_version": SCREENING_PROTOCOL_VERSION,
                "input_digest": input_digest,
                "source_second_model": second_packet.get("model"),
                "source_second_input_digest": second_packet.get("input_digest"),
                "source_manifest": source_manifest,
                "source_manifest_digest": stable_json_digest(source_manifest),
                "usage": usage,
                "results_by_context": progress,
                "deferred_screening_problems": sorted(
                    deferred_by_key.values(), key=lambda item: item["unit_key"]
                ),
            },
        )
        _update_cost_report(output_dir, runtime)

    by_pmcid: dict[str, list[dict[str, Any]]] = {}
    for cid, context in contexts.items():
        if cid in progress:
            continue
        pmcid = str(context.get("pmcid") or "")
        by_pmcid.setdefault(pmcid, []).append(context)

    work_units = sorted(by_pmcid.items())

    def context_ids_for(unit: tuple[str, list[dict[str, Any]]]) -> list[str]:
        _pmcid, article_contexts = unit
        return [item["context_id"] for item in article_contexts]

    def label_for(unit: tuple[str, list[dict[str, Any]]]) -> str:
        pmcid, _article_contexts = unit
        return f"PMCID {pmcid}"

    def process_unit(unit: tuple[str, list[dict[str, Any]]]) -> None:
        pmcid, article_contexts = unit
        expected = {
            item["context_id"]: item
            for item in article_contexts
            if item["context_id"] not in progress
        }
        if not expected:
            return
        compact = [
            {
                "context_id": item["context_id"],
                "pmid": item["pmid"],
                "pmcid": item["pmcid"],
                "title": item["title"],
                "exposure": item["exposure"],
                "cancer": item["cancer"],
                "missing_key_fields": item.get("missing_key_fields") or [],
                "invalid_key_fields": item.get("invalid_key_fields") or [],
                "abstract_jbi_grade": (item.get("jbi") or {}).get("grade"),
            }
            for item in expected.values()
        ]
        prompt = (
            FULL_TEXT_INSTRUCTIONS
            + "\n\nCONTEXTS:\n"
            + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            + "\n\nFULL ARTICLE TEXT:\n"
            + sources[pmcid]["text"]
        )
        results = _validated_call(
            runtime,
            "full_text",
            FULL_TEXT_SYSTEM,
            prompt,
            usage,
            lambda payload, values=expected, text=sources[pmcid]["text"], identifier=pmcid: validate_full_text(
                payload,
                values,
                source_text=text,
                source_identifier=identifier,
            ),
            exposure_weights=_exposure_weights(expected.values()),
            on_usage=persist_progress,
        )
        for item in results:
            item["third_round_status"] = "COMPLETED"
            item["third_round_model"] = runtime.full_text_model
            item["full_text_characters_reviewed"] = len(sources[pmcid]["text"])
            item["pmc_source_sha256"] = source_manifest[pmcid]["sha256"]
            adjudication = manual_adjudications.get(item["context_id"])
            if adjudication is not None:
                item["luna_full_text_result_before_manual_adjudication"] = {
                    "final_status": item.get("final_status"),
                    "full_text_status": item.get("full_text_status"),
                    "exclusion_code": item.get("exclusion_code"),
                    "reason": item.get("reason"),
                }
                status = str(adjudication["full_text_status"])
                item.update(
                    {
                        "final_status": status,
                        "full_text_status": status,
                        "exclusion_code": adjudication["exclusion_code"],
                        "reason": adjudication["reason"],
                        "manual_full_text_adjudication": True,
                        "manual_full_text_adjudication_source": (
                            FULL_TEXT_MANUAL_ADJUDICATIONS_FILENAME
                        ),
                        "manual_full_text_adjudication_recorded_at": adjudication[
                            "recorded_at"
                        ],
                    }
                )
            progress[item["context_id"]] = item
        persist_progress()
        print(
            f"{runtime.name} full-text screen: {pmcid}; {len(progress)}/{len(contexts)} contexts",
            flush=True,
        )

    _run_units_with_problem_deferral(
        output_dir,
        runtime,
        "full_text",
        work_units,
        process_unit,
        context_ids_for,
        label_for,
        deferred_by_key,
        persist_progress,
    )

    results = sorted(progress.values(), key=lambda item: item["context_id"])
    if len(results) != len(contexts):
        raise ScreeningValidationError(f"{runtime.name} full-text screen is incomplete")
    packet = {
        "schema_version": 2,
        "created_at": utc_now(),
        "track": runtime.name,
        "model": runtime.full_text_model,
        "protocol_version": SCREENING_PROTOCOL_VERSION,
        "input_digest": input_digest,
        "source_second_model": second_packet.get("model"),
        "source_second_input_digest": second_packet.get("input_digest"),
        "source_manifest": source_manifest,
        "source_manifest_digest": stable_json_digest(source_manifest),
        "contexts": len(results),
        "status_counts": dict(Counter(item["full_text_status"] for item in results)),
        "manual_full_text_adjudications": len(manual_adjudications),
        "manual_full_text_adjudications_digest": (
            manual_full_text_adjudications_digest(manual_adjudications)
        ),
        "usage": usage,
        "results": results,
    }
    validate_full_text_packet_lineage(
        second_packet,
        packet,
        runtime.name,
        runtime.full_text_model,
        manual_adjudications=manual_adjudications,
    )
    save_json(output_path, packet)
    progress_path.unlink(missing_ok=True)
    _update_cost_report(output_dir, runtime)
    return packet


def _whole_food_subtype_review_reasons(item: dict[str, Any]) -> list[str]:
    """Return deterministic reasons that related-component evidence needs review."""
    exposure_key = str(item.get("exposure") or "").casefold()
    exposure_config = {
        "tomato": {
            "label": "Tomato",
            "component": "lycopene",
            "component_pattern": r"\blycopene\b|\blyc-o-mato\b",
            "food_pattern": r"\btomato(?:es)?\b",
        },
        "pepper (capsicum)": {
            "label": "Pepper (Capsicum)",
            "component": "capsaicin",
            "component_pattern": r"\bcapsaicin\b|\bcapsaicinoids?\b",
            "food_pattern": (
                r"\bcapsicum\b|\b(?:bell|sweet|chile|chili|chilli|hot|red|green|"
                r"cayenne|jalape(?:n|\u00f1)o) peppers?\b|\bpaprika\b|\bpimientos?\b|\bpimentos?\b"
            ),
        },
    }.get(exposure_key)
    if not exposure_config:
        return []

    extraction = item.get("extraction") or {}
    subtype = str(extraction.get("exposure_subtype") or "mixed_or_unclear")
    if subtype not in {"whole_food", "mixed_or_unclear", "NA", "unclear"}:
        return [f"model_classified_{subtype}"]

    evidence = extraction.get("evidence") or {}

    def excerpt(field: str) -> str:
        return one_line((evidence.get(field) or {}).get("excerpt"))

    comparison_text = " ".join(
        filter(None, [one_line(extraction.get("comparison_type")), excerpt("comparison")])
    ).casefold()
    measurement_text = excerpt("exposure_measurement").casefold()
    target_text = f"{comparison_text} {measurement_text}".strip()
    component_pattern = exposure_config["component_pattern"]
    food_pattern = exposure_config["food_pattern"]
    component_name = exposure_config["component"]
    whole_food_cue = bool(re.search(food_pattern, comparison_text))
    biomarker_pattern = (
        rf"\b(?:serum|plasma|blood|circulating|biospecimen)\b.{{0,80}}(?:{component_pattern})|"
        rf"(?:{component_pattern}).{{0,80}}\b(?:serum|plasma|blood|circulating|biospecimen)\b"
    )
    supplement_pattern = (
        rf"(?:{component_pattern}|{food_pattern}).{{0,50}}\b(?:supplement|capsule|tablet|dose|extract)\b|"
        rf"\b(?:supplement|capsule|tablet)\b.{{0,50}}(?:{component_pattern}|{food_pattern})"
    )
    dietary_component_pattern = (
        rf"\b(?:dietary|total)\b.{{0,30}}(?:{component_pattern})|"
        rf"(?:{component_pattern}).{{0,30}}\bintake\b"
    )

    reasons = []
    if extraction.get("exposure_measurement_type_original", extraction.get("exposure_measurement_type")) == "human_biospecimen":
        reasons.append("whole_food_conflicts_with_human_biospecimen_measurement")
    elif re.search(biomarker_pattern, comparison_text) or (
        re.search(biomarker_pattern, measurement_text) and not whole_food_cue
    ):
        reasons.append(f"whole_food_conflicts_with_{component_name}_biomarker_evidence")
    if re.search(supplement_pattern, target_text):
        reasons.append("whole_food_conflicts_with_supplement_evidence")
    if re.search(dietary_component_pattern, comparison_text) and not whole_food_cue:
        reasons.append(f"whole_food_conflicts_with_dietary_{component_name}_evidence")
    elif re.search(component_pattern, comparison_text) and not whole_food_cue:
        reasons.append(f"whole_food_conflicts_with_{component_name}_component_evidence")
    return reasons


def apply_whole_food_subtype_disposition(item: dict[str, Any]) -> dict[str, Any]:
    """Quarantine related components before whole-food pooling and reporting."""
    if item.get("final_status") != "INCLUDE_META":
        return item
    reasons = _whole_food_subtype_review_reasons(item)
    if not reasons:
        return item

    guarded = dict(item)
    guarded["screening_reason_before_subtype_guard"] = item.get("reason")
    guarded["subtype_review_reasons"] = reasons
    guarded["final_status"] = "ELIGIBLE_SUBTYPE_REVIEW"
    guarded["full_text_status"] = "ELIGIBLE_SUBTYPE_REVIEW"
    exposure_label = str(item.get("exposure") or "whole-food")
    subtype = str((item.get("extraction") or {}).get("exposure_subtype") or "mixed_or_unclear")
    if reasons == [f"model_classified_{subtype}"]:
        label = subtype.replace("_", "-")
        guarded["reason"] = one_sentence(
            f"The study is relevant {exposure_label} evidence, but its {label} exposure subtype is retained for separate review rather than pooled with the whole food"
        )
    else:
        guarded["reason"] = (
            f"The study is relevant {exposure_label} evidence, but its supporting text conflicts with the "
            "whole-food subtype and requires separate review before pooling."
        )
    return guarded


def _validate_packet_header(
    packet: dict[str, Any],
    *,
    stage: str,
    track: str,
    expected_model: str,
) -> None:
    """Reject stale, cross-provider, or model-substituted screening packets."""
    if not isinstance(packet, dict):
        raise ScreeningValidationError(f"{track} {stage} packet must be an object")
    if packet.get("schema_version") != 2:
        raise ScreeningValidationError(
            f"{track} {stage} packet has unsupported schema_version "
            f"{packet.get('schema_version')!r}"
        )
    if packet.get("track") != track:
        raise ScreeningValidationError(
            f"{track} {stage} packet has track {packet.get('track')!r}"
        )
    if packet.get("model") != expected_model:
        raise ScreeningValidationError(
            f"{track} {stage} packet uses model {packet.get('model')!r}; "
            f"expected {expected_model!r}"
        )
    if packet.get("protocol_version") != SCREENING_PROTOCOL_VERSION:
        raise ScreeningValidationError(
            f"{track} {stage} packet uses protocol_version "
            f"{packet.get('protocol_version')!r}; expected {SCREENING_PROTOCOL_VERSION}"
        )
    digest = packet.get("input_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ScreeningValidationError(f"{track} {stage} packet has an invalid input_digest")


def _packet_context_ids(
    packet: dict[str, Any],
    *,
    track: str,
    stage: str,
) -> list[str]:
    results = packet.get("results")
    if not isinstance(results, list):
        raise ScreeningValidationError(f"{track} {stage} results must be an array")
    identifiers: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            raise ScreeningValidationError(
                f"{track} {stage} contains a non-object result"
            )
        cid = str(result.get("context_id") or "")
        if not cid:
            raise ScreeningValidationError(
                f"{track} {stage} contains a result without context_id"
            )
        identifiers.append(cid)
    if len(identifiers) != len(set(identifiers)):
        raise ScreeningValidationError(f"{track} {stage} contains duplicate context_id values")
    return identifiers


def _require_exact_context_ids(
    packet: dict[str, Any],
    expected: Iterable[str],
    *,
    track: str,
    stage: str,
) -> None:
    actual_ids = _packet_context_ids(packet, track=track, stage=stage)
    expected_ids = set(expected)
    actual_set = set(actual_ids)
    if actual_set != expected_ids:
        missing = sorted(expected_ids - actual_set)
        unexpected = sorted(actual_set - expected_ids)
        raise ScreeningValidationError(
            f"{track} {stage} context lineage mismatch; "
            f"missing={missing}, unexpected={unexpected}"
        )


def _manual_full_text_adjudications_from_packet(
    packet: dict[str, Any],
    *,
    track: str,
) -> dict[str, dict[str, Any]]:
    """Recover and validate adjudication content embedded in full-text results."""
    adjudications: dict[str, dict[str, Any]] = {}
    for item in packet.get("results", []):
        if not item.get("manual_full_text_adjudication"):
            continue
        context_id = one_line(item.get("context_id"))
        status = one_line(item.get("full_text_status")).upper()
        code = one_line(item.get("exclusion_code")) or None
        recorded_at = one_line(item.get("manual_full_text_adjudication_recorded_at"))
        if not context_id or context_id in adjudications:
            raise ScreeningValidationError(
                f"{track} full_text packet has a duplicate manual adjudication"
            )
        if item.get("manual_full_text_adjudication_source") != (
            FULL_TEXT_MANUAL_ADJUDICATIONS_FILENAME
        ):
            raise ScreeningValidationError(
                f"{track} full_text manual adjudication {context_id} has an invalid source"
            )
        if not recorded_at:
            raise ScreeningValidationError(
                f"{track} full_text manual adjudication {context_id} lacks recorded_at"
            )
        if status not in {"ELIGIBLE_NOT_POOLABLE", "EXCLUDE"}:
            raise ScreeningValidationError(
                f"{track} full_text manual adjudication {context_id} has invalid status"
            )
        if item.get("final_status") != status:
            raise ScreeningValidationError(
                f"{track} full_text manual adjudication {context_id} has inconsistent status"
            )
        if status == "EXCLUDE" and code not in EXCLUSION_CODES:
            raise ScreeningValidationError(
                f"{track} full_text manual adjudication {context_id} has invalid exclusion code"
            )
        if status != "EXCLUDE" and code is not None:
            raise ScreeningValidationError(
                f"{track} full_text manual adjudication {context_id} has an exclusion code"
            )
        adjudications[context_id] = {
            "full_text_status": status,
            "exclusion_code": code,
            "reason": one_sentence(item.get("reason")),
            "recorded_at": recorded_at,
        }
    declared_count = packet.get("manual_full_text_adjudications")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != len(adjudications)
    ):
        raise ScreeningValidationError(
            f"{track} full_text manual adjudication count is inconsistent"
        )
    return adjudications


def validate_full_text_packet_lineage(
    second_packet: dict[str, Any],
    full_text_packet: dict[str, Any],
    track: str,
    expected_model: str,
    *,
    manual_adjudications: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a durable full-text packet without refetching its PMC sources."""
    _validate_packet_header(
        full_text_packet,
        stage="full_text",
        track=track,
        expected_model=expected_model,
    )
    expected_contexts = {
        str(item.get("context_id") or ""): item
        for item in second_packet.get("results", [])
        if is_full_text_candidate(item)
    }
    if "" in expected_contexts:
        raise ScreeningValidationError(
            f"{track} second packet has a full-text candidate without context_id"
        )
    _require_exact_context_ids(
        full_text_packet,
        expected_contexts,
        track=track,
        stage="full_text",
    )
    if full_text_packet.get("source_second_model") != second_packet.get("model"):
        raise ScreeningValidationError(
            f"{track} full_text packet is not linked to the current second-screen model"
        )
    if full_text_packet.get("source_second_input_digest") != second_packet.get("input_digest"):
        raise ScreeningValidationError(
            f"{track} full_text packet is not linked to the current second-screen digest"
        )

    manifest = full_text_packet.get("source_manifest")
    if not isinstance(manifest, dict):
        raise ScreeningValidationError(f"{track} full_text source_manifest must be an object")
    expected_pmcids = {
        str(item.get("pmcid") or "") for item in expected_contexts.values()
    }
    if set(manifest) != expected_pmcids:
        raise ScreeningValidationError(
            f"{track} full_text source manifest does not match the expected PMCIDs"
        )
    for pmcid, entry in manifest.items():
        if not isinstance(entry, dict):
            raise ScreeningValidationError(
                f"{track} full_text source manifest entry {pmcid!r} must be an object"
            )
        status = entry.get("status")
        characters = entry.get("characters")
        sha256 = entry.get("sha256")
        if status != "full_text":
            raise ScreeningValidationError(
                f"{track} gated PMCID {pmcid} lacks complete verified full text "
                f"(status={status!r})"
            )
        if isinstance(characters, bool) or not isinstance(characters, int) or characters < 2000:
            raise ScreeningValidationError(
                f"{track} full_text source manifest has invalid character count"
            )
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ScreeningValidationError(
                f"{track} full_text source manifest has invalid SHA-256"
            )
        if not isinstance(entry.get("error"), str):
            raise ScreeningValidationError(
                f"{track} full_text source manifest has invalid error field"
            )
        if entry.get("error"):
            raise ScreeningValidationError(
                f"{track} complete PMC source {pmcid} cannot retain a retrieval error"
            )
    if full_text_packet.get("source_manifest_digest") != stable_json_digest(manifest):
        raise ScreeningValidationError(
            f"{track} full_text source-manifest digest is stale"
        )

    packet_adjudications = _manual_full_text_adjudications_from_packet(
        full_text_packet,
        track=track,
    )
    packet_manual_digest = manual_full_text_adjudications_digest(packet_adjudications)
    if manual_adjudications is not None and (
        manual_full_text_adjudications_digest(manual_adjudications)
        != packet_manual_digest
    ):
        raise ScreeningValidationError(
            f"{track} full_text packet does not match the current manual adjudications"
        )
    digest_field_present = "manual_full_text_adjudications_digest" in full_text_packet
    if not digest_field_present:
        raise ScreeningValidationError(
            f"{track} full_text packet lacks a manual-adjudication digest"
        )
    stored_manual_digest = full_text_packet.get(
        "manual_full_text_adjudications_digest"
    )
    if stored_manual_digest is not None and (
        not isinstance(stored_manual_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", stored_manual_digest)
    ):
        raise ScreeningValidationError(
            f"{track} full_text packet has an invalid manual-adjudication digest"
        )
    if stored_manual_digest != packet_manual_digest:
        raise ScreeningValidationError(
            f"{track} full_text packet manual-adjudication digest is inconsistent"
        )
    expected_digest = full_text_input_digest(
        second_packet,
        expected_model,
        manifest,
        packet_adjudications,
    )
    if full_text_packet.get("input_digest") != expected_digest:
        raise ScreeningValidationError(
            f"{track} full_text input_digest does not match its second-screen/source lineage"
        )
    if full_text_packet.get("contexts") != len(expected_contexts):
        raise ScreeningValidationError(f"{track} full_text context count is inconsistent")
    usage = full_text_packet.get("usage")
    if expected_contexts and (
        not isinstance(usage, dict)
        or usage.get("model") != expected_model
        or isinstance(usage.get("calls"), bool)
        or not isinstance(usage.get("calls"), int)
        or usage.get("calls", 0) < len(expected_pmcids)
    ):
        raise ScreeningValidationError(
            f"{track} full_text packet lacks an actual {expected_model} call "
            "for every gated PMCID"
        )
    for item in full_text_packet.get("results", []):
        pmcid = str(item.get("pmcid") or "")
        expected_context = expected_contexts[item["context_id"]]
        if pmcid != str(expected_context.get("pmcid") or ""):
            raise ScreeningValidationError(
                f"{track} full_text result {item['context_id']} changed its gated PMCID"
            )
        source = manifest[pmcid]
        if item.get("pmc_source_status") != source["status"]:
            raise ScreeningValidationError(
                f"{track} full_text result {item['context_id']} has inconsistent source status"
            )
        if item.get("pmc_source_sha256") != source["sha256"]:
            raise ScreeningValidationError(
                f"{track} full_text result {item['context_id']} has inconsistent source digest"
            )
        if source["status"] == "full_text" and item.get(
            "full_text_characters_reviewed"
        ) != source["characters"]:
            raise ScreeningValidationError(
                f"{track} full_text result {item['context_id']} has inconsistent source length"
            )
        if (
            item.get("third_round_status") != "COMPLETED"
            or item.get("third_round_model") != expected_model
        ):
            raise ScreeningValidationError(
                f"{track} full_text result {item['context_id']} lacks a completed "
                f"{expected_model} third-round result"
            )
    return {
        "model": full_text_packet["model"],
        "input_digest": full_text_packet["input_digest"],
    }


def validate_track_lineage(
    candidates: dict[str, Any],
    first_packet: dict[str, Any],
    second_packet: dict[str, Any],
    full_text_packet: dict[str, Any],
    track: str,
    expected_models: dict[str, str],
) -> dict[str, Any]:
    """Validate candidate -> first -> second -> full-text lineage before finalization."""
    if set(expected_models) != {"first", "second", "full_text"} or not all(
        isinstance(value, str) and value for value in expected_models.values()
    ):
        raise ScreeningValidationError(f"{track} expected model configuration is incomplete")

    _validate_packet_header(
        first_packet,
        stage="first",
        track=track,
        expected_model=expected_models["first"],
    )
    expected = expected_contexts(candidates)
    _require_exact_context_ids(
        first_packet,
        expected,
        track=track,
        stage="first",
    )
    for item in first_packet.get("results", []):
        if item.get("decision") not in {"PASS", "FAIL"}:
            raise ScreeningValidationError(
                f"{track} first result {item['context_id']} has an invalid decision"
            )
    expected_first_digest = first_input_digest(candidates, expected_models["first"])
    if first_packet.get("input_digest") != expected_first_digest:
        raise ScreeningValidationError(
            f"{track} first input_digest does not match candidates.json"
        )

    _validate_packet_header(
        second_packet,
        stage="second",
        track=track,
        expected_model=expected_models["second"],
    )
    passing = {
        item["context_id"]
        for item in first_packet.get("results", [])
        if item.get("decision") == "PASS"
    }
    _require_exact_context_ids(
        second_packet,
        passing,
        track=track,
        stage="second",
    )
    for item in second_packet.get("results", []):
        if item.get("eligibility") not in {"INCLUDE", "EXCLUDE"}:
            raise ScreeningValidationError(
                f"{track} second result {item['context_id']} has invalid eligibility"
            )
    if second_packet.get("source_first_model") != first_packet.get("model"):
        raise ScreeningValidationError(
            f"{track} second packet is not linked to the current first-screen model"
        )
    if second_packet.get("source_first_input_digest") != first_packet.get("input_digest"):
        raise ScreeningValidationError(
            f"{track} second packet is not linked to the current first-screen digest"
        )
    expected_second_digest = second_input_digest(
        candidates,
        first_packet,
        expected_models["second"],
    )
    if second_packet.get("input_digest") != expected_second_digest:
        raise ScreeningValidationError(
            f"{track} second input_digest does not match candidate/first-screen lineage"
        )

    validate_full_text_packet_lineage(
        second_packet,
        full_text_packet,
        track,
        expected_models["full_text"],
    )
    return {
        "protocol_version": SCREENING_PROTOCOL_VERSION,
        "screening_models": {
            "first": first_packet["model"],
            "second": second_packet["model"],
            "full_text": full_text_packet["model"],
        },
        "screening_input_digests": {
            "first": first_packet["input_digest"],
            "second": second_packet["input_digest"],
            "full_text": full_text_packet["input_digest"],
        },
    }


def finalize_track(
    candidates: dict[str, Any],
    first_packet: dict[str, Any],
    second_packet: dict[str, Any],
    full_text_packet: dict[str, Any],
    track: str,
) -> dict[str, Any]:
    expected = expected_contexts(candidates)
    first = {item["context_id"]: item for item in first_packet.get("results", [])}
    second = {item["context_id"]: item for item in second_packet.get("results", [])}
    full_text = {item["context_id"]: item for item in full_text_packet.get("results", [])}
    articles = _article_lookup(candidates)
    results = []
    for cid, base in expected.items():
        first_item = first.get(cid)
        if not first_item:
            raise ScreeningValidationError(f"{track} finalization lacks first-screen context {cid}")
        if first_item["decision"] == "FAIL":
            final = {
                **{
                    key: value
                    for key, value in base.items()
                    if key != "protocol_exclusion" and not key.startswith("_")
                },
                "final_status": "EXCLUDE",
                "full_text_status": "EXCLUDE",
                "screen_layer": "first",
                "exclusion_code": "S1-NOT-DIRECT",
                "reason": one_sentence(first_item["reason"]),
                "extraction": None,
                "jbi": None,
            }
        else:
            second_item = second.get(cid)
            if not second_item:
                raise ScreeningValidationError(f"{track} finalization lacks second-screen context {cid}")
            if second_item["eligibility"] == "EXCLUDE":
                final = {
                    **second_item,
                    "final_status": "EXCLUDE",
                    "full_text_status": "EXCLUDE",
                    "screen_layer": "second",
                }
            elif cid in full_text:
                final = {**full_text[cid], "screen_layer": "full_text"}
                if isinstance(final.get("extraction"), dict):
                    final["extraction"] = dict(final["extraction"])
                    apply_poolability_policy(final["extraction"])
                    final["missing_key_fields"] = missing_key_fields(
                        final["extraction"]
                    )
                    final["invalid_key_fields"] = invalid_key_fields(
                        final["extraction"]
                    )
                    if final.get("final_status") != "EXCLUDE":
                        poolable = is_poolable(final["extraction"], final.get("jbi"))
                        final_status = (
                            "INCLUDE_META" if poolable else "ELIGIBLE_NOT_POOLABLE"
                        )
                        if final_status != final.get("final_status"):
                            final["reason"] = (
                                "The extraction meets the current sample-size, effect, "
                                "and uncertainty requirements with Moderate or Good JBI."
                                if poolable
                                else final.get("reason")
                            )
                        final["final_status"] = final_status
                        final["full_text_status"] = final_status
            else:
                final_extraction = (
                    dict(second_item["extraction"])
                    if isinstance(second_item.get("extraction"), dict)
                    else second_item.get("extraction")
                )
                apply_poolability_policy(final_extraction)
                poolable = is_poolable(final_extraction, second_item.get("jbi"))
                final_status = "INCLUDE_META" if poolable else "ELIGIBLE_NOT_POOLABLE"
                reason = (
                    "The title, abstract, and MeSH extraction contains complete Moderate-or-Good evidence for pooling."
                    if poolable
                    else "The study is relevant, but required evidence remains incomplete or its JBI grade is Fair."
                )
                final = {
                    **second_item,
                    "final_status": final_status,
                    "full_text_status": final_status,
                    "screen_layer": "second",
                    "reason": reason,
                    "missing_key_fields": missing_key_fields(final_extraction),
                    "invalid_key_fields": invalid_key_fields(final_extraction),
                    "extraction": final_extraction,
                }
        protocol_block = protocol_exclusion(articles.get(str(base.get("pmid") or ""), base))
        if protocol_block:
            code, reason = protocol_block
            final.update(
                {
                    "final_status": "EXCLUDE",
                    "full_text_status": "EXCLUDE",
                    "screen_layer": "deterministic_protocol",
                    "exclusion_code": code,
                    "reason": reason,
                    "extraction": None,
                    "jbi": None,
                }
            )
        final = apply_whole_food_subtype_disposition(final)
        final = dietary_policy.apply_record(final)
        final["track"] = track
        results.append(final)
    results.sort(key=lambda item: item["context_id"])
    moderate_good_missing = [
        item
        for item in results
        if item.get("final_status") == "ELIGIBLE_NOT_POOLABLE"
        and (item.get("jbi") or {}).get("grade") in {"Moderate", "Good"}
        and missing_key_fields(item.get("extraction"))
    ]
    missing_field_contexts = Counter(
        field
        for item in moderate_good_missing
        for field in missing_key_fields(item.get("extraction"))
    )
    missing_field_pmids = {
        field: sorted(
            {
                str(item.get("pmid"))
                for item in moderate_good_missing
                if field in missing_key_fields(item.get("extraction"))
            }
        )
        for field in KEY_EVIDENCE_FIELDS
    }
    return {
        "schema_version": 2,
        "created_at": utc_now(),
        "track": track,
        "publication_date_start": candidates.get("publication_date_start"),
        "poolability_policy_version": POOLABILITY_POLICY_VERSION,
        "publication_date_end": candidates.get("publication_date_end"),
        "counts": dict(Counter(item["final_status"] for item in results)),
        "wald_confidence_intervals_computed": sum(
            (item.get("extraction") or {}).get("ci_method")
            == WALD_CONFIDENCE_INTERVAL_METHOD
            for item in results
            if item.get("final_status") != "EXCLUDE"
        ),
        "wald_confidence_interval_method": WALD_CONFIDENCE_INTERVAL_METHOD,
        "wald_p_values_computed": sum(
            (item.get("extraction") or {}).get("p_value_method")
            == WALD_P_VALUE_METHOD
            for item in results
            if item.get("final_status") != "EXCLUDE"
        ),
        "wald_p_value_method": WALD_P_VALUE_METHOD,
        "moderate_good_missing_key_evidence": {
            "contexts": len(moderate_good_missing),
            "unique_pmids": len(
                {str(item.get("pmid")) for item in moderate_good_missing}
            ),
            "by_field_contexts": {
                field: missing_field_contexts.get(field, 0)
                for field in KEY_EVIDENCE_FIELDS
            },
            "by_field_unique_pmids": {
                field: len(missing_field_pmids[field])
                for field in KEY_EVIDENCE_FIELDS
            },
        },
        "results": results,
    }


def prepare_full_text_sources(
    output_dir: Path,
    second_packets: Iterable[dict[str, Any]],
    fetcher: Callable[[str], bytes],
    parser: Callable[[bytes], str],
    force: bool = False,
    prevalidated_sources: dict[str, dict[str, Any]] | None = None,
    required_prevalidated_pmcids: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Fetch each requested PMC article once; failures become auditable records."""
    pmcids = {
        str(item.get("pmcid"))
        for packet in second_packets
        for item in packet.get("results", [])
        if is_full_text_candidate(item)
    }
    sources: dict[str, dict[str, Any]] = {
        "": {"status": "no_pmcid", "text": "", "error": ""}
    }
    full_text_dir = output_dir / "full_text"
    full_text_dir.mkdir(parents=True, exist_ok=True)
    requests = 0
    trusted = prevalidated_sources or {}
    required_trusted = {str(value) for value in (required_prevalidated_pmcids or set())}
    if not required_trusted.issubset(pmcids):
        raise ScreeningValidationError(
            "Required prevalidated PMC scope is not an exact subset of gated sources"
        )
    for pmcid in sorted(pmcids):
        text_path = full_text_dir / f"{pmcid}.txt"
        if pmcid in required_trusted and pmcid not in trusted:
            raise ScreeningValidationError(
                f"Sealed PMC repair source is required before generic retrieval: {pmcid}"
            )
        try:
            prevalidated = trusted.get(pmcid)
            if prevalidated is not None:
                text = str(prevalidated.get("text") or "").rstrip("\n")
                if len(text) < 2000:
                    raise ScreeningValidationError(
                        f"Prevalidated PMC source is not substantive: {pmcid}"
                    )
                required_provenance = (
                    "source_format",
                    "source_route",
                    "raw_sha256",
                    "validation_digest",
                    "receipt_ledger_sha256",
                )
                if any(
                    not one_line(prevalidated.get(field) or "")
                    for field in required_provenance
                ):
                    raise ScreeningValidationError(
                        f"Prevalidated PMC source provenance is incomplete: {pmcid}"
                    )
                source_format = one_line(prevalidated["source_format"])
                source_route = one_line(prevalidated["source_route"])
                allowed_route_formats = {
                    "efetch": "jats_xml",
                    "oai": "jats_xml",
                    "s3": "jats_xml",
                    "html": "pmc_html",
                    "pdf": "pmc_pdf",
                }
                if allowed_route_formats.get(source_route) != source_format:
                    raise ScreeningValidationError(
                        f"Prevalidated PMC source route/format is invalid: {pmcid}"
                    )
                for field in (
                    "raw_sha256",
                    "validation_digest",
                    "receipt_ledger_sha256",
                ):
                    if not re.fullmatch(
                        r"[0-9a-f]{64}", one_line(prevalidated[field])
                    ):
                        raise ScreeningValidationError(
                            f"Prevalidated PMC source {field} is invalid: {pmcid}"
                        )
                status = "full_text"
                sources[pmcid] = {
                    "status": status,
                    "text": text,
                    "error": "",
                    "prevalidated": True,
                    **{
                        field: one_line(prevalidated[field])
                        for field in required_provenance
                    },
                }
                print(
                    f"PMC source {pmcid}: {status} "
                    f"({len(text):,} characters; prevalidated official receipt)",
                    flush=True,
                )
                continue
            if text_path.exists() and not force:
                text = text_path.read_text(encoding="utf-8").rstrip("\n")
            else:
                requests += 1
                text = parser(fetcher(pmcid)).rstrip("\n")
                if len(text) >= 2000:
                    text_path.write_text(text + "\n", encoding="utf-8")
            status = "full_text" if len(text) >= 2000 else "partial_record"
            sources[pmcid] = {
                "status": status,
                "text": text,
                "error": "",
                "prevalidated": False,
            }
        except ScreeningValidationError:
            raise
        except Exception as exc:  # network/XML errors must not destroy the monthly run
            sources[pmcid] = {
                "status": "fetch_failed",
                "text": "",
                "error": f"{type(exc).__name__}: {one_line(exc)}"[:500],
            }
        print(
            f"PMC source {pmcid}: {sources[pmcid]['status']} "
            f"({len(sources[pmcid]['text']):,} characters)",
            flush=True,
        )
    source_manifest = full_text_source_manifest(sources, pmcids)
    save_json(
        output_dir / "pmc_source_status.json",
        {
            "schema_version": 1,
            "created_at": utc_now(),
            "ncbi_requests": requests,
            "source_manifest": source_manifest,
            "source_manifest_digest": stable_json_digest(source_manifest),
            "sources": {
                key: {field: value for field, value in source.items() if field != "text"}
                for key, source in sources.items()
                if key
            },
        },
    )
    return sources, requests


def _comparison_view(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    extraction = item.get("extraction") or {}
    jbi = item.get("jbi") or {}
    view = {
        "final_status": item.get("final_status"),
        "full_text_status": item.get("full_text_status"),
        "screen_layer": item.get("screen_layer"),
        "pmc_source_status": item.get("pmc_source_status"),
        "missing_key_fields": item.get("missing_key_fields") or [],
        "invalid_key_fields": item.get("invalid_key_fields") or [],
        "subtype_review_reasons": item.get("subtype_review_reasons") or [],
        "exclusion_code": item.get("exclusion_code"),
        "reason": item.get("reason"),
        "jbi_grade": jbi.get("grade"),
        "jbi_score_percent": jbi.get("score_percent"),
        "jbi_checklist_type": jbi.get("checklist_type"),
        "jbi_answers": jbi.get("answers") or {},
        "extraction_evidence": extraction.get("evidence") or {},
        "ethnicity": extraction.get("ethnicity"),
        "ethnicity_basis": extraction.get("ethnicity_basis"),
    }
    view.update({field: extraction.get(field) for field in EXTRACTION_FIELDS})
    return view


def compare_tracks(
    chatgpt_packet: dict[str, Any],
    deepseek_packet: dict[str, Any],
) -> dict[str, Any]:
    chatgpt = {item["context_id"]: item for item in chatgpt_packet.get("results", [])}
    deepseek = {item["context_id"]: item for item in deepseek_packet.get("results", [])}
    records = []
    for cid in sorted(set(chatgpt) | set(deepseek)):
        gpt_view = _comparison_view(chatgpt.get(cid))
        deepseek_view = _comparison_view(deepseek.get(cid))
        fields = sorted(
            key
            for key in set(gpt_view or {}) | set(deepseek_view or {})
            if (gpt_view or {}).get(key) != (deepseek_view or {}).get(key)
        )
        if not fields:
            continue
        source = chatgpt.get(cid) or deepseek.get(cid) or {}
        records.append(
            {
                "context_id": cid,
                "pmid": source.get("pmid") or cid.split("|", 1)[0],
                "exposure": source.get("exposure"),
                "cancer": source.get("cancer"),
                "different_fields": fields,
                "chatgpt": gpt_view,
                "deepseek": deepseek_view,
                "canonical_resolution": "chatgpt",
            }
        )
    return {
        "schema_version": 1,
        "created_at": utc_now(),
        "canonical_track": "chatgpt",
        "compared_contexts": len(set(chatgpt) | set(deepseek)),
        "track_final_status_counts": {
            "chatgpt": dict(
                Counter(item.get("final_status") or "MISSING" for item in chatgpt.values())
            ),
            "deepseek": dict(
                Counter(item.get("final_status") or "MISSING" for item in deepseek.values())
            ),
        },
        "discrepancy_count": len(records),
        "records": records,
    }


def pending_validation_comparison(chatgpt_packet: dict[str, Any]) -> dict[str, Any]:
    """Record that comparison is deferred, rather than calling absence agreement."""
    chatgpt = {
        item["context_id"]: item for item in chatgpt_packet.get("results", [])
    }
    return {
        "schema_version": 1,
        "created_at": utc_now(),
        "canonical_track": "chatgpt",
        "validation_track": "deepseek",
        "validation_status": DEEPSEEK_VALIDATION_STATUS,
        "validation_reason": DEEPSEEK_VALIDATION_REASON,
        "compared_contexts": 0,
        "pending_contexts": len(chatgpt),
        "track_final_status_counts": {
            "chatgpt": dict(
                Counter(
                    item.get("final_status") or "MISSING"
                    for item in chatgpt.values()
                )
            ),
            "deepseek": {},
        },
        "discrepancy_count": 0,
        "records": [],
    }


def write_discrepancies(
    output_dir: Path,
    report_dir: Path,
    chatgpt_packet: dict[str, Any],
    deepseek_packet: dict[str, Any] | None,
) -> dict[str, Any]:
    packet = (
        compare_tracks(chatgpt_packet, deepseek_packet)
        if deepseek_packet is not None
        else pending_validation_comparison(chatgpt_packet)
    )
    save_json(output_dir / "discrepancies.json", packet)
    report_dir.mkdir(parents=True, exist_ok=True)
    if deepseek_packet is None:
        lines = [
            "MetaVir DeepSeek validation status",
            "===================================",
            "Canonical web/cache result: ChatGPT",
            f"Validation status: {packet['validation_status']}",
            f"Pending ChatGPT contexts: {packet['pending_contexts']}",
            f"Reason: {packet['validation_reason']}",
            "No ChatGPT–DeepSeek comparison was performed and no agreement is implied.",
        ]
        (report_dir / "modelDiscrepancies.txt").write_text(
            "\n".join(lines).rstrip() + "\n", encoding="utf-8"
        )
        return packet

    lines = [
        "MetaVir ChatGPT–DeepSeek screening discrepancies",
        "=================================================",
        f"Compared contexts: {packet['compared_contexts']}",
        f"Contexts with discrepancies: {packet['discrepancy_count']}",
        "ChatGPT final-status counts: "
        + json.dumps(packet["track_final_status_counts"]["chatgpt"], sort_keys=True),
        "DeepSeek final-status counts: "
        + json.dumps(packet["track_final_status_counts"]["deepseek"], sort_keys=True),
        "Canonical web/cache result: ChatGPT",
        "",
    ]
    if not packet["records"]:
        lines.append("No discrepancies were detected.")
    for record in packet["records"]:
        lines.extend(
            [
                f"PMID {record['pmid']} | {record['exposure']} | {record['cancer']}",
                f"Different fields: {', '.join(record['different_fields'])}",
                "ChatGPT: " + json.dumps(record["chatgpt"], ensure_ascii=False, sort_keys=True),
                "DeepSeek: " + json.dumps(record["deepseek"], ensure_ascii=False, sort_keys=True),
                "Resolution: ChatGPT is canonical for the web platform and saved caches.",
                "",
            ]
        )
    (report_dir / "modelDiscrepancies.txt").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )
    return packet


def write_canonical_exclusions(output_dir: Path, final_packet: dict[str, Any]) -> dict[str, Any]:
    records = [
        {
            "context_id": item["context_id"],
            "pmid": str(item.get("pmid") or item["context_id"].split("|", 1)[0]),
            "pmcid": item.get("pmcid") or "",
            "exposure": item.get("exposure"),
            "cancer": item.get("cancer"),
            "published_date": item.get("published_date") or "Unknown",
            "screen_layer": item.get("screen_layer"),
            "exclusion_code": item.get("exclusion_code"),
            "reason": one_sentence(item.get("reason")),
        }
        for item in final_packet.get("results", [])
        if item.get("final_status") == "EXCLUDE"
    ]
    packet = {
        "schema_version": 2,
        "created_at": utc_now(),
        "canonical_track": "chatgpt",
        "count": len(records),
        "records": records,
    }
    save_json(output_dir / "exclusions.json", packet)
    return packet


SECOND_INSTRUCTIONS += relaxed_policy.POLICY_INSTRUCTIONS
FULL_TEXT_INSTRUCTIONS += relaxed_policy.POLICY_INSTRUCTIONS


# Deterministic saved-result classification also applies to future model workflows.
SECOND_INSTRUCTIONS += "\n" + dietary_policy.PROMPT
FULL_TEXT_INSTRUCTIONS += "\n" + dietary_policy.PROMPT
