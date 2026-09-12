import json
import tempfile
import unittest
from pathlib import Path

from scripts import reclassification_review as review


class ReclassificationReviewTests(unittest.TestCase):
    def setUp(self):
        self.models = {
            "first": "gpt-5.6-luna",
            "second": "gpt-5.6-sol",
            "full_text": "gpt-5.6-luna",
        }

    def _fixture(self, protocol=7):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        cache = root / "Cached_results" / "fish_oil"
        cache.mkdir(parents=True)
        (root / "data").mkdir()
        manifest = root / "data" / "exposure_revision_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "reclassification_targets": {
                        "Fish oil": [
                            "Eicosapentaenoic acid",
                            "Docosahexaenoic acid",
                            "Omega-3 fatty acids",
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        (cache / "disease_incidence_true_core.json").write_text(
            json.dumps(
                {
                    "exposure": "Fish oil",
                    "canonical_track": "chatgpt",
                    "screening_protocol_version": protocol,
                    "screening_models": self.models,
                    "studies": [
                        {
                            "PMID": "123",
                            "effect_record_id": "effect-1",
                            "meta_analysis_status": "INCLUDE_META",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return temporary, root, manifest

    def test_prepares_included_only_no_search_packet_and_exact_transaction(self):
        temporary, root, manifest = self._fixture()
        self.addCleanup(temporary.cleanup)
        packet = review.prepare_review(
            repo_root=root,
            cache_dir=root / "Cached_results",
            manifest_path=manifest,
            output_path=root / "review.json",
            expected_protocol=7,
            expected_models=self.models,
        )
        self.assertFalse(packet["search_performed"])
        self.assertFalse(packet["model_calls_performed"])
        self.assertEqual(packet["count"], 1)
        record = packet["records"][0]
        self.assertFalse(record["source_removal_allowed"])

        wrong = review.accepted_transaction_plan(
            packet,
            {
                record["review_id"]: {
                    "pmid": "123",
                    "effect_record_id": "different-effect",
                    "accepted_target": "Omega-3 fatty acids",
                }
            },
        )
        self.assertEqual(wrong["source_removal_count"], 0)
        self.assertEqual(len(wrong["pending_adjudication"]), 1)

        accepted = review.accepted_transaction_plan(
            packet,
            {
                record["review_id"]: {
                    "pmid": "123",
                    "effect_record_id": "effect-1",
                    "accepted_target": "Omega-3 fatty acids",
                    "reason": "Omega-3-only wording; no fish-oil wording.",
                }
            },
        )
        self.assertEqual(accepted["source_removal_count"], 1)
        self.assertEqual(
            accepted["actions"][0]["transaction_order"],
            [
                "write_and_validate_target",
                "remove_source",
                "commit_both_or_rollback_both",
            ],
        )

    def test_records_stale_legacy_lineage_without_blocking_fresh_review(self):
        temporary, root, manifest = self._fixture(protocol=6)
        self.addCleanup(temporary.cleanup)
        packet = review.prepare_review(
            repo_root=root,
            cache_dir=root / "Cached_results",
            manifest_path=manifest,
            output_path=root / "review.json",
            expected_protocol=7,
            expected_models=self.models,
        )
        self.assertEqual(packet["count"], 1)
        self.assertEqual(len(packet["legacy_lineage_warnings"]), 1)
        lineage = packet["records"][0]["legacy_source_lineage"]
        self.assertFalse(lineage["is_current"])
        self.assertIn("stale", lineage["warning"])


if __name__ == "__main__":
    unittest.main()
