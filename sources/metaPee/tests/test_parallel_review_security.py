import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts import parallel_review_security as security


class ParallelReviewSecurityTests(unittest.TestCase):
    def test_credentials_are_allowlisted_and_not_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mykey.env"
            path.write_text(
                "CORNELL_API_KEY=secret\n"
                "API_Base_URI=https://api.ai.it.cornell.edu/v1\n"
                "UNRELATED_SECRET=must-not-load\n",
                encoding="utf-8",
            )
            before = dict(os.environ)
            values = security.read_allowed_credentials(
                path,
                label="test",
                names=("CORNELL_API_KEY", "API_Base_URI"),
            )
            self.assertEqual(
                values,
                {
                    "CORNELL_API_KEY": "secret",
                    "API_Base_URI": "https://api.ai.it.cornell.edu/v1",
                },
            )
            self.assertEqual(dict(os.environ), before)
            link = Path(directory) / "linked.env"
            link.symlink_to(path)
            with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
                security.read_allowed_credentials(link, label="test")

    def test_cornell_url_is_exact_and_errors_are_redacted(self):
        self.assertEqual(
            security.validated_cornell_base_url(
                "https://api.ai.it.cornell.edu/v1/"
            ),
            "https://api.ai.it.cornell.edu/v1",
        )
        for value in (
            "http://api.ai.it.cornell.edu/v1",
            "https://api.ai.it.cornell.edu.evil.example/v1",
            "https://api.ai.it.cornell.edu/v1/models",
        ):
            with self.assertRaises(RuntimeError):
                security.validated_cornell_base_url(value)
        redacted = security.redact_error(
            "Authorization: Bearer supersecret Cookie=session=abc "
            "api_key='alsosecret' sk-abcdefghijk"
        )
        self.assertNotIn("supersecret", redacted)
        self.assertNotIn("alsosecret", redacted)
        self.assertNotIn("abcdefghijk", redacted)

    def test_atomic_write_ignores_predictable_tmp_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "state.json"
            victim = root / "victim"
            victim.write_text("safe", encoding="utf-8")
            target.with_suffix(".json.tmp").symlink_to(victim)
            security.atomic_write_json(target, {"ok": True})
            self.assertEqual(json.loads(target.read_text()), {"ok": True})
            self.assertEqual(victim.read_text(encoding="utf-8"), "safe")

    def test_cache_receipt_acceptance_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            cache = output / "model_response_cache" / "claude_r2"
            cache.mkdir(parents=True)
            text = '{"results":[]}'
            common = {
                "receipt_id": "r1",
                "stage": "claude_r2",
                "model_requested": "claude-opus-5",
                "work_unit_key": "u1",
                "attempt": 1,
                "context_ids": ["1|Exposure|Disease"],
                "input_digest": "i1",
                "output_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "response_cost_usd": "0.1",
                "validation_status": "valid",
                "scientific_result_accepted": True,
            }
            record = {
                **common,
                "output_text": text,
                "validated_results": [{"context_id": "1|Exposure|Disease"}],
            }
            security.atomic_write_json(cache / "u1_0001.json", record)
            security.atomic_write_json(
                output / "gateway_receipts.json", {"receipts": [common]}
            )
            self.assertEqual(
                security.audit_cache_receipt_acceptance(output)["cache_files"],
                1,
            )
            mismatched_receipt = {**common, "validation_status": "received"}
            security.atomic_write_json(
                output / "gateway_receipts.json",
                {"receipts": [mismatched_receipt]},
            )
            with self.assertRaisesRegex(RuntimeError, "validation status differs"):
                security.audit_cache_receipt_acceptance(output)
            security.atomic_write_json(
                output / "gateway_receipts.json", {"receipts": [common]}
            )
            record["scientific_result_accepted"] = False
            security.atomic_write_json(cache / "u1_0001.json", record)
            with self.assertRaisesRegex(RuntimeError, "acceptance differs"):
                security.audit_cache_receipt_acceptance(output)

    def test_receipt_status_and_unmatched_receipts_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            security.atomic_write_json(
                output / "gateway_receipts.json",
                {"receipts": [{"receipt_id": "orphan"}]},
            )
            with self.assertRaisesRegex(RuntimeError, "not an explicitly documented"):
                security.audit_cache_receipt_acceptance(output)
            security.atomic_write_json(
                output / "gateway_receipts.json",
                {
                    "receipts": [
                        {
                            "receipt_id": "orphan",
                            "validation_status": "unknown_after_process_interruption",
                        }
                    ]
                },
            )
            self.assertEqual(
                security.audit_cache_receipt_acceptance(output)[
                    "unmatched_receipts"
                ],
                1,
            )


if __name__ == "__main__":
    unittest.main()
