import copy
import unittest

from scripts import build_combined_results as builder
from scripts import monthly_evidence_update as monthly
from scripts import rebuild_poolability_results as rebuild


class PoolabilityProjectionTests(unittest.TestCase):
    def row(self, **updates):
        row = {
            "context_id": "123|Tea|Prostate cancer", "pmid": "123", "exposure": "Tea",
            "cancer": "Prostate cancer", "authors": ["van Dijk AB"],
            "final_status": "ELIGIBLE_NOT_POOLABLE", "jbi": {"grade": "Good"},
            "extraction": {"effect_size": 1.2, "effect_type": "RR", "ci_lower": 1.1,
                           "ci_upper": 1.4, "total_n": 1000, "cases": None,
                           "p_value": None, "comparison_type": None,
                           "exposure_classification_ambiguous": True},
        }
        row.update(updates)
        return row

    def test_projection_promotes_without_altering_source(self):
        source = self.row()
        before = copy.deepcopy(source)
        rows, audit = rebuild.project({source["context_id"]: source}, "CHATGPT", monthly)
        projected = rows[source["context_id"]]
        self.assertEqual(source, before)
        self.assertEqual(projected["final_status"], "INCLUDE_META")
        self.assertTrue(projected["extraction"]["direction_imputed"])
        self.assertEqual(audit[0]["first_author_last_name"], "van Dijk")
        study = monthly.study_row(projected)
        self.assertIsNone(study["Cases"])
        self.assertEqual(study["Estimated Cases"], 132)
        self.assertTrue(study["direction_imputed"])
        self.assertIn("assumes", study["extraction_supporting_text"]["effect_direction"])

    def test_both_tracks_use_identical_policy(self):
        source = self.row()
        chat, _ = rebuild.project({"x": source}, "CHATGPT", monthly)
        claude = self.row(eligibility="INCLUDE", needs_full_text=True)
        claude.pop("final_status")
        other, _ = rebuild.project({"x": claude}, "CLAUDE_GEMINI", monthly)
        self.assertEqual(chat["x"]["final_status"], other["x"]["final_status"])
        self.assertEqual(chat["x"]["extraction"], other["x"]["extraction"])

    def test_exclusions_and_manual_nonpoolable_decision_preserved(self):
        for status in ("EXCLUDE", "ELIGIBLE_NOT_POOLABLE"):
            source = self.row(final_status=status,
                combined_adjudicated_fields={"SCREENING_DECISION": "CUSTOM"})
            rows = {"x": source}
            builder.reclassify_poolability(rows, monthly)
            self.assertEqual(rows["x"]["final_status"], status)

    def test_total_n_still_required_after_fable_sample_change(self):
        source = self.row(final_status="INCLUDE_META")
        source["extraction"]["total_n"] = None
        builder.reclassify_poolability({"x": source}, monthly)
        self.assertEqual(source["final_status"], "ELIGIBLE_NOT_POOLABLE")
        self.assertIn("total_n", source["poolability_policy"]["remaining_issues"])

    def test_known_wrong_whole_food_subtype_remains_separate(self):
        source = self.row(exposure="Tomato")
        source["extraction"]["exposure_subtype"] = "human_biomarker"
        builder.reclassify_poolability({"x": source}, monthly)
        self.assertEqual(source["final_status"], "ELIGIBLE_SUBTYPE_REVIEW")

    def test_dietary_classification_does_not_lift_source_measurement_guard(self):
        source = self.row(exposure="Tomato")
        source["extraction"].update(exposure_subtype="whole_food", exposure_measurement_type="human_biospecimen")
        builder.reclassify_poolability({"x": source}, monthly)
        self.assertEqual(source["final_status"], "ELIGIBLE_SUBTYPE_REVIEW")
        self.assertEqual(source["extraction"]["exposure_measurement_type"], "dietary_intake")
        self.assertEqual(source["extraction"]["exposure_measurement_type_original"], "human_biospecimen")
        builder.reclassify_poolability({"x": source}, monthly)
        self.assertEqual(source["final_status"], "ELIGIBLE_SUBTYPE_REVIEW")


if __name__ == "__main__":
    unittest.main()
