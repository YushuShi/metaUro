import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import dual_screening
from scripts import run_claude_gemini_parallel_review as review


class ClaudeGeminiParallelReviewTests(unittest.TestCase):
    def test_dense_r2_and_r3_contexts_are_split_deterministically(self):
        expected = {
            f"1|Exposure {index:02d}|Bladder cancer": {
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
        remaining = review._make_r2_units(
            expected, articles, set(units[0]["context_ids"]), batch_size=1
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
        self.assertEqual(
            review.parse_args(["--r3-max-contexts-per-unit", "3"])
            .r3_max_contexts_per_unit,
            3,
        )

    def test_r3_exact_legacy_pmc_url_alias_is_audited_but_others_stay_strict(self):
        context_id = "1|Avocado|Bladder cancer"
        canonical = "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/"
        legacy = "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/"

        def payload(url):
            return {
                "results": [
                    {
                        "context_id": context_id,
                        "extraction": {
                            "evidence": {
                                "sample_size": {
                                    "excerpt": "The cohort included 100 people.",
                                    "source": "full_text",
                                    "url": url,
                                    "location": "Methods",
                                }
                            }
                        },
                        "additional_extractions": [],
                    }
                ]
            }

        def strict_stub(candidate, _expected, *, source_text, source_identifier):
            evidence = candidate["results"][0]["extraction"]["evidence"]
            dual_screening._normalise_evidence(
                evidence,
                required_source="full_text",
                source_text=source_text,
                source_identifier=source_identifier,
            )
            return [{"context_id": context_id}]

        with patch.object(
            review.dual_screening,
            "validate_full_text",
            side_effect=strict_stub,
        ):
            canonical_result = review._validate_r3_payload_with_canonical_pmc_url(
                payload(canonical),
                {context_id: {}},
                source_text="The cohort included 100 people.",
                pmcid="PMC123",
            )
            raw_legacy = payload(legacy)
            legacy_result = review._validate_r3_payload_with_canonical_pmc_url(
                raw_legacy,
                {context_id: {}},
                source_text="The cohort included 100 people.",
                pmcid="PMC123",
            )
            for rejected in (
                "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC999/",
                "https://example.org/articles/PMC123/",
            ):
                with self.subTest(rejected=rejected), self.assertRaises(
                    dual_screening.ScreeningValidationError
                ):
                    review._validate_r3_payload_with_canonical_pmc_url(
                        payload(rejected),
                        {context_id: {}},
                        source_text="The cohort included 100 people.",
                        pmcid="PMC123",
                    )

        self.assertEqual(
            canonical_result[0]["r3_provider_url_normalization_events"], []
        )
        self.assertEqual(
            raw_legacy["results"][0]["extraction"]["evidence"]
            ["sample_size"]["url"],
            legacy,
        )
        self.assertEqual(
            legacy_result[0]["r3_provider_url_normalization_events"],
            [
                {
                    "field": "extraction.evidence.sample_size.url",
                    "before": legacy,
                    "after": canonical,
                    "rule": "exact_legacy_official_pmc_url_alias",
                }
            ],
        )

        prompt = review._r3_prompt(
            {
                context_id: {
                    "context_id": context_id,
                    "pmid": "1",
                    "pmcid": "PMC123",
                    "title": "Title",
                    "exposure": "Avocado",
                    "cancer": "Bladder cancer",
                    "jbi": {"grade": "Fair"},
                }
            },
            "Full text",
        )
        self.assertIn(f"url exactly to `{canonical}`", prompt)
        self.assertIn(
            "Use additional_extractions only when the "
            "(gender, exposure_measurement_type) tuple differs",
            prompt,
        )
        self.assertIn(
            "exposure_subtype difference alone is not a distinct stratum",
            prompt,
        )
        self.assertIn(
            "Every result, whether INCLUDE or EXCLUDE, requires one non-empty "
            "single-sentence reason",
            prompt,
        )
        self.assertIn("S2-NO-TARGET-ESTIMATE", prompt)
        self.assertIn("Do not invent or translate exclusion codes", prompt)

    def test_invalid_cached_legacy_pmc_url_is_revalidated_without_a_model_call(self):
        context_id = "1|Avocado|Bladder cancer"
        source_sha256 = "a" * 64
        unit_key = review._work_unit_key(
            "gemini_r3", [context_id], source_sha256
        )
        legacy = "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/"
        raw_payload = {
            "results": [
                {
                    "context_id": context_id,
                    "extraction": {
                        "evidence": {
                            "sample_size": {
                                "excerpt": "The cohort included 100 people.",
                                "source": "full_text",
                                "url": legacy,
                                "location": "Methods",
                            }
                        }
                    },
                    "additional_extractions": [],
                }
            ]
        }
        output_text = json.dumps(raw_payload)
        receipt_id = "cached-receipt"

        def strict_stub(candidate, _expected, *, source_text, source_identifier):
            evidence = candidate["results"][0]["extraction"]["evidence"]
            dual_screening._normalise_evidence(
                evidence,
                required_source="full_text",
                source_text=source_text,
                source_identifier=source_identifier,
            )
            return [{"context_id": context_id}]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            cache_dir = output / "model_response_cache" / "gemini_r3"
            cache_dir.mkdir(parents=True)
            cache_path = cache_dir / f"{unit_key}_0001.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "receipt_id": receipt_id,
                        "validation_status": "invalid",
                        "validation_error": (
                            "Evidence URL for sample_size must use "
                            "pmc.ncbi.nlm.nih.gov"
                        ),
                        "scientific_result_accepted": False,
                        "validation_contract_version": "legacy",
                        "output_text": output_text,
                        "output_sha256": review.hashlib.sha256(
                            output_text.encode("utf-8")
                        ).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            (output / "gateway_receipts.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "receipts": [
                            {
                                "receipt_id": receipt_id,
                                "received_at": "2026-09-03T00:00:00+00:00",
                                "stage": "gemini_r3",
                                "context_ids": [context_id],
                                "exposure_weights": {"Avocado": 1},
                                "usage": {},
                                "response_cost_usd": "0.01",
                                "cost_header_status": "reported",
                                "scientific_result_accepted": False,
                                "validation_status": "invalid",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                review.dual_screening,
                "validate_full_text",
                side_effect=strict_stub,
            ), patch.object(
                review,
                "COST_PATH",
                root / "ClaudeGeminiCost.txt",
            ):
                results = review._call_gateway_json(
                    SimpleNamespace(),
                    output_dir=output,
                    stage="gemini_r3",
                    model=review.GEMINI_R3_MODEL,
                    system="system",
                    prompt="prompt",
                    context_ids=[context_id],
                    exposure_weights={"Avocado": 1},
                    source_sha256=source_sha256,
                    validator=lambda candidate: (
                        review._validate_r3_payload_with_canonical_pmc_url(
                            candidate,
                            {context_id: {}},
                            source_text="The cohort included 100 people.",
                            pmcid="PMC123",
                        )
                    ),
                )
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            receipts = json.loads(
                (output / "gateway_receipts.json").read_text(encoding="utf-8")
            )["receipts"]

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["context_id"], context_id)
        self.assertEqual(
            results[0]["r3_provider_url_normalization_events"][0]["before"],
            legacy,
        )
        self.assertEqual(cached["output_text"], output_text)
        self.assertEqual(cached["validation_status"], "valid")
        self.assertTrue(cached["scientific_result_accepted"])
        self.assertEqual(
            cached["validation_contract_version"],
            review.MODEL_CACHE_VALIDATION_CONTRACT_VERSION,
        )
        self.assertEqual(
            cached["validation_history"][-1]["validation_status"], "invalid"
        )
        self.assertEqual(receipts[0]["validation_status"], "valid")
        self.assertTrue(receipts[0]["scientific_result_accepted"])

    def test_blank_titles_use_sealed_official_pubmed_repair_only_when_needed(self):
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
            self.assertEqual(receipt["target_pmids"], ["123"])
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

            no_blank = {"articles": [{"pmid": "1", "title": "Already present"}]}
            with patch.object(
                review.monthly_evidence_update,
                "entrez_fetch",
                side_effect=AssertionError("no blank title means no PubMed request"),
            ):
                _packet, no_blank_receipt = review._r2_candidates_with_official_metadata(
                    output / "no-blank", no_blank
                )
            self.assertEqual(no_blank_receipt["request_count"], 0)

    def test_catalog_union_and_cost_file_have_exactly_one_row_per_exposure(self):
        exposures = review.catalog_exposures()
        self.assertEqual(len(exposures), 449)
        self.assertEqual(len(set(exposures)), 449)
        self.assertNotIn("Diet pills", exposures)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cost_path = root / "ClaudeGeminiCost.txt"
            with patch.object(review, "COST_PATH", cost_path):
                review._write_cost_files(root / "output")
            rows = cost_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 449)
        self.assertEqual([row.split("\t", 1)[0] for row in rows], exposures)
        self.assertTrue(all("claude_r2_calls=0" in row for row in rows))
        self.assertTrue(all("gemini_r3_calls=0" in row for row in rows))

    def test_prepare_uses_exact_metapee_precedence_and_complete_dispositions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                manifest = review.prepare_r1_lineage(root / "parallel")
            candidates = json.loads(
                (root / "parallel" / "r1_pass_candidates.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(manifest["catalog_exposure_count"], 449)
        self.assertTrue(manifest["source_scope_union_matches_current_catalogs"])
        self.assertEqual(manifest["source_scope_union_exposures"], 449)
        self.assertEqual(len(manifest["exposure_dispositions"]), 449)
        self.assertEqual(
            sum(manifest["exposure_disposition_summary"].values()), 449
        )
        self.assertEqual(manifest["selected_contexts"], 23795)
        self.assertEqual(manifest["selected_pass"], 2126)
        self.assertEqual(manifest["selected_fail"], 21669)
        self.assertEqual(candidates["candidate_contexts"], 2126)
        self.assertEqual(candidates["candidate_pmids"], 1114)
        self.assertEqual(manifest["decision_conflict_count"], 2)
        self.assertEqual(
            [source["source"] for source in manifest["sources"]],
            [
                "all_history_through_2026-08-22",
                "all_history_through_2026-08-29",
                "vitamin_b6_plp_diet_v2_all_history_through_2026-08-29",
                "included_reclassification_2026-08-29",
                "bacterial_alias_delta_through_2026-08-29",
            ],
        )
        expected_hashes = {
            "all_history_through_2026-08-22": "29c664ede5e3cd185a085a333c8db48b2a6dc4fddaa3e1860364dae54fb34a81",
            "all_history_through_2026-08-29": "345925890f351686fdc6646ad46d19cbd5918699de5d2a92df3cbf9b0f930856",
            "vitamin_b6_plp_diet_v2_all_history_through_2026-08-29": "475975705a2e799ce70b6ad1597eb736ffb018e108ceab58e00dea66c66e5d0d",
            "included_reclassification_2026-08-29": "6fc11805a483eaaf45f58e46b796094e0ad2b96df8f38766b266c6c35b7cac9d",
            "bacterial_alias_delta_through_2026-08-29": "d1764a6abc74015e6b0a2026a17c3b057e6a35fa0740bb7f2b1da30014207386",
        }
        self.assertEqual(
            {item["source"]: item["first_screen_sha256"] for item in manifest["sources"]},
            expected_hashes,
        )
        resolutions = {
            item["context_id"]: (item["selected_source"], item["selected_decision"])
            for item in manifest["decision_conflicts"]
        }
        self.assertEqual(
            resolutions["26892894|Oleic acid|Kidney and renal pelvis cancer"],
            ("all_history_through_2026-08-29", "PASS"),
        )
        self.assertEqual(
            resolutions["36360204|Oleic acid|Bladder cancer"],
            ("all_history_through_2026-08-29", "PASS"),
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
            "1|Vitamin B6|Bladder cancer": {
                "pmid": "1",
                "exposure": "Vitamin B6",
                "cancer": "Bladder cancer",
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
            "extraction": {**complete, "p_value": None},
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

    def test_parallel_gate_checks_all_extraction_strata_after_wald(self):
        complete = {
            "effect_size": 1.1,
            "effect_type": "RR",
            "ci_lower": 0.9,
            "ci_upper": 1.3,
            "p_value": 0.5,
            "total_n": 100,
            "cases": 10,
            "comparison_type": "high versus low",
        }
        item = {
            "eligibility": "INCLUDE",
            "pmcid": "PMC1",
            "extraction": complete,
            "additional_extractions": [{**complete, "cases": None}],
            "jbi": {"grade": "Moderate"},
        }
        reasons = review.r3_gate_reasons(item)
        self.assertEqual(len(reasons), 1)
        self.assertIn("cases", reasons[0])

    def test_strict_jats_requires_exact_top_level_identity_and_direct_body(self):
        paragraph = "This cohort reports urinary cancer incidence and quantitative evidence. " * 30
        body = (
            f"<p>{paragraph} First section.</p>"
            f"<p>{paragraph} Second section.</p>"
            f"<p>{paragraph} Third section.</p>"
        )
        valid = f"""
        <pmc-articleset>
          <article>
            <front><article-meta>
              <article-id pub-id-type="pmc">PMC123</article-id>
              <article-id pub-id-type="pmid">456</article-id>
            </article-meta></front>
            <body><sec><title>Results</title>{body}</sec></body>
          </article>
        </pmc-articleset>
        """.encode()
        text, validation = review._validate_pmc_jats(
            valid, expected_pmcid="PMC123", expected_pmid="456"
        )
        self.assertIn("urinary cancer incidence", text)
        self.assertTrue(validation["identity_verified"])
        self.assertTrue(validation["direct_article_body_verified"])

        with self.assertRaises(review.OperationalProblem):
            review._validate_pmc_jats(
                valid, expected_pmcid="PMC123", expected_pmid="999"
            )

        nested_body_only = f"""
        <pmc-articleset>
          <article>
            <front><article-meta>
              <article-id pub-id-type="pmc">PMC123</article-id>
              <article-id pub-id-type="pmid">456</article-id>
            </article-meta></front>
            <sub-article><body>{body}</body></sub-article>
          </article>
        </pmc-articleset>
        """.encode()
        with self.assertRaises(review.OperationalProblem):
            review._validate_pmc_jats(
                nested_body_only, expected_pmcid="PMC123", expected_pmid="456"
            )

    def test_cached_pmc_xml_is_revalidated_without_network(self):
        paragraph = "This direct article body contains substantive epidemiologic evidence. " * 30
        body = (
            f"<p>{paragraph} First section.</p>"
            f"<p>{paragraph} Second section.</p>"
            f"<p>{paragraph} Third section.</p>"
        )
        raw = f"""
        <article>
          <front><article-meta>
            <article-id pub-id-type="pmc">123</article-id>
            <article-id pub-id-type="pmid">456</article-id>
          </article-meta></front>
          <body><sec>{body}</sec></body>
        </article>
        """.encode()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            xml_path = output / "verified_pmc_sources" / "PMC123" / "article.xml"
            review._save_bytes(xml_path, raw)
            with patch.object(
                review.monthly_evidence_update,
                "fetch_pmc_full_text",
                side_effect=AssertionError("network must not be used for a valid cache"),
            ):
                text, receipt = review._verified_pmc_text(
                    output, "PMC123", "456"
                )
            self.assertTrue(text.startswith("This direct article body"))
            self.assertEqual(receipt["source_route"], "cached_jats_revalidation")
            self.assertTrue(
                (output / "verified_pmc_sources" / "PMC123" / "receipt.json").exists()
            )

    def test_jats_preformat_ocr_is_preserved_as_complete_body_text(self):
        ocr = "Historical full article OCR sentence with urinary cancer evidence. " * 120
        xml = (
            '<article><front><article-meta>'
            '<article-id pub-id-type="pmc">PMC123</article-id>'
            '<article-id pub-id-type="pmid">123</article-id>'
            '<title-group><article-title>Historical article</article-title></title-group>'
            '</article-meta></front><body><preformat>'
            + ocr
            + "</preformat></body></article>"
        ).encode("utf-8")
        text, validation = review._validate_pmc_jats(
            xml, expected_pmcid="PMC123", expected_pmid="123"
        )
        self.assertEqual(validation["body_paragraphs"], 0)
        self.assertEqual(validation["body_preformats"], 1)
        self.assertIn("Historical full article OCR sentence", text)

    def test_split_pmcid_units_share_one_exact_canonical_source_proof(self):
        text = ("Complete official article text with quantitative evidence. " * 120) + "\n"
        first_receipt = review._new_source_receipt(
            pmcid="PMC123",
            pmid="123",
            text=text,
            raw=b"first official source",
            source="Official PMC",
            source_route="efetch",
            source_format="jats_xml",
            validation={"valid": True},
        )
        equivalent_receipt = review._new_source_receipt(
            pmcid="PMC123",
            pmid="123",
            text=text,
            raw=b"second dynamic official source",
            source="Official PMC",
            source_route="efetch",
            source_format="jats_xml",
            validation={"valid": True},
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
                "context_id": "123|Vitamin B6|Bladder cancer",
                "pmcid": "PMC123",
                **review._r3_source_binding(first),
            }
            review._validate_r3_source_proofs(
                [row], {"PMC123": first}, exact_coverage=True
            )

            changed_text = text + "scientific change"
            changed_receipt = review._new_source_receipt(
                pmcid="PMC123",
                pmid="123",
                text=changed_text,
                raw=b"changed official source",
                source="Official PMC",
                source_route="efetch",
                source_format="jats_xml",
                validation={"valid": True},
            )
            with self.assertRaises(review.OperationalProblem):
                review._register_r3_source(
                    output, "PMC123", "123", changed_text, changed_receipt
                )

    def test_source_loader_uses_strict_official_pdf_after_jats_and_html_fail(self):
        text = ("Verified PDF article text with epidemiologic evidence. " * 130) + "\n"
        validation = {
            "identity_verified": True,
            "direct_article_body_verified": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            review.save_json(
                output / "r1_pass_candidates.json",
                {
                    "articles": [
                        {
                            "pmid": "123",
                            "title": "Exact official title",
                            "doi": "10.1/example",
                        }
                    ]
                },
            )
            with patch.object(
                review.monthly_evidence_update, "configure_entrez"
            ), patch.object(
                review.monthly_evidence_update,
                "fetch_pmc_full_text",
                return_value=b"short-jats",
            ), patch.object(
                review,
                "_validate_pmc_jats",
                side_effect=review.OperationalProblem("short JATS"),
            ), patch.object(
                review,
                "_fetch_official_pmc_html",
                return_value=(b"short-html", {"official": True}),
            ), patch.object(
                review,
                "_validate_pmc_html",
                side_effect=review.OperationalProblem("short HTML"),
            ), patch.object(
                review,
                "_fetch_official_pmc_pdf",
                return_value=(b"strict-pdf-bytes", {"downloaded": True}),
            ) as fetch_pdf, patch.object(
                review,
                "_validate_pmc_pdf",
                return_value=(text, validation),
            ) as validate_pdf:
                loaded_text, receipt = review._verified_pmc_text(
                    output, "PMC123", "123"
                )
        self.assertEqual(loaded_text, text)
        self.assertEqual(receipt["source_format"], "pmc_pdf")
        fetch_pdf.assert_called_once()
        validate_pdf.assert_called_once()

    def test_unverified_pmc_source_is_operational_problem_and_never_calls_gemini(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            review.save_json(
                output / "claude_r2_results.json",
                {
                    "results": [
                        {
                            "context_id": "456|Vitamin B6|Bladder cancer",
                            "pmid": "456",
                            "pmcid": "PMC123",
                            "title": "Study",
                            "exposure": "Vitamin B6",
                            "cancer": "Bladder cancer",
                            "eligibility": "INCLUDE",
                            "full_text_eligible": True,
                            "needs_full_text": True,
                        }
                    ]
                },
            )

            def reject_source(_output, _pmcid, _pmid):
                raise review.OperationalProblem("strict JATS identity mismatch")

            class NoCalls:
                def __getattr__(self, _name):
                    raise AssertionError("Gemini must not run without verified full text")

            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                packet = review.run_r3(
                    output,
                    workers=1,
                    max_units=1,
                    client=NoCalls(),
                    source_loader=reject_source,
                )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(encoding="utf-8")
            )
            exclusions = json.loads(
                (output / "gemini_r3_exclusions.json").read_text(encoding="utf-8")
            )
        self.assertEqual(packet["completed_contexts"], 0)
        self.assertEqual(ledger["unresolved_count"], 1)
        self.assertEqual(ledger["unresolved"][0]["stage"], "gemini_r3")
        self.assertEqual(exclusions["exclusions"], [])

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
                    context_ids=["1|Vitamin B6|Bladder cancer"],
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
                            "context_id": "1|Vitamin B6|Bladder cancer",
                            "eligibility": "EXCLUDE",
                            "exclusion_code": "S2-WRONG-OUTCOME",
                            "reason": "The report does not evaluate incident bladder cancer.",
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
                            "cancer": "Bladder cancer",
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
                    "context_id": "1|Vitamin B6|Bladder cancer",
                    "pmid": "1",
                    "exposure": "Vitamin B6",
                    "cancer": "Bladder cancer",
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
        self.assertEqual(packet["status"], "complete")
        self.assertEqual(receipt["receipt_count"], 1)
        self.assertEqual(
            receipt["receipts"][0]["dispatch_work_unit_key"],
            review._work_unit_key(
                "claude_r2", ["1|Vitamin B6|Bladder cancer"]
            ),
        )
        self.assertEqual(receipt["receipts"][0]["api_style"], "chat_completions")
        self.assertNotIn("authorization", receipt["receipts"][0]["safe_gateway_headers"])
        self.assertEqual(exclusions["exclusions"][0]["reason"], "The report does not evaluate incident bladder cancer.")
        vitamin_b6 = next(row for row in cost_rows if row.startswith("Vitamin B6\t"))
        self.assertIn("claude_r2_input_tokens=100", vitamin_b6)
        self.assertIn("claude_r2_cost_usd=0.2500000000", vitamin_b6)
        self.assertEqual(len(raw_api.calls), 1)
        self.assertIn("messages", raw_api.calls[0])
        self.assertNotIn("response_format", raw_api.calls[0])
        self.assertNotIn("text", raw_api.calls[0])

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
            return [{"context_id": "1|Vitamin B6|Bladder cancer"}]

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
                    context_ids=["1|Vitamin B6|Bladder cancer"],
                    exposure_weights={"Vitamin B6": 1},
                    validator=validator,
                )
                costs = json.loads(
                    (output / "cost_accounting.json").read_text(encoding="utf-8")
                )
            receipts = json.loads(
                (output / "gateway_receipts.json").read_text(encoding="utf-8")
            )["receipts"]
        self.assertEqual(results[0]["context_id"], "1|Vitamin B6|Bladder cancer")
        self.assertEqual(len(receipts), 2)
        self.assertEqual(
            [receipt["scientific_result_accepted"] for receipt in receipts],
            [False, True],
        )
        row = next(item for item in costs["rows"] if item["exposure"] == "Vitamin B6")
        self.assertEqual(row["claude_r2_calls"], 2)
        self.assertEqual(row["claude_r2_cost_usd"], "0.3000000000")
        self.assertEqual(costs["invalid_or_unaccepted_receipts_retained_for_cost"], 1)

    def test_transport_failure_is_counted_with_unknown_billing(self):
        class RawAPI:
            def create(self, **_kwargs):
                raise TimeoutError("gateway response timed out")

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(with_raw_response=RawAPI())
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            output.mkdir()
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                with self.assertRaises(review.OperationalProblem) as caught:
                    review._call_gateway_json(
                        client,
                        output_dir=output,
                        stage="claude_r2",
                        model=review.CLAUDE_R2_MODEL,
                        system="system",
                        prompt="prompt",
                        context_ids=["1|Vitamin B6|Bladder cancer"],
                        exposure_weights={"Vitamin B6": 1},
                        validator=lambda _payload: [],
                    )
                costs = json.loads(
                    (output / "cost_accounting.json").read_text(encoding="utf-8")
                )
            receipts = json.loads(
                (output / "gateway_receipts.json").read_text(encoding="utf-8")
            )["receipts"]
        self.assertEqual(
            caught.exception.billing_status,
            "unknown_after_gateway_transport_failure",
        )
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            receipts[0]["billing_status"],
            "unknown_after_gateway_transport_failure",
        )
        row = next(item for item in costs["rows"] if item["exposure"] == "Vitamin B6")
        self.assertEqual(row["claude_r2_calls"], 1)
        self.assertEqual(row["claude_r2_unknown_billing_calls"], 1)
        self.assertIsNone(row["claude_r2_cost_usd"])

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

    def test_missing_gateway_cost_is_written_as_na_without_estimation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            review.save_json(
                output / "gateway_receipts.json",
                {
                    "receipts": [
                        {
                            "stage": "claude_r2",
                            "exposure_weights": {"Vitamin B6": 1},
                            "usage": {"input_tokens": 12, "output_tokens": 3},
                            "response_cost_usd": None,
                        }
                    ]
                },
            )
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                review._write_cost_files(output)
            row = next(
                line
                for line in (root / "ClaudeGeminiCost.txt").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.startswith("Vitamin B6\t")
            )
        self.assertIn("claude_r2_cost_usd=NA", row)
        self.assertIn("claude_r2_cost_unavailable_calls=1", row)

    def test_dispatch_is_worker_bounded_and_does_not_swallow_keyboard_interrupt(self):
        active_counts = []
        completed = []

        def process(unit):
            self.assertTrue(active_counts and active_counts[-1] > 0)
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

        interrupted_journal = []
        with self.assertRaises(KeyboardInterrupt):
            review._run_units(
                [1],
                lambda _unit: (_ for _ in ()).throw(KeyboardInterrupt()),
                lambda _key, _results: None,
                lambda _unit, _error: self.fail("KeyboardInterrupt was swallowed"),
                workers=1,
                on_active=lambda values: interrupted_journal.append(list(values)),
            )
        self.assertEqual(interrupted_journal[-1], [1])

    def test_problem_resolution_uses_completed_context_coverage_not_batch_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            contexts = [
                "1|Vitamin B6|Bladder cancer",
                "2|Vitamin B6|Bladder cancer",
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
            r2 = {
                "source_first_input_digest": "r1",
                "results": [
                    {
                        "context_id": "1|Vitamin B6|Bladder cancer",
                        "pmid": "1",
                        "pmcid": "",
                        "exposure": "Vitamin B6",
                        "cancer": "Bladder cancer",
                        "eligibility": "INCLUDE",
                        "needs_full_text": True,
                        "full_text_eligible": False,
                        "parallel_r3_gate_reasons": ["JBI remains Fair after Claude R2"],
                    }
                ],
            }
            review.save_json(output / "r1_pass_packet.json", r1)
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

    def test_strict_pmc_source_failure_defers_only_after_two_attempts(self):
        context_id = "1|Vitamin B6|Bladder cancer"
        work_key = review._work_unit_key("gemini_r3", [context_id])
        source_error = review.OfficialPMCSourceUnavailable(
            "No strict complete official PMC source for PMC123/1; all routes failed"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            output.mkdir()
            r1 = {"input_digest": "r1", "pass": 1}
            r2 = {
                "source_first_input_digest": "r1",
                "results": [
                    {
                        "context_id": context_id,
                        "pmid": "1",
                        "pmcid": "PMC123",
                        "exposure": "Vitamin B6",
                        "cancer": "Bladder cancer",
                        "eligibility": "INCLUDE",
                        "needs_full_text": True,
                        "full_text_eligible": True,
                        "parallel_r3_gate_reasons": [
                            "JBI remains Fair after Claude R2"
                        ],
                    }
                ],
            }
            review.save_json(output / "r1_pass_packet.json", r1)
            review.save_json(output / "claude_r2_results.json", r2)

            for attempt in (1, 2):
                failure_type, billing_status = review._problem_record_fields(
                    source_error
                )
                review._record_problem(
                    output,
                    stage="gemini_r3",
                    model=review.GEMINI_R3_MODEL,
                    work_unit_key=work_key,
                    context_ids=[context_id],
                    error=source_error,
                    failure_type=failure_type,
                    billing_status=billing_status,
                )
                deferred = review._defer_twice_failed_r3_source_problem(
                    output, work_key
                )
                self.assertEqual(deferred, attempt == 2)

            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                state = review.write_outputs_and_state(
                    output, stopped_reason="test"
                )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(
                    encoding="utf-8"
                )
            )
            terminal = json.loads(
                (output / "r3_verified_source_failure_deferrals.json").read_text(
                    encoding="utf-8"
                )
            )
            public_deferrals = json.loads(
                (output / "r3_source_deferrals.json").read_text(encoding="utf-8")
            )

        self.assertEqual(ledger["unresolved_count"], 0)
        self.assertEqual(terminal["deferral_count"], 1)
        self.assertEqual(terminal["context_count"], 1)
        self.assertEqual(terminal["deferrals"][0]["attempts"], 2)
        self.assertFalse(
            terminal["deferrals"][0]["scientific_disposition_created"]
        )
        self.assertEqual(
            public_deferrals[
                "decision_free_twice_verified_source_failure_count"
            ],
            1,
        )
        self.assertEqual(
            state["status"], "complete_with_decision_free_source_deferrals"
        )
        self.assertEqual(state["gemini_r3_completed_contexts"], 0)
        self.assertEqual(state["gemini_r3_settled_contexts"], 1)

    def test_interrupted_active_call_is_unknown_billing_and_idempotent(self):
        context_id = "1|Vitamin B6|Bladder cancer"
        work_key = review._work_unit_key("claude_r2", [context_id])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            review._write_active_dispatch(
                output,
                stage="claude_r2",
                model=review.CLAUDE_R2_MODEL,
                units=[{"work_unit_key": work_key, "context_ids": [context_id]}],
            )
            with patch.object(
                review, "_process_is_alive", return_value=False
            ), patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                recovered = review.recover_interrupted_active_dispatch(
                    output
                )
                repeated = review.recover_interrupted_active_dispatch(
                    output
                )
            receipts = json.loads(
                (output / "gateway_receipts.json").read_text(encoding="utf-8")
            )
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(encoding="utf-8")
            )
            row = next(
                line
                for line in (root / "ClaudeGeminiCost.txt").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.startswith("Vitamin B6\t")
            )
        self.assertEqual((recovered, repeated), (1, 0))
        self.assertEqual(receipts["receipt_count"], 1)
        self.assertEqual(
            receipts["receipts"][0]["billing_status"],
            "unknown_after_process_interruption",
        )
        self.assertEqual(ledger["unresolved_count"], 1)
        self.assertEqual(
            ledger["unresolved"][0]["failure_type"],
            "interrupted_active_call",
        )
        self.assertEqual(
            ledger["unresolved"][0]["billing_status"],
            "unknown_after_process_interruption",
        )
        self.assertIn("claude_r2_calls=1", row)
        self.assertIn("claude_r2_input_tokens=0", row)
        self.assertIn("claude_r2_cost_usd=NA", row)
        self.assertIn("claude_r2_unknown_billing_calls=1", row)

    def test_live_active_dispatch_is_not_migrated(self):
        context_id = "1|Vitamin B6|Bladder cancer"
        work_key = review._work_unit_key("gemini_r3", [context_id])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "parallel"
            review._write_active_dispatch(
                output,
                stage="gemini_r3",
                model=review.GEMINI_R3_MODEL,
                units=[{"work_unit_key": work_key, "context_ids": [context_id]}],
            )
            with patch.object(review, "COST_PATH", root / "ClaudeGeminiCost.txt"):
                recovered = review.recover_interrupted_active_dispatch(
                    output
                )
            self.assertEqual(recovered, 0)
            self.assertFalse((output / "gateway_receipts.json").exists())
            active = json.loads(
                (output / "active_dispatch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(active["active_count"], 1)

    def test_hard_budget_rejection_stops_after_active_worker_window(self):
        started = []
        completed = []
        errors = []
        barrier = review.threading.Barrier(2)

        def process(unit):
            started.append(unit)
            barrier.wait(timeout=2)
            if unit == 0:
                raise review.GatewayBudgetExceeded("429 budget_exceeded")
            review.time.sleep(0.05)
            return str(unit), [{"context_id": str(unit)}]

        review._run_units(
            list(range(8)),
            process,
            lambda key, _results: completed.append(key),
            lambda unit, error: errors.append((unit, error)),
            workers=2,
        )
        self.assertEqual(sorted(started), [0, 1])
        self.assertEqual(completed, ["1"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0][1], review.GatewayBudgetExceeded)

    def test_gateway_429_budget_is_pre_response_and_creates_no_receipt(self):
        class BudgetError(RuntimeError):
            status_code = 429

        class RawAPI:
            calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                raise BudgetError("type=budget_exceeded code=429")

        raw_api = RawAPI()
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(with_raw_response=raw_api)
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with self.assertRaises(review.GatewayBudgetExceeded):
                review._call_gateway_json(
                    client,
                    output_dir=output,
                    stage="claude_r2",
                    model=review.CLAUDE_R2_MODEL,
                    system="system",
                    prompt="prompt",
                    context_ids=["1|Vitamin B6|Bladder cancer"],
                    exposure_weights={"Vitamin B6": 1},
                    validator=lambda _payload: [],
                )
            self.assertFalse((output / "gateway_receipts.json").exists())
            self.assertEqual(
                list((output / "model_response_cache").rglob("*.json")), []
            )
        self.assertEqual(raw_api.calls, 1)

    def test_budget_failure_migration_exposes_explicit_restart_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            review._record_problem(
                output,
                stage="claude_r2",
                model=review.CLAUDE_R2_MODEL,
                work_unit_key="budget-unit",
                context_ids=["1|Vitamin B6|Bladder cancer"],
                error="Error code: 429 - type=budget_exceeded",
            )
            changed = review._migrate_budget_problem_classification(output)
            ledger = json.loads(
                (output / "screening_problem_ledger.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(changed, 2)
        self.assertEqual(review._problem_record_fields("budget exceeded"), (
            "model_or_validation_failure",
            "known_from_completed_or_failed_local_call",
        ))
        self.assertEqual(
            ledger["unresolved"][0]["failure_type"],
            "infrastructure_budget_exhausted",
        )
        self.assertEqual(
            ledger["unresolved"][0]["billing_status"],
            "pre_response_gateway_rejection_no_model_output",
        )
        self.assertFalse(
            ledger["unresolved"][0]["scientific_disposition_created"]
        )
        self.assertTrue(
            review.parse_args(["--resume-after-budget-reset"]).resume_after_budget_reset
        )

    def test_budget_restart_guard_dispatches_no_model_call(self):
        context_id = "1|Vitamin B6|Bladder cancer"
        candidates = {
            "articles": [
                {
                    "pmid": "1",
                    "title": "Indexed title",
                    "abstract": "Abstract",
                    "mesh": [],
                    "candidate_contexts": [
                        {
                            "exposure": "Vitamin B6",
                            "cancer": "Bladder cancer",
                            "outcome": "Incidence",
                        }
                    ],
                }
            ]
        }
        first = {"input_digest": "first", "pass": 1}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            review.save_json(output / "r1_pass_candidates.json", candidates)
            review.save_json(output / "r1_pass_packet.json", first)
            review._record_problem(
                output,
                stage="claude_r2",
                model=review.CLAUDE_R2_MODEL,
                work_unit_key="budget-unit",
                context_ids=[context_id],
                error="Error code: 429 - type=budget_exceeded",
            )
            with patch.object(
                review,
                "_r2_candidates_with_official_metadata",
                return_value=(candidates, {"r2_input_candidates_digest": "repair"}),
            ), patch.object(
                review,
                "_configure_client",
                side_effect=AssertionError("budget guard must prevent configuration"),
            ) as configure, patch.object(
                review, "write_outputs_and_state", return_value={}
            ):
                packet = review.run_r2(output, batch_size=1, workers=1)
            configure.assert_not_called()
        self.assertEqual(packet["completed_contexts"], 0)
        self.assertTrue(packet["budget_restart_required"])
        self.assertEqual(
            packet["stopped_reason"],
            "r2_infrastructure_budget_exhausted_restart_required",
        )


if __name__ == "__main__":
    unittest.main()
