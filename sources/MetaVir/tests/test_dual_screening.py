import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app as webapp
from scripts import dual_screening as screening
from scripts import monthly_evidence_update as monthly


def jbi_answers(yes=6, total=11):
    answers = {}
    for index in range(1, 14):
        if index > total:
            answers[f"q{index}"] = "NA"
        else:
            answers[f"q{index}"] = "Yes" if index <= yes else "No"
    return {"checklist_type": "cohort", "answers": answers}


def evidence_entry(excerpt, source="abstract", url="https://pubmed.ncbi.nlm.nih.gov/123/"):
    return {
        "excerpt": excerpt,
        "source": source,
        "url": url,
        "location": "Abstract results",
    }


def extraction(*, country="Sweden", cases=132, subtype="whole_food"):
    evidence = {field: None for field in screening.EVIDENCE_FIELDS}
    evidence.update(
        {
            "sample_size": evidence_entry("The cohort included 1,000 participants."),
            "cases": evidence_entry("During follow-up, 132 prostate cancers occurred."),
            "effect_size": evidence_entry("The adjusted hazard ratio was 0.82."),
            "confidence_interval": evidence_entry("The 95% confidence interval was 0.70 to 0.96."),
            "p_value": evidence_entry("The two-sided p value for this hazard ratio was 0.03."),
            "comparison": evidence_entry("Highest versus lowest tomato intake."),
            "design": evidence_entry("We conducted a prospective cohort study."),
            "timing": evidence_entry("Exposure was assessed before diagnosis."),
            "continent": evidence_entry("The study was conducted in Europe."),
            "ethnicity": None,
            "location": evidence_entry("Participants were recruited in Sweden."),
            "exposure_measurement": evidence_entry("Diet was measured using a food-frequency questionnaire."),
        }
    )
    if cases is None:
        evidence["cases"] = None
    return {
        "effect_size": 0.82,
        "effect_type": "HR",
        "ci_lower": 0.70,
        "ci_upper": 0.96,
        "p_value": 0.03,
        "ci_confidence_level": 95.0,
        "total_n": 1000,
        "cases": cases,
        "comparison_type": "Highest versus lowest",
        "design": "Prospective cohort",
        "timing": "Prediagnostic",
        "continent": "Europe",
        "country": country,
        "location": country if country else "NA",
        "ethnicity": "NA",
        "population": "Swedish adults" if country == "Sweden" else None,
        "exposure_subtype": subtype,
        "exposure_measurement_type": "dietary_intake",
        "exposure_classification_ambiguous": subtype == "mixed_or_unclear",
        "wald_inputs_same_estimate": True,
        "needs_inversion": False,
        "evidence": evidence,
    }


def candidate(title="Tomato intake and prostate cancer risk"):
    return {
        "schema_version": 1,
        "publication_date_start": "2026-07-01",
        "publication_date_end": "2026-07-31",
        "articles": [
            {
                "pmid": "123",
                "pmcid": "",
                "published_date": "2026-07-15",
                "title": title,
                "abstract": (
                    "A prospective study of tomato intake and incident prostate cancer. "
                    "We conducted a prospective cohort study. The cohort included 1,000 participants. "
                    "Diet was measured using a food-frequency questionnaire. Participants were recruited in Sweden. "
                    "Exposure was assessed before diagnosis. The study was conducted in Europe. "
                    "Highest versus lowest tomato intake. During follow-up, 132 prostate cancers occurred. "
                    "The adjusted hazard ratio was 0.82. The 95% confidence interval was 0.70 to 0.96. "
                    "The two-sided p value for this hazard ratio was 0.03."
                ),
                "mesh": ["Prostatic Neoplasms", "Solanum lycopersicum"],
                "authors": ["Example A"],
                "journal": "Example Journal",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/123/",
                "candidate_contexts": [
                    {
                        "exposure": "Tomato",
                        "cancer": "Prostate cancer",
                        "outcome": "Incidence",
                    }
                ],
            }
        ],
    }


def candidate_with_pmids(*pmids):
    packet = candidate()
    template = packet["articles"][0]
    packet["articles"] = []
    for pmid in pmids:
        article = copy.deepcopy(template)
        article["pmid"] = str(pmid)
        article["title"] = f"Tomato intake and prostate cancer risk {pmid}"
        article["pubmed_url"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        packet["articles"].append(article)
    return packet


def first_payload(pmid, decision="PASS"):
    return {
        "results": [
            {
                "context_id": f"{pmid}|Tomato|Prostate cancer",
                "decision": decision,
                "reason": "The article is relevant." if decision == "PASS" else "The article is not relevant.",
            }
        ]
    }


def second_exclude_payload(pmid):
    return {
        "results": [
            {
                "context_id": f"{pmid}|Tomato|Prostate cancer",
                "eligibility": "EXCLUDE",
                "exclusion_code": "S2-EXPOSURE-MISMATCH",
                "reason": "The requested exposure is not evaluated.",
                "extraction": None,
                "jbi": None,
            }
        ]
    }


class FakeResponses:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return SimpleNamespace(
            output_text=json.dumps(payload),
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


class FakeCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)
        message = SimpleNamespace(content=json.dumps(payload))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=6),
        )


def runtimes(gpt_payloads, deepseek_payloads):
    gpt_api = FakeResponses(gpt_payloads)
    deepseek_api = FakeCompletions(deepseek_payloads)
    return (
        {
            "chatgpt": screening.TrackRuntime(
                "chatgpt",
                SimpleNamespace(responses=gpt_api),
                screening.CHATGPT_FIRST_MODEL,
                screening.CHATGPT_SECOND_MODEL,
                screening.CHATGPT_FULL_TEXT_MODEL,
                "responses",
            ),
            "deepseek": screening.TrackRuntime(
                "deepseek",
                SimpleNamespace(chat=SimpleNamespace(completions=deepseek_api)),
                screening.DEEPSEEK_FIRST_MODEL,
                screening.DEEPSEEK_SECOND_MODEL,
                screening.DEEPSEEK_FULL_TEXT_MODEL,
                "chat_completions",
            ),
        },
        gpt_api,
        deepseek_api,
    )


class DualScreeningTests(unittest.TestCase):
    def test_one_sentence_treats_st_johns_as_an_abbreviation_only(self):
        reason = (
            "The abstract discusses St. John's wort as treatment rather than "
            "an exposure associated with incident prostate cancer."
        )
        self.assertEqual(screening.one_sentence(reason), reason)
        with self.assertRaisesRegex(
            screening.ScreeningValidationError,
            "multiple sentences",
        ):
            screening.one_sentence(
                "St. John's wort was evaluated as treatment. "
                "It was not studied as an incident cancer exposure."
            )

    def test_one_sentence_treats_vs_as_an_abbreviation(self):
        reason = (
            "The cohort reported a risk estimate for Q5 vs. Q1 in incident "
            "prostate cancer."
        )
        self.assertEqual(screening.one_sentence(reason), reason)

    def test_discovery_uses_core_and_explicit_downstream_terms(self):
        exposures = ["Tomato", "Ketogenic diet"]
        synonyms = {
            "Tomato": {"core": "tomato, tomatoes", "downstream": "lycopene, cis-lycopene"},
            "Ketogenic diet": {
                "core": "keto diet, modified Atkins diet",
                "downstream": "ketone bodies",
            },
        }

        def fake_load(path, _default):
            return exposures if path == monthly.EXPOSURES_FILE else synonyms

        with patch.object(monthly, "load_json", side_effect=fake_load):
            terms = monthly.exposure_terms()
        self.assertIn("lycopene", terms["Tomato"])
        self.assertIn("cis-lycopene", terms["Tomato"])
        self.assertIn("modified Atkins diet", terms["Ketogenic diet"])
        self.assertIn("ketone bodies", terms["Ketogenic diet"])

    def test_exposure_filter_is_repeatable_exact_and_catalog_ordered(self):
        selected = monthly.selected_exposure_terms(
            ["Vitamin K2", "Vitamin K1", "Vitamin K2"]
        )
        self.assertEqual(list(selected), ["Vitamin K1", "Vitamin K2"])
        with self.assertRaisesRegex(ValueError, "must exactly match"):
            monthly.selected_exposure_terms(["vitamin k1"])

        with patch(
            "sys.argv",
            [
                "monthly_evidence_update.py",
                "--start-date", "2026-07-01",
                "--end-date", "2026-07-31",
                "--stage", "all",
                "--exposure", "Vitamin K2",
                "--exposure", "Vitamin K1",
                "--rescreen-saved-contexts",
            ],
        ):
            args = monthly.parse_args()
        self.assertEqual(args.exposure, ["Vitamin K2", "Vitamin K1"])
        self.assertTrue(args.rescreen_saved_contexts)

    def test_revision_ncbi_scope_excludes_narrowed_classifications(self):
        self.assertIn("vitamin b3", monthly.revision_ncbi_scope_names())
        self.assertIn("vitamin k1", monthly.revision_ncbi_scope_names())
        self.assertNotIn("fish oil", monthly.revision_ncbi_scope_names())
        self.assertNotIn("tomato", monthly.revision_ncbi_scope_names())
        self.assertEqual(
            monthly.reclassification_scope_names(),
            {"fish oil", "omega-3 fatty acids", "tomato"},
        )

    def test_revision_catalog_adds_all_additional_exposures_without_narrowed(self):
        terms = monthly.exposure_terms(revision_aware=True)
        self.assertIn("Vitamin B3", terms)
        self.assertIn("Vitamin K1", terms)
        self.assertIn("PQQ", terms)
        self.assertNotIn("Fish oil", terms)
        self.assertNotIn("Tomato", terms)
        self.assertIn("DHA", terms["Docosahexaenoic acid"])
        self.assertIn("protein intake", terms["Dietary protein intake"])
        self.assertNotIn("NAD+", terms["NAD+"])
        self.assertNotIn("NAD", terms["NAD+"])

    def test_hierarchy_rolls_up_transitively_after_direct_screening(self):
        self.assertEqual(
            monthly.inherited_exposures("Linoleic acid"),
            ["Omega-6 fatty acids", "Polyunsaturated fat (PUFA)"],
        )
        self.assertEqual(
            monthly.inherited_exposures("Mead acid"),
            ["Omega-9 fatty acids", "Polyunsaturated fat (PUFA)"],
        )
        item = {
            "pmid": "123",
            "exposure": "Linoleic acid",
            "direct_exposure": "Linoleic acid",
            "cancer": "Prostate cancer",
            "outcome": "Incidence",
            "extraction": {
                "effect_type": "RR",
                "effect_size": 0.8,
                "comparison_type": "highest versus lowest",
                "exposure_subtype": "dietary_component",
                "exposure_measurement_type": "dietary_intake",
            },
        }
        first_id = monthly.stable_effect_id(item)
        rolled_up = {**item, "exposure": "Omega-6 fatty acids", "hierarchy_inherited": True}
        self.assertEqual(first_id, monthly.stable_effect_id(rolled_up))
        searchable = set(monthly.exposure_terms("combined"))
        self.assertFalse(
            {
                parent
                for parents in monthly.exposure_hierarchy().values()
                for parent in parents
            }.difference(searchable)
        )

    def test_same_pmid_parent_context_is_not_screened_twice(self):
        contexts = {
            "1": {
                ("Linoleic acid", "Prostate cancer"),
                ("Omega-6 fatty acids", "Prostate cancer"),
                ("Polyunsaturated fat (PUFA)", "Prostate cancer"),
            },
            "2": {("Omega-6 fatty acids", "Prostate cancer")},
        }

        removed = monthly.dedupe_parent_search_contexts(contexts)

        self.assertEqual(len(removed), 2)
        self.assertTrue(all(item["reason"].endswith("omitted.") for item in removed))
        self.assertEqual(contexts["1"], {("Linoleic acid", "Prostate cancer")})
        self.assertEqual(contexts["2"], {("Omega-6 fatty acids", "Prostate cancer")})

    def test_vitamin_k_child_discovery_does_not_create_parent_screen_contexts(self):
        records = {
            pmid: {
                "pmid": pmid,
                "published_date": "2026-07-15",
                "journal_publication_date": "2026-07-15",
            }
            for pmid in ("101", "102")
        }
        known = {("101", "vitamin_k", "prostate_cancer")}
        searched = monthly.EntrezSearchIds(
            ["101", "102"],
            {
                "complete": True,
                "reported_count": 2,
                "returned_unique_pmids": 2,
                "request_count": 1,
                "leaf_intervals": [
                    {
                        "publication_date_start": "2026-07-01",
                        "publication_date_end": "2026-07-31",
                        "reported_count": 2,
                        "returned_unique_pmids": 2,
                        "complete": True,
                    }
                ],
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with (
                patch.object(monthly, "configure_entrez", return_value="working"),
                patch.object(monthly, "saved_contexts", return_value=known),
                patch.object(monthly, "entrez_search", return_value=searched) as search,
                patch.object(monthly, "entrez_fetch", return_value=(records, 1)),
            ):
                packet = monthly.discover(
                    "2026-07-01",
                    "2026-07-31",
                    output,
                    selected_exposures=["Vitamin K1"],
                )

        contexts = {
            article["pmid"]: {
                context["exposure"] for context in article["candidate_contexts"]
            }
            for article in packet["articles"]
        }
        self.assertEqual(search.call_count, 1)
        self.assertIn("phylloquinone", search.call_args.args[0])
        self.assertNotIn("menaquinone", search.call_args.args[0])
        self.assertEqual(contexts["101"], {"Vitamin K1"})
        self.assertEqual(contexts["102"], {"Vitamin K1"})
        self.assertEqual(packet["selected_exposures"], ["Vitamin K1"])
        self.assertEqual(packet["candidate_contexts"], 2)
        self.assertEqual(packet["search_completion"][0]["reported_count"], 2)

    def test_discovery_fails_when_efetch_omits_a_requested_pmid(self):
        searched = monthly.EntrezSearchIds(
            ["101"],
            {
                "complete": True,
                "reported_count": 1,
                "returned_unique_pmids": 1,
                "request_count": 1,
                "leaf_intervals": [
                    {
                        "publication_date_start": "2026-07-01",
                        "publication_date_end": "2026-07-31",
                        "reported_count": 1,
                        "returned_unique_pmids": 1,
                        "complete": True,
                    }
                ],
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with (
                patch.object(monthly, "configure_entrez", return_value="working"),
                patch.object(monthly, "entrez_search", return_value=searched),
                patch.object(monthly, "entrez_fetch", return_value=({}, 1)),
                self.assertRaisesRegex(RuntimeError, "EFetch omitted requested PMIDs.*101"),
            ):
                monthly.discover(
                    "2026-07-01",
                    "2026-07-31",
                    output,
                    selected_exposures=["Vitamin K1"],
                )
            self.assertFalse((output / "candidates.json").exists())

    def test_matching_window_discovery_reuses_immutable_candidates(self):
        original = candidate()
        original["vocabulary_digest"] = monthly.exposure_vocabulary_digest(
            monthly.exposure_terms()
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monthly.save_json(output / "candidates.json", original)
            with patch.object(
                monthly,
                "configure_entrez",
                side_effect=AssertionError("network discovery should not rerun"),
            ):
                reused = monthly.discover(
                    "2026-07-01", "2026-07-31", output
                )
            persisted = json.loads((output / "candidates.json").read_text())

        self.assertEqual(reused, original)
        self.assertEqual(persisted, original)

    def test_all_history_discovery_reuses_explicit_null_lower_bound(self):
        original = candidate()
        original["publication_date_start"] = None
        original["publication_date_end"] = "2026-06-30"
        original["vocabulary_digest"] = monthly.exposure_vocabulary_digest(
            monthly.exposure_terms()
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monthly.save_json(output / "candidates.json", original)
            with patch.object(
                monthly,
                "configure_entrez",
                side_effect=AssertionError("network discovery should not rerun"),
            ):
                reused = monthly.discover(None, "2026-06-30", output)

        self.assertIsNone(reused["publication_date_start"])
        self.assertEqual(reused["publication_date_end"], "2026-06-30")

    def test_all_history_esearch_uses_year_one_sentinel_to_make_maxdate_effective(self):
        class FakeHandle:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with (
            patch.object(monthly.Entrez, "esearch", return_value=FakeHandle()) as search,
            patch.object(
                monthly.Entrez,
                "read",
                return_value={"Count": "1", "IdList": ["123"]},
            ),
        ):
            found = monthly.entrez_search("example query", None, "2026-06-30")

        self.assertEqual(found, ["123"])
        self.assertEqual(search.call_args.kwargs["mindate"], "0001/01/01")
        self.assertEqual(search.call_args.kwargs["maxdate"], "2026/06/30")
        self.assertEqual(search.call_args.kwargs["datetype"], "pdat")
        self.assertEqual(search.call_args.kwargs["retstart"], 0)

    def test_bounded_esearch_keeps_the_requested_lower_bound(self):
        class FakeHandle:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with (
            patch.object(monthly.Entrez, "esearch", return_value=FakeHandle()) as search,
            patch.object(
                monthly.Entrez,
                "read",
                return_value={"Count": "0", "IdList": []},
            ),
        ):
            monthly.entrez_search("example query", "2026-07-01", "2026-07-31")

        self.assertEqual(search.call_args.kwargs["mindate"], "2026/07/01")
        self.assertEqual(search.call_args.kwargs["maxdate"], "2026/07/31")

    def test_esearch_retries_wrapped_429_and_5xx_runtime_errors(self):
        class FakeHandle:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        for status in (429, 502, 503):
            with self.subTest(status=status):
                with (
                    patch.object(
                        monthly.Entrez,
                        "esearch",
                        side_effect=[
                            RuntimeError(
                                f"HTTP Error {status}: transient backend failure"
                            ),
                            FakeHandle(),
                        ],
                    ) as search,
                    patch.object(
                        monthly.Entrez,
                        "read",
                        return_value={"Count": "1", "IdList": ["123"]},
                    ),
                    patch.object(monthly.time, "sleep") as sleep,
                ):
                    found = monthly.entrez_search(
                        "example query", "2026-07-01", "2026-07-31"
                    )
                self.assertEqual(found, ["123"])
                self.assertEqual(search.call_count, 2)
                sleep.assert_called_once_with(1)

    def test_esearch_does_not_retry_400_or_payload_invariants(self):
        class FakeHandle:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with (
            patch.object(
                monthly.Entrez,
                "esearch",
                side_effect=RuntimeError("HTTP Error 400: Bad Request"),
            ) as search,
            patch.object(monthly.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "Bad Request"),
        ):
            monthly.entrez_search(
                "bad query", "2026-07-01", "2026-07-31"
            )
        self.assertEqual(search.call_count, 1)
        sleep.assert_not_called()

        with (
            patch.object(monthly.Entrez, "esearch", return_value=FakeHandle()) as search,
            patch.object(monthly.Entrez, "read", return_value=[]),
            patch.object(monthly.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "non-object"),
        ):
            monthly.entrez_search(
                "example query", "2026-07-01", "2026-07-31"
            )
        self.assertEqual(search.call_count, 1)
        sleep.assert_not_called()

    def test_efetch_uses_the_same_transient_retry_taxonomy(self):
        class FakeHandle:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        parsed = {"pmid": "123", "title": "Fetched"}
        with (
            patch.object(
                monthly.Entrez,
                "efetch",
                side_effect=[RuntimeError("HTTP Error 502: Bad Gateway"), FakeHandle()],
            ) as fetch,
            patch.object(
                monthly.Entrez,
                "read",
                return_value={"PubmedArticle": [{}], "PubmedBookArticle": []},
            ),
            patch.object(monthly, "article_record", return_value=parsed),
            patch.object(monthly.time, "sleep") as sleep,
        ):
            records, requests = monthly.entrez_fetch(["123"])
        self.assertEqual(records, {"123": parsed})
        self.assertEqual(requests, 1)
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_api_key_validation_uses_the_same_retry_taxonomy(self):
        class FakeHandle:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        environment = {
            "NCBI_EMAIL": "screening@example.org",
            "NCBI_API_KEY": "test-key",
        }
        with (
            patch.dict(monthly.os.environ, environment, clear=True),
            patch.object(monthly, "load_dotenv"),
            patch.object(
                monthly.Entrez,
                "esearch",
                side_effect=[RuntimeError("HTTP Error 502: Bad Gateway"), FakeHandle()],
            ) as search,
            patch.object(monthly.Entrez, "read", return_value={}),
            patch.object(monthly.time, "sleep") as sleep,
        ):
            self.assertEqual(monthly.configure_entrez(), "working")
        self.assertEqual(search.call_count, 2)
        sleep.assert_called_once_with(1)

        with (
            patch.dict(monthly.os.environ, environment, clear=True),
            patch.object(monthly, "load_dotenv"),
            patch.object(
                monthly.Entrez,
                "esearch",
                side_effect=RuntimeError("HTTP Error 400: Bad Request"),
            ) as search,
            patch.object(monthly.time, "sleep") as sleep,
        ):
            self.assertEqual(monthly.configure_entrez(), "rejected_http_400")
        self.assertEqual(search.call_count, 1)
        sleep.assert_not_called()

    def test_esearch_splits_large_date_ranges_without_10k_pagination(self):
        class FakeHandle:
            def __init__(self, record):
                self.record = record

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        records = {
            ("2026/07/01", "2026/07/02"): {
                "Count": "10001",
                "IdList": [],
            },
            ("2026/07/01", "2026/07/01"): {
                "Count": "5000",
                "IdList": [str(value) for value in range(1, 5001)],
            },
            ("2026/07/02", "2026/07/02"): {
                "Count": "5001",
                "IdList": [str(value) for value in range(5001, 10002)],
            },
        }
        calls = []

        def fake_search(**kwargs):
            calls.append(kwargs)
            return FakeHandle(records[(kwargs["mindate"], kwargs["maxdate"])])

        with (
            patch.object(monthly.Entrez, "esearch", side_effect=fake_search),
            patch.object(monthly.Entrez, "read", side_effect=lambda handle: handle.record),
        ):
            found = monthly.entrez_search(
                "example query", "2026-07-01", "2026-07-02"
            )

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call["retstart"] == 0 for call in calls))
        self.assertEqual(len(found), 10001)
        self.assertEqual(found.audit["reported_count"], 10001)
        self.assertEqual(found.audit["returned_unique_pmids"], 10001)
        self.assertEqual(len(found.audit["leaf_intervals"]), 2)

    def test_esearch_fails_when_one_day_exceeds_10000(self):
        class FakeHandle:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with (
            patch.object(monthly.Entrez, "esearch", return_value=FakeHandle()),
            patch.object(
                monthly.Entrez,
                "read",
                return_value={"Count": "10001", "IdList": []},
            ),
            self.assertRaisesRegex(RuntimeError, "single publication day"),
        ):
            monthly.entrez_search(
                "example query", "2026-07-01", "2026-07-01"
            )

    def test_esearch_rejects_leaf_count_unique_mismatch(self):
        class FakeHandle:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with (
            patch.object(monthly.Entrez, "esearch", return_value=FakeHandle()),
            patch.object(
                monthly.Entrez,
                "read",
                return_value={"Count": "2", "IdList": ["123", "123"]},
            ),
            self.assertRaisesRegex(RuntimeError, "Count/IdList mismatch"),
        ):
            monthly.entrez_search(
                "example query", "2026-07-01", "2026-07-01"
            )

    def test_all_history_date_filter_has_only_an_inclusive_upper_bound(self):
        self.assertTrue(monthly.date_overlaps_window("1850", None, "2026-06-30"))
        self.assertTrue(monthly.date_overlaps_window("2026-06-30", None, "2026-06-30"))
        self.assertFalse(monthly.date_overlaps_window("2026-07-01", None, "2026-06-30"))
        self.assertEqual(
            monthly.select_in_window_publication_date(
                {
                    "journal_publication_date": "2026-07-01",
                    "electronic_publication_date": "2026-06-30",
                },
                None,
                "2026-06-30",
            ),
            "2026-06-30",
        )

    def test_all_history_window_text_never_renders_none(self):
        self.assertEqual(
            monthly.publication_window_text(None, "2026-06-30"),
            "no lower publication-date limit through 2026-06-30",
        )
        self.assertEqual(
            monthly.publication_window_text("2026-07-01", "2026-07-31"),
            "2026-07-01 through 2026-07-31",
        )

    def test_all_history_cli_uses_a_separate_default_subdirectory(self):
        report_root = Path("/tmp/metavir-test-reports")
        with patch(
            "sys.argv",
            [
                "monthly_evidence_update.py",
                "--all-history-through",
                "2026-06-30",
                "--monthly-report-subdir",
                "--report-dir",
                str(report_root),
            ],
        ):
            args = monthly.parse_args()

        self.assertIsNone(args.start_date)
        self.assertEqual(args.end_date, "2026-06-30")
        self.assertEqual(
            args.output_dir,
            monthly.DATA_DIR / "monthly_updates" / "all_history_through_2026-06-30",
        )
        self.assertEqual(
            args.report_dir,
            report_root / "all_history_through_2026-06-30",
        )

    def test_all_history_cli_disallows_force(self):
        with patch(
            "sys.argv",
            [
                "monthly_evidence_update.py",
                "--all-history-through",
                "2026-06-30",
                "--force",
            ],
        ):
            with self.assertRaises(SystemExit):
                monthly.parse_args()

    def test_default_models_and_provider_specific_api_shapes(self):
        configured, gpt_api, deepseek_api = runtimes(
            [{"results": [{"context_id": "x", "decision": "PASS", "reason": "Relevant."}]}],
            [{"results": [{"context_id": "x", "decision": "PASS", "reason": "Relevant."}]}],
        )
        screening.call_json(
            configured["chatgpt"], "first", "system", "prompt", screening.empty_usage("gpt")
        )
        screening.call_json(
            configured["deepseek"], "first", "system", "prompt", screening.empty_usage("deepseek")
        )

        self.assertEqual(gpt_api.calls[0]["model"], "gpt-5.6-luna")
        self.assertIn("text", gpt_api.calls[0])
        self.assertEqual(gpt_api.calls[0]["text"]["format"]["type"], "json_schema")
        self.assertNotIn("messages", gpt_api.calls[0])
        self.assertEqual(deepseek_api.calls[0]["model"], "deepseek-v4-flash")
        self.assertEqual(deepseek_api.calls[0]["response_format"], {"type": "json_object"})
        self.assertIn("OUTPUT CONTRACT", deepseek_api.calls[0]["messages"][1]["content"])
        self.assertEqual(deepseek_api.calls[0]["max_tokens"], 8192)

    def test_tracks_share_cornell_gateway_but_keep_separate_clients(self):
        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            return object()

        environment = {
            "CORNELL_API_KEY": "cornell-secret",
            "CORNELL_API_BASE_URL": "https://gateway.example/v1",
            "CHATGPT_API_KEY": "ignored-direct-gpt-secret",
            "DEEPSEEK_API_KEY": "ignored-direct-deepseek-secret",
        }
        with patch.dict("os.environ", environment, clear=True):
            gpt = screening.configure_runtime("chatgpt", Path("/missing"), factory)
            deepseek = screening.configure_runtime("deepseek", Path("/missing"), factory)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["api_key"], "cornell-secret")
        self.assertEqual(calls[1]["api_key"], "cornell-secret")
        self.assertEqual(calls[0]["base_url"], "https://gateway.example/v1")
        self.assertEqual(calls[1]["base_url"], "https://gateway.example/v1")
        self.assertEqual(gpt.api_style, "responses")
        self.assertEqual(deepseek.api_style, "chat_completions")

    def test_cornell_gateway_credentials_are_required_for_both_tracks(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "CORNELL_API_KEY"):
                screening.configure_runtime("chatgpt", Path("/missing"), lambda **_: object())

        with patch.dict("os.environ", {"CORNELL_API_KEY": "secret"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "CORNELL_API_BASE_URL"):
                screening.configure_runtime("deepseek", Path("/missing"), lambda **_: object())

    def test_existing_mykey_aliases_configure_both_cornell_tracks(self):
        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            return object()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mykey.env").write_text(
                'Cornell_API_Key = "local-cornell-secret"\n'
                'API_Base_URI = "https://gateway.example/v1"\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                screening.configure_runtime("chatgpt", root, factory)
                screening.configure_runtime("deepseek", root, factory)

        self.assertEqual([call["api_key"] for call in calls], [
            "local-cornell-secret",
            "local-cornell-secret",
        ])
        self.assertEqual([call["base_url"] for call in calls], [
            "https://gateway.example/v1",
            "https://gateway.example/v1",
        ])

    def test_full_text_trigger_is_exact_and_moderate_can_pool(self):
        complete = screening._normalise_extraction(extraction())
        moderate = screening._strict_jbi(jbi_answers(yes=6))
        fair = screening._strict_jbi(jbi_answers(yes=5))
        incomplete = screening._normalise_extraction(extraction(cases=None))

        self.assertFalse(screening.needs_full_text(complete, moderate))
        self.assertTrue(screening.is_poolable(complete, moderate))
        self.assertTrue(screening.needs_full_text(complete, fair))
        self.assertFalse(screening.is_poolable(complete, fair))
        self.assertFalse(screening.needs_full_text(incomplete, moderate))
        self.assertTrue(screening.is_poolable(incomplete, moderate))

        missing_comparison = screening._normalise_extraction(extraction())
        missing_comparison["comparison_type"] = None
        self.assertFalse(screening.needs_full_text(missing_comparison, moderate))
        self.assertTrue(screening.is_poolable(missing_comparison, moderate))
        self.assertTrue(missing_comparison["direction_imputed"])

    def test_wald_ci_from_p_completes_key_evidence(self):
        raw = extraction()
        raw["ci_lower"] = None
        raw["ci_upper"] = None
        raw["evidence"]["confidence_interval"] = None

        normalized = screening._normalise_extraction(raw)
        moderate = screening._strict_jbi(jbi_answers(yes=6))

        self.assertLess(normalized["ci_lower"], normalized["effect_size"])
        self.assertGreater(normalized["ci_upper"], normalized["effect_size"])
        self.assertEqual(
            normalized["ci_method"], screening.WALD_CONFIDENCE_INTERVAL_METHOD
        )
        self.assertTrue(screening.is_poolable(normalized, moderate))

    def test_relaxed_policy_retains_optional_na_and_requires_total_sample_size(self):
        raw = extraction(cases=None, subtype="mixed_or_unclear")
        raw.update(
            p_value=None,
            ci_confidence_level=None,
            wald_inputs_same_estimate=False,
            exposure_measurement_type="NA",
        )
        raw["evidence"]["p_value"] = None
        raw["evidence"]["exposure_measurement"] = None
        normalized = screening._normalise_extraction(raw)
        moderate = screening._strict_jbi(jbi_answers(yes=6))
        self.assertTrue(screening.is_poolable(normalized, moderate))
        self.assertIsNone(normalized["p_value"])
        self.assertIsNone(normalized["cases"])
        self.assertIsNone(normalized["exposure_measurement_type"])
        self.assertFalse(screening.needs_full_text(normalized, moderate))
        normalized["total_n"] = None
        self.assertFalse(screening.is_poolable(normalized, moderate))
        self.assertEqual(screening.missing_key_fields(normalized), ["total_n"])

    def test_relaxed_policy_imputation_is_idempotent_and_preserves_evidence(self):
        raw = extraction()
        raw["comparison_type"] = "Binary exposure; direction unclear"
        before_evidence = copy.deepcopy(raw["evidence"])
        screening.apply_poolability_policy(raw)
        first_result = copy.deepcopy(raw)
        self.assertIs(screening.apply_poolability_policy(raw), raw)
        self.assertEqual(raw, first_result)
        self.assertEqual(raw["comparison_type"], "Yes versus no")
        self.assertTrue(raw["direction_imputed"])
        self.assertEqual(raw["evidence"], before_evidence)

    def test_explicit_inverse_direction_remains_explicit(self):
        for comparison in (
            "Lowest versus highest",
            "25-hydroxyvitamin D below the median versus above the median",
            "<20 versus 30-<50 ng/ml",
            "below median vs above median",
            "non-vegetarian versus vegetarian diet",
            "top_vs_bottom_quartile",
        ):
            with self.subTest(comparison=comparison):
                raw = extraction()
                raw.update(comparison_type=comparison, needs_inversion=True)
                screening.apply_poolability_policy(raw)
                self.assertFalse(raw["direction_imputed"])
                self.assertTrue(raw["needs_inversion"])
                self.assertEqual(raw["comparison_type"], comparison)

    def test_unspecified_reference_is_marked_as_imputed(self):
        for comparison in (
            "Highest serum quartile versus an unspecified reference quartile",
            "Highest quartile versus an unknown reference quartile",
            "High intake versus reference category not specified",
            "High intake versus a referent group not provided",
            "Higher exposure versus undefined comparator",
        ):
            with self.subTest(comparison=comparison):
                raw = extraction()
                raw["comparison_type"] = comparison
                screening.apply_poolability_policy(raw)
                self.assertTrue(raw["direction_imputed"])
                self.assertEqual(raw["comparison_type"], "High versus low")
                self.assertEqual(raw["direction_original_comparison"], comparison)

    def test_unspecified_unit_does_not_make_explicit_direction_imputed(self):
        for comparison in (
            "Highest versus lowest; exposure unit unspecified",
            "Highest versus lowest; adjustment model unspecified",
            "<20 versus 30-<50; units unspecified",
            "Greater versus lower healthful plant-based diet index (unspecified quantile contrast)",
            "Higher versus lower dietary n-3 PUFA intake (categories not specified in abstract)",
        ):
            with self.subTest(comparison=comparison):
                raw = extraction()
                raw["comparison_type"] = comparison
                screening.apply_poolability_policy(raw)
                self.assertFalse(raw["direction_imputed"])
                self.assertEqual(raw["comparison_type"], comparison)

    def test_full_text_missing_direction_preserves_abstract_direction(self):
        abstract = screening._normalise_extraction(extraction())
        abstract.update(comparison_type="Lowest versus highest", needs_inversion=True)
        full_text = extraction()
        full_text["comparison_type"] = None
        full_text["evidence"]["comparison"] = None
        for evidence in full_text["evidence"].values():
            if evidence:
                evidence["source"] = "full_text"
                evidence["url"] = "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/"
        full_text = screening._normalise_extraction(full_text, required_source="full_text")
        merged = screening._merge_extractions(abstract, full_text)
        self.assertEqual(merged["comparison_type"], "Lowest versus highest")
        self.assertTrue(merged["needs_inversion"])
        self.assertFalse(merged["direction_imputed"])

    def test_full_text_explicit_direction_replaces_prior_imputation(self):
        abstract = extraction()
        abstract["comparison_type"] = None
        screening.apply_poolability_policy(abstract)
        full_text = extraction()
        full_text.update(comparison_type="Lowest versus highest", needs_inversion=True)
        merged = screening._merge_extractions(abstract, full_text)
        self.assertFalse(merged["direction_imputed"])
        self.assertTrue(merged["needs_inversion"])
        self.assertNotIn("direction_imputation_reason", merged)

    def test_p_only_bounds_and_wrong_estimate_remain_unpoolable(self):
        moderate = screening._strict_jbi(jbi_answers(yes=6))
        for p_value, excerpt in (
            ("<0.05", "The p value was <0.05."),
            (0.03, "The p for trend across categories was 0.03."),
        ):
            with self.subTest(p_value=p_value, excerpt=excerpt):
                raw = extraction(cases=None)
                raw.update(ci_lower=None, ci_upper=None, p_value=p_value)
                raw["evidence"]["confidence_interval"] = None
                raw["evidence"]["p_value"] = evidence_entry(excerpt)
                normalized = screening._normalise_extraction(raw)
                self.assertFalse(screening.is_poolable(normalized, moderate))
                self.assertIsNone(normalized["ci_lower"])

    def test_wald_p_value_from_ci_is_retained_with_provenance(self):
        raw = extraction()
        raw["p_value"] = None
        raw["evidence"]["p_value"] = None

        normalized = screening._normalise_extraction(raw)

        self.assertGreater(normalized["p_value"], 0)
        self.assertLess(normalized["p_value"], 1)
        self.assertEqual(
            normalized["p_value_method"], screening.WALD_P_VALUE_METHOD
        )

    def test_p_value_boundary_accepts_one_and_rejects_zero(self):
        self.assertEqual(screening._number_or_none(1, "p_value"), 1)
        self.assertEqual(screening._wald_p_value_input(1), 1.0)
        self.assertIsNone(screening._wald_p_value_input(0))
        self.assertIsNone(
            screening.wald_ratio_ci_from_effect_p(0.82, 1, "HR")
        )
        with self.assertRaisesRegex(
            screening.ScreeningValidationError, "greater than zero"
        ):
            screening._number_or_none(0, "p_value")

    def test_wald_ci_is_not_inverted_from_a_p_value_lower_bound(self):
        raw = extraction()
        raw["ci_lower"] = None
        raw["ci_upper"] = None
        raw["p_value"] = ">0.05"
        raw["evidence"]["confidence_interval"] = None
        raw["evidence"]["p_value"] = evidence_entry("The p value was >0.05.")

        normalized = screening._normalise_extraction(raw)

        self.assertIsNone(normalized["ci_lower"])
        self.assertIsNone(normalized["ci_upper"])

    def test_wald_ci_rejects_trend_p_value_for_target_estimate(self):
        raw = extraction()
        raw["ci_lower"] = None
        raw["ci_upper"] = None
        raw["evidence"]["confidence_interval"] = None
        raw["evidence"]["p_value"] = evidence_entry(
            "The p for trend across intake quartiles was 0.03."
        )

        normalized = screening._normalise_extraction(raw)

        self.assertIsNone(normalized["ci_lower"])
        self.assertIsNone(normalized["ci_upper"])

    def test_optional_invalid_p_does_not_block_a_usable_ci(self):
        extraction_record = screening._normalise_extraction(extraction())
        extraction_record["p_value"] = 0
        moderate = screening._strict_jbi(jbi_answers(yes=6))

        self.assertEqual(screening.invalid_key_fields(extraction_record), [])
        self.assertFalse(screening.needs_full_text(extraction_record, moderate))
        self.assertTrue(screening.is_poolable(extraction_record, moderate))

    def test_manual_full_text_adjudications_are_conservative_and_validated(self):
        context_id = "123|Tomato|Prostate cancer"
        packet = {
            "schema_version": 1,
            "recorded_at": "2026-08-21T01:20:00+08:00",
            "adjudications": [
                {
                    "context_id": context_id,
                    "full_text_status": "ELIGIBLE_NOT_POOLABLE",
                    "exclusion_code": None,
                    "reason": "The full text reports no poolable ratio estimate.",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            (output_dir / screening.FULL_TEXT_MANUAL_ADJUDICATIONS_FILENAME).write_text(
                json.dumps(packet), encoding="utf-8"
            )
            adjudications = screening.load_full_text_manual_adjudications(
                output_dir, {context_id: {"context_id": context_id}}
            )

        self.assertEqual(
            adjudications[context_id]["full_text_status"],
            "ELIGIBLE_NOT_POOLABLE",
        )

    def test_unclear_comparison_direction_is_imputed_without_full_text(self):
        unclear = extraction()
        unclear["comparison_type"] = "unclear"
        unclear["evidence"]["comparison"] = evidence_entry(
            "The comparison direction was unclear."
        )
        normalized = screening._normalise_extraction(unclear)
        moderate = screening._strict_jbi(jbi_answers(yes=6))

        self.assertEqual(normalized["comparison_type"], "High versus low")
        self.assertEqual(normalized["direction_original_comparison"], "unclear")
        self.assertTrue(normalized["direction_imputed"])
        self.assertNotIn("comparison_type", screening.missing_key_fields(normalized))
        self.assertFalse(screening.needs_full_text(normalized, moderate))

    def test_jbi_na_answers_cannot_inflate_quality(self):
        answers = {f"q{index}": "NA" for index in range(1, 14)}
        answers["q1"] = "Yes"
        rated = screening._strict_jbi(
            {"checklist_type": "cohort", "answers": answers}
        )
        self.assertEqual(rated["score_percent"], 9.1)
        self.assertEqual(rated["grade"], "Fair")

    def test_quantitative_validation_keeps_values_and_blanks_bad_excerpts(self):
        bad_number = extraction()
        bad_number["effect_size"] = "not ascertainable"
        with self.assertRaises(screening.ScreeningValidationError):
            screening._normalise_extraction(bad_number)

        candidates = candidate()
        expected = screening.expected_contexts(candidates)
        cid = next(iter(expected))
        unsupported = extraction()
        unsupported["evidence"]["effect_size"] = evidence_entry(
            "This sentence is not present in the supplied abstract."
        )
        result = screening.validate_second(
            {
                "results": [
                    {
                        "context_id": cid,
                        "eligibility": "INCLUDE",
                        "exclusion_code": None,
                        "reason": "The study reports an eligible estimate.",
                        "extraction": unsupported,
                        "jbi": jbi_answers(yes=6),
                    }
                ]
            },
            expected,
        )[0]
        self.assertEqual(result["extraction"]["effect_size"], 0.82)
        self.assertIn(
            "adjusted hazard ratio was 0.82",
            result["extraction"]["evidence"]["effect_size"]["excerpt"],
        )

        wrong_url = extraction()
        wrong_url["evidence"]["effect_size"] = evidence_entry(
            "The adjusted hazard ratio was 0.82.",
            url="https://pubmed.ncbi.nlm.nih.gov/51234/",
        )
        with self.assertRaises(screening.ScreeningValidationError):
            screening.validate_second(
                {
                    "results": [
                        {
                            "context_id": cid,
                            "eligibility": "INCLUDE",
                            "exclusion_code": None,
                            "reason": "The study reports an eligible estimate.",
                            "extraction": wrong_url,
                            "jbi": jbi_answers(yes=6),
                        }
                    ]
                },
                expected,
            )

    def test_percent_effect_uses_relevant_text_without_printed_ratio(self):
        raw = extraction()
        raw.update(
            {
                "effect_size": 1.19,
                "effect_type": "percent higher risk",
                "ci_lower": None,
                "ci_upper": None,
                "p_value": None,
            }
        )
        raw["evidence"]["effect_size"] = evidence_entry(
            "Observed biopsies suggested a 19% higher risk of high-grade prostate cancer."
        )
        raw["evidence"]["confidence_interval"] = None
        raw["evidence"]["p_value"] = None
        source = (
            "Observed biopsies suggested a 19% higher risk of high-grade prostate cancer."
        )

        normalized = screening._normalise_extraction(
            raw,
            required_source="abstract",
            source_text=source,
            source_identifier="123",
        )

        self.assertEqual(normalized["effect_type"], "RR")
        self.assertEqual(normalized["effect_size"], 1.19)
        self.assertIn("19% higher risk", normalized["evidence"]["effect_size"]["excerpt"])

    def test_raw_percentage_effect_is_converted_to_ratio(self):
        for raw_value, label, statement, expected in (
            (91, "percent increase in risk", "Risk increased by 91%.", 1.91),
            (63, "63% higher risk", "Participants had a 63% higher risk.", 1.63),
            (37, "percent lower risk", "Participants had a 37% lower risk.", 0.63),
            (1.91, "percent increase in risk", "Risk increased by 91%.", 1.91),
        ):
            raw = extraction()
            raw.update(
                {
                    "effect_size": raw_value,
                    "effect_type": label,
                    "ci_lower": None,
                    "ci_upper": None,
                    "p_value": None,
                }
            )
            raw["evidence"]["effect_size"] = evidence_entry(statement)
            raw["evidence"]["confidence_interval"] = None
            raw["evidence"]["p_value"] = None

            normalized = screening._normalise_extraction(raw, source_text=statement)

            self.assertAlmostEqual(normalized["effect_size"], expected)

    def test_missing_audit_excerpts_and_qualitative_p_value_are_handled(self):
        raw = extraction()
        raw["p_value"] = "not statistically significant"
        raw["total_n"] = 398
        raw["cases"] = 133
        raw["evidence"]["sample_size"] = None
        raw["evidence"]["cases"] = None
        raw["evidence"]["design"] = evidence_entry("This quotation is not in the source.")
        raw["evidence"]["exposure_measurement"] = None

        normalized = screening._normalise_extraction(raw, source_text="No numerical sample statement.")

        self.assertGreater(normalized["p_value"], 0)
        self.assertEqual(
            normalized["p_value_method"], screening.WALD_P_VALUE_METHOD
        )
        self.assertEqual(normalized["total_n"], 398)
        self.assertEqual(normalized["cases"], 133)
        self.assertIsNone(normalized["evidence"]["sample_size"])
        self.assertEqual(normalized["evidence"]["design"]["excerpt"], "")

    def test_absolute_mean_difference_is_excluded_from_ratio_meta_analysis(self):
        candidates = candidate()
        expected = screening.expected_contexts(candidates)
        cid = next(iter(expected))
        raw = extraction()
        raw["effect_size"] = 0
        raw["effect_type"] = "absolute mean difference in serum concentration"

        result = screening.validate_second(
            {
                "results": [
                    {
                        "context_id": cid,
                        "eligibility": "INCLUDE",
                        "exclusion_code": None,
                        "reason": "The study reports a mean difference.",
                        "extraction": raw,
                        "jbi": jbi_answers(yes=6),
                    }
                ]
            },
            expected,
        )[0]

        self.assertEqual(result["eligibility"], "EXCLUDE")
        self.assertEqual(result["exclusion_code"], "S2-NO-TARGET-ESTIMATE")
        self.assertIsNone(result["extraction"])

    def test_country_ethnicity_and_field_specific_evidence_are_saved(self):
        normalized = screening._normalise_extraction(extraction())
        self.assertEqual(normalized["ethnicity"], "NA")
        self.assertEqual(normalized["location"], "Sweden")

        no_ethnicity_evidence = extraction()
        no_ethnicity_evidence["evidence"]["ethnicity"] = None
        ethnicity_na = screening._normalise_extraction(no_ethnicity_evidence)
        self.assertEqual(ethnicity_na["ethnicity"], "NA")

        china = screening.infer_ethnicity(
            {
                "country": "China",
                "population": "Chinese adults",
                "evidence": {"ethnicity": {"excerpt": "Participants in China"}},
            }
        )
        self.assertEqual(china[0], "NA")
        for country in ("Japan", "Finland", "Nigeria", "United States"):
            inferred = screening.infer_ethnicity(
                {
                    "country": country,
                    "population": None,
                    "evidence": {"ethnicity": {"excerpt": f"Participants in {country}"}},
                }
            )
            self.assertEqual(inferred[0], "NA")
        explicit_white = screening.infer_ethnicity(
            {
                "country": "United States",
                "ethnicity": "Caucasian",
                "population": "Caucasian participants",
                "evidence": {"ethnicity": {"excerpt": "Caucasian participants"}},
            }
        )
        self.assertEqual(explicit_white[0], "Caucasian")
        mixed = screening.infer_ethnicity(
            {
                "country": "Sweden and China",
                "ethnicity": "Swedish and Chinese",
                "population": "Swedish and Chinese men",
                "evidence": {"ethnicity": {"excerpt": "Swedish and Chinese men"}},
            }
        )
        self.assertEqual(mixed[0], "Swedish and Chinese")

        country_only = extraction(country="China")
        country_only["ethnicity"] = "Asian"
        country_only["evidence"]["ethnicity"] = evidence_entry(
            "Participants were recruited in China."
        )
        country_only["evidence"]["location"] = evidence_entry(
            "Participants were recruited in China."
        )
        normalized_country_only = screening._normalise_extraction(
            country_only,
            required_source="abstract",
            source_text="Participants were recruited in China.",
            source_identifier="123",
        )
        self.assertEqual(normalized_country_only["ethnicity"], "NA")

        luna_attempt = copy.deepcopy(normalized_country_only)
        luna_attempt["ethnicity"] = "Asian"
        luna_attempt["location"] = "Beijing"
        luna_attempt["evidence"]["ethnicity"] = evidence_entry(
            "The full text describes Asian participants.", source="full_text"
        )
        luna_attempt["evidence"]["location"] = evidence_entry(
            "Participants were recruited in Beijing.", source="full_text"
        )
        merged = screening._merge_extractions(normalized_country_only, luna_attempt)
        self.assertEqual(merged["ethnicity"], "NA")
        self.assertEqual(merged["location"], "China")

        item = {
            "pmid": "123",
            "pmcid": "PMC123",
            "published_date": "2026-07-15",
            "title": "Example",
            "authors": ["Example A"],
            "journal": "Journal",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            "cancer": "Prostate cancer",
            "extraction": normalized,
            "jbi": screening._strict_jbi(jbi_answers(yes=6)),
        }
        row = monthly.study_row(item)
        self.assertEqual(row["PMCID"], "PMC123")
        self.assertEqual(row["Ethnicity"], "NA")
        self.assertEqual(row["Location"], "Sweden")
        self.assertIn("1,000 participants", row["extraction_supporting_text"]["sample_size"])
        self.assertIn("0.70 to 0.96", row["extraction_supporting_text"]["confidence_interval"])
        self.assertEqual(row["exposure_subtype"], "whole_food")

    def test_numeric_evidence_excerpt_is_repaired_from_supplied_abstract(self):
        raw = extraction()
        raw["ci_upper"] = 0.97
        raw["evidence"]["confidence_interval"] = evidence_entry(
            "The adjusted hazard ratio was 0.82."
        )
        source_text = candidate()["articles"][0]["abstract"].replace("0.96", "0.97")

        normalized = screening._normalise_extraction(
            raw,
            required_source="abstract",
            source_text=source_text,
            source_identifier="123",
        )

        repaired = normalized["evidence"]["confidence_interval"]["excerpt"]
        self.assertIn("0.70 to 0.97", repaired)

    def test_hyphenated_confidence_interval_does_not_make_upper_bound_negative(self):
        for interval in ("0.70-0.97", "0.70--0.97", "0.70 -0.97"):
            self.assertEqual(
                screening._numbers_in_text(f"OR 0.82; 95% CI: {interval}"),
                [0.82, 95.0, 0.70, 0.97],
            )
        excerpt, components = screening._source_excerpt_for_numeric_field(
            "ci_upper",
            0.97,
            "Tomato products were associated with risk (OR 0.82; 95% CI: 0.70-0.97).",
            {"effect_size": 0.82, "ci_lower": 0.70},
        )
        self.assertIn("0.70-0.97", excerpt)
        self.assertEqual(components, ())

    def test_unusual_decimal_typography_is_normalized_for_matching(self):
        self.assertEqual(screening._numbers_in_text("HR 0·53; CI 0·34, 0·85"), [0.53, 0.34, 0.85])
        self.assertEqual(screening._numbers_in_text("CI 0. 46-0.99"), [0.46, 0.99])

    def test_spaced_thousands_are_recognized_as_one_sample_size(self):
        for text in (
            "We examined 142 251 men in the cohort.",
            "Using data from 27\u2009859 men, we emulated a target trial.",
        ):
            excerpt, components = screening._source_excerpt_for_numeric_field(
                "total_n",
                screening._numbers_in_text(text)[0],
                text,
            )
            self.assertTrue(excerpt)
            self.assertEqual(components, ())

    def test_total_sample_can_be_derived_from_reported_cases_and_controls(self):
        source_text = (
            "Cases (n = 193) were men with prostate cancer diagnosed in three hospitals, "
            "and controls (n = 197) were matched by age and county."
        )
        excerpt, components = screening._source_excerpt_for_numeric_field(
            "total_n",
            390,
            source_text,
            {"cases": 193},
        )
        self.assertIn("193", excerpt)
        self.assertIn("197", excerpt)
        self.assertEqual(components, (193.0, 197.0))

        evidence = evidence_entry("Cases (n = 193) were enrolled.")
        screening._require_numeric_support(
            "total_n",
            390,
            evidence,
            source_text=source_text,
            extraction={"cases": 193},
        )
        self.assertIn("193", evidence["excerpt"])
        self.assertIn("193 + 197 = 390", evidence["location"])

    def test_written_and_semantically_derived_sample_sizes_are_supported(self):
        for total_n, source_text, expected_components in (
            (60, "Sixty volunteers with HG-PIN were enrolled.", ()),
            (312, "A prospective case-control study included 156 pairs of cases and controls.", (156.0, 156.0)),
            (
                394,
                "The study included 197 cases and an equal number of matched controls.",
                (197.0, 197.0),
            ),
            (
                81,
                "A 1:2 matched case-control study included Twenty-seven cases of prostate cancer.",
                (27.0, 54.0),
            ),
        ):
            excerpt, components = screening._source_excerpt_for_numeric_field(
                "total_n",
                total_n,
                source_text,
            )
            self.assertTrue(excerpt)
            self.assertEqual(components, expected_components)

    def test_cases_can_be_derived_from_arm_specific_counts(self):
        source_text = "No difference in prostate cancer cases was observed: 5 of 49 versus 9 of 48."
        excerpt, components = screening._source_excerpt_for_numeric_field(
            "cases",
            14,
            source_text,
        )
        self.assertTrue(excerpt)
        self.assertEqual(components, (5.0, 9.0))

    def test_mr_and_genotype_interactions_receive_explicit_exclusion_codes(self):
        for title, expected_code in (
            ("Mendelian randomization of tomato and prostate cancer", "S2-MENDELIAN-RANDOMIZATION"),
            ("Two-sample MR of tomato and prostate cancer", "S2-MENDELIAN-RANDOMIZATION"),
            ("MR analysis of lycopene and prostate cancer", "S2-MENDELIAN-RANDOMIZATION"),
            ("An MR study of tomato intake", "S2-MENDELIAN-RANDOMIZATION"),
            ("Genetically predicted lycopene and prostate cancer", "S2-MENDELIAN-RANDOMIZATION"),
            ("Genetic variants as instrumental variables for lycopene", "S2-MENDELIAN-RANDOMIZATION"),
            ("Instrumental-variable analysis using SNPs", "S2-MENDELIAN-RANDOMIZATION"),
            ("Genotype-diet interaction and prostate cancer", "S2-GENOTYPE-INTERACTION"),
            ("Interaction between tomato intake and rs123 genotype", "S2-GENOTYPE-INTERACTION"),
            ("Effect modification by genotype in a tomato cohort", "S2-GENOTYPE-INTERACTION"),
            ("G×E analysis of diet and prostate cancer", "S2-GENOTYPE-INTERACTION"),
            ("Gene–diet interaction and prostate cancer", "S2-GENOTYPE-INTERACTION"),
            ("Association stratified according to GSTM1 genotype", "S2-GENOTYPE-INTERACTION"),
            ("The association differed by rs123 status", "S2-GENOTYPE-INTERACTION"),
            ("SNP modifies the tomato association", "S2-GENOTYPE-INTERACTION"),
            ("Gene by diet interaction and prostate cancer", "S2-GENOTYPE-INTERACTION"),
            ("G*E analysis of diet and prostate cancer", "S2-GENOTYPE-INTERACTION"),
        ):
            candidates = candidate(title)
            expected = screening.expected_contexts(candidates)
            cid = next(iter(expected))
            first = screening.validate_first(
                {"results": [{"context_id": cid, "decision": "FAIL", "reason": "Not relevant."}]},
                expected,
            )
            self.assertEqual(first[0]["decision"], "PASS")
            second = screening.validate_second(
                {
                    "results": [
                        {
                            "context_id": cid,
                            "eligibility": "INCLUDE",
                            "exclusion_code": None,
                            "reason": "Appears relevant.",
                            "extraction": None,
                            "jbi": None,
                        }
                    ]
                },
                expected,
            )
            self.assertEqual(second[0]["eligibility"], "EXCLUDE")
            self.assertEqual(second[0]["exclusion_code"], expected_code)
        for negative in (
            "MRI analysis of prostate tissue",
            "Instrumental-variable analysis of food prices and cancer incidence",
        ):
            self.assertIsNone(screening.protocol_exclusion(candidate(negative)["articles"][0]))

    def test_apply_guard_cannot_save_model_misclassified_genetic_evidence(self):
        final_item = {
            "context_id": "123|Tomato|Prostate cancer",
            "pmid": "123",
            "track": "chatgpt",
            "exposure": "Tomato",
            "cancer": "Prostate cancer",
            "full_text_status": "INCLUDE_META",
            "final_status": "INCLUDE_META",
            "reason": "The model marked this record eligible.",
            "extraction": {"exposure_subtype": "whole_food"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            cache = Path(temporary) / "cache"
            monthly.save_json(
                output / "final_screening.json",
                {"canonical_track": "chatgpt", "results": [final_item]},
            )
            monthly.save_json(
                output / "candidates.json",
                candidate("Two-sample MR of tomato and prostate cancer"),
            )
            with patch.object(monthly, "CACHE_DIR", cache), patch.object(
                monthly, "_validate_current_chatgpt_lineage", return_value={}
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "rerun --stage finalize.*S2-MENDELIAN-RANDOMIZATION",
                ):
                    monthly.apply_to_saved_results(output)
            unchanged = json.loads((output / "final_screening.json").read_text())

        self.assertEqual(unchanged["results"][0], final_item)
        self.assertFalse(cache.exists())
        self.assertFalse((output / "protocol_guard_exclusions.json").exists())

    def test_misclassified_mr_is_excluded_consistently_from_every_final_artifact(self):
        cid = "123|Tomato|Prostate cancer"
        first_payload = {
            "results": [
                {
                    "context_id": cid,
                    "decision": "PASS",
                    "reason": "The report appears relevant to prostate cancer risk.",
                }
            ]
        }
        mistaken_include = {
            "results": [
                {
                    "context_id": cid,
                    "eligibility": "INCLUDE",
                    "exclusion_code": None,
                    "reason": "The model mistakenly considered the estimate eligible.",
                    "extraction": extraction(),
                    "jbi": jbi_answers(yes=6),
                }
            ]
        }
        configured, _, _ = runtimes(
            [first_payload, mistaken_include],
            [first_payload, mistaken_include],
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            reports = Path(temporary) / "reports"
            monthly.save_json(
                output / "candidates.json",
                candidate("Two-sample MR of tomato and prostate cancer"),
            )
            monthly.first_screen(output, runtimes=configured)
            monthly.second_screen(output, runtimes=configured)
            monthly.full_text_review(output, runtimes=configured)
            canonical = monthly.finalize_evidence(output, reports)

            for filename in (
                "chatgpt_final_screening.json",
                "deepseek_final_screening.json",
                "final_screening.json",
            ):
                packet = json.loads((output / filename).read_text())
                self.assertEqual(packet["results"][0]["final_status"], "EXCLUDE", filename)
                self.assertEqual(
                    packet["results"][0]["exclusion_code"],
                    "S2-MENDELIAN-RANDOMIZATION",
                    filename,
                )

            exclusions = json.loads((output / "exclusions.json").read_text())
            self.assertEqual(exclusions["records"][0]["pmid"], "123")
            self.assertEqual(
                exclusions["records"][0]["exclusion_code"],
                "S2-MENDELIAN-RANDOMIZATION",
            )
            discrepancy = json.loads((output / "discrepancies.json").read_text())
            self.assertEqual(
                discrepancy["track_final_status_counts"],
                {"chatgpt": {"EXCLUDE": 1}, "deepseek": {"EXCLUDE": 1}},
            )
            self.assertEqual(discrepancy["discrepancy_count"], 0)
            study_report = (reports / "newStudies.txt").read_text()
            exclusion_report = (reports / "newStudyExclusions.txt").read_text()
            discrepancy_report = (reports / "modelDiscrepancies.txt").read_text()

            self.assertEqual(canonical["results"][0]["screen_layer"], "deterministic_protocol")
            self.assertIn("Final status: EXCLUDE", study_report)
            self.assertIn("S2-MENDELIAN-RANDOMIZATION", exclusion_report)
            self.assertIn('ChatGPT final-status counts: {"EXCLUDE": 1}', discrepancy_report)
            self.assertIn('DeepSeek final-status counts: {"EXCLUDE": 1}', discrepancy_report)
            self.assertNotIn("Final status: INCLUDE_META", study_report)
            self.assertNotIn('"INCLUDE_META": 1', discrepancy_report)
            self.assertFalse((output / "applied_changes.json").exists())

    def test_mocked_dual_workflow_is_independent_and_chatgpt_is_canonical(self):
        cid = "123|Tomato|Prostate cancer"
        gpt_payloads = [
            {"results": [{"context_id": cid, "decision": "PASS", "reason": "The study is relevant."}]},
            {
                "results": [
                    {
                        "context_id": cid,
                        "eligibility": "INCLUDE",
                        "exclusion_code": None,
                        "reason": "The cohort evaluates incident prostate cancer.",
                        "extraction": extraction(),
                        "jbi": jbi_answers(yes=6),
                    }
                ]
            },
        ]
        deepseek_payloads = [
            {"results": [{"context_id": cid, "decision": "PASS", "reason": "The study is relevant."}]},
            {
                "results": [
                    {
                        "context_id": cid,
                        "eligibility": "EXCLUDE",
                        "exclusion_code": "S2-EXPOSURE-MISMATCH",
                        "reason": "The requested exposure is incidental.",
                        "extraction": None,
                        "jbi": None,
                    }
                ]
            },
        ]
        configured, gpt_api, deepseek_api = runtimes(gpt_payloads, deepseek_payloads)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            reports = Path(temporary) / "reports"
            monthly.save_json(output / "candidates.json", candidate())
            monthly.first_screen(output, runtimes=configured)
            monthly.second_screen(output, runtimes=configured)
            monthly.full_text_review(output, runtimes=configured)
            canonical = monthly.finalize_evidence(output, reports)

            self.assertEqual(canonical["canonical_track"], "chatgpt")
            self.assertEqual(canonical["results"][0]["final_status"], "INCLUDE_META")
            deep_final = json.loads((output / "deepseek_final_screening.json").read_text())
            self.assertEqual(deep_final["results"][0]["final_status"], "EXCLUDE")
            discrepancy = json.loads((output / "discrepancies.json").read_text())
            self.assertEqual(discrepancy["discrepancy_count"], 1)
            self.assertTrue((reports / "modelDiscrepancies.txt").is_file())
            self.assertEqual(len(gpt_api.calls), 2)
            self.assertEqual(len(deepseek_api.calls), 2)

    def test_finalize_fails_closed_on_packet_lineage_or_header_tampering(self):
        candidates = {
            "schema_version": 1,
            "publication_date_start": "2026-07-01",
            "publication_date_end": "2026-07-31",
            "articles": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            reports = Path(temporary) / "reports"
            monthly.save_json(output / "candidates.json", candidates)
            monthly.first_screen(output)
            monthly.second_screen(output)
            monthly.full_text_review(output)
            originals = {
                stage: json.loads((output / f"chatgpt_{stage}.json").read_text())
                for stage in ("first_screen", "second_screen", "full_text_review")
            }
            cases = (
                ("first_screen", lambda packet: packet.update(track="deepseek"), "track"),
                ("first_screen", lambda packet: packet.update(model="substituted-model"), "model"),
                ("first_screen", lambda packet: packet.update(protocol_version=0), "protocol_version"),
                ("first_screen", lambda packet: packet.update(input_digest="0" * 64), "candidates.json"),
                (
                    "second_screen",
                    lambda packet: packet.update(source_first_input_digest="0" * 64),
                    "current first-screen digest",
                ),
                (
                    "second_screen",
                    lambda packet: packet.update(
                        results=[
                            {
                                "context_id": "unexpected",
                                "eligibility": "EXCLUDE",
                            }
                        ]
                    ),
                    "context lineage mismatch",
                ),
                (
                    "full_text_review",
                    lambda packet: packet.update(source_second_model="substituted-model"),
                    "current second-screen model",
                ),
                (
                    "full_text_review",
                    lambda packet: packet.update(input_digest="0" * 64),
                    "source lineage",
                ),
            )
            for stage, mutate, error in cases:
                with self.subTest(stage=stage, error=error):
                    changed = copy.deepcopy(originals[stage])
                    mutate(changed)
                    monthly.save_json(output / f"chatgpt_{stage}.json", changed)
                    with self.assertRaisesRegex(screening.ScreeningValidationError, error):
                        monthly.finalize_evidence(output, reports)
                    monthly.save_json(
                        output / f"chatgpt_{stage}.json",
                        originals[stage],
                    )

            final = monthly.finalize_evidence(output, reports)

        self.assertEqual(final["results"], [])
        self.assertEqual(
            set(final["screening_input_digests"]),
            {"first", "second", "full_text"},
        )

    def test_actual_packet_models_are_propagated_to_final_and_cache(self):
        cid = "123|Tomato|Prostate cancer"
        first_payload = {
            "results": [
                {"context_id": cid, "decision": "PASS", "reason": "The study is relevant."}
            ]
        }
        second_payload = {
            "results": [
                {
                    "context_id": cid,
                    "eligibility": "INCLUDE",
                    "exclusion_code": None,
                    "reason": "The cohort evaluates incident prostate cancer.",
                    "extraction": extraction(),
                    "jbi": jbi_answers(yes=6),
                }
            ]
        }
        model_names = {
            "CHATGPT_FIRST_SCREEN_MODEL": screening.CHATGPT_FIRST_MODEL,
            "CHATGPT_SECOND_SCREEN_MODEL": screening.CHATGPT_SECOND_MODEL,
            "CHATGPT_FULL_TEXT_MODEL": screening.CHATGPT_FULL_TEXT_MODEL,
            "DEEPSEEK_FIRST_SCREEN_MODEL": "deepseek-first-deployment",
            "DEEPSEEK_SECOND_SCREEN_MODEL": "deepseek-second-deployment",
            "DEEPSEEK_FULL_TEXT_MODEL": "deepseek-full-deployment",
        }
        gpt_api = FakeResponses([first_payload, second_payload])
        deepseek_api = FakeCompletions([first_payload, second_payload])
        configured = {
            "chatgpt": screening.TrackRuntime(
                "chatgpt",
                SimpleNamespace(responses=gpt_api),
                model_names["CHATGPT_FIRST_SCREEN_MODEL"],
                model_names["CHATGPT_SECOND_SCREEN_MODEL"],
                model_names["CHATGPT_FULL_TEXT_MODEL"],
                "responses",
            ),
            "deepseek": screening.TrackRuntime(
                "deepseek",
                SimpleNamespace(chat=SimpleNamespace(completions=deepseek_api)),
                model_names["DEEPSEEK_FIRST_SCREEN_MODEL"],
                model_names["DEEPSEEK_SECOND_SCREEN_MODEL"],
                model_names["DEEPSEEK_FULL_TEXT_MODEL"],
                "chat_completions",
            ),
        }

        def fake_meta(frame, *_args, **_kwargs):
            return {
                "success": True,
                "studies": frame.to_dict("records"),
                "summary_html": "summary",
                "headline": "headline",
                "screening_stats": {},
                "plot_url": None,
                "funnel_plot_url": None,
                "baujat_plot_url": None,
            }

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ", model_names, clear=False
        ):
            output = Path(temporary) / "run"
            reports = Path(temporary) / "reports"
            cache_dir = Path(temporary) / "cache"
            monthly.save_json(output / "candidates.json", candidate())
            monthly.first_screen(output, runtimes=configured)
            monthly.second_screen(output, runtimes=configured)
            monthly.full_text_review(output, runtimes=configured)
            canonical = monthly.finalize_evidence(output, reports)
            with patch.object(monthly, "CACHE_DIR", cache_dir), patch.object(
                monthly.meta_analysis,
                "perform_meta_analysis",
                side_effect=fake_meta,
            ):
                monthly.apply_to_saved_results(output)
            saved_cache = json.loads(
                (
                    cache_dir
                    / "tomato"
                    / "prostate_cancer_incidence_true_all.json"
                ).read_text()
            )

        expected_chatgpt_models = {
            "first": model_names["CHATGPT_FIRST_SCREEN_MODEL"],
            "second": model_names["CHATGPT_SECOND_SCREEN_MODEL"],
            "full_text": model_names["CHATGPT_FULL_TEXT_MODEL"],
        }
        self.assertEqual(canonical["screening_models"], expected_chatgpt_models)
        self.assertEqual(saved_cache["screening_models"], expected_chatgpt_models)
        self.assertEqual(
            saved_cache["screening_input_digests"],
            canonical["screening_input_digests"],
        )
        self.assertEqual(
            saved_cache["screening_protocol_version"],
            screening.SCREENING_PROTOCOL_VERSION,
        )

    def test_discrepancy_audit_covers_direction_jbi_and_evidence(self):
        gpt_extraction = screening._normalise_extraction(extraction())
        deep_extraction = json.loads(json.dumps(gpt_extraction))
        deep_extraction["comparison_type"] = "Per serving"
        deep_extraction["needs_inversion"] = True
        deep_extraction["p_value"] = 0.01
        deep_extraction["design"] = "Nested case-control"
        deep_extraction["country"] = "China"
        deep_extraction["evidence"]["comparison"]["excerpt"] = "Per serving tomato intake."
        gpt_jbi = screening._strict_jbi(jbi_answers(yes=6))
        deep_jbi = json.loads(json.dumps(gpt_jbi))
        deep_jbi["answers"]["q1"] = "No"
        common = {
            "context_id": "123|Tomato|Prostate cancer",
            "pmid": "123",
            "exposure": "Tomato",
            "cancer": "Prostate cancer",
            "final_status": "INCLUDE_META",
            "full_text_status": "INCLUDE_META",
            "reason": "Eligible evidence.",
        }
        compared = screening.compare_tracks(
            {"results": [{**common, "extraction": gpt_extraction, "jbi": gpt_jbi}]},
            {"results": [{**common, "extraction": deep_extraction, "jbi": deep_jbi}]},
        )

        self.assertEqual(compared["discrepancy_count"], 1)
        fields = set(compared["records"][0]["different_fields"])
        self.assertTrue(
            {
                "comparison_type",
                "needs_inversion",
                "p_value",
                "design",
                "country",
                "jbi_answers",
                "extraction_evidence",
            }.issubset(fields)
        )

    def test_changed_abstract_invalidates_resumable_first_screen(self):
        cid = "123|Tomato|Prostate cancer"
        payloads = [
            {"results": [{"context_id": cid, "decision": "PASS", "reason": "Relevant."}]},
            {"results": [{"context_id": cid, "decision": "FAIL", "reason": "No longer relevant."}]},
        ]
        fake_api = FakeResponses(payloads)
        runtime = screening.TrackRuntime(
            "chatgpt",
            SimpleNamespace(responses=fake_api),
            screening.CHATGPT_FIRST_MODEL,
            screening.CHATGPT_SECOND_MODEL,
            screening.CHATGPT_FULL_TEXT_MODEL,
            "responses",
        )
        first_candidates = candidate()
        changed_candidates = candidate()
        changed_candidates["articles"][0]["abstract"] += " Updated source metadata."
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            screening.run_first_track(output, first_candidates, runtime)
            rerun = screening.run_first_track(output, changed_candidates, runtime)

        self.assertEqual(len(fake_api.calls), 2)
        self.assertEqual(rerun["results"][0]["decision"], "FAIL")
        self.assertIn("input_digest", rerun)

    def test_first_screen_defers_gateway_error_continues_then_revisits(self):
        candidates = candidate_with_pmids("123", "124", "125")
        actions = [
            OSError("Connection error"),
            first_payload("124"),
            first_payload("125"),
            first_payload("123"),
        ]
        fake_api = FakeResponses(actions)
        runtime = screening.TrackRuntime(
            "chatgpt",
            SimpleNamespace(responses=fake_api),
            screening.CHATGPT_FIRST_MODEL,
            screening.CHATGPT_SECOND_MODEL,
            screening.CHATGPT_FULL_TEXT_MODEL,
            "responses",
        )

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            screening.time, "sleep", return_value=None
        ):
            output = Path(temporary)
            packet = screening.run_first_track(
                output, candidates, runtime, batch_size=1
            )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(encoding="utf-8")
            )
            progress_exists = (output / "chatgpt_first_screen_progress.json").exists()

        call_contexts = [
            next(
                context_id
                for context_id in (
                    "123|Tomato|Prostate cancer",
                    "124|Tomato|Prostate cancer",
                    "125|Tomato|Prostate cancer",
                )
                if context_id in call["input"]
            )
            for call in fake_api.calls
        ]
        self.assertEqual(call_contexts, [
            "123|Tomato|Prostate cancer",
            "124|Tomato|Prostate cancer",
            "125|Tomato|Prostate cancer",
            "123|Tomato|Prostate cancer",
        ])
        self.assertEqual(len(packet["results"]), 3)
        self.assertEqual(ledger["pending_count"], 0)
        self.assertEqual([item["event"] for item in ledger["events"]], [
            "deferred",
            "resolved",
        ])
        self.assertEqual(ledger["events"][0]["problem_type"], "gateway")
        self.assertEqual(ledger["events"][0]["pmids"], ["123"])
        self.assertEqual(ledger["events"][0]["exposures"], ["Tomato"])
        self.assertEqual(
            ledger["events"][0]["disease_scopes"], ["Prostate cancer"]
        )
        self.assertFalse(progress_exists)

    def test_strict_validation_problem_is_audited_and_revisited(self):
        candidates = candidate_with_pmids("123", "124", "125")
        fake_api = FakeResponses(
            [
                {"results": []},
                {"results": []},
                first_payload("124"),
                first_payload("125"),
                first_payload("123"),
            ]
        )
        runtime = screening.TrackRuntime(
            "chatgpt",
            SimpleNamespace(responses=fake_api),
            screening.CHATGPT_FIRST_MODEL,
            screening.CHATGPT_SECOND_MODEL,
            screening.CHATGPT_FULL_TEXT_MODEL,
            "responses",
        )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            packet = screening.run_first_track(
                output, candidates, runtime, batch_size=1
            )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(len(packet["results"]), 3)
        self.assertEqual(ledger["pending_count"], 0)
        self.assertEqual(
            [item["event"] for item in ledger["events"]],
            ["deferred", "resolved"],
        )
        deferred = ledger["events"][0]
        self.assertEqual(deferred["problem_type"], "strict_validation")
        self.assertEqual(deferred["pmids"], ["123"])
        self.assertEqual(deferred["exposures"], ["Tomato"])
        self.assertEqual(deferred["disease_scopes"], ["Prostate cancer"])
        self.assertEqual(
            deferred["contexts"],
            [
                {
                    "context_id": "123|Tomato|Prostate cancer",
                    "pmid": "123",
                    "exposure": "Tomato",
                    "disease_scope": "Prostate cancer",
                }
            ],
        )

    def test_unresolved_gateway_batches_are_marked_after_full_stage_sweep(self):
        candidates = candidate_with_pmids("123", "124")
        fake_api = FakeResponses([OSError("Connection error")] * 4)
        runtime = screening.TrackRuntime(
            "chatgpt",
            SimpleNamespace(responses=fake_api),
            screening.CHATGPT_FIRST_MODEL,
            screening.CHATGPT_SECOND_MODEL,
            screening.CHATGPT_FULL_TEXT_MODEL,
            "responses",
        )

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            screening.time, "sleep", return_value=None
        ):
            output = Path(temporary)
            with self.assertRaisesRegex(
                screening.UnresolvedScreeningProblems,
                "2 unresolved deferred screening work unit",
            ):
                screening.run_first_track(output, candidates, runtime, batch_size=1)
            checkpoint = json.loads(
                (output / "chatgpt_first_screen_progress.json").read_text(encoding="utf-8")
            )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(fake_api.calls), 4)
        self.assertEqual(checkpoint["results_by_context"], {})
        self.assertEqual(len(checkpoint["deferred_screening_problems"]), 2)
        self.assertEqual(ledger["pending_count"], 2)
        self.assertEqual(
            [item["event"] for item in ledger["events"]],
            ["deferred", "deferred", "deferred", "deferred"],
        )

    def test_second_screen_uses_the_same_defer_then_revisit_contract(self):
        candidates = candidate_with_pmids("123", "124", "125")
        first_packet = {
            "model": screening.CHATGPT_FIRST_MODEL,
            "input_digest": "a" * 64,
            "results": [
                {
                    "context_id": f"{pmid}|Tomato|Prostate cancer",
                    "decision": "PASS",
                }
                for pmid in ("123", "124", "125")
            ],
        }
        fake_api = FakeResponses(
            [
                OSError("Connection error"),
                second_exclude_payload("124"),
                second_exclude_payload("125"),
                second_exclude_payload("123"),
            ]
        )
        runtime = screening.TrackRuntime(
            "chatgpt",
            SimpleNamespace(responses=fake_api),
            screening.CHATGPT_FIRST_MODEL,
            screening.CHATGPT_SECOND_MODEL,
            screening.CHATGPT_FULL_TEXT_MODEL,
            "responses",
        )

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            screening.time, "sleep", return_value=None
        ):
            output = Path(temporary)
            packet = screening.run_second_track(
                output, candidates, first_packet, runtime, batch_size=1
            )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(packet["results"]), 3)
        self.assertEqual(packet["exclude"], 3)
        self.assertEqual(len(fake_api.calls), 4)
        self.assertEqual(ledger["pending_count"], 0)
        self.assertEqual(
            [item["event"] for item in ledger["events"]],
            ["deferred", "resolved"],
        )

    def test_smaller_resume_batches_resolve_older_grouped_problem_records(self):
        candidates = candidate_with_pmids("123", "124")
        first_packet = {
            "model": screening.CHATGPT_FIRST_MODEL,
            "input_digest": "a" * 64,
            "results": [
                {
                    "context_id": f"{pmid}|Tomato|Prostate cancer",
                    "decision": "PASS",
                }
                for pmid in ("123", "124")
            ],
        }
        failing_runtime = screening.TrackRuntime(
            "chatgpt",
            SimpleNamespace(
                responses=FakeResponses([{"results": []} for _ in range(4)])
            ),
            screening.CHATGPT_FIRST_MODEL,
            screening.CHATGPT_SECOND_MODEL,
            screening.CHATGPT_FULL_TEXT_MODEL,
            "responses",
        )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with self.assertRaises(screening.UnresolvedScreeningProblems):
                screening.run_second_track(
                    output, candidates, first_packet, failing_runtime, batch_size=2
                )

            recovery_runtime = screening.TrackRuntime(
                "chatgpt",
                SimpleNamespace(
                    responses=FakeResponses(
                        [second_exclude_payload("123"), second_exclude_payload("124")]
                    )
                ),
                screening.CHATGPT_FIRST_MODEL,
                screening.CHATGPT_SECOND_MODEL,
                screening.CHATGPT_FULL_TEXT_MODEL,
                "responses",
            )
            packet = screening.run_second_track(
                output, candidates, first_packet, recovery_runtime, batch_size=1
            )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(len(packet["results"]), 2)
        self.assertEqual(ledger["pending_count"], 0)
        self.assertEqual(
            [item["event"] for item in ledger["events"]],
            ["deferred", "deferred", "resolved"],
        )

    def test_first_and_second_prompts_include_the_complete_abstract(self):
        candidates = candidate()
        tail_marker = "TAIL_EVIDENCE_AFTER_THE_OLD_LIMIT"
        candidates["articles"][0]["abstract"] = "x" * 20000 + tail_marker
        expected = screening.expected_contexts(candidates)

        first_prompt = screening._first_prompt(candidates["articles"], expected)
        second_prompt = screening._second_prompt(candidates["articles"], expected)

        self.assertIn(tail_marker, first_prompt)
        self.assertIn(tail_marker, second_prompt)

    def test_zero_candidates_still_writes_complete_stage_packets(self):
        candidates = {
            "schema_version": 1,
            "publication_date_start": "2026-07-01",
            "publication_date_end": "2026-07-31",
            "articles": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            reports = Path(temporary) / "reports"
            monthly.save_json(output / "candidates.json", candidates)
            monthly.first_screen(output)
            monthly.second_screen(output)
            monthly.full_text_review(output)
            final = monthly.finalize_evidence(output, reports)
            deepseek_final_exists = (output / "deepseek_final_screening.json").exists()
            expected_files = [
                f"{track}_{stage}.json"
                for track in screening.DEFAULT_ACTIVE_TRACK_NAMES
                for stage in ("first_screen", "second_screen", "full_text_review")
            ]
            for filename in expected_files:
                packet = json.loads((output / filename).read_text())
                self.assertEqual(packet["results"], [], filename)
                self.assertIn("input_digest", packet, filename)
            validation = json.loads((output / "validation_status.json").read_text())
            discrepancies = json.loads((output / "discrepancies.json").read_text())

        self.assertEqual(final["canonical_track"], "chatgpt")
        self.assertEqual(final["validation_status"], "pending_gateway_enablement")
        self.assertEqual(final["results"], [])
        self.assertEqual(validation["active_tracks"], ["chatgpt"])
        self.assertEqual(
            validation["validation"]["status"], "pending_gateway_enablement"
        )
        self.assertEqual(discrepancies["compared_contexts"], 0)
        self.assertEqual(discrepancies["pending_contexts"], 0)
        self.assertFalse(deepseek_final_exists)

    def test_default_run_constructs_and_calls_only_chatgpt(self):
        cid = "123|Tomato|Prostate cancer"
        gpt_api = FakeResponses(
            [
                {
                    "results": [
                        {
                            "context_id": cid,
                            "decision": "PASS",
                            "reason": "The study may report relevant incidence evidence.",
                        }
                    ]
                },
                {
                    "results": [
                        {
                            "context_id": cid,
                            "eligibility": "INCLUDE",
                            "exclusion_code": None,
                            "reason": "The cohort evaluates incident prostate cancer.",
                            "extraction": extraction(),
                            "jbi": jbi_answers(yes=6),
                        }
                    ]
                },
            ]
        )
        configured_names = []

        def configure(name, _root):
            configured_names.append(name)
            if name == "deepseek":
                raise AssertionError("DeepSeek must not be configured")
            return screening.TrackRuntime(
                "chatgpt",
                SimpleNamespace(responses=gpt_api),
                screening.CHATGPT_FIRST_MODEL,
                screening.CHATGPT_SECOND_MODEL,
                screening.CHATGPT_FULL_TEXT_MODEL,
                "responses",
            )

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            monthly.dual_screening, "configure_runtime", side_effect=configure
        ):
            output = Path(temporary) / "run"
            reports = Path(temporary) / "reports"
            monthly.save_json(output / "candidates.json", candidate())
            monthly.first_screen(output)
            monthly.second_screen(output)
            monthly.full_text_review(output)
            final = monthly.finalize_evidence(output, reports)
            deepseek_final_exists = (output / "deepseek_final_screening.json").exists()

        self.assertEqual(configured_names, ["chatgpt", "chatgpt"])
        self.assertEqual(len(gpt_api.calls), 2)
        self.assertEqual(
            [call["model"] for call in gpt_api.calls],
            ["gpt-5.6-luna", "gpt-5.6-sol"],
        )
        self.assertEqual(final["validation_status"], "pending_gateway_enablement")
        self.assertFalse(deepseek_final_exists)

    def test_all_first_screen_failures_still_finalize_complete_empty_later_stages(self):
        cid = "123|Tomato|Prostate cancer"
        first_failure = {
            "results": [
                {
                    "context_id": cid,
                    "decision": "FAIL",
                    "reason": "The exposure is only incidental.",
                }
            ]
        }
        configured, gpt_api, deepseek_api = runtimes(
            [first_failure], [first_failure]
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            reports = Path(temporary) / "reports"
            monthly.save_json(output / "candidates.json", candidate())
            monthly.first_screen(output, runtimes=configured)
            monthly.second_screen(output)
            monthly.full_text_review(output)
            final = monthly.finalize_evidence(output, reports)

            for track in screening.TRACK_NAMES:
                second = json.loads((output / f"{track}_second_screen.json").read_text())
                full_text = json.loads(
                    (output / f"{track}_full_text_review.json").read_text()
                )
                self.assertEqual(second["results"], [], track)
                self.assertEqual(full_text["results"], [], track)

        self.assertEqual(final["counts"], {"EXCLUDE": 1})
        self.assertEqual(final["results"][0]["screen_layer"], "first")
        self.assertEqual(len(gpt_api.calls), 1)
        self.assertEqual(len(deepseek_api.calls), 1)

    def test_pmc_fetch_failure_is_recorded_without_model_call(self):
        packet = {
            "results": [
                {
                    "context_id": "123|Tomato|Prostate cancer",
                    "pmid": "123",
                    "pmcid": "PMC123",
                    "eligibility": "INCLUDE",
                    "needs_full_text": True,
                    "full_text_eligible": True,
                }
            ]
        }

        def failing_fetch(_):
            raise OSError("temporary NCBI failure")

        with tempfile.TemporaryDirectory() as temporary:
            sources, requests = screening.prepare_full_text_sources(
                Path(temporary), [packet], failing_fetch, lambda raw: raw.decode()
            )
        self.assertEqual(requests, 1)
        self.assertEqual(sources["PMC123"]["status"], "fetch_failed")
        self.assertIn("OSError", sources["PMC123"]["error"])

    def test_prevalidated_pmc_source_bypasses_network_and_binds_receipt(self):
        packet = {
            "results": [
                {
                    "context_id": "123|Tomato|Prostate cancer",
                    "pmid": "123",
                    "pmcid": "PMC123",
                    "eligibility": "INCLUDE",
                    "needs_full_text": True,
                    "full_text_eligible": True,
                }
            ]
        }
        full_text = "Complete methods, results, and tables. " * 100

        def forbidden_fetch(_):
            raise AssertionError("prevalidated source must not be refetched")

        provenance = {
            "text": full_text,
            "source_format": "pmc_html",
            "source_route": "html",
            "raw_sha256": "a" * 64,
            "validation_digest": "b" * 64,
            "receipt_ledger_sha256": "c" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            sources, requests = screening.prepare_full_text_sources(
                output,
                [packet],
                forbidden_fetch,
                lambda raw: raw.decode(),
                prevalidated_sources={"PMC123": provenance},
            )
            status = json.loads((output / "pmc_source_status.json").read_text())
        self.assertEqual(requests, 0)
        self.assertEqual(sources["PMC123"]["status"], "full_text")
        self.assertTrue(sources["PMC123"]["prevalidated"])
        manifest = status["source_manifest"]["PMC123"]
        self.assertTrue(manifest["prevalidated"])
        self.assertEqual(manifest["source_format"], "pmc_html")
        self.assertEqual(manifest["raw_sha256"], "a" * 64)
        self.assertEqual(manifest["receipt_ledger_sha256"], "c" * 64)

    def test_prevalidated_exact_pdf_route_is_accepted_atomically(self):
        packet = {
            "results": [
                {
                    "context_id": "14720329|Genistein|Prostate cancer",
                    "pmid": "14720329",
                    "pmcid": "PMC11160038",
                    "eligibility": "INCLUDE",
                    "needs_full_text": True,
                    "full_text_eligible": True,
                }
            ]
        }
        provenance = {
            "text": "Complete PDF methods, results, and discussion. " * 100,
            "source_format": "pmc_pdf",
            "source_route": "pdf",
            "raw_sha256": "a" * 64,
            "validation_digest": "b" * 64,
            "receipt_ledger_sha256": "c" * 64,
        }
        fetcher = Mock(side_effect=AssertionError("sealed PDF must not be refetched"))
        with tempfile.TemporaryDirectory() as temporary:
            sources, requests = screening.prepare_full_text_sources(
                Path(temporary),
                [packet],
                fetcher,
                lambda raw: raw.decode(),
                prevalidated_sources={"PMC11160038": provenance},
                required_prevalidated_pmcids={"PMC11160038"},
            )
        self.assertEqual(requests, 0)
        self.assertEqual(sources["PMC11160038"]["source_format"], "pmc_pdf")
        self.assertEqual(sources["PMC11160038"]["source_route"], "pdf")
        fetcher.assert_not_called()

    def test_repair_loader_uses_completed_seal_after_r3_progress_advances(self):
        audit = monthly.audit_blocked_pmc_sources
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            staging = output / audit.STAGING_DIRNAME
            staging.mkdir()
            manifest_sources = {}
            for pmcid, pmid in audit.EXPECTED_BLOCKERS.items():
                text = "Complete official article methods and results. " * 80
                (staging / f"{pmcid}.txt").write_text(text + "\n")
                manifest_sources[pmcid] = {
                    "pmid": pmid,
                    "route": "html",
                    "source_format": audit.PMC_HTML_FORMAT,
                    "raw_sha256": "a" * 64,
                    "validation": {"valid": True},
                }
            monthly.save_json(
                staging / "manifest.json",
                {
                    "schema_version": audit.STAGING_MANIFEST_SCHEMA_VERSION,
                    "sources": manifest_sources,
                },
            )
            ledger_path = output / audit.LEDGER_FILENAME
            monthly.save_json(ledger_path, {"all_sources_complete": True})
            ledger_sha256 = monthly.file_sha256(ledger_path)
            monthly.save_json(
                output / audit.LEDGER_VALIDATION_FILENAME,
                {
                    "status": "validated",
                    "all_sources_complete": True,
                    "production_block_checked": True,
                    "validated_source_count": len(audit.EXPECTED_BLOCKERS),
                    "unresolved_source_count": 0,
                    "decision": "all_sources_validated_stage_ready",
                    "ledger_sha256": ledger_sha256,
                },
            )
            # This represents a later resumable R3 state; the loader must rely
            # on the completed repair seal rather than require the old block.
            monthly.save_json(
                output / "chatgpt_full_text_review_progress.json",
                {"status": "interrupted_after_source_preparation"},
            )
            with patch.object(
                monthly, "is_exact_pmc_repair_scope", return_value=True
            ), patch.object(
                audit, "validate_audit_ledger", return_value={"status": "validated"}
            ) as validate:
                sources = monthly.validated_pmc_repair_sources(output)
            self.assertEqual(set(sources), set(audit.EXPECTED_BLOCKERS))
            validate.assert_called_once()
            self.assertFalse(validate.call_args.kwargs["require_blocked_production"])

    def test_exact_repair_scope_without_staging_blocks_before_reuse_or_fetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monthly.save_json(
                output / "chatgpt_full_text_review.json",
                {"stale_generic_packet": True},
            )
            fetcher = Mock(side_effect=AssertionError("generic fetch must not run"))
            with patch.object(
                monthly, "is_exact_pmc_repair_scope", return_value=True
            ), patch.object(monthly, "fetch_pmc_full_text", fetcher):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "lacks its all-or-none PMC staging seal",
                ):
                    monthly.full_text_review(output)
            fetcher.assert_not_called()

    def test_exact_repair_scope_rejects_custom_fetcher_and_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(
                monthly, "is_exact_pmc_repair_scope", return_value=True
            ):
                with self.assertRaisesRegex(RuntimeError, "forbids custom PMC"):
                    monthly.full_text_review(
                        output,
                        fetcher=lambda _pmcid: b"article",
                        parser=lambda raw: raw.decode(),
                    )

    def test_required_prevalidated_source_cannot_fall_back_to_cache_or_fetch(self):
        context = {
            "context_id": "123|Tomato|Prostate cancer",
            "pmid": "123",
            "pmcid": "PMC123",
            "eligibility": "INCLUDE",
            "extraction": screening._normalise_extraction(extraction()),
            "jbi": screening._strict_jbi(jbi_answers(yes=5)),
        }
        packet = {"results": [context]}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            full_text = output / "full_text"
            full_text.mkdir()
            (full_text / "PMC123.txt").write_text("cached generic text " * 200)
            fetcher = Mock(side_effect=AssertionError("generic fetch must not run"))
            with self.assertRaisesRegex(
                screening.ScreeningValidationError,
                "required before generic retrieval",
            ):
                screening.prepare_full_text_sources(
                    output,
                    [packet],
                    fetcher,
                    lambda raw: raw.decode(),
                    required_prevalidated_pmcids={"PMC123"},
                )
            fetcher.assert_not_called()

    def test_prevalidated_source_requires_official_route_format_and_hashes(self):
        context = {
            "context_id": "123|Tomato|Prostate cancer",
            "pmid": "123",
            "pmcid": "PMC123",
            "eligibility": "INCLUDE",
            "extraction": screening._normalise_extraction(extraction()),
            "jbi": screening._strict_jbi(jbi_answers(yes=5)),
        }
        packet = {"results": [context]}
        base = {
            "text": "complete source text " * 200,
            "source_format": "pmc_html",
            "source_route": "html",
            "raw_sha256": "a" * 64,
            "validation_digest": "b" * 64,
            "receipt_ledger_sha256": "c" * 64,
        }
        invalid_records = [
            {**base, "source_format": "not_official", "source_route": "invented"},
            {**base, "raw_sha256": "x", "validation_digest": "y"},
        ]
        for invalid in invalid_records:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                fetcher = Mock(side_effect=AssertionError("generic fetch must not run"))
                with self.assertRaises(screening.ScreeningValidationError):
                    screening.prepare_full_text_sources(
                        Path(temporary),
                        [packet],
                        fetcher,
                        lambda raw: raw.decode(),
                        prevalidated_sources={"PMC123": invalid},
                        required_prevalidated_pmcids={"PMC123"},
                    )
                fetcher.assert_not_called()

    def test_exact_repair_manifest_rejects_generic_blocker_provenance(self):
        repair_sources = {
            pmcid: {
                "text": "complete official article " * 150,
                "source_format": "pmc_html",
                "source_route": "html",
                "raw_sha256": "a" * 64,
                "validation_digest": "b" * 64,
                "receipt_ledger_sha256": "c" * 64,
            }
            for pmcid in monthly.audit_blocked_pmc_sources.EXPECTED_BLOCKERS
        }
        generic = {
            pmcid: {
                "status": "full_text",
                "text": source["text"],
                "error": "",
                "prevalidated": False,
            }
            for pmcid, source in repair_sources.items()
        }
        manifest = screening.full_text_source_manifest(generic, generic)
        with self.assertRaisesRegex(
            screening.ScreeningValidationError,
            "lacks sealed PMC repair provenance",
        ):
            monthly.require_exact_pmc_repair_manifest(manifest, repair_sources)

    def test_finalize_and_apply_lineage_call_exact_repair_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            report = output / "report"
            for filename in (
                "candidates.json",
                "chatgpt_first_screen.json",
                "chatgpt_second_screen.json",
                "chatgpt_full_text_review.json",
            ):
                monthly.save_json(output / filename, {"present": True})
            blocker = Mock(side_effect=RuntimeError("sealed repair required"))
            with patch.object(
                monthly, "_screening_track_names", return_value=("chatgpt",)
            ), patch.object(monthly, "require_exact_pmc_repair_packet", blocker):
                with self.assertRaisesRegex(RuntimeError, "sealed repair required"):
                    monthly.finalize_evidence(output, report)
                with self.assertRaisesRegex(
                    RuntimeError, "screening lineage validation failed"
                ):
                    monthly._validate_current_chatgpt_lineage(output, {}, {})
            self.assertEqual(blocker.call_count, 2)

    def test_incomplete_gated_pmc_sources_block_without_terminal_packet(self):
        context = {
            "context_id": "123|Tomato|Prostate cancer",
            "pmid": "123",
            "pmcid": "PMC123",
            "title": "Example",
            "exposure": "Tomato",
            "cancer": "Prostate cancer",
            "eligibility": "INCLUDE",
            # A stored flag cannot disable the recomputed Fair-JBI gate.
            "needs_full_text": False,
            "extraction": screening._normalise_extraction(extraction()),
            "jbi": screening._strict_jbi(jbi_answers(yes=5)),
        }
        second_packet = {
            "model": screening.CHATGPT_SECOND_MODEL,
            "input_digest": "a" * 64,
            "results": [context],
        }
        configured, gpt_api, _deepseek_api = runtimes([], [])
        runtime = configured["chatgpt"]
        for status, text, error in (
            ("partial_record", "front matter", ""),
            ("fetch_failed", "", "RuntimeError: fetch failed"),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                monthly.save_json(
                    output / "chatgpt_full_text_review.json", {"stale": True}
                )
                with self.assertRaisesRegex(
                    screening.ScreeningValidationError,
                    "complete verified PMC full text",
                ):
                    screening.run_full_text_track(
                        output,
                        second_packet,
                        {
                            "PMC123": {
                                "status": status,
                                "text": text,
                                "error": error,
                            }
                        },
                        runtime,
                    )
                self.assertFalse(
                    (output / "chatgpt_full_text_review.json").exists()
                )
                progress = json.loads(
                    (
                        output / "chatgpt_full_text_review_progress.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    progress["status"], "blocked_incomplete_pmc_source"
                )
                self.assertEqual(progress["source_failures"][0]["status"], status)
                self.assertEqual(progress["results_by_context"], {})
        self.assertEqual(gpt_api.calls, [])

    def test_no_pmcid_context_remains_sol_finalized_outside_r3(self):
        context = {
            "context_id": "123|Tomato|Prostate cancer",
            "pmid": "123",
            "pmcid": "",
            "title": "Example",
            "exposure": "Tomato",
            "cancer": "Prostate cancer",
            "eligibility": "INCLUDE",
            "needs_full_text": True,
            "extraction": screening._normalise_extraction(extraction()),
            "jbi": screening._strict_jbi(jbi_answers(yes=5)),
        }
        self.assertFalse(screening.is_full_text_candidate(context))
        second_packet = {
            "model": screening.CHATGPT_SECOND_MODEL,
            "input_digest": "a" * 64,
            "results": [context],
        }
        configured, gpt_api, _deepseek_api = runtimes([], [])
        with tempfile.TemporaryDirectory() as temporary:
            packet = screening.run_full_text_track(
                Path(temporary),
                second_packet,
                {},
                configured["chatgpt"],
            )
        self.assertEqual(packet["contexts"], 0)
        self.assertEqual(packet["results"], [])
        self.assertEqual(packet["usage"]["calls"], 0)
        self.assertEqual(gpt_api.calls, [])

    def test_ordinary_apply_rejects_tampered_gated_luna_result(self):
        candidates = candidate()
        candidates["articles"][0]["pmcid"] = "PMC123"
        cid = "123|Tomato|Prostate cancer"
        first = {
            "schema_version": 2,
            "track": "chatgpt",
            "model": screening.CHATGPT_FIRST_MODEL,
            "protocol_version": screening.SCREENING_PROTOCOL_VERSION,
            "input_digest": screening.first_input_digest(
                candidates, screening.CHATGPT_FIRST_MODEL
            ),
            "results": [
                {
                    "context_id": cid,
                    "decision": "PASS",
                    "reason": "The context is directly relevant.",
                }
            ],
        }
        expected = screening.expected_contexts(candidates)[cid]
        second_item = {
            **expected,
            "eligibility": "INCLUDE",
            "exclusion_code": None,
            "reason": "The abstract evidence is eligible.",
            "extraction": screening._normalise_extraction(extraction(cases=None)),
            "jbi": screening._strict_jbi(jbi_answers(yes=5)),
        }
        second = {
            "schema_version": 2,
            "track": "chatgpt",
            "model": screening.CHATGPT_SECOND_MODEL,
            "protocol_version": screening.SCREENING_PROTOCOL_VERSION,
            "source_first_model": first["model"],
            "source_first_input_digest": first["input_digest"],
            "input_digest": screening.second_input_digest(
                candidates, first, screening.CHATGPT_SECOND_MODEL
            ),
            "results": [second_item],
        }
        recovered = {
            field: None
            for field in screening.EXTRACTION_FIELDS
            if field
            not in {
                "exposure_subtype",
                "exposure_measurement_type",
                "needs_inversion",
                "exposure_classification_ambiguous",
                "wald_inputs_same_estimate",
            }
        }
        recovered.update(
            {
                "cases": 140,
                "exposure_subtype": "mixed_or_unclear",
                "exposure_measurement_type": "unclear",
                "exposure_classification_ambiguous": False,
                "wald_inputs_same_estimate": False,
                "needs_inversion": False,
                "evidence": {field: None for field in screening.EVIDENCE_FIELDS},
            }
        )
        recovered["evidence"]["cases"] = evidence_entry(
            "A total of 140 incident prostate cancers were documented.",
            source="full_text",
            url="https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
        )
        full_payload = {
            "results": [
                {
                    "context_id": cid,
                    "status": "INCLUDE",
                    "exclusion_code": None,
                    "reason": "Luna completed the full-text appraisal.",
                    "extraction": recovered,
                    "jbi": jbi_answers(yes=6),
                }
            ]
        }
        configured, _gpt_api, _deepseek_api = runtimes([full_payload], [])
        expected_models = {
            "first": screening.CHATGPT_FIRST_MODEL,
            "second": screening.CHATGPT_SECOND_MODEL,
            "full_text": screening.CHATGPT_FULL_TEXT_MODEL,
        }
        full_text_source = (
            "Complete PMC methods and results. " * 80
            + " A total of 140 incident prostate cancers were documented."
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            full = screening.run_full_text_track(
                output,
                second,
                {
                    "PMC123": {
                        "status": "full_text",
                        "text": full_text_source,
                        "error": "",
                    }
                },
                configured["chatgpt"],
            )
            lineage = screening.validate_track_lineage(
                candidates,
                first,
                second,
                full,
                "chatgpt",
                expected_models,
            )
            final = screening.finalize_track(
                candidates, first, second, full, "chatgpt"
            )
            final = monthly.apply_reclassification_wording_boundaries(
                candidates, final
            )
            for item in final["results"]:
                item["direct_exposure"] = item["exposure"]
                item["inherited_exposures"] = monthly.inherited_exposures(
                    item["exposure"]
                )
            final.update(lineage)
            final["canonical_track"] = "chatgpt"
            full["results"][0]["third_round_status"] = "UNRESOLVED_SOURCE"
            for name, packet in (
                ("candidates.json", candidates),
                ("chatgpt_first_screen.json", first),
                ("chatgpt_second_screen.json", second),
                ("chatgpt_full_text_review.json", full),
                ("final_screening.json", final),
            ):
                monthly.save_json(output / name, packet)
            with self.assertRaisesRegex(
                RuntimeError, "lacks a completed gpt-5.6-luna"
            ):
                monthly.apply_to_saved_results(output)
            self.assertFalse((output / "applied_changes.json").exists())

    def test_luna_full_text_can_recover_missing_cases_and_upgrade_fair_jbi(self):
        base_extraction = screening._normalise_extraction(extraction(cases=None))
        context = {
            "context_id": "123|Tomato|Prostate cancer",
            "pmid": "123",
            "pmcid": "PMC123",
            "published_date": "2026-07-15",
            "title": "Example",
            "authors": ["Example A"],
            "journal": "Journal",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            "exposure": "Tomato",
            "cancer": "Prostate cancer",
            "outcome": "Incidence",
            "eligibility": "INCLUDE",
            "exclusion_code": None,
            "reason": "The article is eligible but the case count is missing.",
            "missing_key_fields": ["cases"],
            "needs_full_text": True,
            "extraction": base_extraction,
            "jbi": screening._strict_jbi(jbi_answers(yes=5)),
        }
        recovered = {
            field: None
            for field in screening.EXTRACTION_FIELDS
            if field not in {
                "exposure_subtype", "exposure_measurement_type", "needs_inversion",
                "exposure_classification_ambiguous", "wald_inputs_same_estimate",
            }
        }
        recovered.update(
            {
                "cases": 140,
                "exposure_subtype": "mixed_or_unclear",
                "exposure_measurement_type": "unclear",
                "exposure_classification_ambiguous": False,
                "wald_inputs_same_estimate": False,
                "needs_inversion": False,
                "evidence": {field: None for field in screening.EVIDENCE_FIELDS},
            }
        )
        recovered["evidence"]["cases"] = evidence_entry(
            "A total of 140 incident prostate cancers were documented.",
            source="full_text",
            url="https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
        )
        payload = {
            "results": [
                {
                    "context_id": context["context_id"],
                    "status": "INCLUDE",
                    "exclusion_code": None,
                    "reason": "The full text supplies the missing case count and adequate methods.",
                    "extraction": recovered,
                    "jbi": jbi_answers(yes=6),
                }
            ]
        }
        fake_api = FakeResponses([OSError("Connection error"), payload])
        runtime = screening.TrackRuntime(
            "chatgpt",
            SimpleNamespace(responses=fake_api),
            screening.CHATGPT_FIRST_MODEL,
            screening.CHATGPT_SECOND_MODEL,
            screening.CHATGPT_FULL_TEXT_MODEL,
            "responses",
        )
        second_packet = {"results": [context]}
        full_text = (
            "x" * 180001
            + " A total of 140 incident prostate cancers were documented."
        )
        sources = {
            "PMC123": {
                "status": "full_text",
                "text": full_text,
                "error": "",
            }
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            screening.time, "sleep", return_value=None
        ):
            output = Path(temporary)
            result = screening.run_full_text_track(
                output, second_packet, sources, runtime
            )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(fake_api.calls), 2)
        self.assertEqual(fake_api.calls[0]["model"], "gpt-5.6-luna")
        self.assertEqual(ledger["pending_count"], 0)
        self.assertEqual(
            [item["event"] for item in ledger["events"]],
            ["deferred", "resolved"],
        )
        self.assertEqual(result["results"][0]["full_text_status"], "INCLUDE_META")
        self.assertEqual(result["results"][0]["extraction"]["cases"], 140)
        self.assertEqual(result["results"][0]["jbi"]["grade"], "Moderate")
        self.assertEqual(result["results"][0]["extraction"]["exposure_subtype"], "whole_food")
        self.assertIn(
            "A total of 140 incident prostate cancers were documented.",
            fake_api.calls[0]["input"],
        )
        self.assertEqual(
            result["results"][0]["full_text_characters_reviewed"],
            len(full_text),
        )

    def test_nonforce_rerun_reuses_valid_full_text_packets_without_refetch(self):
        context = {
            "context_id": "123|Tomato|Prostate cancer",
            "pmid": "123",
            "pmcid": "PMC123",
            "published_date": "2026-07-15",
            "title": "Example",
            "authors": ["Example A"],
            "journal": "Journal",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            "exposure": "Tomato",
            "cancer": "Prostate cancer",
            "outcome": "Incidence",
            "eligibility": "INCLUDE",
            "exclusion_code": None,
            "reason": "The case count is missing from the abstract.",
            "missing_key_fields": ["cases"],
            "needs_full_text": True,
            "extraction": screening._normalise_extraction(extraction(cases=None)),
            "jbi": screening._strict_jbi(jbi_answers(yes=5)),
        }
        recovered = {
            field: None
            for field in screening.EXTRACTION_FIELDS
            if field not in {
                "exposure_subtype", "exposure_measurement_type", "needs_inversion",
                "exposure_classification_ambiguous", "wald_inputs_same_estimate",
            }
        }
        recovered.update(
            {
                "cases": 140,
                "exposure_subtype": "mixed_or_unclear",
                "exposure_measurement_type": "unclear",
                "exposure_classification_ambiguous": False,
                "wald_inputs_same_estimate": False,
                "needs_inversion": False,
                "evidence": {field: None for field in screening.EVIDENCE_FIELDS},
            }
        )
        recovered["evidence"]["cases"] = evidence_entry(
            "A total of 140 incident prostate cancers were documented.",
            source="full_text",
            url="https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
        )
        full_payload = {
            "results": [
                {
                    "context_id": context["context_id"],
                    "status": "INCLUDE",
                    "exclusion_code": None,
                    "reason": "The full text supplies the missing case count.",
                    "extraction": recovered,
                    "jbi": jbi_answers(yes=6),
                }
            ]
        }
        configured, gpt_api, deepseek_api = runtimes([full_payload], [full_payload])
        full_text = (
            "Methods and results. " * 150
            + "A total of 140 incident prostate cancers were documented."
        )
        self.assertGreaterEqual(len(full_text), 2000)
        sources = {"PMC123": {"status": "full_text", "text": full_text, "error": ""}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            for name in screening.TRACK_NAMES:
                second_packet = {
                    "schema_version": 2,
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "track": name,
                    "model": configured[name].second_model,
                    "protocol_version": screening.SCREENING_PROTOCOL_VERSION,
                    "input_digest": ("a" if name == "chatgpt" else "b") * 64,
                    "results": [context],
                }
                monthly.save_json(output / f"{name}_second_screen.json", second_packet)
                screening.run_full_text_track(
                    output,
                    second_packet,
                    sources,
                    configured[name],
                )
            stale_record = {
                "unit_key": screening._problem_unit_key(
                    "chatgpt",
                    "full_text",
                    [context["context_id"]],
                ),
                "track": "chatgpt",
                "stage": "full_text",
                "context_ids": [context["context_id"]],
                "location": "PMCID PMC123",
            }
            screening._write_problem_event(output, stale_record, "deferred")
            self.assertFalse(
                (output / "chatgpt_full_text_review_progress.json").exists()
            )
            before = {
                name: (output / f"{name}_full_text_review.json").read_text()
                for name in screening.TRACK_NAMES
            }
            fetch_calls = []

            def outage_fetch(pmcid):
                fetch_calls.append(pmcid)
                raise OSError("simulated NCBI outage")

            resumed = monthly.full_text_review(
                output,
                runtimes=configured,
                fetcher=outage_fetch,
                parser=lambda raw: raw.decode(),
            )
            after = {
                name: (output / f"{name}_full_text_review.json").read_text()
                for name in screening.TRACK_NAMES
            }
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(fetch_calls, [])
        self.assertEqual(before, after)
        self.assertEqual(resumed["reused_tracks"], ["chatgpt", "deepseek"])
        self.assertEqual(resumed["ncbi_requests"], 0)
        self.assertEqual(resumed["results"][0]["extraction"]["cases"], 140)
        self.assertEqual(len(gpt_api.calls), 1)
        self.assertEqual(len(deepseek_api.calls), 1)
        self.assertEqual(ledger["pending_count"], 0)
        self.assertEqual(
            [item["event"] for item in ledger["events"]],
            ["deferred", "resolved"],
        )

    def test_wrapper_invalidates_reuse_for_added_and_changed_manual_adjudications(self):
        context_id = "123|Tomato|Prostate cancer"
        context = {
            "context_id": context_id,
            "pmid": "123",
            "pmcid": "PMC123",
            "title": "Example",
            "exposure": "Tomato",
            "cancer": "Prostate cancer",
            "eligibility": "INCLUDE",
            "needs_full_text": True,
            "missing_key_fields": [],
            "extraction": screening._normalise_extraction(extraction()),
            "jbi": screening._strict_jbi(jbi_answers(yes=5)),
        }
        preserved_context_id = "123|Calcium|Prostate cancer"
        preserved_context = {
            **context,
            "context_id": preserved_context_id,
            "exposure": "Calcium",
        }
        luna_extraction = {
            field: None
            for field in screening.EXTRACTION_FIELDS
            if field
            not in {
                "exposure_subtype",
                "exposure_measurement_type",
                "needs_inversion",
                "exposure_classification_ambiguous",
                "wald_inputs_same_estimate",
            }
        }
        luna_extraction.update(
            {
                "cases": 132,
                "exposure_subtype": "mixed_or_unclear",
                "exposure_measurement_type": "unclear",
                "exposure_classification_ambiguous": False,
                "wald_inputs_same_estimate": False,
                "needs_inversion": False,
                "evidence": {field: None for field in screening.EVIDENCE_FIELDS},
            }
        )
        luna_extraction["evidence"]["cases"] = evidence_entry(
            "During follow-up, 132 prostate cancers occurred.",
            source="full_text",
            url="https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
        )
        def luna_payload_for(*items):
            return {
                "results": [
                    {
                        "context_id": item["context_id"],
                        "status": "INCLUDE",
                        "exclusion_code": None,
                        "reason": "Luna completed the full-text appraisal.",
                        "extraction": copy.deepcopy(luna_extraction),
                        "jbi": jbi_answers(yes=5),
                    }
                    for item in items
                ]
            }
        configured, gpt_api, _deepseek_api = runtimes(
            [
                luna_payload_for(context),
                luna_payload_for(context, preserved_context),
                luna_payload_for(context),
            ],
            [],
        )
        runtime = configured["chatgpt"]
        second_packet = {
            "schema_version": 2,
            "created_at": "2026-08-01T00:00:00+00:00",
            "track": "chatgpt",
            "model": runtime.second_model,
            "protocol_version": screening.SCREENING_PROTOCOL_VERSION,
            "input_digest": "a" * 64,
            "results": [context, preserved_context],
        }
        full_text = (
            "Complete article methods and results. " * 80
            + " During follow-up, 132 prostate cancers occurred."
        )
        sources = {
            "PMC123": {"status": "full_text", "text": full_text, "error": ""}
        }
        source_manifest = screening.full_text_source_manifest(sources, {"PMC123"})
        existing_result = {
            **context,
            "final_status": "ELIGIBLE_NOT_POOLABLE",
            "full_text_status": "ELIGIBLE_NOT_POOLABLE",
            "reason": "The Fair appraisal requires further review.",
            "pmc_source_status": "full_text",
            "full_text_characters_reviewed": len(full_text),
            "pmc_source_sha256": source_manifest["PMC123"]["sha256"],
            "third_round_status": "COMPLETED",
            "third_round_model": runtime.full_text_model,
        }
        preserved_result = {
            **existing_result,
            **preserved_context,
            "reason": "The existing non-manual result remains valid.",
        }
        existing_packet = {
            "schema_version": 2,
            "created_at": "2026-08-01T00:00:00+00:00",
            "track": "chatgpt",
            "model": runtime.full_text_model,
            "protocol_version": screening.SCREENING_PROTOCOL_VERSION,
            "input_digest": screening.full_text_input_digest(
                second_packet,
                runtime.full_text_model,
                source_manifest,
            ),
            "source_second_model": second_packet["model"],
            "source_second_input_digest": second_packet["input_digest"],
            "source_manifest": source_manifest,
            "source_manifest_digest": screening.stable_json_digest(
                source_manifest
            ),
            "contexts": 2,
            "status_counts": {"ELIGIBLE_NOT_POOLABLE": 2},
            "manual_full_text_adjudications": 0,
            "manual_full_text_adjudications_digest": None,
            "usage": {
                **screening.empty_usage(runtime.full_text_model),
                "calls": 1,
            },
            "results": [existing_result, preserved_result],
        }

        def manual_packet(recorded_at, reason):
            return {
                "schema_version": 1,
                "recorded_at": recorded_at,
                "adjudications": [
                    {
                        "context_id": context_id,
                        "full_text_status": "EXCLUDE",
                        "exclusion_code": "S2-NO-TARGET-ESTIMATE",
                        "reason": reason,
                    }
                ],
            }

        fetch_calls = []

        def fetcher(pmcid):
            fetch_calls.append(pmcid)
            return b"<article/>"

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monthly.save_json(output / "chatgpt_second_screen.json", second_packet)
            monthly.save_json(
                output / "chatgpt_full_text_review.json", existing_packet
            )
            monthly.save_json(
                output / screening.FULL_TEXT_MANUAL_ADJUDICATIONS_FILENAME,
                manual_packet(
                    "2026-08-21T01:20:00+08:00",
                    "The full text lacks a target ratio estimate.",
                ),
            )
            first = monthly.full_text_review(
                output,
                runtimes={"chatgpt": runtime},
                fetcher=fetcher,
                parser=lambda _raw: full_text,
            )
            first_digest = first["input_digest"]
            legacy_packet = json.loads(
                (output / "chatgpt_full_text_review.json").read_text(
                    encoding="utf-8"
                )
            )
            legacy_packet.pop("manual_full_text_adjudications")
            legacy_packet.pop("manual_full_text_adjudications_digest")
            legacy_packet["input_digest"] = existing_packet["input_digest"]
            monthly.save_json(
                output / "chatgpt_full_text_review.json",
                legacy_packet,
            )
            unchanged_legacy = monthly.full_text_review(
                output,
                runtimes={"chatgpt": runtime},
                fetcher=fetcher,
                parser=lambda _raw: full_text,
            )
            monthly.save_json(
                output / screening.FULL_TEXT_MANUAL_ADJUDICATIONS_FILENAME,
                manual_packet(
                    "2026-08-22T01:20:00+08:00",
                    "The adjudicated full text confirms no target ratio estimate.",
                ),
            )
            second = monthly.full_text_review(
                output,
                runtimes={"chatgpt": runtime},
                fetcher=fetcher,
                parser=lambda _raw: full_text,
            )

        self.assertEqual(first["reused_tracks"], [])
        self.assertEqual(unchanged_legacy["reused_tracks"], [])
        self.assertEqual(second["reused_tracks"], [])
        self.assertEqual(first["manual_full_text_adjudications"], 1)
        self.assertEqual(second["manual_full_text_adjudications"], 1)
        first_results = {item["context_id"]: item for item in first["results"]}
        second_results = {item["context_id"]: item for item in second["results"]}
        self.assertEqual(first_results[context_id]["final_status"], "EXCLUDE")
        self.assertEqual(
            first_results[preserved_context_id]["reason"],
            "The existing non-manual result remains valid.",
        )
        self.assertEqual(
            second_results[context_id]["reason"],
            "The adjudicated full text confirms no target ratio estimate.",
        )
        self.assertNotEqual(existing_packet["input_digest"], first_digest)
        self.assertNotEqual(first_digest, second["input_digest"])
        self.assertEqual(fetch_calls, ["PMC123", "PMC123", "PMC123"])
        self.assertEqual(len(gpt_api.calls), 3)

    def test_related_tomato_subtypes_are_retained_without_automatic_pooling(self):
        predisposed = screening.apply_whole_food_subtype_disposition(
            {
                "exposure": "Tomato",
                "final_status": "INCLUDE_META",
                "full_text_status": "INCLUDE_META",
                "reason": "The component evidence is otherwise poolable.",
                "extraction": {"exposure_subtype": "dietary_component"},
            }
        )
        self.assertEqual(predisposed["final_status"], "ELIGIBLE_SUBTYPE_REVIEW")

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monthly.save_json(
                output / "final_screening.json",
                {
                    "canonical_track": "chatgpt",
                    "results": [
                        {
                            "context_id": "123|Tomato|Prostate cancer",
                            "pmid": "123",
                            "track": "chatgpt",
                            "exposure": "Tomato",
                            "cancer": "Prostate cancer",
                            "full_text_status": "ELIGIBLE_SUBTYPE_REVIEW",
                            "final_status": "ELIGIBLE_SUBTYPE_REVIEW",
                            "extraction": {"exposure_subtype": "human_biomarker"},
                        }
                    ],
                },
            )
            with patch.object(
                monthly, "_validate_current_chatgpt_lineage", return_value={}
            ):
                changes = monthly.apply_to_saved_results(output)
            review = json.loads((output / "exposure_subtype_review.json").read_text())

        self.assertEqual(changes["changes"], [])
        self.assertEqual(review["records"][0]["pmid"], "123")
        self.assertIn("not automatically pooled", review["reason"])

    def test_unknown_whole_food_measurement_does_not_block_pooling(self):
        item = {
            "exposure": "Tomato",
            "final_status": "INCLUDE_META",
            "full_text_status": "INCLUDE_META",
            "extraction": {
                "exposure_subtype": "mixed_or_unclear",
                "exposure_measurement_type": "unclear",
                "exposure_classification_ambiguous": True,
            },
        }
        self.assertEqual(
            screening.apply_whole_food_subtype_disposition(item)["final_status"],
            "INCLUDE_META",
        )
        item["extraction"]["exposure_measurement_type"] = "human_biospecimen"
        self.assertEqual(
            screening.apply_whole_food_subtype_disposition(item)["final_status"],
            "ELIGIBLE_SUBTYPE_REVIEW",
        )

    def test_pepper_component_subtype_is_quarantined_before_final_artifacts(self):
        predisposed = screening.apply_whole_food_subtype_disposition(
            {
                "exposure": "Pepper (Capsicum)",
                "final_status": "INCLUDE_META",
                "full_text_status": "INCLUDE_META",
                "reason": "The component evidence is otherwise poolable.",
                "extraction": {
                    "exposure_subtype": "human_biomarker",
                    "exposure_measurement_type": "human_biospecimen",
                },
            }
        )

        self.assertEqual(predisposed["final_status"], "ELIGIBLE_SUBTYPE_REVIEW")
        self.assertEqual(predisposed["full_text_status"], "ELIGIBLE_SUBTYPE_REVIEW")
        self.assertIn("Pepper (Capsicum)", predisposed["reason"])

    def test_tomato_whole_food_label_conflicting_with_biomarker_is_quarantined_before_final(self):
        candidates = candidate()
        expected = screening.expected_contexts(candidates)
        cid, base = next(iter(expected.items()))
        raw_extraction = extraction()
        raw_extraction["comparison_type"] = "Highest versus lowest serum lycopene"
        raw_extraction["exposure_measurement_type"] = "human_biospecimen"
        raw_extraction["evidence"]["comparison"] = evidence_entry(
            "Highest versus lowest serum lycopene concentrations."
        )
        raw_extraction["evidence"]["exposure_measurement"] = evidence_entry(
            "Serum lycopene concentrations were measured in blood samples."
        )
        normalized = screening._normalise_extraction(raw_extraction)
        second_item = {
            **{
                key: value
                for key, value in base.items()
                if key != "protocol_exclusion" and not key.startswith("_")
            },
            "eligibility": "INCLUDE",
            "exclusion_code": None,
            "reason": "The study reports complete eligible evidence.",
            "missing_key_fields": [],
            "needs_full_text": False,
            "full_text_eligible": False,
            "extraction": normalized,
            "jbi": screening._strict_jbi(jbi_answers(yes=6)),
        }
        final = screening.finalize_track(
            candidates,
            {
                "results": [
                    {
                        "context_id": cid,
                        "decision": "PASS",
                        "reason": "The study is relevant.",
                    }
                ]
            },
            {"results": [second_item]},
            {"results": []},
            "chatgpt",
        )

        result = final["results"][0]
        self.assertEqual(result["final_status"], "ELIGIBLE_SUBTYPE_REVIEW")
        self.assertEqual(result["full_text_status"], "ELIGIBLE_SUBTYPE_REVIEW")
        self.assertIn(
            "whole_food_conflicts_with_human_biospecimen_measurement",
            result["subtype_review_reasons"],
        )
        self.assertNotEqual(result["reason"], result["screening_reason_before_subtype_guard"])

    def test_inversion_and_prostate_case_estimate_are_applied_in_saved_rows(self):
        inverted = extraction(cases=132)
        inverted.update(
            {
                "effect_size": 2.0,
                "ci_lower": 1.5,
                "ci_upper": 2.5,
                "needs_inversion": True,
            }
        )
        inverted["evidence"]["effect_size"] = evidence_entry(
            "The adjusted hazard ratio was 2.0."
        )
        inverted["evidence"]["confidence_interval"] = evidence_entry(
            "The 95% confidence interval was 1.5 to 2.5."
        )
        normalized = screening._normalise_extraction(inverted)
        item = {
            "pmid": "123",
            "pmcid": "PMC123",
            "published_date": "2026-07-15",
            "title": "Example",
            "authors": ["Example A"],
            "journal": "Journal",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            "cancer": "Prostate cancer",
            "extraction": normalized,
            "jbi": screening._strict_jbi(jbi_answers(yes=6)),
        }
        row = monthly.study_row(item)
        self.assertAlmostEqual(row["Effect Size"], 0.5)
        self.assertAlmostEqual(row["Lower CI"], 0.4)
        self.assertAlmostEqual(row["Upper CI"], 2 / 3)
        self.assertTrue(row["effect_inverted"])
        merged = screening._merge_extractions(
            normalized,
            {
                "comparison_type": None,
                "needs_inversion": False,
                "evidence": {},
            },
        )
        self.assertTrue(merged["needs_inversion"])

        no_cases = extraction(cases=None)
        no_cases_item = {**item, "extraction": screening._normalise_extraction(no_cases)}
        self.assertEqual(monthly.study_row(no_cases_item)["Estimated Cases"], 132)
        for scope in ({"cancer": "Aggressive prostate cancer"},
                      {"subcategory": "aggressive_prostate_cancer"}):
            aggressive_row = monthly.study_row({**no_cases_item, **scope})
            self.assertIsNone(aggressive_row["Estimated Cases"])
            self.assertEqual(aggressive_row["Sample Size"], no_cases["total_n"])
            reported_row = monthly.study_row({**item, **scope})
            self.assertEqual(reported_row["Cases"], 132)

    def test_study_row_does_not_late_impute_p_value_from_ci(self):
        raw = extraction()
        raw["p_value"] = None
        raw["evidence"]["p_value"] = None
        raw["wald_inputs_same_estimate"] = False
        normalized = screening._normalise_extraction(raw)
        self.assertIsNone(normalized["p_value"])
        item = {
            "pmid": "123",
            "pmcid": "PMC123",
            "published_date": "2026-07-15",
            "title": "Example",
            "authors": ["Example A"],
            "journal": "Journal",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            "cancer": "Prostate cancer",
            "extraction": normalized,
            "jbi": screening._strict_jbi(jbi_answers(yes=6)),
        }

        with patch.object(
            monthly.meta_analysis,
            "p_value_from_effect_ci",
            side_effect=AssertionError("late p-value imputation is forbidden"),
        ) as late_imputation:
            row = monthly.study_row(item)

        self.assertIsNone(row["P Value"])
        late_imputation.assert_not_called()

    def test_all_exposure_slugs_match_monthly_and_web_contract(self):
        exposures = json.loads(monthly.EXPOSURES_FILE.read_text(encoding="utf-8"))
        for exposure in exposures:
            self.assertEqual(
                monthly.safe_context_part(exposure),
                webapp.safe_path_component(exposure),
                exposure,
            )
        script = (monthly.REPO_ROOT / "static" / "script.js").read_text(encoding="utf-8")
        self.assertIn("replace(/[^a-z0-9]/g, '_')", script)

    def test_production_exposure_vocabulary_is_curated(self):
        exposures = json.loads(monthly.EXPOSURES_FILE.read_text(encoding="utf-8"))
        synonyms = json.loads(monthly.SYNONYMS_FILE.read_text(encoding="utf-8"))
        self.assertIn("Tomato", exposures)
        self.assertIn("Pepper (Capsicum)", exposures)
        self.assertIn("Ketogenic diet", exposures)
        self.assertNotIn("Capsicum", exposures)
        self.assertEqual(synonyms["Tomato"]["downstream"], "")
        self.assertNotIn("lycopene", monthly.exposure_terms()["Tomato"])
        self.assertIn("Solanum lycopersicum", synonyms["Tomato"]["core"])
        self.assertIn("Capsicum annuum", synonyms["Pepper (Capsicum)"]["core"])
        self.assertNotIn("black pepper", synonyms["Pepper (Capsicum)"]["core"].casefold())
        self.assertIn("modified Atkins diet", synonyms["Ketogenic diet"]["core"])

    def test_strict_validation_rejects_duplicate_contexts(self):
        expected = screening.expected_contexts(candidate())
        cid = next(iter(expected))
        with self.assertRaises(screening.ScreeningValidationError):
            screening.validate_first(
                {
                    "results": [
                        {"context_id": cid, "decision": "PASS", "reason": "Relevant."},
                        {"context_id": cid, "decision": "PASS", "reason": "Relevant."},
                    ]
                },
                expected,
            )


if __name__ == "__main__":
    unittest.main()
