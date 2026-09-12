import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import cost_accounting
from scripts import dual_screening


class CostAccountingTests(unittest.TestCase):
    def test_additional_cost_scope_includes_v2_catalog(self):
        root = Path(__file__).resolve().parents[1]
        exposures = cost_accounting._additional_exposure_names(root)
        self.assertEqual(len(exposures), 215)
        self.assertIn("DASH diet", exposures)

    def test_list_price_uses_cached_input_as_input_subset(self):
        usage = {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 250_000,
            "output_tokens": 100_000,
        }
        # 750k * $0.20/MTok + 250k * $0.02/MTok + 100k * $1.20/MTok
        self.assertAlmostEqual(
            cost_accounting.estimated_cost_usd(usage, "gpt-5.6-luna"),
            0.275,
        )
        self.assertAlmostEqual(
            cost_accounting.estimated_cost_usd(usage, "gpt-5.6-sol"),
            6.875,
        )

    def test_actual_response_usage_is_allocated_without_changing_totals(self):
        usage = dual_screening.empty_usage("gpt-5.6-luna")
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=101,
                input_tokens_details=SimpleNamespace(cached_tokens=20),
                output_tokens=50,
                output_tokens_details=SimpleNamespace(reasoning_tokens=11),
            )
        )
        dual_screening._add_usage(
            usage,
            response,
            {"Tomato": 2, "Coffee": 1},
        )

        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["input_tokens"], 101)
        self.assertEqual(usage["cached_input_tokens"], 20)
        self.assertEqual(usage["output_tokens"], 50)
        self.assertEqual(usage["reasoning_tokens"], 11)
        by_exposure = usage["by_exposure"]
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
        ):
            self.assertEqual(
                sum(item[field] for item in by_exposure.values()),
                usage[field],
            )
        self.assertGreater(
            by_exposure["Tomato"]["input_tokens"],
            by_exposure["Coffee"]["input_tokens"],
        )
        self.assertTrue(
            all(
                item["cached_input_tokens"] <= item["input_tokens"]
                for item in by_exposure.values()
            )
        )

    def test_litellm_chat_usage_aliases_and_dict_details_are_supported(self):
        usage = dual_screening.empty_usage("gpt-5.6-luna")
        response = SimpleNamespace(
            usage={
                "prompt_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 12},
                "completion_tokens": 9,
                "completion_tokens_details": {"reasoning_tokens": 3},
            }
        )
        dual_screening._add_usage(usage, response, {"Tomato": 1})
        self.assertEqual(usage["input_tokens"], 40)
        self.assertEqual(usage["cached_input_tokens"], 12)
        self.assertEqual(usage["output_tokens"], 9)
        self.assertEqual(usage["reasoning_tokens"], 3)

    def test_long_context_multiplier_is_retained_and_costed_per_call(self):
        usage = dual_screening.empty_usage("gpt-5.6-luna")
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=300_000,
                input_tokens_details=SimpleNamespace(cached_tokens=50_000),
                output_tokens=10_000,
                output_tokens_details=SimpleNamespace(reasoning_tokens=2_000),
            )
        )
        dual_screening._add_usage(usage, response, {"Tomato": 1})

        allocated = usage["by_exposure"]["Tomato"]
        self.assertEqual(allocated["long_context_calls"], 1)
        self.assertEqual(allocated["long_context_input_tokens"], 300_000)
        # (250k * .20 + 50k * .02) * 2 + (10k * 1.20) * 1.5
        self.assertAlmostEqual(
            cost_accounting.estimated_cost_usd(allocated, "gpt-5.6-luna"),
            0.12,
        )

    def test_report_lists_zero_candidate_rows_and_per_stage_actual_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "data" / "monthly_updates" / "run"
            (root / "static").mkdir(parents=True)
            output.mkdir(parents=True)
            (root / "static" / "exposures.json").write_text(
                json.dumps(["Coffee", "Pepper (Capsicum)", "Tomato"]),
                encoding="utf-8",
            )
            (output / "candidates.json").write_text(
                json.dumps(
                    {
                        "publication_date_start": "2026-07-01",
                        "publication_date_end": "2026-07-31",
                        "articles": [
                            {
                                "candidate_contexts": [
                                    {
                                        "exposure": "Tomato",
                                        "cancer": "Prostate cancer",
                                    }
                                ]
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output / "chatgpt_first_screen_progress.json").write_text(
                json.dumps(
                    {
                        "usage": {
                            "model": "gpt-5.6-luna",
                            "calls": 1,
                            "input_tokens": 100,
                            "cached_input_tokens": 20,
                            "output_tokens": 50,
                            "reasoning_tokens": 10,
                            "by_exposure": {
                                "Tomato": {
                                    "calls": 1,
                                    "input_tokens": 100,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 50,
                                    "reasoning_tokens": 10,
                                }
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            (output / "chatgpt_second_screen.json").write_text(
                json.dumps(
                    {
                        "usage": {
                            "model": "gpt-5.6-sol",
                            "calls": 1,
                            "input_tokens": 200,
                            "cached_input_tokens": 0,
                            "output_tokens": 20,
                            "reasoning_tokens": 0,
                            "by_exposure": {
                                "Tomato": {
                                    "calls": 1,
                                    "input_tokens": 200,
                                    "cached_input_tokens": 0,
                                    "output_tokens": 20,
                                    "reasoning_tokens": 0,
                                }
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            path = cost_accounting.write_cost_report(output, root)
            report = path.read_text(encoding="utf-8")

        self.assertIn("Coffee | no | $0.000000 | $0.000000 | $0.000000", report)
        self.assertIn(
            "Tomato | yes | $0.000076 | $0.001600 | $0.001676",
            report,
        )
        self.assertIn("Luna input tokens", report)
        self.assertIn("100 | 20 | 50 | 150 | 200 | 0 | 20 | 220", report)
        self.assertIn("First title/abstract/MeSH screen", report)
        self.assertIn("Detailed title/abstract/MeSH screen", report)
        self.assertIn("may differ from the amount billed by the Cornell gateway", report)
        self.assertIn("superseded checkpoint is not included", report)
        self.assertIn("does not report cache-write tokens", report)
        self.assertNotIn("cornell-secret-value", report)

    def test_historical_report_labels_no_lower_limit_and_lists_all_228_exposures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "data" / "historical_updates" / "through_2026-06-30"
            (root / "static").mkdir(parents=True)
            output.mkdir(parents=True)
            exposures = [f"Exposure {index:03d}" for index in range(1, 229)]
            (root / "static" / "exposures.json").write_text(
                json.dumps(exposures),
                encoding="utf-8",
            )
            (output / "candidates.json").write_text(
                json.dumps(
                    {
                        "publication_date_start": None,
                        "publication_date_end": "2026-06-30",
                        "articles": [],
                    }
                ),
                encoding="utf-8",
            )

            report = cost_accounting.render_cost_report(
                output,
                root,
                updated_at="2026-08-16T12:00:00+08:00",
            )

        self.assertIn(
            "Run: no lower publication-date limit through 2026-06-30",
            report,
        )
        self.assertIn("gpt-5.6-luna | $0.20 | $0.02 | $1.20", report)
        rows = [
            line
            for line in report.splitlines()
            if line.startswith("Exposure ") and " | no | " in line
        ]
        self.assertEqual(len(rows), 228)
        self.assertTrue(all("$0.000000 | $0.000000 | $0.000000" in line for line in rows))
        self.assertTrue(all(line.endswith("0 | 0 | 0 | 0 | 0 | 0 | 0 | 0") for line in rows))

    def test_additional_scope_writes_separate_cost_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "data" / "monthly_updates" / "additional-run"
            (root / "static").mkdir(parents=True)
            output.mkdir(parents=True)
            (root / "static" / "exposures.json").write_text(
                json.dumps(["Current exposure"]), encoding="utf-8"
            )
            (root / "data" / "additionalExposure.json").write_text(
                json.dumps({"Additional exposure": {"aliases": ["extra alias"]}}),
                encoding="utf-8",
            )
            (output / "candidates.json").write_text(
                json.dumps({"exposure_scope": "additional", "articles": []}),
                encoding="utf-8",
            )

            path = cost_accounting.write_cost_report(output, root)
            report = path.read_text(encoding="utf-8")

        self.assertEqual(path.name, "additionalExposureCost.txt")
        self.assertIn("Additional exposure | no |", report)
        self.assertNotIn("Current exposure | no |", report)


if __name__ == "__main__":
    unittest.main()
