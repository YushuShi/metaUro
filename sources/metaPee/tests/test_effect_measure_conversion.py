import unittest

import pandas as pd

from effect_measure import (
    baseline_risk_for_disease,
    baseline_risk_from_percent,
    convert_ratio_to_rr,
    load_disease_registry,
    normalize_effect_type,
    resolve_disease,
)
from meta_analysis import (
    add_effect_measure_conversion_columns,
    add_estimated_cases_to_row,
    perform_meta_analysis,
)


class EffectMeasureConversionTests(unittest.TestCase):
    def test_registry_loads_all_urinary_cancers_and_aliases(self):
        registry = load_disease_registry()
        self.assertEqual(len(registry), 12)
        self.assertEqual(next(iter(registry)), "bladder_cancer")
        self.assertEqual(registry["bladder_cancer"]["disease"], "Bladder cancer")
        self.assertEqual(registry["bladder_cancer"]["lifetime_risk_percent"], 2.1)
        self.assertIn("urinary bladder cancer", registry["bladder_cancer"]["aliases"])

        self.assertEqual(resolve_disease("urinary bladder tumour"), "Bladder cancer")
        self.assertEqual(resolve_disease("renal cell cancer"), "Renal cell carcinoma")
        self.assertEqual(
            resolve_disease("clear-cell renal carcinoma"),
            "Clear cell renal cell carcinoma",
        )

        # Callers receive a copy rather than mutable cached registry state.
        registry["bladder_cancer"]["aliases"].append("not a real alias")
        self.assertNotIn(
            "not a real alias",
            load_disease_registry()["bladder_cancer"]["aliases"],
        )

    def test_gender_combined_lifetime_incidence_is_used_for_each_disease(self):
        self.assertEqual(baseline_risk_for_disease("Bladder cancer"), 0.021)
        self.assertEqual(baseline_risk_for_disease("bladder carcinoma"), 0.021)
        self.assertAlmostEqual(
            baseline_risk_for_disease("renal cell cancer"), 0.0162
        )
        self.assertEqual(
            baseline_risk_for_disease("papillary renal carcinoma"), 0.0016
        )
        with self.assertRaises(ValueError):
            baseline_risk_for_disease("Prostate cancer")

    def test_lifetime_risk_percent_is_converted_to_probability(self):
        self.assertEqual(baseline_risk_from_percent(0.66), 0.0066)
        self.assertEqual(baseline_risk_from_percent(9.1), 0.091)

    def test_or_hr_and_irr_are_converted_to_rr(self):
        p0 = 0.021
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
        self.assertEqual(normalize_effect_type("SHR"), "HR")
        self.assertEqual(
            normalize_effect_type("subdistribution hazard ratio"), "HR"
        )

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

    def test_missing_cases_are_estimated_from_disease_lifetime_incidence(self):
        row = add_estimated_cases_to_row(
            {"Cases": None, "Sample Size": 1000},
            "kidney cancer",
        )
        self.assertEqual(row["Estimated Cases"], 18)

    def test_conversion_columns_preserve_exact_reported_measure(self):
        frame = pd.DataFrame([{
            "Effect Type": "adjusted odds ratio",
            "Effect Size": "1.90",
            "Lower CI": "1.40",
            "Upper CI": "2.70",
        }])
        converted = add_effect_measure_conversion_columns(frame, "bladder carcinoma")
        row = converted.iloc[0]

        self.assertEqual(row["Original Effect Type"], "adjusted odds ratio")
        self.assertEqual(row["Original Effect Size"], "1.90")
        self.assertEqual(row["Original Lower CI"], "1.40")
        self.assertEqual(row["Original Upper CI"], "2.70")
        self.assertEqual(row["Converted Effect Type"], "RR")
        self.assertTrue(row["Conversion Applied"])
        self.assertEqual(row["Conversion Baseline Risk"], 0.021)
        self.assertEqual(row["Conversion Baseline Risk Percent"], 2.1)
        self.assertAlmostEqual(
            row["Converted Effect Size"],
            convert_ratio_to_rr(1.9, "OR", 0.021),
        )

    def test_meta_analysis_uses_original_fields_and_disease_baseline(self):
        # The paper reported the inverse comparison. The standard fields have
        # already normalized its direction; conversion must use those values
        # while retaining the pre-inversion paper values for audit.
        frame = pd.DataFrame([{
            "Study": "Study 1",
            "PMID": "1",
            "Effect Type": "adjusted OR",
            "Effect Size": 2.0,
            "Lower CI": 1.25,
            "Upper CI": 2.5,
            "Original Effect Type": "adjusted OR",
            "Original Effect Type Reported Label": "multivariable-adjusted odds ratio",
            "Original Effect Size": "0.50",
            "Original Lower CI": "0.40",
            "Original Upper CI": "0.80",
            "Original P Value": "0.03",
            "effect_inverted": True,
            "Screening Record ID": "1|Iron|Bladder cancer|women|dietary",
            "SE": 0.1,
            "Cases": 100,
            "Sample Size": 1000,
            "gender": "Women",
            "smoking_status": "Never smokers",
        }])

        result = perform_meta_analysis(
            frame,
            "urinary bladder cancer",
            "Iron",
            df_all=frame,
            generate_plots=False,
        )

        self.assertNotIn("error", result)
        self.assertEqual(result["headline"]["baseline_risk"], 0.021)
        self.assertEqual(result["headline"]["baseline_risk_percent"], 2.1)
        study = result["studies"][0]
        self.assertEqual(study["Original Effect Type"], "adjusted OR")
        self.assertEqual(
            study["Original Effect Type Reported Label"],
            "multivariable-adjusted odds ratio",
        )
        self.assertEqual(study["Original Effect Size"], "0.50")
        self.assertEqual(study["Original P Value"], "0.03")
        self.assertTrue(study["effect_inverted"])
        self.assertEqual(
            study["Screening Record ID"],
            "1|Iron|Bladder cancer|women|dietary",
        )
        self.assertEqual(study["Gender"], "Women")
        self.assertEqual(study["Smoking Status"], "Never smokers")
        self.assertTrue(study["Conversion Applied"])
        self.assertAlmostEqual(
            study["Converted Effect Size"],
            convert_ratio_to_rr(2.0, "OR", 0.021),
        )

        rerun = add_effect_measure_conversion_columns(
            pd.DataFrame(result["studies"]), "Bladder cancer"
        ).iloc[0]
        self.assertEqual(rerun["Original Effect Size"], "0.50")
        self.assertAlmostEqual(
            rerun["Converted Effect Size"], study["Converted Effect Size"]
        )

    def test_wald_imputed_inverted_ci_does_not_fill_missing_original_ci(self):
        frame = pd.DataFrame([{
            "Study": "Wald Study",
            "PMID": "2",
            # These are the direction-normalized fields after Wald CI
            # imputation and inversion by the screening workflow.
            "Effect Type": "OR",
            "Effect Size": 2.0,
            "Lower CI": 1.2,
            "Upper CI": 3.4,
            # The paper reported an inverse-comparison effect and p value, but
            # no CI. The audit fields must continue to say exactly that.
            "Original Effect Type": "OR",
            "Original Effect Size": 0.5,
            "Original Lower CI": None,
            "Original Upper CI": None,
            "Original P Value": 0.04,
            "effect_inverted": True,
            "SE": 0.2,
            "Cases": 50,
            "Sample Size": 2000,
        }])

        result = perform_meta_analysis(
            frame,
            "Bladder cancer",
            "Coffee",
            df_all=frame,
            generate_plots=False,
        )

        self.assertNotIn("error", result)
        study = result["studies"][0]
        self.assertEqual(study["Original Effect Size"], 0.5)
        self.assertIsNone(study["Original Lower CI"])
        self.assertIsNone(study["Original Upper CI"])
        self.assertEqual(study["Original P Value"], 0.04)
        self.assertTrue(study["effect_inverted"])
        self.assertAlmostEqual(
            study["Converted Effect Size"],
            convert_ratio_to_rr(2.0, "OR", 0.021),
        )
        self.assertAlmostEqual(
            study["Converted Lower CI"],
            convert_ratio_to_rr(1.2, "OR", 0.021),
        )
        self.assertAlmostEqual(
            study["Converted Upper CI"],
            convert_ratio_to_rr(3.4, "OR", 0.021),
        )

    def test_multi_study_meta_analysis_uses_selected_disease_incidence(self):
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
            "Kidney and renal pelvis cancer",
            "Iron",
            df_all=frame,
            generate_plots=False,
        )

        self.assertNotIn("error", result)
        self.assertAlmostEqual(result["headline"]["baseline_risk"], 0.018)
        self.assertEqual(result["headline"]["baseline_risk_percent"], 1.8)
        self.assertGreater(result["headline"]["pooled_es"], 1)

    def test_multiple_strata_from_same_study_have_distinct_analysis_labels(self):
        frame = pd.DataFrame([
            {
                "Study": "Shared Study",
                "Screening Record ID": "123|Tea|Bladder cancer|women|dietary",
                "Effect Type": "RR",
                "Effect Size": 1.2,
                "Lower CI": 1.05,
                "Upper CI": 1.38,
                "SE": 0.07,
                "Cases": 90,
                "Sample Size": 900,
                "gender": "Women",
                "exposure_measurement_type": "dietary",
            },
            {
                "Study": "Shared Study",
                "Screening Record ID": "123|Tea|Bladder cancer|men|biospecimen",
                "Effect Type": "RR",
                "Effect Size": 1.45,
                "Lower CI": 1.12,
                "Upper CI": 1.88,
                "SE": 0.13,
                "Cases": 110,
                "Sample Size": 1100,
                "gender": "Men",
                "exposure_measurement_type": "biospecimen",
            },
        ], index=[7, 7])

        result = perform_meta_analysis(
            frame,
            "Bladder cancer",
            "Tea",
            df_all=frame,
            generate_plots=False,
        )

        self.assertNotIn("error", result)
        self.assertEqual(len(result["studies"]), 2)
        self.assertIn("Shared Study [stratum 1]", result["summary_html"])
        self.assertIn("Shared Study [stratum 2]", result["summary_html"])

    def test_hierarchy_children_are_displayed_but_not_pooled_in_parent(self):
        frame = pd.DataFrame([
            {
                "Study": "Direct parent estimate",
                "PMID": "1",
                "Screening Record ID": "1|Omega-3 fatty acids|Bladder cancer",
                "direct_exposure": "Omega-3 fatty acids",
                "inherited_exposure_memberships": [],
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
                "Screening Record ID": "2|Docosahexaenoic acid|Bladder cancer",
                "direct_exposure": "Docosahexaenoic acid",
                "inherited_exposure_memberships": ["Omega-3 fatty acids"],
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
            "Bladder cancer",
            "Omega-3 fatty acids",
            df_all=frame,
            generate_plots=False,
        )

        self.assertNotIn("error", result)
        self.assertAlmostEqual(result["headline"]["pooled_es"], 1.2)
        self.assertEqual(len(result["studies"]), 2)
        child = next(
            row for row in result["studies"]
            if row["direct_exposure"] == "Docosahexaenoic acid"
        )
        self.assertTrue(child["Hierarchy Inherited"])
        self.assertFalse(child["pool_in_parent_meta_analysis"])
        self.assertEqual(
            child["inherited_exposure_memberships"],
            ["Omega-3 fatty acids"],
        )


if __name__ == "__main__":
    unittest.main()
