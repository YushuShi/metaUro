from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from Bio import Entrez

from scripts import monthly_evidence_update as monthly


class Http400(RuntimeError):
    code = 400


class ReadableHandle:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b"<article/>"


class NcbiKeyFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = (
            monthly._ENTREZ_REJECTED_KEY_DIGEST,
            monthly._NCBI_LAST_REQUEST_AT,
            Entrez.email,
            Entrez.api_key,
            Entrez.max_tries,
        )
        monthly._ENTREZ_REJECTED_KEY_DIGEST = None
        monthly._NCBI_LAST_REQUEST_AT = None

    def tearDown(self) -> None:
        (
            monthly._ENTREZ_REJECTED_KEY_DIGEST,
            monthly._NCBI_LAST_REQUEST_AT,
            Entrez.email,
            Entrez.api_key,
            Entrez.max_tries,
        ) = self.original

    def test_http_400_is_latched_and_warned_only_once_despite_dotenv_reload(self) -> None:
        calls = 0

        def rejected_esearch(**_kwargs):
            nonlocal calls
            calls += 1
            raise Http400("HTTP 400 invalid api key")

        output = io.StringIO()
        environment = {
            "NCBI_EMAIL": "review@example.org",
            "NCBI_API_KEY": "rejected-secret",
            "MONTHLY_REQUIRE_VALID_NCBI_KEY": "false",
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(monthly, "load_dotenv") as dotenv,
            mock.patch.object(Entrez, "esearch", side_effect=rejected_esearch),
            mock.patch.object(monthly, "wait_for_ncbi_request_slot"),
            redirect_stdout(output),
        ):
            self.assertEqual(monthly.configure_entrez(), "rejected_http_400")
            self.assertEqual(monthly.configure_entrez(), "rejected_http_400")

        self.assertEqual(calls, 1)
        self.assertEqual(dotenv.call_count, 2)
        self.assertIsNone(Entrez.api_key)
        self.assertEqual(Entrez.max_tries, 1)
        self.assertEqual(output.getvalue().count("NCBI_API_KEY was rejected"), 1)

    def test_keyed_efetch_http_400_gets_exactly_one_public_retry(self) -> None:
        attempts: list[str | None] = []

        def efetch(**_kwargs):
            attempts.append(Entrez.api_key)
            if len(attempts) == 1:
                raise Http400("HTTP 400 invalid api key")
            return ReadableHandle()

        Entrez.api_key = "rejected-secret"
        with (
            mock.patch.object(Entrez, "efetch", side_effect=efetch),
            mock.patch.object(monthly, "wait_for_ncbi_request_slot"),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(monthly.fetch_pmc_full_text("PMC1"), b"<article/>")

        self.assertEqual(attempts, ["rejected-secret", None])

    def test_failed_public_retry_is_not_retried(self) -> None:
        attempts: list[str | None] = []

        def efetch(**_kwargs):
            attempts.append(Entrez.api_key)
            if len(attempts) == 1:
                raise Http400("HTTP 400 invalid api key")
            raise RuntimeError("HTTP 502 public endpoint failure")

        Entrez.api_key = "rejected-secret"
        with (
            mock.patch.object(Entrez, "efetch", side_effect=efetch),
            mock.patch.object(monthly, "wait_for_ncbi_request_slot"),
            mock.patch.object(monthly.time, "sleep") as retry_sleep,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                monthly.fetch_pmc_full_text("PMC1")

        self.assertEqual(attempts, ["rejected-secret", None])
        retry_sleep.assert_not_called()

    def test_strict_mode_never_falls_back_to_public_efetch(self) -> None:
        attempts: list[str | None] = []

        def efetch(**_kwargs):
            attempts.append(Entrez.api_key)
            raise Http400("HTTP 400 invalid api key")

        Entrez.api_key = "rejected-secret"
        with (
            mock.patch.dict(
                os.environ,
                {"MONTHLY_REQUIRE_VALID_NCBI_KEY": "true"},
                clear=True,
            ),
            mock.patch.object(Entrez, "efetch", side_effect=efetch),
            mock.patch.object(monthly, "wait_for_ncbi_request_slot"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                monthly.fetch_pmc_full_text("PMC1")

        self.assertEqual(attempts, ["rejected-secret"])
        self.assertEqual(Entrez.api_key, "rejected-secret")
        self.assertIsNone(monthly._ENTREZ_REJECTED_KEY_DIGEST)

    def test_public_request_starts_are_spaced_at_least_point_37_seconds(self) -> None:
        Entrez.api_key = None
        with (
            mock.patch.object(
                monthly.time,
                "monotonic",
                side_effect=[1.0, 1.1, 1.37],
            ),
            mock.patch.object(monthly, "_NCBI_RATE_SLEEP") as sleeper,
        ):
            monthly.wait_for_ncbi_request_slot()
            monthly.wait_for_ncbi_request_slot()

        self.assertGreaterEqual(monthly.NCBI_PUBLIC_MIN_INTERVAL_SECONDS, 0.37)
        sleeper.assert_called_once()
        self.assertAlmostEqual(sleeper.call_args.args[0], 0.27)

    def test_keyed_request_starts_are_spaced_at_least_point_11_seconds(self) -> None:
        Entrez.api_key = "working-key"
        with (
            mock.patch.object(
                monthly.time,
                "monotonic",
                side_effect=[2.0, 2.05, 2.11],
            ),
            mock.patch.object(monthly, "_NCBI_RATE_SLEEP") as sleeper,
        ):
            monthly.wait_for_ncbi_request_slot()
            monthly.wait_for_ncbi_request_slot()

        self.assertGreaterEqual(monthly.NCBI_KEYED_MIN_INTERVAL_SECONDS, 0.11)
        sleeper.assert_called_once()
        self.assertAlmostEqual(sleeper.call_args.args[0], 0.06)


if __name__ == "__main__":
    unittest.main()
