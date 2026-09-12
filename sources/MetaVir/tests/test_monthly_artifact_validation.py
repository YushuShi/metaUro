import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import dual_screening as screening
from scripts import validate_monthly_artifacts as validator


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


class MonthlyArtifactValidationTests(unittest.TestCase):
    def build_zero_candidate_run(self, root, start="2026-07-01", end="2026-07-31"):
        updates_root = Path(root) / "updates"
        reports_root = Path(root) / "reports"
        run_name = (
            f"all_history_through_{end}"
            if start is None
            else f"{start}_{end}"
        )
        update_dir = updates_root / run_name
        report_dir = reports_root / run_name
        candidates = {
            "schema_version": 1,
            "publication_date_start": start,
            "publication_date_end": end,
            "candidate_pmids": 0,
            "candidate_contexts": 0,
            "articles": [],
        }
        write_json(update_dir / "candidates.json", candidates)
        write_json(update_dir / "out_of_window_records.json", {"count": 0, "records": []})
        write_json(
            update_dir / "validation_status.json",
            screening.validation_status_packet(candidates),
        )

        packets = {}
        for track, models in validator.EXPECTED_MODELS.items():
            usage = lambda model: {
                "model": model,
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
            }
            first = {
                "schema_version": 2,
                "created_at": "2026-08-01T00:00:00Z",
                "track": track,
                "model": models["first"],
                "protocol_version": validator.EXPECTED_PROTOCOL_VERSION,
                "input_digest": screening.first_input_digest(
                    candidates, models["first"]
                ),
                "source_candidates": 0,
                "source_contexts": 0,
                "pass": 0,
                "fail": 0,
                "usage": usage(models["first"]),
                "results": [],
            }
            second = {
                "schema_version": 2,
                "created_at": "2026-08-01T00:00:00Z",
                "track": track,
                "model": models["second"],
                "protocol_version": validator.EXPECTED_PROTOCOL_VERSION,
                "input_digest": screening.second_input_digest(
                    candidates, first, models["second"]
                ),
                "source_first_model": first["model"],
                "source_first_input_digest": first["input_digest"],
                "source_first_screen_pass": 0,
                "include": 0,
                "exclude": 0,
                "needs_full_text": 0,
                "pmcid_full_text_candidates": 0,
                "usage": usage(models["second"]),
                "results": [],
            }
            source_manifest = screening.full_text_source_manifest({}, set())
            full_text = {
                "schema_version": 2,
                "created_at": "2026-08-01T00:00:00Z",
                "track": track,
                "model": models["full_text"],
                "protocol_version": validator.EXPECTED_PROTOCOL_VERSION,
                "input_digest": screening.full_text_input_digest(
                    second, models["full_text"], source_manifest
                ),
                "source_second_model": second["model"],
                "source_second_input_digest": second["input_digest"],
                "source_manifest": source_manifest,
                "source_manifest_digest": screening.stable_json_digest(
                    source_manifest
                ),
                "contexts": 0,
                "status_counts": {},
                "manual_full_text_adjudications": 0,
                "manual_full_text_adjudications_digest": None,
                "usage": usage(models["full_text"]),
                "results": [],
            }
            final = {
                "schema_version": 2,
                "created_at": "2026-08-01T00:00:00Z",
                "track": track,
                "publication_date_start": start,
                "publication_date_end": end,
                "protocol_version": validator.EXPECTED_PROTOCOL_VERSION,
                "screening_models": dict(models),
                "screening_input_digests": {
                    "first": first["input_digest"],
                    "second": second["input_digest"],
                    "full_text": full_text["input_digest"],
                },
                "counts": {},
                "results": [],
            }
            packets[track] = (first, second, full_text, final)
            for stage, packet in zip(
                ("first_screen", "second_screen", "full_text_review", "final_screening"),
                packets[track],
            ):
                write_json(update_dir / f"{track}_{stage}.json", packet)

        chatgpt_first, chatgpt_second, chatgpt_full, chatgpt_final = packets["chatgpt"]
        write_json(update_dir / "first_screen.json", chatgpt_first)
        write_json(update_dir / "second_screen.json", chatgpt_second)
        write_json(update_dir / "pmc_source_status.json", {
            "schema_version": 1,
            "created_at": "2026-08-01T00:00:00Z",
            "ncbi_requests": 0,
            "sources": {},
        })
        write_json(update_dir / "full_text_review.json", {
            **chatgpt_full,
            "ncbi_requests": 0,
            "reused_tracks": [],
        })
        write_json(update_dir / "final_screening.json", {
            **chatgpt_final,
            "canonical_track": "chatgpt",
            "validation_status": screening.DEEPSEEK_VALIDATION_STATUS,
        })
        write_json(update_dir / "exclusions.json", {
            "schema_version": 2,
            "canonical_track": "chatgpt",
            "count": 0,
            "records": [],
        })
        write_json(update_dir / "discrepancies.json", {
            "schema_version": 1,
            "canonical_track": "chatgpt",
            "validation_track": "deepseek",
            "validation_status": screening.DEEPSEEK_VALIDATION_STATUS,
            "validation_reason": screening.DEEPSEEK_VALIDATION_REASON,
            "compared_contexts": 0,
            "pending_contexts": 0,
            "discrepancy_count": 0,
            "records": [],
        })
        write_json(update_dir / "exposure_subtype_review.json", {"records": []})
        write_json(update_dir / "applied_changes.json", {"changes": []})
        report_dir.mkdir(parents=True, exist_ok=True)
        for filename in validator.REQUIRED_REPORTS:
            (report_dir / filename).write_text("validated report\n", encoding="utf-8")
        return updates_root, reports_root, update_dir

    def test_previous_month_window_handles_year_boundary(self):
        self.assertEqual(
            validator.previous_month_window(date(2026, 1, 15)),
            ("2025-12-01", "2025-12-31"),
        )

    def test_validator_uses_exact_run_not_lexicographic_latest(self):
        with tempfile.TemporaryDirectory() as temporary:
            updates, reports, _ = self.build_zero_candidate_run(temporary)
            newer = updates / "9999-12-01_9999-12-31"
            newer.mkdir(parents=True)
            (newer / "candidates.json").write_text("{broken", encoding="utf-8")
            summary = validator.validate_run(
                updates, reports, "2026-07-01", "2026-07-31"
            )

        self.assertEqual(summary["run"], "2026-07-01_2026-07-31")
        self.assertEqual(summary["candidate_contexts"], 0)

    def test_validator_accepts_explicit_all_history_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            updates, reports, update_dir = self.build_zero_candidate_run(
                temporary, start=None, end="2026-06-30"
            )
            summary = validator.validate_run(
                updates, reports, None, "2026-06-30"
            )
            candidates = json.loads(
                (update_dir / "candidates.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["run"], "all_history_through_2026-06-30")
        self.assertIsNone(candidates["publication_date_start"])
        self.assertEqual(summary["candidate_contexts"], 0)

    def test_validator_rejects_missing_historical_start_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            updates, reports, update_dir = self.build_zero_candidate_run(
                temporary, start=None, end="2026-06-30"
            )
            path = update_dir / "candidates.json"
            candidates = json.loads(path.read_text(encoding="utf-8"))
            candidates.pop("publication_date_start")
            write_json(path, candidates)

            with self.assertRaisesRegex(
                validator.ArtifactValidationError,
                "explicitly record their publication-date start",
            ):
                validator.validate_run(updates, reports, None, "2026-06-30")

    def test_parse_args_supports_all_history_and_preserves_bounded_mode(self):
        with patch(
            "sys.argv",
            ["validate_monthly_artifacts.py", "--all-history-through", "2026-06-30"],
        ):
            historical = validator.parse_args()
        self.assertIsNone(historical.start_date)
        self.assertEqual(historical.end_date, "2026-06-30")

        with patch(
            "sys.argv",
            [
                "validate_monthly_artifacts.py",
                "--start-date",
                "2026-07-01",
                "--end-date",
                "2026-07-31",
            ],
        ):
            bounded = validator.parse_args()
        self.assertEqual(bounded.start_date, "2026-07-01")
        self.assertEqual(bounded.end_date, "2026-07-31")

    def test_validator_rejects_wrong_model_protocol_and_digest(self):
        mutations = (
            ("model", "wrong-model", "model must be"),
            ("protocol_version", 3, "protocol_version must be"),
            ("input_digest", "0" * 64, "input digest is stale"),
        )
        for key, value, message in mutations:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                updates, reports, update_dir = self.build_zero_candidate_run(temporary)
                path = update_dir / "chatgpt_first_screen.json"
                packet = json.loads(path.read_text(encoding="utf-8"))
                packet[key] = value
                write_json(path, packet)
                with self.assertRaisesRegex(validator.ArtifactValidationError, message):
                    validator.validate_run(updates, reports, "2026-07-01", "2026-07-31")

    def test_validator_rejects_broken_full_text_source_lineage(self):
        with tempfile.TemporaryDirectory() as temporary:
            updates, reports, update_dir = self.build_zero_candidate_run(temporary)
            path = update_dir / "chatgpt_full_text_review.json"
            packet = json.loads(path.read_text(encoding="utf-8"))
            packet["source_second_input_digest"] = "0" * 64
            write_json(path, packet)
            with self.assertRaisesRegex(
                validator.ArtifactValidationError, "second-screen digest"
            ):
                validator.validate_run(updates, reports, "2026-07-01", "2026-07-31")

    def test_full_text_validation_uses_the_exact_pmcid_gate(self):
        second_rows = {
            "1|Exposure|Prostate cancer": {
                "context_id": "1|Exposure|Prostate cancer",
                "eligibility": "INCLUDE",
                "pmcid": "PMC1",
                "needs_full_text": True,
                "extraction": {},
                "jbi": {"grade": "Moderate"},
            },
            "2|Exposure|Prostate cancer": {
                "context_id": "2|Exposure|Prostate cancer",
                "eligibility": "INCLUDE",
                "pmcid": "",
                "needs_full_text": True,
                "extraction": {},
                "jbi": {"grade": "Moderate"},
            },
        }
        packet = {
            "schema_version": 2,
            "track": "chatgpt",
            "model": "gpt-5.6-luna",
            "protocol_version": validator.EXPECTED_PROTOCOL_VERSION,
            "input_digest": "0" * 64,
            "contexts": 1,
            "status_counts": {"ELIGIBLE_NOT_POOLABLE": 1},
            "usage": {
                "model": "gpt-5.6-luna",
                "calls": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "reasoning_tokens": 1,
            },
            "results": [
                {
                    "context_id": "1|Exposure|Prostate cancer",
                    "full_text_status": "ELIGIBLE_NOT_POOLABLE",
                }
            ],
        }

        rows = validator._validate_full_text(packet, second_rows, "chatgpt")

        self.assertEqual(set(rows), {"1|Exposure|Prostate cancer"})
        with self.assertRaisesRegex(
            validator.ArtifactValidationError, "unexpected"
        ):
            validator._validate_full_text(
                {
                    **packet,
                    "contexts": 2,
                    "status_counts": {"ELIGIBLE_NOT_POOLABLE": 2},
                    "results": [
                        *packet["results"],
                        {
                            "context_id": "2|Exposure|Prostate cancer",
                            "full_text_status": "ELIGIBLE_NOT_POOLABLE",
                        },
                    ],
                },
                second_rows,
                "chatgpt",
            )

    def test_provider_final_hierarchy_enrichment_matches_canonical_contract(self):
        provider = {
            "schema_version": 2,
            "results": [
                {
                    "context_id": "1|Linoleic acid|Prostate cancer",
                    "exposure": "Linoleic acid",
                }
            ],
        }
        enriched = validator._expected_canonical_final(provider)

        row = enriched["results"][0]
        self.assertEqual(row["direct_exposure"], "Linoleic acid")
        self.assertEqual(
            row["inherited_exposures"],
            ["Omega-6 fatty acids", "Polyunsaturated fat (PUFA)"],
        )
        validator._validate_canonical_provider(enriched, provider)
        mutations = (
            ("direct_exposure", "Omega-6 fatty acids"),
            ("inherited_exposures", []),
            (
                "inherited_exposures",
                ["Polyunsaturated fat (PUFA)", "Omega-6 fatty acids"],
            ),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                stale = deepcopy(enriched)
                stale["results"][0][field] = value
                with self.assertRaisesRegex(
                    validator.ArtifactValidationError,
                    "differs from the ChatGPT final packet",
                ):
                    validator._validate_canonical_provider(stale, provider)

    def test_validator_rejects_broken_final_lineage(self):
        with tempfile.TemporaryDirectory() as temporary:
            updates, reports, update_dir = self.build_zero_candidate_run(temporary)
            path = update_dir / "chatgpt_final_screening.json"
            packet = json.loads(path.read_text(encoding="utf-8"))
            packet["screening_models"]["second"] = "substituted-model"
            write_json(path, packet)
            with self.assertRaisesRegex(
                validator.ArtifactValidationError, "screening_models"
            ):
                validator.validate_run(updates, reports, "2026-07-01", "2026-07-31")

    def test_validator_accepts_full_reuse_with_prior_pmc_request_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            updates, reports, update_dir = self.build_zero_candidate_run(temporary)
            alias_path = update_dir / "full_text_review.json"
            alias = json.loads(alias_path.read_text(encoding="utf-8"))
            alias["reused_tracks"] = ["chatgpt"]
            write_json(alias_path, alias)
            status_path = update_dir / "pmc_source_status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["ncbi_requests"] = 7
            write_json(status_path, status)

            summary = validator.validate_run(
                updates, reports, "2026-07-01", "2026-07-31"
            )

        self.assertEqual(summary["candidate_contexts"], 0)

    def test_validator_rejects_invalid_reused_track_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            updates, reports, update_dir = self.build_zero_candidate_run(temporary)
            path = update_dir / "full_text_review.json"
            alias = json.loads(path.read_text(encoding="utf-8"))
            alias["reused_tracks"] = ["unknown"]
            write_json(path, alias)
            with self.assertRaisesRegex(
                validator.ArtifactValidationError, "reused_tracks"
            ):
                validator.validate_run(updates, reports, "2026-07-01", "2026-07-31")

    def test_validator_rejects_stale_result_counts_and_noncanonical_final(self):
        with tempfile.TemporaryDirectory() as temporary:
            updates, reports, update_dir = self.build_zero_candidate_run(temporary)
            first = json.loads((update_dir / "chatgpt_first_screen.json").read_text())
            first["pass"] = 1
            write_json(update_dir / "chatgpt_first_screen.json", first)
            with self.assertRaisesRegex(validator.ArtifactValidationError, "pass must equal 0"):
                validator.validate_run(updates, reports, "2026-07-01", "2026-07-31")

        with tempfile.TemporaryDirectory() as temporary:
            updates, reports, update_dir = self.build_zero_candidate_run(temporary)
            final = json.loads((update_dir / "final_screening.json").read_text())
            final["canonical_track"] = "deepseek"
            write_json(update_dir / "final_screening.json", final)
            with self.assertRaisesRegex(validator.ArtifactValidationError, "ChatGPT-canonical"):
                validator.validate_run(updates, reports, "2026-07-01", "2026-07-31")


if __name__ == "__main__":
    unittest.main()
