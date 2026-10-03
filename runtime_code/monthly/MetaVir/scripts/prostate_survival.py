#!/usr/bin/env python3
"""Minimal, auditable prostate-cancer survival meta-analysis workflow.

This module deliberately writes outside the incidence cache.  It reuses the
MetaVir exposure vocabulary, PubMed helpers, and ChatGPT gateway runtime while
keeping survival effect estimates on their reported hazard-ratio scale.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from effect_measure import normalize_effect_type  # noqa: E402
from scripts import dual_screening  # noqa: E402
from scripts import monthly_evidence_update as monthly  # noqa: E402


FIRST_MODEL = dual_screening.CHATGPT_FIRST_MODEL
SECOND_MODEL = dual_screening.CHATGPT_SECOND_MODEL
FULL_TEXT_MODEL = dual_screening.CHATGPT_FULL_TEXT_MODEL

EXPOSURE_TIMINGS = ("prediagnosis", "postdiagnosis", "both", "unclear")
SURVIVAL_OUTCOMES = (
    "overall_survival",
    "prostate_cancer_specific_survival",
    "progression_free_survival",
    "metastasis_free_survival",
    "biochemical_recurrence_free_survival",
)
COMPARISON_TYPES = (
    "highest_vs_lowest",
    "user_vs_nonuser",
    "per_serving",
    "per_unit",
    "continuous",
    "other",
    "unclear",
)
STAGE_CATEGORIES = (
    "localized",
    "locally_advanced",
    "nonmetastatic",
    "metastatic",
    "mixed",
    "unclear",
)
DISEASE_SETTINGS = (
    "active_surveillance",
    "after_prostatectomy",
    "after_radiotherapy",
    "biochemical_recurrence",
    "metastatic_hormone_sensitive",
    "castration_resistant",
    "mixed",
    "unclear",
)

SURVIVAL_CANCER_QUERY = (
    "(" + monthly.CANCER_QUERIES["Prostate cancer"] + ") AND "
    "(\"Survival\"[MeSH Terms] OR \"Mortality\"[MeSH Terms] "
    "OR \"Disease Progression\"[MeSH Terms] OR surviv*[Title/Abstract] "
    "OR mortalit*[Title/Abstract] OR death*[Title/Abstract] "
    "OR fatal*[Title/Abstract] OR progression-free[Title/Abstract] "
    "OR time-to-progression[Title/Abstract] OR disease progression[Title/Abstract] "
    "OR metastasis-free[Title/Abstract] OR recurrence-free[Title/Abstract] "
    "OR biochemical recurrence[Title/Abstract] OR biochemical failure[Title/Abstract] "
    "OR PSA recurrence[Title/Abstract] OR PSA failure[Title/Abstract] "
    "OR PSA relapse[Title/Abstract])"
)

FIRST_EXCLUSION_CODES = (
    "S1-NOT-SURVIVAL",
    "S1-CLEARLY-INELIGIBLE",
    "S2-REVIEW-META-ANALYSIS",
    "S2-INCIDENCE-ONLY",
)
SECOND_EXCLUSION_CODES = (
    "S2-REVIEW-META-ANALYSIS",
    "S2-NONPRIMARY",
    "S2-NONHUMAN",
    "S2-INCIDENCE-ONLY",
    "S2-NOT-DIAGNOSED",
    "S2-WRONG-EXPOSURE",
    "S2-WRONG-OUTCOME",
    "S2-TREATMENT-NOT-EXPOSURE",
    "S2-NO-SEPARABLE-ESTIMATE",
    "S2-OTHER",
)

EXTRACTION_FIELDS = (
    "pmid",
    "pmcid",
    "publication_year",
    "cohort_name",
    "country",
    "ethnicity",
    "study_population",
    "sample_size",
    "number_of_events",
    "prostate_cancer_stage",
    "prostate_cancer_stage_category",
    "disease_setting",
    "primary_treatment",
    "exposure",
    "exposure_alias",
    "exposure_timing",
    "exposure_assessment_method",
    "outcome",
    "outcome_definition",
    "follow_up",
    "effect_measure",
    "effect_estimate",
    "lower_ci",
    "upper_ci",
    "ci_confidence_level",
    "exposure_contrast",
    "comparison_type",
    "contrast_scale",
    "needs_inversion",
    "reference_group",
    "adjustment_variables",
    "is_adjusted",
)

EVIDENCE_FIELDS = (
    "cohort_name",
    "country",
    "ethnicity",
    "study_population",
    "sample_size",
    "number_of_events",
    "prostate_cancer_stage",
    "disease_setting",
    "primary_treatment",
    "exposure_alias",
    "exposure_timing",
    "exposure_assessment_method",
    "outcome_definition",
    "follow_up",
    "effect_measure",
    "effect_estimate",
    "confidence_interval",
    "exposure_contrast",
    "reference_group",
    "adjustment_variables",
)


def load_exposure_catalog(
    selected_exposures: list[str] | None = None,
) -> dict[str, list[str]]:
    """Return every current and additional canonical exposure with search aliases."""
    return monthly.selected_exposure_terms(
        selected_exposures,
        catalog="combined",
    )


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"Required survival workflow artifact is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reuse_bound_packet(
    path: Path,
    expected_lineage: dict[str, Any],
    *,
    force: bool,
) -> dict[str, Any] | None:
    if not path.is_file() or force:
        return None
    packet = monthly.load_json(path, {})
    if all(packet.get(field) == digest for field, digest in expected_lineage.items()):
        return packet
    raise RuntimeError(
        f"Existing {path.name} is stale for its upstream survival artifacts; use --force to rebuild it"
    )


def _clean_text(value: Any, *, default: str | None = None) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or default


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: Any) -> int | None:
    number = _number(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _publication_year(value: Any) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def normalize_exposure_timing(value: Any) -> str:
    text = re.sub(r"[^a-z]+", " ", str(value or "").casefold()).strip()
    if text in EXPOSURE_TIMINGS:
        return text
    pre = bool(re.search(r"\bpre\s*diagnos|before (?:the )?diagnos|prior to (?:the )?diagnos", text))
    post = bool(re.search(r"\bpost\s*diagnos|after (?:the )?diagnos|following (?:the )?diagnos", text))
    if pre and post or re.search(r"\bboth\b|before and after|pre and post", text):
        return "both"
    if pre:
        return "prediagnosis"
    if post:
        return "postdiagnosis"
    return "unclear"


def normalize_outcome(value: Any) -> str | None:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    canonical = text.replace(" ", "_")
    if canonical in SURVIVAL_OUTCOMES:
        return canonical
    if re.search(r"biochemical.*recurr|recurr.*biochemical|bcr.*free", text):
        return "biochemical_recurrence_free_survival"
    if re.search(r"metastasis.*free|metastatic.*progression.*free", text):
        return "metastasis_free_survival"
    if re.search(r"progression.*free|progression free survival|time to progression", text):
        return "progression_free_survival"
    if re.search(
        r"prostate cancer specific|prostate specific|cancer specific|disease specific|"
        r"death from prostate|fatal prostate",
        text,
    ):
        return "prostate_cancer_specific_survival"
    if re.search(r"overall survival|all cause mortal|death from any cause|overall mortal", text):
        return "overall_survival"
    return None


def normalize_stage_category(original: Any, supplied: Any = None) -> str:
    supplied_text = str(supplied or "").casefold().strip().replace(" ", "_")
    if supplied_text in STAGE_CATEGORIES:
        return supplied_text
    text = re.sub(r"[^a-z0-9+]+", " ", str(original or "").casefold()).strip()
    if not text or text in {"na", "n a", "unknown", "not reported", "unclear"}:
        return "unclear"
    if "mixed" in text or re.search(
        r"localized.*(?:locally advanced|metastatic)|"
        r"(?:locally advanced|metastatic).*localized|"
        r"non\s*metastatic.*metastatic|metastatic.*non\s*metastatic",
        text,
    ):
        return "mixed"
    if re.search(r"locally advanced|t3|t4", text) and not re.search(r"m1|metast", text):
        return "locally_advanced"
    if re.search(r"non\s*metastatic|m0\b", text):
        return "nonmetastatic"
    if re.search(r"metastatic|metastases|\bm1[a-c]?\b|stage iv", text):
        return "metastatic"
    if re.search(r"localized|organ confined|\bt1\b|\bt2\b|stage i\b|stage ii\b", text):
        return "localized"
    return "unclear"


def normalize_disease_setting(value: Any, primary_treatment: Any = None) -> str:
    supplied = str(value or "").casefold().strip().replace(" ", "_")
    if supplied in DISEASE_SETTINGS:
        return supplied
    text = " ".join([str(value or ""), str(primary_treatment or "")]).casefold()
    if "mixed" in text or "multiple settings" in text:
        return "mixed"
    if re.search(r"active surveillance|watchful waiting", text):
        return "active_surveillance"
    if re.search(r"castration resistant|hormone refractory|crpc", text):
        return "castration_resistant"
    if re.search(r"metastatic hormone sensitive|metastatic castration sensitive|mhspc|mcspc", text):
        return "metastatic_hormone_sensitive"
    if re.search(r"biochemical recurrence|biochemical failure|rising psa", text):
        return "biochemical_recurrence"
    if re.search(r"prostatectomy|radical prostatectomy", text):
        return "after_prostatectomy"
    if re.search(r"radiotherapy|radiation therapy|external beam|brachytherapy", text):
        return "after_radiotherapy"
    return "unclear"


def normalize_comparison_type(
    value: Any,
    exposure_contrast: Any = None,
    reference_group: Any = None,
) -> str:
    supplied = str(value or "").casefold().strip().replace(" ", "_")
    if supplied in COMPARISON_TYPES:
        return supplied
    if supplied:
        raise ValueError(f"Unsupported comparison_type: {value!r}")
    text = " ".join(
        [str(exposure_contrast or ""), str(reference_group or "")]
    ).casefold()
    if re.search(r"per\s+(?:daily\s+)?serving|each (?:additional )?serving", text):
        return "per_serving"
    if re.search(r"per\s+(?:unit|gram|mg|g\b|sd\b|standard deviation)|one[- ]unit", text):
        return "per_unit"
    if re.search(r"continuous|per increment|dose response", text):
        return "continuous"
    if re.search(
        r"non[- ]?users?|never users?|\bno\b.{0,40}\b(?:use|users?|intake|supplements?)\b",
        text,
    ):
        return "user_vs_nonuser"
    if re.search(r"highest.*lowest|high.*low|top.*bottom|lowest (?:intake |exposure )?(?:group|category|quartile|quintile|tertile)", text):
        return "highest_vs_lowest"
    return "unclear"


def normalize_contrast_scale(
    comparison_type: str,
    supplied: Any = None,
    exposure_contrast: Any = None,
) -> str | None:
    if comparison_type in {"highest_vs_lowest", "user_vs_nonuser"}:
        return None
    text = _clean_text(supplied) or _clean_text(exposure_contrast)
    if not text:
        return None
    normalized = str(text).casefold().replace("μ", "u").replace("µ", "u")
    normalized = re.sub(r"\bmicrograms?\b|\bmcg\b", "ug", normalized)
    normalized = re.sub(r"\bmilligrams?\b", "mg", normalized)
    normalized = re.sub(r"\bgrams?\b", "g", normalized)
    normalized = re.sub(r"\bkilograms?\b", "kg", normalized)
    normalized = re.sub(r"\bmillilit(?:er|re)s?\b", "ml", normalized)
    amount_unit = re.search(
        r"(?:(?:per|each|every|increase(?: of)?|increment(?: of)?)\s+)?"
        r"(\d+(?:\.\d+)?)\s*(ug|mg|kg|g|ml|servings?)\b",
        normalized,
    )
    if amount_unit:
        amount = f"{float(amount_unit.group(1)):g}"
        unit = amount_unit.group(2)
        if unit.startswith("serving"):
            unit = "serving"
        period = re.search(r"(?:/|per\s+)(day|week|month|year)\b", normalized)
        return f"{amount} {unit}" + (f"/{period.group(1)}" if period else "")
    if "serving" in normalized:
        period = re.search(r"(?:/|per\s+)(day|week|month|year)\b", normalized)
        return "1 serving" + (f"/{period.group(1)}" if period else "")
    if comparison_type == "continuous":
        standard_deviation = re.search(r"(?:per|each)\s+(\d+(?:\.\d+)?)?\s*(?:sd|standard deviation)", normalized)
        if standard_deviation:
            amount = float(standard_deviation.group(1) or 1)
            return f"{amount:g} SD"
    if comparison_type in {"per_unit", "per_serving", "continuous"}:
        return None
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip() or None


def _contrast_key(row: dict[str, Any]) -> str:
    comparison_type = str(row.get("comparison_type") or "unclear")
    if comparison_type in {"highest_vs_lowest", "user_vs_nonuser"}:
        return comparison_type
    scale = _clean_text(row.get("contrast_scale"))
    if scale:
        return f"{comparison_type}:{scale.casefold()}"
    contrast = _comparison_text(row.get("exposure_contrast"))
    if comparison_type == "other" and contrast:
        return f"other:{contrast}"
    return f"{comparison_type}:unclear"


def outcome_definition_family(outcome: Any, definition: Any) -> str:
    canonical = normalize_outcome(outcome)
    if canonical == "overall_survival":
        return "all_cause_death"
    if canonical == "prostate_cancer_specific_survival":
        return "prostate_cancer_death"
    if canonical == "metastasis_free_survival":
        return "metastasis"
    if canonical == "biochemical_recurrence_free_survival":
        return "biochemical_recurrence"
    text = _comparison_text(definition)
    if canonical != "progression_free_survival":
        return "unclear"
    biochemical = bool(re.search(r"biochemical|psa", text))
    radiographic = bool(re.search(r"radiographic|imaging|scan", text))
    clinical = bool(re.search(r"\bclinical\b|symptomatic", text))
    if biochemical and not (radiographic or clinical):
        return "biochemical_progression"
    if radiographic and clinical:
        return "clinical_or_radiographic_progression"
    if radiographic:
        return "radiographic_progression"
    if clinical:
        return "clinical_progression"
    if re.search(r"treatment failure|new treatment|salvage therapy", text):
        return "treatment_failure"
    return "progression_unspecified"


def _adjustment_variables(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    elif value is None:
        raw = []
    else:
        raw = re.split(r"[,;]", str(value))
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _clean_text(item)
        if text and text.casefold() not in seen:
            result.append(text)
            seen.add(text.casefold())
    return result


def _estimate_identifier(row: dict[str, Any]) -> str:
    payload = "|".join(
        str(row.get(field) or "")
        for field in (
            "pmid",
            "cohort_name",
            "exposure",
            "exposure_timing",
            "outcome",
            "outcome_definition_family",
            "contrast_key",
            "effect_estimate",
            "reference_group",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def normalize_estimate(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize one report-level estimate without imputing event counts."""
    row = dict(raw or {})
    raw_outcome = row.get("outcome")
    raw_measure = _clean_text(row.get("effect_measure"))
    adjustments = _adjustment_variables(row.get("adjustment_variables"))
    adjusted_value = row.get("is_adjusted")
    if isinstance(adjusted_value, bool):
        is_adjusted = adjusted_value
    else:
        is_adjusted = bool(
            adjustments or re.search(r"\badjusted\b|\bmultivariable", raw_measure or "", re.I)
        )
    treatment = _clean_text(row.get("primary_treatment"), default="unclear")
    stage_original = _clean_text(row.get("prostate_cancer_stage"), default="unclear")
    supplied_stage = row.get("prostate_cancer_stage_category")
    if supplied_stage is None:
        supplied_stage = row.get("prostate_cancer_stage_group")
    if supplied_stage is not None:
        supplied_stage_key = str(supplied_stage).casefold().strip().replace(" ", "_")
        if supplied_stage_key not in STAGE_CATEGORIES:
            raise ValueError(
                f"Unsupported prostate_cancer_stage_category: {supplied_stage!r}"
            )
    supplied_setting = row.get("disease_setting")
    if supplied_setting is not None:
        supplied_setting_key = str(supplied_setting).casefold().strip().replace(" ", "_")
        if supplied_setting_key not in DISEASE_SETTINGS:
            raise ValueError(f"Unsupported disease_setting: {supplied_setting!r}")
    supplied_timing = row.get("exposure_timing")
    normalized_timing = normalize_exposure_timing(supplied_timing)
    timing_key = str(supplied_timing or "").casefold().strip().replace(" ", "_")
    if (
        supplied_timing is not None
        and normalized_timing == "unclear"
        and timing_key not in {"", "unclear", "unknown", "not_reported"}
    ):
        raise ValueError(f"Unsupported exposure_timing: {supplied_timing!r}")
    canonical_measure = normalize_effect_type(raw_measure)
    publication_year = _publication_year(row.get("publication_year"))
    ci_confidence_level = _number(row.get("ci_confidence_level"))
    if ci_confidence_level is not None and 0 < ci_confidence_level <= 1:
        ci_confidence_level *= 100.0
    comparison_type = normalize_comparison_type(
        row.get("comparison_type"),
        row.get("exposure_contrast"),
        row.get("reference_group"),
    )
    contrast_scale = normalize_contrast_scale(
        comparison_type,
        row.get("contrast_scale"),
        row.get("exposure_contrast"),
    )
    canonical_outcome = normalize_outcome(raw_outcome)
    definition = _clean_text(
        row.get("outcome_definition"), default=_clean_text(raw_outcome)
    )
    normalized = {
        "pmid": _clean_text(row.get("pmid"), default="Unknown"),
        "pmcid": _clean_text(row.get("pmcid")),
        "publication_year": publication_year,
        "cohort_name": _clean_text(row.get("cohort_name"), default="unclear"),
        "country": _clean_text(row.get("country")),
        "ethnicity": _clean_text(row.get("ethnicity")),
        "study_population": _clean_text(row.get("study_population")),
        "sample_size": _positive_int(row.get("sample_size")),
        "number_of_events": _positive_int(row.get("number_of_events")),
        "prostate_cancer_stage": stage_original,
        "prostate_cancer_stage_category": normalize_stage_category(
            stage_original, supplied_stage
        ),
        "disease_setting": normalize_disease_setting(supplied_setting, treatment),
        "primary_treatment": treatment,
        "exposure": _clean_text(row.get("exposure"), default="unclear"),
        "exposure_alias": _clean_text(
            row.get("exposure_alias"),
            default=_clean_text(row.get("exposure"), default="unclear"),
        ),
        "exposure_timing": normalized_timing,
        "exposure_assessment_method": _clean_text(
            row.get("exposure_assessment_method")
        ),
        "outcome": canonical_outcome,
        "outcome_definition": definition,
        "outcome_definition_family": outcome_definition_family(
            canonical_outcome, definition
        ),
        "follow_up": _clean_text(row.get("follow_up")),
        "effect_measure": canonical_measure or raw_measure,
        "effect_estimate": _number(row.get("effect_estimate")),
        "lower_ci": _number(row.get("lower_ci")),
        "upper_ci": _number(row.get("upper_ci")),
        "ci_confidence_level": ci_confidence_level,
        "exposure_contrast": _clean_text(row.get("exposure_contrast")),
        "comparison_type": comparison_type,
        "contrast_scale": contrast_scale,
        "needs_inversion": row.get("needs_inversion") is True,
        "reference_group": _clean_text(row.get("reference_group")),
        "adjustment_variables": adjustments,
        "is_adjusted": is_adjusted,
        "evidence": row.get("evidence") if isinstance(row.get("evidence"), dict) else {},
    }
    normalized["contrast_key"] = _contrast_key(normalized)
    normalized["estimate_id"] = _clean_text(row.get("estimate_id")) or _estimate_identifier(normalized)
    return normalized


def classify_candidate(article: dict[str, Any]) -> tuple[bool, str | None, str]:
    """Apply only high-certainty deterministic exclusions before/after LLM review."""
    title = str(article.get("title") or "")
    abstract = str(article.get("abstract") or "")
    publication_types = [str(value) for value in article.get("publication_types", [])]
    publication_types.extend(
        str(article.get(field) or "") for field in ("publication_type", "study_type")
    )
    publication_text = " ".join(publication_types).casefold()
    title_text = title.casefold()
    all_text = " ".join(
        [
            title,
            abstract,
            str(article.get("population_status") or ""),
            str(article.get("outcome") or ""),
            str(article.get("study_type") or ""),
        ]
    ).casefold()
    if (
        re.search(r"meta.?analysis|\breview\b", publication_text)
        or re.search(
            r"\b(meta.?analysis|systematic review|scoping review|umbrella review|narrative review)\b",
            title_text,
        )
    ):
        return (
            False,
            "S2-REVIEW-META-ANALYSIS",
            "This report is an existing review or meta-analysis rather than an eligible primary study.",
        )
    incidence_marker = bool(
        re.search(
            r"\bprostate cancer incidence\b|\bincident prostate cancer\b|"
            r"\brisk of (?:developing )?prostate cancer\b|\bdevelop(?:ing|ed)? prostate cancer\b|"
            r"\bprostate cancer risk\b",
            all_text,
        )
        or str(article.get("outcome") or "").casefold() == "incidence"
    )
    survival_marker = bool(
        re.search(
            r"overall survival|all[- ]cause mortal|prostate cancer[- ]specific|"
            r"cancer[- ]specific (?:survival|mortal)|progression[- ]free|"
            r"metastasis[- ]free|biochemical recurrence|recurrence[- ]free|"
            r"fatal prostate cancer|survival after|mortality (?:after|among)|"
            r"death from prostate cancer|\bsurviv",
            all_text,
        )
    )
    if incidence_marker and not survival_marker:
        return (
            False,
            "S2-INCIDENCE-ONLY",
            "This study evaluates prostate cancer incidence in participants without a prior diagnosis rather than survival after diagnosis.",
        )
    return (
        True,
        None,
        "The citation is not deterministically ineligible and should proceed through lenient survival screening.",
    )


def group_estimates(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Group estimates by the three dimensions that may never be crossed."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        row = normalize_estimate(raw)
        key = (
            str(row.get("exposure") or "unclear"),
            str(row.get("exposure_timing") or "unclear"),
            str(row.get("outcome") or "unclear"),
        )
        grouped[key].append(row)
    return dict(grouped)


def _cohort_keys(row: dict[str, Any]) -> set[str]:
    name = str(row.get("cohort_name") or "").strip()
    if not name or name.casefold() in {"unclear", "unknown", "not reported", "na", "n/a"}:
        return {f"pmid:{row.get('pmid')}"}
    keys = {re.sub(r"[^a-z0-9]+", "", name.casefold())}
    parenthetical = re.findall(r"\(([A-Z][A-Z0-9-]{1,12})\)", name)
    keys.update(value.casefold().replace("-", "") for value in parenthetical)
    tokens = re.findall(r"[A-Za-z0-9]+", re.sub(r"\([^)]*\)", "", name))
    if len(tokens) >= 2:
        stopwords = {"a", "an", "and", "for", "in", "of", "the", "up"}
        initials = "".join(token[0] for token in tokens if token.casefold() not in stopwords)
        if len(initials) >= 2:
            keys.add(initials.casefold())
        generic = stopwords | {"cohort", "project", "registry", "study", "trial"}
        short_initials = "".join(token[0] for token in tokens if token.casefold() not in generic)
        if len(short_initials) >= 2:
            keys.add(short_initials.casefold())
    return {key for key in keys if key}


def _selection_rank(row: dict[str, Any]) -> tuple[Any, ...]:
    values_complete = all(
        _number(row.get(field)) is not None
        for field in ("effect_estimate", "lower_ci", "upper_ci")
    )
    return (
        bool(row.get("is_adjusted")),
        normalize_effect_type(row.get("effect_measure")) == "HR",
        values_complete,
        len(_adjustment_variables(row.get("adjustment_variables"))),
        row.get("number_of_events") is not None,
        int(row.get("number_of_events") or 0),
        int(row.get("sample_size") or 0),
        int(row.get("publication_year") or 0),
        str(row.get("pmid") or ""),
    )


def _age_interval(value: Any) -> tuple[float, float] | None:
    text = _comparison_text(value)
    if not text:
        return None
    range_match = re.search(
        r"(?:aged?|ages?)\s*(\d{2,3})\s*(?:to|through|-)\s*(\d{2,3})",
        text,
    )
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))
    upper = re.search(
        r"(?:age[ds]?\s*)?(?:under|younger than|less than|below)\s*(\d{2,3})",
        text,
    )
    lower = re.search(
        r"(?:age[ds]?\s*)?(?:at least|or older|older than|greater than|over)\s*(\d{2,3})|"
        r"(?:age[ds]?\s*)?(\d{2,3})\s*(?:or older|and older|plus)",
        text,
    )
    if upper:
        return 0.0, float(upper.group(1)) - 1e-6
    if lower:
        threshold = lower.group(1) or lower.group(2)
        return float(threshold), math.inf
    return None


def _clearly_disjoint_populations(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_age = _age_interval(left.get("study_population"))
    right_age = _age_interval(right.get("study_population"))
    if left_age and right_age and (
        left_age[1] < right_age[0] or right_age[1] < left_age[0]
    ):
        return True
    nonmetastatic = {"localized", "locally_advanced", "nonmetastatic"}
    left_stage = str(left.get("prostate_cancer_stage_category") or "unclear")
    right_stage = str(right.get("prostate_cancer_stage_category") or "unclear")
    if (
        left_stage == "metastatic" and right_stage in nonmetastatic
    ) or (
        right_stage == "metastatic" and left_stage in nonmetastatic
    ):
        return True
    advanced_settings = {"metastatic_hormone_sensitive", "castration_resistant"}
    definitive_settings = {
        "active_surveillance",
        "after_prostatectomy",
        "after_radiotherapy",
    }
    left_setting = str(left.get("disease_setting") or "unclear")
    right_setting = str(right.get("disease_setting") or "unclear")
    return bool(
        left_setting in advanced_settings and right_setting in definitive_settings
        or right_setting in advanced_settings and left_setting in definitive_settings
    )


def _exclusion(row: dict[str, Any], code: str, reason: str) -> dict[str, Any]:
    return {
        "pmid": str(row.get("pmid") or "Unknown"),
        "code": code,
        "reason": dual_screening.one_sentence(reason),
        "estimate_id": row.get("estimate_id"),
        "exposure": row.get("exposure"),
        "exposure_timing": row.get("exposure_timing"),
        "outcome": row.get("outcome"),
    }


def select_independent_estimates(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Retain one estimate per cohort inside each compatible analysis stratum."""
    kept: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for key, members in group_estimates(rows).items():
        by_comparison: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in members:
            by_comparison[
                (
                    str(row.get("contrast_key") or "unclear:unclear"),
                    str(row.get("outcome_definition_family") or "unclear"),
                )
            ].append(row)
        for comparison_rows in by_comparison.values():
            cohort_clusters: list[tuple[set[str], list[dict[str, Any]]]] = []
            for row in comparison_rows:
                keys = _cohort_keys(row)
                matching = [
                    index
                    for index, (known, clustered_rows) in enumerate(cohort_clusters)
                    if known & keys
                    and not all(
                        _clearly_disjoint_populations(row, existing)
                        for existing in clustered_rows
                    )
                ]
                if not matching:
                    cohort_clusters.append((set(keys), [row]))
                    continue
                target = matching[0]
                cohort_clusters[target][0].update(keys)
                cohort_clusters[target][1].append(row)
                for index in reversed(matching[1:]):
                    cohort_clusters[target][0].update(cohort_clusters[index][0])
                    cohort_clusters[target][1].extend(cohort_clusters[index][1])
                    cohort_clusters.pop(index)
            for _, cohort_rows in cohort_clusters:
                ranked = sorted(cohort_rows, key=_selection_rank, reverse=True)
                winner = ranked[0]
                kept.append(winner)
                for duplicate in ranked[1:]:
                    cohort = duplicate.get("cohort_name") or winner.get("cohort_name") or "the same cohort"
                    exclusions.append(
                        _exclusion(
                            duplicate,
                            "A-OVERLAPPING-COHORT",
                            f"This report overlaps the {cohort} estimate retained from PMID {winner.get('pmid')} in the same exposure, timing, outcome, and contrast analysis",
                        )
                    )
    kept.sort(
        key=lambda row: (
            str(row.get("exposure")).casefold(),
            str(row.get("exposure_timing")),
            str(row.get("outcome")),
            str(row.get("pmid")),
        )
    )
    return kept, exclusions


def log_hr_and_se(row: dict[str, Any]) -> tuple[float, float]:
    """Return log(HR) and its SE from a two-sided 95% confidence interval."""
    normalized = normalize_estimate(row)
    if normalize_effect_type(normalized.get("effect_measure")) != "HR":
        raise ValueError("Only hazard ratios can be transformed for survival meta-analysis")
    confidence_level = _number(normalized.get("ci_confidence_level"))
    if confidence_level is None or not math.isclose(confidence_level, 95.0, abs_tol=0.05):
        raise ValueError("A reported 95% confidence interval is required")
    estimate = _number(normalized.get("effect_estimate"))
    lower = _number(normalized.get("lower_ci"))
    upper = _number(normalized.get("upper_ci"))
    if (
        estimate is None
        or lower is None
        or upper is None
        or estimate <= 0
        or lower <= 0
        or upper <= 0
        or lower > estimate
        or estimate > upper
        or lower >= upper
    ):
        raise ValueError("A positive coherent HR and 95% confidence interval are required")
    standard_error = (math.log(upper) - math.log(lower)) / 3.92
    if not math.isfinite(standard_error) or standard_error <= 0:
        raise ValueError("The HR confidence interval does not yield a positive standard error")
    log_estimate = math.log(estimate)
    if normalized.get("needs_inversion") is True:
        log_estimate = -log_estimate
    return log_estimate, standard_error


def _pool_random_effects(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) < 2:
        raise ValueError("At least two independent studies are required for meta-analysis")
    transformed = [log_hr_and_se(row) for row in rows]
    effects = [value[0] for value in transformed]
    variances = [value[1] ** 2 for value in transformed]
    fixed_weights = [1.0 / variance for variance in variances]
    fixed_mean = sum(weight * effect for weight, effect in zip(fixed_weights, effects)) / sum(fixed_weights)
    q = sum(
        weight * (effect - fixed_mean) ** 2
        for weight, effect in zip(fixed_weights, effects)
    )
    degrees_of_freedom = len(rows) - 1
    c_value = sum(fixed_weights) - (
        sum(weight**2 for weight in fixed_weights) / sum(fixed_weights)
    )
    tau_squared = max(0.0, (q - degrees_of_freedom) / c_value) if c_value > 0 else 0.0
    random_weights = [1.0 / (variance + tau_squared) for variance in variances]
    pooled_log_hr = sum(
        weight * effect for weight, effect in zip(random_weights, effects)
    ) / sum(random_weights)
    pooled_se = math.sqrt(1.0 / sum(random_weights))
    i_squared = max(0.0, (q - degrees_of_freedom) / q * 100.0) if q > 0 else 0.0
    return {
        "k": len(rows),
        "model": "DerSimonian-Laird random effects",
        "effect_scale": "log hazard ratio",
        "pooled_log_hr": pooled_log_hr,
        "pooled_log_hr_se": pooled_se,
        "pooled_hr": math.exp(pooled_log_hr),
        "lower_ci": math.exp(pooled_log_hr - 1.96 * pooled_se),
        "upper_ci": math.exp(pooled_log_hr + 1.96 * pooled_se),
        "q": q,
        "q_degrees_of_freedom": degrees_of_freedom,
        "tau_squared": tau_squared,
        "i_squared": i_squared,
        "studies": [
            {
                "pmid": row.get("pmid"),
                "cohort_name": row.get("cohort_name"),
                "publication_year": row.get("publication_year"),
                "hazard_ratio": (
                    1.0 / float(row["effect_estimate"])
                    if row.get("needs_inversion") is True
                    else row.get("effect_estimate")
                ),
                "lower_ci": (
                    1.0 / float(row["upper_ci"])
                    if row.get("needs_inversion") is True
                    else row.get("lower_ci")
                ),
                "upper_ci": (
                    1.0 / float(row["lower_ci"])
                    if row.get("needs_inversion") is True
                    else row.get("upper_ci")
                ),
                "reported_hazard_ratio": row.get("effect_estimate"),
                "reported_lower_ci": row.get("lower_ci"),
                "reported_upper_ci": row.get("upper_ci"),
                "inverted_for_analysis": row.get("needs_inversion") is True,
                "log_hr": effect,
                "standard_error": math.sqrt(variance),
            }
            for row, effect, variance in zip(rows, effects, variances)
        ],
    }


def _analysis_id(
    exposure: str,
    timing: str,
    outcome: str,
    contrast_key: str,
    outcome_family: str,
    stratification: str | None = None,
    stratum: str | None = None,
) -> str:
    parts = [
        exposure,
        timing,
        outcome,
        contrast_key,
        outcome_family,
        stratification or "overall",
        stratum or "all",
    ]
    slug = "_".join(re.sub(r"[^a-z0-9]+", "_", part.casefold()).strip("_") for part in parts)
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:8]
    return f"{slug[:120]}_{digest}"


def run_meta_analyses(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pool adjusted HRs without crossing exposure, timing, or outcome strata."""
    eligible: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for raw in rows:
        row = normalize_estimate(raw)
        if row.get("outcome") not in SURVIVAL_OUTCOMES:
            exclusions.append(
                _exclusion(row, "A-INCOMPATIBLE-OUTCOME", "This estimate has no supported time-to-event survival outcome")
            )
            continue
        if normalize_effect_type(row.get("effect_measure")) != "HR":
            exclusions.append(
                _exclusion(row, "A-NON-HR", "This estimate is not a hazard ratio and cannot be combined with survival hazard ratios")
            )
            continue
        if not row.get("is_adjusted"):
            exclusions.append(
                _exclusion(row, "A-UNADJUSTED-HR", "This hazard ratio is unadjusted and is not used in the primary adjusted analysis")
            )
            continue
        if row.get("comparison_type") == "unclear":
            exclusions.append(
                _exclusion(
                    row,
                    "A-UNCLEAR-CONTRAST",
                    "This estimate has no compatible exposure contrast and is not pooled with differently scaled comparisons",
                )
            )
            continue
        if str(row.get("contrast_key") or "").endswith(":unclear"):
            exclusions.append(
                _exclusion(
                    row,
                    "A-UNCLEAR-CONTRAST-SCALE",
                    "This continuous estimate has no recoverable increment scale and cannot be pooled with differently scaled contrasts",
                )
            )
            continue
        confidence_level = _number(row.get("ci_confidence_level"))
        if confidence_level is None or not math.isclose(confidence_level, 95.0, abs_tol=0.05):
            exclusions.append(
                _exclusion(
                    row,
                    "A-NON-95-CI",
                    "This estimate does not report a 95% confidence interval and is not transformed with the 95% CI formula",
                )
            )
            continue
        try:
            log_hr_and_se(row)
        except ValueError:
            exclusions.append(
                _exclusion(row, "A-INCOMPLETE-HR", "This adjusted hazard ratio lacks a positive coherent estimate and 95% confidence interval")
            )
            continue
        eligible.append(row)

    independent, overlap_exclusions = select_independent_estimates(eligible)
    exclusions.extend(overlap_exclusions)
    analyses: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = []

    compatible_groups: dict[
        tuple[str, str, str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for row in independent:
        compatible_groups[
            (
                str(row.get("exposure") or "unclear"),
                str(row.get("exposure_timing") or "unclear"),
                str(row.get("outcome") or "unclear"),
                str(row.get("contrast_key") or "unclear:unclear"),
                str(row.get("outcome_definition_family") or "unclear"),
            )
        ].append(row)

    for (
        exposure,
        timing,
        outcome,
        contrast_key,
        outcome_family,
    ), members in compatible_groups.items():
        comparison_type = str(members[0].get("comparison_type") or "unclear")
        contrast_scale = members[0].get("contrast_scale")
        if len(members) < 2:
            reason = dual_screening.one_sentence(
                "Only one independent cohort is available, so no pooled meta-analysis is calculated"
            )
            insufficient.append(
                {
                    "exposure": exposure,
                    "exposure_timing": timing,
                    "outcome": outcome,
                    "comparison_type": comparison_type,
                    "contrast_scale": contrast_scale,
                    "contrast_key": contrast_key,
                    "outcome_definition_family": outcome_family,
                    "k": len(members),
                    "pmids": [str(row.get("pmid")) for row in members],
                    "reason": reason,
                }
            )
            for row in members:
                exclusions.append(_exclusion(row, "A-INSUFFICIENT-STUDIES", reason))
            continue
        pooled = _pool_random_effects(members)
        pooled.update(
            {
                "analysis_id": _analysis_id(
                    exposure,
                    timing,
                    outcome,
                    contrast_key,
                    outcome_family,
                ),
                "exposure": exposure,
                "exposure_timing": timing,
                "outcome": outcome,
                "comparison_type": comparison_type,
                "contrast_scale": contrast_scale,
                "contrast_key": contrast_key,
                "outcome_definition_family": outcome_family,
                "stratification": None,
                "stratum": None,
            }
        )
        analyses.append(pooled)

        for field in ("prostate_cancer_stage_category", "disease_setting"):
            strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in members:
                value = str(row.get(field) or "unclear")
                if value != "unclear":
                    strata[value].append(row)
            for stratum, stratum_rows in strata.items():
                if len(stratum_rows) < 2:
                    continue
                subgroup = _pool_random_effects(stratum_rows)
                subgroup.update(
                    {
                        "analysis_id": _analysis_id(
                            exposure,
                            timing,
                            outcome,
                            contrast_key,
                            outcome_family,
                            field,
                            stratum,
                        ),
                        "exposure": exposure,
                        "exposure_timing": timing,
                        "outcome": outcome,
                        "comparison_type": comparison_type,
                        "contrast_scale": contrast_scale,
                        "contrast_key": contrast_key,
                        "outcome_definition_family": outcome_family,
                        "stratification": field,
                        "stratum": stratum,
                    }
                )
                analyses.append(subgroup)

    analyses.sort(
        key=lambda item: (
            str(item["exposure"]).casefold(),
            item["exposure_timing"],
            item["outcome"],
            str(item.get("comparison_type") or ""),
            str(item.get("contrast_key") or ""),
            str(item.get("outcome_definition_family") or ""),
            str(item.get("stratification") or ""),
            str(item.get("stratum") or ""),
        )
    )
    exclusions.sort(
        key=lambda item: (
            str(item.get("pmid")),
            str(item.get("exposure")),
            str(item.get("exposure_timing")),
            str(item.get("outcome")),
            str(item.get("code")),
        )
    )
    return {
        "analyses": analyses,
        "insufficient_groups": insufficient,
        "exclusions": exclusions,
    }


def needs_quantitative_full_text(context: dict[str, Any]) -> bool:
    """Gate conditional Luna extraction on inclusion, PMCID, and missing data."""
    is_flat_estimate = "estimates" not in context and any(
        field in context for field in ("effect_measure", "effect_estimate", "lower_ci", "upper_ci")
    )
    decision = str(context.get("decision") or context.get("eligibility") or "").upper()
    if is_flat_estimate and not decision:
        decision = "INCLUDE"
    if decision != "INCLUDE" or not _clean_text(context.get("pmcid")):
        return False
    estimates = [context] if is_flat_estimate else context.get("estimates")
    if not isinstance(estimates, list) or not estimates:
        return True
    for raw in estimates:
        if not isinstance(raw, dict):
            return True
        normalized = normalize_estimate(raw)
        if normalized.get("sample_size") is None or normalized.get("number_of_events") is None:
            return True
        if not normalized.get("is_adjusted"):
            return True
        if normalize_effect_type(normalized.get("effect_measure")) != "HR":
            return True
        if normalized.get("comparison_type") == "unclear":
            return True
        if str(normalized.get("contrast_key") or "").endswith(":unclear"):
            return True
        try:
            log_hr_and_se(normalized)
        except ValueError:
            return True
    return False


def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


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


def _estimate_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "pmid": {"type": "string"},
        "pmcid": _nullable("string"),
        "publication_year": _nullable("integer"),
        "cohort_name": _nullable("string"),
        "country": _nullable("string"),
        "ethnicity": _nullable("string"),
        "study_population": _nullable("string"),
        "sample_size": _nullable("integer"),
        "number_of_events": _nullable("integer"),
        "prostate_cancer_stage": _nullable("string"),
        "prostate_cancer_stage_category": {
            "type": "string",
            "enum": list(STAGE_CATEGORIES),
        },
        "disease_setting": {"type": "string", "enum": list(DISEASE_SETTINGS)},
        "primary_treatment": _nullable("string"),
        "exposure": {"type": "string"},
        "exposure_alias": _nullable("string"),
        "exposure_timing": {"type": "string", "enum": list(EXPOSURE_TIMINGS)},
        "exposure_assessment_method": _nullable("string"),
        "outcome": {"type": "string", "enum": list(SURVIVAL_OUTCOMES)},
        "outcome_definition": _nullable("string"),
        "follow_up": _nullable("string"),
        "effect_measure": _nullable("string"),
        "effect_estimate": _nullable("number"),
        "lower_ci": _nullable("number"),
        "upper_ci": _nullable("number"),
        "ci_confidence_level": _nullable("number"),
        "exposure_contrast": _nullable("string"),
        "comparison_type": {"type": "string", "enum": list(COMPARISON_TYPES)},
        "contrast_scale": _nullable("string"),
        "needs_inversion": {"type": "boolean"},
        "reference_group": _nullable("string"),
        "adjustment_variables": {"type": "array", "items": {"type": "string"}},
        "is_adjusted": {"type": "boolean"},
        "evidence": _evidence_schema(),
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def response_schema(stage: str) -> dict[str, Any]:
    if stage == "first":
        result_properties = {
            "context_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["INCLUDE", "EXCLUDE"]},
            "exclusion_code": {
                "type": ["string", "null"],
                "enum": [None, *FIRST_EXCLUSION_CODES],
            },
            "reason": {"type": "string"},
        }
    elif stage in {"second", "full_text"}:
        allowed_decisions = ["INCLUDE"] if stage == "full_text" else ["INCLUDE", "EXCLUDE"]
        allowed_codes: list[str | None] = [None] if stage == "full_text" else [None, *SECOND_EXCLUSION_CODES]
        result_properties = {
            "context_id": {"type": "string"},
            "decision": {"type": "string", "enum": allowed_decisions},
            "exclusion_code": {
                "type": ["string", "null"],
                "enum": allowed_codes,
            },
            "reason": {"type": "string"},
            "estimates": {"type": "array", "items": _estimate_schema()},
        }
    else:
        raise ValueError(f"Unsupported screening stage: {stage}")
    result = {
        "type": "object",
        "properties": result_properties,
        "required": list(result_properties),
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {"results": {"type": "array", "items": result}},
        "required": ["results"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "name": f"prostate_survival_{stage}",
        "strict": True,
        "schema": schema,
    }


FIRST_SYSTEM_PROMPT = """You are the first, deliberately lenient reviewer for a prostate-cancer survival evidence review. Include an original human study whenever it may involve people already diagnosed with prostate cancer, a dietary/supplement/nutritional exposure, and a time-to-event survival, mortality, progression, metastasis, or biochemical-recurrence outcome. Pass uncertainty to the detailed reviewer. Exclude only clearly ineligible citations, incidence-only studies in initially cancer-free people, and existing reviews/meta-analyses. Return one concise sentence per decision."""

SECOND_SYSTEM_PROMPT = """You are the careful eligibility and extraction reviewer for a prostate-cancer survival meta-analysis. Include only original human studies whose participants already had prostate cancer and that evaluate a dietary, supplement, or nutritional exposure in relation to a supported time-to-event outcome. Exclude incidence-only cohorts, reviews/meta-analyses, nonprimary research, anticancer treatments presented as exposures, and incompatible outcomes. Extract every separable estimate: prediagnosis and postdiagnosis estimates must be separate rows, as must different outcomes and exposure contrasts. Prefer the most fully adjusted HR, but retain reported non-HR and unadjusted estimates for the audit. Record the CI confidence level, exact comparison contrast and reference group, standardized continuous increment or serving scale, study population, and whether the reported contrast must be inverted to match the standard higher-exposure-versus-reference direction. Preserve verbatim stage, treatment, outcome definition, follow-up, exposure alias, and field-level source evidence. Never invent values."""

FULL_TEXT_SYSTEM_PROMPT = """You are the conditional full-text quantitative extractor. Sol has already established eligibility, so the decision must remain INCLUDE. Use the supplied PMC article only to complete or correct key study and estimate fields. Return every separable exposure-timing, outcome, and exposure-contrast estimate, including the reported CI confidence level and standardized continuous increment or serving scale, preserve exact wording, and never infer or invent an HR, confidence interval, sample size, or event count that the article does not report."""


def screening_protocol_digest(stage: str) -> str:
    system = {
        "first": FIRST_SYSTEM_PROMPT,
        "second": SECOND_SYSTEM_PROMPT,
        "full_text": FULL_TEXT_SYSTEM_PROMPT,
    }[stage]
    payload = system + "\n" + json.dumps(
        response_schema(stage)["schema"], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _call_model(
    runtime: dual_screening.TrackRuntime,
    stage: str,
    system: str,
    prompt: str,
    usage: dict[str, Any],
    exposure_weights: dict[str, int],
) -> dict[str, Any]:
    schema = response_schema(stage)
    contracted = (
        prompt
        + "\n\nReturn exactly one JSON object matching this schema, with null for unavailable values and no prose:\n"
        + json.dumps(schema["schema"], ensure_ascii=False, separators=(",", ":"))
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = runtime.client.responses.create(
                model=runtime.model_for(stage),
                instructions=system,
                input=contracted,
                text={"format": schema},
                store=False,
            )
            dual_screening._add_usage(usage, response, exposure_weights)
            return dual_screening.json_from_text(response.output_text)
        except Exception as exc:  # pragma: no cover - integration retry path
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"{stage} survival screening failed after retries: {last_error}") from last_error


def _validated_results(
    payload: dict[str, Any],
    expected_ids: Iterable[str],
    stage: str,
) -> list[dict[str, Any]]:
    expected = list(expected_ids)
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("Screening response is missing its results array")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise ValueError("Every screening result must be an object")
        context_id = str(raw.get("context_id") or "")
        if not context_id or context_id in by_id:
            raise ValueError("Screening response has a missing or duplicate context_id")
        by_id[context_id] = raw
    if set(by_id) != set(expected):
        raise ValueError("Screening response context IDs do not exactly match the requested batch")
    results = []
    for context_id in expected:
        raw = dict(by_id[context_id])
        decision = str(raw.get("decision") or "").upper()
        if decision not in {"INCLUDE", "EXCLUDE"}:
            raise ValueError(f"Invalid screening decision for {context_id}")
        if stage == "full_text" and decision != "INCLUDE":
            raise ValueError("Conditional full-text extraction cannot reverse Sol eligibility")
        raw["decision"] = decision
        raw["reason"] = dual_screening.one_sentence(raw.get("reason"))
        if decision == "EXCLUDE" and not raw.get("exclusion_code"):
            raw["exclusion_code"] = (
                "S1-NOT-SURVIVAL" if stage == "first" else "S2-OTHER"
            )
        if decision == "INCLUDE":
            raw["exclusion_code"] = None
        if stage != "first":
            estimates = raw.get("estimates")
            if not isinstance(estimates, list):
                raise ValueError(f"Detailed result {context_id} has no estimates array")
        results.append(raw)
    return results


def _usage_packet() -> dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "by_exposure": {},
    }


def _batch(values: list[Any], size: int) -> Iterable[list[Any]]:
    if size <= 0:
        raise ValueError("Screening batch size must be positive")
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _context_id(pmid: str, exposure: str) -> str:
    return f"{pmid}|{exposure}|Prostate cancer survival"


def _matched_aliases(article: dict[str, Any], terms: list[str], exposure: str) -> list[str]:
    text = " ".join([str(article.get("title") or ""), str(article.get("abstract") or "")]).casefold()
    matches = [term for term in terms if term.casefold() in text]
    return matches or [exposure]


def _candidate_index(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for article in packet.get("articles", []):
        for context in article.get("contexts", []):
            index[str(context["context_id"])] = {"article": article, "context": context}
    return index


def discover(
    output_dir: Path,
    start_date: str | None,
    end_date: str,
    selected_exposures: list[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Discover survival candidates for every selected combined-catalog exposure."""
    path = output_dir / "candidates.json"
    catalog = load_exposure_catalog(selected_exposures)
    vocabulary_digest = monthly.exposure_vocabulary_digest(catalog)
    discovery_protocol_digest = hashlib.sha256(
        SURVIVAL_CANCER_QUERY.encode("utf-8")
    ).hexdigest()
    if path.is_file() and not force:
        existing = monthly.load_json(path, {})
        reusable = bool(
            existing.get("schema_version") == 1
            and existing.get("protocol") == "prostate_cancer_survival"
            and existing.get("publication_window")
            == {"start_date": start_date, "end_date": end_date}
            and existing.get("selected_exposures") == list(catalog)
            and existing.get("vocabulary_digest") == vocabulary_digest
            and existing.get("discovery_protocol_digest")
            == discovery_protocol_digest
        )
        if reusable:
            return existing
        raise RuntimeError(
            "Existing survival candidates do not match this date window and exposure vocabulary; use --force or a separate output directory"
        )
    api_key_status = monthly.configure_entrez(validate_api_key=False)
    contexts_by_pmid: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    search_audit: dict[str, Any] = {}
    for exposure, terms in catalog.items():
        query = monthly.build_query(terms, SURVIVAL_CANCER_QUERY)
        ids = monthly.entrez_search(query, start_date, end_date)
        search_audit[exposure] = {
            "query": query,
            "pmid_count": len(ids),
            "entrez": ids.audit,
        }
        for pmid in ids:
            contexts_by_pmid[str(pmid)][exposure] = {
                "context_id": _context_id(str(pmid), exposure),
                "exposure": exposure,
                "search_terms": terms,
            }
    pmids = sorted(contexts_by_pmid, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))
    records, fetch_requests = monthly.entrez_fetch(pmids) if pmids else ({}, 0)
    missing_pmids = sorted(set(pmids).difference(records))
    if missing_pmids:
        raise RuntimeError(
            "NCBI EFetch omitted survival candidate PMIDs, so no incomplete packet was written: "
            + ", ".join(missing_pmids)
        )
    articles = []
    for pmid in pmids:
        article = records.get(pmid)
        if not article:
            continue
        selected_date = monthly.select_in_window_publication_date(article, start_date, end_date)
        if selected_date is None:
            continue
        saved = dict(article)
        saved["selected_publication_date"] = selected_date
        saved_contexts = []
        for exposure, context in sorted(contexts_by_pmid[pmid].items(), key=lambda item: item[0].casefold()):
            item = dict(context)
            item["matched_aliases"] = _matched_aliases(article, context["search_terms"], exposure)
            saved_contexts.append(item)
        saved["contexts"] = saved_contexts
        articles.append(saved)
    packet = {
        "schema_version": 1,
        "protocol": "prostate_cancer_survival",
        "created_at": dual_screening.utc_now(),
        "publication_window": {"start_date": start_date, "end_date": end_date},
        "catalog": "combined",
        "selected_exposures": list(catalog),
        "vocabulary_digest": vocabulary_digest,
        "discovery_protocol_digest": discovery_protocol_digest,
        "canonical_exposure_count": len(catalog),
        "models": {"first": FIRST_MODEL, "second": SECOND_MODEL, "full_text": FULL_TEXT_MODEL},
        "api_key_status": api_key_status,
        "entrez_fetch_requests": fetch_requests,
        "missing_pmids": [],
        "search_audit": search_audit,
        "articles": articles,
    }
    monthly.save_json(path, packet)
    return packet


def _compact_context(item: dict[str, Any]) -> dict[str, Any]:
    article, context = item["article"], item["context"]
    return {
        "context_id": context["context_id"],
        "pmid": article.get("pmid"),
        "pmcid": article.get("pmcid") or None,
        "exposure": context["exposure"],
        "matched_aliases": context.get("matched_aliases", []),
        "title": article.get("title"),
        "abstract": article.get("abstract"),
        "mesh": article.get("mesh", []),
        "publication_types": article.get("publication_types", []),
        "published_date": article.get("selected_publication_date") or article.get("published_date"),
        "pubmed_url": article.get("pubmed_url"),
    }


def first_screen(
    output_dir: Path,
    *,
    batch_size: int = 20,
    force: bool = False,
) -> dict[str, Any]:
    path = output_dir / "first_screen.json"
    candidate_path = output_dir / "candidates.json"
    candidate_sha256 = _file_sha256(candidate_path)
    protocol_digest = screening_protocol_digest("first")
    reusable = _reuse_bound_packet(
        path,
        {
            "candidate_sha256": candidate_sha256,
            "model": FIRST_MODEL,
            "protocol_digest": protocol_digest,
        },
        force=force,
    )
    if reusable is not None:
        return reusable
    candidates = monthly.load_json(candidate_path, {})
    index = _candidate_index(candidates)
    usage = _usage_packet()
    results: list[dict[str, Any]] = []
    items = [_compact_context(index[key]) for key in sorted(index)]
    runtime = dual_screening.configure_runtime("chatgpt", REPO_ROOT) if items else None
    for values in _batch(items, batch_size):
        expected = [item["context_id"] for item in values]
        payload = _call_model(
            runtime,
            "first",
            FIRST_SYSTEM_PROMPT,
            "Screen these citation contexts leniently:\n" + json.dumps(values, ensure_ascii=False),
            usage,
            {item["exposure"]: 1 for item in values},
        )
        batch_results = _validated_results(payload, expected, "first")
        for result in batch_results:
            article = index[result["context_id"]]["article"]
            eligible, code, reason = classify_candidate(article)
            if not eligible:
                result.update(decision="EXCLUDE", exclusion_code=code, reason=dual_screening.one_sentence(reason))
        results.extend(batch_results)
    packet = {
        "schema_version": 1,
        "created_at": dual_screening.utc_now(),
        "model": FIRST_MODEL,
        "protocol_digest": protocol_digest,
        "role": "lenient first screen",
        "candidate_sha256": candidate_sha256,
        "usage": usage,
        "results": results,
    }
    monthly.save_json(path, packet)
    return packet


def _screening_map(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("context_id")): item for item in packet.get("results", [])}


def _prepare_estimates(
    estimates: Iterable[dict[str, Any]],
    article: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    prepared = []
    aliases = context.get("matched_aliases") or [context.get("exposure")]
    for raw in estimates:
        row = dict(raw)
        row["pmid"] = str(article.get("pmid") or row.get("pmid") or "Unknown")
        row["pmcid"] = article.get("pmcid") or row.get("pmcid") or None
        row["publication_year"] = _publication_year(
            article.get("selected_publication_date")
            or article.get("published_date")
            or row.get("publication_year")
        )
        row["exposure"] = context.get("exposure")
        if not _clean_text(row.get("exposure_alias")):
            row["exposure_alias"] = aliases[0]
        prepared.append(normalize_estimate(row))
    return prepared


def _comparison_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _estimate_match_score(left: dict[str, Any], right: dict[str, Any]) -> int:
    if left.get("exposure_timing") != right.get("exposure_timing"):
        return -1
    if left.get("outcome") != right.get("outcome"):
        return -1
    left_comparison = str(left.get("comparison_type") or "unclear")
    right_comparison = str(right.get("comparison_type") or "unclear")
    if (
        left_comparison != "unclear"
        and right_comparison != "unclear"
        and left_comparison != right_comparison
    ):
        return -1
    score = 8
    if left_comparison == right_comparison:
        score += 3
    if normalize_effect_type(left.get("effect_measure")) == normalize_effect_type(
        right.get("effect_measure")
    ):
        score += 2
    for field in ("exposure_contrast", "reference_group"):
        left_text = _comparison_text(left.get(field))
        right_text = _comparison_text(right.get(field))
        if left_text and right_text and left_text == right_text:
            score += 2
    for field in ("effect_estimate", "lower_ci", "upper_ci"):
        left_number = _number(left.get(field))
        right_number = _number(right.get(field))
        if (
            left_number is not None
            and right_number is not None
            and math.isclose(left_number, right_number, rel_tol=1e-6, abs_tol=1e-9)
        ):
            score += 1
    return score


def merge_full_text_estimates(
    abstract_estimates: Iterable[dict[str, Any]],
    full_text_estimates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge Luna fields into Sol rows without deleting valid Sol evidence/rows."""
    merged = [normalize_estimate(row) for row in abstract_estimates]
    used: set[int] = set()
    for full_raw in full_text_estimates:
        full = normalize_estimate(full_raw)
        candidates = [
            (score, index)
            for index, abstract in enumerate(merged)
            if index not in used
            for score in [_estimate_match_score(abstract, full)]
            if score >= 8
        ]
        if not candidates:
            merged.append(full)
            continue
        best_score = max(score for score, _ in candidates)
        best_indices = [index for score, index in candidates if score == best_score]
        if len(best_indices) != 1:
            # Ambiguous rows are retained separately; cohort/report selection can
            # later choose one without risking a fieldwise cross-estimate merge.
            merged.append(full)
            continue
        index = best_indices[0]
        abstract = dict(merged[index])
        evidence = dict(abstract.get("evidence") or {})
        for field, entry in (full.get("evidence") or {}).items():
            if isinstance(entry, dict) and any(_clean_text(value) for value in entry.values()):
                evidence[field] = entry
        evidence_dependencies = {
            "cohort_name": ("cohort_name",),
            "country": ("country",),
            "ethnicity": ("ethnicity",),
            "study_population": ("study_population",),
            "sample_size": ("sample_size",),
            "number_of_events": ("number_of_events",),
            "prostate_cancer_stage": ("prostate_cancer_stage",),
            "prostate_cancer_stage_category": ("prostate_cancer_stage",),
            "disease_setting": ("disease_setting",),
            "primary_treatment": ("primary_treatment",),
            "exposure_alias": ("exposure_alias",),
            "exposure_timing": ("exposure_timing",),
            "exposure_assessment_method": ("exposure_assessment_method",),
            "outcome": ("outcome_definition",),
            "outcome_definition": ("outcome_definition",),
            "follow_up": ("follow_up",),
            "effect_measure": ("effect_measure",),
            "effect_estimate": ("effect_estimate",),
            "lower_ci": ("confidence_interval",),
            "upper_ci": ("confidence_interval",),
            "ci_confidence_level": ("confidence_interval",),
            "exposure_contrast": ("exposure_contrast",),
            "comparison_type": ("exposure_contrast", "reference_group"),
            "contrast_scale": ("exposure_contrast",),
            "needs_inversion": ("exposure_contrast", "reference_group"),
            "reference_group": ("reference_group",),
            "adjustment_variables": ("adjustment_variables",),
            "is_adjusted": ("adjustment_variables",),
        }

        def supported(field: str) -> bool:
            if field in {"pmid", "pmcid", "publication_year", "exposure"}:
                return True
            for evidence_field in evidence_dependencies.get(field, ()):
                entry = (full.get("evidence") or {}).get(evidence_field)
                if isinstance(entry, dict) and any(
                    _clean_text(entry.get(key))
                    for key in ("excerpt", "source", "url", "location")
                ):
                    return True
            return False

        placeholders = {"", "unclear", "unknown", "not reported"}
        for field, value in full.items():
            if field in {"evidence", "estimate_id"}:
                continue
            if not supported(field):
                continue
            if isinstance(value, str) and value.casefold() in placeholders:
                continue
            if value is None or value == {}:
                continue
            abstract[field] = value
        abstract["evidence"] = evidence
        abstract["estimate_id"] = merged[index].get("estimate_id")
        merged[index] = normalize_estimate(abstract)
        used.add(index)
    return merged


def second_screen(
    output_dir: Path,
    *,
    batch_size: int = 8,
    force: bool = False,
) -> dict[str, Any]:
    path = output_dir / "second_screen.json"
    candidate_path = output_dir / "candidates.json"
    first_path = output_dir / "first_screen.json"
    lineage = {
        "candidate_sha256": _file_sha256(candidate_path),
        "first_screen_sha256": _file_sha256(first_path),
    }
    protocol_digest = screening_protocol_digest("second")
    reusable = _reuse_bound_packet(
        path,
        {
            **lineage,
            "model": SECOND_MODEL,
            "protocol_digest": protocol_digest,
        },
        force=force,
    )
    if reusable is not None:
        return reusable
    candidates = monthly.load_json(candidate_path, {})
    first = _screening_map(monthly.load_json(first_path, {}))
    index = _candidate_index(candidates)
    selected_ids = [key for key in sorted(index) if first.get(key, {}).get("decision") == "INCLUDE"]
    usage = _usage_packet()
    results: list[dict[str, Any]] = []
    items = [_compact_context(index[key]) for key in selected_ids]
    runtime = dual_screening.configure_runtime("chatgpt", REPO_ROOT) if items else None
    for values in _batch(items, batch_size):
        expected = [item["context_id"] for item in values]
        payload = _call_model(
            runtime,
            "second",
            SECOND_SYSTEM_PROMPT,
            "Carefully screen and extract these abstract-level contexts:\n" + json.dumps(values, ensure_ascii=False),
            usage,
            {item["exposure"]: 1 for item in values},
        )
        batch_results = _validated_results(payload, expected, "second")
        for result in batch_results:
            source = index[result["context_id"]]
            eligible, code, reason = classify_candidate(source["article"])
            if not eligible:
                result.update(decision="EXCLUDE", exclusion_code=code, reason=dual_screening.one_sentence(reason), estimates=[])
            else:
                result["estimates"] = _prepare_estimates(
                    result.get("estimates", []), source["article"], source["context"]
                )
        results.extend(batch_results)
    packet = {
        "schema_version": 1,
        "created_at": dual_screening.utc_now(),
        "model": SECOND_MODEL,
        "protocol_digest": protocol_digest,
        "role": "careful eligibility and extraction",
        **lineage,
        "usage": usage,
        "results": results,
    }
    monthly.save_json(path, packet)
    return packet


def full_text_screen(
    output_dir: Path,
    *,
    max_characters: int = 90000,
    force: bool = False,
) -> dict[str, Any]:
    path = output_dir / "full_text_screen.json"
    candidate_path = output_dir / "candidates.json"
    second_path = output_dir / "second_screen.json"
    lineage = {
        "candidate_sha256": _file_sha256(candidate_path),
        "second_screen_sha256": _file_sha256(second_path),
    }
    protocol_digest = screening_protocol_digest("full_text")
    reusable = _reuse_bound_packet(
        path,
        {
            **lineage,
            "model": FULL_TEXT_MODEL,
            "protocol_digest": protocol_digest,
            "full_text_max_characters": max_characters,
        },
        force=force,
    )
    if reusable is not None:
        return reusable
    candidates = monthly.load_json(candidate_path, {})
    second = _screening_map(monthly.load_json(second_path, {}))
    index = _candidate_index(candidates)
    selected = []
    for context_id, result in second.items():
        article = index[context_id]["article"]
        gate = dict(result)
        gate["pmcid"] = article.get("pmcid")
        if needs_quantitative_full_text(gate):
            selected.append(context_id)
    runtime = dual_screening.configure_runtime("chatgpt", REPO_ROOT) if selected else None
    usage = _usage_packet()
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    if selected:
        monthly.configure_entrez(validate_api_key=False)
    full_text_cache: dict[str, str] = {}
    for context_id in selected:
        source = index[context_id]
        article, context = source["article"], source["context"]
        pmcid = str(article.get("pmcid") or "")
        try:
            if pmcid not in full_text_cache:
                full_text_cache[pmcid] = monthly.jats_to_text(monthly.fetch_pmc_full_text(pmcid))
            full_text = full_text_cache[pmcid][:max_characters]
            prompt_item = _compact_context(source)
            prompt_item["sol_estimates"] = second[context_id].get("estimates", [])
            prompt_item["pmc_url"] = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
            prompt_item["full_text"] = full_text
            payload = _call_model(
                runtime,
                "full_text",
                FULL_TEXT_SYSTEM_PROMPT,
                "Complete the missing quantitative fields for this included context:\n" + json.dumps(prompt_item, ensure_ascii=False),
                usage,
                {str(context["exposure"]): 1},
            )
            result = _validated_results(payload, [context_id], "full_text")[0]
            full_estimates = _prepare_estimates(
                result.get("estimates", []), article, context
            )
            result["estimates"] = merge_full_text_estimates(
                second[context_id].get("estimates", []), full_estimates
            )
            results.append(result)
        except Exception as exc:  # preserve Sol extraction when PMC retrieval/call fails
            failures.append(
                {
                    "context_id": context_id,
                    "pmid": str(article.get("pmid") or "Unknown"),
                    "pmcid": pmcid,
                    "reason": "Conditional full-text extraction was unavailable for this PMCID.",
                    "error": re.sub(r"\s+", " ", str(exc)).strip(),
                }
            )
    packet = {
        "schema_version": 1,
        "created_at": dual_screening.utc_now(),
        "model": FULL_TEXT_MODEL,
        "protocol_digest": protocol_digest,
        "full_text_max_characters": max_characters,
        "role": "conditional PMC quantitative extraction",
        **lineage,
        "gate": "included + PMCID + missing key quantitative information",
        "usage": usage,
        "results": results,
        "failures": failures,
    }
    monthly.save_json(path, packet)
    return packet


def _screen_exclusion(
    article: dict[str, Any],
    context: dict[str, Any],
    result: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    return {
        "pmid": str(article.get("pmid") or "Unknown"),
        "pmcid": article.get("pmcid") or None,
        "exposure": context.get("exposure"),
        "stage": stage,
        "code": result.get("exclusion_code") or "S2-OTHER",
        "reason": dual_screening.one_sentence(result.get("reason")),
    }


def finalize(output_dir: Path, *, force: bool = False) -> dict[str, Any]:
    path = output_dir / "final_screening.json"
    candidate_path = output_dir / "candidates.json"
    first_path = output_dir / "first_screen.json"
    second_path = output_dir / "second_screen.json"
    full_path = output_dir / "full_text_screen.json"
    lineage = {
        "candidate_sha256": _file_sha256(candidate_path),
        "first_screen_sha256": _file_sha256(first_path),
        "second_screen_sha256": _file_sha256(second_path),
        "full_text_screen_sha256": _file_sha256(full_path),
    }
    reusable = _reuse_bound_packet(path, lineage, force=force)
    if reusable is not None:
        return reusable
    candidates = monthly.load_json(candidate_path, {})
    first = _screening_map(monthly.load_json(first_path, {}))
    second = _screening_map(monthly.load_json(second_path, {}))
    full = _screening_map(monthly.load_json(full_path, {}))
    studies: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    final_contexts: list[dict[str, Any]] = []
    for context_id, source in sorted(_candidate_index(candidates).items()):
        article, context = source["article"], source["context"]
        first_result = first.get(context_id)
        if not first_result:
            raise RuntimeError(f"First-screen result is missing for {context_id}")
        if first_result.get("decision") == "EXCLUDE":
            exclusions.append(_screen_exclusion(article, context, first_result, "first_screen"))
            final_contexts.append({"context_id": context_id, "decision": "EXCLUDE", "source": "first_screen"})
            continue
        second_result = second.get(context_id)
        if not second_result:
            raise RuntimeError(f"Detailed-screen result is missing for {context_id}")
        if second_result.get("decision") == "EXCLUDE":
            exclusions.append(_screen_exclusion(article, context, second_result, "second_screen"))
            final_contexts.append({"context_id": context_id, "decision": "EXCLUDE", "source": "second_screen"})
            continue
        chosen = full.get(context_id) if full.get(context_id, {}).get("estimates") else second_result
        estimates = _prepare_estimates(chosen.get("estimates", []), article, context)
        if not estimates:
            result = {
                "exclusion_code": "S2-NO-SEPARABLE-ESTIMATE",
                "reason": "The eligible report has no separable quantitative estimate for the requested survival analyses.",
            }
            exclusions.append(_screen_exclusion(article, context, result, "finalize"))
            final_contexts.append({"context_id": context_id, "decision": "EXCLUDE", "source": "finalize"})
            continue
        studies.extend(estimates)
        final_contexts.append(
            {
                "context_id": context_id,
                "decision": "INCLUDE",
                "source": "full_text" if chosen is full.get(context_id) else "second_screen",
                "estimate_ids": [row["estimate_id"] for row in estimates],
            }
        )
    packet = {
        "schema_version": 1,
        "created_at": dual_screening.utc_now(),
        "protocol": "prostate_cancer_survival",
        **lineage,
        "contexts": final_contexts,
        "included_estimates": len(studies),
        "excluded_contexts": len(exclusions),
    }
    monthly.save_json(path, packet)
    monthly.save_json(output_dir / "studies.json", {"schema_version": 1, "studies": studies})
    monthly.save_json(output_dir / "exclusions.json", {"schema_version": 1, "exclusions": exclusions})
    return packet


def write_analysis_plots(result: dict[str, Any], output_dir: Path) -> None:
    """Write survival-only forest plots and a compact heterogeneity summary."""
    analyses = result.get("analyses", [])
    if not analyses:
        return
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir = output_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)
    for analysis in analyses:
        studies = analysis["studies"]
        height = max(3.0, 0.45 * len(studies) + 1.7)
        fig, axis = plt.subplots(figsize=(8.5, height))
        positions = list(range(len(studies), 0, -1))
        for position, study in zip(positions, studies):
            hr = float(study["hazard_ratio"])
            lower = float(study["lower_ci"])
            upper = float(study["upper_ci"])
            axis.errorbar(hr, position, xerr=[[hr - lower], [upper - hr]], fmt="o", color="#245b91", capsize=3)
        axis.errorbar(
            analysis["pooled_hr"],
            0,
            xerr=[
                [analysis["pooled_hr"] - analysis["lower_ci"]],
                [analysis["upper_ci"] - analysis["pooled_hr"]],
            ],
            fmt="D",
            color="#a33d32",
            capsize=4,
        )
        labels = [f"{study.get('cohort_name')} (PMID {study.get('pmid')})" for study in studies] + ["Random effects"]
        axis.set_yticks(positions + [0], labels)
        axis.set_xscale("log")
        axis.axvline(1.0, color="black", linestyle="--", linewidth=0.8)
        axis.set_xlabel("Hazard ratio (95% CI)")
        title = (
            f"{analysis['exposure']} | {analysis['exposure_timing']} | "
            f"{analysis['outcome']} | {analysis['contrast_key']} | "
            f"{analysis['outcome_definition_family']}"
        )
        if analysis.get("stratification"):
            title += f" | {analysis['stratification']}={analysis['stratum']}"
        axis.set_title(title)
        fig.tight_layout()
        relative = Path("plots") / f"forest_{analysis['analysis_id']}.png"
        fig.savefig(output_dir / relative, dpi=180)
        plt.close(fig)
        analysis["forest_plot"] = str(relative)

    base = [item for item in analyses if item.get("stratification") is None]
    if base:
        fig, axis = plt.subplots(figsize=(8.5, max(4.0, 0.35 * len(base) + 2)))
        x_values = [item["pooled_hr"] for item in base]
        y_values = [item["i_squared"] for item in base]
        axis.scatter(x_values, y_values, color="#245b91")
        for item, x_value, y_value in zip(base, x_values, y_values):
            axis.annotate(
                f"{item['exposure']} ({item['exposure_timing']}, {item['outcome']}, {item['contrast_key']})",
                (x_value, y_value),
                fontsize=7,
                xytext=(4, 3),
                textcoords="offset points",
            )
        axis.set_xscale("log")
        axis.axvline(1.0, color="black", linestyle="--", linewidth=0.8)
        axis.set_xlabel("Pooled hazard ratio")
        axis.set_ylabel("I² (%)")
        axis.set_ylim(-2, 102)
        axis.set_title("Prostate cancer survival heterogeneity by compatible analysis")
        fig.tight_layout()
        relative = Path("plots") / "heterogeneity_summary.png"
        fig.savefig(output_dir / relative, dpi=180)
        plt.close(fig)
        result["heterogeneity_plot"] = str(relative)


def analyze(output_dir: Path) -> dict[str, Any]:
    studies_packet = monthly.load_json(output_dir / "studies.json", {})
    result = run_meta_analyses(studies_packet.get("studies", []))
    result["schema_version"] = 1
    result["analysis_rules"] = {
        "primary_measure": "adjusted HR",
        "scale": "log HR",
        "confidence_interval": "95%",
        "minimum_independent_studies": 2,
        "group_by": [
            "exposure",
            "exposure_timing",
            "outcome",
            "contrast_key",
            "outcome_definition_family",
        ],
        "never_pool": [
            "prediagnosis with postdiagnosis",
            "different survival outcomes",
            "HR with OR or RR",
        ],
    }
    result["created_at"] = dual_screening.utc_now()
    write_analysis_plots(result, output_dir)
    monthly.save_json(output_dir / "meta_analyses.json", result)
    ledger_path = output_dir / "exclusions.json"
    ledger = monthly.load_json(ledger_path, {"schema_version": 1, "exclusions": []})
    screening_exclusions = [
        item for item in ledger.get("exclusions", []) if not str(item.get("code") or "").startswith("A-")
    ]
    ledger["exclusions"] = screening_exclusions + result["exclusions"]
    monthly.save_json(ledger_path, ledger)
    return result


def _window(arguments: argparse.Namespace) -> tuple[str | None, str]:
    if arguments.all_history_through:
        if arguments.start_date or arguments.end_date:
            raise ValueError("--all-history-through cannot be combined with --start-date or --end-date")
        return None, arguments.all_history_through
    if arguments.start_date or arguments.end_date:
        if not arguments.start_date or not arguments.end_date:
            raise ValueError("--start-date and --end-date must be supplied together")
        return arguments.start_date, arguments.end_date
    return monthly.previous_month()


def _default_output_dir(start_date: str | None, end_date: str) -> Path:
    label = f"all_history_through_{end_date}" if start_date is None else f"{start_date}_to_{end_date}"
    return REPO_ROOT / "data" / "survival_updates" / label


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=("discover", "first-screen", "second-screen", "full-text", "finalize", "analyze", "all"),
    )
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--all-history-through")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--exposure", action="append", dest="exposures")
    parser.add_argument("--first-batch-size", type=int, default=20)
    parser.add_argument("--second-batch-size", type=int, default=8)
    parser.add_argument("--full-text-max-characters", type=int, default=90000)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    start_date, end_date = _window(arguments)
    # Validate dates before creating or reading an output namespace.
    if start_date is not None:
        date.fromisoformat(start_date)
    date.fromisoformat(end_date)
    output_dir = arguments.output_dir or _default_output_dir(start_date, end_date)
    output_dir.mkdir(parents=True, exist_ok=True)
    stages = (
        ("discover", lambda: discover(output_dir, start_date, end_date, arguments.exposures, force=arguments.force)),
        ("first-screen", lambda: first_screen(output_dir, batch_size=arguments.first_batch_size, force=arguments.force)),
        ("second-screen", lambda: second_screen(output_dir, batch_size=arguments.second_batch_size, force=arguments.force)),
        ("full-text", lambda: full_text_screen(output_dir, max_characters=arguments.full_text_max_characters, force=arguments.force)),
        ("finalize", lambda: finalize(output_dir, force=arguments.force)),
        ("analyze", lambda: analyze(output_dir)),
    )
    summary: dict[str, Any] = {"output_dir": str(output_dir)}
    for name, action in stages:
        if arguments.stage not in {name, "all"}:
            continue
        value = action()
        if name == "discover":
            summary[name] = {"articles": len(value.get("articles", [])), "canonical_exposures": value.get("canonical_exposure_count")}
        elif name in {"first-screen", "second-screen", "full-text"}:
            summary[name] = {"results": len(value.get("results", [])), "model": value.get("model")}
        elif name == "finalize":
            summary[name] = {"included_estimates": value.get("included_estimates"), "excluded_contexts": value.get("excluded_contexts")}
        else:
            summary[name] = {"meta_analyses": len(value.get("analyses", [])), "analysis_exclusions": len(value.get("exclusions", []))}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
