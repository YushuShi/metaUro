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
            monthly._ENTREZ_VALIDATED_KEY_DIGEST,
            monthly._ENTREZ_REJECTED_KEY_DIGEST,
            monthly._NCBI_LAST_REQUEST_AT,
            Entrez.email,
            Entrez.api_key,
        )
        monthly._ENTREZ_VALIDATED_KEY_DIGEST = None
        monthly._ENTREZ_REJECTED_KEY_DIGEST = None
        monthly._NCBI_LAST_REQUEST_AT = None

    def tearDown(self) -> None:
        (
            monthly._ENTREZ_VALIDATED_KEY_DIGEST,
            monthly._ENTREZ_REJECTED_KEY_DIGEST,
            monthly._NCBI_LAST_REQUEST_AT,
            Entrez.email,
            Entrez.api_key,
        ) = self.original

    def test_http_400_is_validated_and_warned_only_once_per_process(self) -> None:
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
            mock.patch.object(monthly, "load_dotenv"),
            mock.patch.object(Entrez, "esearch", side_effect=rejected_esearch),
            mock.patch.object(monthly, "wait_for_ncbi_request_slot"),
            redirect_stdout(output),
        ):
            self.assertEqual(monthly.configure_entrez(), "rejected_http_400")
            self.assertEqual(monthly.configure_entrez(), "rejected_http_400")

        self.assertEqual(calls, 1)
        self.assertIsNone(Entrez.api_key)
        self.assertEqual(output.getvalue().count("NCBI_API_KEY was rejected"), 1)

    def test_keyed_efetch_400_retries_once_without_key(self) -> None:
        attempts = []

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

    def test_public_request_starts_are_spaced_conservatively(self) -> None:
        Entrez.api_key = None
        with (
            mock.patch.object(monthly.time, "monotonic", side_effect=[1.0, 1.1, 1.37]),
            mock.patch.object(monthly, "_NCBI_RATE_SLEEP") as sleep,
        ):
            monthly.wait_for_ncbi_request_slot()
            monthly.wait_for_ncbi_request_slot()
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.27)


if __name__ == "__main__":
    unittest.main()
