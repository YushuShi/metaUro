import unittest

import pandas as pd

from effect_measure import (
    baseline_risk_for_disease,
    baseline_risk_from_percent,
    convert_ratio_to_rr,
    normalize_effect_type,
)
from meta_analysis import add_estimated_cases_to_row, perform_meta_analysis


class EffectMeasureConversionTests(unittest.TestCase):
    def test_prostate_cancer_lifetime_risk_is_thirteen_point_two_percent(self):
        self.assertEqual(baseline_risk_for_disease("Prostate cancer"), 0.132)
        self.assertEqual(baseline_risk_for_disease("Prostatic carcinoma"), 0.132)
        with self.assertRaises(ValueError):
            baseline_risk_for_disease("Breast cancer")

    def test_lifetime_risk_percent_is_converted_to_probability(self):
        self.assertEqual(baseline_risk_from_percent(0.66), 0.0066)
        self.assertEqual(baseline_risk_from_percent(9.1), 0.091)

    def test_aggressive_prostate_has_no_general_prostate_baseline(self):
        for disease in ("Aggressive prostate cancer", "aggressive_prostate_cancer",
                        "aggressive_prostate", "Aggressive prostatic carcinoma"):
            self.assertIsNone(baseline_risk_for_disease(disease), disease)
        for effect_type in ("RR", "OR", "HR", "IRR"):
            self.assertEqual(convert_ratio_to_rr(1.9, effect_type, None), 1.9)
        for value, effect_type in ((-1, "RR"), (float("nan"), "OR"), (1.2, "Beta")):
            with self.assertRaises(ValueError):
                convert_ratio_to_rr(value, effect_type, None)

    def test_or_hr_and_irr_are_converted_to_rr(self):
        p0 = 0.132
        self.assertAlmostEqual(
            convert_ratio_to_rr(1.9, "OR", p0),
            1.9 / (1 - p0 + p0 * 1.9),
        )
        expected_rate_rr = (1 - (1 - p0) ** 1.9) / p0
        self.assertAlmostEqual(convert_ratio_to_rr(1.9, "HR", p0), expected_rate_rr)
        self.assertAlmostEqual(convert_ratio_to_rr(1.9, "IRR", p0), expected_rate_rr)
        self.assertEqual(convert_ratio_to_rr(1.9, "RR", p0), 1.9)
        self.assertEqual(normalize_effect_type("incidence rate ratio"), "IRR")
        self.assertEqual(normalize_effect_type("rate ratio"), "IRR")

    def test_adjustment_qualifiers_normalize_to_base_ratio_measure(self):
        for label in ("adjusted odds ratio", "unadjusted OR", "multivariable OR"):
            self.assertEqual(normalize_effect_type(label), "OR")
        for label in ("adjusted risk ratio", "unadjusted relative risk", "adjusted RR"):
            self.assertEqual(normalize_effect_type(label), "RR")
        for label in ("age-adjusted hazard ratio", "unadjusted HR", "multivariable HR"):
            self.assertEqual(normalize_effect_type(label), "HR")
        for label in (
            "percent higher risk",
            "percent lower risk",
            "percent increase in risk",
            "32% higher risk",
        ):
            self.assertEqual(normalize_effect_type(label), "RR")
        self.assertEqual(normalize_effect_type("percent increase in odds"), "OR")
        self.assertIsNone(normalize_effect_type("linear regression coefficient (β)"))
        self.assertEqual(normalize_effect_type("relative risk ratio (RRR)"), "RR")
        self.assertEqual(normalize_effect_type("RRR"), "RR")

    def test_exact_subdistribution_hazard_aliases_use_hr_family(self):
        for label in (
            "SHR",
            "sHR",
            "sub-hazard",
            "subhazard",
            "subdistribution hazard",
            "sub-hazard ratio",
            "subhazard ratio",
            "subdistribution hazard ratio",
        ):
            self.assertEqual(normalize_effect_type(label), "HR", label)
            self.assertAlmostEqual(
                convert_ratio_to_rr(1.2, label, 0.132),
                convert_ratio_to_rr(1.2, "HR", 0.132),
            )
        self.assertIsNone(normalize_effect_type("adjusted SHR"))

    def test_missing_cases_are_estimated_from_prostate_lifetime_risk(self):
        row = add_estimated_cases_to_row(
            {"Cases": None, "Sample Size": 1000},
            "Prostate cancer",
        )
        self.assertEqual(row["Estimated Cases"], 132)

    def test_prostate_meta_analysis_uses_thirteen_point_two_percent(self):
        frame = pd.DataFrame([
            {"Study": "Study 1", "Effect Type": "RR", "Effect Size": 1.31, "Lower CI": 1.07, "Upper CI": 1.61},
            {"Study": "Study 2", "Effect Type": "OR", "Effect Size": 1.90, "Lower CI": 1.40, "Upper CI": 2.70},
            {"Study": "Study 3", "Effect Type": "OR", "Effect Size": 1.70, "Lower CI": 0.90, "Upper CI": 3.30},
            {"Study": "Study 4", "Effect Type": "OR", "Effect Size": 1.66, "Lower CI": 1.02, "Upper CI": 2.69},
        ])
        frame["SE"] = (frame["Upper CI"] - frame["Lower CI"]) / 3.92
        frame["Cases"] = 100
        frame["Sample Size"] = 1000

        result = perform_meta_analysis(
            frame,
            "Prostate cancer",
            "Iron",
            df_all=frame,
            generate_plots=False,
        )

        self.assertNotIn("error", result)
        self.assertEqual(result["headline"]["baseline_risk"], 0.132)
        self.assertEqual(result["headline"]["baseline_risk_percent"], 13.2)
        self.assertGreater(result["headline"]["pooled_es"], 1)

    def test_hierarchy_children_are_displayed_but_not_pooled_in_parent(self):
        frame = pd.DataFrame([
            {
                "Study": "Direct parent estimate",
                "PMID": "1",
                "Direct Exposure": "Omega-3 fatty acids",
                "Displayed Exposure": "Omega-3 fatty acids",
                "Hierarchy Inherited": False,
                "pool_in_parent_meta_analysis": True,
                "Effect Type": "RR",
                "Effect Size": 1.2,
                "Lower CI": 1.1,
                "Upper CI": 1.3,
                "SE": 0.04,
                "Cases": 100,
                "Sample Size": 1000,
            },
            {
                "Study": "Screened DHA child",
                "PMID": "2",
                "Direct Exposure": "Docosahexaenoic acid",
                "Displayed Exposure": "Omega-3 fatty acids",
                "Hierarchy Inherited": True,
                "pool_in_parent_meta_analysis": False,
                "Effect Type": "RR",
                "Effect Size": 4.0,
                "Lower CI": 3.0,
                "Upper CI": 5.0,
                "SE": 0.13,
                "Cases": 100,
                "Sample Size": 1000,
            },
        ])

        result = perform_meta_analysis(
            frame,
            "Prostate cancer",
            "Omega-3 fatty acids",
            df_all=frame,
            generate_plots=False,
        )

        self.assertNotIn("error", result)
        self.assertAlmostEqual(result["headline"]["pooled_es"], 1.2)
        self.assertEqual(len(result["studies"]), 2)
        child = next(
            row for row in result["studies"]
            if row["Direct Exposure"] == "Docosahexaenoic acid"
        )
        self.assertTrue(child["Hierarchy Inherited"])
        self.assertFalse(child["pool_in_parent_meta_analysis"])


if __name__ == "__main__":
    unittest.main()
