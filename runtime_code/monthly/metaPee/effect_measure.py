"""Convert eligible ratio measures to a common relative-risk scale.

OR conversion uses the baseline-risk correction described by Zhang and Yu.
HR conversion assumes proportional hazards, while IRR conversion assumes a
constant event rate over the selected risk horizon.  Both assumptions yield
the same cumulative-risk transformation once a baseline risk is supplied.
"""

from __future__ import annotations

import csv
import math
import os
import re
from functools import lru_cache


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DISEASE_REGISTRY_FILE = os.path.join(
    BASE_DIR, "bladder_kidney_cancer_lifetime_incidence_aliases.csv"
)

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
    raw_label = str(effect_type or "").upper()
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
    # A bare "rate ratio" denotes an incidence-rate ratio. Keep this exact so
    # phrases such as "hazard rate ratio" remain fail-closed.
    if normalized == "RATE RATIO":
        return "IRR"
    if normalized in {
        "SHR",
        "SUB HAZARD",
        "SUBHAZARD",
        "SUBDISTRIBUTION HAZARD",
        "SUB HAZARD RATIO",
        "SUBHAZARD RATIO",
        "SUBDISTRIBUTION HAZARD RATIO",
    }:
        return "HR"
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


def baseline_risk_from_percent(percent) -> float:
    """Convert a lifetime-risk percentage to a validated probability."""
    try:
        probability = float(percent) / 100.0
    except (TypeError, ValueError) as exc:
        raise ValueError("A numeric lifetime-risk percentage is required.") from exc
    if not math.isfinite(probability) or not 0 < probability < 1:
        raise ValueError("Lifetime-risk percentage must be between 0 and 100.")
    return probability


def _normalize_disease_name(value) -> str:
    """Normalize a registry label or alias for exact, punctuation-blind matching."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _disease_key(value) -> str:
    return _normalize_disease_name(value).replace(" ", "_")


def _parse_optional_percent(value, field_name, row_number):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        percent = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be numeric on disease-registry row {row_number}."
        ) from exc
    if not math.isfinite(percent) or not 0 < percent < 100:
        raise ValueError(
            f"{field_name} must be between 0 and 100 on disease-registry row "
            f"{row_number}."
        )
    return percent


@lru_cache(maxsize=8)
def _load_disease_registry_cached(resolved_path):
    required_columns = {
        "cancer_name",
        "aliases",
        "lifetime_incidence_both_percent",
    }
    try:
        handle = open(resolved_path, newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(
            f"Could not read the urinary-cancer disease registry: {resolved_path}"
        ) from exc

    with handle:
        reader = csv.DictReader(handle)
        missing_columns = required_columns.difference(reader.fieldnames or ())
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise ValueError(f"Disease registry is missing required columns: {missing}.")

        registry = {}
        alias_owners = {}
        for row_number, row in enumerate(reader, start=2):
            disease = str(row.get("cancer_name") or "").strip()
            if not disease:
                raise ValueError(
                    f"cancer_name is required on disease-registry row {row_number}."
                )
            key = _disease_key(disease)
            if not key or key in registry:
                raise ValueError(
                    f"Duplicate or invalid cancer_name on disease-registry row "
                    f"{row_number}: {disease!r}."
                )

            aliases = []
            seen_aliases = set()
            for alias in str(row.get("aliases") or "").split(";"):
                alias = alias.strip()
                normalized_alias = _normalize_disease_name(alias)
                if alias and normalized_alias not in seen_aliases:
                    aliases.append(alias)
                    seen_aliases.add(normalized_alias)

            combined_percent = _parse_optional_percent(
                row.get("lifetime_incidence_both_percent"),
                "lifetime_incidence_both_percent",
                row_number,
            )
            if combined_percent is None:
                raise ValueError(
                    "lifetime_incidence_both_percent is required on "
                    f"disease-registry row {row_number}."
                )

            scope = {
                "key": key,
                "site_id": key,
                "label": disease,
                "disease": disease,
                "aliases": tuple(aliases),
                "lifetime_risk_percent": combined_percent,
                "lifetime_incidence_men_percent": _parse_optional_percent(
                    row.get("lifetime_incidence_men_percent"),
                    "lifetime_incidence_men_percent",
                    row_number,
                ),
                "lifetime_incidence_women_percent": _parse_optional_percent(
                    row.get("lifetime_incidence_women_percent"),
                    "lifetime_incidence_women_percent",
                    row_number,
                ),
                "lifetime_incidence_both_percent": combined_percent,
                "subcategories": (),
            }

            for candidate in (key, disease, *aliases):
                normalized_candidate = _normalize_disease_name(candidate)
                owner = alias_owners.get(normalized_candidate)
                if owner is not None and owner != key:
                    raise ValueError(
                        f"Disease alias {candidate!r} is assigned to both "
                        f"{registry[owner]['disease']!r} and {disease!r}."
                    )
                alias_owners[normalized_candidate] = key
            registry[key] = scope

    if not registry:
        raise ValueError("Disease registry contains no urinary-system cancers.")
    return registry


def load_disease_registry(path=None):
    """Load canonical urinary-cancer names, aliases, and lifetime incidence.

    The returned mapping follows CSV order and is safe for callers to modify;
    the cached registry itself remains private.
    """
    resolved_path = os.path.abspath(path or DISEASE_REGISTRY_FILE)
    registry = _load_disease_registry_cached(resolved_path)
    scopes = {
        key: {
            **scope,
            "aliases": list(scope["aliases"]),
            "subcategories": list(scope["subcategories"]),
        }
        for key, scope in registry.items()
    }
    from disease_hierarchy import decorate_registry
    return decorate_registry(scopes, 'urinary')


def resolve_disease(disease, registry=None) -> str:
    """Resolve a canonical name, registry key, or curated alias."""
    requested = _normalize_disease_name(disease)
    if not requested:
        raise ValueError("A urinary-system cancer name is required.")

    scopes = registry if registry is not None else load_disease_registry()
    for key, scope in scopes.items():
        aliases = scope.get("aliases") or ()
        if isinstance(aliases, str):
            aliases = aliases.split(";")
        candidates = (
            key,
            scope.get("key"),
            scope.get("site_id"),
            scope.get("label"),
            scope.get("disease"),
            *aliases,
        )
        if any(_normalize_disease_name(candidate) == requested for candidate in candidates):
            return str(scope.get("disease") or scope.get("label") or "").strip()
    raise ValueError(f"Unsupported urinary-system cancer: {disease!r}.")


def baseline_risk_for_disease(disease, registry=None) -> float:
    """Return gender-combined lifetime cumulative incidence for a disease."""
    scopes = registry if registry is not None else load_disease_registry()
    canonical = resolve_disease(disease, scopes)
    for scope in scopes.values():
        if str(scope.get("disease") or "").strip() == canonical:
            percent = scope.get(
                "lifetime_incidence_both_percent",
                scope.get("lifetime_risk_percent"),
            )
            return baseline_risk_from_percent(percent)
    raise ValueError(f"No combined lifetime incidence is configured for {canonical!r}.")


def convert_ratio_to_rr(value, effect_type, baseline_risk: float) -> float:
    """Convert an RR/OR/HR/IRR estimate to cumulative relative risk."""
    try:
        estimate = float(value)
        p0 = float(baseline_risk)
    except (TypeError, ValueError) as exc:
        raise ValueError("Effect estimate and baseline risk must be numeric.") from exc
    if not math.isfinite(estimate) or estimate <= 0:
        raise ValueError("Effect estimate must be finite and greater than zero.")
    if not math.isfinite(p0) or not 0 < p0 < 1:
        raise ValueError("Baseline risk must be a probability between zero and one.")

    canonical = normalize_effect_type(effect_type)
    if canonical == "RR":
        return estimate
    if canonical == "OR":
        return estimate / (1.0 - p0 + p0 * estimate)
    if canonical in {"HR", "IRR"}:
        # Numerically stable form of [1 - (1 - p0)^estimate] / p0.
        return -math.expm1(estimate * math.log1p(-p0)) / p0
    raise ValueError(f"Unsupported ratio effect type: {effect_type!r}.")
