"""Reject trend-test uncertainty for a selected contrast, using saved excerpts.

The source p value and directly reported intervals are retained. Retractions
apply only where a recorded method/evidence establishes p-derived uncertainty.
Raw screening receipts must remain immutable; apply this to effective records.
"""
from __future__ import annotations

import copy
import math
import re
import unicodedata

VERSION = "trend-p-ci-2026-09-27-v1"
REASON = "A p value for trend does not supply uncertainty for the selected contrast."
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:e[+\-]?\d+)?"
_P = r"p\s*(?:[-_ ]*\s*value)?"
_TREND_LABEL = rf"(?:{_P}\s*[-_:( ]*\s*(?:for\s+)?(?:linear\s+)?trend\s*\)?|(?:linear\s+)?trend\s*(?:test\s*)?(?:[-_:( ]*{_P}\s*\)?)?)"
_TREND_VALUE = re.compile(rf"{_TREND_LABEL}\s*(?:was\s*|of\s*)?(?:[=:<>]+\s*)?(?P<value>{_NUMBER})", re.I)
_VALUE_TREND = re.compile(rf"{_P}\s*[=:<>]*\s*(?P<value>{_NUMBER})\s*(?:\(\s*)?for\s+(?:linear\s+)?trend", re.I)
_NARRATIVE_TREND = re.compile(rf"\blinear trend\b[^.;]{{0,180}}?\b{_P}\s*[=:<>]+\s*(?P<value>{_NUMBER})", re.I)
_DERIVED = re.compile(r"(?:deriv|comput|imput|reconstruct|calculat|wald)", re.I)
_PVALUE = re.compile(r"\bp\b(?:\s*[-_ ]?\s*value\b)?", re.I)
_CI_DERIVATION = re.compile(
    r"(?:computed|derived|imputed|reconstructed|calculated)\s+(?:from|using|based on)\s+"
    r"(?:the\s+)?(?:reported|ratio|effect|point|exact|two[- ]sided|p\b)", re.I)


def _text(value):
    return unicodedata.normalize("NFKC", str(value or "")).replace("−", "-").replace("–", "-").replace("‑", "-").replace("≤", "<=").replace("≥", ">=")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        match = re.fullmatch(rf"\s*(?:p\s*[=:<>]+\s*)?({_NUMBER})\s*", _text(value), re.I)
        result = float(match.group(1)) if match else None
    return result if result is not None and math.isfinite(result) else None


def _strings(value, path):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, path + "." + str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, path + "." + str(index))


def source_excerpts(study, context=None):
    """Read relevant saved extraction evidence, never arbitrary article text."""
    for prefix, record in (("study", study), ("context", context)):
        if not isinstance(record, dict):
            continue
        for key in ("p_value_provenance", "p_value_supporting_text", "p_value_context", "supporting_text", "extraction_supporting_text"):
            value = record.get(key)
            if isinstance(value, dict) and key.endswith("supporting_text"):
                value = {k: v for k, v in value.items() if any(s in k.lower() for s in ("effect", "outcome", "p_value", "pvalue", "uncertainty", "confidence", "quantitative"))}
            yield from _strings(value, prefix + "." + key)
        for evidence_key in ("evidence", "extraction_evidence"):
            evidence = record.get(evidence_key)
            if isinstance(evidence, dict):
                for key in ("p_value", "effect_size", "confidence_interval"):
                    yield from _strings(evidence.get(key), prefix + "." + evidence_key + "." + key)
        for key in ("p_value", "P Value", "p_value_type", "p_value_kind", "p_value_test", "p_value_reported_as", "p_value_relation"):
            yield from _strings(record.get(key), prefix + "." + key)


def trend_evidence(study, context=None):
    """Return evidence tying the selected source p value to a trend test.

    Numeric association avoids treating an unrelated trend p elsewhere in the
    same excerpt as the selected contrast's p. Explicit p-test kind labels are
    also accepted. Unlabelled/ambiguous prose is left for human review.
    """
    selected = next((_number(study.get(key)) for key in ("ci_source_p_value", "reported_p_value", "p_value", "P Value") if _number(study.get(key)) is not None), None)
    matches = []
    for path, original in source_excerpts(study, context):
        value = _text(original)
        if not re.search(r"trend", value, re.I):
            continue
        explicit_kind = path.endswith((".kind", ".test", ".p_value_type", ".p_value_kind", ".p_value_test")) and "p_value" in path
        numeric = [float(match.group("value")) for match in _VALUE_TREND.finditer(value)]
        numeric += [float(match.group("value")) for match in _NARRATIVE_TREND.finditer(value)]
        for match in _TREND_VALUE.finditer(value):
            numeric.append(float(match.group("value")))
            # A labelled list may give separate models/strata, e.g. "P for
            # trend: .05 and .017, respectively". Stop at intervening prose.
            tail = value[match.end():]
            while True:
                item = re.match(rf"\s*(?:,\s*(?:and\s*)?|and\s+|&\s*|/\s*)({_NUMBER})", tail, re.I)
                if not item:
                    break
                numeric.append(float(item.group(1)))
                tail = tail[item.end():]
        matched = selected is not None and any(math.isclose(selected, n, rel_tol=1e-7, abs_tol=1e-12) for n in numeric)
        if explicit_kind or matched:
            matches.append({"field": path, "excerpt": original, "matched_p_value": selected, "basis": "explicit trend-test label" if explicit_kind else "trend-labelled number matches source p value"})
    return matches


def ci_is_p_derived(study):
    method = str(study.get("ci_method") or study.get("ci_imputation_method") or "").replace("_", " ")
    if _DERIVED.search(method) and _PVALUE.search(method):
        return True
    texts = []
    for evidence_key in ("evidence", "extraction_evidence", "supporting_text", "extraction_supporting_text"):
        evidence = study.get(evidence_key) or {}
        ci_evidence = evidence.get("confidence_interval") if isinstance(evidence, dict) else None
        texts += list(_strings(ci_evidence, "confidence_interval"))
    texts += list(_strings(study.get("imputation_notes"), "imputation_notes"))
    for derivation in study.get("policy_derivations") or []:
        if isinstance(derivation, dict) and derivation.get("field") in ("confidence_interval", "ci_lower", "ci_upper"):
            method = str(derivation.get("method") or "").replace("_", " ")
            if _DERIVED.search(method) and _PVALUE.search(method):
                return True
    for _, value in texts:
        if _CI_DERIVATION.search(value) and _PVALUE.search(value) and re.search(r"\bCI\b|confidence interval|Wald interval", value, re.I):
            return True
    return study.get("ci_imputed") is True and study.get("ci_source_p_value") is not None


def attach_context(study, context):
    """Carry the actual saved effect excerpt into study-only completion paths."""
    if not isinstance(study, dict) or not isinstance(context, dict):
        return study
    # Replay must not nest the saved context beneath another context prefix.
    # Keep the original excerpt paths and merge newly supplied source fields.
    excerpts = dict(study.get("p_value_context") or {}) if isinstance(study.get("p_value_context"), dict) else {}
    inherited = context.get("p_value_context")
    if isinstance(inherited, dict):
        excerpts.update(inherited)
    fresh = {key: value for key, value in context.items() if key != "p_value_context"}
    excerpts.update({path: value for path, value in source_excerpts({}, fresh)})
    if excerpts:
        study["p_value_context"] = excerpts
    return study


def retract_trend_ci(study, context=None):
    """Blank a demonstrably p-derived interval; preserve effect and source p.

    Returns True only for a new retraction. All removed values and saved source
    evidence remain in a durable row-level receipt. Repeated calls are stable.
    """
    if not isinstance(study, dict):
        return False
    evidence = trend_evidence(study, context)
    if not evidence:
        return False
    study["p_value_ci_ineligible_reason"] = REASON
    study["p_value_ci_guard_version"] = VERSION
    if not ci_is_p_derived(study):
        return False
    fields = [key for key in ("ci_lower", "ci_upper", "Lower CI", "Upper CI", "lower_ci", "upper_ci", "Original Lower CI", "Original Upper CI", "log_ci_lower", "log_ci_upper", "ci_method", "ci_source_p_value", "ci_source_p_value_role", "ci_wald_source_estimate_identity_digest") if key in study]
    fields += [key for key in study if re.fullmatch(r"(?:RR_BOTH|RR_MALE|RR_FEMALE|OR|HR|RR) (?:Lower|Upper) CI", key)]
    # A previously p-derived interval also invalidates SE/weights computed from
    # it. Independently reported SE is kept only with explicit source identity.
    se_prov = study.get("standard_error_provenance") or {}
    reported_se = _number(study.get("reported_standard_error"))
    independent_se = isinstance(se_prov, dict) and reported_se is not None and reported_se > 0 and all(se_prov.get(key) is True for key in ("exact", "same_estimate", "same_model", "same_stratum"))
    if not independent_se:
        fields += [key for key in ("standard_error", "SE", "se", "standard_error_method", "variance", "weight", "Weight") if key in study]
    removed = {key: copy.deepcopy(study[key]) for key in fields if study.get(key) is not None}
    if not any(key in removed for key in ("ci_lower", "ci_upper", "Lower CI", "Upper CI", "lower_ci", "upper_ci")):
        return False
    study["p_value_ci_retraction"] = {"version": VERSION, "reason": REASON, "removed": removed, "source_evidence": evidence}
    for key in fields:
        study[key] = None
    if "ci_imputed" in study:
        study["ci_imputed"] = False
    return True
