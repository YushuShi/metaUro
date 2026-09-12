import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import app as web_app
from scripts import build_combined_results as combined_builder


REPO_ROOT = Path(__file__).resolve().parents[1]
COMBINED_ROOT = REPO_ROOT / "combined"
COMPARISON_ROOT = REPO_ROOT / "outputs" / "chatgpt_claude_gemini_comparison_2026-09-03"
FABLE_ROOT = REPO_ROOT / "outputs" / "fable51_discrepancy_adjudication_2026-09-03"


def cache(slug: str, suffix: str = "core") -> dict:
    path = (
        COMBINED_ROOT
        / slug
        / f"prostate_cancer_incidence_true_{suffix}.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def study(slug: str, pmid: str) -> dict:
    matches = [row for row in cache(slug)["studies"] if row["PMID"] == pmid]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {slug}/{pmid} row, found {len(matches)}")
    return matches[0]


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class CombinedBuilderPolicyTests(unittest.TestCase):
    def base_adjudication(self):
        return {
            "fable51_adjudication_status": "MODEL_ADJUDICATED",
            "title_abstract_mesh_assessment": "SUFFICIENT",
            "fable51_manual_review_needed": "false",
            "fable51_verdict": "CLAUDE_GEMINI_CORRECT",
            "fable51_evidence_source": "ABSTRACT",
            "fable51_supporting_excerpt": "A directly supporting abstract excerpt.",
        }

    def test_automatic_gate_requires_sufficient_unflagged_model_result(self):
        row = self.base_adjudication()
        self.assertEqual(combined_builder.automatic_choice(row), "CLAUDE_GEMINI")

        for field, value in (
            ("fable51_adjudication_status", "PROVIDER_CONTENT_FILTERED"),
            ("title_abstract_mesh_assessment", "INSUFFICIENT"),
            ("fable51_manual_review_needed", "true"),
            ("fable51_verdict", "UNSURE"),
            ("fable51_verdict", "NEITHER_CORRECT"),
        ):
            with self.subTest(field=field, value=value):
                changed = dict(row)
                changed[field] = value
                self.assertIsNone(combined_builder.automatic_choice(changed))

        for field, value in (
            ("fable51_evidence_source", "NONE"),
            ("fable51_supporting_excerpt", ""),
        ):
            with self.subTest(field=field, value=value):
                changed = dict(row)
                changed[field] = value
                self.assertIsNone(combined_builder.automatic_choice(changed))

    def test_csv_boolean_parser_does_not_treat_false_as_truthy(self):
        self.assertFalse(combined_builder.strict_bool("false", field="test"))
        self.assertTrue(combined_builder.strict_bool("true", field="test"))
        with self.assertRaises(ValueError):
            combined_builder.strict_bool("0", field="test")

    def test_claude_status_normalization_distinguishes_exclude_and_not_poolable(self):
        self.assertEqual(
            combined_builder.normalized_status(
                {"eligibility": "EXCLUDE"}, claude_gemini=True
            ),
            "EXCLUDE",
        )
        self.assertEqual(
            combined_builder.normalized_status(
                {"eligibility": "INCLUDE", "needs_full_text": True},
                claude_gemini=True,
            ),
            "ELIGIBLE_NOT_POOLABLE",
        )
        self.assertEqual(
            combined_builder.normalized_status(
                {"eligibility": "INCLUDE", "needs_full_text": False},
                claude_gemini=True,
            ),
            "INCLUDE_META",
        )

    def test_fable_csv_requires_exact_model_scope_and_context(self):
        row = {
            "PMID": "1",
            "exposure": "Tea",
            "cancer": "Prostate cancer",
            "context_id": "1|Tea|Prostate cancer",
            "discrepancy_type": "SCREENING",
            "discrepant_field": "SCREENING_DECISION",
            "fable51_verdict": "CHATGPT_CORRECT",
            "fable51_manual_review_needed": "false",
            "fable51_adjudication_status": "MODEL_ADJUDICATED",
            "title_abstract_mesh_assessment": "SUFFICIENT",
            "fable51_evidence_source": "ABSTRACT",
            "fable51_supporting_excerpt": "Supporting abstract text.",
            "one_sentence_explanation": "The abstract supports ChatGPT.",
            "model_requested": "claude-fable-5-1",
            "model_reported": "claude-fable-5-1",
            "source_scope": "TITLE_ABSTRACT_MESH_ONLY",
        }

        def load(candidate):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fable.csv"
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, fieldnames=list(candidate), lineterminator="\n"
                    )
                    writer.writeheader()
                    writer.writerow(candidate)
                return combined_builder.load_fable_rows(path)

        self.assertEqual(load(row)[0]["context_id"], row["context_id"])
        for field, value in (
            ("model_requested", "claude-sonnet-4-5"),
            ("model_reported", "claude-sonnet-4-5"),
            ("source_scope", "FULL_TEXT"),
            ("context_id", "2|Tea|Prostate cancer"),
        ):
            with self.subTest(field=field):
                changed = dict(row)
                changed[field] = value
                with self.assertRaises(ValueError):
                    load(changed)

    @staticmethod
    def evidence(excerpt="Directly supporting text."):
        return {"excerpt": excerpt, "source": "full_text", "location": "page 2"}

    def estimate_override(self, field="EFFECT_SIZE", effect_size=1.2):
        return {
            "context_id": "1|Tea|Prostate cancer",
            "discrepant_field": field,
            "selected_pipeline": "CUSTOM",
            "custom_reason": "Curator verified the estimate.",
            "custom_value": {
                "effect_size": effect_size,
                "effect_type": "RR",
                "ci_lower": 1.0,
                "ci_upper": 1.4,
                "p_value": None,
                "comparison_type": "HIGH_VS_LOW",
                "needs_inversion": False,
                "cases": 20,
            },
            "custom_evidence": {
                "effect_size": self.evidence(),
                "comparison": self.evidence(),
                "confidence_interval": self.evidence(),
            },
        }

    def test_custom_overrides_require_reason_evidence_and_atomic_estimate_bundle(self):
        sample_rows = [
            {
                "context_id": "1|Tea|Prostate cancer",
                "discrepant_field": "SAMPLE_SIZE",
            }
        ]
        sample = {
            "context_id": "1|Tea|Prostate cancer",
            "discrepant_field": "SAMPLE_SIZE",
            "selected_pipeline": "CUSTOM",
            "custom_value": 100,
        }
        with self.assertRaises(ValueError):
            combined_builder.manual_override_map({"overrides": [sample]}, sample_rows)
        sample["custom_reason"] = "Verified in the report."
        with self.assertRaises(ValueError):
            combined_builder.manual_override_map({"overrides": [sample]}, sample_rows)

        estimate_rows = [
            {
                "context_id": "1|Tea|Prostate cancer",
                "discrepant_field": field,
            }
            for field in ("EFFECT_SIZE", "CI_OR_P_VALUE")
        ]
        effect_override = self.estimate_override()
        with self.assertRaises(ValueError):
            combined_builder.manual_override_map(
                {"overrides": [effect_override]}, estimate_rows
            )
        ci_override = self.estimate_override("CI_OR_P_VALUE")
        mapped = combined_builder.manual_override_map(
            {"overrides": [effect_override, ci_override]}, estimate_rows
        )
        self.assertEqual(len(mapped), 2)
        ci_override["custom_value"] = dict(ci_override["custom_value"])
        ci_override["custom_value"]["effect_size"] = 1.3
        with self.assertRaises(ValueError):
            combined_builder.manual_override_map(
                {"overrides": [effect_override, ci_override]}, estimate_rows
            )

    def test_custom_application_replaces_stale_values_and_evidence(self):
        target = {
            "final_status": "EXCLUDE",
            "exclusion_code": "OLD",
            "exclusion_reason": "Stale",
            "extraction": {
                "total_n": 50,
                "effect_size": 2.0,
                "effect_type": "OR",
                "ci_lower": 1.5,
                "ci_upper": 2.5,
                "p_value": 0.01,
                "comparison_type": "OLD",
                "needs_inversion": True,
                "cases": 10,
                "ci_method": "old method",
                "evidence": {
                    "sample_size": self.evidence("Old sample evidence."),
                    "effect_size": self.evidence("Old effect evidence."),
                    "p_value": self.evidence("Old p-value evidence."),
                    "design": self.evidence("Preserved design evidence."),
                },
            },
        }
        sample_override = {
            "custom_value": 100,
            "custom_evidence": {
                "sample_size": self.evidence("New sample evidence.")
            },
        }
        combined_builder.apply_custom_value(target, "SAMPLE_SIZE", sample_override)
        self.assertEqual(target["extraction"]["total_n"], 100)
        self.assertEqual(
            target["extraction"]["evidence"]["sample_size"]["excerpt"],
            "New sample evidence.",
        )

        estimate_override = self.estimate_override()
        combined_builder.apply_custom_value(target, "EFFECT_SIZE", estimate_override)
        extraction = target["extraction"]
        self.assertEqual(extraction["effect_size"], 1.2)
        self.assertNotIn("ci_method", extraction)
        self.assertNotIn("p_value", extraction["evidence"])
        self.assertEqual(
            extraction["evidence"]["effect_size"]["excerpt"],
            "Directly supporting text.",
        )
        self.assertIn("design", extraction["evidence"])

        combined_builder.apply_custom_value(
            target,
            "SCREENING_DECISION",
            {
                "custom_value": "INCLUDE_META",
                "custom_reason": "Curator confirmed eligibility.",
            },
        )
        self.assertEqual(target["final_status"], "INCLUDE_META")
        self.assertNotIn("exclusion_code", target)
        self.assertNotIn("exclusion_reason", target)


class CombinedProductionDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (COMBINED_ROOT / "_merge_manifest.json").read_text(encoding="utf-8")
        )
        cls.ledger = json.loads(
            (COMBINED_ROOT / "_adjudication_ledger.json").read_text(encoding="utf-8")
        )["records"]
        cls.pending = json.loads(
            (COMBINED_ROOT / "_pending_review.json").read_text(encoding="utf-8")
        )["records"]
        cls.poolability_audit = json.loads(
            (COMBINED_ROOT / "_poolability_audit.json").read_text(encoding="utf-8")
        )["records"]
        cls.final_records = {row['context_id']: row for row in json.loads((COMBINED_ROOT / 'final_screening.json').read_text())['results']}

    def test_manifest_reconciles_the_complete_selected_lineage(self):
        counts = self.manifest["counts"]
        self.assertEqual(self.manifest["canonical_track"], "combined")
        self.assertEqual(counts["selected_contexts"], 35545)
        from collections import Counter
        self.assertEqual(counts['combined_statuses'], dict(Counter(row['final_status'] for row in self.final_records.values())))
        self.assertEqual(counts['fable_field_rows'], len(self.ledger))
        self.assertEqual(counts['pending_fields'], len(self.pending))
        self.assertEqual(counts['pending_contexts'], len({row['context_id'] for row in self.pending}))
        self.assertEqual(counts['merge_actions'], dict(Counter(row['merge_action'] for row in self.ledger)))
        self.assertEqual(counts['cache_files'], counts['cache_exposures'] * 2)
        self.assertGreaterEqual(counts['displayed_ui_studies'], counts['direct_ui_studies'])

    def test_poolability_audit_reconciles_retained_and_adjudicated_studies(self):
        retained = sum(row['final_status'] != 'EXCLUDE' for row in self.final_records.values())
        self.assertEqual(len(self.poolability_audit), retained)
        self.assertEqual(sum(row['final_status'] == 'INCLUDE_META' for row in self.poolability_audit),
                         self.manifest['counts']['combined_statuses']['INCLUDE_META'])
        for row in self.poolability_audit:
            if row['previous_status'] != row['final_status']:
                self.assertTrue(self.final_records[row['context_id']].get('astra_adjudication'))
        for row in self.final_records.values():
            if row.get('astra_quantitative_follow_up_required'):
                self.assertEqual(row['final_status'], 'ELIGIBLE_NOT_POOLABLE')

        # Missing nonkey case counts can be filled from the other model; the
        # source is recorded, and filled counts are no longer marked estimated.
        selenium = study("selenium", "9634050")
        self.assertEqual(selenium["Sample Size"], 974)
        self.assertEqual(selenium["Cases"], 48)
        self.assertIsNone(selenium["Estimated Cases"])
        fills = json.loads((COMBINED_ROOT / "_nonkey_field_fills.json").read_text())["records"]
        self.assertTrue(any(row["context_id"] == "9634050|Selenium|Prostate cancer"
                            and row["field"] == "extraction.cases" and row["value"] == 48
                            and row["source"] == "CLAUDE_GEMINI" for row in fills))
        oleic = study("oleic_acid", "10491529")
        self.assertTrue(oleic["direction_imputed"])
        self.assertEqual(oleic["comparison_type"], "High versus low")
        self.assertIn("assumes", oleic["direction_imputation_reason"])

    def test_manifest_records_the_exact_committed_source_bytes(self):
        source_digests = self.manifest["source_digests"]
        selected_path = (
            REPO_ROOT
            / "data"
            / "claude_gemini_screening"
            / "all_history_through_2026-08-29"
            / "r1_selected_decisions.json"
        )
        fable_path = (
            REPO_ROOT
            / "outputs"
            / "fable51_discrepancy_adjudication_2026-09-03"
            / "fable51Adjudication.csv"
        )
        self.assertEqual(
            source_digests["selected_chatgpt_sha256"],
            hashlib.sha256(selected_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            source_digests["fable51_adjudication_sha256"],
            hashlib.sha256(fable_path.read_bytes()).hexdigest(),
        )

        generator_digests = self.manifest["generator_digests"]
        self.assertEqual(set(generator_digests), set(combined_builder.GENERATOR_FILES))
        for name, path in combined_builder.GENERATOR_FILES.items():
            with self.subTest(generator=name):
                self.assertEqual(
                    generator_digests[name], hashlib.sha256(path.read_bytes()).hexdigest()
                )

        self.assertEqual(
            self.manifest["build_id"],
            combined_builder.canonical_digest(
                {
                    "schema_version": self.manifest["schema_version"],
                    "fable": source_digests["fable51_adjudication_sha256"],
                    "manual": source_digests["manual_overrides_sha256"],
                    "selected": source_digests["selected_chatgpt_sha256"],
                    "chatgpt_lineage": source_digests["chatgpt_lineage_sha256"],
                    "claude_gemini_lineage": source_digests[
                        "claude_gemini_lineage_sha256"
                    ],
                    "combined_rows": source_digests["combined_rows_sha256"],
                    "projected_studies": source_digests[
                        "projected_studies_sha256"
                    ],
                    "generator_code": generator_digests,
                }
            ),
        )

    def test_every_cache_pair_is_combined_and_scientifically_identical(self):
        directories = sorted(path for path in COMBINED_ROOT.iterdir() if path.is_dir() and any(path.glob("*core.json")))
        self.assertEqual(len(directories), self.manifest["counts"]["cache_exposures"])
        for directory in directories:
            with self.subTest(exposure=directory.name):
                core = cache(directory.name, "core")
                all_cache = cache(directory.name, "all")
                self.assertEqual(core["canonical_track"], "combined")
                self.assertEqual(all_cache["canonical_track"], "combined")
                for field in (
                    "studies", "summary_html", "headline", "screening_stats",
                    "plot_url", "funnel_plot_url", "baujat_plot_url",
                ):
                    self.assertEqual(core[field], all_cache[field])
                self.assertEqual(core["screening_stats"]["canonical_track"], "combined")
                self.assertEqual(core["screening_stats"]["after_prefilter"], 35545)
                self.assertEqual(core["screening_stats"]["llm_screened_in"], sum(row["final_status"] != "EXCLUDE" for row in self.final_records.values()))
                self.assertEqual(core["screening_stats"]["llm_screened_out"], self.manifest["counts"]["combined_statuses"]["EXCLUDE"])

    def test_only_evidence_backed_numeric_winners_change_the_projection(self):
        selenium = study("selenium", "11008904")
        self.assertEqual(selenium["Sample Size"], 498)
        self.assertEqual(selenium["Cases"], 249)

        tea = study("tea", "16804523")
        self.assertEqual(
            [tea["Effect Type"], tea["Effect Size"], tea["Lower CI"], tea["Upper CI"]],
            ["HR", 0.85, 0.5, 1.43],
        )

        # The abstract-validated Astra decision replaces the prior Fable-era
        # provisional calcium bundle; source provenance remains in combined.
        calcium = study("calcium", "18768497")
        self.assertEqual(
            [calcium["Effect Type"], calcium["Effect Size"], calcium["Lower CI"], calcium["Upper CI"], calcium["P Value"], calcium["Cases"]],
            ["RR", 1.31, 0.77, 2.2, 0.341, 85],
        )

        vitamin_a = study("vitamin_a", "19336558")
        self.assertEqual(
            [vitamin_a["Effect Type"], vitamin_a["Effect Size"], vitamin_a["Lower CI"], vitamin_a["Upper CI"], vitamin_a["P Value"]],
            ["OR", 0.8, 0.57, 1.11, 0.11],
        )

    def test_screening_winners_add_poolable_records_and_hierarchy_rows(self):
        self.assertEqual(study("tea", "17906295")["Direct Exposure"], "Tea")
        oats = study("oats", "22527172")
        self.assertEqual(oats["Effect Size"], 0.99)
        self.assertFalse(oats["Hierarchy Inherited"])

        omega6 = study("omega_6_fatty_acids", "38343844")
        self.assertEqual(omega6["Direct Exposure"], "Omega-6 fatty acids")
        inherited = [
            row
            for row in cache("polyunsaturated_fat_pufa")["studies"]
            if row["PMID"] == "38343844"
            and row["Direct Exposure"] == "Omega-6 fatty acids"
        ]
        self.assertEqual(len(inherited), 1)
        self.assertTrue(inherited[0]["Hierarchy Inherited"])
        self.assertFalse(inherited[0]["pool_in_parent_meta_analysis"])

    def test_unresolved_results_stay_provisional_and_are_uniquely_queued(self):
        keys = [(row["context_id"], row["discrepant_field"]) for row in self.pending]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), self.manifest["counts"]["pending_fields"])
        garlic = study("garlic", "9303371")
        self.assertEqual(
            [garlic["Effect Size"], garlic["Lower CI"], garlic["Upper CI"]],
            [0.56, 0.33, 0.93],
        )
        garlic_pending = [
            row for row in self.pending
            if row["context_id"] == "9303371|Garlic|Prostate cancer"
        ]
        self.assertEqual(garlic_pending, [])
        self.assertTrue(self.final_records['9303371|Garlic|Prostate cancer'].get('astra_adjudication'))
        for row in self.pending:
            self.assertFalse(self.final_records[row['context_id']].get('astra_adjudication'))

    def test_application_uses_combined_cache_and_exposes_new_oats_result(self):
        self.assertEqual(Path(web_app.CACHE_DIR), COMBINED_ROOT)
        web_app.app.config["TESTING"] = True
        client = web_app.app.test_client()
        config = client.get("/api/config?track=combined").get_json()
        self.assertEqual(config["canonical_track"], "combined")
        self.assertIn("oats", config["cached_exposures"])
        response = client.post(
            "/analyze",
            json={
                "disease": "Prostate cancer",
                "exposure": "Oats", "evidence_track": "combined",
                "outcome": "Incidence",
                "exclude_meta": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["studies"][0]["PMID"], "22527172")


class AdjudicationArtifactTests(unittest.TestCase):
    def test_comparison_reports_only_requested_quantitative_fields(self):
        rows = csv_rows(COMPARISON_ROOT / "extraction_discrepancies.csv")
        reported = {
            field
            for row in rows
            for field in row["discrepant_fields"].split("; ")
        }
        self.assertEqual(
            reported,
            {"sample_size", "effect_size", "confidence_interval_or_p_value"},
        )

    def test_evidence_less_pipeline_winners_are_sent_to_manual_review(self):
        adjudications = csv_rows(FABLE_ROOT / "fable51Adjudication.csv")
        manual = csv_rows(FABLE_ROOT / "fable51InsufficientOrUnsure.csv")
        self.assertEqual(len(adjudications), 631)
        self.assertEqual(
            sum(row["fable51_manual_review_needed"] == "true" for row in adjudications),
            len(manual),
        )
        unsupported = [
            row
            for row in adjudications
            if row["fable51_verdict"]
            in {"CHATGPT_CORRECT", "CLAUDE_GEMINI_CORRECT"}
            and row["fable51_evidence_source"] == "NONE"
        ]
        self.assertTrue(unsupported)
        self.assertTrue(
            all(row["fable51_manual_review_needed"] == "true" for row in unsupported)
        )

    def test_cost_provenance_distinguishes_incomplete_and_historical_rows(self):
        rows = csv_rows(FABLE_ROOT / "fable51Cost.csv")
        self.assertEqual(len(rows), 306)
        self.assertEqual(len({row["PMID"] for row in rows}), 306)
        self.assertEqual(
            sum(row["cost_scope"] == "HISTORICAL_NONREPORTABLE_REVIEW" for row in rows),
            3,
        )
        self.assertEqual(
            sum(
                row["cost_scope"]
                == "MIXED_CURRENT_AND_HISTORICAL_NONREPORTABLE_REVIEW"
                for row in rows
            ),
            3,
        )
        self.assertFalse(
            any(
                row["cost_status"] != "complete"
                and "all attempts included" in row["cost_provenance"]
                for row in rows
            )
        )


if __name__ == "__main__":
    unittest.main()
