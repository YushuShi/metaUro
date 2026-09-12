import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import audit_blocked_pmc_sources, dual_screening
from scripts import run_claude_gemini_parallel_review as review


class ClaudeGeminiParallelReviewTests(unittest.TestCase):
    def test_gemini_cache_normalizer_is_deep_copy_and_exact_only(self):
        source_text = (
            "Verified full text starts here. Exact abstract excerpt. "
            "The reported subdistribution estimate appears in Table 2."
        )
        aliases = [
            "EXCLUDE_EXPOSURE",
            "WRONG_EXPOSURE",
            "E3_wrong_exposure",
            "EXCLUDE_WRONG_EXPOSURE",
            "EXCLUDE_STUDY_DESIGN",
            "EXCLUDE_ECOLOGICAL",
            "S2_NO_USABLE_DATA",
        ]
        results = [
            {
                "context_id": f"1|Exposure {index}|Prostate cancer",
                "exclusion_code": code,
                "extraction": None,
                "jbi": None,
            }
            for index, code in enumerate(aliases)
        ]
        results.append(
            {
                "context_id": "1|Vitamin D|Prostate cancer",
                "exclusion_code": None,
                "extraction": {
                    "effect_type": "sHR",
                    "evidence": {
                        "sample_size": {
                            "excerpt": "Exact abstract excerpt.",
                            "source": "abstract",
                            "url": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/",
                            "location": "Abstract, Results",
                        },
                        "cases": {
                            "excerpt": "not present verbatim",
                            "source": "abstract",
                            "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
                            "location": "Abstract",
                        },
                        "effect_size": {
                            "excerpt": "Exact abstract excerpt.",
                            "source": "abstract",
                            "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
                            "location": "Table 2",
                        },
                        "p_value": {
                            "excerpt": "Exact abstract excerpt.",
                            "source": "full_text",
                            "url": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC999/",
                            "location": "Abstract",
                        },
                    },
                },
                "jbi": {
                    "checklist_type": "cohort",
                    "answers": {
                        **{f"q{number}": "Yes" for number in range(1, 12)},
                        "q12": "NA",
                        "q13": "Yes",
                    },
                },
            }
        )
        payload = {"results": results}
        original = json.loads(json.dumps(payload))

        normalized, events = review._normalise_gemini_r3_payload(
            payload,
            expected_pmcid="PMC123",
            source_text=source_text,
        )

        self.assertEqual(payload, original, "raw payload must remain untouched")
        self.assertEqual(
            [row["exclusion_code"] for row in normalized["results"][:4]],
            ["S2-EXPOSURE-MISMATCH"] * 4,
        )
        self.assertEqual(
            [row["exclusion_code"] for row in normalized["results"][4:7]],
            aliases[4:],
            "non-allowlisted codes must not be guessed",
        )
        included = normalized["results"][-1]
        evidence = included["extraction"]["evidence"]
        self.assertEqual(included["extraction"]["effect_type"], "HR")
        self.assertEqual(
            evidence["sample_size"]["url"],
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
        )
        self.assertEqual(evidence["sample_size"]["source"], "full_text")
        self.assertEqual(evidence["cases"]["source"], "abstract")
        self.assertEqual(evidence["effect_size"]["source"], "abstract")
        self.assertEqual(
            evidence["p_value"]["url"],
            "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC999/",
        )
        self.assertEqual(included["jbi"]["answers"]["q13"], "NA")
        event_types = [event["event_type"] for event in events]
        self.assertEqual(event_types.count("exclusion_code_alias"), 4)
        self.assertEqual(event_types.count("effect_type_alias"), 1)
        self.assertEqual(event_types.count("legacy_pmc_url"), 1)
        self.assertEqual(event_types.count("verified_source_label"), 1)
        self.assertEqual(event_types.count("jbi_beyond_checklist"), 1)

    def test_exact_subdistribution_hazard_aliases_map_to_hr_only(self):
        aliases = [
            "SHR",
            "sub-hazard",
            "subhazard",
            "subdistribution hazard",
            "sub-hazard ratio",
            "subhazard ratio",
            "subdistribution hazard ratio",
        ]
        near_misses = ["adjusted SHR", "sub hazard ratio", "subdistribution HR"]
        payload = {
            "results": [
                {
                    "context_id": f"1|Exposure {index}|Prostate cancer",
                    "extraction": {"effect_type": value, "evidence": {}},
                }
                for index, value in enumerate([*aliases, *near_misses])
            ]
        }
        normalized, events = review._normalise_gemini_r3_payload(
            payload,
            expected_pmcid="PMC123",
            source_text="full text",
        )
        values = [row["extraction"]["effect_type"] for row in normalized["results"]]
        self.assertEqual(values[: len(aliases)], ["HR"] * len(aliases))
        self.assertEqual(values[len(aliases) :], near_misses)
        self.assertEqual(
            sum(event["event_type"] == "effect_type_alias" for event in events),
            len(aliases),
        )

    def test_r3_prompt_and_schema_require_exact_pmc_url_and_code_enum(self):
        contexts = {
            "1|Vitamin D|Prostate cancer": {
                "context_id": "1|Vitamin D|Prostate cancer",
                "pmid": "1",
                "pmcid": "PMC123",
                "title": "Title",
                "exposure": "Vitamin D",
                "cancer": "Prostate cancer",
            }
        }
        prompt = review._r3_prompt(contexts, "complete full text")
        self.assertIn(
            "exactly this URL: https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
            prompt,
        )
        self.assertIn("Every context requires a non-empty one-sentence reason", prompt)
        for code in dual_screening.EXCLUSION_CODES:
            self.assertIn(code, prompt)
        schema = review._gateway_response_schema("gemini_r3")
        code_schema = schema["schema"]["properties"]["results"]["items"][
            "properties"
        ]["exclusion_code"]
        self.assertEqual(
            set(code_schema["enum"]),
            {None, *dual_screening.EXCLUSION_CODES},
        )
        base_schema = dual_screening.response_schema("full_text")
        self.assertNotIn(
            "enum",
            base_schema["schema"]["properties"]["results"]["items"][
                "properties"
            ]["exclusion_code"],
            "runner schema hardening must not mutate the shared schema",
        )

    def test_pmid_21518693_prompt_has_neutral_prevalence_reminder_only_there(self):
        def context(pmid):
            return {
                f"{pmid}|Omega-3 fatty acids|Prostate cancer": {
                    "context_id": f"{pmid}|Omega-3 fatty acids|Prostate cancer",
                    "pmid": pmid,
                    "pmcid": "PMC3145396" if pmid == "21518693" else "PMC123",
                    "title": "Title",
                    "exposure": "Omega-3 fatty acids",
                    "cancer": "Prostate cancer",
                }
            }

        forced = review._r3_prompt(context("21518693"), "full text")
        ordinary = review._r3_prompt(context("123"), "full text")
        self.assertIn("7-year-period prevalence", forced)
        self.assertIn("does not preassign an inclusion or exclusion decision", forced)
        self.assertNotIn("7-year-period prevalence", ordinary)

    def test_safe_error_redacts_common_secret_forms(self):
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "not-a-real-private-key-value\n"
            "-----END PRIVATE KEY-----"
        )
        secret_values = [
            "bearer-example-value",
            "basic-example-value==",
            "bare-bearer-example",
            "quoted-api-example",
            "plain-token-example",
            "password-example",
            "client-secret-example",
            "session-cookie-example",
            "cookie-header-example",
            "set-cookie-example",
            "sk-notarealtoken1234",
            "github_pat_notarealtoken123456",
            "AKIA1234567890ABCDEF",
            "AIza1234567890abcdefghijklmnopqrstuv",
            "not-a-real-private-key-value",
        ]
        value = "\n".join(
            [
                "safe prefix",
                f"Authorization: Bearer {secret_values[0]}",
                f"Proxy_Authorization=Basic {secret_values[1]}",
                f"Bearer {secret_values[2]}",
                f'"api_key": "{secret_values[3]}"',
                f"access_token={secret_values[4]}",
                f"password: {secret_values[5]}",
                f"client_secret={secret_values[6]}",
                f"session_cookie={secret_values[7]}",
                f"Cookie: session={secret_values[8]}; Path=/",
                f"Set-Cookie={secret_values[9]}; Secure",
                *secret_values[10:14],
                private_key,
                "safe suffix",
            ]
        )
        redacted = review.safe_error(value)
        self.assertIn("safe prefix", redacted)
        self.assertIn("[REDACTED]", redacted)
        for secret in secret_values:
            self.assertNotIn(secret, redacted)

    def test_cornell_gateway_url_is_exact_https_host_and_known_base(self):
        self.assertEqual(
            review._validated_cornell_base_url(
                "https://api.ai.it.cornell.edu/"
            ),
            "https://api.ai.it.cornell.edu",
        )
        self.assertEqual(
            review._validated_cornell_base_url(
                "https://api.ai.it.cornell.edu:443/v1/"
            ),
            "https://api.ai.it.cornell.edu/v1",
        )
        for value in (
            "http://api.ai.it.cornell.edu",
            "https://api.ai.it.cornell.edu.evil.example/v1",
            "https://user:password@api.ai.it.cornell.edu/v1",
            "https://api.ai.it.cornell.edu:444/v1",
            "https://api.ai.it.cornell.edu/other",
            "https://api.ai.it.cornell.edu/v1?redirect=evil",
            "https://api.ai.it.cornell.edu/v1?",
            "https://api.ai.it.cornell.edu/v1#fragment",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "approved HTTPS host"):
                    review._validated_cornell_base_url(value)

    def test_local_credential_loading_is_allowlisted_and_never_exported(self):
        with tempfile.TemporaryDirectory() as temporary:
            credentials = Path(temporary) / "mykey.env"
            credentials.write_text(
                "Cornell_API_Key=file-cornell-key\n"
                "API_Base_URI=https://api.ai.it.cornell.edu/v1\n"
                "NCBI_API_KEY=file-ncbi-key\n"
                "NCBI_EMAIL=reviewer@example.edu\n"
                "deepseek_api_key=must-not-load\n"
                "PASSWORD=must-not-export\n",
                encoding="utf-8",
            )
            environment = {
                "CORNELL_API_KEY": "ambient-cornell-key",
                "CORNELL_API_BASE_URL": "https://evil.example/v1",
                "UNCHANGED_VALUE": "preserved",
            }
            with patch.dict(os.environ, environment, clear=True):
                before = dict(os.environ)
                values = review._allowed_credential_values(credentials)
                self.assertEqual(dict(os.environ), before)
                with self.assertRaisesRegex(RuntimeError, "non-allowlisted"):
                    review._allowed_credential_values(
                        credentials, names=("deepseek_api_key",)
                    )
        self.assertEqual(values["Cornell_API_Key"], "file-cornell-key")
        self.assertEqual(
            values["API_Base_URI"], "https://api.ai.it.cornell.edu/v1"
        )
        self.assertEqual(values["NCBI_API_KEY"], "file-ncbi-key")
        self.assertNotIn("CORNELL_API_KEY", values)
        self.assertNotIn("deepseek_api_key", values)
        self.assertNotIn("PASSWORD", values)

    def test_client_accepts_local_legacy_aliases_and_rejects_ambient_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            credentials = Path(temporary) / "mykey.env"
            credentials.write_text(
                "Cornell_API_Key=test-key\n"
                "API_Base_URI=https://api.ai.it.cornell.edu/\n",
                encoding="utf-8",
            )
            expected_client = object()
            with (
                patch.object(review, "AUTHORIZED_CREDENTIAL_FILE", credentials),
                patch.object(
                    review, "OpenAI", return_value=expected_client
                ) as constructor,
                patch.dict(
                    os.environ,
                    {
                        "CORNELL_API_KEY": "ambient-key",
                        "CORNELL_API_BASE_URL": "https://evil.example/v1",
                    },
                    clear=True,
                ),
            ):
                before = dict(os.environ)
                client = review._configure_client()
                self.assertEqual(dict(os.environ), before)
            self.assertIs(client, expected_client)
            constructor.assert_called_once_with(
                api_key="test-key",
                base_url="https://api.ai.it.cornell.edu",
                timeout=600,
            )

            credentials.write_text(
                "NCBI_EMAIL=reviewer@example.edu\n", encoding="utf-8"
            )
            with (
                patch.object(review, "AUTHORIZED_CREDENTIAL_FILE", credentials),
                patch.dict(
                    os.environ,
                    {
                        "CORNELL_API_KEY": "ambient-key",
                        "CORNELL_API_BASE_URL": (
                            "https://api.ai.it.cornell.edu/v1"
                        ),
                    },
                    clear=True,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "not configured"):
                    review._configure_client()

    def test_authorized_local_credential_path_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "actual.env"
            target.write_text("NCBI_EMAIL=reviewer@example.edu\n", encoding="utf-8")
            link = root / "mykey.env"
            link.symlink_to(target)
            with patch.object(review, "AUTHORIZED_CREDENTIAL_FILE", link):
                with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
                    review._authorized_credentials_file()

    def test_entrez_credentials_are_file_scoped_and_environment_is_unchanged(self):
        entrez = review.monthly_evidence_update.Entrez
        previous = (
            getattr(entrez, "email", None),
            getattr(entrez, "api_key", None),
            getattr(entrez, "tool", None),
            getattr(entrez, "max_tries", None),
        )
        with tempfile.TemporaryDirectory() as temporary:
            credentials = Path(temporary) / "mykey.env"
            credentials.write_text(
                "NCBI_EMAIL=file-reviewer@example.edu\n"
                "NCBI_API_KEY=file-ncbi-key\n",
                encoding="utf-8",
            )
            with (
                patch.object(review, "AUTHORIZED_CREDENTIAL_FILE", credentials),
                patch.object(
                    review.monthly_evidence_update,
                    "_ENTREZ_REJECTED_KEY_DIGEST",
                    None,
                ),
                patch.dict(
                    os.environ,
                    {
                        "NCBI_EMAIL": "ambient@example.edu",
                        "NCBI_API_KEY": "ambient-key",
                        "UNCHANGED_VALUE": "preserved",
                    },
                    clear=True,
                ),
            ):
                before = dict(os.environ)
                with review._configured_local_entrez():
                    self.assertEqual(entrez.email, "file-reviewer@example.edu")
                    self.assertEqual(entrez.api_key, "file-ncbi-key")
                    self.assertEqual(dict(os.environ), before)
                self.assertEqual(dict(os.environ), before)
        self.assertEqual(
            (
                getattr(entrez, "email", None),
                getattr(entrez, "api_key", None),
                getattr(entrez, "tool", None),
                getattr(entrez, "max_tries", None),
            ),
            previous,
        )

    def test_atomic_json_source_and_cost_writes_do_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            victim = root / "victim.txt"
            victim.write_text("do not overwrite", encoding="utf-8")

            json_path = root / "checkpoint.json"
            json_path.symlink_to(victim)
            predictable = root / "checkpoint.json.tmp"
            predictable.symlink_to(victim)
            review.save_json(json_path, {"safe": True})
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not overwrite")
            self.assertFalse(json_path.is_symlink())
            self.assertEqual(json.loads(json_path.read_text()), {"safe": True})
            self.assertTrue(predictable.is_symlink())

            bytes_path = root / "source.bin"
            bytes_path.symlink_to(victim)
            review._durable_atomic_write_bytes(bytes_path, b"verified source")
            self.assertEqual(bytes_path.read_bytes(), b"verified source")
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not overwrite")

            text_path = root / "source.txt"
            text_path.symlink_to(victim)
            review._durable_atomic_write_text(text_path, "verified text\n")
            self.assertEqual(text_path.read_text(), "verified text\n")
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not overwrite")

            cost_path = root / "ClaudeGeminiCost.txt"
            cost_path.symlink_to(victim)
            with (
                patch.object(review, "COST_PATH", cost_path),
                patch.object(review, "catalog_exposures", return_value=["Vitamin B6"]),
            ):
                review._write_cost_files(root / "output")
            self.assertFalse(cost_path.is_symlink())
            self.assertIn("Vitamin B6", cost_path.read_text())
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not overwrite")
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_pmc_html_retries_only_transient_canonical_shell_and_keeps_strict_gate(self):
        def route(valid, reason, marker):
            raw = marker.encode("utf-8")
            return (
                {
                    "route": "pmc_canonical_article_html",
                    "validation": {
                        "valid": valid,
                        "reason": reason,
                        "bytes": len(raw),
                        "sha256": review.hashlib.sha256(raw).hexdigest(),
                    },
                    "transport": {
                        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC4763492/",
                        "final_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC4763492/",
                        "redirected": False,
                        "http_status": 200,
                        "content_type": "text/html; charset=utf-8",
                        "request_accept": "text/html; charset=utf-8",
                        "request_cookie": "",
                        "attempts": [
                            {
                                "attempt": 1,
                                "http_status": 200,
                                "bytes": len(raw),
                                "sha256": review.hashlib.sha256(raw).hexdigest(),
                                "error": "",
                                "redirected": False,
                                "content_type": "text/html; charset=utf-8",
                            }
                        ],
                    },
                },
                "verified text" if valid else "",
                raw,
            )

        responses = [
            route(False, "canonical_url_identity_mismatch", "shell-one"),
            route(False, "canonical_url_identity_mismatch", "shell-two"),
            route(
                True,
                "validated_complete_canonical_pmc_html_article_body",
                "article",
            ),
        ]
        delays = []
        with patch.object(
            review.audit_blocked_pmc_sources,
            "probe_pmc_html",
            side_effect=responses,
        ) as probe:
            final_route, text, raw, attempts = (
                review._probe_pmc_html_with_bounded_transient_retry(
                    "PMC4763492",
                    "26817504",
                    user_agent="test-agent",
                    sleep=delays.append,
                )
            )
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(delays, [1.0, 2.0])
        self.assertTrue(final_route["validation"]["valid"])
        self.assertEqual(text, "verified text")
        self.assertEqual(raw, b"article")
        self.assertEqual(
            [item["validation"]["reason"] for item in attempts],
            [
                "canonical_url_identity_mismatch",
                "canonical_url_identity_mismatch",
                "validated_complete_canonical_pmc_html_article_body",
            ],
        )
        self.assertTrue(
            all("request_cookie" not in item["transport"] for item in attempts)
        )
        self.assertTrue(
            all(
                item["transport"]["request_cookie_present"] is False
                for item in attempts
            )
        )

    def test_pmc_html_does_not_retry_non_transient_strict_validation_failure(self):
        route = {
            "route": "pmc_canonical_article_html",
            "validation": {
                "valid": False,
                "reason": "pmid_identity_mismatch",
            },
            "transport": {
                "request_cookie": "",
                "attempts": [],
            },
        }
        delays = []
        with patch.object(
            review.audit_blocked_pmc_sources,
            "probe_pmc_html",
            return_value=(route, "", b"wrong-identity"),
        ) as probe:
            final_route, text, raw, attempts = (
                review._probe_pmc_html_with_bounded_transient_retry(
                    "PMC4763492",
                    "26817504",
                    user_agent="test-agent",
                    sleep=delays.append,
                )
            )
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(delays, [])
        self.assertIs(final_route, route)
        self.assertEqual(text, "")
        self.assertEqual(raw, b"wrong-identity")
        self.assertEqual(len(attempts), 1)

    def test_pdf_pow_cookie_is_not_persisted_in_source_receipt(self):
        cookie = "cloudpmc-viewer-pow=secret-solved-pow-value"
        transport = {
            "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC99999999/bin/x.pdf",
            "final_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC99999999/bin/x.pdf",
            "redirected": False,
            "http_status": 200,
            "content_type": "application/pdf",
            "request_accept": "application/pdf",
            "request_cookie": cookie,
            "attempts": [
                {
                    "attempt": 1,
                    "http_status": 200,
                    "bytes": 100,
                    "sha256": "a" * 64,
                    "error": "",
                    "redirected": False,
                    "content_type": "application/pdf",
                }
            ],
        }
        full_text = "Verified Methods Results Discussion evidence. " * 150
        html_route = {
            "route": "pmc_canonical_article_html",
            "validation": {"valid": False, "reason": "narrative_too_short"},
            "transport": {**transport, "request_cookie": ""},
        }
        pdf_route = {
            "validation": {"valid": True, "reason": "validated_pdf"},
            "pdf_transport": transport,
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with (
                patch.object(
                    review, "_configured_local_entrez",
                    return_value=review.contextlib.nullcontext(),
                ),
                patch.object(
                    review.monthly_evidence_update,
                    "fetch_pmc_full_text",
                    return_value=b"<invalid/>",
                ),
                patch.object(
                    review.audit_blocked_pmc_sources,
                    "validate_jats_receipt",
                    return_value=({"valid": False, "reason": "invalid_jats"}, ""),
                ),
                patch.object(
                    review, "_allowed_credential_values",
                    return_value={"NCBI_EMAIL": "reviewer@example.edu"},
                ),
                patch.object(
                    review.audit_blocked_pmc_sources,
                    "probe_pmc_html",
                    return_value=(html_route, "", b"html"),
                ),
                patch.object(
                    review.audit_blocked_pmc_sources,
                    "probe_pmc_pdf",
                    return_value=(pdf_route, full_text, b"%PDF-verified"),
                ),
            ):
                _text, receipt = review._verified_pmc_text_unlocked(
                    output, "PMC99999999", "99999999"
                )
            tracked = json.loads(
                (output / "full_text_cache" / "PMC99999999.source.json").read_text()
            )
        serialized = json.dumps(tracked, sort_keys=True)
        self.assertNotIn(cookie, serialized)
        self.assertNotIn("secret-solved-pow-value", serialized)
        self.assertNotIn('"request_cookie":', serialized)
        sanitized = receipt["official_pdf_transport_receipt"]
        self.assertTrue(sanitized["request_cookie_present"])
        self.assertEqual(
            sanitized["request_cookie_sha256"],
            review.hashlib.sha256(cookie.encode("utf-8")).hexdigest(),
        )

    def test_cached_invalid_is_revalidated_newest_first_without_gateway(self):
        context_id = "1|Vitamin D|Prostate cancer"
        source_sha256 = "a" * 64
        unit_key = review._work_unit_key(
            "gemini_r3", [context_id], source_sha256
        )

        class MustNotCall:
            @property
            def chat(self):
                raise AssertionError("gateway must not be called")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            response_dir = output / "model_response_cache" / "gemini_r3"
            response_dir.mkdir(parents=True)
            for attempt, marker, status in (
                (1, "older", "invalid"),
                (2, "newest", "received"),
            ):
                output_text = json.dumps({"marker": marker})
                review.save_json(
                    response_dir / f"{unit_key}_{attempt:04d}.json",
                    {
                        "receipt_id": f"receipt-{attempt}",
                        "attempt": attempt,
                        "validation_status": status,
                        "validation_error": "legacy failure",
                        "scientific_result_accepted": False,
                        "output_text": output_text,
                        "output_sha256": review.hashlib.sha256(
                            output_text.encode("utf-8")
                        ).hexdigest(),
                        "response_cost_usd": f"0.{attempt}",
                    },
                )
            review.save_json(
                output / "gateway_receipts.json",
                {
                    "schema_version": 1,
                    "receipts": [
                        {
                            "receipt_id": f"receipt-{attempt}",
                            "received_at": f"2026-09-02T00:00:0{attempt}+08:00",
                            "stage": "gemini_r3",
                            "context_ids": [context_id],
                            "exposure_weights": {"Vitamin D": 1},
                            "usage": {},
                            "response_cost_usd": f"0.{attempt}",
                            "scientific_result_accepted": False,
                            "validation_status": status,
                            "validation_error": "legacy failure",
                            "output_sha256": "b" * 64,
                        }
                        for attempt, status in ((1, "invalid"), (2, "received"))
                    ],
                },
            )
            seen = []

            def validator(payload):
                seen.append(payload["marker"])
                return [{"context_id": context_id, "marker": payload["marker"]}]

            cost_path = root / "ClaudeGeminiCost.txt"
            with patch.object(review, "COST_PATH", cost_path):
                result = review._call_gateway_json(
                    MustNotCall(),
                    output_dir=output,
                    stage="gemini_r3",
                    model=review.GEMINI_R3_MODEL,
                    system="system",
                    prompt="prompt",
                    context_ids=[context_id],
                    exposure_weights={"Vitamin D": 1},
                    source_sha256=source_sha256,
                    validator=validator,
                    payload_normalizer=lambda payload: (
                        payload,
                        [{"event_type": "test_exact_repair"}],
                    ),
                )
            newest_path = response_dir / f"{unit_key}_0002.json"
            newest = json.loads(newest_path.read_text(encoding="utf-8"))
            receipts = json.loads(
                (output / "gateway_receipts.json").read_text(encoding="utf-8")
            )["receipts"]

        self.assertEqual(seen, ["newest"])
        self.assertEqual(result[0]["marker"], "newest")
        self.assertEqual(newest["validation_status"], "valid")
        self.assertEqual(newest["output_text"], json.dumps({"marker": "newest"}))
        self.assertEqual(newest["response_cost_usd"], "0.2")
        self.assertEqual(newest["validation_history"][0]["validation_status"], "received")
        self.assertEqual(
            newest["normalization_events"], [{"event_type": "test_exact_repair"}]
        )
        newest_receipt = next(item for item in receipts if item["receipt_id"] == "receipt-2")
        self.assertEqual(newest_receipt["response_cost_usd"], "0.2")
        self.assertEqual(
            newest_receipt["validation_history"][0]["validation_status"],
            "received",
        )

    def test_cached_revalidation_falls_back_to_older_attempt_before_gateway(self):
        context_id = "1|Vitamin D|Prostate cancer"
        source_sha256 = "c" * 64
        unit_key = review._work_unit_key("gemini_r3", [context_id], source_sha256)

        class MustNotCall:
            @property
            def chat(self):
                raise AssertionError("gateway must not be called")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            response_dir = output / "model_response_cache" / "gemini_r3"
            response_dir.mkdir(parents=True)
            for attempt, marker in ((1, "recoverable"), (2, "still-invalid")):
                output_text = json.dumps({"marker": marker})
                review.save_json(
                    response_dir / f"{unit_key}_{attempt:04d}.json",
                    {
                        "attempt": attempt,
                        "validation_status": "invalid",
                        "validation_error": "legacy failure",
                        "output_text": output_text,
                        "output_sha256": review.hashlib.sha256(
                            output_text.encode("utf-8")
                        ).hexdigest(),
                    },
                )
            seen = []

            def validator(payload):
                seen.append(payload["marker"])
                if payload["marker"] == "still-invalid":
                    raise ValueError("still invalid")
                return [{"context_id": context_id}]

            result = review._call_gateway_json(
                MustNotCall(),
                output_dir=output,
                stage="gemini_r3",
                model=review.GEMINI_R3_MODEL,
                system="system",
                prompt="prompt",
                context_ids=[context_id],
                exposure_weights={"Vitamin D": 1},
                source_sha256=source_sha256,
                validator=validator,
                payload_normalizer=lambda payload: (
                    payload,
                    [{"event_type": f"normalized-{payload['marker']}"}],
                ),
            )
            newest = json.loads(
                (response_dir / f"{unit_key}_0002.json").read_text(encoding="utf-8")
            )

        self.assertEqual(seen, ["still-invalid", "recoverable"])
        self.assertEqual(result, [{"context_id": context_id}])
        self.assertEqual(newest["validation_status"], "invalid")
        self.assertEqual(len(newest["validation_history"]), 1)
        self.assertEqual(
            newest["normalization_events"],
            [{"event_type": "normalized-still-invalid"}],
        )

    def test_validation_transitions_append_without_mutating_prior_history(self):
        raw_text = '{"raw":"provider output"}'
        raw_digest = review.hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        record = {
            "validation_status": "received",
            "validation_error": "",
            "scientific_result_accepted": False,
            "output_text": raw_text,
            "output_sha256": raw_digest,
            "response_cost_usd": "1.25",
        }
        review._transition_cached_validation(
            record,
            validation_status="invalid",
            validation_error="first validation failure",
            normalization_events=[{"event_type": "first"}],
            transition="first_validation",
        )
        first_history = json.loads(json.dumps(record["validation_history"][0]))
        review._transition_cached_validation(
            record,
            validation_status="valid",
            validated_results=[{"context_id": "1|Vitamin D|Prostate cancer"}],
            normalization_events=[{"event_type": "second"}],
            transition="revalidation",
        )
        self.assertEqual(record["validation_history"][0], first_history)
        self.assertEqual(
            [item["validation_status"] for item in record["validation_history"]],
            ["received", "invalid"],
        )
        self.assertEqual(record["output_text"], raw_text)
        self.assertEqual(record["output_sha256"], raw_digest)
        self.assertEqual(record["response_cost_usd"], "1.25")

    def test_legacy_acceptance_backfill_changes_only_boolean_and_is_idempotent(self):
        context_id = "1|Vitamin B6|Prostate cancer"
        output_text = json.dumps({"results": [{"context_id": context_id}]})
        output_sha256 = review.hashlib.sha256(output_text.encode()).hexdigest()
        shared = {
            "receipt_id": "receipt-1",
            "stage": "claude_r2",
            "model_requested": review.CLAUDE_R2_MODEL,
            "work_unit_key": "unit-1",
            "attempt": 1,
            "context_ids": [context_id],
            "input_digest": "input-digest",
            "output_sha256": output_sha256,
            "response_cost_usd": "1.25",
            "validation_status": "valid",
            "scientific_result_accepted": False,
        }
        cache = {
            **shared,
            "output_text": output_text,
            "validated_results": [
                {"context_id": context_id, "eligibility": "INCLUDE"}
            ],
            "validation_history": [{"immutable": "history"}],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        receipt = {
            **shared,
            "scientific_result_accepted": True,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            cache_path = (
                output
                / "model_response_cache"
                / "claude_r2"
                / "unit-1_0001.json"
            )
            review.save_json(cache_path, cache)
            review.save_json(
                output / "gateway_receipts.json",
                {"schema_version": 1, "receipts": [receipt]},
            )
            receipt_bytes_before = (output / "gateway_receipts.json").read_bytes()
            protected_before = review._model_cache_protected_digest(cache)

            dry_run = review.reconcile_model_cache_acceptance(
                output, write=False
            )
            self.assertEqual(dry_run["remaining_acceptance_mismatches"], 1)
            self.assertFalse(
                json.loads(cache_path.read_text())["scientific_result_accepted"]
            )
            self.assertFalse(
                (output / "model_cache_acceptance_reconciliation.json").exists()
            )

            applied = review.reconcile_model_cache_acceptance(output, write=True)
            updated = json.loads(cache_path.read_text())
            audit_path = output / "model_cache_acceptance_reconciliation.json"
            audit_bytes = audit_path.read_bytes()
            cache_bytes = cache_path.read_bytes()
            receipt_bytes_after = (output / "gateway_receipts.json").read_bytes()
            second = review.reconcile_model_cache_acceptance(output, write=True)
            cache_bytes_after_second = cache_path.read_bytes()
            audit_bytes_after_second = audit_path.read_bytes()

        self.assertEqual(applied["reconciled_cache_files"], 1)
        self.assertEqual(applied["reconciled_by_stage"], {"claude_r2": 1})
        self.assertEqual(applied["remaining_acceptance_mismatches"], 0)
        self.assertTrue(updated["scientific_result_accepted"])
        self.assertEqual(
            review._model_cache_protected_digest(updated), protected_before
        )
        self.assertEqual(receipt_bytes_before, receipt_bytes_after)
        self.assertEqual(second["reconciled_cache_files"], 0)
        self.assertEqual(cache_bytes, cache_bytes_after_second)
        self.assertEqual(audit_bytes, audit_bytes_after_second)
        audit = json.loads(audit_bytes)
        self.assertEqual(audit["cumulative_reconciled_cache_files"], 1)
        event = audit["latest_run"]["reconciliation_records"][0]
        self.assertEqual(event["output_sha256"], output_sha256)
        self.assertFalse(audit["latest_run"]["scientific_decisions_changed"])
        for protected_field in (
            "output_text",
            "output_sha256",
            "validated_results",
            "response_cost_usd",
            "usage",
            "validation_history",
        ):
            self.assertEqual(updated[protected_field], cache[protected_field])

    def test_acceptance_reconciliation_fails_closed_before_any_unsafe_write(self):
        context_id = "1|Vitamin B6|Prostate cancer"
        output_text = '{"valid":true}'
        output_sha256 = review.hashlib.sha256(output_text.encode()).hexdigest()
        shared = {
            "receipt_id": "receipt-1",
            "stage": "claude_r2",
            "model_requested": review.CLAUDE_R2_MODEL,
            "work_unit_key": "unit-1",
            "attempt": 1,
            "context_ids": [context_id],
            "input_digest": "input-digest",
            "output_sha256": output_sha256,
            "response_cost_usd": "1.25",
            "validation_status": "valid",
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            cache_path = (
                output
                / "model_response_cache"
                / "claude_r2"
                / "unit-1_0001.json"
            )
            review.save_json(
                cache_path,
                {
                    **shared,
                    "scientific_result_accepted": False,
                    "output_text": output_text + "tampered",
                    "validated_results": [{"context_id": context_id}],
                },
            )
            review.save_json(
                output / "gateway_receipts.json",
                {
                    "schema_version": 1,
                    "receipts": [
                        {**shared, "scientific_result_accepted": True}
                    ],
                },
            )
            before = cache_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "failed closed"):
                review.reconcile_model_cache_acceptance(output, write=True)
            self.assertEqual(cache_path.read_bytes(), before)
            self.assertFalse(
                (output / "model_cache_acceptance_reconciliation.json").exists()
            )

    def test_cached_raw_output_digest_mismatch_cannot_be_salvaged(self):
        with self.assertRaisesRegex(
            dual_screening.ScreeningValidationError,
            "SHA-256 does not match",
        ):
            review._verify_cached_raw_output(
                {
                    "output_text": '{"tampered":true}',
                    "output_sha256": "0" * 64,
                }
            )

    def test_pmid_21518693_legacy_cache_is_bypassed_for_fresh_adjudication(self):
        context_id = "21518693|Omega-3 fatty acids|Prostate cancer"
        source_sha256 = "d" * 64
        unit_key = review._work_unit_key("gemini_r3", [context_id], source_sha256)

        class RawResponse:
            headers = {"x-litellm-response-cost": "0.5"}

            def parse(self):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({"marker": "fresh"})
                            )
                        )
                    ],
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    model=review.GEMINI_R3_MODEL,
                )

        class RawAPI:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                return RawResponse()

        raw_api = RawAPI()
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(with_raw_response=raw_api)
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            response_dir = output / "model_response_cache" / "gemini_r3"
            response_dir.mkdir(parents=True)
            legacy_text = json.dumps({"marker": "legacy-cache"})
            legacy_path = response_dir / f"{unit_key}_0001.json"
            review.save_json(
                legacy_path,
                {
                    "attempt": 1,
                    "validation_status": "invalid",
                    "validation_error": "abstract source label",
                    "output_text": legacy_text,
                    "output_sha256": review.hashlib.sha256(
                        legacy_text.encode("utf-8")
                    ).hexdigest(),
                },
            )
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                result = review._call_gateway_json(
                    client,
                    output_dir=output,
                    stage="gemini_r3",
                    model=review.GEMINI_R3_MODEL,
                    system="system",
                    prompt="prompt",
                    context_ids=[context_id],
                    exposure_weights={"Omega-3 fatty acids": 1},
                    source_sha256=source_sha256,
                    validator=lambda payload: [
                        {"context_id": context_id, "marker": payload["marker"]}
                    ],
                    force_fresh_cached_adjudication=True,
                )
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            fresh = json.loads(
                (response_dir / f"{unit_key}_0002.json").read_text(encoding="utf-8")
            )

        self.assertEqual(raw_api.calls, 1)
        self.assertEqual(result[0]["marker"], "fresh")
        self.assertEqual(legacy["output_text"], legacy_text)
        self.assertEqual(
            legacy["cache_revalidation_history"][0]["disposition"],
            "forced_fresh_adjudication_required",
        )
        self.assertEqual(fresh["validation_status"], "valid")
        self.assertEqual(
            fresh["validation_contract_version"],
            review.R3_CACHE_RECOVERY_CONTRACT_VERSION,
        )

    def test_dense_r2_and_r3_contexts_are_split_deterministically(self):
        expected = {
            f"1|Exposure {index:02d}|Prostate cancer": {
                "pmid": "1",
                "exposure": f"Exposure {index:02d}",
            }
            for index in range(25)
        }
        articles = {"1": {"pmid": "1"}}
        units = review._make_r2_units(
            expected, articles, set(), batch_size=1
        )
        self.assertEqual([len(unit["context_ids"]) for unit in units], [12, 12, 1])
        self.assertEqual(
            units,
            review._make_r2_units(expected, articles, set(), batch_size=1),
        )
        completed = set(units[0]["context_ids"])
        remaining = review._make_r2_units(
            expected, articles, completed, batch_size=1
        )
        self.assertEqual(sum(len(unit["context_ids"]) for unit in remaining), 13)

        gated = {
            context_id: {
                "context_id": context_id,
                "pmid": "1",
                "pmcid": "pmc123",
            }
            for context_id in expected
        }
        r3_units = review._make_r3_units(gated, set())
        self.assertEqual([len(unit["context_ids"]) for unit in r3_units], [8, 8, 8, 1])
        self.assertTrue(all(unit["pmcid"] == "PMC123" for unit in r3_units))

    def test_blank_titles_are_repaired_from_official_pubmed_without_requiring_optional_fields(self):
        parsed_book = review.monthly_evidence_update.book_article_record(
            {
                "BookDocument": {
                    "PMID": "123",
                    "ArticleTitle": "",
                    "Book": {"BookTitle": "Official indexed book title"},
                },
                "PubmedBookData": {},
            }
        )
        self.assertEqual(parsed_book["title"], "Official indexed book title")
        candidates = {
            "articles": [
                {
                    "pmid": "123",
                    "title": "",
                    "abstract": "legacy abstract",
                    "mesh": ["legacy"],
                    "candidate_contexts": [],
                }
            ]
        }
        official = {
            "123": {
                "pmid": "123",
                "title": "Official PubMed title",
                "abstract": "",
                "mesh": [],
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(
                review.monthly_evidence_update, "configure_entrez"
            ), patch.object(
                review.monthly_evidence_update,
                "entrez_fetch",
                return_value=(official, 1),
            ) as fetch:
                repaired, receipt = review._r2_candidates_with_official_metadata(
                    output, candidates
                )
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(repaired["articles"][0]["title"], "Official PubMed title")
            self.assertEqual(repaired["articles"][0]["abstract"], "legacy abstract")
            self.assertEqual(repaired["articles"][0]["mesh"], ["legacy"])
            self.assertEqual(
                receipt["repairs"][0]["field_provenance"]["abstract"],
                "preserved_existing_nonblank",
            )
            self.assertEqual(
                receipt["repairs"][0]["officially_absent_optional_fields"],
                ["abstract", "mesh"],
            )
            with patch.object(
                review.monthly_evidence_update,
                "entrez_fetch",
                side_effect=AssertionError("cached repair must be reused"),
            ):
                cached, cached_receipt = review._r2_candidates_with_official_metadata(
                    output, candidates
                )
            self.assertEqual(cached, repaired)
            self.assertEqual(cached_receipt, receipt)

    def test_split_pmcid_units_share_one_exact_canonical_source_proof(self):
        text = "Complete official article text. " * 220
        digest = review.hashlib.sha256(text.encode("utf-8")).hexdigest()
        base = {
            "schema_version": 1,
            "pmcid": "PMC123",
            "pmid": "123",
            "status": "verified_full_text",
            "source_route": "efetch",
            "source_format": "jats_xml",
            "identity_verified": True,
            "direct_article_body_verified": True,
            "minimum_characters_required": 5000,
            "characters": len(text),
            "raw_sha256": "a" * 64,
            "text_sha256": digest,
        }
        first_receipt = review._finalize_source_receipt(
            {**base, "retrieved_at": "first"}
        )
        equivalent_receipt = review._finalize_source_receipt(
            {**base, "retrieved_at": "second", "raw_sha256": "b" * 64}
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            first = review._register_r3_source(
                output, "PMC123", "123", text, first_receipt
            )
            second = review._register_r3_source(
                output, "pmc123", "123", text, equivalent_receipt
            )
        self.assertEqual(second, first)
        row = {
            "context_id": "123|Vitamin B6|Prostate cancer",
            "pmcid": "PMC123",
            **review._r3_source_binding(first),
        }
        review._validate_r3_source_proofs(
            [row], {"PMC123": first}, exact_coverage=True
        )
        changed_text = text + "scientific change"
        changed_receipt = review._finalize_source_receipt(
            {
                **base,
                "retrieved_at": "third",
                "characters": len(changed_text),
                "text_sha256": review.hashlib.sha256(
                    changed_text.encode("utf-8")
                ).hexdigest(),
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            review._register_r3_source(
                output, "PMC123", "123", text, first_receipt
            )
            with self.assertRaises(review.OperationalProblem):
                review._register_r3_source(
                    output, "PMC123", "123", changed_text, changed_receipt
                )

    def test_invalid_model_response_receipt_and_cost_are_retained(self):
        class RawResponse:
            def __init__(self, content, cost, request_id):
                self.headers = {
                    "x-request-id": request_id,
                    "x-litellm-response-cost": cost,
                }
                self.content = content
                self.request_id = request_id

            def parse(self):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=self.content)
                        )
                    ],
                    usage=SimpleNamespace(input_tokens=10, output_tokens=2),
                    model=review.CLAUDE_R2_MODEL,
                    _request_id=self.request_id,
                )

        class RawAPI:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return RawResponse("{}", "0.10", "invalid-response")
                return RawResponse('{"accepted":true}', "0.20", "valid-response")

        raw_api = RawAPI()
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(with_raw_response=raw_api)
            )
        )

        def validator(payload):
            if payload != {"accepted": True}:
                raise ValueError("not accepted")
            return [{"context_id": "1|Vitamin B6|Prostate cancer"}]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            output.mkdir()
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                results = review._call_gateway_json(
                    client,
                    output_dir=output,
                    stage="claude_r2",
                    model=review.CLAUDE_R2_MODEL,
                    system="system",
                    prompt="prompt",
                    context_ids=["1|Vitamin B6|Prostate cancer"],
                    exposure_weights={"Vitamin B6": 1},
                    validator=validator,
                )
                costs = json.loads(
                    (output / "cost_accounting.json").read_text(encoding="utf-8")
                )
            receipts = json.loads(
                (output / "gateway_receipts.json").read_text(encoding="utf-8")
            )["receipts"]
        self.assertEqual(results[0]["context_id"], "1|Vitamin B6|Prostate cancer")
        self.assertEqual(len(receipts), 2)
        self.assertEqual(
            [receipt["scientific_result_accepted"] for receipt in receipts],
            [False, True],
        )
        row = next(item for item in costs["rows"] if item["exposure"] == "Vitamin B6")
        self.assertEqual(row["claude_r2_calls"], 2)
        self.assertEqual(row["claude_r2_cost_usd"], "0.3000000000")
        self.assertEqual(
            costs["invalid_or_unaccepted_receipts_retained_for_cost"], 1
        )

    def test_jats_preformat_ocr_is_preserved_as_complete_body_text(self):
        ocr = "Historical full article OCR sentence. " * 220
        xml = (
            '<article><front><article-meta>'
            '<article-id pub-id-type="pmc">PMC123</article-id>'
            '<article-id pub-id-type="pmid">123</article-id>'
            '<title-group><article-title>Historical article</article-title></title-group>'
            '</article-meta></front><body><preformat>'
            + ocr
            + "</preformat></body></article>"
        ).encode("utf-8")
        validation, text = audit_blocked_pmc_sources.validate_jats_receipt(
            xml, expected_pmcid="PMC123", expected_pmid="123"
        )
        self.assertTrue(validation["valid"], validation)
        self.assertEqual(validation["body_paragraphs"], 0)
        self.assertEqual(validation["body_preformats"], 1)
        self.assertIn("Historical full article OCR sentence", text)

    def test_catalog_union_and_cost_file_have_exactly_one_row_per_exposure(self):
        exposures = review.catalog_exposures()
        self.assertEqual(len(exposures), 446)
        self.assertEqual(len(set(exposures)), 446)
        self.assertNotIn("Diet pills", exposures)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cost_path = root / "ClaudeGeminiCost.txt"
            with patch.object(review, "COST_PATH", cost_path):
                review._write_cost_files(root / "output")
            rows = cost_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 446)
        self.assertEqual([row.split("\t", 1)[0] for row in rows], exposures)
        self.assertTrue(all("claude_r2_calls=0" in row for row in rows))
        self.assertTrue(all("gemini_r3_calls=0" in row for row in rows))

    def test_prepare_resolves_all_ten_decision_conflicts_by_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                manifest = review.prepare_r1_lineage(root / "parallel")
        self.assertEqual(manifest["catalog_exposure_count"], 446)
        self.assertTrue(manifest["source_scope_union_matches_current_catalogs"])
        self.assertEqual(manifest["source_scope_union_exposures"], 446)
        self.assertEqual(len(manifest["exposure_dispositions"]), 446)
        self.assertEqual(
            sum(manifest["exposure_disposition_summary"].values()), 446
        )
        self.assertEqual(manifest["decision_conflict_count"], 10)
        self.assertEqual(manifest["superseded_source_decision_contexts"], 38)
        self.assertEqual(manifest["selected_pass"], 6314)
        resolutions = {
            item["context_id"]: (item["selected_source"], item["selected_decision"])
            for item in manifest["decision_conflicts"]
        }
        self.assertEqual(
            resolutions["40759593|Probiotics|Prostate cancer"],
            ("2026-07-01_2026-07-31", "PASS"),
        )
        self.assertEqual(
            resolutions["15824168|Vitamin B6|Prostate cancer"],
            (
                "vitamin_b6_plp_diet_v2_all_history_through_2026-08-29",
                "FAIL",
            ),
        )
        self.assertEqual(
            resolutions["19005974|Mushrooms|Prostate cancer"],
            ("all_history_through_2026-08-29", "FAIL"),
        )

    def test_r2_prompt_article_payload_has_only_allowed_metadata(self):
        articles = [
            {
                "pmid": "1",
                "title": "Title",
                "abstract": "Abstract",
                "mesh": ["Term"],
                "authors": ["Must Not Be Sent"],
                "journal": "Must Not Be Sent",
                "candidate_contexts": [],
            }
        ]
        expected = {
            "1|Vitamin B6|Prostate cancer": {
                "pmid": "1",
                "exposure": "Vitamin B6",
                "cancer": "Prostate cancer",
            }
        }
        prompt = review._r2_prompt(articles, expected)
        payload = json.loads(prompt.split("ARTICLES:\n", 1)[1])
        self.assertEqual(
            set(payload[0]), {"pmid", "title", "abstract", "mesh", "contexts"}
        )
        self.assertNotIn("Must Not Be Sent", prompt)

    def test_parallel_r3_gate_ignores_ambiguity_alone_and_requires_pmcid(self):
        complete = {
            "effect_size": 1.1,
            "effect_type": "RR",
            "ci_lower": 0.9,
            "ci_upper": 1.3,
            "p_value": 0.5,
            "total_n": 100,
            "cases": 10,
            "comparison_type": "high versus low",
            "exposure_classification_ambiguous": True,
        }
        ambiguous_only = {
            "eligibility": "INCLUDE",
            "pmcid": "PMC1",
            "needs_full_text": True,
            "extraction": complete,
            "jbi": {"grade": "Moderate"},
        }
        result = review.apply_parallel_r3_gate(ambiguous_only)
        self.assertEqual(result["parallel_r3_gate_reasons"], [])
        self.assertFalse(result["needs_full_text"])
        self.assertFalse(result["full_text_eligible"])

        fair = {**ambiguous_only, "jbi": {"grade": "Fair"}}
        result = review.apply_parallel_r3_gate(fair)
        self.assertTrue(result["needs_full_text"])
        self.assertTrue(result["full_text_eligible"])

        missing_without_pmc = {
            **ambiguous_only,
            "pmcid": "",
            "extraction": {**complete, "total_n": None},
            "jbi": {"grade": "Moderate"},
        }
        result = review.apply_parallel_r3_gate(missing_without_pmc)
        self.assertTrue(result["needs_full_text"])
        self.assertFalse(result["full_text_eligible"])

    def test_wald_completion_precedes_parallel_gate(self):
        extraction = {
            "effect_size": 2.0,
            "effect_type": "RR",
            "ci_lower": 1.2,
            "ci_upper": 3.3,
            "p_value": None,
            "total_n": 100,
            "cases": 20,
            "comparison_type": "high versus low",
            "wald_inputs_same_estimate": True,
            "p_value_is_exact": False,
            "ci_confidence_level": 95,
        }
        derived_ci, derived_p = dual_screening.add_wald_derivations(extraction)
        self.assertFalse(derived_ci)
        self.assertTrue(derived_p)
        item = {
            "eligibility": "INCLUDE",
            "pmcid": "PMC1",
            "extraction": extraction,
            "jbi": {"grade": "Moderate"},
        }
        self.assertEqual(review.r3_gate_reasons(item), [])

    def test_parallel_policy_accepts_optional_fields_and_marks_default_direction(self):
        extraction = {
            "effect_size": 1.2,
            "effect_type": "RR",
            "ci_lower": 0.9,
            "ci_upper": 1.6,
            "total_n": 100,
            "cases": None,
            "p_value": None,
            "comparison_type": None,
            "exposure_measurement_type": None,
            "exposure_classification_ambiguous": True,
        }
        item = {
            "eligibility": "INCLUDE",
            "pmcid": "PMC1",
            "extraction": extraction,
            "jbi": {"grade": "Moderate"},
            "missing_key_fields": ["cases", "p_value", "comparison_type"],
        }
        result = review.apply_parallel_r3_gate(item)
        self.assertFalse(result["needs_full_text"])
        self.assertEqual(result["missing_key_fields"], [])
        self.assertTrue(result["extraction"]["direction_imputed"])
        self.assertTrue(dual_screening.is_poolable(result["extraction"], result["jbi"]))
        self.assertNotIn("direction_imputed", extraction)

    def test_operational_problem_is_not_written_as_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            output.mkdir()
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                review._record_problem(
                    output,
                    stage="claude_r2",
                    model=review.CLAUDE_R2_MODEL,
                    work_unit_key="unit",
                    context_ids=["1|Vitamin B6|Prostate cancer"],
                    error="gateway timeout Authorization: secret",
                )
                review.write_outputs_and_state(output, stopped_reason="test")
            exclusions = json.loads(
                (output / "exclusions.json").read_text(encoding="utf-8")
            )
            errors = json.loads(
                (output / "claude_r2_errors.json").read_text(encoding="utf-8")
            )
        self.assertEqual(exclusions["exclusions"], [])
        self.assertEqual(errors["unresolved_count"], 1)
        self.assertNotIn("secret", json.dumps(errors))

    def test_bounded_r2_fake_gateway_writes_separate_result_receipt_and_cost(self):
        class RawResponse:
            headers = {
                "x-request-id": "request-1",
                "x-litellm-response-cost": "0.25",
                "authorization": "must-not-be-saved",
            }

            def parse(self):
                payload = {
                    "results": [
                        {
                            "context_id": "1|Vitamin B6|Prostate cancer",
                            "eligibility": "EXCLUDE",
                            "exclusion_code": "S2-WRONG-OUTCOME",
                            "reason": "The report does not evaluate incident prostate cancer.",
                            "extraction": None,
                            "jbi": None,
                        }
                    ]
                }
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=json.dumps(payload))
                        )
                    ],
                    usage=SimpleNamespace(
                        input_tokens=100,
                        output_tokens=20,
                        input_tokens_details=SimpleNamespace(cached_tokens=10),
                        output_tokens_details=SimpleNamespace(reasoning_tokens=5),
                    ),
                    model=review.CLAUDE_R2_MODEL,
                    _request_id="request-1",
                )

        class RawAPI:
            calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return RawResponse()

        raw_api = RawAPI()
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(with_raw_response=raw_api)
            )
        )
        candidates = {
            "schema_version": 1,
            "publication_date_end": review.PUBLICATION_DATE_END,
            "articles": [
                {
                    "pmid": "1",
                    "pmcid": "",
                    "title": "A non-incidence report",
                    "abstract": "This report has no incidence outcome.",
                    "mesh": [],
                    "candidate_contexts": [
                        {
                            "exposure": "Vitamin B6",
                            "cancer": "Prostate cancer",
                            "outcome": "Incidence",
                        }
                    ],
                }
            ],
        }
        first = {
            "input_digest": "first-digest",
            "pass": 1,
            "results": [
                {
                    "context_id": "1|Vitamin B6|Prostate cancer",
                    "pmid": "1",
                    "exposure": "Vitamin B6",
                    "cancer": "Prostate cancer",
                    "decision": "PASS",
                    "reason": "The context passed R1.",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            output.mkdir()
            review.save_json(output / "r1_pass_candidates.json", candidates)
            review.save_json(output / "r1_pass_packet.json", first)
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                packet = review.run_r2(
                    output,
                    batch_size=1,
                    workers=1,
                    max_units=1,
                    client=fake_client,
                )
                cost_rows = (root / "ClaudeGeminiCost.txt").read_text(
                    encoding="utf-8"
                ).splitlines()
            receipt = json.loads(
                (output / "gateway_receipts.json").read_text(encoding="utf-8")
            )
            exclusions = json.loads(
                (output / "claude_r2_exclusions.json").read_text(encoding="utf-8")
            )
            cache_files = list(
                (output / "model_response_cache" / "claude_r2").glob("*.json")
            )
            self.assertEqual(len(cache_files), 1)
            cache_record = json.loads(cache_files[0].read_text(encoding="utf-8"))
        self.assertEqual(packet["status"], "complete")
        self.assertEqual(receipt["receipt_count"], 1)
        self.assertTrue(receipt["receipts"][0]["scientific_result_accepted"])
        self.assertTrue(cache_record["scientific_result_accepted"])
        self.assertEqual(receipt["receipts"][0]["api_style"], "chat_completions")
        self.assertNotIn("authorization", receipt["receipts"][0]["safe_gateway_headers"])
        self.assertEqual(exclusions["exclusions"][0]["reason"], "The report does not evaluate incident prostate cancer.")
        vitamin_b6 = next(row for row in cost_rows if row.startswith("Vitamin B6\t"))
        self.assertIn("claude_r2_input_tokens=100", vitamin_b6)
        self.assertIn("claude_r2_cost_usd=0.2500000000", vitamin_b6)
        self.assertEqual(len(raw_api.calls), 1)
        self.assertIn("messages", raw_api.calls[0])
        self.assertNotIn("response_format", raw_api.calls[0])
        self.assertNotIn("text", raw_api.calls[0])

    def test_invalid_cost_headers_are_unavailable_not_numeric(self):
        for raw in ("NaN", "Infinity", "-0.01", "not-a-number"):
            value, status = review._validated_gateway_cost(
                {"x-litellm-response-cost": raw}
            )
            self.assertIsNone(value)
            self.assertTrue(status.startswith("unavailable_"))
        self.assertEqual(
            review._validated_gateway_cost({"x-litellm-response-cost": "0.125"}),
            ("0.125", "validated_finite_nonnegative_gateway_header"),
        )

    def test_dispatch_is_worker_bounded_and_does_not_swallow_keyboard_interrupt(self):
        active_counts = []
        completed = []

        def process(unit):
            return str(unit), [{"context_id": str(unit)}]

        review._run_units(
            list(range(7)),
            process,
            lambda key, _results: completed.append(key),
            lambda _unit, error: self.fail(f"unexpected error: {error}"),
            workers=2,
            on_active=lambda values: active_counts.append(len(values)),
        )
        self.assertEqual(sorted(completed), [str(value) for value in range(7)])
        self.assertLessEqual(max(active_counts), 2)

        interrupted_active = []
        with self.assertRaises(KeyboardInterrupt):
            review._run_units(
                [1],
                lambda _unit: (_ for _ in ()).throw(KeyboardInterrupt()),
                lambda _key, _results: None,
                lambda _unit, _error: self.fail("KeyboardInterrupt was swallowed"),
                workers=1,
                on_active=lambda values: interrupted_active.append(list(values)),
            )
        self.assertEqual(interrupted_active[-1], [1])

    def test_stale_active_dispatch_is_decision_free_and_cost_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            output.mkdir()
            context_id = "1|Vitamin B6|Prostate cancer"
            review.save_json(
                output / "active_dispatch.json",
                {
                    "schema_version": 2,
                    "stage": "claude_r2",
                    "model": review.CLAUDE_R2_MODEL,
                    "api_style": "chat_completions",
                    "process_id": 99999999,
                    "dispatched_at": "2026-09-02T00:00:00+08:00",
                    "active_units": [
                        {
                            "pmids": ["1"],
                            "context_ids": [context_id],
                            "work_unit_key": "interrupted-unit",
                        }
                    ],
                },
            )
            cost_path = root / "ClaudeGeminiCost.txt"
            with patch.object(review, "COST_PATH", cost_path):
                recovered = review._recover_interrupted_active_dispatch(output)
            self.assertEqual(recovered, 1)
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(encoding="utf-8")
            )
            self.assertEqual(ledger["unresolved_count"], 1)
            problem = ledger["unresolved"][0]
            self.assertFalse(problem["scientific_disposition_created"])
            self.assertEqual(
                problem["response_receipt_status"],
                "unknown_after_process_interruption",
            )
            self.assertEqual(
                problem["billing_status"], "unknown_after_process_interruption"
            )
            receipts = json.loads(
                (output / "gateway_receipts.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipts["receipt_count"], 1)
            self.assertEqual(
                receipts["receipts"][0]["token_provenance"],
                "unavailable_after_process_interruption",
            )
            row = next(
                value
                for value in cost_path.read_text(encoding="utf-8").splitlines()
                if value.startswith("Vitamin B6\t")
            )
            self.assertIn("claude_r2_cost_usd=NA", row)
            self.assertIn("claude_r2_unknown_billing_calls=1", row)
            self.assertIn(
                "claude_r2_token_provenance=unavailable_after_process_interruption",
                row,
            )
            active = json.loads(
                (output / "active_dispatch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(active["active_units"], [])
            self.assertEqual(active["recovery_history"][0]["recovered_problem_units"], 1)

            review._resolve_problems_by_completed_contexts(
                output,
                stage="claude_r2",
                completed_context_ids=[context_id],
            )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(encoding="utf-8")
            )
            self.assertEqual(ledger["unresolved_count"], 0)
            self.assertEqual(ledger["events"][-1]["event"], "resolved")
            self.assertEqual(
                ledger["events"][-1]["response_receipt_status"],
                "unknown_after_process_interruption",
            )

    def test_live_active_dispatch_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            active = {
                "schema_version": 2,
                "stage": "claude_r2",
                "process_id": review.os.getpid(),
                "active_units": [
                    {
                        "context_ids": ["1|Vitamin B6|Prostate cancer"],
                        "work_unit_key": "live-unit",
                    }
                ],
            }
            review.save_json(output / "active_dispatch.json", active)
            self.assertEqual(review._recover_interrupted_active_dispatch(output), 0)
            self.assertEqual(
                review.load_json(output / "active_dispatch.json", {}), active
            )

    def test_problem_resolution_uses_completed_context_coverage_not_batch_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            contexts = [
                "1|Vitamin B6|Prostate cancer",
                "2|Vitamin B6|Prostate cancer",
            ]
            review._record_problem(
                output,
                stage="claude_r2",
                model=review.CLAUDE_R2_MODEL,
                work_unit_key="old-two-context-batch",
                context_ids=contexts,
                error="timeout",
            )
            review._resolve_problems_by_completed_contexts(
                output,
                stage="claude_r2",
                completed_context_ids=contexts,
            )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(encoding="utf-8")
            )
        self.assertEqual(ledger["unresolved_count"], 0)
        self.assertEqual(ledger["events"][-1]["event"], "resolved")

    def test_science_gate_without_pmcid_is_decision_free_source_deferral(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            output.mkdir()
            r1 = {"input_digest": "r1", "pass": 1}
            repair = {
                "status": "complete",
                "r2_input_candidates_digest": "repaired-input",
            }
            r2 = {
                "source_first_input_digest": "r1",
                "source_r2_input_digest": review.stable_digest(
                    {
                        "source_first_input_digest": "r1",
                        "r2_input_candidates_digest": "repaired-input",
                    }
                ),
                "results": [
                    {
                        "context_id": "1|Vitamin B6|Prostate cancer",
                        "pmid": "1",
                        "pmcid": "",
                        "exposure": "Vitamin B6",
                        "cancer": "Prostate cancer",
                        "eligibility": "INCLUDE",
                        "needs_full_text": True,
                        "full_text_eligible": False,
                        "parallel_r3_gate_reasons": ["JBI remains Fair after Claude R2"],
                    }
                ],
            }
            review.save_json(output / "r1_pass_packet.json", r1)
            review.save_json(output / "r2_official_metadata_repair.json", repair)
            review.save_json(output / "claude_r2_results.json", r2)
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                state = review.write_outputs_and_state(
                    output, stopped_reason="test"
                )
            deferrals = json.loads(
                (output / "r3_source_deferrals.json").read_text(encoding="utf-8")
            )
        self.assertEqual(state["status"], "complete_with_decision_free_source_deferrals")
        self.assertEqual(state["gemini_r3_decision_free_no_pmcid_deferrals"], 1)
        self.assertEqual(deferrals["deferrals"][0]["disposition"], "decision_free_deferred_no_pmcid")


if __name__ == "__main__":
    unittest.main()
