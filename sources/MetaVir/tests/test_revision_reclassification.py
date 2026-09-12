import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from scripts import dual_screening
from scripts import migrate_k3_candidate_generation as k3_migration
from scripts import monthly_evidence_update as monthly


SOURCE_STUDY = {
    "PMID": "123",
    "Direct Exposure": "Fish oil",
    "Effect Type": "RR",
    "Effect Size": 0.80,
    "Lower CI": 0.70,
    "Upper CI": 0.91,
    "comparison_type": "highest versus lowest erythrocyte EPA quartile",
}

TARGET_ROW = {
    "PMID": "123",
    "Direct Exposure": "Eicosapentaenoic acid",
    "Effect Type": "RR",
    "Effect Size": 0.80,
    "Lower CI": 0.70,
    "Upper CI": 0.91,
    "comparison_type": "highest versus lowest erythrocyte EPA quartile",
}


def write_exact_all_fail_chatgpt_lineage(output: Path, candidates: dict):
    expected = dual_screening.expected_contexts(candidates)
    first = {
        "schema_version": 2,
        "track": "chatgpt",
        "model": dual_screening.CHATGPT_FIRST_MODEL,
        "protocol_version": dual_screening.SCREENING_PROTOCOL_VERSION,
        "input_digest": dual_screening.first_input_digest(
            candidates, dual_screening.CHATGPT_FIRST_MODEL
        ),
        "results": [
            {
                "context_id": cid,
                "decision": "FAIL",
                "reason": "The context is not directly relevant.",
            }
            for cid in expected
        ],
    }
    second = {
        "schema_version": 2,
        "track": "chatgpt",
        "model": dual_screening.CHATGPT_SECOND_MODEL,
        "protocol_version": dual_screening.SCREENING_PROTOCOL_VERSION,
        "source_first_model": first["model"],
        "source_first_input_digest": first["input_digest"],
        "input_digest": dual_screening.second_input_digest(
            candidates, first, dual_screening.CHATGPT_SECOND_MODEL
        ),
        "results": [],
    }
    manifest = {}
    full = {
        "schema_version": 2,
        "track": "chatgpt",
        "model": dual_screening.CHATGPT_FULL_TEXT_MODEL,
        "protocol_version": dual_screening.SCREENING_PROTOCOL_VERSION,
        "source_second_model": second["model"],
        "source_second_input_digest": second["input_digest"],
        "source_manifest": manifest,
        "source_manifest_digest": dual_screening.stable_json_digest(manifest),
        "contexts": 0,
        "manual_full_text_adjudications": 0,
        "manual_full_text_adjudications_digest": None,
        "input_digest": dual_screening.full_text_input_digest(
            second, dual_screening.CHATGPT_FULL_TEXT_MODEL, manifest
        ),
        "results": [],
    }
    lineage = dual_screening.validate_track_lineage(
        candidates,
        first,
        second,
        full,
        "chatgpt",
        {
            "first": dual_screening.CHATGPT_FIRST_MODEL,
            "second": dual_screening.CHATGPT_SECOND_MODEL,
            "full_text": dual_screening.CHATGPT_FULL_TEXT_MODEL,
        },
    )
    final = dual_screening.finalize_track(
        candidates, first, second, full, "chatgpt"
    )
    final = monthly.apply_reclassification_wording_boundaries(candidates, final)
    for item in final["results"]:
        item["direct_exposure"] = item["exposure"]
        item["inherited_exposures"] = monthly.inherited_exposures(item["exposure"])
    final.update(lineage)
    final["canonical_track"] = "chatgpt"
    monthly.save_json(output / "candidates.json", candidates)
    monthly.save_json(output / "chatgpt_first_screen.json", first)
    monthly.save_json(output / "chatgpt_second_screen.json", second)
    monthly.save_json(output / "chatgpt_full_text_review.json", full)
    monthly.save_json(output / "final_screening.json", final)
    return final


class IncludedReclassificationTests(unittest.TestCase):
    def test_revision_aware_reclassification_uses_non_authoritative_lineage(self):
        candidates = {
            "revision_aware": True,
            "run_mode": "included_reclassification",
            "articles": [],
        }
        final = {"canonical_track": "chatgpt", "results": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            with (
                patch.object(
                    monthly,
                    "_validate_current_chatgpt_lineage",
                    side_effect=RuntimeError("ordinary lineage path"),
                ) as ordinary_lineage,
                patch.object(
                    monthly, "require_current_revision_apply"
                ) as authoritative_lineage,
                self.assertRaisesRegex(RuntimeError, "ordinary lineage path"),
            ):
                monthly.apply_to_saved_results(output)

        ordinary_lineage.assert_called_once()
        authoritative_lineage.assert_not_called()

    def test_source_cache_scope_is_the_union_of_divergent_all_and_core(self):
        second = {**SOURCE_STUDY, "PMID": "456", "Effect Size": 0.72}
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            monthly, "CACHE_DIR", Path(temp_dir)
        ), patch.object(
                monthly,
                "reclassification_targets",
                return_value={"Fish oil": ["Eicosapentaenoic acid"]},
        ):
            all_path, core_path = monthly._canonical_cache_paths(
                "Fish oil", "Prostate cancer"
            )
            monthly.save_json(all_path, {"studies": [SOURCE_STUDY]})
            monthly.save_json(core_path, {"studies": [second]})
            records = monthly.included_reclassification_records()

        self.assertEqual({record["pmid"] for record in records}, {"123", "456"})
        self.assertEqual(
            {tuple(record["source_cache_variants"]) for record in records},
            {("all",), ("core",)},
        )

    def test_discovery_efetches_only_previously_included_pmids(self):
        source = {
            "source_record_id": "record-1",
            "source_exposure": "Fish oil",
            "cancer": "Prostate cancer",
            "outcome": "Incidence",
            "pmid": "123",
            "source_study": SOURCE_STUDY,
        }
        fetched = {
            "123": {
                "pmid": "123",
                "pmcid": "PMC123",
                "title": "EPA and prostate cancer",
                "abstract": "An original human incidence study of EPA.",
                "mesh": [],
                "authors": ["Example A"],
                "journal": "Example",
                "journal_publication_date": "2011",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            with (
                patch.object(monthly, "included_reclassification_records", return_value=[source]),
                patch.object(
                    monthly,
                    "reclassification_targets",
                    return_value={
                        "Fish oil": [
                            "Eicosapentaenoic acid",
                            "Omega-3 fatty acids",
                        ]
                    },
                ),
                patch.object(monthly, "configure_entrez", return_value="working") as configure,
                patch.object(monthly, "entrez_fetch", return_value=(fetched, 1)) as fetch,
            ):
                packet = monthly.discover_reclassification(output)

            with (
                patch.object(
                    monthly, "included_reclassification_records", return_value=[source]
                ),
                patch.object(
                    monthly,
                    "reclassification_targets",
                    return_value={
                        "Fish oil": [
                            "Eicosapentaenoic acid",
                            "Omega-3 fatty acids",
                        ]
                    },
                ),
            ):
                monthly._validate_current_reclassification_scope(packet)

        configure.assert_called_once_with(validate_api_key=False)
        fetch.assert_called_once_with(["123"])
        self.assertEqual(packet["run_mode"], "included_reclassification")
        self.assertEqual(packet["ncbi_requests"], {"esearch": 0, "efetch": 1, "total": 1})
        self.assertEqual(packet["included_source_effects"], 1)
        self.assertEqual(
            {item["exposure"] for item in packet["articles"][0]["candidate_contexts"]},
            {"Fish oil", "Eicosapentaenoic acid", "Omega-3 fatty acids"},
        )
        roles = {
            item["exposure"]: item["reclassification_roles"]
            for item in packet["articles"][0]["candidate_contexts"]
        }
        self.assertEqual(roles["Fish oil"], ["source"])
        self.assertEqual(roles["Eicosapentaenoic acid"], ["approved_target"])

    def test_effect_match_requires_numeric_type_and_comparison_agreement(self):
        self.assertTrue(monthly._same_reported_effect(SOURCE_STUDY, TARGET_ROW))
        mismatch = copy.deepcopy(TARGET_ROW)
        mismatch["comparison_type"] = "second versus first DHA tertile"
        self.assertFalse(monthly._same_reported_effect(SOURCE_STUDY, mismatch))

    def test_wording_boundaries_keep_epa_atomic_and_rollup_only(self):
        cancer = "Prostate cancer"
        contexts = [
            {
                "exposure": "Fish oil",
                "cancer": cancer,
                "outcome": "Incidence",
                "reclassification_roles": ["source"],
                "reclassification_sources": ["Fish oil"],
            },
            {
                "exposure": "Eicosapentaenoic acid",
                "cancer": cancer,
                "outcome": "Incidence",
                "reclassification_roles": ["approved_target"],
                "reclassification_sources": ["Fish oil"],
            },
            {
                "exposure": "Omega-3 fatty acids",
                "cancer": cancer,
                "outcome": "Incidence",
                "reclassification_roles": ["approved_target"],
                "reclassification_sources": ["Fish oil"],
            },
        ]
        candidates = {
            "run_mode": "included_reclassification",
            "articles": [
                {
                    "pmid": "123",
                    "title": "Eicosapentaenoic acid and prostate cancer",
                    "abstract": "Circulating EPA was measured prospectively.",
                    "mesh": [],
                    "candidate_contexts": contexts,
                }
            ],
        }
        packet = {
            "results": [
                {
                    "context_id": dual_screening.context_id("123", exposure, cancer),
                    "pmid": "123",
                    "exposure": exposure,
                    "cancer": cancer,
                    "final_status": "INCLUDE_META",
                    "full_text_status": "INCLUDE_META",
                    "reason": "The model included this context.",
                    "extraction": {},
                    "jbi": {},
                }
                for exposure in (
                    "Fish oil",
                    "Eicosapentaenoic acid",
                    "Omega-3 fatty acids",
                )
            ]
        }

        guarded = monthly.apply_reclassification_wording_boundaries(
            candidates, packet
        )
        by_exposure = {item["exposure"]: item for item in guarded["results"]}
        self.assertEqual(by_exposure["Fish oil"]["final_status"], "EXCLUDE")
        self.assertEqual(
            by_exposure["Omega-3 fatty acids"]["final_status"], "EXCLUDE"
        )
        self.assertEqual(
            by_exposure["Eicosapentaenoic acid"]["final_status"], "INCLUDE_META"
        )
        self.assertIn(
            "Omega-3 fatty acids",
            monthly.inherited_exposures("Eicosapentaenoic acid"),
        )

    def test_lycopene_only_cannot_remain_tomato(self):
        candidates = {
            "run_mode": "included_reclassification",
            "articles": [
                {
                    "pmid": "8",
                    "title": "Plasma lycopene and prostate cancer",
                    "abstract": "Lycopene concentrations were assessed.",
                    "mesh": [],
                    "candidate_contexts": [
                        {
                            "exposure": "Tomato",
                            "cancer": "Prostate cancer",
                            "reclassification_roles": ["source"],
                        },
                        {
                            "exposure": "Lycopene",
                            "cancer": "Prostate cancer",
                            "reclassification_roles": ["approved_target"],
                        }
                    ],
                }
            ],
        }
        final = {
            "results": [
                {
                    "context_id": "8|Tomato|Prostate cancer",
                    "pmid": "8",
                    "exposure": "Tomato",
                    "final_status": "INCLUDE_META",
                    "full_text_status": "INCLUDE_META",
                    "reason": "Included by the model.",
                },
                {
                    "context_id": "8|Lycopene|Prostate cancer",
                    "pmid": "8",
                    "exposure": "Lycopene",
                    "final_status": "INCLUDE_META",
                    "full_text_status": "INCLUDE_META",
                    "reason": "Included by the model.",
                },
            ]
        }
        guarded = monthly.apply_reclassification_wording_boundaries(candidates, final)
        self.assertEqual(guarded["results"][0]["final_status"], "EXCLUDE")
        self.assertIn("lycopene-only", guarded["results"][0]["reason"])
        self.assertEqual(guarded["results"][1]["final_status"], "INCLUDE_META")
        mismatch = copy.deepcopy(TARGET_ROW)
        mismatch["Upper CI"] = 1.20
        self.assertFalse(monthly._same_reported_effect(SOURCE_STUDY, mismatch))

    def _packets(self, cache_dir: Path):
        record_id = monthly._source_record_id(
            "Fish oil", "Prostate cancer", SOURCE_STUDY
        )
        candidates = {
            "run_mode": "included_reclassification",
            "reclassification_targets": {
                "Fish oil": ["Eicosapentaenoic acid"]
            },
            "reclassification_source_records": [
                {
                    "source_record_id": record_id,
                    "source_exposure": "Fish oil",
                    "cancer": "Prostate cancer",
                    "pmid": "123",
                    "source_study": SOURCE_STUDY,
                }
            ],
        }
        source_context = dual_screening.context_id(
            "123", "Fish oil", "Prostate cancer"
        )
        target_context = dual_screening.context_id(
            "123", "Eicosapentaenoic acid", "Prostate cancer"
        )
        final = {
            "canonical_track": "chatgpt",
            "protocol_version": dual_screening.SCREENING_PROTOCOL_VERSION,
            "screening_models": {
                "first": dual_screening.CHATGPT_FIRST_MODEL,
                "second": dual_screening.CHATGPT_SECOND_MODEL,
                "full_text": dual_screening.CHATGPT_FULL_TEXT_MODEL,
            },
            "screening_input_digests": {
                "first": "a" * 64,
                "second": "b" * 64,
                "full_text": "c" * 64,
            },
            "results": [
                {
                    "context_id": source_context,
                    "pmid": "123",
                    "exposure": "Fish oil",
                    "cancer": "Prostate cancer",
                    "final_status": "EXCLUDE",
                    "full_text_status": "EXCLUDE",
                    "exclusion_code": "S2-EXPOSURE-MISMATCH",
                    "reason": "The article reports EPA without fish-oil wording.",
                },
                {
                    "context_id": target_context,
                    "pmid": "123",
                    "exposure": "Eicosapentaenoic acid",
                    "cancer": "Prostate cancer",
                    "final_status": "INCLUDE_META",
                    "full_text_status": "INCLUDE_META",
                    "reason": "The requested EPA exposure is directly reported.",
                },
            ],
        }
        cache_path = (
            cache_dir
            / "fish_oil"
            / "prostate_cancer_incidence_true_all.json"
        )
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(json.dumps({"studies": [SOURCE_STUDY]}), encoding="utf-8")
        return candidates, final, cache_path

    def test_source_is_retained_until_matching_target_cache_is_verified(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            candidates, final, _cache_path = self._packets(root / "cache")
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(monthly, "require_current_reclassification_apply"),
                patch.object(monthly, "apply_to_saved_results", return_value={"changes": []}),
                patch.object(monthly, "study_row", return_value=TARGET_ROW),
                patch.object(monthly, "_target_effect_is_persisted", return_value=False),
                patch.object(monthly, "_rebuild_reclassified_source_cache") as rebuild,
            ):
                packet = monthly.apply_reclassification_results(output)

        rebuild.assert_not_called()
        self.assertEqual(packet["moved_source_effects"], 0)
        self.assertEqual(packet["records"][0]["action"], "retained_for_adjudication")
        self.assertIn("not verified", packet["records"][0]["reason"])

    def test_reclassification_packet_persistence_normalizes_numpy_bool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            candidates, final, _cache_path = self._packets(root / "cache")
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(monthly, "require_current_reclassification_apply"),
                patch.object(
                    monthly,
                    "apply_to_saved_results",
                    return_value={"changed": np.bool_(True)},
                ),
                patch.object(monthly, "study_row", return_value=TARGET_ROW),
                patch.object(
                    monthly, "_target_effect_is_persisted", return_value=False
                ),
                patch.object(monthly, "_rebuild_reclassified_source_cache"),
            ):
                monthly.apply_reclassification_results(output)

            persisted = json.loads(
                (output / "reclassification_applied_changes.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertIs(persisted["target_apply"]["changed"], True)

    def test_missing_exact_stage_lineage_fails_before_target_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            candidates, final, _cache_path = self._packets(root / "cache")
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(monthly, "apply_to_saved_results") as target_apply,
                self.assertRaisesRegex(RuntimeError, "complete chatgpt_first_screen"),
            ):
                monthly.apply_reclassification_results(output)

        target_apply.assert_not_called()

    def test_exact_current_lineage_and_scope_are_required_together(self):
        source = {
            "source_record_id": monthly._source_record_id(
                "Fish oil", "Prostate cancer", SOURCE_STUDY
            ),
            "source_exposure": "Fish oil",
            "cancer": "Prostate cancer",
            "outcome": "Incidence",
            "pmid": "123",
            "source_study": SOURCE_STUDY,
            "source_cache_variants": ["all"],
        }
        targets = {"Fish oil": ["Eicosapentaenoic acid"]}
        roles = monthly._reclassification_context_roles([source], targets)["123"]
        contexts = [
            monthly._serialise_reclassification_context(exposure, cancer, detail)
            for (exposure, cancer), detail in sorted(
                roles.items(), key=lambda item: item[0]
            )
        ]
        candidates = {
            "run_mode": "included_reclassification",
            "publication_date_start": None,
            "publication_date_end": monthly.revision_manifest()["revision_date"],
            "selected_exposures": ["Eicosapentaenoic acid", "Fish oil"],
            "catalog": "combined",
            "revision_aware": True,
            "rescreen_saved_contexts": True,
            "source_records_digest": monthly._reclassification_source_digest(
                [source], targets
            ),
            "listed_exposures": 2,
            "cancer_types": ["Prostate cancer"],
            "saved_contexts_omitted": 0,
            "ncbi_requests": {"esearch": 0, "efetch": 1, "total": 1},
            "candidate_pmids": 1,
            "candidate_contexts": 2,
            "included_source_effects": 1,
            "reclassification_targets": targets,
            "reclassification_source_records": [source],
            "articles": [
                {
                    "pmid": "123",
                    "title": "EPA and prostate cancer",
                    "abstract": "Circulating EPA was measured.",
                    "mesh": [],
                    "candidate_contexts": contexts,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            final = write_exact_all_fail_chatgpt_lineage(output, candidates)
            with (
                patch.object(
                    monthly, "included_reclassification_records", return_value=[source]
                ),
                patch.object(
                    monthly, "reclassification_targets", return_value=targets
                ),
            ):
                lineage = monthly.require_current_reclassification_apply(output)
                self.assertEqual(
                    lineage["screening_models"]["second"],
                    dual_screening.CHATGPT_SECOND_MODEL,
                )
                final["results"][0]["reason"] = "A stale final reason."
                monthly.save_json(output / "final_screening.json", final)
                with self.assertRaisesRegex(RuntimeError, "stale or differ"):
                    monthly.require_current_reclassification_apply(output)

    def test_changed_source_scope_fails_before_target_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            candidates, final, _cache_path = self._packets(root / "cache")
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(monthly, "_validate_current_chatgpt_lineage"),
                patch.object(
                    monthly,
                    "included_reclassification_records",
                    return_value=[],
                ),
                patch.object(
                    monthly,
                    "reclassification_targets",
                    return_value={"Fish oil": ["Eicosapentaenoic acid"]},
                ),
                patch.object(monthly, "apply_to_saved_results") as target_apply,
                self.assertRaisesRegex(RuntimeError, "changed after discovery"),
            ):
                monthly.apply_reclassification_results(output)

        target_apply.assert_not_called()

    def test_verified_target_is_written_before_source_removal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            candidates, final, _cache_path = self._packets(root / "cache")
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            applied = {"value": False}

            def target_apply(_output):
                applied["value"] = True
                return {"changes": [{"exposure": "Eicosapentaenoic acid"}]}

            def target_verified(*_args):
                self.assertTrue(applied["value"])
                return True

            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(monthly, "require_current_reclassification_apply"),
                patch.object(monthly, "apply_to_saved_results", side_effect=target_apply),
                patch.object(monthly, "study_row", return_value=TARGET_ROW),
                patch.object(monthly, "_target_effect_is_persisted", side_effect=target_verified),
                patch.object(monthly, "_rebuild_reclassified_source_cache") as rebuild,
            ):
                packet = monthly.apply_reclassification_results(output)

        rebuild.assert_called_once()
        self.assertEqual(packet["moved_source_effects"], 1)
        self.assertEqual(packet["records"][0]["accepted_targets"], ["Eicosapentaenoic acid"])
        self.assertEqual(packet["transaction_rule"], "target_persisted_before_source_removal")

    def test_partial_source_cache_failure_rolls_back_prior_source_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            candidates, final, all_path = self._packets(root / "cache")
            core_path = all_path.with_name(
                "prostate_cancer_incidence_true_core.json"
            )
            core_path.write_text(
                json.dumps({"studies": [SOURCE_STUDY]}), encoding="utf-8"
            )
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)

            def failing_rebuild(path, *_args):
                path.write_text(json.dumps({"studies": []}), encoding="utf-8")
                if path.name.endswith("_core.json"):
                    raise RuntimeError("simulated second-cache failure")

            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(monthly, "require_current_reclassification_apply"),
                patch.object(monthly, "apply_to_saved_results", return_value={"changes": []}),
                patch.object(monthly, "study_row", return_value=TARGET_ROW),
                patch.object(monthly, "_target_effect_is_persisted", return_value=True),
                patch.object(
                    monthly,
                    "_rebuild_reclassified_source_cache",
                    side_effect=failing_rebuild,
                ),
                self.assertRaises(RuntimeError),
            ):
                monthly.apply_reclassification_results(output)

            self.assertEqual(
                json.loads(all_path.read_text(encoding="utf-8"))["studies"],
                [SOURCE_STUDY],
            )
            self.assertEqual(
                json.loads(core_path.read_text(encoding="utf-8"))["studies"],
                [SOURCE_STUDY],
            )


class RevisionAuthoritativeApplyTests(unittest.TestCase):
    cancer = "Prostate cancer"

    @staticmethod
    def _lineage_fields():
        return {
            "canonical_track": "chatgpt",
            "protocol_version": dual_screening.SCREENING_PROTOCOL_VERSION,
            "screening_models": {
                "first": dual_screening.CHATGPT_FIRST_MODEL,
                "second": dual_screening.CHATGPT_SECOND_MODEL,
                "full_text": dual_screening.CHATGPT_FULL_TEXT_MODEL,
            },
            "screening_input_digests": {
                "first": "a" * 64,
                "second": "b" * 64,
                "full_text": "c" * 64,
            },
        }

    @staticmethod
    def _fake_meta(frame, *_args, **_kwargs):
        return {
            "success": True,
            "studies": frame.to_dict("records"),
            "summary_html": "summary",
            "headline": "headline",
            "screening_stats": {"extracted": len(frame)},
            "plot_url": None,
            "funnel_plot_url": None,
            "baujat_plot_url": None,
        }

    def _write_revision_packets(self, output: Path, final_results):
        candidates = {
            "revision_aware": True,
            "publication_date_start": None,
            "publication_date_end": "2026-08-29",
            "articles": [],
            "redundant_parent_contexts": [],
            "vocabulary_digest": "d" * 64,
        }
        final = {
            **self._lineage_fields(),
            "publication_date_start": None,
            "publication_date_end": "2026-08-29",
            "results": final_results,
        }
        monthly.save_json(output / "candidates.json", candidates)
        monthly.save_json(output / "final_screening.json", final)
        return candidates, final

    def test_revision_search_scope_is_current_and_fail_closed(self):
        terms = monthly.selected_exposure_terms(
            ["Vitamin B3"], catalog="current", revision_aware=True
        )
        expected_query = monthly.build_query(
            terms["Vitamin B3"], monthly.CANCER_QUERIES[self.cancer]
        )
        candidates = {
            "revision_aware": True,
            "publication_date_start": None,
            "publication_date_end": "2026-08-29",
            "rescreen_saved_contexts": True,
            "saved_contexts_omitted": 0,
            "discovery_complete": True,
            "selected_exposures": ["Vitamin B3"],
            "catalog": "current",
            "searched_exposures": ["Vitamin B3"],
            "searched_contexts": [
                {
                    "exposure": "Vitamin B3",
                    "cancer": self.cancer,
                    "outcome": "Incidence",
                }
            ],
            "vocabulary_digest": monthly.exposure_vocabulary_digest(terms),
            "listed_exposures": 1,
            "ncbi_requests": {"esearch": 1, "efetch": 0, "total": 1},
            "search_completion": [
                {
                    "exposure": "Vitamin B3",
                    "cancer": self.cancer,
                    "query_sha256": hashlib.sha256(
                        expected_query.encode("utf-8")
                    ).hexdigest(),
                    "complete": True,
                    "reported_count": 0,
                    "returned_unique_pmids": 0,
                    "request_count": 1,
                    "leaf_intervals": [
                        {
                            "publication_date_start": "0001-01-01",
                            "publication_date_end": "2026-08-29",
                            "reported_count": 0,
                            "returned_unique_pmids": 0,
                            "complete": True,
                        }
                    ],
                }
            ],
            "candidate_pmids": 0,
            "candidate_contexts": 0,
            "articles": [],
        }
        self.assertEqual(
            monthly._validate_current_revision_search_scope(candidates),
            {("Vitamin B3", self.cancer)},
        )
        candidates["discovery_complete"] = False
        with self.assertRaisesRegex(RuntimeError, "not marked complete"):
            monthly._validate_current_revision_search_scope(candidates)

    def test_zero_inclusion_clears_cache_and_records_reason(self):
        old = {
            "PMID": "91",
            "Direct Exposure": "Vitamin B3",
            "Effect ID": "old-effect",
        }
        excluded = {
            "context_id": "91|Vitamin B3|Prostate cancer",
            "pmid": "91",
            "track": "chatgpt",
            "exposure": "Vitamin B3",
            "cancer": self.cancer,
            "full_text_status": "EXCLUDE",
            "final_status": "EXCLUDE",
            "exclusion_code": "S2-EXPOSURE-MISMATCH",
            "reason": "The article did not evaluate vitamin B3 exposure.",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            cache_root = root / "cache"
            self._write_revision_packets(output, [excluded])
            with patch.object(monthly, "CACHE_DIR", cache_root):
                all_path, _core_path = monthly._canonical_cache_paths(
                    "Vitamin B3", self.cancer
                )
                monthly.save_json(all_path, {"studies": [old]})
                with patch.object(
                    monthly,
                    "require_current_revision_apply",
                    return_value={("Vitamin B3", self.cancer)},
                ):
                    packet = monthly.apply_to_saved_results(output)
                saved = json.loads(all_path.read_text(encoding="utf-8"))
            ledger = json.loads(
                (output / "revision_removed_records.json").read_text(encoding="utf-8")
            )

        self.assertEqual(saved["studies"], [])
        self.assertEqual(packet["mode"], "all_history_authoritative")
        self.assertEqual(ledger["records"][0]["pmid"], "91")
        self.assertEqual(
            ledger["records"][0]["status"], "EXCLUDE"
        )
        self.assertEqual(
            ledger["records"][0]["reason"], excluded["reason"]
        )

    def test_authoritative_multi_cache_write_rolls_back_on_failure(self):
        old = {
            "PMID": "92",
            "Direct Exposure": "Vitamin B3",
            "Effect ID": "old-effect",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            cache_root = root / "cache"
            self._write_revision_packets(output, [])
            with patch.object(monthly, "CACHE_DIR", cache_root):
                all_path, core_path = monthly._canonical_cache_paths(
                    "Vitamin B3", self.cancer
                )
                monthly.save_json(all_path, {"studies": [old], "variant": "all"})
                monthly.save_json(core_path, {"studies": [old], "variant": "core"})
                before_all = all_path.read_bytes()
                before_core = core_path.read_bytes()
                real_save = monthly.save_json
                calls = {"count": 0}

                def fail_second(path, data, indent=2, ensure_ascii=False):
                    calls["count"] += 1
                    if calls["count"] == 2:
                        raise RuntimeError("simulated second write failure")
                    return real_save(path, data, indent=indent, ensure_ascii=ensure_ascii)

                with (
                    patch.object(
                        monthly,
                        "require_current_revision_apply",
                        return_value={("Vitamin B3", self.cancer)},
                    ),
                    patch.object(monthly, "save_json", side_effect=fail_second),
                    self.assertRaisesRegex(RuntimeError, "simulated second write"),
                ):
                    monthly.apply_to_saved_results(output)

                self.assertEqual(all_path.read_bytes(), before_all)
                self.assertEqual(core_path.read_bytes(), before_core)
            self.assertFalse((output / "applied_changes.json").exists())

    def test_k_subtype_refresh_preserves_general_vitamin_k_direct_rows(self):
        direct_general = {
            "PMID": "10",
            "Direct Exposure": "Vitamin K",
            "Effect ID": "general-direct",
            "Effect Type": "RR",
            "Effect Size": 0.9,
            "Lower CI": 0.8,
            "Upper CI": 1.0,
        }
        stale_k1 = {
            "PMID": "11",
            "Direct Exposure": "Vitamin K1",
            "Displayed Exposure": "Vitamin K",
            "Effect ID": "stale-k1",
        }
        included = {
            "context_id": "12|Vitamin K1|Prostate cancer",
            "pmid": "12",
            "track": "chatgpt",
            "exposure": "Vitamin K1",
            "direct_exposure": "Vitamin K1",
            "inherited_exposures": ["Vitamin K"],
            "cancer": self.cancer,
            "full_text_status": "INCLUDE_META",
            "final_status": "INCLUDE_META",
            "reason": "The study reports phylloquinone incidence evidence.",
            "extraction": {"exposure_subtype": "dietary_component"},
        }

        def row(item):
            return {
                "PMID": item["pmid"],
                "Direct Exposure": item.get("direct_exposure") or item["exposure"],
                "Displayed Exposure": item["exposure"],
                "Hierarchy Inherited": bool(item.get("hierarchy_inherited")),
                "pool_in_parent_meta_analysis": not bool(
                    item.get("hierarchy_inherited")
                ),
                "Effect ID": "new-k1",
                "Effect Type": "RR",
                "Effect Size": 0.8,
                "Lower CI": 0.7,
                "Upper CI": 0.9,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            cache_root = root / "cache"
            self._write_revision_packets(output, [included])
            with patch.object(monthly, "CACHE_DIR", cache_root):
                parent_all, parent_core = monthly._canonical_cache_paths(
                    "Vitamin K", self.cancer
                )
                monthly.save_json(parent_all, {"studies": [direct_general, stale_k1]})
                monthly.save_json(parent_core, {"studies": [direct_general, stale_k1]})
                with (
                    patch.object(
                        monthly,
                        "require_current_revision_apply",
                        return_value={("Vitamin K1", self.cancer)},
                    ),
                    patch.object(monthly, "study_row", side_effect=row),
                    patch.object(
                        monthly.meta_analysis,
                        "perform_meta_analysis",
                        side_effect=self._fake_meta,
                    ),
                ):
                    monthly.apply_to_saved_results(output)
                saved_parent = json.loads(parent_all.read_text(encoding="utf-8"))

        by_effect = {study["Effect ID"]: study for study in saved_parent["studies"]}
        self.assertIn("general-direct", by_effect)
        self.assertIn("new-k1", by_effect)
        self.assertNotIn("stale-k1", by_effect)
        self.assertEqual(by_effect["general-direct"]["Direct Exposure"], "Vitamin K")

    def test_same_pmid_parent_direct_is_replaced_by_screened_child(self):
        direct_general = {
            "PMID": "12",
            "Direct Exposure": "Vitamin K",
            "Effect ID": "general-direct",
            "Effect Type": "RR",
            "Effect Size": 0.9,
            "Lower CI": 0.8,
            "Upper CI": 1.0,
        }
        included = {
            "context_id": "12|Vitamin K1|Prostate cancer",
            "pmid": "12",
            "track": "chatgpt",
            "exposure": "Vitamin K1",
            "direct_exposure": "Vitamin K1",
            "inherited_exposures": ["Vitamin K"],
            "cancer": self.cancer,
            "full_text_status": "INCLUDE_META",
            "final_status": "INCLUDE_META",
            "reason": "The study reports phylloquinone incidence evidence.",
            "extraction": {"exposure_subtype": "dietary_component"},
        }

        def row(item):
            inherited = bool(item.get("hierarchy_inherited"))
            return {
                "PMID": item["pmid"],
                "Direct Exposure": item.get("direct_exposure") or item["exposure"],
                "Displayed Exposure": item["exposure"],
                "Hierarchy Inherited": inherited,
                "pool_in_parent_meta_analysis": not inherited,
                "Effect ID": "new-k1",
                "Effect Type": "RR",
                "Effect Size": 0.8,
                "Lower CI": 0.7,
                "Upper CI": 0.9,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run"
            output.mkdir()
            cache_root = root / "cache"
            self._write_revision_packets(output, [included])
            with patch.object(monthly, "CACHE_DIR", cache_root):
                parent_all, parent_core = monthly._canonical_cache_paths(
                    "Vitamin K", self.cancer
                )
                monthly.save_json(parent_all, {"studies": [direct_general]})
                monthly.save_json(parent_core, {"studies": [direct_general]})
                with (
                    patch.object(
                        monthly,
                        "require_current_revision_apply",
                        return_value={("Vitamin K1", self.cancer)},
                    ),
                    patch.object(monthly, "study_row", side_effect=row),
                    patch.object(
                        monthly.meta_analysis,
                        "perform_meta_analysis",
                        side_effect=self._fake_meta,
                    ),
                ):
                    packet = monthly.apply_to_saved_results(output)
                saved_parent = json.loads(parent_all.read_text(encoding="utf-8"))
            ledger = json.loads(
                (output / "revision_removed_records.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(
            [study["Effect ID"] for study in saved_parent["studies"]],
            ["new-k1"],
        )
        self.assertIsNone(saved_parent["headline"])
        self.assertEqual(packet["mode"], "all_history_authoritative")
        parent_record = next(
            record for record in ledger["records"]
            if record.get("effect_id") == "general-direct"
        )
        self.assertEqual(
            parent_record["status"], "PARENT_DIRECT_REPLACED_BY_CHILD"
        )
        self.assertEqual(
            parent_record["replacement_direct_exposures"], ["Vitamin K1"]
        )


class VitaminB3HierarchyTests(unittest.TestCase):
    def test_related_precursors_do_not_roll_into_vitamin_b3(self):
        self.assertEqual(monthly.inherited_exposures("Nicotinamide"), ["Vitamin B3"])
        self.assertEqual(monthly.inherited_exposures("Nicotinic acid"), ["Vitamin B3"])
        for exposure in (
            "Nicotinamide riboside",
            "NAD+",
            "NADH",
            "Nicotinamide mononucleotide",
            "Dihydronicotinamide mononucleotide (NMNH)",
        ):
            self.assertNotIn("Vitamin B3", monthly.inherited_exposures(exposure))

    def test_non_inheriting_note_and_hierarchy_checksum_are_auditable(self):
        manifest = monthly.revision_manifest()
        relationship = next(
            item
            for item in manifest["non_inheriting_relationships"]
            if item["exposure"] == "Nicotinamide riboside"
        )
        self.assertFalse(relationship["evidence_rollup"])
        digest = hashlib.sha256(monthly.EXPOSURE_HIERARCHY_FILE.read_bytes()).hexdigest()
        self.assertEqual(manifest["exposure_hierarchy_sha256"], digest)


class VitaminK3AliasParsingTests(unittest.TestCase):
    def test_comma_bearing_chemical_alias_remains_atomic(self):
        raw = monthly.load_json(monthly.SYNONYMS_FILE, {})
        self.assertIsInstance(raw["Vitamin K3"]["core"], list)

        terms = monthly.exposure_terms(revision_aware=True)["Vitamin K3"]
        self.assertIn("2-methyl-1,4-naphthoquinone", terms)
        self.assertNotIn("2-methyl-1", terms)
        self.assertNotIn("4-naphthoquinone", terms)
        self.assertEqual(
            monthly.meta_analysis.get_canonical_name(
                "2-methyl-1,4-naphthoquinone"
            ),
            "Vitamin K3",
        )

    def test_all_reviewed_comma_bearing_chemical_aliases_are_atomic(self):
        expected = {
            "Vitamin K3": "2-methyl-1,4-naphthoquinone",
            "Dihydronicotinamide mononucleotide (NMNH)":
                "1,4-dihydronicotinamide mononucleotide",
            "DIM": "3,3′-diindolylmethane",
            "Theobromine": "3,7-dimethylxanthine",
        }
        terms = monthly.exposure_terms(revision_aware=True)
        self.assertEqual(
            {
                exposure: term
                for exposure, values in terms.items()
                for term in values
                if "," in term
            },
            expected,
        )
        synonyms = monthly.load_json(monthly.SYNONYMS_FILE, {})
        additional = monthly.additional_exposure_catalog()
        for exposure, term in expected.items():
            entry = synonyms.get(exposure, additional.get(exposure))
            self.assertIsInstance(entry["search_terms"], list)
            self.assertIn(term, entry["search_terms"])
            self.assertIn(term, terms[exposure])
            left, right = term.split(",", 1)
            self.assertNotIn(left, terms[exposure])
            self.assertNotIn(right, terms[exposure])

    def test_v2_additional_catalog_is_in_revision_vocabulary(self):
        v2 = monthly.load_json(monthly.ADDITIONAL_EXPOSURES_V2_FILE, {})
        additional = monthly.additional_exposure_catalog()
        self.assertEqual(len(v2), 29)
        self.assertEqual(len(additional), 215)
        self.assertTrue(set(v2).issubset(additional))
        self.assertIn('DASH eating plan', monthly.exposure_terms(
            revision_aware=True
        )['DASH diet'])

    def test_scalar_atomic_comma_alias_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "list-valued search_terms"):
            monthly._split_catalog_scalar(
                "1,4-dihydronicotinamide mononucleotide",
                "Dihydronicotinamide mononucleotide (NMNH)",
                "core",
            )

    def test_query_diff_is_exactly_the_authoritative_k3_job_set(self):
        current, legacy = k3_migration._current_and_legacy_terms()
        changed = k3_migration._changed_jobs(current, legacy)
        self.assertEqual(
            [(item["exposure"], item["cancer"]) for item in changed],
            [
                ("Vitamin K3", cancer)
                for cancer in monthly.CANCER_QUERIES
            ],
        )
        self.assertEqual(len(changed), 1)

    def test_context_digest_ignores_only_sibling_transport_contexts(self):
        article = {
            "pmid": "123",
            "title": "A stable title",
            "abstract": "A stable abstract.",
            "mesh": ["Humans"],
            "published_date": "2020-01-01",
            "candidate_contexts": [
                {
                    "exposure": "Vitamin B3",
                    "cancer": "Prostate cancer",
                    "outcome": "Incidence",
                },
                {
                    "exposure": "Vitamin K3",
                    "cancer": "Prostate cancer",
                    "outcome": "Incidence",
                },
            ],
        }
        before = {"articles": [article]}
        after = copy.deepcopy(before)
        after["articles"][0]["candidate_contexts"] = [
            article["candidate_contexts"][0]
        ]
        cid = "123|Vitamin B3|Prostate cancer"
        before_digest = dual_screening.first_context_input_digests(
            before, dual_screening.CHATGPT_FIRST_MODEL
        )[cid]
        after_digest = dual_screening.first_context_input_digests(
            after, dual_screening.CHATGPT_FIRST_MODEL
        )[cid]
        self.assertEqual(before_digest, after_digest)

        after["articles"][0]["abstract"] = "Changed abstract."
        changed_digest = dual_screening.first_context_input_digests(
            after, dual_screening.CHATGPT_FIRST_MODEL
        )[cid]
        self.assertNotEqual(before_digest, changed_digest)

    def test_prepared_generation_sidecar_blocks_screening(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            monthly.save_json(
                output / monthly.CANDIDATE_GENERATION_STATE_NAME,
                {"schema_version": 1, "status": "prepared"},
            )
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                monthly.require_candidate_generation_ready(output)


class RevisionCliTests(unittest.TestCase):
    def test_revision_aware_requires_all_history(self):
        argv = [
            "monthly_evidence_update.py",
            "--revision-aware",
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-08-29",
        ]
        with patch("sys.argv", argv), self.assertRaises(SystemExit):
            monthly.parse_args()

    def test_reclassification_mode_sets_a_dedicated_default_output(self):
        argv = [
            "monthly_evidence_update.py",
            "--revision-reclassification",
            "--stage",
            "discover",
        ]
        with patch("sys.argv", argv):
            args = monthly.parse_args()
        self.assertTrue(args.revision_reclassification)
        self.assertIsNone(args.start_date)
        self.assertEqual(args.end_date, "2026-08-29")
        self.assertEqual(
            args.output_dir.name,
            "included_reclassification_2026-08-29",
        )


if __name__ == "__main__":
    unittest.main()
