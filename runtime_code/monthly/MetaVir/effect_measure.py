"""Convert eligible ratio measures to a common relative-risk scale.

OR conversion uses the baseline-risk correction described by Zhang and Yu.
HR conversion assumes proportional hazards, while IRR conversion assumes a
constant event rate over the selected risk horizon.  Both assumptions yield
the same cumulative-risk transformation once a baseline risk is supplied.
"""

from __future__ import annotations

import math
import re


PROSTATE_CANCER_LIFETIME_RISK = 0.132

RATIO_EFFECT_TYPES = {
    "OR", "RR", "IRR", "HR", "ODDS RATIO", "RISK RATIO", "RELATIVE RISK",
    "INCIDENCE RATE RATIO", "HAZARD RATIO",
}


def normalize_effect_type(effect_type):
    """Return the canonical RR, OR, HR, or IRR label, or ``None``.

    Adjustment qualifiers do not change the underlying ratio measure.  The
    review protocol also treats percentage risk statements as RR, percentage
    odds statements and linear-regression beta estimates as OR, and relative
    risk ratios (RRR) as RR.
    """
    raw_value = str(effect_type or "")
    raw_label = raw_value.upper()
    exact_label = re.sub(r"\s+", " ", raw_value).strip().casefold()
    # Subdistribution-hazard estimates are hazard ratios for this pipeline's
    # computational family. Keep the allowlist exact: no study-design or
    # arbitrary text is inferred from a substring. Bare forms are retained
    # because provider responses in the typed effect-measure field sometimes
    # omit the redundant word "ratio" while still naming this measure.
    if exact_label in {
        "shr",
        "sub-hazard",
        "subhazard",
        "subdistribution hazard",
        "sub-hazard ratio",
        "subhazard ratio",
        "subdistribution hazard ratio",
    }:
        return "HR"
    normalized = re.sub(r"[^A-Z]+", " ", raw_label).strip()
    if normalized in {"AOR", "ARR", "AHR", "AIRR"}:
        return normalized[1:]
    if re.search(r"\b(?:LINEAR REGRESSION (?:COEFFICIENT )?|BETA)\b", normalized):
        return None
    if "%" in raw_label or re.search(r"\b(?:PERCENT|PCT)\b", normalized):
        if re.search(r"\bODDS?\b", normalized):
            return "OR"
        if re.search(r"\bRISK\b", normalized):
            return "RR"
    if normalized == "RRR" or "RELATIVE RISK RATIO" in normalized:
        return "RR"
    # A bare "rate ratio" denotes an incidence-rate ratio.  Keep this exact
    # so phrases such as "hazard rate ratio" are not silently reclassified.
    if normalized == "RATE RATIO":
        return "IRR"
    aliases = (
        ("IRR", ("IRR", "INCIDENCE RATE RATIO")),
        ("OR", ("OR", "ODDS RATIO")),
        ("HR", ("HR", "HAZARD RATIO")),
        ("RR", ("RR", "RISK RATIO", "RELATIVE RISK")),
    )
    for canonical, labels in aliases:
        if any(re.search(rf"(?:^|\s){re.escape(label)}(?:\s|$)", normalized) for label in labels):
            return canonical
    return None


def is_eligible_effect_type(effect_type):
    """Return whether a ratio estimate can be converted to the RR scale."""
    return normalize_effect_type(effect_type) is not None


def baseline_risk_for_disease(disease) -> float | None:
    """Return the configured baseline, or ``None`` for the aggressive subtype.

    No matching published aggressive-prostate risk is available.  Its baseline
    must be supplied for the selected age and follow-up in analysis settings;
    the general prostate lifetime risk must never be substituted.
    """
    normalized = re.sub(r"[^a-z]+", " ", str(disease or "").lower()).strip()
    if "prostate" in normalized or "prostatic" in normalized:
        if "aggressive" in normalized.split():
            return None
        return PROSTATE_CANCER_LIFETIME_RISK
    raise ValueError(f"MetaVir supports prostate cancer only, not {disease!r}.")


def baseline_risk_from_percent(percent) -> float:
    """Convert a lifetime-risk percentage to a validated probability."""
    try:
        probability = float(percent) / 100.0
    except (TypeError, ValueError) as exc:
        raise ValueError("A numeric lifetime-risk percentage is required.") from exc
    if not math.isfinite(probability) or not 0 < probability < 1:
        raise ValueError("Lifetime-risk percentage must be between 0 and 100.")
    return probability


def convert_ratio_to_rr(value, effect_type, baseline_risk: float | None) -> float:
    """Convert a ratio; missing risk uses the app's labeled identity approximation."""
    try:
        estimate = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Effect estimate must be numeric.") from exc
    if not math.isfinite(estimate) or estimate <= 0:
        raise ValueError("Effect estimate must be finite and greater than zero.")
    canonical = normalize_effect_type(effect_type)
    if canonical is None:
        raise ValueError(f"Unsupported ratio effect type: {effect_type!r}.")
    if baseline_risk is None:
        return estimate
    try:
        p0 = float(baseline_risk)
    except (TypeError, ValueError) as exc:
        raise ValueError("Baseline risk must be numeric.") from exc
    if not math.isfinite(p0) or not 0 < p0 < 1:
        raise ValueError("Baseline risk must be a probability between zero and one.")

    if canonical == "RR":
        return estimate
    if canonical == "OR":
        return estimate / (1.0 - p0 + p0 * estimate)
    if canonical in {"HR", "IRR"}:
        # Numerically stable form of [1 - (1 - p0)^estimate] / p0.
        return -math.expm1(estimate * math.log1p(-p0)) / p0
    raise ValueError(f"Unsupported ratio effect type: {effect_type!r}.")
