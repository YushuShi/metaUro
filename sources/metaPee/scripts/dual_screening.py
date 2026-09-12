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
    "total_n",
    "cases",
    "comparison_type",
    "design",
    "timing",
    "continent",
    "country",
    "population",
    "gender",
    "smoking_status",
    "exposure_subtype",
    "exposure_measurement_type",
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
    "exposure_measurement",
    "gender",
    "smoking_status",
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
SCREENING_PROTOCOL_VERSION = 7
WALD_RATIO_EFFECT_TYPES = frozenset({"OR", "RR", "IRR", "HR"})
WALD_95_Z = NormalDist().inv_cdf(0.975)
WALD_CONFIDENCE_INTERVAL_METHOD = (
    "95% Wald CI from effect and two-sided p value on the log scale"
)
WALD_P_VALUE_METHOD = "two-sided Wald test from effect and 95% CI on the log scale"
LONG_CONTEXT_INPUT_TOKEN_THRESHOLD = 272_000
FULL_TEXT_MANUAL_ADJUDICATIONS_FILENAME = "full_text_manual_adjudications.json"
RECLASSIFICATION_ROLE_INSTRUCTIONS = """
When a context supplies reclassification_roles, those roles and its
classification_instruction are binding. Judge the source and every approved
target independently; never infer a direct parent/family exposure from an atomic
name alone. In particular, DHA/EPA-only wording can support the corresponding
atomic exposure but is not direct Fish oil or direct Omega-3 evidence. Accepted
atomic evidence reaches its parent only through the post-screen hierarchy roll-up.
"""


FIRST_SYSTEM = (
    "You are the sensitive first screening layer for a systematic review of "
    "nutritional exposures and incident urinary-system cancers. Return only the "
    "requested JSON."
)
FIRST_INSTRUCTIONS = """
Judge every context using ONLY the supplied article title, abstract, and MeSH
terms. PASS when the requested nutritional exposure and the requested incident
urinary-system cancer are plausibly connected. A null result or missing quantitative detail is
not a reason to fail. When uncertain, PASS to the detailed screen.

FAIL only for a clear lexical false match, incidental exposure, wrong cancer,
or treatment/survival/prognosis/recurrence/mechanism without incident-cancer
risk. Potential Mendelian-randomization and genotype-interaction reports must
PASS here so the detailed screen can assign their explicit protocol code.

Return one result per context_id. Each reason must be one complete sentence.
"""

SECOND_SYSTEM = (
    "You are the detailed abstract screening, extraction, and JBI layer for an "
    "incident urinary-system-cancer systematic review. Return only the requested JSON."
)
SECOND_INSTRUCTIONS = """
Re-read ONLY the supplied title, abstract, and MeSH terms. Include original human
studies of the requested nutritional exposure and requested incident urinary-system
cancer. Judge each supplied cancer context separately.
Apply exactly one of these codes to every exclusion:

S2-REVIEW: systematic, scoping, narrative, umbrella, or meta-analysis review.
S2-NONPRIMARY: editorial, commentary, guideline, protocol, non-comparative case
report/series, correction without original results, or retracted publication.
S2-ANIMAL: exclusively animal, cell-line, organoid, xenograft, or preclinical.
S2-EXPOSURE-MISMATCH: requested exposure is absent, incidental, another concept,
or a medical treatment instead of the nutritional exposure.
S2-COMPOUND: target is inseparable from an index, mixture, multi-nutrient
intervention, or combined exposure with no target-specific estimate.
S2-CANCER-MISMATCH: requested urinary-cancer incidence results are absent or inseparable.
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
or biomarker discrimination without evidence about developing the requested cancer.
S2-NO-TARGET-ESTIMATE: target incidence result cannot be separated.
S2-DUPLICATE: supplied metadata establishes a duplicate/overlapping analysis.

Do not exclude because an effect, CI, p value, sample size, or case count is
missing. For included contexts, extract only explicit evidence. Classify
exposure_subtype as whole_food, dietary_component, human_biomarker, supplement,
or mixed_or_unclear; in particular, do not treat tomato food intake, dietary
lycopene, circulating lycopene, and lycopene supplements as interchangeable.
Keep the effect type and effect value exactly as reported by the paper; do not
convert an OR or HR to an RR during extraction. Record gender as exactly Male,
Female, Mixed, or NA; use NA when the abstract does not establish it. Record
smoking status when stated and use null when unavailable. If the article reports
different estimates by gender, or reports both dietary-intake and human-
biospecimen exposure estimates, put the first estimate in extraction and every
other unique gender-by-measurement estimate in additional_extractions. Use the
most fully adjusted target estimate within each such stratum. Return an empty
additional_extractions array when the article has no distinct extra stratum.

Numeric evidence may be written in words (for example, "sixty percent"), and a
total sample can require combining counts stated in separate sentences. Treat a
typographic dash in a range as a separator, never as a minus sign. Supporting
excerpts are audit aids: if no excerpt matches a key extracted value, retain the
extracted value as authoritative and use an empty/NA excerpt temporarily rather
than discarding the value. Leave a missing CI or p value null; code applies the
pre-specified two-sided Wald derivation after extraction. Report a p value only
when it is the exact two-sided p value for the same target effect, comparison,
model, outcome, and stratum. Do not put a trend, global, interaction,
heterogeneity, one-sided, or inequality-bounded p value in the target p_value
field.

Provide a field-specific verbatim excerpt (short, never an invented paraphrase), source
(abstract), PubMed URL, and location for every non-null value. Use null when
absent. Report country and population only when stated.

Appraise JBI with cohort q1-q11, case_control q1-q10, cross_sectional q1-q8, or
rct q1-q13. Use Yes, No, Unclear, or NA; unreported methods are Unclear. Supply
q1-q13 and mark questions beyond the selected checklist NA.

Full-text escalation is determined in code after the Wald checkpoint. It occurs
only for an included context with a PMCID when JBI is Fair, required quantitative
evidence is missing or invalid, or exposure classification remains ambiguous.
Each reason must be one sentence.
"""

FULL_TEXT_SYSTEM = (
    "You are a full-text evidence extractor and JBI appraiser for an incident "
    "urinary-system-cancer systematic review. Return only the requested JSON."
)
FULL_TEXT_INSTRUCTIONS = """
Use ONLY the supplied PMC full text. Re-evaluate each context, recover missing
sample size/case/effect/CI information, and reperform JBI. Exclude only when the
full text establishes one of the detailed-screen S2 codes. Mendelian
randomization and genotype-interaction papers use their explicit exclusion codes.

For every value recovered or revised here, provide a short field-specific
original-article excerpt, source full_text, the matching PMC URL, and a
section/table/paragraph location. Return null for unchanged abstract values;
the code merges them from the prior extraction. Prefer the most fully adjusted
target-context estimate; never invent values. Preserve separate gender and
dietary-intake versus human-biospecimen records in the same order as the abstract
screen, adding newly found strata to additional_extractions. Record gender as
Male, Female, Mixed, or NA and record smoking status when available. Keep the paper's original effect
type/value without OR/HR-to-RR conversion. Numeric wording, inferred totals,
range dashes, and temporarily unmatched excerpts follow the detailed-screen
rules above. Supply q1-q13, with
non-applicable questions marked NA. Each reason must be one sentence. The code
will determine poolability: complete key evidence plus Moderate or Good JBI is
poolable; Fair evidence is not.
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


def _p_value_relation(value: Any) -> str | None:
    """Preserve a reported p-value comparator after numeric normalization."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return None
    text = str(value).strip().replace("≤", "<=").replace("≥", ">=")
    match = re.search(r"([<>]=?)", text)
    return match.group(1) if match else None


def _wald_p_value_input(value: Any) -> float | None:
    """Return an exact numeric p value; inequalities are never invertible."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        p_value = float(value)
    else:
        text = str(value).strip().lower().replace("−", "-")
        if any(operator in text for operator in ("<", ">", "≤", "≥")):
            return None
        match = re.search(r"(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", text)
        if not match:
            return None
        p_value = float(match.group(0))
    return p_value if math.isfinite(p_value) and 0 < p_value <= 1 else None


def _p_value_supports_exact_two_sided(extraction: dict[str, Any]) -> bool:
    """Reject p values that cannot identify the target coefficient's Wald SE."""
    if extraction.get("p_value_relation"):
        return False
    evidence = ((extraction.get("evidence") or {}).get("p_value") or {})
    excerpt = one_line(evidence.get("excerpt")).casefold()
    if any(operator in excerpt for operator in ("<", ">", "≤", "≥")):
        return False
    non_target = re.compile(
        r"\b(?:p\s*(?:for|-)?\s*trend|trend\s+p|interaction|heterogeneity|"
        r"global\s+(?:test|p)|overall\s+(?:test|p)|one[- ]sided|one[- ]tailed)\b"
    )
    return not non_target.search(excerpt)


def _ci_supports_two_sided_wald(extraction: dict[str, Any]) -> bool:
    """Require a conventional, approximately log-symmetric 95% ratio CI."""
    evidence = ((extraction.get("evidence") or {}).get("confidence_interval") or {})
    excerpt = one_line(evidence.get("excerpt")).casefold()
    stated_levels = {
        float(value)
        for value in re.findall(
            r"\b(\d+(?:\.\d+)?)\s*%\s*(?:confidence\s+interval|ci)\b",
            excerpt,
        )
    }
    if stated_levels and stated_levels != {95.0}:
        return False
    if re.search(
        r"\b(?:profile[- ]likelihood|bootstrap(?:ped)?|credible|bayesian|exact)\b",
        excerpt,
    ):
        return False
    try:
        effect = float(extraction.get("effect_size"))
        lower = float(extraction.get("ci_lower"))
        upper = float(extraction.get("ci_upper"))
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(value) and value > 0 for value in (effect, lower, upper)):
        return False
    if not lower < upper or not lower <= effect <= upper:
        return False
    log_midpoint = (math.log(lower) + math.log(upper)) / 2.0
    log_half_width = (math.log(upper) - math.log(lower)) / 2.0
    return abs(math.log(effect) - log_midpoint) <= max(
        0.15 * log_half_width,
        0.01,
    )


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
    return math.exp(log_effect - margin), math.exp(log_effect + margin)


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


def add_wald_derivations(extraction):
    if not isinstance(extraction, dict):
        return False, False
    before_ci = extraction.get("ci_lower") is None and extraction.get("ci_upper") is None
    before_p = extraction.get("p_value") is None
    relaxed_policy.complete(extraction)
    return before_ci and extraction.get("ci_lower") is not None, before_p and extraction.get("p_value") is not None



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
        }
    return manifest


def full_text_input_digest(
    second_packet: dict[str, Any],
    model: str,
    source_manifest: dict[str, dict[str, Any]],
) -> str:
    """Bind a full-text screen to its second screen and hashed PMC inputs."""
    return screening_input_digest(
        "full_text",
        model,
        {
            "second_model": second_packet.get("model"),
            "second_input_digest": second_packet.get("input_digest"),
            "second_results": second_packet.get("results", []),
            "source_manifest": source_manifest,
        },
    )


def empty_usage(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "calls": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "long_context_calls": 0,
        "long_context_input_tokens": 0,
        "long_context_cached_input_tokens": 0,
        "long_context_output_tokens": 0,
        "long_context_reasoning_tokens": 0,
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
    long_context = input_tokens > LONG_CONTEXT_INPUT_TOKEN_THRESHOLD
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
        if long_context:
            usage["long_context_calls"] = int(usage.get("long_context_calls") or 0) + 1
            usage["long_context_input_tokens"] = int(
                usage.get("long_context_input_tokens") or 0
            ) + input_tokens
            usage["long_context_cached_input_tokens"] = int(
                usage.get("long_context_cached_input_tokens") or 0
            ) + cached_input
            usage["long_context_output_tokens"] = int(
                usage.get("long_context_output_tokens") or 0
            ) + output_tokens
            usage["long_context_reasoning_tokens"] = int(
                usage.get("long_context_reasoning_tokens") or 0
            ) + reasoning
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
                    "long_context_calls": 0,
                    "long_context_input_tokens": 0,
                    "long_context_cached_input_tokens": 0,
                    "long_context_output_tokens": 0,
                    "long_context_reasoning_tokens": 0,
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
            if long_context:
                entry["long_context_calls"] = int(
                    entry.get("long_context_calls") or 0
                ) + 1
                entry["long_context_input_tokens"] = int(
                    entry.get("long_context_input_tokens") or 0
                ) + cached_allocated.get(exposure, 0) + uncached_allocated.get(
                    exposure, 0
                )
                entry["long_context_cached_input_tokens"] = int(
                    entry.get("long_context_cached_input_tokens") or 0
                ) + cached_allocated.get(exposure, 0)
                entry["long_context_output_tokens"] = int(
                    entry.get("long_context_output_tokens") or 0
                ) + output_allocated.get(exposure, 0)
                entry["long_context_reasoning_tokens"] = int(
                    entry.get("long_context_reasoning_tokens") or 0
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
        "total_n": _nullable("number"),
        "cases": _nullable("number"),
        "comparison_type": _nullable("string"),
        "design": _nullable("string"),
        "timing": _nullable("string"),
        "continent": _nullable("string"),
        "country": _nullable("string"),
        "population": _nullable("string"),
        "gender": {
            "type": ["string", "null"],
            "enum": ["Male", "Female", "Mixed", "NA", None],
        },
        "smoking_status": _nullable("string"),
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
            "type": "string",
            "enum": ["dietary_intake", "human_biospecimen", "unclear"],
        },
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
            "additional_extractions": {
                "type": "array",
                "items": _extraction_schema(),
            },
            "jbi": _jbi_schema(),
        }
    elif stage == "full_text":
        item_properties = {
            "context_id": {"type": "string"},
            "status": {"type": "string", "enum": ["INCLUDE", "EXCLUDE"]},
            "exclusion_code": {"type": ["string", "null"]},
            "reason": {"type": "string"},
            "extraction": _extraction_schema(),
            "additional_extractions": {
                "type": "array",
                "items": _extraction_schema(),
            },
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
                "reclassification_roles": context.get(
                    "reclassification_roles"
                ) or [],
                "approved_target_for_sources": context.get(
                    "approved_target_for_sources"
                ) or [],
                "classification_instruction": context.get(
                    "classification_instruction"
                ) or "",
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
    cleaned = (
        str(value)
        .strip()
        .replace(",", "")
        .replace("−", "-")
        .replace("≤", "<=")
        .replace("≥", ">=")
    )
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


def _percentage_mentions(value: Any) -> list[tuple[float, int]]:
    """Return numeric and spelled-out percentages with their source offsets."""
    text = str(value or "")
    mentions = [
        (float(match.group(1)), match.start())
        for match in re.finditer(
            r"(\d+(?:\.\d+)?)\s*(?:%|per\s+cent\b|percent\b)",
            text,
            flags=re.IGNORECASE,
        )
    ]
    for match in WORD_NUMBER_PATTERN.finditer(text):
        if not re.match(
            r"\s*(?:%|per\s+cent\b|percent\b)",
            text[match.end() :],
            flags=re.IGNORECASE,
        ):
            continue
        parts = re.split(r"[- ]", match.group(0).casefold())
        number = WORD_NUMBER_ONES.get(parts[0], WORD_NUMBER_TENS.get(parts[0], 0))
        if len(parts) == 2:
            number += WORD_NUMBER_ONES[parts[1]]
        mentions.append((float(number), match.start()))
    return mentions


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
            for percent, percent_position in _percentage_mentions(excerpt):
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
                    key=lambda match: abs(match.start() - percent_position),
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
        for percent, percent_position in _percentage_mentions(excerpt):
            if not math.isclose(target, percent, rel_tol=1e-9, abs_tol=1e-12):
                continue
            direction = min(
                directions,
                key=lambda match: abs(match.start() - percent_position),
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
    """Apply auditable ethnicity imputation, or return NA without evidence."""
    country = one_line(extraction.get("country"))
    population = one_line(extraction.get("population"))
    haystack = f"{country} {population}".casefold()
    evidence = (extraction.get("evidence") or {}).get("ethnicity") or {}
    if not evidence:
        return (
            "NA",
            "NA: no field-specific ethnicity evidence was available",
            "NA",
        )
    support = one_line(evidence.get("excerpt")) or country or population
    population_rules = (
        (r"\b(?:white|caucasian|european ancestry)\b|\b(?:sweden|swedish|finland|finnish|norway|norwegian|denmark|danish|iceland|icelandic|germany|german|netherlands|dutch|poland|polish|france|french|italy|italian|spain|spanish|portugal|portuguese)\b", "White", "stated White/European population"),
        (r"\b(?:asian|east asian|south asian)\b|\b(?:china|chinese|japan|japanese|korea|korean|taiwan|taiwanese|india|indian|pakistan|pakistani|bangladesh|bangladeshi|vietnam|vietnamese|thailand|thai)\b", "Asian", "stated Asian country/population"),
        (r"\bafrican[- ]american\b|\bblack\b|\b(?:nigeria|nigerian|ghana|ghanaian|kenya|kenyan|ethiopia|ethiopian|sub[- ]saharan african)\b", "Black", "stated Black/African population"),
        (r"\bhispanic\b|\blatin[oa]x?\b|\bmexic(?:o|an)\b|\bpuerto rican\b", "Hispanic/Latino", "stated Hispanic/Latino population"),
        (r"\b(?:iran|iranian|saudi arabia|saudi|turkey|turkish|middle eastern|north african)\b", "Middle Eastern/North African", "stated Middle Eastern/North African population"),
        (r"\b(?:aboriginal|first nations|native american|american indian|indigenous)\b", "Indigenous", "stated Indigenous population"),
    )
    matches = [
        (label, description)
        for pattern, label, description in population_rules
        if re.search(pattern, haystack)
    ]
    labels = {label for label, _ in matches}
    if len(labels) > 1:
        return (
            "Mixed",
            "Mixed: the article explicitly indicates multiple mapped populations",
            support,
        )
    if re.search(r"\bsweden\b|\bswedish\b", haystack):
        return "White", "Country imputation: Sweden -> White", support
    if re.search(r"\bchina\b|\bchinese\b", haystack):
        return "Asian", "Country imputation: China -> Asian", support
    if matches:
        label, description = matches[0]
        return label, f"Population imputation: {description} -> {label}", support
    return (
        "Mixed",
        "Default: no protocol-mapped specific country or population was established",
        support,
    )


def _normalise_gender(value: Any) -> str:
    text = one_line(value).casefold()
    if text in {"male", "males", "man", "men"}:
        return "Male"
    if text in {"female", "females", "woman", "women"}:
        return "Female"
    if text in {"mixed", "both", "male and female", "men and women"}:
        return "Mixed"
    if text in {
        "", "na", "n/a", "nr", "unknown", "unclear", "not reported", "none", "null"
    }:
        return "NA"
    raise ScreeningValidationError(
        f"gender must be Male, Female, Mixed, or NA, not {value!r}"
    )


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
    extraction["p_value_relation"] = _p_value_relation(raw.get("p_value"))
    numeric = {"effect_size", "ci_lower", "ci_upper", "p_value", "total_n", "cases"}
    for field in EXTRACTION_FIELDS:
        if field not in raw:
            # Older durable packets predate optional population descriptors.
            # New structured responses require them, while replay remains
            # backwards compatible and treats absence as unavailable.
            if field == "gender":
                extraction[field] = "NA"
                continue
            if field == "smoking_status":
                extraction[field] = None
                continue
            raise ScreeningValidationError(f"Extraction is missing required field: {field}")
        if field in numeric:
            extraction[field] = _number_or_none(raw.get(field), field)
        elif field == "needs_inversion":
            if not isinstance(raw.get(field), bool):
                raise ScreeningValidationError("needs_inversion must be boolean")
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
                None
                if text.casefold() in missing_values
                else text
            )
    extraction["gender"] = _normalise_gender(extraction.get("gender"))
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
    # Preserve the paper-reported measurement before Wald completion or later
    # direction/OR/HR-to-RR transformations.  These fields are deliberately
    # internal to the durable packet rather than part of the model schema.
    extraction["reported_effect_type"] = extraction.get("effect_type")
    extraction["reported_effect_type_label"] = raw_effect_type
    extraction["reported_effect_size"] = extraction.get("effect_size")
    extraction["reported_ci_lower"] = extraction.get("ci_lower")
    extraction["reported_ci_upper"] = extraction.get("ci_upper")
    extraction["reported_p_value"] = extraction.get("p_value")
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
    if measurement not in {None, "NA", "dietary_intake", "human_biospecimen", "unclear"}:
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
    add_wald_derivations(extraction)
    ethnicity, basis, support = infer_ethnicity(extraction)
    extraction["ethnicity"] = ethnicity
    extraction["ethnicity_basis"] = basis
    extraction["ethnicity_supporting_text"] = support
    return extraction


def extraction_stratum_key(extraction: dict[str, Any] | None) -> tuple[str, str]:
    """Return the dimensions that require separate saved study records."""
    extraction = extraction or {}
    gender = one_line(extraction.get("gender")).casefold() or "na"
    measurement = (
        one_line(extraction.get("exposure_measurement_type")).casefold()
        or "unclear"
    )
    return gender, measurement


def _normalise_additional_extractions(
    raw: Any,
    *,
    primary: dict[str, Any],
    required_source: str | None = None,
    source_text: str | None = None,
    source_identifier: str | None = None,
) -> list[dict[str, Any]]:
    """Normalise extra gender/measurement strata and reject duplicate strata."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ScreeningValidationError("additional_extractions must be an array")
    records = [primary]
    for value in raw:
        records.append(
            _normalise_extraction(
                value,
                required_source=required_source,
                source_text=source_text,
                source_identifier=source_identifier,
            )
        )
    preferred = relaxed_policy.prefer_adjusted(records)
    superseded = [r for r in records if r.get("superseded_by_adjusted")]
    if preferred[0] is not primary:
        chosen = dict(preferred[0])
        primary.clear()
        primary.update(chosen)
    if superseded:
        primary["superseded_unadjusted_estimates"] = [dict(r) for r in superseded if r is not primary]
    records = [primary] + preferred[1:]
    keys = [extraction_stratum_key(record) for record in records]
    if required_source != "full_text" and len(keys) != len(set(keys)):
        raise ScreeningValidationError(
            "Each extraction must have a unique gender and exposure-measurement stratum"
        )
    return records[1:]


def extraction_records(item: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return a context's primary and additional extraction records."""
    if not isinstance(item, dict) or not isinstance(item.get("extraction"), dict):
        return []
    return [
        item["extraction"],
        *[
            value
            for value in item.get("additional_extractions") or []
            if isinstance(value, dict)
        ],
    ]


def split_study_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand one screened context into separately saveable estimate records."""
    records = extraction_records(item)
    if not records:
        return [item]
    split = []
    for index, extraction in enumerate(records, start=1):
        identity_extraction = dict(extraction)
        if extraction.get("exposure_measurement_overridden") is True:
            identity_extraction["exposure_measurement_type"] = extraction.get("exposure_measurement_type_original")
        gender, measurement = extraction_stratum_key(identity_extraction)
        record_id = (
            f"{item.get('context_id', '')}#gender={gender};measurement={measurement}"
        )
        split.append(
            {
                **item,
                "record_id": record_id,
                "estimate_index": index,
                "gender": extraction.get("gender") or "NA",
                "smoking_status": extraction.get("smoking_status") or "NA",
                "exposure_measurement_type": measurement,
                "extraction": extraction,
                "additional_extractions": [],
            }
        )
    return split


def missing_key_fields(extraction: dict[str, Any] | None) -> list[str]:
    extraction = extraction or {}
    relaxed_policy.complete(extraction)
    return [field for field in KEY_EVIDENCE_FIELDS if extraction.get(field) is None]


def invalid_key_fields(extraction: dict[str, Any] | None) -> list[str]:
    """Return required quantitative fields that are present but incoherent."""
    extraction = extraction or {}
    invalid: set[str] = set()
    if extraction.get("effect_type") not in WALD_RATIO_EFFECT_TYPES:
        invalid.add("effect_type")
    numeric_fields = ("effect_size", "ci_lower", "ci_upper")
    coherent_ratio_value: dict[str, bool] = {}
    for field in numeric_fields:
        value = extraction.get(field)
        coherent_ratio_value[field] = bool(
            value is not None
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) > 0
        )
        if value is not None and not coherent_ratio_value[field]:
            invalid.add(field)
    p_value = extraction.get("p_value")
    if p_value is not None and (
        not isinstance(p_value, (int, float))
        or isinstance(p_value, bool)
        or not math.isfinite(float(p_value))
        or not 0 < float(p_value) <= 1
    ):
        invalid.add("p_value")
    effect = extraction.get("effect_size")
    lower = extraction.get("ci_lower")
    upper = extraction.get("ci_upper")
    if (
        coherent_ratio_value["ci_lower"]
        and coherent_ratio_value["ci_upper"]
        and not float(lower) < float(upper)
    ):
        invalid.update({"ci_lower", "ci_upper"})
    if (
        coherent_ratio_value["effect_size"]
        and coherent_ratio_value["ci_lower"]
        and float(effect) < float(lower)
    ):
        invalid.update({"effect_size", "ci_lower"})
    if (
        coherent_ratio_value["effect_size"]
        and coherent_ratio_value["ci_upper"]
        and float(effect) > float(upper)
    ):
        invalid.update({"effect_size", "ci_upper"})
    total_n, cases = extraction.get("total_n"), extraction.get("cases")
    if total_n is not None and (
        not isinstance(total_n, int) or isinstance(total_n, bool) or total_n <= 0
    ):
        invalid.add("total_n")
    if cases is not None and (
        not isinstance(cases, int) or isinstance(cases, bool) or cases <= 0
    ):
        invalid.add("cases")
    if (
        isinstance(total_n, int)
        and not isinstance(total_n, bool)
        and isinstance(cases, int)
        and not isinstance(cases, bool)
        and cases > total_n
    ):
        invalid.add("cases")
    return sorted(invalid & set(KEY_EVIDENCE_FIELDS))


def required_quantitative_issues(extraction: dict[str, Any] | None) -> list[str]:
    return sorted(set(missing_key_fields(extraction)) | set(invalid_key_fields(extraction)))


def exposure_classification_ambiguous(extraction: dict[str, Any] | None) -> bool:
    extraction = extraction or {}
    return bool(
        extraction.get("exposure_subtype") == "mixed_or_unclear"
        or extraction.get("exposure_measurement_type") == "unclear"
    )


def needs_full_text(extraction: dict[str, Any] | None, jbi: dict[str, Any] | None) -> bool:
    """Apply the post-Wald condition; PMCID eligibility is checked separately."""
    return bool(
        (jbi or {}).get("grade") == "Fair"
        or required_quantitative_issues(extraction)
    )


def missing_key_fields_for_item(item: dict[str, Any]) -> list[str]:
    """Return the union of missing fields across all estimate strata."""
    if not extraction_records(item):
        return list(KEY_EVIDENCE_FIELDS)
    return sorted(
        {
            field
            for extraction in extraction_records(item)
            for field in missing_key_fields(extraction)
        }
    )


def invalid_key_fields_for_item(item: dict[str, Any]) -> list[str]:
    return sorted(
        {
            field
            for extraction in extraction_records(item)
            for field in invalid_key_fields(extraction)
        }
    )


def item_exposure_classification_ambiguous(item: dict[str, Any]) -> bool:
    return any(
        exposure_classification_ambiguous(value)
        for value in extraction_records(item)
    )


def item_needs_full_text(item: dict[str, Any], jbi: dict[str, Any] | None) -> bool:
    return bool(
        (jbi or {}).get("grade") == "Fair"
        or not extraction_records(item)
        or any(required_quantitative_issues(value) for value in extraction_records(item))
    )


def item_has_poolable_extraction(item: dict[str, Any], jbi: dict[str, Any] | None) -> bool:
    return any(is_poolable(value, jbi) for value in extraction_records(item))


def is_poolable(extraction: dict[str, Any] | None, jbi: dict[str, Any] | None) -> bool:
    return bool(
        not required_quantitative_issues(extraction)
        and (jbi or {}).get("grade") in {"Moderate", "Good"}
    )


def full_text_gate(item: dict[str, Any]) -> bool:
    """Return the exact included + PMCID + post-Wald Luna eligibility gate."""
    return bool(
        item.get("eligibility") == "INCLUDE"
        and item.get("pmcid")
        and item_needs_full_text(item, item.get("jbi"))
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
                "reclassification_roles": base.get(
                    "reclassification_roles"
                ) or [],
                "approved_target_for_sources": base.get(
                    "approved_target_for_sources"
                ) or [],
                "classification_instruction": base.get(
                    "classification_instruction"
                ) or "",
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
            additional_extractions = []
            jbi = None
            missing = []
            invalid = []
            ambiguous = False
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
            additional_extractions = _normalise_additional_extractions(
                raw.get("additional_extractions"),
                primary=extraction,
                required_source="abstract",
                source_text=base.get("_abstract_text") or "",
                source_identifier=base["pmid"],
            )
            jbi = _strict_jbi(raw.get("jbi"))
            extraction_item = {
                "extraction": extraction,
                "additional_extractions": additional_extractions,
            }
            missing = missing_key_fields_for_item(extraction_item)
            invalid = invalid_key_fields_for_item(extraction_item)
            ambiguous = item_exposure_classification_ambiguous(extraction_item)
            escalate = item_needs_full_text(extraction_item, jbi)
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
                "exposure_classification_ambiguous": ambiguous,
                "needs_full_text": escalate,
                "full_text_eligible": bool(
                    eligibility == "INCLUDE" and escalate and base.get("pmcid")
                ),
                "extraction": extraction,
                "additional_extractions": additional_extractions,
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
    # A changed input digest can legitimately retire an old progress
    # checkpoint while the durable problem ledger remains. Reconcile matching
    # run-level records as well, so a subsequently completed context cannot be
    # left marked pending merely because its original checkpoint was replaced.
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
            active.setdefault(str(record["unit_key"]), record)
    _resolve_completed_deferred_problems(output_dir, active, progress)
    return active


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
    return FIRST_INSTRUCTIONS + RECLASSIFICATION_ROLE_INSTRUCTIONS + "\n\nARTICLES:\n" + json.dumps(
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
        _active_deferred_problems(
            output_dir,
            {},
            existing_results,
            track=runtime.name,
            stage="first",
        )
        return existing

    checkpoint = {} if force else load_json(progress_path, {})
    if (
        checkpoint.get("model") != runtime.first_model
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
    usage = checkpoint.get("usage") or existing.get("usage") or empty_usage(runtime.first_model)
    deferred_by_key = _active_deferred_problems(
        output_dir,
        checkpoint,
        progress,
        track=runtime.name,
        stage="first",
    )

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
                "deferred_screening_problems": sorted(
                    deferred_by_key.values(), key=lambda item: item["unit_key"]
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
                "reclassification_roles": value.get(
                    "reclassification_roles"
                ) or [],
                "approved_target_for_sources": value.get(
                    "approved_target_for_sources"
                ) or [],
                "classification_instruction": value.get(
                    "classification_instruction"
                ) or "",
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
    return SECOND_INSTRUCTIONS + RECLASSIFICATION_ROLE_INSTRUCTIONS + "\n\nARTICLES:\n" + json.dumps(
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
        _active_deferred_problems(
            output_dir,
            {},
            existing_results,
            track=runtime.name,
            stage="second",
        )
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
    deferred_by_key = _active_deferred_problems(
        output_dir,
        checkpoint,
        progress,
        track=runtime.name,
        stage="second",
    )

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
        value = full_text.get(field)
        if value is None:
            continue
        if field == "gender":
            # Gender is an R2-Sol field for MetaPee.  R3 Luna may recover
            # quantitative/JBI evidence but must not replace Sol's value,
            # including an explicit NA.
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
        merged[field] = value
        reported_field = {
            "effect_type": "reported_effect_type",
            "effect_size": "reported_effect_size",
            "ci_lower": "reported_ci_lower",
            "ci_upper": "reported_ci_upper",
            "p_value": "reported_p_value",
        }.get(field)
        if reported_field and full_text.get(reported_field) is not None:
            merged[reported_field] = full_text[reported_field]
        if field == "effect_type" and full_text.get("reported_effect_type_label"):
            merged["reported_effect_type_label"] = full_text[
                "reported_effect_type_label"
            ]
    merged_evidence = dict((abstract or {}).get("evidence") or {})
    for field, evidence in full_evidence.items():
        if evidence:
            merged_evidence[field] = evidence
    merged["evidence"] = merged_evidence
    add_wald_derivations(merged)
    ethnicity, basis, support = infer_ethnicity(merged)
    merged["ethnicity"] = ethnicity
    merged["ethnicity_basis"] = basis
    merged["ethnicity_supporting_text"] = support
    return merged


def _merge_extraction_records(
    abstract_item: dict[str, Any],
    full_primary: dict[str, Any],
    full_additional: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge full-text strata positionally, preserving the requested ordering."""
    abstract_records = extraction_records(abstract_item)
    full_records = [full_primary, *full_additional]
    merged = []
    for index in range(max(len(abstract_records), len(full_records))):
        if index < len(abstract_records) and index < len(full_records):
            merged.append(_merge_extractions(abstract_records[index], full_records[index]))
        elif index < len(abstract_records):
            merged.append(abstract_records[index])
        else:
            merged.append(full_records[index])
    keys = [extraction_stratum_key(record) for record in merged]
    if len(keys) != len(set(keys)):
        raise ScreeningValidationError(
            "Full-text extraction produced duplicate gender/measurement strata"
        )
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
            additional_extractions = list(base.get("additional_extractions") or [])
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
            full_additional = _normalise_additional_extractions(
                raw.get("additional_extractions"),
                primary=full_extraction,
                required_source="full_text",
                source_text=source_text,
                source_identifier=source_identifier,
            )
            merged_records = _merge_extraction_records(
                base,
                full_extraction,
                full_additional,
            )
            extraction = merged_records[0]
            additional_extractions = merged_records[1:]
            jbi = _strict_jbi(raw.get("jbi"))
            code = None
            merged_item = {
                "extraction": extraction,
                "additional_extractions": additional_extractions,
            }
            final_status = (
                "INCLUDE_META"
                if item_has_poolable_extraction(merged_item, jbi)
                else "ELIGIBLE_NOT_POOLABLE"
            )
        result_item = {
            **base,
            "extraction": extraction,
            "additional_extractions": additional_extractions,
        }
        results.append(
            {
                **result_item,
                "final_status": final_status,
                "full_text_status": final_status,
                "exclusion_code": code,
                "reason": reason,
                "missing_key_fields": missing_key_fields_for_item(result_item),
                "invalid_key_fields": invalid_key_fields_for_item(result_item),
                "exposure_classification_ambiguous": item_exposure_classification_ambiguous(result_item),
                "jbi": jbi,
                "pmc_source_status": "full_text",
            }
        )
    return sorted(results, key=lambda item: item["context_id"])


def unavailable_full_text_record(
    context: dict[str, Any],
    source_status: str,
    error: str = "",
) -> dict[str, Any]:
    pmcid = context.get("pmcid")
    if not pmcid:
        reason = "Full text was indicated but no PMCID was available, so missing evidence could not be recovered."
    elif source_status == "fetch_failed":
        reason = "The PMC full text could not be retrieved after retries, so missing evidence could not be recovered."
    else:
        reason = "The PMC record did not contain usable full article text, so missing evidence could not be recovered."
    return {
        **context,
        "final_status": "ELIGIBLE_NOT_POOLABLE",
        "full_text_status": "ELIGIBLE_NOT_POOLABLE",
        "exclusion_code": None,
        "reason": reason,
        "missing_key_fields": missing_key_fields_for_item(context),
        "pmc_source_status": source_status,
        "pmc_error": one_line(error)[:500],
    }


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
        if full_text_gate(item)
    }
    relevant_pmcids = {str(item.get("pmcid") or "") for item in contexts.values()}
    source_manifest = full_text_source_manifest(sources, relevant_pmcids)
    retrieval_failures = [
        {
            "pmcid": pmcid,
            "status": entry.get("status") or "unavailable",
            "reason": entry.get("error") or "Verified full PMC article text was not retrieved.",
        }
        for pmcid, entry in source_manifest.items()
        if entry.get("status") != "full_text"
    ]
    if retrieval_failures:
        save_json(
            output_dir / f"{runtime.name}_full_text_retrieval_failures.json",
            {
                "schema_version": 1,
                "recorded_at": utc_now(),
                "model": runtime.full_text_model,
                "failures": retrieval_failures,
            },
        )
        raise ScreeningValidationError(
            "Every PMCID-gated context must receive Luna review of verified full "
            "PMC content; retrieval failed for "
            + ", ".join(item["pmcid"] for item in retrieval_failures)
        )
    input_digest = full_text_input_digest(
        second_packet,
        runtime.full_text_model,
        source_manifest,
    )
    existing = {} if force else load_json(output_path, {})
    if (
        existing.get("model") != runtime.full_text_model
        or existing.get("input_digest") != input_digest
    ):
        existing = {}
    existing_results = {
        item["context_id"]: item
        for item in existing.get("results", [])
        if item.get("context_id") in contexts
    }
    if existing and set(existing_results) == set(contexts):
        _active_deferred_problems(
            output_dir,
            {},
            existing_results,
            track=runtime.name,
            stage="full_text",
        )
        return existing

    checkpoint = {} if force else load_json(progress_path, {})
    if (
        checkpoint.get("model") != runtime.full_text_model
        or checkpoint.get("input_digest") != input_digest
    ):
        checkpoint = {}
    progress = dict(existing_results)
    progress.update(
        {
            cid: item
            for cid, item in (checkpoint.get("results_by_context") or {}).items()
            if cid in contexts
        }
    )
    usage = checkpoint.get("usage") or existing.get("usage") or empty_usage(runtime.full_text_model)
    manual_adjudications = load_full_text_manual_adjudications(output_dir, contexts)
    if manual_adjudications:
        raise ScreeningValidationError(
            "Manual adjudication cannot replace the required Luna PMC review"
        )
    for cid, adjudication in manual_adjudications.items():
        context = contexts[cid]
        pmcid = str(context.get("pmcid") or "")
        source = sources.get(pmcid, {"status": "unavailable", "text": "", "error": ""})
        if source.get("status") != "full_text":
            raise ScreeningValidationError(
                f"Manual full-text adjudication {cid} requires a complete full-text source"
            )
        status = str(adjudication["full_text_status"])
        progress[cid] = {
            **context,
            "final_status": status,
            "full_text_status": status,
            "exclusion_code": adjudication["exclusion_code"],
            "reason": adjudication["reason"],
            "missing_key_fields": missing_key_fields_for_item(context),
            "pmc_source_status": "full_text",
            "full_text_characters_reviewed": len(source["text"]),
            "manual_full_text_adjudication": True,
            "manual_full_text_adjudication_source": (
                FULL_TEXT_MANUAL_ADJUDICATIONS_FILENAME
            ),
            "manual_full_text_adjudication_recorded_at": adjudication["recorded_at"],
        }
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
                "input_digest": input_digest,
                "source_second_model": second_packet.get("model"),
                "source_second_input_digest": second_packet.get("input_digest"),
                "source_manifest": source_manifest,
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
        source = sources.get(pmcid, {"status": "no_pmcid"})
        if source.get("status") != "full_text":
            raise ScreeningValidationError(
                f"Verified full PMC article text is unavailable for {pmcid}"
            )
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
                "abstract_jbi_grade": (item.get("jbi") or {}).get("grade"),
                "reclassification_roles": item.get(
                    "reclassification_roles"
                ) or [],
                "approved_target_for_sources": item.get(
                    "approved_target_for_sources"
                ) or [],
                "classification_instruction": item.get(
                    "classification_instruction"
                ) or "",
            }
            for item in expected.values()
        ]
        prompt = (
            FULL_TEXT_INSTRUCTIONS
            + RECLASSIFICATION_ROLE_INSTRUCTIONS
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
            item["full_text_characters_reviewed"] = len(sources[pmcid]["text"])
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
        "contexts": len(results),
        "status_counts": dict(Counter(item["full_text_status"] for item in results)),
        "manual_full_text_adjudications": len(manual_adjudications),
        "usage": usage,
        "results": results,
    }
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
    if subtype != "whole_food":
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


def validate_full_text_packet_lineage(
    second_packet: dict[str, Any],
    full_text_packet: dict[str, Any],
    track: str,
    expected_model: str,
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
        if full_text_gate(item)
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
                f"{track} full_text source manifest has invalid status {status!r}"
            )
        if isinstance(characters, bool) or not isinstance(characters, int) or characters < 0:
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

    expected_digest = full_text_input_digest(
        second_packet,
        expected_model,
        manifest,
    )
    if full_text_packet.get("input_digest") != expected_digest:
        raise ScreeningValidationError(
            f"{track} full_text input_digest does not match its second-screen/source lineage"
        )
    if full_text_packet.get("contexts") != len(expected_contexts):
        raise ScreeningValidationError(f"{track} full_text context count is inconsistent")
    for item in full_text_packet.get("results", []):
        pmcid = str(item.get("pmcid") or "")
        source = manifest[pmcid]
        if item.get("pmc_source_status") != source["status"]:
            raise ScreeningValidationError(
                f"{track} full_text result {item['context_id']} has inconsistent source status"
            )
        if source["status"] == "full_text" and item.get(
            "full_text_characters_reviewed"
        ) != source["characters"]:
            raise ScreeningValidationError(
                f"{track} full_text result {item['context_id']} has inconsistent source length"
            )
        if item.get("manual_full_text_adjudication"):
            raise ScreeningValidationError(
                f"{track} full_text result {item['context_id']} bypassed Luna"
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
                "additional_extractions": [],
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
                    final["additional_extractions"] = [
                        dict(value)
                        for value in final.get("additional_extractions") or []
                        if isinstance(value, dict)
                    ]
                    for value in extraction_records(final):
                        add_wald_derivations(value)
                    final["missing_key_fields"] = missing_key_fields_for_item(final)
                    if final.get("final_status") != "EXCLUDE":
                        poolable = item_has_poolable_extraction(final, final.get("jbi"))
                        final_status = (
                            "INCLUDE_META" if poolable else "ELIGIBLE_NOT_POOLABLE"
                        )
                        if final_status != final.get("final_status"):
                            final["reason"] = (
                                "Wald-derived uncertainty completed the key evidence, "
                                "and the JBI grade is Moderate or Good."
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
                additional_extractions = [
                    dict(value)
                    for value in second_item.get("additional_extractions") or []
                    if isinstance(value, dict)
                ]
                final_extraction_item = {
                    "extraction": final_extraction,
                    "additional_extractions": additional_extractions,
                }
                for value in extraction_records(final_extraction_item):
                    add_wald_derivations(value)
                poolable = item_has_poolable_extraction(
                    final_extraction_item,
                    second_item.get("jbi"),
                )
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
                    "missing_key_fields": missing_key_fields_for_item(
                        final_extraction_item
                    ),
                    "extraction": final_extraction,
                    "additional_extractions": additional_extractions,
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
                    "additional_extractions": [],
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
        and missing_key_fields_for_item(item)
    ]
    missing_field_contexts = Counter(
        field
        for item in moderate_good_missing
        for field in missing_key_fields_for_item(item)
    )
    missing_field_pmids = {
        field: sorted(
            {
                str(item.get("pmid"))
                for item in moderate_good_missing
                if field in missing_key_fields_for_item(item)
            }
        )
        for field in KEY_EVIDENCE_FIELDS
    }
    return {
        "schema_version": 2,
        "created_at": utc_now(),
        "track": track,
        "publication_date_start": candidates.get("publication_date_start"),
        "publication_date_end": candidates.get("publication_date_end"),
        "counts": dict(Counter(item["final_status"] for item in results)),
        "wald_confidence_intervals_computed": sum(
            extraction.get("ci_method")
            == WALD_CONFIDENCE_INTERVAL_METHOD
            for item in results
            if item.get("final_status") != "EXCLUDE"
            for extraction in extraction_records(item)
        ),
        "wald_confidence_interval_method": WALD_CONFIDENCE_INTERVAL_METHOD,
        "wald_p_values_computed": sum(
            extraction.get("p_value_method")
            == WALD_P_VALUE_METHOD
            for item in results
            if item.get("final_status") != "EXCLUDE"
            for extraction in extraction_records(item)
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
) -> tuple[dict[str, dict[str, str]], int]:
    """Fetch each requested PMC article once; failures become auditable records."""
    pmcids = {
        str(item.get("pmcid"))
        for packet in second_packets
        for item in packet.get("results", [])
        if full_text_gate(item)
    }
    sources: dict[str, dict[str, str]] = {
        "": {"status": "no_pmcid", "text": "", "error": ""}
    }
    full_text_dir = output_dir / "full_text"
    full_text_dir.mkdir(parents=True, exist_ok=True)
    requests = 0
    for pmcid in sorted(pmcids):
        text_path = full_text_dir / f"{pmcid}.txt"
        try:
            if text_path.exists() and not force:
                text = text_path.read_text(encoding="utf-8")
            else:
                requests += 1
                text = parser(fetcher(pmcid))
                if len(text) >= 2000:
                    # Hash and screen the exact bytes that a resumed run will
                    # read.  Previously the returned in-memory text omitted
                    # this trailing newline while the cached file included it,
                    # changing the full-text input digest on the first resume
                    # and invalidating already-paid checkpoints.
                    text = text.rstrip() + "\n"
                    text_path.write_text(text, encoding="utf-8")
            status = "full_text" if len(text) >= 2000 else "partial_record"
            sources[pmcid] = {"status": status, "text": text, "error": ""}
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
    save_json(
        output_dir / "pmc_source_status.json",
        {
            "schema_version": 1,
            "created_at": utc_now(),
            "ncbi_requests": requests,
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
            "MetaPee DeepSeek validation status",
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
        "MetaPee ChatGPT–DeepSeek screening discrepancies",
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
