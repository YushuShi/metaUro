"""Statistical and cache-facing helpers for the MetaVir application.

Article discovery and model screening live exclusively in the audited monthly
pipeline.  This module intentionally contains no network or model clients.
"""

from __future__ import annotations

from scripts import dietary_only_policy as dietary_policy

import json
import math
import os
import re
from functools import lru_cache

import forestplot
import matplotlib
import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.stats.meta_analysis import combine_effects

from effect_measure import (
    baseline_risk_for_disease,
    convert_ratio_to_rr,
    is_eligible_effect_type,
    normalize_effect_type,
)

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SYNONYMS_CACHE = os.path.join(DATA_DIR, "synonyms_cache.json")

EGGERS_MIN_STUDIES = 10
EGGERS_FEWER_THAN_TEN_MESSAGE = (
    "Egger’s test is generally not recommended when fewer than 10 studies are available."
)

MENDELIAN_RANDOMIZATION_PATTERN = re.compile(
    r"\b(?:mendelian\s+randomi[sz](?:ation|ed)|genetically\s+predicted|"
    r"genetically\s+(?:proxied|instrumented)|genetic\s+risk\s+score|"
    r"genetic(?:ally derived)?\s+instrument(?:al variable)?s?|"
    r"(?:two[- ]sample|one[- ]sample|bidirectional|multivariable)?\s*"
    r"MR\s+(?:analysis|study|design)|"
    r"(?:genetic variants?|SNPs?)\b.{0,80}\binstrumental variables?|"
    r"instrumental[- ]variables?\b.{0,80}\b(?:genetic variants?|SNPs?))\b",
    re.IGNORECASE,
)


def is_mendelian_randomization_study(study):
    """Return whether a saved record is a Mendelian-randomization study."""
    if study is None:
        return False
    getter = study.get if hasattr(study, "get") else lambda _key, _default=None: None
    searchable = " ".join(
        str(getter(key) or "")
        for key in (
            "Design", "design", "Reference", "reference", "Study", "study",
            "Title", "title", "exposure_measurement_supporting_text",
            "extraction_supporting_text",
        )
    )
    return bool(MENDELIAN_RANDOMIZATION_PATTERN.search(searchable))


def analysis_context_key(disease, exposure, outcome="Incidence"):
    """Return the stable key used for context-specific curated exclusions."""
    parts = (disease, exposure, outcome)
    return "_".join(
        re.sub(r"[^a-zA-Z0-9]+", "_", str(part or "").lower()).strip("_")
        for part in parts
    )


@lru_cache(maxsize=1)
def curated_meta_analysis_exclusions():
    """Load explicit context-specific exclusions from the review store."""
    path = os.path.join(DATA_DIR, "verifications.json")
    try:
        with open(path, encoding="utf-8") as handle:
            verifications = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}

    exclusions = {}
    for pmid, verification in verifications.items():
        if not isinstance(verification, dict):
            continue
        curated = verification.get("curated_exclusion")
        if isinstance(curated, dict) and curated.get("excluded_from_meta_analysis") is True:
            context = str(curated.get("context") or "").strip()
            exclusions.setdefault(str(pmid), set()).add(context)

    return exclusions


def is_curated_meta_analysis_exclusion(pmid, disease, exposure, outcome="Incidence"):
    """Return whether a PMID was explicitly excluded for this analysis context."""
    contexts = curated_meta_analysis_exclusions().get(str(pmid or "").strip())
    if not contexts:
        return False
    return bool({"*", analysis_context_key(disease, exposure, outcome)} & set(contexts))


def filter_curated_meta_analysis_exclusions(frame, disease, exposure, outcome="Incidence"):
    """Remove curator-approved records and Mendelian-randomization studies."""
    if frame is None or frame.empty:
        return frame.copy() if frame is not None else frame
    excluded = pd.Series(False, index=frame.index)
    if "PMID" in frame.columns:
        excluded |= frame["PMID"].map(
            lambda pmid: is_curated_meta_analysis_exclusion(
                pmid, disease, exposure, outcome
            )
        )
    excluded |= frame.apply(is_mendelian_randomization_study, axis=1)
    return frame.loc[~excluded].copy()


def is_parent_pooling_eligible(value):
    """Allow direct/legacy rows and fail closed for hierarchy projections."""
    if value is None:
        return True
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        return False
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return False


def p_value_from_effect_ci(effect, lower, upper, effect_type=None):
    """Compute a two-sided normal-approximation p-value from a 95% CI."""
    try:
        effect, lower, upper = float(effect), float(lower), float(upper)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (effect, lower, upper)):
        return None
    if lower >= upper or not lower <= effect <= upper:
        return None

    normalized_type = normalize_effect_type(effect_type)
    if normalized_type is None:
        return None
    if min(effect, lower, upper) <= 0:
        return None
    theta, ci_lower, ci_upper = math.log(effect), math.log(lower), math.log(upper)

    standard_error = (ci_upper - ci_lower) / 3.92
    if standard_error <= 0 or not math.isfinite(standard_error):
        return None
    z_score = theta / standard_error
    return math.erfc(abs(z_score) / math.sqrt(2.0))


def format_computed_p_value(p_value):
    if p_value is None:
        return None
    if p_value < 1e-300:
        return "p<1e-300"
    if p_value < 0.001:
        return f"p={p_value:.3e}"
    return f"p={p_value:.3f}"


def get_canonical_name(exposure):
    """Resolve an exposure or core synonym to its curated canonical name."""
    if not exposure:
        return exposure

    exposure_lower = str(exposure).casefold().strip()
    try:
        with open(SYNONYMS_CACHE, encoding="utf-8") as handle:
            cache = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return exposure_lower

    for canonical in cache:
        if canonical.casefold().strip() == exposure_lower:
            return canonical

    for canonical, terms in cache.items():
        core = terms.get("core", "") if isinstance(terms, dict) else str(terms)
        core_values = core if isinstance(core, list) else str(core).split(",")
        core_terms = {str(term).casefold().strip() for term in core_values}
        if exposure_lower in core_terms:
            return canonical
    return exposure_lower


def add_estimated_cases_to_row(row, disease_keyword):
    """Normalize case counts and estimate missing cases from prostate risk."""
    raw_cases = row.get("Cases")
    clean_cases = None
    if raw_cases is not None:
        try:
            # Clean string like '1,200' to 1200
            clean_cases = int(str(raw_cases).replace(',', '').strip())
        except (TypeError, ValueError):
            pass

    sample_size = row.get("Sample Size")
    clean_n = None
    if sample_size is not None:
        try:
            clean_n = int(str(sample_size).replace(',', '').strip())
        except (TypeError, ValueError):
            pass

    estimated_cases = None
    if clean_cases is None and clean_n is not None and row.get('Disease Endpoint') != 'aggressive':
        try:
            prev = baseline_risk_for_disease(disease_keyword)
        except ValueError:
            prev = 0.0

        if prev is not None and prev > 0:
            estimated_cases = int(round(clean_n * prev))

    row["Cases"] = clean_cases if clean_cases is not None else None
    row["Estimated Cases"] = estimated_cases
    return row


def eggers_test(log_es, log_se):
    """
    Performs Egger's regression test for funnel plot asymmetry.
    Regression: Standardized Effect ~ 1/SE (Precision)
    """
    if len(log_es) < 3:
        return None, None

    precision = 1.0 / log_se
    std_effect = log_es / log_se

    X = sm.add_constant(precision)
    model = sm.OLS(std_effect, X).fit()

    # The intercept (const) indicates asymmetry
    intercept = model.params.get('const', 0)
    p_value = model.pvalues.get('const', 1.0)

    return intercept, p_value


def perform_leave_one_out(df):
    """
    Iteratively performs meta-analysis excluding one study at a time.
    """
    results = []
    studies = df.index.tolist()

    for i in range(len(df)):
        subset = df.drop(df.index[i])
        if len(subset) < 2:
            continue

        res = combine_effects(subset['log_ES'], subset['var'], method_re='dl')
        summary = res.summary_frame().iloc[-1] # Random effects row

        results.append({
            "omitted": studies[i],
            "pooled_es": np.exp(summary['eff']),
            "ci_low": np.exp(summary['ci_low']),
            "ci_upp": np.exp(summary['ci_upp']),
            "is_significant": (summary['ci_low'] > 0) or (summary['ci_upp'] < 0)
        })

    return results


def capitalize_sentences(text):
    """Lowercases the string then capitalizes the start of each sentence, while preserving 'CI' and 'Vitamin X'."""
    if not text:
        return text
    text = text.lower()
    # Capitalize the very first character
    text = text[0].upper() + text[1:]
    # Capitalize characters following a period and space
    text = re.sub(r'\.\s+([a-z])', lambda m: ". " + m.group(1).upper(), text)
    # Restore CI
    text = re.sub(r'\bci\b', 'CI', text)
    # Restore I²
    text = text.replace('i²', 'I²')
    # Restore Vitamin casing (Vitamin A, Vitamin B12, etc.)
    text = re.sub(r'\bvitamin\s+([a-z])', lambda m: "Vitamin " + m.group(1).upper(), text)
    # Restore known acronyms
    text = re.sub(r'\bbcaas\b', 'BCAAs', text)
    return text


def get_funnel_interpretation(disease, exposure, n_studies, eggers_p, intercept):
    """
    Provides a brief rule-based interpretation of the funnel plot and Egger's test.
    """
    if n_studies < EGGERS_MIN_STUDIES:
        return EGGERS_FEWER_THAN_TEN_MESSAGE
    if eggers_p is None:
        interpretation = "Insufficient studies to perform formal publication bias testing."
    elif eggers_p < 0.05:
        interpretation = f"The significant funnel plot asymmetry (Egger's p={eggers_p:.4f}) indicates substantial publication bias in this meta-analysis on {exposure} and {disease}. This specific pattern suggests that smaller studies with smaller or null effects may be underrepresented, compromising the reliability of the pooled estimate and potentially leading to an overestimation of the true association."
    else:
        interpretation = f"Egger's test indicates no significant funnel plot asymmetry (p={eggers_p:.4f}). The distribution of studies appears symmetric, suggesting a lower risk of publication bias."

    return capitalize_sentences(interpretation)


def get_results_interpretation(disease, exposure, outcome, n_studies, pooled_es, ci_low, ci_upp, i2, stat_interpretation):
    """
    Provides a brief, non-causal rule-based interpretation of the meta-analysis results.
    """
    # Parse the statistical interpretation for direction and significance
    is_sig = "not" not in stat_interpretation.lower()

    is_decreased = "decreased" in stat_interpretation.lower() or (pooled_es is not None and float(pooled_es) < 1.0)
    is_increased = "increased" in stat_interpretation.lower() or (pooled_es is not None and float(pooled_es) > 1.0)

    # Handle I2 interpretation
    try:
        i2_val = float(i2)
    except:
        i2_val = 0

    if i2_val < 25:
        het_text = "low between-study heterogeneity"
        het_implication = "indicating consistent findings across the included studies"
    elif i2_val < 50:
        het_text = "moderate between-study heterogeneity"
        het_implication = "suggesting some variability in the results across studies"
    elif i2_val < 75:
        het_text = "substantial between-study heterogeneity"
        het_implication = "reflecting considerable variability across studies"
    else:
        het_text = "very high between-study heterogeneity"
        het_implication = "reflecting highly inconsistent results across studies"

    if is_sig:
        dir_text = "higher risk" if is_increased else "lower risk"
        interpretation = f"The pooled analysis of {n_studies} studies yielded an overall effect size of {pooled_es} (95% CI: {ci_low}–{ci_upp}), suggesting that {exposure} is associated with a {dir_text} of {disease}. "
        if i2_val >= 50:
            interpretation += f"However, this association should be interpreted with caution, as there was {het_text} (I² = {i2_val:.2f}%), {het_implication}."
        else:
            interpretation += f"These findings were supported by {het_text} (I² = {i2_val:.2f}%), {het_implication}."
    else:
        rel_text = "positive" if is_increased else "inverse"
        interpretation = f"The pooled analysis of {n_studies} studies yielded an effect size of {pooled_es} (95% CI: {ci_low}–{ci_upp}), indicating no statistically significant association between {exposure} and {disease}. "
        interpretation += f"Although the point estimate suggests a potential {rel_text} relationship, the confidence interval includes the null. "
        interpretation += f"These findings were accompanied by {het_text.replace('between-study ', '')} (I² = {i2_val:.2f}%), {het_implication}."

    return capitalize_sentences(interpretation)


def generate_baujat_plot(df, disease, exposure, outcome="Incidence", exclude_meta=False, plot_namespace=""):
    """
    Generates a Baujat plot to identify studies contributing to heterogeneity and influence.
    X: Contribution to Q (heterogeneity)
    Y: Influence on pooled effect
    """
    if len(df) < 3:
        return None

    # X: (observed - predicted)^2 / variance
    # Y: (pooled_all - pooled_without_i)^2 / var_pooled_all

    res_all = combine_effects(df['log_ES'], df['var'], method_re='dl')
    summary_all = res_all.summary_frame().iloc[-1]  # Random effects row
    pooled_all = float(summary_all['eff'])
    var_all = float(summary_all['sd_eff'])**2  # var = sd^2

    q_contributions = []
    influence = []

    for i in range(len(df)):
        # Q contribution
        qi = float(((df['log_ES'].iloc[i] - pooled_all)**2) / df['var'].iloc[i])
        q_contributions.append(qi)

        # Influence
        subset = df.drop(df.index[i])
        res_sub = combine_effects(subset['log_ES'], subset['var'], method_re='dl')
        sub_summary = res_sub.summary_frame().iloc[-1]
        sub_eff = float(sub_summary['eff'])
        inf = float(((pooled_all - sub_eff)**2) / var_all) if var_all > 0 else 0.0
        influence.append(inf)

    plt.figure(figsize=(8, 6))
    plt.scatter(q_contributions, influence, alpha=0.6, edgecolors='k')

    for i, txt in enumerate(df.index):
        plt.annotate(str(txt), (float(q_contributions[i]), float(influence[i])), fontsize=8, alpha=0.7)

    plt.xlabel('Contribution to Heterogeneity (Q)')
    plt.ylabel('Influence on Pooled Effect')
    plt.title(f'Baujat Plot: {disease} vs {exposure}')
    plt.grid(True, alpha=0.2)

    safe_disease = re.sub(r'[^a-zA-Z0-9]+', '_', disease.lower()).strip('_')
    safe_exposure = re.sub(r'[^a-zA-Z0-9]+', '_', exposure.lower()).strip('_')
    if plot_namespace:
        if not re.fullmatch(r"chatgpt|subexposures/(yes|no)/interactive/[a-f0-9]{16}", plot_namespace):
            raise ValueError("Invalid plot namespace")
        safe_exposure = plot_namespace + "/" + safe_exposure
    safe_outcome = re.sub(r'[^a-zA-Z0-9]+', '_', outcome.lower()).strip('_')
    safe_meta = "primary" if exclude_meta else "all"
    # Create exposure subfolder
    exposure_dir = os.path.join("static", safe_exposure)
    os.makedirs(exposure_dir, exist_ok=True)

    path = os.path.join(exposure_dir, f"baujat_{safe_disease}_{safe_outcome}_{safe_meta}.png")
    plt.savefig(path, bbox_inches='tight')
    plt.close()
    return path


def perform_meta_analysis(
    df_clean,
    disease,
    exposure,
    outcome="Incidence",
    exclude_meta=False,
    df_all=None,
    screening_stats=None,
    generate_plots=True,
    plot_namespace="",
):
    """
    Performs random-effects meta-analysis on the provided DataFrame.
    """
    # The protocol requires total N, while exact cases are optional.
    if 'Sample Size' in df_clean.columns:
        sample_n = pd.to_numeric(df_clean['Sample Size'].astype(str).str.replace(',', '', regex=False), errors='coerce')
        df_clean = df_clean.loc[sample_n.gt(0) & sample_n.mod(1).eq(0)].copy()
    else:
        df_clean = df_clean.iloc[0:0].copy()
    if df_all is None:
        df_all = df_clean
    if 'subexposure_pooling_selected' in df_clean.columns:
        df_clean = df_clean.loc[~df_clean['subexposure_pooling_selected'].eq(False)].copy()
    if 'disease_scope_pooling_selected' in df_clean.columns:
        df_clean = df_clean.loc[~df_clean['disease_scope_pooling_selected'].eq(False)].copy()
    # Display-only eligible Fair records remain in tables, never in pooled input.
    if 'display_only' in df_clean.columns:
        df_clean = df_clean.loc[~df_clean['display_only'].eq(True)].copy()


    # Curator-approved context exclusions are removed here; otherwise keep every
    # saved article in the returned list. All eligible ratio measures are
    # converted to cumulative RR before pooling.
    df_all = filter_curated_meta_analysis_exclusions(
        df_all, disease, exposure, outcome
    )
    df_clean = filter_curated_meta_analysis_exclusions(
        df_clean, disease, exposure, outcome
    )
    # Screened child evidence is visible in its hierarchy parents, but the
    # explicit hierarchy contract forbids pooling those correlated child rows
    # as independent parent-level estimates.
    if 'pool_in_parent_meta_analysis' in df_clean.columns:
        df_clean = df_clean.loc[
            df_clean['pool_in_parent_meta_analysis'].map(
                is_parent_pooling_eligible
            )
        ].copy()
    if 'Effect Type' in df_clean.columns:
        eligible_effect = df_clean['Effect Type'].map(is_eligible_effect_type)
        df_clean = df_clean.loc[eligible_effect].copy()
    else:
        df_clean = df_clean.iloc[0:0].copy()
    required_effect_columns = {'Effect Size', 'Lower CI', 'Upper CI'}
    if required_effect_columns.issubset(df_clean.columns):
        for column in required_effect_columns:
            df_clean[column] = pd.to_numeric(df_clean[column], errors='coerce')
        df_clean = df_clean.loc[
            df_clean['Effect Size'].gt(0)
            & df_clean['Lower CI'].gt(0)
            & df_clean['Upper CI'].gt(0)
            & df_clean['Lower CI'].le(df_clean['Effect Size'])
            & df_clean['Effect Size'].le(df_clean['Upper CI'])
        ].copy()

    if df_clean.empty and 'display_only' in df_all.columns and df_all['display_only'].eq(True).any():
        return {'success': True, 'studies': df_all.to_dict(orient='records'),
                'summary_html': '<p>Eligible studies are listed below. No studies meet the default pooling criteria.</p>',
                'headline': None, 'screening_stats': screening_stats,
                'plot_url': None, 'funnel_plot_url': None, 'baujat_plot_url': None}

    if df_clean.empty:
        return {"error": "No studies with eligible RR, IRR, OR, or HR measurements were available for meta-analysis."}

    safe_disease = re.sub(r'[^a-zA-Z0-9]+', '_', disease.lower()).strip('_')
    safe_exposure = re.sub(r'[^a-zA-Z0-9]+', '_', exposure.lower()).strip('_')
    if plot_namespace:
        if not re.fullmatch(r"chatgpt|subexposures/(yes|no)/interactive/[a-f0-9]{16}", plot_namespace):
            raise ValueError("Invalid plot namespace")
        safe_exposure = plot_namespace + "/" + safe_exposure
    safe_outcome = re.sub(r'[^a-zA-Z0-9]+', '_', outcome.lower()).strip('_')
    safe_meta = "primary" if exclude_meta else "all"

    # Sort by Effect Size for consistent display (Table and Plot)
    df_clean = df_clean.sort_values(by='Effect Size', ascending=True)

    # Meta-Analysis
    # Convert every eligible ratio to a cumulative RR using the baseline risk
    # for the selected cancer site, then compute heterogeneity on log(RR).
    p0 = baseline_risk_for_disease(disease)

    def convert_row(row, field):
        # Inheritance changes category membership, not the endpoint's baseline.
        if row.get('Disease Endpoint') == 'aggressive' and disease == 'Prostate cancer':
            from effect_measure import convert_ratio_to_rr as endpoint_conversion
            return endpoint_conversion(row[field], row['Effect Type'], None)
        return convert_ratio_to_rr(row[field], row['Effect Type'], p0)

    df_clean['converted_ES'] = df_clean.apply(lambda x: convert_row(x, 'Effect Size'), axis=1)
    df_clean['converted_Lower_CI'] = df_clean.apply(lambda x: convert_row(x, 'Lower CI'), axis=1)
    df_clean['converted_Upper_CI'] = df_clean.apply(lambda x: convert_row(x, 'Upper CI'), axis=1)

    df_clean['log_ES'] = df_clean.apply(
        lambda x: np.log(x['converted_ES'])
        if is_eligible_effect_type(x['Effect Type']) and x['converted_ES'] > 0
        else x['converted_ES'],
        axis=1,
    )

    def calc_log_se(row):
        if is_eligible_effect_type(row['Effect Type']) and row['converted_Lower_CI'] > 0 and row['converted_Upper_CI'] > 0:
             return (np.log(row['converted_Upper_CI']) - np.log(row['converted_Lower_CI'])) / 3.92
        return row['SE']

    df_clean['log_SE'] = df_clean.apply(calc_log_se, axis=1)
    df_clean['var'] = df_clean['log_SE'] ** 2
    capture_pooling_rows(df_clean)

    # Set index to Study for better summary labels
    analysis_df = df_clean.set_index('Study')

    try:
        baujat_url = None
        if len(analysis_df) < 2:
            # Handling Single Study: Use original raw values to match exactly
            if len(analysis_df) == 1:
                row = analysis_df.iloc[0]
                pooled_es = row['converted_ES']
                pooled_lower = row['converted_Lower_CI']
                pooled_upper = row['converted_Upper_CI']
            else:
                 # Empty
                 pooled_es = 0
                 pooled_lower = 0
                 pooled_upper = 0


            # Create a dummy summary_df just to satisfy variable existence for summary_html (even if unused)
            summary_df = pd.DataFrame({
                'Cases': [row['Cases'] if len(analysis_df) == 1 else 0],
                'Sample Size': [row['Sample Size'] if len(analysis_df) == 1 else 0],
                'Effect': [pooled_es],
                '95% CI lower': [pooled_lower],
                '95% CI upper': [pooled_upper]
            }, index=['Pooled Result (Single Study)'])
            summary = summary_df.to_html(classes='table table-striped', header=True)

            # Interpretation logic
            is_significant = (pooled_lower > 1) or (pooled_upper < 1)
            # Note: The above significance check assumes Ratio (null=1).
            # If not ratio (e.g. 0), it should be diff > 0.
            # Use confidence interval crossing null hypothesis check based on CI signs?
            # Actually, standard way: if lower and upper are on same side of Null.
            # Ratios are always > 0.

            # Let's improve significance check based on type
            eff_type = row['Effect Type'] if len(analysis_df) == 1 else 'OR' # Default to OR for interpretation if empty
            if is_eligible_effect_type(eff_type):
                 is_significant = (pooled_lower > 1) or (pooled_upper < 1)
                 log_eff = np.log(pooled_es) if pooled_es > 0 else 0 # For direction check
            else:
                 # Linear scale, null is 0
                 is_significant = (pooled_lower > 0 and pooled_upper > 0) or (pooled_lower < 0 and pooled_upper < 0)
                 log_eff = pooled_es # Just for direction

            interpretation = "statistically significant" if is_significant else "not statistically significant"
            if is_significant:
                 direction = "increased risk/odds" if log_eff > 0 else "decreased risk/odds"
                 interpretation += f" ({direction})"

            def sanitize(val):
                if val is None: return None
                try:
                    if np.isnan(val) or np.isinf(val): return None
                    return float(round(val, 2))
                except:
                    return None

            headline = {
                 "canonical_rr": {"pooled_es": float(pooled_es), "ci_low": float(pooled_lower), "ci_upp": float(pooled_upper), "pi_low": float(pi_lower) if "pi_lower" in locals() and pi_lower is not None else None, "pi_upp": float(pi_upper) if "pi_upper" in locals() and pi_upper is not None else None},
                 "pooled_es": sanitize(pooled_es),
                 "ci_low": sanitize(pooled_lower),
                 "ci_upp": sanitize(pooled_upper),
                 "interpretation": interpretation,
                 "baseline_risk": p0,
                 "baseline_risk_percent": (round(p0 * 100, 10) if p0 is not None else None),
                 "effect_conversion_method": "baseline_conversion" if p0 is not None else "identity_approximation",
            }

        else:
            res = combine_effects(analysis_df['log_ES'], analysis_df['var'], method_re='dl')
            summary_df = res.summary_frame()

            # Explicitly update the index
            n_studies = len(analysis_df)
            new_index = list(analysis_df.index) + list(summary_df.index[n_studies:])
            summary_df.index = new_index

            # Cleanup table columns
            cols_to_drop = ['w_fe', 'w_re']
            summary_df = summary_df.drop(columns=cols_to_drop, errors='ignore')

            # Add Cases and Sample Size from analysis_df
            summary_df['Cases'] = analysis_df['Cases']
            summary_df['Sample Size'] = analysis_df['Sample Size']

            # Fill NaN for the pooled rows (since they aren't in analysis_df)
            summary_df['Cases'] = summary_df['Cases'].fillna('-')
            summary_df['Sample Size'] = summary_df['Sample Size'].fillna('-')

            # Rename columns
            summary_df = summary_df.rename(columns={
                'eff': 'Effect',
                'sd_eff': 'SD Effect',
                'ci_low': '95% CI lower',
                'ci_upp': '95% CI upper'
            })

            # Rename rows
            summary_df = summary_df.rename(index={
                'random effect wls': 'Random-effects meta-analysis (WLS)',
                'fixed effect wls': 'Fixed-effect meta-analysis (WLS)'
            })

            summary_df = summary_df.round(4)

            # Display DF
            display_df = summary_df.copy()
            rows_to_drop = ['Fixed-effect meta-analysis (WLS)', 'Random-effects meta-analysis (WLS)']
            rows_to_drop.extend(['fixed effect wls', 'random effect wls', 'fixed effect', 'random effect'])
            display_df = display_df.drop(index=[r for r in rows_to_drop if r in display_df.index], errors='ignore')

            summary = display_df.to_html(classes='table table-striped', header=True)

            # Extract Headline
            try:
                # Handle potential row naming variations from statsmodels combine_effects
                # Check for negative tau2 which causes numerical overflow in some DL implementations
                # where heterogeneity is extremely low (Q < df).
                re_keywords = ['random effect wls', 'random effect', 'Random-effects meta-analysis (WLS)']
                re_row = None

                is_invalid_re = False
                if getattr(res, 'tau2', 0) < 0:
                    is_invalid_re = True

                if not is_invalid_re:
                    for kw in re_keywords:
                        if kw in summary_df.index:
                            re_row = summary_df.loc[kw]
                            break

                if re_row is None or is_invalid_re:
                    # Fallback to fixed effect if random effect is invalid or not found
                    fe_keywords = ['fixed effect wls', 'fixed effect', 'Fixed-effect meta-analysis (WLS)']
                    for kw in fe_keywords:
                        if kw in summary_df.index:
                            re_row = summary_df.loc[kw]
                            if is_invalid_re:
                                print(f"WARNING: Negative tau2 ({res.tau2}) detected. Falling back to fixed effect.")
                            break

                if re_row is None:
                    # Fallback to last row if keywords not found
                    re_row = summary_df.iloc[-1]

                log_eff = float(re_row['Effect'].iloc[0]) if isinstance(re_row['Effect'], pd.Series) else float(re_row['Effect'])
                log_ci_low = float(re_row['95% CI lower'].iloc[0]) if isinstance(re_row['95% CI lower'], pd.Series) else float(re_row['95% CI lower'])
                log_ci_upp = float(re_row['95% CI upper'].iloc[0]) if isinstance(re_row['95% CI upper'], pd.Series) else float(re_row['95% CI upper'])

                pooled_es = np.exp(log_eff)
                pooled_lower = np.exp(log_ci_low)
                pooled_upper = np.exp(log_ci_upp)

                is_significant = (log_ci_low > 0) or (log_ci_upp < 0)

                interpretation = "statistically significant" if is_significant else "not statistically significant"

                if is_significant:
                    direction = "increased risk/odds" if log_eff > 0 else "decreased risk/odds"
                    interpretation += f" ({direction})"

                def sanitize(val):
                    if val is None: return None
                    try:
                        if np.isnan(val) or np.isinf(val): return None
                        return float(round(val, 2))
                    except:
                        return None

                # Heterogeneity metrics from statsmodels res object
                # Truncate to 0 if negative (standard practice for DL method when Q < df)
                i2 = max(0, getattr(res, 'i2', 0))
                tau2 = max(0, getattr(res, 'tau2', 0))

                # Prediction Interval
                pi_lower = None
                pi_upper = None
                if n_studies >= 3:
                    try:
                        import scipy.stats
                        t_val = scipy.stats.t.ppf(0.975, df=n_studies-2)
                        var_pooled = float(re_row['SD Effect'].iloc[0]**2) if isinstance(re_row['SD Effect'], pd.Series) else float(re_row['SD Effect']**2)
                        se_pred = np.sqrt(tau2 + var_pooled)
                        pi_lower = np.exp(log_eff - t_val * se_pred)
                        pi_upper = np.exp(log_eff + t_val * se_pred)
                    except Exception as e:
                        print(f"PI calculation failed: {e}")

                # Egger's Test
                eggers_intercept, eggers_p = eggers_test(analysis_df['log_ES'], analysis_df['log_SE'])

                # Funnel interpretation (non-fatal)
                funnel_interpretation = 'no interpretation available.'
                try:
                    funnel_interpretation = get_funnel_interpretation(disease, exposure, len(analysis_df), eggers_p, eggers_intercept)
                except Exception as e:
                    print(f"Funnel interpretation generation failed (non-fatal): {e}")

                # Baujat Plot (non-fatal)
                baujat_url = None
                if generate_plots:
                    try:
                        baujat_path = generate_baujat_plot(analysis_df, disease, exposure, outcome=outcome, exclude_meta=exclude_meta, plot_namespace=plot_namespace)
                        if baujat_path:
                            baujat_url = f"{baujat_path}?t=" + str(np.random.randint(0,10000))
                    except Exception as e:
                        print(f"Baujat plot generation failed (non-fatal): {e}")

                # Leave-one-out analysis (non-fatal)
                loo_results = []
                try:
                    loo_results = perform_leave_one_out(analysis_df)
                except Exception as e:
                    print(f"Leave-one-out analysis failed (non-fatal): {e}")

                def sanitize_pvalue(val):
                    if val is None: return None
                    try:
                        if np.isnan(val) or np.isinf(val): return None
                        return float(val)
                    except:
                        return None

                headline = {
                    "canonical_rr": {"pooled_es": float(pooled_es), "ci_low": float(pooled_lower), "ci_upp": float(pooled_upper), "pi_low": float(pi_lower) if "pi_lower" in locals() and pi_lower is not None else None, "pi_upp": float(pi_upper) if "pi_upper" in locals() and pi_upper is not None else None},
                 "pooled_es": sanitize(pooled_es),
                    "ci_low": sanitize(pooled_lower),
                    "ci_upp": sanitize(pooled_upper),
                    "pi_low": sanitize(pi_lower),
                    "pi_upp": sanitize(pi_upper),
                    "interpretation": capitalize_sentences(interpretation),
                    "i2": sanitize(i2 * 100), # Percentage
                    "tau2": sanitize(tau2),
                    "eggers_p": sanitize_pvalue(eggers_p),
                    "funnel_interpretation": funnel_interpretation,
                    "loo_results": loo_results,
                    "baseline_risk": p0,
                    "baseline_risk_percent": (round(p0 * 100, 10) if p0 is not None else None),
                    "effect_conversion_method": "baseline_conversion" if p0 is not None else "identity_approximation",
                }

                # Generate a rule-based narrative interpretation (non-fatal).
                try:
                    results_interp = get_results_interpretation(
                        disease, exposure, outcome, len(analysis_df),
                        sanitize(pooled_es), sanitize(pooled_lower), sanitize(pooled_upper),
                        sanitize(i2 * 100), interpretation
                    )
                    headline["results_interpretation"] = results_interp
                except Exception as e:
                    print(f"Results interpretation generation failed (non-fatal): {e}")
                    headline["results_interpretation"] = ""

            except Exception as e:
                print(f"Error parsing summary stats: {e}")
                headline = None

        try:
            # Calculate dynamic height based on number of studies to avoid overcrowding
            fp_df = df_clean.copy()
            num_studies = len(fp_df)

            # Keep every study label legible without producing an unnecessarily
            # enormous web asset. The forestplot package must receive figsize
            # directly; setting only pyplot's current figure is ignored.
            if num_studies > 70:
                dynamic_height = max(7, 0.25 * num_studies + 4)
            else:
                dynamic_height = max(7, 0.35 * num_studies + 3.5)

            # Dynamic font scaling
            if num_studies < 15: font_size = 10
            elif num_studies < 30: font_size = 8
            elif num_studies < 60: font_size = 6
            else: font_size = 5 # Smallest tier for extreme density

            plt.rcParams.update({'font.size': font_size})

            if not fp_df.empty:
                fp_df = fp_df.rename(columns={'Study': 'group', 'log_ES': 'est'})
                fp_df['lb'] = fp_df['est'] - 1.96 * fp_df['log_SE']
                fp_df['ub'] = fp_df['est'] + 1.96 * fp_df['log_SE']
                fp_df['label'] = fp_df['group']

                # Add human-readable annotation column for the plot using converted values
                def format_ci(row):
                    try:
                        return f"{float(row['converted_ES']):.2f} ({float(row['converted_Lower_CI']):.2f}, {float(row['converted_Upper_CI']):.2f})"
                    except:
                        return "N/A"

                fp_df['Est. RR (95% CI)'] = fp_df.apply(format_ci, axis=1)

                # Log scale check for xlabel
                is_log = any(is_eligible_effect_type(row['Effect Type']) for _, row in fp_df.iterrows())
                xlbl = "Log Relative Risk (95% CI)" if is_log else "Effect Size (95% CI)"

                forest_ax = forestplot.forestplot(
                    fp_df,
                    estimate="est",
                    ll="lb",
                    hl="ub",
                    varlabel="label",
                    rightannote=['Est. RR (95% CI)'],
                    right_annoteheaders=['Est. RR (95% CI)'],
                    xlabel=xlbl,
                    title=f"Forest Plot: {disease} vs {exposure}",
                    flush=True,
                    color_alt_rows=True,
                    figsize=(12, dynamic_height),
                )

                if generate_plots:
                    # Create exposure subfolder
                    exposure_dir = os.path.join("static", safe_exposure)
                    os.makedirs(exposure_dir, exist_ok=True)

                    # Generate unique filename using pre-defined safe strings
                    filename_base = f"forest_{safe_disease}_{safe_outcome}_{safe_meta}.png"

                    plot_path = os.path.join(exposure_dir, filename_base)
                    forest_ax.figure.savefig(plot_path, bbox_inches='tight')
                plt.close(forest_ax.figure)
            plt.close()
        except Exception as e:
            print(f"Forest plot failed: {e}")
            plt.close()

        # Funnel Plot
        try:
            if not df_clean.empty and 'pooled_es' in locals() and pooled_es > 0:
                plt.figure(figsize=(8, 6))

                # Data points
                plt.scatter(df_clean['log_ES'], df_clean['SE'], alpha=0.6, edgecolors='k')

                # Pooled Effect Line
                pooled_log = np.log(pooled_es)
                plt.axvline(x=pooled_log, color='red', linestyle='--', label='Pooled Effect')

                # Pseudo 95% CI Triangle
                max_se = df_clean['SE'].max()
                if not np.isnan(max_se) and max_se > 0:
                    max_se_padded = max_se * 1.1
                    se_seq = np.linspace(0, max_se_padded, 100)

                    ci_left = pooled_log - 1.96 * se_seq
                    ci_right = pooled_log + 1.96 * se_seq

                    plt.plot(ci_left, se_seq, 'k--', alpha=0.3)
                    plt.plot(ci_right, se_seq, 'k--', alpha=0.3)

                    # Invert Y axis (SE 0 is top)
                    plt.ylim(max_se_padded, 0)

                plt.xlabel('Log Effect Size')
                plt.ylabel('Standard Error')
                plt.title(f'Funnel Plot: {disease} vs {exposure}')
                plt.grid(True, alpha=0.2)

                if generate_plots:
                    # Generate unique filename for funnel plot using pre-defined safe strings
                    funnel_filename = f"funnel_{safe_disease}_{safe_outcome}_{safe_meta}.png"
                    exposure_dir = os.path.join("static", safe_exposure)
                    os.makedirs(exposure_dir, exist_ok=True)

                    funnel_path = os.path.join(exposure_dir, funnel_filename)
                    plt.savefig(funnel_path, bbox_inches='tight')
                plt.close()
        except Exception as e:
            print(f"Funnel plot failed: {e}")
            plt.close()

        # Convert df to records
        # Use df_all for the return list so the table shows everything
        # Gracefully handle fields absent from an older screening packet.
        cols_to_keep = ['Study', 'PMID', 'Effect ID', 'Direct Exposure', 'Displayed Exposure', 'Hierarchy Inherited', 'pool_in_parent_meta_analysis', 'Effect Size', 'Lower CI', 'Upper CI', 'P Value', 'Population', 'Location', 'Ethnicity', 'Ethnicity Basis', 'Ethnicity Supporting Text', 'Reference', 'Authors', 'Journal', 'Year', 'Link', 'Effect Type', 'SE', 'Sample Size', 'Cases', 'Estimated Cases', 'Design', 'Timing', 'Continent', 'Stage', 'include_subexposures', 'subexposure_source', 'subexposure_inherited', 'subexposure_projection_id', 'subexposure_pooling_selected', 'subexposure_pooling_reason', 'key_comparison', 'key_comparison_p_resolution', 'key_comparison_pending', 'comparison_candidate_track', 'display_only', 'display_only_reason', 'policy_poolable', 'Quality %', 'Quality Score', 'comparison_type', 'direction_imputed', 'direction_imputation_reason', 'effect_size_imputed', 'effect_size_imputation_method', 'ci_level_assumed', 'effect_is_adjusted', 'reported_effect_type', 'poolability_policy_version', 'JBI', *dietary_policy.AUDIT_FIELDS, 'exposure_measurement_type', 'exposure_measurement_supporting_text', 'extraction_supporting_text']

        cols_to_keep.extend([
            'direction_imputed', 'direction_imputation_reason',
            'direction_original_comparison', 'effect_inverted',
        ])
        from scripts.aggressive_prostate_policy import AUDIT_FIELDS
        cols_to_keep.extend(field for field in AUDIT_FIELDS if field in df_all.columns)
        for field,default in {'manual_review_pending':False,'provisional_ui_selection':None,
                'provisional_origin_context_id':None,'provisional_ui_display_only':False,
                'provisional_candidate_track':None}.items():
            cols_to_keep.append(field)
            if field not in df_all.columns:df_all[field]=default

        # The screening pipeline stores extraction keys in lower-case while the
        # public study table keeps its long-standing title-cased column names.
        if 'Ethnicity' not in df_all.columns and 'ethnicity' in df_all.columns:
            df_all['Ethnicity'] = df_all['ethnicity']

        cols_to_keep = [col for col in dict.fromkeys(cols_to_keep) if col not in dietary_policy.AUDIT_FIELDS or col in df_all.columns]

        # Ensure columns exist in df_all
        for col in cols_to_keep:
            if col not in df_all.columns:
                if col == 'exposure_measurement_type':
                    df_all[col] = 'unclear'
                elif col == 'exposure_measurement_supporting_text':
                    df_all[col] = ''
                elif col == 'extraction_supporting_text':
                    df_all[col] = None
                elif col == 'Ethnicity':
                    df_all[col] = 'Mixed'
                elif col == 'Location':
                    df_all[col] = 'NA'
                elif col in {'Ethnicity Basis', 'Ethnicity Supporting Text'}:
                    df_all[col] = 'NA'
                elif col == 'Direct Exposure':
                    df_all[col] = exposure
                elif col == 'Displayed Exposure':
                    df_all[col] = exposure
                elif col == 'Hierarchy Inherited':
                    df_all[col] = False
                elif col == 'pool_in_parent_meta_analysis':
                    df_all[col] = True
                elif col in {'direction_imputed', 'effect_inverted'}:
                    df_all[col] = False
                else:
                    df_all[col] = "-"

        # Older caches predate ethnicity extraction.  Keep the public table and
        # exports complete while allowing newly extracted values to pass through.
        df_all['Ethnicity'] = df_all['Ethnicity'].replace(
            {None: 'Mixed', '': 'Mixed', '-': 'Mixed'}
        ).fillna('Mixed')

        studies_data = df_all[cols_to_keep].to_dict(orient='records')

        return {
            "success": True,
            "studies": studies_data,
            "summary_html": summary,
            "headline": headline,
            "screening_stats": screening_stats,
            "plot_url": (
                f"static/{safe_exposure}/forest_{safe_disease}_{safe_outcome}_{safe_meta}.png?t="
                + str(np.random.randint(0,10000))
                if generate_plots else None
            ),
            "funnel_plot_url": (
                f"static/{safe_exposure}/funnel_{safe_disease}_{safe_outcome}_{safe_meta}.png?t="
                + str(np.random.randint(0,10000))
                if generate_plots else None
            ),
            "baujat_plot_url": baujat_url
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": f"Meta-analysis failed: {str(e)}"}


# Live requests use explicit finite-interval settings; offline workflows retain their API.
from analysis_settings_backend import configure_meta_analysis, capture_pooling_rows, settings_active
configure_meta_analysis(globals())
