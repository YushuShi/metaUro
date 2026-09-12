import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from scripts import dual_screening
from scripts import monthly_evidence_update as monthly


SOURCE = {
    "PMID": "123",
    "Direct Exposure": "Fish oil",
    "Effect Type": "RR",
    "Effect Size": 0.8,
    "Lower CI": 0.7,
    "Upper CI": 0.91,
    "comparison_type": "highest versus lowest erythrocyte EPA quartile",
}
TARGET = {
    **SOURCE,
    "Direct Exposure": "Eicosapentaenoic acid",
}


class VitaminK3AliasParsingTests(unittest.TestCase):
    def test_comma_bearing_chemical_alias_remains_atomic(self):
        raw = monthly.load_json(monthly.SYNONYMS_FILE, {})
        self.assertIsInstance(raw["Vitamin K3"]["core"], list)

        terms = monthly.exposure_terms("all")["Vitamin K3"]
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
        all_terms = monthly.exposure_terms("all")
        selected = monthly.revision_scope_names() | {
            name.casefold() for name in monthly.additional_exposure_entries()
        }
        terms = {
            exposure: values
            for exposure, values in all_terms.items()
            if exposure.casefold() in selected
        }
        self.assertEqual(len(terms), 229)
        self.assertIn("DASH diet", terms)
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
        additional = monthly.additional_exposure_entries()
        for exposure, term in expected.items():
            entry = synonyms.get(exposure, additional.get(exposure))
            self.assertIsInstance(entry["search_terms"], list)
            self.assertIn(term, entry["search_terms"])
            self.assertIn(term, terms[exposure])
            left, right = term.split(",", 1)
            self.assertNotIn(left, terms[exposure])
            self.assertNotIn(right, terms[exposure])

    def test_scalar_atomic_comma_alias_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "list-valued search_terms"):
            monthly._split_catalog_scalar(
                "N,N-dimethylglycine",
                "Example",
                "core",
            )

    def test_superseded_generation_marker_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            candidate_path = output / "candidates.json"
            monthly.save_json(candidate_path, {"articles": []})
            monthly.save_json(
                output / monthly.SUPERSEDED_GENERATION_NAME,
                {
                    "schema_version": 1,
                    "status": "superseded",
                    "candidate_sha256": monthly.hashlib.sha256(
                        candidate_path.read_bytes()
                    ).hexdigest(),
                    "reason": "fragmented test vocabulary",
                },
            )
            with self.assertRaisesRegex(RuntimeError, "superseded and unusable"):
                monthly.require_output_not_superseded(output)


class IncludedReclassificationTests(unittest.TestCase):
    def test_same_effect_refresh_replaces_legacy_all_participants_duplicate(self):
        legacy_all = {
            "PMID": "19328261",
            "Screening Record ID": (
                "19328261|Tomato|Renal cell carcinoma#"
                "gender=all participants;measurement=dietary_intake"
            ),
            "Effect Type": "OR",
            "Effect Size": 0.50,
            "Lower CI": 0.31,
            "Upper CI": 0.81,
            "comparison_type": "Tomato consumption",
        }
        distinct_male = {
            **legacy_all,
            "Screening Record ID": (
                "19328261|Tomato|Renal cell carcinoma#"
                "gender=male;measurement=dietary_intake"
            ),
            "Effect Size": 0.47,
            "Lower CI": 0.24,
            "Upper CI": 0.95,
        }
        current = {
            **legacy_all,
            "Screening Record ID": (
                "19328261|Tomato|Renal cell carcinoma#"
                "gender=mixed;measurement=dietary_intake"
            ),
            "direct_exposure": "Tomato",
            "comparison_type": "increased tomato consumption",
        }
        studies = [legacy_all, distinct_male, current]

        action, superseded = monthly._merge_screened_cache_row(
            studies,
            current,
            "Tomato",
            included_reclassification=True,
        )

        self.assertEqual(action, "refreshed")
        self.assertEqual(
            superseded,
            [legacy_all["Screening Record ID"]],
        )
        self.assertEqual(len(studies), 2)
        self.assertEqual(
            {row["Effect Size"] for row in studies},
            {0.50, 0.47},
        )

    def test_revision_aware_reclassification_dispatch_excludes_authoritative_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            monthly.save_json(
                output / "candidates.json",
                {
                    "revision_aware": True,
                    "run_mode": "included_reclassification",
                },
            )
            with (
                patch.object(monthly, "require_output_not_superseded"),
                patch.object(
                    monthly,
                    "apply_reclassification_results",
                    return_value={"mode": "reclassification"},
                ) as reclassification,
                patch.object(
                    monthly, "apply_revision_authoritative_results"
                ) as authoritative,
                patch.object(monthly, "apply_to_saved_results") as ordinary,
            ):
                packet = monthly.apply_screened_results(output)

        self.assertEqual(packet, {"mode": "reclassification"})
        reclassification.assert_called_once_with(output)
        authoritative.assert_not_called()
        ordinary.assert_not_called()

    def test_discovery_efetches_included_pmids_without_esearch_validation(self):
        included = {
            "source_record_id": "record-1",
            "source_exposure": "Fish oil",
            "cancer": "Prostate cancer",
            "outcome": "Incidence",
            "pmid": "123",
            "source_study": SOURCE,
        }
        fetched = {
            "123": {
                "pmid": "123",
                "pmcid": "PMC123",
                "title": "EPA study",
                "abstract": "An original human incidence study.",
                "journal_publication_date": "2011",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    monthly,
                    "included_reclassification_records",
                    return_value=[included],
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
                patch.object(
                    monthly, "configure_entrez", return_value="working"
                ) as configure,
                patch.object(
                    monthly, "entrez_fetch", return_value=(fetched, 1)
                ) as fetch,
            ):
                packet = monthly.discover_reclassification(Path(directory))

        configure.assert_called_once_with(validate_api_key=False)
        fetch.assert_called_once_with(["123"])
        self.assertEqual(
            packet["ncbi_requests"],
            {"esearch": 0, "efetch": 1, "total": 1},
        )
        self.assertEqual(packet["run_mode"], "included_reclassification")
        self.assertEqual(
            {
                context["exposure"]
                for context in packet["articles"][0]["candidate_contexts"]
            },
            {"Fish oil", "Eicosapentaenoic acid", "Omega-3 fatty acids"},
        )

    def test_effect_match_is_strict(self):
        self.assertTrue(monthly._same_reported_effect(SOURCE, TARGET))
        wrong = {**TARGET, "Upper CI": 1.2}
        self.assertFalse(monthly._same_reported_effect(SOURCE, wrong))
        wrong = {**TARGET, "comparison_type": "second versus first DHA tertile"}
        self.assertFalse(monthly._same_reported_effect(SOURCE, wrong))

    def _packets(self, cache_root):
        source_id = monthly._source_record_id(
            "Fish oil", "Prostate cancer", SOURCE
        )
        candidates = {
            "run_mode": "included_reclassification",
            "reclassification_targets": {
                "Fish oil": ["Eicosapentaenoic acid"]
            },
            "reclassification_source_records": [
                {
                    "source_record_id": source_id,
                    "source_exposure": "Fish oil",
                    "cancer": "Prostate cancer",
                    "pmid": "123",
                    "source_study": SOURCE,
                }
            ],
        }
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
                    "context_id": monthly.context_id(
                        "123", "Fish oil", "Prostate cancer"
                    ),
                    "final_status": "EXCLUDE",
                    "full_text_status": "EXCLUDE",
                    "reason": "EPA only.",
                },
                {
                    "context_id": monthly.context_id(
                        "123", "Eicosapentaenoic acid", "Prostate cancer"
                    ),
                    "pmid": "123",
                    "exposure": "Eicosapentaenoic acid",
                    "cancer": "Prostate cancer",
                    "final_status": "INCLUDE_META",
                    "full_text_status": "INCLUDE_META",
                },
            ],
        }
        cache = (
            cache_root
            / "fish_oil"
            / "prostate_cancer_incidence_true_all.json"
        )
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"studies": [SOURCE]}), encoding="utf-8")
        return candidates, final

    def test_source_retained_until_target_cache_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "run"
            output.mkdir()
            candidates, final = self._packets(root / "cache")
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(
                    monthly, "apply_to_saved_results", return_value={"changes": []}
                ),
                patch.object(monthly, "study_row", return_value=TARGET),
                patch.object(
                    monthly, "_target_effect_is_persisted", return_value=False
                ),
                patch.object(
                    monthly, "_rebuild_reclassified_source_cache"
                ) as rebuild,
            ):
                packet = monthly.apply_reclassification_results(output)

        rebuild.assert_not_called()
        self.assertEqual(packet["moved_source_effects"], 0)
        self.assertEqual(
            packet["records"][0]["action"], "retained_for_adjudication"
        )

    def test_reclassification_packet_persistence_normalizes_numpy_bool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "run"
            output.mkdir()
            candidates, final = self._packets(root / "cache")
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(
                    monthly,
                    "apply_to_saved_results",
                    return_value={"changed": np.bool_(True)},
                ),
                patch.object(monthly, "study_row", return_value=TARGET),
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

    def test_target_is_applied_before_source_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "run"
            output.mkdir()
            candidates, final = self._packets(root / "cache")
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            target_written = {"value": False}

            def apply_target(_output):
                target_written["value"] = True
                return {"changes": []}

            def verify(*_args):
                self.assertTrue(target_written["value"])
                return True

            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(
                    monthly, "apply_to_saved_results", side_effect=apply_target
                ),
                patch.object(monthly, "study_row", return_value=TARGET),
                patch.object(
                    monthly, "_target_effect_is_persisted", side_effect=verify
                ),
                patch.object(
                    monthly, "_rebuild_reclassified_source_cache"
                ) as rebuild,
            ):
                packet = monthly.apply_reclassification_results(output)

        rebuild.assert_called_once()
        self.assertEqual(packet["moved_source_effects"], 1)
        self.assertEqual(
            packet["transaction_rule"],
            "target_persisted_before_source_removal",
        )


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

    def test_reclassification_has_dedicated_output(self):
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
        self.assertEqual(
            args.output_dir.name,
            "included_reclassification_2026-08-29",
        )


class DiscoveryCheckpointTests(unittest.TestCase):
    START = None
    END = "2026-08-29"
    TERMS = {
        "Vitamin B3": ["vitamin B3", "niacin"],
        "PQQ": ["PQQ", "methoxatin"],
    }
    CANCERS = {
        "Bladder cancer": '"bladder cancer"[Title/Abstract]',
        "Kidney cancer": '"kidney cancer"[Title/Abstract]',
    }

    def _scope(self):
        jobs = monthly.build_search_jobs(self.TERMS, self.CANCERS)
        binding, specs = monthly.discovery_scope_binding(
            start_date=self.START,
            end_date=self.END,
            exposure_scope="current",
            revision_aware=False,
            rollup_vitamin_k=False,
            selected_exposures=list(self.TERMS),
            catalog_digest=monthly.exposure_catalog_digest(self.TERMS),
            search_jobs=jobs,
            cancer_queries=self.CANCERS,
        )
        return binding, specs

    def _audit(self, pmids):
        return {
            "esearch_protocol_version": monthly.ESEARCH_PROTOCOL_VERSION,
            "requested_start_date": self.START,
            "effective_start_date": "0001-01-01",
            "requested_end_date": self.END,
            "reported_count": len(pmids),
            "retrieved_count": len(pmids),
            "unique_pmid_count": len(pmids),
            "esearch_requests": 1,
            "recursive_split_required": False,
            "recursive_windows": [],
        }

    def _result(self, spec, pmid):
        return {
            **{
                key: spec[key]
                for key in (
                    "job_id", "exposure", "cancer", "outcome", "query_sha256"
                )
            },
            "pmids": [str(pmid)],
            "request_count": 1,
            "audit": self._audit([str(pmid)]),
        }

    def test_interrupted_discovery_resumes_only_missing_exact_jobs(self):
        binding, specs = self._scope()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            checkpoint_path = output / "discovery_query_checkpoint.sqlite3"
            connection, completed = monthly.open_discovery_checkpoint(
                checkpoint_path,
                binding,
                specs,
                start_date=self.START,
                end_date=self.END,
            )
            self.assertEqual(completed, {})
            monthly.save_discovery_result(
                connection,
                self._result(specs[0], "100"),
            )
            connection.close()  # Simulate interruption after one committed query.

            queried = []

            def search(query, start_date, end_date, *, return_audit=False):
                self.assertTrue(return_audit)
                self.assertIsNone(start_date)
                self.assertEqual(end_date, self.END)
                queried.append(query)
                pmid = str(200 + len(queried))
                return [pmid], 1, self._audit([pmid])

            fetched = {
                str(pmid): {
                    "pmid": str(pmid),
                    "journal_publication_date": "2026-01-01",
                    "electronic_publication_date": "2026-01-01",
                    "title": "Example",
                    "abstract": "Example abstract",
                    "mesh": [],
                }
                for pmid in (100, 201, 202, 203)
            }
            with (
                patch.object(monthly, "CANCER_QUERIES", self.CANCERS),
                patch.object(monthly, "exposure_terms", return_value=self.TERMS),
                patch.object(
                    monthly,
                    "exposure_catalog_sources",
                    return_value={name: "current" for name in self.TERMS},
                ),
                patch.object(monthly, "configure_entrez", return_value="working"),
                patch.object(monthly, "saved_contexts", return_value=set()),
                patch.object(monthly, "entrez_search", side_effect=search),
                patch.object(monthly, "entrez_fetch", return_value=(fetched, 1)),
            ):
                packet = monthly.discover(
                    self.START,
                    self.END,
                    output,
                )

            self.assertEqual(len(queried), len(specs) - 1)
            self.assertNotIn(specs[0]["query"], queried)
            self.assertEqual(packet["ncbi_requests"]["esearch_jobs"], len(specs))
            self.assertEqual(
                packet["discovery_query_checkpoint"]["completed_jobs"],
                len(specs),
            )
            self.assertEqual(packet["candidate_pmids"], len(specs))

    def test_checkpoint_rejects_cutoff_vocabulary_or_outcome_scope_change(self):
        binding, specs = self._scope()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.sqlite3"
            connection, _ = monthly.open_discovery_checkpoint(
                path,
                binding,
                specs,
                start_date=self.START,
                end_date=self.END,
            )
            connection.close()
            changed = json.loads(json.dumps(binding))
            changed["publication_date_end"] = "2026-08-30"
            with self.assertRaisesRegex(RuntimeError, "scope is stale or tampered"):
                monthly.open_discovery_checkpoint(
                    path,
                    changed,
                    specs,
                    start_date=self.START,
                    end_date="2026-08-30",
                )

    def test_checkpoint_rejects_result_payload_tampering(self):
        binding, specs = self._scope()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.sqlite3"
            connection, _ = monthly.open_discovery_checkpoint(
                path,
                binding,
                specs,
                start_date=self.START,
                end_date=self.END,
            )
            monthly.save_discovery_result(
                connection,
                self._result(specs[0], "100"),
            )
            connection.execute(
                "UPDATE results SET payload_json = replace(payload_json, '100', '999')"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                monthly.open_discovery_checkpoint(
                    path,
                    binding,
                    specs,
                    start_date=self.START,
                    end_date=self.END,
                )


class AuthoritativeRevisionApplyTests(unittest.TestCase):
    def _packets(self):
        cancer = next(iter(monthly.CANCER_QUERIES))
        cid = monthly.context_id("123", "Fish oil", cancer)
        candidates = {
            "revision_aware": True,
            "selected_exposures": ["Fish oil"],
            "cancer_types": [cancer],
            "redundant_parent_context_omissions": [],
            "articles": [
                {
                    "pmid": "123",
                    "title": "Prior included record",
                    "abstract": "Abstract",
                    "candidate_contexts": [
                        {
                            "exposure": "Fish oil",
                            "cancer": cancer,
                            "outcome": "Incidence",
                        }
                    ],
                }
            ],
        }
        final = {
            "canonical_track": "chatgpt",
            "protocol_version":
                dual_screening.SCREENING_PROTOCOL_VERSION,
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
                    "context_id": cid,
                    "pmid": "123",
                    "exposure": "Fish oil",
                    "cancer": cancer,
                    "outcome": "Incidence",
                    "final_status": "EXCLUDE",
                    "full_text_status": "EXCLUDE",
                    "reason": "No explicit fish-oil wording.",
                }
            ],
        }
        return cancer, candidates, final

    def test_zero_inclusion_scope_is_cleared_with_removal_reason(self):
        cancer, candidates, final = self._packets()
        prior = {
            "PMID": "123",
            "direct_exposure": "Fish oil",
            "Effect Type": "RR",
            "Effect Size": 1.1,
            "Lower CI": 0.9,
            "Upper CI": 1.3,
            "comparison_type": "highest versus lowest",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "run"
            output.mkdir()
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            safe_cancer = monthly.re.sub(
                r"[^a-zA-Z0-9]+", "_", cancer.lower()
            ).strip("_")
            cache = (
                root
                / "cache"
                / "fish_oil"
                / f"{safe_cancer}_incidence_true_all.json"
            )
            monthly.save_json(
                cache,
                {
                    "exposure": "Fish oil",
                    "cancer": cancer,
                    "studies": [prior],
                },
            )
            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(
                    monthly,
                    "apply_to_saved_results",
                    return_value={"changes": []},
                ),
            ):
                packet = monthly.apply_revision_authoritative_results(
                    output
                )
            cleared = monthly.load_json(cache, {})

        self.assertEqual(cleared["studies"], [])
        self.assertEqual(
            packet["run_mode"], "authoritative_revision_rebuild"
        )
        self.assertEqual(
            packet["prior_row_dispositions"][0]["reason"],
            "No explicit fish-oil wording.",
        )

    def test_missing_old_row_disposition_fails_before_mutation(self):
        cancer, candidates, final = self._packets()
        final["results"] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "run"
            output.mkdir()
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                self.assertRaisesRegex(
                    RuntimeError, "one final disposition"
                ),
            ):
                monthly.apply_revision_authoritative_results(output)

    def test_unsearched_parent_preserves_direct_rows_while_child_rollup_is_replaced(self):
        cancer, candidates, final = self._packets()
        candidates["selected_exposures"] = ["Vitamin K1"]
        candidates["articles"][0]["candidate_contexts"][0]["exposure"] = "Vitamin K1"
        cid = monthly.context_id("123", "Vitamin K1", cancer)
        final["results"] = [
            {
                "context_id": cid,
                "pmid": "123",
                "exposure": "Vitamin K1",
                "cancer": cancer,
                "outcome": "Incidence",
                "final_status": "EXCLUDE",
                "full_text_status": "EXCLUDE",
                "reason": "Fresh screening excluded the child context.",
            }
        ]
        direct_parent = {"PMID": "999", "direct_exposure": "Vitamin K"}
        inherited_child = {"PMID": "123", "direct_exposure": "Vitamin K1"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "run"
            output.mkdir()
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            safe_cancer = monthly.re.sub(
                r"[^a-zA-Z0-9]+", "_", cancer.lower()
            ).strip("_")
            parent_cache = (
                root / "cache" / "vitamin_k"
                / f"{safe_cancer}_incidence_true_all.json"
            )
            monthly.save_json(
                parent_cache,
                {
                    "exposure": "Vitamin K",
                    "cancer": cancer,
                    "studies": [direct_parent, inherited_child],
                },
            )

            def preserve(path, exposure, scoped_cancer, retained, removed, _final):
                monthly.save_json(
                    path,
                    {
                        "exposure": exposure,
                        "cancer": scoped_cancer,
                        "studies": retained,
                        "removed": removed,
                    },
                )

            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(
                    monthly, "apply_to_saved_results", return_value={"changes": []}
                ),
                patch.object(
                    monthly,
                    "exposure_hierarchy",
                    return_value={"Vitamin K1": ["Vitamin K"]},
                ),
                patch.object(
                    monthly,
                    "_replace_unsearched_parent_rows",
                    side_effect=preserve,
                ),
            ):
                packet = monthly.apply_revision_authoritative_results(output)
            rebuilt = monthly.load_json(parent_cache, {})
        self.assertEqual(rebuilt["studies"], [direct_parent])
        self.assertEqual(
            packet["unsearched_parent_inherited_row_replacements"][0][
                "direct_exposure"
            ],
            "Vitamin K1",
        )

    def test_legacy_screening_record_id_recovers_direct_child(self):
        self.assertEqual(
            monthly._direct_exposure(
                {
                    "Screening Record ID": (
                        "123|Docosahexaenoic acid|Renal cell carcinoma"
                        "#gender=mixed;measurement=dietary_intake"
                    )
                },
                "Omega-3 fatty acids",
            ),
            "Docosahexaenoic acid",
        )

    def test_accepted_child_replaces_same_pmid_unsearched_parent_direct_row(self):
        cancer, candidates, final = self._packets()
        candidates["selected_exposures"] = ["Vitamin K1"]
        candidates["articles"][0]["candidate_contexts"][0][
            "exposure"
        ] = "Vitamin K1"
        cid = monthly.context_id("123", "Vitamin K1", cancer)
        final["results"] = [
            {
                "context_id": cid,
                "pmid": "123",
                "exposure": "Vitamin K1",
                "direct_exposure": "Vitamin K1",
                "inherited_exposure_memberships": ["Vitamin K"],
                "cancer": cancer,
                "outcome": "Incidence",
                "final_status": "INCLUDE_META",
                "full_text_status": "INCLUDE_META",
                "reason": "Fresh screening accepted the child context.",
            }
        ]
        legacy_parent = {
            "PMID": "123",
            "Screening Record ID": (
                f"123|Vitamin K|{cancer}#gender=na;measurement=dietary_intake"
            ),
        }
        legacy_child = {
            "PMID": "123",
            "Screening Record ID": (
                f"123|Vitamin K1|{cancer}#gender=na;measurement=dietary_intake"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "run"
            output.mkdir()
            monthly.save_json(output / "candidates.json", candidates)
            monthly.save_json(output / "final_screening.json", final)
            safe_cancer = monthly.re.sub(
                r"[^a-zA-Z0-9]+", "_", cancer.lower()
            ).strip("_")
            parent_cache = (
                root / "cache" / "vitamin_k"
                / f"{safe_cancer}_incidence_true_all.json"
            )
            monthly.save_json(
                parent_cache,
                {
                    "exposure": "Vitamin K",
                    "cancer": cancer,
                    "studies": [legacy_parent, legacy_child],
                },
            )

            def preserve(path, exposure, scoped_cancer, retained, removed, _final):
                monthly.save_json(
                    path,
                    {
                        "exposure": exposure,
                        "cancer": scoped_cancer,
                        "studies": retained,
                        "removed": removed,
                    },
                )

            with (
                patch.object(monthly, "CACHE_DIR", root / "cache"),
                patch.object(
                    monthly, "apply_to_saved_results", return_value={"changes": []}
                ),
                patch.object(
                    monthly,
                    "exposure_hierarchy",
                    return_value={"Vitamin K1": ["Vitamin K"]},
                ),
                patch.object(
                    monthly,
                    "_replace_unsearched_parent_rows",
                    side_effect=preserve,
                ),
            ):
                packet = monthly.apply_revision_authoritative_results(output)
            rebuilt = monthly.load_json(parent_cache, {})

        self.assertEqual(rebuilt["studies"], [])
        reasons = {
            item["reason"]
            for item in packet["unsearched_parent_inherited_row_replacements"]
        }
        self.assertTrue(
            any("redundant parent-direct row" in reason for reason in reasons)
        )
        self.assertTrue(
            any("inherited row" in reason for reason in reasons)
        )


if __name__ == "__main__":
    unittest.main()
