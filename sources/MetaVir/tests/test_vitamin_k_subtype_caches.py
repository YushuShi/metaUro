import json
import math
import unittest
from pathlib import Path
from statistics import NormalDist

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = REPO_ROOT / "Cached_results"


def load_cache(slug: str, track: str = "core") -> dict:
    path = CACHE_ROOT / slug / f"prostate_cancer_incidence_true_{track}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class VitaminKSubtypeCacheTests(unittest.TestCase):
    def test_parent_is_preserved_while_ci_only_children_are_recovered(self):
        parent = load_cache("vitamin_k")
        vitamin_k1 = load_cache("vitamin_k1")
        vitamin_k2 = load_cache("vitamin_k2")

        self.assertEqual(parent["exposure"], "Vitamin K")
        direct_parent = [row for row in parent["studies"]
                         if not row["Hierarchy Inherited"]]
        self.assertEqual(len(direct_parent), 1)
        self.assertEqual(direct_parent[0]["Effect Size"], 0.65)
        self.assertEqual(parent["headline"]["pooled_es"], 0.65)
        inherited = [row for row in parent["studies"]
                     if row["Hierarchy Inherited"]]
        self.assertEqual({row["Direct Exposure"] for row in inherited},
                         {"Vitamin K1", "Vitamin K2"})
        self.assertTrue(all(not row["pool_in_parent_meta_analysis"]
                            for row in inherited))

        for exposure, payload in {
            "Vitamin K1": vitamin_k1,
            "Vitamin K2": vitamin_k2,
        }.items():
            with self.subTest(exposure=exposure):
                self.assertEqual(payload["exposure"], exposure)
                self.assertEqual(payload["cancer"], "Prostate cancer")
                self.assertEqual(payload["outcome"], "Incidence")
                self.assertEqual(len(payload["studies"]), 1)
                row = payload["studies"][0]
                self.assertEqual(row["PMID"], "18400723")
                self.assertEqual(row["Direct Exposure"], exposure)
                self.assertFalse(row["Hierarchy Inherited"])
                self.assertTrue(row["pool_in_parent_meta_analysis"])
                self.assertEqual(row["Sample Size"], 11319)
                # The saved poolability projection now computes the two-sided
                # Wald p value; the source packet's missing p remains unchanged.
                self.assertIsNone(row['extraction_evidence'].get('p_value'))
                self.assertIn('Computed from the effect and 95% CI',
                              row['extraction_supporting_text']['p_value'])
                se = (math.log(row['Upper CI']) - math.log(row['Lower CI'])) / (2 * NormalDist().inv_cdf(.975))
                expected_p = math.erfc(abs(math.log(row['Effect Size']) / se) / math.sqrt(2))
                self.assertAlmostEqual(row['P Value'], expected_p, places=12)
                self.assertFalse(row["direction_imputed"])
                self.assertTrue(row["extraction_evidence"]["confidence_interval"]["excerpt"])
                expected = (1.02, 0.7, 1.48) if exposure == "Vitamin K1" else (0.65, 0.39, 1.06)
                self.assertEqual(tuple(row[field] for field in
                                       ("Effect Size", "Lower CI", "Upper CI")), expected)
                self.assertEqual(payload["headline"]["pooled_es"], expected[0])
                self.assertEqual(payload["poolability_projection"]["new_model_calls"], 0)
                # Keep the original strict-policy removal as historical audit,
                # even though its usable CI now allows recovery without a p value.
                update = payload["revision_updates"][-1]
                self.assertEqual(update["mode"], "all_history_authoritative")
                self.assertEqual(update["studies_after"], 0)
                self.assertEqual(len(update["removed_records"]), 1)
                removed = update["removed_records"][0]
                self.assertEqual(removed["pmid"], "18400723")
                self.assertEqual(removed["status"], "ELIGIBLE_NOT_POOLABLE")
                self.assertTrue(removed["reason"])

    def test_core_and_all_tracks_are_paired_and_summaries_recompute(self):
        for slug, exposure in (("vitamin_k1", "Vitamin K1"), ("vitamin_k2", "Vitamin K2")):
            with self.subTest(exposure=exposure):
                core = load_cache(slug, "core")
                all_studies = load_cache(slug, "all")
                for field in (
                    "studies",
                    "summary_html",
                    "headline",
                    "screening_stats",
                    "plot_url",
                    "funnel_plot_url",
                    "baujat_plot_url",
                    "screening_models",
                    "screening_input_digests",
                    "screening_protocol_version",
                ):
                    self.assertEqual(core[field], all_studies[field])
                self.assertEqual(len(core["studies"]), 1)
                self.assertIn("Pooled Result (Single Study)", core["summary_html"])
                row = core["studies"][0]
                self.assertEqual(core["headline"]["pooled_es"], row["Effect Size"])
                self.assertEqual(core["headline"]["ci_low"], row["Lower CI"])
                self.assertEqual(core["headline"]["ci_upp"], row["Upper CI"])

    def test_recovered_children_have_separate_chatgpt_plots_and_are_not_assigned_to_k3(self):
        for slug in ("vitamin_k1", "vitamin_k2"):
            payload = load_cache(slug)
            for key in ("plot_url", "funnel_plot_url"):
                plot_url = payload[key]
                self.assertIn(f"static/chatgpt/{slug}/", plot_url)
                path = REPO_ROOT / plot_url.split("?", 1)[0].lstrip("/")
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
            self.assertIsNone(payload["baujat_plot_url"])

        for path in (CACHE_ROOT / "vitamin_k3").glob(
            "prostate_cancer_incidence_true_*.json"
        ):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("18400723", {study["PMID"] for study in payload["studies"]})

    def test_recovered_children_keep_original_model_decisions_unchanged(self):
        source = json.loads((
            REPO_ROOT / "data/monthly_updates/all_history_through_2026-08-29"
            / "chatgpt_final_screening.json"
        ).read_text(encoding="utf-8"))
        rows = {row["context_id"]: row for row in source["results"]}
        for exposure in ("Vitamin K1", "Vitamin K2"):
            row = rows[f"18400723|{exposure}|Prostate cancer"]
            self.assertEqual(row["final_status"], "ELIGIBLE_NOT_POOLABLE")
            self.assertIsNone(row["extraction"]["p_value"])
            self.assertIsNotNone(row["extraction"]["ci_lower"])
            self.assertIsNotNone(row["extraction"]["ci_upper"])


if __name__ == "__main__":
    unittest.main()
