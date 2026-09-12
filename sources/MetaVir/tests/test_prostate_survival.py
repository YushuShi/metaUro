import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from scripts import dual_screening
from scripts import prostate_survival as survival


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "prostate_survival_studies.json"


class ProstateSurvivalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.candidates = cls.fixture["candidates"]
        cls.estimates = cls.fixture["estimates"]

    def normalized_estimates(self):
        return [survival.normalize_estimate(copy.deepcopy(row)) for row in self.estimates]

    def assert_one_sentence(self, reason):
        self.assertIsInstance(reason, str)
        self.assertTrue(reason.strip())
        self.assertEqual(dual_screening.one_sentence(reason), reason)

    @staticmethod
    def records(value):
        if isinstance(value, dict):
            return list(value.values())
        return list(value)

    def test_survival_models_follow_luna_sol_luna_contract(self):
        self.assertEqual(survival.FIRST_MODEL, "gpt-5.6-luna")
        self.assertEqual(survival.SECOND_MODEL, "gpt-5.6-sol")
        self.assertEqual(survival.FULL_TEXT_MODEL, "gpt-5.6-luna")

    def test_survival_extraction_schema_is_multi_estimate_and_full_text_is_include_only(self):
        second = survival.response_schema("second")["schema"]
        estimate = second["properties"]["results"]["items"]["properties"][
            "estimates"
        ]["items"]
        self.assertTrue(set(survival.EXTRACTION_FIELDS).issubset(estimate["properties"]))
        self.assertTrue(set(survival.EXTRACTION_FIELDS).issubset(estimate["required"]))
        self.assertEqual(
            estimate["properties"]["exposure_timing"]["enum"],
            list(survival.EXPOSURE_TIMINGS),
        )
        self.assertEqual(
            estimate["properties"]["outcome"]["enum"],
            list(survival.SURVIVAL_OUTCOMES),
        )
        full = survival.response_schema("full_text")["schema"]
        decision = full["properties"]["results"]["items"]["properties"]["decision"]
        self.assertEqual(decision["enum"], ["INCLUDE"])

    def test_exposure_catalog_is_the_exact_union_of_all_ontologies(self):
        synonyms = json.loads(
            (ROOT / "data" / "synonyms_cache.json").read_text(encoding="utf-8")
        )
        additional = json.loads(
            (ROOT / "data" / "additionalExposure.json").read_text(encoding="utf-8")
        )
        additional_v2 = json.loads(
            (ROOT / "data" / "additionalExposureV2.json").read_text(encoding="utf-8")
        )
        catalog = survival.load_exposure_catalog()

        self.assertEqual(
            set(catalog), set(synonyms) | set(additional) | set(additional_v2)
        )
        self.assertFalse(set(synonyms) & set(additional))
        self.assertFalse(set(synonyms) & set(additional_v2))
        self.assertFalse(set(additional) & set(additional_v2))
        self.assertIn("DASH diet", catalog)
        self.assertIn("fish-oil supplement", {term.casefold() for term in catalog["Fish oil"]})
        self.assertIn("omega-7", {term.casefold() for term in catalog["Omega-7 fatty acids"]})
        self.assertIn("omega-9", {term.casefold() for term in catalog["Omega-9 fatty acids"]})

    def test_incidence_only_study_and_existing_meta_analysis_are_excluded(self):
        incidence, review, eligible_survival = self.candidates

        incidence_eligible, incidence_code, incidence_reason = (
            survival.classify_candidate(incidence)
        )
        self.assertFalse(incidence_eligible)
        self.assertTrue(incidence_code)
        self.assertIn("incidence", incidence_reason.casefold())
        self.assert_one_sentence(incidence_reason)

        review_eligible, review_code, review_reason = survival.classify_candidate(review)
        self.assertFalse(review_eligible)
        self.assertTrue(review_code)
        self.assertTrue(
            {"review", "meta-analysis"} & set(review_reason.casefold().split())
            or "meta-analysis" in review_reason.casefold()
        )
        self.assert_one_sentence(review_reason)

        included, code, reason = survival.classify_candidate(eligible_survival)
        self.assertTrue(included)
        self.assertIsNone(code)
        self.assert_one_sentence(reason)

        for candidate in (incidence, review):
            self.assertTrue(candidate["pmid"])

        mixed_report = copy.deepcopy(incidence)
        mixed_report["abstract"] += (
            " A separate analysis among men already diagnosed with prostate cancer "
            "evaluated overall survival."
        )
        self.assertTrue(survival.classify_candidate(mixed_report)[0])

        newly_diagnosed_survival = {
            "pmid": "90000004",
            "title": "Diet and survival in newly diagnosed prostate cancer patients",
            "abstract": "Patients were followed from diagnosis for survival outcomes.",
            "publication_types": ["Journal Article"],
        }
        self.assertTrue(survival.classify_candidate(newly_diagnosed_survival)[0])

    def test_normalization_retains_required_study_and_clinical_fields(self):
        source = next(row for row in self.estimates if row["pmid"] == "90010002")
        row = survival.normalize_estimate(copy.deepcopy(source))
        required = {
            "pmid",
            "pmcid",
            "publication_year",
            "cohort_name",
            "country",
            "ethnicity",
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
            "reference_group",
            "adjustment_variables",
        }
        self.assertTrue(required.issubset(row))
        self.assertEqual(row["cohort_name"], "Health Professionals Follow-up Study")
        self.assertEqual(
            row["prostate_cancer_stage"],
            "Localized prostate cancer (T1-T2, N0 M0)",
        )
        self.assertEqual(row["prostate_cancer_stage_category"], "localized")
        self.assertEqual(row["disease_setting"], "after_prostatectomy")
        self.assertEqual(row["primary_treatment"], "Radical prostatectomy")
        self.assertEqual(row["country"], "United States")
        self.assertEqual(row["ethnicity"], "Predominantly White men")

    def test_missing_event_count_stays_missing_and_is_never_incidence_imputed(self):
        source = next(row for row in self.estimates if row["pmid"] == "90010005")
        row = survival.normalize_estimate(copy.deepcopy(source))

        self.assertIsNone(row["number_of_events"])
        self.assertNotIn("estimated_events", row)
        self.assertNotIn("estimated_cases", row)

    def test_stage_and_disease_setting_classifications_use_closed_vocabularies(self):
        template = next(row for row in self.estimates if row["pmid"] == "90010002")
        stage_categories = (
            "localized",
            "locally_advanced",
            "nonmetastatic",
            "metastatic",
            "mixed",
            "unclear",
        )
        disease_settings = (
            "active_surveillance",
            "after_prostatectomy",
            "after_radiotherapy",
            "biochemical_recurrence",
            "metastatic_hormone_sensitive",
            "castration_resistant",
            "mixed",
            "unclear",
        )

        for index, category in enumerate(stage_categories):
            row = copy.deepcopy(template)
            row["pmid"] = str(90300000 + index)
            row["prostate_cancer_stage"] = f"Author-reported stage {index}"
            row["prostate_cancer_stage_category"] = category
            normalized = survival.normalize_estimate(row)
            self.assertEqual(normalized["prostate_cancer_stage"], row["prostate_cancer_stage"])
            self.assertEqual(normalized["prostate_cancer_stage_category"], category)

        for index, setting in enumerate(disease_settings):
            row = copy.deepcopy(template)
            row["pmid"] = str(90400000 + index)
            row["disease_setting"] = setting
            normalized = survival.normalize_estimate(row)
            self.assertEqual(normalized["disease_setting"], setting)

        invalid_stage = copy.deepcopy(template)
        invalid_stage["prostate_cancer_stage_category"] = "stage_ii"
        with self.assertRaises(ValueError):
            survival.normalize_estimate(invalid_stage)

        invalid_setting = copy.deepcopy(template)
        invalid_setting["disease_setting"] = "adjuvant_therapy"
        with self.assertRaises(ValueError):
            survival.normalize_estimate(invalid_setting)

    def test_prediagnosis_postdiagnosis_both_and_outcomes_are_separate_groups(self):
        groups = survival.group_estimates(self.normalized_estimates())
        pre_key = ("Fish oil", "prediagnosis", "overall_survival")
        post_key = ("Fish oil", "postdiagnosis", "overall_survival")
        both_key = ("Fish oil", "both", "overall_survival")
        cancer_specific_key = (
            "Fish oil",
            "prediagnosis",
            "prostate_cancer_specific_survival",
        )
        progression_key = ("Fish oil", "prediagnosis", "progression_free_survival")

        for key in (pre_key, post_key, both_key, cancer_specific_key, progression_key):
            self.assertIn(key, groups)
        self.assertTrue(all(row["exposure_timing"] == "prediagnosis" for row in groups[pre_key]))
        self.assertTrue(all(row["exposure_timing"] == "postdiagnosis" for row in groups[post_key]))
        self.assertTrue(all(row["exposure_timing"] == "both" for row in groups[both_key]))
        self.assertTrue(all(row["outcome"] == "overall_survival" for row in groups[pre_key]))
        self.assertTrue(
            all(
                row["outcome"] == "prostate_cancer_specific_survival"
                for row in groups[cancer_specific_key]
            )
        )
        self.assertFalse(
            {row["pmid"] for row in groups[pre_key]}
            & {row["pmid"] for row in groups[post_key]}
        )

    def test_all_timing_and_survival_outcome_categories_are_supported(self):
        template = next(row for row in self.estimates if row["pmid"] == "90010002")
        timings = ("prediagnosis", "postdiagnosis", "both", "unclear")
        outcomes = (
            "overall_survival",
            "prostate_cancer_specific_survival",
            "progression_free_survival",
            "metastasis_free_survival",
            "biochemical_recurrence_free_survival",
        )

        rows = []
        for index, outcome in enumerate(outcomes):
            row = copy.deepcopy(template)
            row["pmid"] = str(90100000 + index)
            row["cohort_name"] = f"Synthetic outcome cohort {index}"
            row["outcome"] = outcome
            row["outcome_definition"] = f"Synthetic definition for {outcome}"
            rows.append(survival.normalize_estimate(row))
        outcome_groups = survival.group_estimates(rows)
        self.assertEqual(
            {key[2] for key in outcome_groups},
            set(outcomes),
        )

        for index, timing in enumerate(timings):
            row = copy.deepcopy(template)
            row["pmid"] = str(90200000 + index)
            row["cohort_name"] = f"Synthetic timing cohort {index}"
            row["exposure_timing"] = timing
            normalized = survival.normalize_estimate(row)
            self.assertEqual(normalized["exposure_timing"], timing)

        invalid = copy.deepcopy(template)
        invalid["exposure_timing"] = "peri-diagnosis"
        with self.assertRaises(ValueError):
            survival.normalize_estimate(invalid)

    def test_overlapping_reports_from_one_cohort_are_not_double_counted(self):
        adjusted_hr_rows = [
            row
            for row in self.normalized_estimates()
            if row["effect_measure"] == "HR" and row.get("is_adjusted") is True
        ]
        kept, exclusions = survival.select_independent_estimates(adjusted_hr_rows)
        kept_pmids = {row["pmid"] for row in kept}
        excluded_by_pmid = {str(row["pmid"]): row for row in exclusions}

        self.assertIn("90010002", kept_pmids)
        self.assertNotIn("90010001", kept_pmids)
        self.assertIn("90010001", excluded_by_pmid)
        self.assert_one_sentence(excluded_by_pmid["90010001"]["reason"])

        # Same cohort remains independent when either timing or outcome differs.
        self.assertIn("90010004", kept_pmids)
        self.assertIn("90010006", kept_pmids)

        younger = copy.deepcopy(
            next(row for row in self.estimates if row["pmid"] == "90010002")
        )
        older = copy.deepcopy(younger)
        younger.update(pmid="90500001", study_population="Men aged under 65")
        older.update(pmid="90500002", study_population="Men aged 65 or older")
        disjoint_kept, disjoint_exclusions = survival.select_independent_estimates(
            [younger, older]
        )
        self.assertEqual({row["pmid"] for row in disjoint_kept}, {"90500001", "90500002"})
        self.assertFalse(disjoint_exclusions)

    def test_adjusted_hr_is_used_directly_on_the_log_scale(self):
        row = survival.normalize_estimate(
            copy.deepcopy(next(row for row in self.estimates if row["pmid"] == "90010002"))
        )
        log_hr, log_se = survival.log_hr_and_se(row)

        self.assertAlmostEqual(log_hr, math.log(0.8), places=12)
        self.assertAlmostEqual(
            log_se,
            (math.log(0.94) - math.log(0.68)) / 3.92,
            places=12,
        )

        reversed_row = copy.deepcopy(row)
        reversed_row["effect_estimate"] = 1 / 0.8
        reversed_row["lower_ci"] = 1 / 0.94
        reversed_row["upper_ci"] = 1 / 0.68
        reversed_row["needs_inversion"] = True
        reversed_log_hr, reversed_se = survival.log_hr_and_se(reversed_row)
        self.assertAlmostEqual(reversed_log_hr, math.log(0.8), places=12)
        self.assertAlmostEqual(reversed_se, log_se, places=12)

        rr = survival.normalize_estimate(
            copy.deepcopy(next(row for row in self.estimates if row["pmid"] == "90010007"))
        )
        with self.assertRaises(ValueError):
            survival.log_hr_and_se(rr)

    def test_quantitative_full_text_gate_requires_a_pmcid(self):
        complete = survival.normalize_estimate(
            copy.deepcopy(next(row for row in self.estimates if row["pmid"] == "90010002"))
        )
        missing_effect = copy.deepcopy(complete)
        missing_effect["effect_estimate"] = None

        self.assertTrue(survival.needs_quantitative_full_text(missing_effect))
        missing_effect["pmcid"] = ""
        self.assertFalse(survival.needs_quantitative_full_text(missing_effect))
        self.assertFalse(survival.needs_quantitative_full_text(complete))

        non_hr = copy.deepcopy(complete)
        non_hr["effect_measure"] = "RR"
        self.assertTrue(survival.needs_quantitative_full_text(non_hr))
        non_95 = copy.deepcopy(complete)
        non_95["ci_confidence_level"] = 90
        self.assertTrue(survival.needs_quantitative_full_text(non_95))
        unadjusted = copy.deepcopy(complete)
        unadjusted["is_adjusted"] = False
        self.assertTrue(survival.needs_quantitative_full_text(unadjusted))

    def test_full_text_merge_preserves_sol_fields_and_omitted_estimates(self):
        sol_first = copy.deepcopy(
            next(row for row in self.estimates if row["pmid"] == "90010002")
        )
        sol_second = copy.deepcopy(
            next(row for row in self.estimates if row["pmid"] == "90010006")
        )
        luna_partial = copy.deepcopy(sol_first)
        luna_partial["sample_size"] = None
        luna_partial["number_of_events"] = 260
        luna_partial["effect_estimate"] = 0.79
        luna_partial["adjustment_variables"] = []
        sol_first["needs_inversion"] = True
        luna_partial["is_adjusted"] = False
        luna_partial["needs_inversion"] = False
        luna_partial["exposure_alias"] = "Fish oil"
        luna_partial["outcome_definition"] = "overall_survival"
        luna_partial["evidence"] = {
            "number_of_events": {
                "excerpt": "There were 260 deaths.",
                "source": "full_text",
                "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC90010002/",
                "location": "Results",
            },
            "effect_estimate": {
                "excerpt": "HR 0.79",
                "source": "full_text",
                "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC90010002/",
                "location": "Table 2",
            },
        }

        merged = survival.merge_full_text_estimates(
            [sol_first, sol_second], [luna_partial]
        )
        by_outcome = {row["outcome"]: row for row in merged}
        self.assertEqual(len(merged), 2)
        self.assertEqual(by_outcome["overall_survival"]["sample_size"], 1200)
        self.assertEqual(by_outcome["overall_survival"]["number_of_events"], 260)
        self.assertEqual(by_outcome["overall_survival"]["effect_estimate"], 0.79)
        self.assertEqual(len(by_outcome["overall_survival"]["adjustment_variables"]), 7)
        self.assertTrue(by_outcome["overall_survival"]["is_adjusted"])
        self.assertTrue(by_outcome["overall_survival"]["needs_inversion"])
        self.assertEqual(
            by_outcome["overall_survival"]["exposure_alias"],
            "fish-oil supplement use",
        )
        self.assertEqual(
            by_outcome["overall_survival"]["outcome_definition"],
            "Death from any cause",
        )
        self.assertIn("prostate_cancer_specific_survival", by_outcome)

    def test_meta_analyses_are_hr_only_deduplicated_and_stratum_specific(self):
        result = survival.run_meta_analyses(self.normalized_estimates())
        self.assertEqual(set(result), {"analyses", "insufficient_groups", "exclusions"})

        analyses = self.records(result["analyses"])
        by_key = {
            (row["exposure"], row["exposure_timing"], row["outcome"]): row
            for row in analyses
        }
        pre_key = ("Fish oil", "prediagnosis", "overall_survival")
        post_key = ("Fish oil", "postdiagnosis", "overall_survival")
        cancer_specific_key = (
            "Fish oil",
            "prediagnosis",
            "prostate_cancer_specific_survival",
        )

        self.assertIn(pre_key, by_key)
        self.assertIn(post_key, by_key)
        self.assertNotIn(cancer_specific_key, by_key)
        self.assertEqual(by_key[pre_key]["k"], 2)
        self.assertEqual(by_key[post_key]["k"], 2)
        for analysis in (by_key[pre_key], by_key[post_key]):
            self.assertGreater(analysis["pooled_hr"], 0)
            self.assertLess(analysis["lower_ci"], analysis["pooled_hr"])
            self.assertLess(analysis["pooled_hr"], analysis["upper_ci"])

        insufficient = self.records(result["insufficient_groups"])
        insufficient_keys = {
            (row["exposure"], row["exposure_timing"], row["outcome"])
            for row in insufficient
        }
        self.assertIn(cancer_specific_key, insufficient_keys)
        self.assertIn(("Fish oil", "both", "overall_survival"), insufficient_keys)
        self.assertIn(("Fish oil", "prediagnosis", "progression_free_survival"), insufficient_keys)

        exclusions = self.records(result["exclusions"])
        excluded_pmids = {str(row["pmid"]) for row in exclusions}
        self.assertTrue({"90010001", "90010007", "90010008"}.issubset(excluded_pmids))
        for exclusion in exclusions:
            self.assertTrue(str(exclusion["pmid"]).strip())
            self.assert_one_sentence(exclusion["reason"])

    def test_continuous_scales_and_incompatible_progression_definitions_do_not_pool(self):
        template = copy.deepcopy(
            next(row for row in self.estimates if row["pmid"] == "90010002")
        )
        per_one = copy.deepcopy(template)
        per_ten = copy.deepcopy(template)
        per_one.update(
            pmid="90600001",
            cohort_name="Per-unit cohort one",
            comparison_type="per_unit",
            exposure_contrast="HR per 1 g/day",
            contrast_scale="1 g/day",
        )
        per_ten.update(
            pmid="90600002",
            cohort_name="Per-unit cohort ten",
            comparison_type="per_unit",
            exposure_contrast="HR per 10 g/day",
            contrast_scale="10 g/day",
        )
        scale_result = survival.run_meta_analyses([per_one, per_ten])
        self.assertFalse(scale_result["analyses"])
        self.assertEqual(
            {row["contrast_scale"] for row in scale_result["insufficient_groups"]},
            {"1 g/day", "10 g/day"},
        )

        radiographic = copy.deepcopy(template)
        biochemical = copy.deepcopy(template)
        radiographic.update(
            pmid="90600003",
            cohort_name="Radiographic cohort",
            outcome="progression_free_survival",
            outcome_definition="Radiographic progression on imaging",
        )
        biochemical.update(
            pmid="90600004",
            cohort_name="Biochemical cohort",
            outcome="progression_free_survival",
            outcome_definition="PSA biochemical progression",
        )
        outcome_result = survival.run_meta_analyses([radiographic, biochemical])
        self.assertFalse(outcome_result["analyses"])
        self.assertEqual(
            {
                row["outcome_definition_family"]
                for row in outcome_result["insufficient_groups"]
            },
            {"radiographic_progression", "biochemical_progression"},
        )

    def test_analyze_writes_separate_survival_plots_and_exclusion_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "studies.json").write_text(
                json.dumps({"schema_version": 1, "studies": self.estimates}),
                encoding="utf-8",
            )
            (output_dir / "exclusions.json").write_text(
                json.dumps({"schema_version": 1, "exclusions": []}),
                encoding="utf-8",
            )
            result = survival.analyze(output_dir)

            self.assertTrue((output_dir / "meta_analyses.json").is_file())
            self.assertTrue((output_dir / result["heterogeneity_plot"]).is_file())
            self.assertTrue(result["analyses"])
            for analysis in result["analyses"]:
                self.assertTrue((output_dir / analysis["forest_plot"]).is_file())
            ledger = json.loads(
                (output_dir / "exclusions.json").read_text(encoding="utf-8")
            )
            self.assertTrue(ledger["exclusions"])
            self.assertTrue(
                all(item.get("pmid") and item.get("reason") for item in ledger["exclusions"])
            )


if __name__ == "__main__":
    unittest.main()
