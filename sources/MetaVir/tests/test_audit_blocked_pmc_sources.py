import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import audit_blocked_pmc_sources as audit


def complete_jats(pmcid="PMC2781742", pmid="19808637"):
    paragraphs = "".join(
        f"<p>Paragraph {index} " + ("substantive evidence text " * 45) + "</p>"
        for index in range(1, 5)
    )
    return (
        "<article article-type='research-article'>"
        "<front><article-meta>"
        f"<article-id pub-id-type='pmcid'>{pmcid}</article-id>"
        f"<article-id pub-id-type='pmid'>{pmid}</article-id>"
        "<title-group><article-title>Diet and cancer evidence</article-title></title-group>"
        "</article-meta></front>"
        f"<body><sec><title>Methods and results</title>{paragraphs}</sec></body>"
        "</article>"
    ).encode()


def pmc_html_with_body(body_markup, pmcid="PMC2781742", pmid="19808637"):
    numeric_pmcid = pmcid.removeprefix("PMC")
    canonical = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    return (
        "<!doctype html><html><head>"
        f"<link rel='canonical' href='{canonical}'>"
        f"<meta name='citation_pmid' content='{pmid}'>"
        "<meta name='citation_title' content='Diet and cancer evidence'>"
        f"<meta name='citation_fulltext_html_url' content='{canonical}'>"
        "</head><body>"
        f"<div class='pmc-article-section' data-article-db='pmc' "
        f"data-article-id='{numeric_pmcid}'>"
        "<main id='main-content'><article>"
        "<section aria-label='Article content'>"
        "<section class='body main-article-body'>"
        f"{body_markup}"
        "</section></section></article></main></div>"
        "</body></html>"
    ).encode()


def complete_pmc_html(pmcid="PMC2781742", pmid="19808637"):
    paragraphs = "".join(
        f"<p>Paragraph {index} " + ("substantive evidence text " * 45) + "</p>"
        for index in range(1, 5)
    )
    return pmc_html_with_body(
        f"<section><h2>Methods and results</h2>{paragraphs}</section>",
        pmcid=pmcid,
        pmid=pmid,
    )


def pdf_link_shell_html(pmcid=None, pmid=None):
    pmcid = pmcid or audit.PMC_PDF_BLOCKER
    pmid = pmid or audit.strict_pdf.EXPECTED_PDF_IDENTITIES[pmcid]["pmid"]
    identity = audit.strict_pdf.expected_pdf_identity(pmcid, pmid)
    if identity["html_shell_reason"] == "insufficient_body_paragraphs":
        body = (
            "<section><h2>Article shell</h2><p>"
            + ("single shell paragraph " * 1200)
            + "</p></section>"
        )
    else:
        abstract_paragraphs = "".join(
            f"<p>Abstract paragraph {index} "
            + ("substantive abstract evidence " * 35)
            + "</p>"
            for index in range(1, 5)
        )
        body = (
            f"<section class='abstract'><h2>Abstract</h2>"
            f"{abstract_paragraphs}</section>"
        )
    html = pmc_html_with_body(
        body,
        pmcid=pmcid,
        pmid=pmid,
    )
    html = html.replace(
        b"content='Diet and cancer evidence'",
        f"content='{identity['title']}'".encode(),
        1,
    )
    return html.replace(
        b"</head>",
        (
            f"<meta name='citation_doi' content='{identity['doi']}'>"
            f"<meta name='citation_pdf_url' content='{identity['url']}'>"
            "</head>"
        ).encode(),
        1,
    )


def pow_challenge_html(challenge="boundedPdfChallenge:abc123"):
    return f'''<html><head><title>Preparing to download ...</title></head>
    <body><h1>Preparing to download ...</h1></body><script>
    const POW_CHALLENGE = "{challenge}"
    const POW_DIFFICULTY = "{audit.strict_pdf.EXPECTED_POW_DIFFICULTY}"
    const POW_COOKIE_NAME = "{audit.strict_pdf.POW_COOKIE_NAME}"
    const POW_COOKIE_PATH = "/"
    </script></html>'''.encode()


def receipt(
    url,
    body,
    status=200,
    final_url=None,
    content_type=None,
    request_accept=None,
    request_cookie="",
):
    effective_url = final_url or url
    is_pdf = url == audit._pdf_fallback_url(
        audit.PMC_PDF_BLOCKER,
        audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
    )
    selected_content_type = content_type or (
        "application/pdf" if is_pdf and body.startswith(b"%PDF-") else "text/html; charset=utf-8"
    )
    selected_accept = request_accept or (
        "application/pdf"
        if is_pdf
        else (
            "text/html; charset=utf-8"
            if "pmc.ncbi.nlm.nih.gov/articles/" in url
            else "application/xml, application/json"
        )
    )
    return audit.HttpReceipt(
        url=url,
        status=status,
        body=body,
        attempts=(
            {
                "attempt": 1,
                "http_status": status,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "error": "",
                "redirected": effective_url != url,
                "content_type": selected_content_type,
            },
        ),
        final_url=effective_url,
        redirected=effective_url != url,
        content_type=selected_content_type,
        request_accept=selected_accept,
        request_cookie=request_cookie,
    )


class StrictJatsReceiptTests(unittest.TestCase):
    def test_accepts_oai_wrapped_exact_jats_article_and_body(self):
        xml = (
            b"<OAI-PMH xmlns='http://www.openarchives.org/OAI/2.0/'><GetRecord><record>"
            b"<metadata>"
            + complete_jats()
            + b"</metadata></record></GetRecord></OAI-PMH>"
        )
        validation, text = audit.validate_jats_receipt(
            xml, expected_pmcid="PMC2781742", expected_pmid="19808637"
        )
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["article_count"], 1)
        self.assertEqual(validation["body_count"], 1)
        self.assertGreaterEqual(validation["body_paragraphs"], 3)
        self.assertGreaterEqual(len(text), audit.MIN_EXTRACTED_CHARACTERS)
        self.assertNotIn("body_preformats", validation)
        self.assertNotIn("body_preformat_characters", validation)

    def test_preformat_receipt_fields_are_emitted_only_for_substantive_ocr(self):
        ordinary_with_label = complete_jats().replace(
            b"<body>", b"<body><preformat>brief table label</preformat>", 1
        )
        ordinary_validation, _ = audit.validate_jats_receipt(
            ordinary_with_label,
            expected_pmcid="PMC2781742",
            expected_pmid="19808637",
        )
        self.assertTrue(ordinary_validation["valid"])
        self.assertNotIn("body_preformats", ordinary_validation)
        self.assertNotIn("body_preformat_characters", ordinary_validation)

        ocr = "substantive OCR evidence " * 140
        ocr_xml = (
            "<article><front><article-meta>"
            "<article-id pub-id-type='pmcid'>PMC2781742</article-id>"
            "<article-id pub-id-type='pmid'>19808637</article-id>"
            "<title-group><article-title>Diet and cancer evidence</article-title>"
            "</title-group></article-meta></front>"
            f"<body><preformat>{ocr}</preformat></body></article>"
        ).encode()
        ocr_validation, ocr_text = audit.validate_jats_receipt(
            ocr_xml,
            expected_pmcid="PMC2781742",
            expected_pmid="19808637",
        )
        self.assertTrue(ocr_validation["valid"])
        self.assertEqual(ocr_validation["body_preformats"], 1)
        self.assertGreaterEqual(
            ocr_validation["body_preformat_characters"],
            audit.MIN_BODY_CHARACTERS,
        )
        self.assertIn("substantive OCR evidence", ocr_text)


class StrictPmcHtmlSemanticReceiptTests(unittest.TestCase):
    def test_abstract_definitions_notes_and_references_cannot_mimic_full_text(self):
        definitions = "".join(
            f"<dt>TERM{index}</dt><dd><p>Definition {index}</p></dd>"
            for index in range(1, 10)
        )
        references = "".join(
            f"<li><span class='label'>{index}.</span><cite>"
            + ("long reference citation content " * 12)
            + "</cite> [<a>PubMed</a>] [<a>Google Scholar</a>]</li>"
            for index in range(1, 36)
        )
        html = pmc_html_with_body(
            "<section class='abstract'><h2>Abstract</h2><p>"
            + ("abstract-only evidence " * 120)
            + "</p></section>"
            + "<section id='notes1' class='notes'><h3>Abbreviations</h3>"
            + f"<dl class='def-list'>{definitions}</dl>"
            + "<section class='fn-group'><p>Ancillary footnote</p></section>"
            + "</section>"
            + "<section class='ref-list'><h2>References</h2><ul>"
            + references
            + "</ul></section>"
        )
        validation, text = audit.validate_pmc_html_receipt(
            html, expected_pmcid="PMC2781742", expected_pmid="19808637"
        )
        self.assertFalse(validation["valid"])
        self.assertEqual(
            validation["reason"], "non_abstract_narrative_not_substantive"
        )
        self.assertGreaterEqual(
            validation["body_characters"], audit.MIN_BODY_CHARACTERS
        )
        self.assertGreaterEqual(
            validation["body_paragraphs"], audit.MIN_BODY_PARAGRAPHS
        )
        self.assertGreaterEqual(validation["body_sections"], 1)
        self.assertGreaterEqual(
            validation["extracted_characters"], audit.MIN_EXTRACTED_CHARACTERS
        )
        self.assertEqual(validation["non_abstract_narrative_characters"], 0)
        self.assertEqual(validation["non_abstract_narrative_paragraphs"], 0)
        self.assertEqual(validation["non_abstract_narrative_sections"], 0)
        self.assertEqual(text, "")

    def test_definition_terms_cannot_tip_extracted_text_over_minimum(self):
        paragraphs = "".join(
            f"<p>Paragraph {index} " + ("narrative evidence " * 25) + "</p>"
            for index in range(1, 4)
        )
        narrative = f"<section><h2>Methods</h2>{paragraphs}</section>"
        baseline, _ = audit.validate_pmc_html_receipt(
            pmc_html_with_body(narrative),
            expected_pmcid="PMC2781742",
            expected_pmid="19808637",
        )
        terms = "".join(
            f"<dt>TERM_{index}_" + ("definition-label " * 6) + "</dt>"
            for index in range(1, 10)
        )
        expanded, text = audit.validate_pmc_html_receipt(
            pmc_html_with_body(narrative + f"<dl class='def-list'>{terms}</dl>"),
            expected_pmcid="PMC2781742",
            expected_pmid="19808637",
        )
        self.assertEqual(baseline["reason"], "extracted_text_below_r3_minimum")
        self.assertEqual(expanded["reason"], "extracted_text_below_r3_minimum")
        self.assertEqual(
            expanded["extracted_characters"], baseline["extracted_characters"]
        )
        self.assertEqual(
            expanded["non_abstract_narrative_characters"],
            baseline["non_abstract_narrative_characters"],
        )
        self.assertGreater(expanded["body_characters"], baseline["body_characters"])
        self.assertEqual(text, "")

    def test_reference_entries_cannot_satisfy_extracted_text_minimum(self):
        paragraphs = "".join(
            f"<p>Paragraph {index} " + ("narrative evidence " * 25) + "</p>"
            for index in range(1, 4)
        )
        narrative = f"<section><h2>Methods</h2>{paragraphs}</section>"
        baseline, _ = audit.validate_pmc_html_receipt(
            pmc_html_with_body(narrative),
            expected_pmcid="PMC2781742",
            expected_pmid="19808637",
        )
        reference_items = "".join(
            "<li><cite>" + ("reference-only evidence " * 20) + "</cite>"
            " [<a>DOI</a>] [<a>PubMed</a>]</li>"
            for _ in range(20)
        )
        expanded, text = audit.validate_pmc_html_receipt(
            pmc_html_with_body(
                narrative
                + "<section class='ref-list'><h2>References</h2><ul>"
                + reference_items
                + "</ul></section>"
            ),
            expected_pmcid="PMC2781742",
            expected_pmid="19808637",
        )
        self.assertEqual(baseline["reason"], "extracted_text_below_r3_minimum")
        self.assertEqual(expanded["reason"], "extracted_text_below_r3_minimum")
        self.assertEqual(
            expanded["extracted_characters"], baseline["extracted_characters"]
        )
        self.assertEqual(
            expanded["non_abstract_narrative_characters"],
            baseline["non_abstract_narrative_characters"],
        )
        self.assertGreater(expanded["body_characters"], baseline["body_characters"])
        self.assertEqual(text, "")

    def test_extracts_narrative_lists_and_captions_but_not_ancillary_or_hidden_text(self):
        paragraphs = "".join(
            f"<p>Paragraph {index} " + ("substantive evidence text " * 45) + "</p>"
            for index in range(1, 5)
        )
        body = (
            "<section><h2>Methods and results</h2>"
            + paragraphs
            + "<ul><li>NARRATIVE_LIST_TOKEN</li>"
            + "<li><p>NESTED_LIST_PARAGRAPH_TOKEN</p></li></ul>"
            + "<table><caption>NARRATIVE_TABLE_CAPTION_TOKEN</caption>"
            + "<tr><th>Measure</th><td>Value</td></tr></table>"
            + "<figure><figcaption>NARRATIVE_FIGURE_CAPTION_TOKEN</figcaption></figure>"
            + "</section>"
            + "<section class='ref-list'><h2>REFERENCE_HEADING_TRAP</h2>"
            + "<ul><li>REFERENCE_LIST_TRAP</li></ul></section>"
            + "<dl class='def-list'><dt>DEFINITION_TERM_TRAP</dt>"
            + "<dd><p>DEFINITION_TEXT_TRAP</p></dd></dl>"
            + "<section id='notes1'><p>NOTES_TRAP</p></section>"
            + "<nav><p>NAVIGATION_TRAP</p></nav>"
            + "<div hidden><p>HIDDEN_ATTRIBUTE_TRAP</p></div>"
            + "<div aria-hidden='true'><p>ARIA_HIDDEN_TRAP</p></div>"
            + "<div style='display: none !important'><p>STYLE_HIDDEN_TRAP</p></div>"
            + "<div class='display-none'><p>CLASS_HIDDEN_TRAP</p></div>"
        )
        validation, text = audit.validate_pmc_html_receipt(
            pmc_html_with_body(body),
            expected_pmcid="PMC2781742",
            expected_pmid="19808637",
        )
        self.assertTrue(validation["valid"])
        for token in (
            "NARRATIVE_LIST_TOKEN",
            "NESTED_LIST_PARAGRAPH_TOKEN",
            "NARRATIVE_TABLE_CAPTION_TOKEN",
            "NARRATIVE_FIGURE_CAPTION_TOKEN",
        ):
            self.assertIn(token, text)
        self.assertEqual(text.count("NESTED_LIST_PARAGRAPH_TOKEN"), 1)
        for token in (
            "REFERENCE_HEADING_TRAP",
            "REFERENCE_LIST_TRAP",
            "DEFINITION_TERM_TRAP",
            "DEFINITION_TEXT_TRAP",
            "NOTES_TRAP",
            "NAVIGATION_TRAP",
            "HIDDEN_ATTRIBUTE_TRAP",
            "ARIA_HIDDEN_TRAP",
            "STYLE_HIDDEN_TRAP",
            "CLASS_HIDDEN_TRAP",
        ):
            self.assertNotIn(token, text)
        self.assertGreater(
            validation["body_characters"],
            validation["non_abstract_narrative_characters"],
        )
        self.assertGreater(
            validation["body_paragraphs"],
            validation["non_abstract_narrative_paragraphs"],
        )


class StrictJatsStructuralReceiptTests(unittest.TestCase):
    def test_accepts_legacy_pmc_identifier_type_without_weakening_identity(self):
        xml = complete_jats().replace(b"pub-id-type='pmcid'", b"pub-id-type='pmc'")
        validation, _ = audit.validate_jats_receipt(
            xml, expected_pmcid="PMC2781742", expected_pmid="19808637"
        )
        self.assertTrue(validation["valid"])

    def test_rejects_wrong_or_missing_identity_body_and_malformed_xml(self):
        cases = [
            (complete_jats(pmcid="PMC999"), "pmcid_identity_mismatch"),
            (complete_jats(pmid="999"), "pmid_identity_mismatch"),
            (
                complete_jats().replace(b"<body>", b"<floats-group>").replace(
                    b"</body>", b"</floats-group>"
                ),
                "body_count_not_one",
            ),
            (b"<article><body>", "malformed_xml"),
        ]
        for xml, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                validation, text = audit.validate_jats_receipt(
                    xml, expected_pmcid="PMC2781742", expected_pmid="19808637"
                )
                self.assertFalse(validation["valid"])
                self.assertTrue(validation["reason"].startswith(expected_reason))
                self.assertEqual(text, "")

    def test_oai_protocol_error_is_not_a_full_text_receipt(self):
        xml = (
            b"<OAI-PMH xmlns='http://www.openarchives.org/OAI/2.0/'>"
            b"<error code='cannotDisseminateFormat'>unsupported</error></OAI-PMH>"
        )
        validation, text = audit.validate_jats_receipt(
            xml, expected_pmcid="PMC2781742", expected_pmid="19808637"
        )
        self.assertFalse(validation["valid"])
        self.assertEqual(validation["reason"], "protocol_error_without_jats_article")
        self.assertIn("cannotDisseminateFormat", validation["protocol_error"])
        self.assertEqual(text, "")

    def test_rejects_misplaced_or_duplicate_identity_and_nested_body(self):
        exact = complete_jats()
        pmcid_tag = b"<article-id pub-id-type='pmcid'>PMC2781742</article-id>"
        pmid_tag = b"<article-id pub-id-type='pmid'>19808637</article-id>"
        ids_in_body = exact.replace(pmcid_tag, b"", 1).replace(
            pmid_tag, b"", 1
        ).replace(b"<body>", b"<body>" + pmcid_tag + pmid_tag, 1)
        body_start = exact.index(b"<body>")
        body_end = exact.index(b"</body>") + len(b"</body>")
        body = exact[body_start:body_end]
        nested_body = (exact[:body_start] + exact[body_end:]).replace(
            b"</article-meta>", body + b"</article-meta>", 1
        )
        duplicate_pmcid = exact.replace(pmcid_tag, pmcid_tag + pmcid_tag, 1)
        cases = [
            (ids_in_body, "pmcid_identity_count_or_location_invalid"),
            (nested_body, "body_not_one_direct_article_child"),
            (duplicate_pmcid, "pmcid_identity_count_or_location_invalid"),
        ]
        for xml, reason in cases:
            with self.subTest(reason=reason):
                validation, text = audit.validate_jats_receipt(
                    xml, expected_pmcid="PMC2781742", expected_pmid="19808637"
                )
                self.assertFalse(validation["valid"])
                self.assertEqual(validation["reason"], reason)
                self.assertEqual(text, "")


class StrictPmcHtmlReceiptTests(unittest.TestCase):
    def test_accepts_exact_canonical_article_and_main_body(self):
        html = complete_pmc_html()
        validation, text = audit.validate_pmc_html_receipt(
            html, expected_pmcid="PMC2781742", expected_pmid="19808637"
        )
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["html_count"], 1)
        self.assertEqual(validation["main_count"], 1)
        self.assertEqual(validation["article_count"], 1)
        self.assertEqual(validation["body_count"], 1)
        self.assertGreaterEqual(validation["body_paragraphs"], 3)
        self.assertGreaterEqual(
            validation["non_abstract_narrative_characters"],
            audit.MIN_BODY_CHARACTERS,
        )
        self.assertGreaterEqual(
            validation["non_abstract_narrative_paragraphs"],
            audit.MIN_BODY_PARAGRAPHS,
        )
        self.assertGreaterEqual(validation["non_abstract_narrative_sections"], 1)
        self.assertGreaterEqual(len(text), audit.MIN_EXTRACTED_CHARACTERS)

    def test_rejects_wrong_identity_missing_body_and_interstitial(self):
        exact = complete_pmc_html()
        cases = [
            (
                exact.replace(b"articles/PMC2781742/", b"articles/PMC999/"),
                "canonical_url_identity_mismatch",
            ),
            (exact.replace(b"content='19808637'", b"content='999'"), "pmid_identity_mismatch"),
            (
                exact.replace(b"main-article-body", b"not-the-main-article-body"),
                "body_count_not_one",
            ),
            (
                b"<!doctype html><html><head><title>Checking your browser</title>"
                b"</head><body>captcha</body></html>",
                "canonical_url_identity_mismatch",
            ),
        ]
        for html, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                validation, text = audit.validate_pmc_html_receipt(
                    html, expected_pmcid="PMC2781742", expected_pmid="19808637"
                )
                self.assertFalse(validation["valid"])
                self.assertEqual(validation["reason"], expected_reason)
                self.assertEqual(text, "")

    def test_rejects_detached_body_outer_truncation_and_duplicate_identity(self):
        exact = complete_pmc_html()
        detached = exact.replace(
            b"><main id='main-content'>",
            b"></div><main id='main-content'>",
            1,
        ).replace(b"</main></div>", b"</main>", 1)
        truncated = exact[: exact.index(b"</main>") + len(b"</main>")]
        container = (
            b"<div class='pmc-article-section' data-article-db='pmc' "
            b"data-article-id='2781742'>"
        )
        duplicate = exact.replace(container, container + b"</div>" + container, 1)
        cases = [
            (detached, "main_count_not_one"),
            (truncated, "html_document_not_one_closed_root"),
            (duplicate, "article_container_count_not_one"),
        ]
        for html, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                validation, text = audit.validate_pmc_html_receipt(
                    html, expected_pmcid="PMC2781742", expected_pmid="19808637"
                )
                self.assertFalse(validation["valid"])
                self.assertEqual(validation["reason"], expected_reason)
                self.assertEqual(text, "")

    def test_rejects_article_or_fragment_after_closed_html_root(self):
        exact = complete_pmc_html()
        outside_root = exact.replace(
            b"</head><body>", b"</head></html><body>", 1
        )
        appended_fragment = exact + b"<div>unclosed appended fragment"
        for html in (outside_root, appended_fragment):
            with self.subTest(html=html[-80:]):
                validation, text = audit.validate_pmc_html_receipt(
                    html, expected_pmcid="PMC2781742", expected_pmid="19808637"
                )
                self.assertFalse(validation["valid"])
                self.assertEqual(
                    validation["reason"], "html_document_not_one_closed_root"
                )
                self.assertEqual(text, "")

    def test_rejects_identity_metadata_moved_before_html_root(self):
        exact = complete_pmc_html()
        identity_start = exact.index(b"<link rel='canonical'")
        identity_end = exact.index(b"</head>")
        identity = exact[identity_start:identity_end]
        moved = exact[:identity_start] + exact[identity_end:]
        moved = moved.replace(b"<html>", identity + b"<html>", 1)
        validation, text = audit.validate_pmc_html_receipt(
            moved, expected_pmcid="PMC2781742", expected_pmid="19808637"
        )
        self.assertFalse(validation["valid"])
        self.assertEqual(
            validation["reason"], "html_document_identity_outside_single_head"
        )
        self.assertEqual(text, "")

    def test_rejects_duplicate_or_body_scoped_identity_markup(self):
        exact = complete_pmc_html()
        canonical = (
            b"<link rel='canonical' "
            b"href='https://pmc.ncbi.nlm.nih.gov/articles/PMC2781742/'>"
        )
        duplicate = exact.replace(canonical, canonical + canonical, 1)
        body_meta = exact.replace(
            b"</article></main>",
            b"</article><meta name='citation_pmid' content='19808637'></main>",
            1,
        )
        cases = [
            (duplicate, "canonical_url_identity_mismatch"),
            (body_meta, "html_document_identity_outside_single_head"),
        ]
        for html, reason in cases:
            with self.subTest(reason=reason):
                validation, text = audit.validate_pmc_html_receipt(
                    html, expected_pmcid="PMC2781742", expected_pmid="19808637"
                )
                self.assertFalse(validation["valid"])
                self.assertEqual(validation["reason"], reason)
                self.assertEqual(text, "")

    def test_rejects_nested_head_and_article_inside_head_without_body(self):
        exact = complete_pmc_html()
        nested_head = exact.replace(
            b"<html><head>", b"<html><body><head>", 1
        ).replace(b"</head><body>", b"</head>", 1)
        article_in_head = exact.replace(b"</head><body>", b"", 1).replace(
            b"</body></html>", b"</head></html>", 1
        )
        for html in (nested_head, article_in_head):
            with self.subTest(html=html[:100]):
                validation, text = audit.validate_pmc_html_receipt(
                    html, expected_pmcid="PMC2781742", expected_pmid="19808637"
                )
                self.assertFalse(validation["valid"])
                self.assertIn(
                    validation["reason"],
                    {
                        "html_document_identity_outside_single_head",
                        "html_document_head_body_structure_invalid",
                    },
                )
                self.assertEqual(text, "")

    def test_html_probe_binds_transport_and_raw_receipt(self):
        html = complete_pmc_html()

        def getter(url, *, user_agent):
            return receipt(url, html)

        route, text, raw = audit.probe_pmc_html(
            "PMC2781742", "19808637", user_agent="test", getter=getter
        )
        self.assertEqual(route["route"], "pmc_canonical_article_html")
        self.assertTrue(route["validation"]["valid"])
        self.assertEqual(raw, html)
        self.assertGreaterEqual(len(text), audit.MIN_EXTRACTED_CHARACTERS)

    def test_html_probe_rejects_valid_looking_body_on_non_200_status(self):
        html = complete_pmc_html()

        def getter(url, *, user_agent):
            return receipt(url, html, status=404)

        route, text, raw = audit.probe_pmc_html(
            "PMC2781742", "19808637", user_agent="test", getter=getter
        )
        self.assertFalse(route["validation"]["valid"])
        self.assertEqual(route["validation"]["reason"], "http_status_not_200")
        self.assertEqual(text, "")
        self.assertEqual(raw, html)

    def test_html_probe_rejects_redirected_valid_looking_receipt(self):
        html = complete_pmc_html()

        def getter(url, *, user_agent):
            return receipt(url, html, final_url="https://example.invalid/article")

        route, text, raw = audit.probe_pmc_html(
            "PMC2781742", "19808637", user_agent="test", getter=getter
        )
        self.assertFalse(route["validation"]["valid"])
        self.assertEqual(route["validation"]["reason"], "redirected_transport_url")
        self.assertEqual(text, "")
        self.assertEqual(raw, html)

    def test_http_get_records_the_effective_url_after_redirects(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"body"

            def geturl(self):
                return "https://example.invalid/redirected"

        requested = audit._pmc_html_url("PMC2781742")
        with patch.object(audit.urllib.request, "urlopen", return_value=FakeResponse()):
            result = audit.http_get(
                requested,
                user_agent="test",
                max_attempts=1,
            )
        self.assertEqual(result.url, requested)
        self.assertEqual(result.final_url, "https://example.invalid/redirected")
        self.assertTrue(result.redirected)

    def test_http_get_detects_redirect_before_sensitive_query_redaction(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"body"

            def geturl(self):
                return requested.replace("original%40example.invalid", "changed%40example.invalid")

        requested = audit._efetch_url("PMC2781742", "original@example.invalid")
        with patch.object(audit.urllib.request, "urlopen", return_value=FakeResponse()):
            result = audit.http_get(requested, user_agent="test", max_attempts=1)
        self.assertEqual(result.url, result.final_url)
        self.assertTrue(result.redirected)
        validation, text = audit._require_http_success(
            {"valid": True, "reason": "synthetic_valid"}, "body", result
        )
        self.assertFalse(validation["valid"])
        self.assertEqual(validation["reason"], "redirected_transport_url")
        self.assertEqual(text, "")

    def test_http_get_no_follow_records_302_as_redirect_even_when_url_is_unchanged(self):
        requested = audit._pdf_fallback_url(
            audit.PMC_PDF_BLOCKER,
            audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
        )
        redirect_error = audit.urllib.error.HTTPError(
            requested,
            302,
            "Found",
            {"Content-Type": "text/html; charset=utf-8"},
            io.BytesIO(b"redirect response"),
        )
        opener = Mock()
        opener.open.side_effect = redirect_error
        with patch.object(audit.urllib.request, "build_opener", return_value=opener):
            result = audit.http_get(
                requested,
                user_agent="test",
                accept="application/pdf",
                follow_redirects=False,
                max_attempts=1,
            )
        self.assertEqual(result.status, 302)
        self.assertEqual(result.final_url, requested)
        self.assertTrue(result.redirected)
        validation, text = audit._require_http_success(
            {"valid": True, "reason": "synthetic_valid"}, "body", result
        )
        self.assertFalse(validation["valid"])
        self.assertEqual(validation["reason"], "redirected_transport_url")
        self.assertEqual(text, "")


class ExactPmcPdfFallbackTests(unittest.TestCase):
    def link_route(self):
        raw = pdf_link_shell_html()
        validation, text = audit.validate_pmc_html_receipt(
            raw,
            expected_pmcid=audit.PMC_PDF_BLOCKER,
            expected_pmid=audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
        )
        self.assertEqual(validation["reason"], "non_abstract_narrative_not_substantive")
        self.assertEqual(text, "")
        return (
            {
                "route": "pmc_canonical_article_html",
                "transport": audit._receipt_transport(
                    receipt(audit._pmc_html_url(audit.PMC_PDF_BLOCKER), raw)
                ),
                "validation": validation,
            },
            raw,
        )

    def probe(self, *, pdf_content_type="application/pdf"):
        link_route, link_raw = self.link_route()
        pdf_raw = b"%PDF-1.7\n" + (b"bounded exact article\n" * 4000) + b"%%EOF\n"
        pdf_text = "Complete Methods Results Discussion evidence. " * 1200
        pdf_validation = {
            "valid": True,
            "bytes": len(pdf_raw),
            "sha256": hashlib.sha256(pdf_raw).hexdigest(),
            "reason": "validated_exact_complete_official_pmc_pdf",
        }
        calls = []

        def getter(url, **kwargs):
            calls.append((url, dict(kwargs)))
            if not kwargs.get("cookie_header"):
                raw = pow_challenge_html()
                return receipt(
                    url,
                    raw,
                    content_type="text/html; charset=utf-8",
                    request_accept=kwargs["accept"],
                )
            return receipt(
                url,
                pdf_raw,
                content_type=pdf_content_type,
                request_accept=kwargs["accept"],
                request_cookie=kwargs["cookie_header"],
            )

        with patch.object(
            audit.strict_pdf,
            "validate_pmc_pdf_receipt",
            return_value=(pdf_validation, pdf_text),
        ):
            route, text, raw = audit.probe_pmc_pdf(
                audit.PMC_PDF_BLOCKER,
                audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
                link_html_route=link_route,
                link_html_raw=link_raw,
                user_agent="test",
                getter=getter,
                sleep=lambda _seconds: None,
            )
        return route, text, raw, calls, pdf_validation, pdf_text, pdf_raw

    def test_link_html_is_exactly_head_bound_and_fails_only_narrative_gate(self):
        raw = pdf_link_shell_html()
        validation = audit.validate_pdf_link_html_receipt(
            raw,
            expected_pmcid=audit.PMC_PDF_BLOCKER,
            expected_pmid=audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
        )
        self.assertTrue(validation["valid"])
        self.assertEqual(
            validation["semantic_html_validation"]["reason"],
            "non_abstract_narrative_not_substantive",
        )
        identity = audit.strict_pdf.EXPECTED_PDF_IDENTITIES[audit.PMC_PDF_BLOCKER]
        self.assertEqual(validation["citation_pdf_urls"], [identity["url"]])
        duplicate = raw.replace(
            b"</head>",
            f"<meta name='citation_pdf_url' content='{identity['url']}'></head>".encode(),
            1,
        )
        rejected = audit.validate_pdf_link_html_receipt(
            duplicate,
            expected_pmcid=audit.PMC_PDF_BLOCKER,
            expected_pmid=audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
        )
        self.assertFalse(rejected["valid"])
        self.assertEqual(rejected["reason"], "pdf_link_url_not_exactly_bound")

    def test_second_identity_requires_its_exact_html_shell_reason_and_pdf_url(self):
        pmcid = "PMC11159243"
        pmid = "15016323"
        identity = audit.strict_pdf.expected_pdf_identity(pmcid, pmid)
        raw = pdf_link_shell_html(pmcid, pmid)
        semantic, text = audit.validate_pmc_html_receipt(
            raw, expected_pmcid=pmcid, expected_pmid=pmid
        )
        self.assertFalse(semantic["valid"])
        self.assertEqual(semantic["reason"], "insufficient_body_paragraphs")
        self.assertEqual(text, "")
        validation = audit.validate_pdf_link_html_receipt(
            raw, expected_pmcid=pmcid, expected_pmid=pmid
        )
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["citation_pdf_urls"], [identity["url"]])
        with patch.object(
            audit,
            "validate_pmc_html_receipt",
            return_value=(
                {
                    "valid": False,
                    "reason": "non_abstract_narrative_not_substantive",
                },
                "",
            ),
        ):
            wrong_shell_reason = audit.validate_pdf_link_html_receipt(
                raw, expected_pmcid=pmcid, expected_pmid=pmid
            )
        self.assertFalse(wrong_shell_reason["valid"])
        self.assertEqual(
            wrong_shell_reason["reason"],
            "canonical_html_not_exact_pdf_fallback_shell",
        )

        wrong_url = raw.replace(
            identity["url"].encode(),
            audit.strict_pdf.EXPECTED_PDF_IDENTITIES[
                audit.PMC_PDF_BLOCKER
            ]["url"].encode(),
            1,
        )
        rejected = audit.validate_pdf_link_html_receipt(
            wrong_url, expected_pmcid=pmcid, expected_pmid=pmid
        )
        self.assertFalse(rejected["valid"])
        self.assertEqual(rejected["reason"], "pdf_link_url_not_exactly_bound")

    def test_second_identity_can_use_dynamic_probe_without_expanding_sealed_ledger(self):
        pmcid = "PMC11159243"
        pmid = "15016323"
        link_raw = pdf_link_shell_html(pmcid, pmid)
        semantic, _ = audit.validate_pmc_html_receipt(
            link_raw, expected_pmcid=pmcid, expected_pmid=pmid
        )
        link_route = {
            "route": "pmc_canonical_article_html",
            "transport": audit._receipt_transport(
                receipt(audit._pmc_html_url(pmcid), link_raw)
            ),
            "validation": semantic,
        }
        pdf_raw = b"%PDF-1.7\n" + (b"second exact article\n" * 4000) + b"%%EOF\n"
        pdf_text = "Complete Methods Results Discussion evidence. " * 1200
        pdf_validation = {
            "valid": True,
            "bytes": len(pdf_raw),
            "sha256": hashlib.sha256(pdf_raw).hexdigest(),
            "reason": "validated_exact_complete_official_pmc_pdf",
        }

        def getter(url, **kwargs):
            if not kwargs.get("cookie_header"):
                challenge = pow_challenge_html("secondExactPdfChallenge:abc123")
                return receipt(
                    url,
                    challenge,
                    content_type="text/html; charset=utf-8",
                    request_accept="application/pdf",
                )
            return receipt(
                url,
                pdf_raw,
                content_type="application/pdf; charset=utf-8",
                request_accept="application/pdf",
                request_cookie=kwargs["cookie_header"],
            )

        with patch.object(
            audit.strict_pdf,
            "validate_pmc_pdf_receipt",
            return_value=(pdf_validation, pdf_text),
        ):
            route, text, raw = audit.probe_pmc_pdf(
                pmcid,
                pmid,
                link_html_route=link_route,
                link_html_raw=link_raw,
                user_agent="test",
                getter=getter,
                sleep=lambda _seconds: None,
            )
        self.assertTrue(route["validation"]["valid"])
        self.assertEqual(text, pdf_text)
        self.assertEqual(raw, pdf_raw)
        self.assertEqual(audit.PMC_PDF_BLOCKER, "PMC11160038")
        self.assertNotIn(pmcid, audit.EXPECTED_BLOCKERS)
        with self.assertRaisesRegex(
            audit.AuditInvariantError, "Non-bounded source"
        ):
            audit._validate_pdf_route_transport(
                route, pmcid=pmcid, expected_pmid=pmid
            )

    def test_probe_seals_link_challenge_solution_cookie_and_exact_pdf_content_type(self):
        route, text, raw, calls, validation, expected_text, expected_raw = self.probe()
        self.assertTrue(route["validation"]["valid"])
        self.assertEqual(text, expected_text)
        self.assertEqual(raw, expected_raw)
        self.assertEqual(route["validation"], validation)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["accept"], "application/pdf")
        self.assertEqual(calls[0][1]["cookie_header"], "")
        self.assertFalse(calls[0][1]["follow_redirects"])
        self.assertTrue(
            calls[1][1]["cookie_header"].startswith(
                f"{audit.strict_pdf.POW_COOKIE_NAME}="
            )
        )
        self.assertFalse(calls[1][1]["follow_redirects"])
        with patch.object(
            audit.strict_pdf,
            "validate_pmc_pdf_receipt",
            return_value=(validation, expected_text),
        ):
            audit._validate_pdf_route_transport(
                route,
                pmcid=audit.PMC_PDF_BLOCKER,
                expected_pmid=audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
            )
        self.assertEqual(
            audit._decode_ledger_body(route["link_html"]["body"], label="link"),
            pdf_link_shell_html(),
        )
        self.assertEqual(
            audit._decode_ledger_body(
                route["pow_challenge"]["body"], label="challenge"
            ),
            pow_challenge_html(),
        )

    def test_probe_rejects_nonexact_pdf_content_type(self):
        rejected_values = (
            "application/pdf; charset=binary",
            "application/pdf;charset=utf-8",
            "application/pdf; charset=UTF-8",
            "application/pdf; charset=utf-8; version=1.7",
            "Application/pdf",
            "application/octet-stream",
            "application/pdf ",
        )
        for content_type in rejected_values:
            with self.subTest(content_type=content_type):
                (
                    route,
                    text,
                    raw,
                    _calls,
                    _validation,
                    _expected_text,
                    expected_raw,
                ) = self.probe(pdf_content_type=content_type)
                self.assertFalse(route["validation"]["valid"])
                self.assertEqual(
                    route["validation"]["reason"],
                    "content_type_not_in_exact_pdf_allowlist",
                )
                self.assertEqual(route["validation"]["observed_content_type"], content_type)
                self.assertEqual(text, "")
                self.assertEqual(raw, expected_raw)

    def test_probe_accepts_only_both_exact_allowlisted_pdf_content_types(self):
        self.assertEqual(
            audit.strict_pdf.ALLOWED_OFFICIAL_PDF_CONTENT_TYPES,
            ("application/pdf", "application/pdf; charset=utf-8"),
        )
        for content_type in audit.strict_pdf.ALLOWED_OFFICIAL_PDF_CONTENT_TYPES:
            with self.subTest(content_type=content_type):
                (
                    route,
                    text,
                    raw,
                    _calls,
                    validation,
                    expected_text,
                    _expected_raw,
                ) = self.probe(pdf_content_type=content_type)
                self.assertTrue(route["validation"]["valid"])
                self.assertTrue(text)
                self.assertTrue(raw.startswith(b"%PDF-"))
                with patch.object(
                    audit.strict_pdf,
                    "validate_pmc_pdf_receipt",
                    return_value=(validation, expected_text),
                ):
                    audit._validate_pdf_route_transport(
                        route,
                        pmcid=audit.PMC_PDF_BLOCKER,
                        expected_pmid=audit.EXPECTED_BLOCKERS[
                            audit.PMC_PDF_BLOCKER
                        ],
                    )

    def test_pdf_content_type_header_value_is_not_normalized_into_allowlist(self):
        malformed = "application/pdf;\tcharset=utf-8"
        self.assertEqual(
            audit._content_type_from_headers({"Content-Type": malformed}),
            malformed,
        )

    def test_tampered_ledger_pdf_content_type_is_rejected(self):
        (
            route,
            _text,
            _raw,
            _calls,
            validation,
            expected_text,
            _expected_raw,
        ) = self.probe(pdf_content_type="application/pdf; charset=utf-8")
        tampered_content_type = "application/pdf; charset=UTF-8"
        route["pdf_transport"]["content_type"] = tampered_content_type
        route["pdf_transport"]["attempts"][-1][
            "content_type"
        ] = tampered_content_type
        with patch.object(
            audit.strict_pdf,
            "validate_pmc_pdf_receipt",
            return_value=(validation, expected_text),
        ), self.assertRaisesRegex(
            audit.AuditInvariantError, "PDF content validation differs"
        ):
            audit._validate_pdf_route_transport(
                route,
                pmcid=audit.PMC_PDF_BLOCKER,
                expected_pmid=audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
            )

    def test_cookie_transport_is_bounded_to_exact_url_and_no_redirect_following(self):
        pdf_url = audit._pdf_fallback_url(
            audit.PMC_PDF_BLOCKER,
            audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
        )
        cookie = f"{audit.strict_pdf.POW_COOKIE_NAME}=challenge,1"
        with self.assertRaisesRegex(audit.AuditInvariantError, "cookie escaped"):
            audit.http_get(
                pdf_url,
                user_agent="test",
                accept="application/pdf",
                cookie_header=cookie,
                max_attempts=1,
            )

    def test_cookie_transport_accepts_second_exact_identity_url_only(self):
        identity = audit.strict_pdf.expected_pdf_identity(
            "PMC11159243", "15016323"
        )
        cookie = f"{audit.strict_pdf.POW_COOKIE_NAME}=challenge,1"

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/pdf"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"%PDF-exact"

            def geturl(self):
                return identity["url"]

        opener = Mock()
        opener.open.return_value = FakeResponse()
        with patch.object(audit.urllib.request, "build_opener", return_value=opener):
            result = audit.http_get(
                identity["url"],
                user_agent="test",
                accept="application/pdf",
                cookie_header=cookie,
                follow_redirects=False,
                max_attempts=1,
            )
        self.assertEqual(result.status, 200)
        self.assertFalse(result.redirected)
        self.assertEqual(result.url, identity["url"])
        with self.assertRaisesRegex(audit.AuditInvariantError, "cookie escaped"):
            audit.http_get(
                audit._pmc_html_url(audit.PMC_PDF_BLOCKER),
                user_agent="test",
                accept="application/pdf",
                cookie_header=cookie,
                follow_redirects=False,
                max_attempts=1,
            )

    def test_tampered_link_or_challenge_receipt_is_rejected(self):
        route, *_ = self.probe()
        route["pow_challenge"]["body"]["body_base64"] = route["pow_challenge"][
            "body"
        ]["body_base64"][:-4] + "AAAA"
        with self.assertRaisesRegex(audit.AuditInvariantError, "challenge ledger body"):
            audit._validate_pdf_route_transport(
                route,
                pmcid=audit.PMC_PDF_BLOCKER,
                expected_pmid=audit.EXPECTED_BLOCKERS[audit.PMC_PDF_BLOCKER],
            )

    def test_pdf_contract_declares_exact_tools_and_bounded_action_policy(self):
        complete_contract = audit.retrieval_validation_contract()
        self.assertEqual(audit.RETRIEVAL_VALIDATOR_VERSION, 10)
        self.assertEqual(complete_contract["validator_version"], 10)
        contract = complete_contract["pmc_pdf"]
        self.assertEqual(
            contract["exact_allowed_content_types"],
            list(audit.strict_pdf.ALLOWED_OFFICIAL_PDF_CONTENT_TYPES),
        )
        self.assertNotIn("exact_content_type", contract)
        self.assertEqual(
            contract["poppler_tools"],
            list(audit.strict_pdf.REQUIRED_POPPLER_TOOLS),
        )
        self.assertTrue(contract["poppler_tools_resolve_to_one_directory"])
        self.assertTrue(contract["every_poppler_tool_version_exactly_pinned"])
        self.assertTrue(
            contract["proof_of_work"][
                "raw_challenge_body_revalidated_before_solution_use"
            ]
        )
        policy = contract["bounded_active_content_policy"]
        self.assertFalse(policy["exhaustive_pdf_object_action_inventory"])
        self.assertTrue(policy["exact_raw_sha256_is_primary_integrity_boundary"])
        self.assertNotIn("action_inventory", contract)


class ArticleDatasetProbeTests(unittest.TestCase):
    def test_s3_route_accepts_only_matching_metadata_md5_and_jats(self):
        pmcid = "PMC2781742"
        pmid = "19808637"
        xml = complete_jats(pmcid, pmid)
        digest = hashlib.md5(xml, usedforsecurity=False).hexdigest()
        listing = (
            "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
            f"<CommonPrefixes><Prefix>{pmcid}.1/</Prefix></CommonPrefixes>"
            "<IsTruncated>false</IsTruncated></ListBucketResult>"
        ).encode()
        metadata = json.dumps(
            {
                "pmcid": pmcid,
                "pmid": int(pmid),
                "version": 1,
                "xml_url": (
                    f"s3://pmc-oa-opendata/{pmcid}.1/{pmcid}.1.xml?md5={digest}"
                ),
            }
        ).encode()

        def getter(url, *, user_agent):
            if "list-type=2" in url:
                return receipt(url, listing)
            if "/metadata/" in url:
                return receipt(url, metadata)
            return receipt(url, xml)

        route, text, raw = audit.probe_s3(
            pmcid, pmid, user_agent="test", getter=getter
        )
        self.assertTrue(route["validation"]["valid"])
        self.assertEqual(route["accepted_version"], 1)
        self.assertEqual(raw, xml)
        self.assertGreaterEqual(len(text), audit.MIN_EXTRACTED_CHARACTERS)

    def test_s3_route_rejects_md5_mismatch(self):
        pmcid = "PMC2781742"
        pmid = "19808637"
        xml = complete_jats(pmcid, pmid)
        listing = (
            "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
            f"<CommonPrefixes><Prefix>{pmcid}.1/</Prefix></CommonPrefixes>"
            "<IsTruncated>false</IsTruncated></ListBucketResult>"
        ).encode()
        metadata = json.dumps(
            {
                "pmcid": pmcid,
                "pmid": int(pmid),
                "version": 1,
                "xml_url": (
                    f"s3://pmc-oa-opendata/{pmcid}.1/{pmcid}.1.xml?md5="
                    + ("0" * 32)
                ),
            }
        ).encode()

        def getter(url, *, user_agent):
            if "list-type=2" in url:
                return receipt(url, listing)
            if "/metadata/" in url:
                return receipt(url, metadata)
            return receipt(url, xml)

        route, text, raw = audit.probe_s3(
            pmcid, pmid, user_agent="test", getter=getter
        )
        self.assertFalse(route["validation"]["valid"])
        self.assertEqual(
            route["validation"]["reason"],
            "no_article_dataset_version_passed_strict_validation",
        )
        self.assertEqual(text, "")
        self.assertEqual(raw, b"")
        self.assertFalse(route["version_attempts"][0]["md5_validation"]["valid"])


class AllOrNoneStagingTests(unittest.TestCase):
    def test_incomplete_set_creates_no_staging_or_production_files(self):
        pmcid, pmid = next(iter(audit.EXPECTED_BLOCKERS.items()))
        xml = complete_jats(pmcid, pmid)
        validation, text = audit.validate_jats_receipt(
            xml, expected_pmcid=pmcid, expected_pmid=pmid
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaises(audit.AuditInvariantError):
                audit.stage_complete_set(
                    output,
                    {
                        pmcid: {
                            "pmcid": pmcid,
                            "pmid": pmid,
                            "route": "test",
                            "source_format": audit.JATS_XML_FORMAT,
                            "validation": validation,
                            "text": text,
                            "raw": xml,
                        }
                    },
                )
            self.assertEqual(list(output.iterdir()), [])

    def test_exact_valid_set_is_staged_without_touching_full_text(self):
        records = {}
        for pmcid, pmid in audit.EXPECTED_BLOCKERS.items():
            xml = complete_jats(pmcid, pmid)
            validation, text = audit.validate_jats_receipt(
                xml, expected_pmcid=pmcid, expected_pmid=pmid
            )
            records[pmcid] = {
                "pmcid": pmcid,
                "pmid": pmid,
                "route": "test",
                "source_format": audit.JATS_XML_FORMAT,
                "validation": validation,
                "text": text,
                "raw": xml,
            }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            staging = audit.stage_complete_set(output, records)
            self.assertTrue((staging / "manifest.json").is_file())
            self.assertEqual(len(list(staging.glob("PMC*.xml"))), 10)
            self.assertEqual(len(list(staging.glob("PMC*.txt"))), 10)
            self.assertFalse((output / "full_text").exists())

    def test_exact_mixed_set_stages_and_revalidates_pdf_raw_and_text(self):
        records = {}
        source_records = {}
        pdf_raw = b"%PDF-1.7\n" + (b"sealed article bytes\n" * 4000) + b"%%EOF\n"
        pdf_text = "Complete Methods Results Discussion quantitative article. " * 900
        pdf_validation = {
            "valid": True,
            "bytes": len(pdf_raw),
            "sha256": hashlib.sha256(pdf_raw).hexdigest(),
            "reason": "validated_exact_complete_official_pmc_pdf",
        }
        for pmcid, pmid in audit.EXPECTED_BLOCKERS.items():
            if pmcid == audit.PMC_PDF_BLOCKER:
                raw = pdf_raw
                text = pdf_text
                source_format = audit.PMC_PDF_FORMAT
                route = "pdf"
                validation = pdf_validation
                long_route = audit.PMC_PDF_ROUTE
            else:
                raw = complete_jats(pmcid, pmid)
                validation, text = audit.validate_jats_receipt(
                    raw, expected_pmcid=pmcid, expected_pmid=pmid
                )
                source_format = audit.JATS_XML_FORMAT
                route = "efetch"
                long_route = "ncbi_entrez_efetch_pmc_xml"
            records[pmcid] = {
                "pmcid": pmcid,
                "pmid": pmid,
                "route": route,
                "source_format": source_format,
                "validation": validation,
                "text": text,
                "raw": raw,
            }
            source_records[pmcid] = {
                "accepted_route": route,
                "accepted_source_format": source_format,
                "routes": [{"route": long_route, "validation": validation}],
            }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            audit.strict_pdf,
            "validate_pmc_pdf_receipt",
            return_value=(pdf_validation, pdf_text),
        ):
            staging = audit.stage_complete_set(Path(directory), records)
            manifest = json.loads((staging / "manifest.json").read_text())
            self.assertEqual(
                manifest["schema_version"], audit.STAGING_MANIFEST_SCHEMA_VERSION
            )
            self.assertTrue((staging / f"{audit.PMC_PDF_BLOCKER}.pdf").is_file())
            audit.validate_staged_complete_set(staging, source_records)
            (staging / f"{audit.PMC_PDF_BLOCKER}.pdf").write_bytes(pdf_raw + b"tamper")
            with self.assertRaisesRegex(audit.AuditInvariantError, "raw digest"):
                audit.validate_staged_complete_set(staging, source_records)


class CompletedLedgerValidationTests(unittest.TestCase):
    def synthetic_ledger(self, output, accepted_ids=None):
        accepted_ids = set(accepted_ids or {"PMC12532455"})
        context_ids = list(audit.EXPECTED_CONTEXT_IDS)
        lineage = audit.expected_repair_lineage()
        sources = {}
        for pmcid, pmid in audit.EXPECTED_BLOCKERS.items():
            accepted = pmcid in accepted_ids
            raw = complete_pmc_html(pmcid, pmid) if accepted else b""
            html_validation, _ = audit.validate_pmc_html_receipt(
                raw,
                expected_pmcid=pmcid,
                expected_pmid=pmid,
            )
            empty_jats, _ = audit.validate_jats_receipt(
                b"", expected_pmcid=pmcid, expected_pmid=pmid
            )
            empty_html, _ = audit.validate_pmc_html_receipt(
                b"", expected_pmcid=pmcid, expected_pmid=pmid
            )
            sources[pmcid] = {
                "pmid": pmid,
                "context_ids": sorted(
                    value for value in context_ids if value.startswith(f"{pmid}|")
                ),
                "routes": [
                    {
                        "route": "ncbi_entrez_efetch_pmc_xml",
                        "transport": audit._receipt_transport(
                            receipt(audit._efetch_url(pmcid, ""), b"", status=404)
                        ),
                        "validation": empty_jats,
                    },
                    {
                        "route": "pmc_oai_pmh_metadata_prefix_pmc",
                        "transport": audit._receipt_transport(
                            receipt(audit._oai_url(pmcid), b"", status=404)
                        ),
                        "validation": empty_jats,
                    },
                    {
                        "route": "pmc_article_datasets_s3",
                        "listing_transport": audit._receipt_transport(
                            receipt(audit._s3_list_url(pmcid), b"", status=404)
                        ),
                        "listing_validation": {
                            "xml_well_formed": False,
                            "is_truncated": None,
                            "prefixes": [],
                            "versions": [],
                            "http_status": 404,
                            "reason": "http_status_not_200",
                        },
                        "version_attempts": [],
                        "validation": {
                            "valid": False,
                            "reason": "http_status_not_200",
                        },
                    },
                    {
                        "route": "pmc_canonical_article_html",
                        "transport": audit._receipt_transport(
                            receipt(
                                audit._pmc_html_url(pmcid),
                                raw,
                                status=200 if accepted else 404,
                            )
                        ),
                        "validation": html_validation if accepted else empty_html,
                    },
                    (
                        {
                            "route": audit.PMC_PDF_ROUTE,
                            "skipped": True,
                            "skip_reason": "prior_official_route_validated",
                            "validation": {
                                "valid": False,
                                "reason": "skipped_after_prior_valid_receipt",
                            },
                        }
                        if accepted
                        else (
                            {
                                "route": audit.PMC_PDF_ROUTE,
                                "skipped": True,
                                "skip_reason": (
                                    "exact_pdf_fallback_prerequisite_not_met"
                                ),
                                "observed_canonical_html_reason": empty_html["reason"],
                                "validation": {
                                    "valid": False,
                                    "reason": (
                                        "skipped_pdf_fallback_prerequisite_not_met"
                                    ),
                                },
                            }
                            if pmcid == audit.PMC_PDF_BLOCKER
                            else {
                                "route": audit.PMC_PDF_ROUTE,
                                "skipped": True,
                                "skip_reason": "outside_exact_bounded_pdf_scope",
                                "validation": {
                                    "valid": False,
                                    "reason": (
                                        "skipped_outside_exact_bounded_pdf_scope"
                                    ),
                                },
                            }
                        )
                    ),
                ],
                "accepted": accepted,
                "accepted_route": "html" if accepted else None,
                "accepted_source_format": (
                    audit.PMC_HTML_FORMAT if accepted else None
                ),
                "terminal_reason": (
                    "complete_strict_pmc_source_receipt_validated"
                    if accepted
                    else "no_official_complete_pmc_source_route_validated"
                ),
            }
        unresolved = set(audit.EXPECTED_BLOCKERS) - accepted_ids
        ledger = {
            "schema_version": audit.LEDGER_SCHEMA_VERSION,
            "official_routes_only": True,
            "complete_official_pmc_source_required": True,
            "accepted_source_formats": [
                audit.JATS_XML_FORMAT,
                audit.PMC_HTML_FORMAT,
                audit.PMC_PDF_FORMAT,
            ],
            "abstracts_accepted_as_full_text": False,
            "all_or_none_gate": True,
            "scope": {
                "output_dir": str(output),
                "required_sources": audit.EXPECTED_BLOCKERS,
                "required_source_count": len(audit.EXPECTED_BLOCKERS),
                "required_context_count": audit.EXPECTED_CONTEXT_COUNT,
                "scope_digest": audit.expected_repair_scope_digest(),
                **lineage,
            },
            "validation_contract": audit.retrieval_validation_contract(),
            "sources": sources,
            "validated_source_count": len(accepted_ids),
            "validated_source_ids": sorted(accepted_ids),
            "unresolved_source_ids": sorted(unresolved),
            "all_sources_complete": not unresolved,
            "decision": (
                "all_sources_validated_stage_ready"
                if not unresolved
                else "preserve_blocked_incomplete_pmc_source"
            ),
            "staging_path": None,
            "production_artifacts_unchanged": True,
            "superseded_ledger": None,
        }
        return ledger, lineage

    def test_validates_exact_incomplete_ledger_and_preserved_block(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            ledger, lineage = self.synthetic_ledger(output)
            with patch.object(audit, "verify_production_scope", return_value=lineage):
                validation = audit.validate_audit_ledger(output, ledger)
            self.assertEqual(validation["status"], "validated")
            self.assertEqual(validation["validated_source_count"], 1)
            self.assertEqual(validation["unresolved_source_count"], 9)
            self.assertTrue(validation["production_block_preserved"])

    def test_rejects_tampered_validated_count(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            ledger, lineage = self.synthetic_ledger(output)
            ledger["validated_source_count"] = 2
            with patch.object(audit, "verify_production_scope", return_value=lineage):
                with self.assertRaises(audit.AuditInvariantError):
                    audit.validate_audit_ledger(output, ledger)

    def test_rejects_missing_transport_and_redirect_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            ledger, lineage = self.synthetic_ledger(output)
            pmcid = next(iter(audit.EXPECTED_BLOCKERS))
            del ledger["sources"][pmcid]["routes"][0]["transport"]
            with patch.object(audit, "verify_production_scope", return_value=lineage):
                with self.assertRaisesRegex(
                    audit.AuditInvariantError, "missing its transport receipt"
                ):
                    audit.validate_audit_ledger(output, ledger)

            ledger, lineage = self.synthetic_ledger(output)
            ledger["sources"][pmcid]["routes"][0]["transport"]["final_url"] = (
                "https://example.invalid/redirect"
            )
            with patch.object(audit, "verify_production_scope", return_value=lineage):
                with self.assertRaisesRegex(
                    audit.AuditInvariantError, "effective URL differs"
                ):
                    audit.validate_audit_ledger(output, ledger)

    def test_records_rejected_redirect_but_rejects_redirected_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            ledger, lineage = self.synthetic_ledger(output)
            rejected_pmcid = next(iter(audit.EXPECTED_BLOCKERS))
            rejected_route = ledger["sources"][rejected_pmcid]["routes"][0]
            rejected_route["transport"]["redirected"] = True
            rejected_route["transport"]["attempts"][-1]["redirected"] = True
            rejected_route["validation"] = {
                "valid": False,
                "reason": "redirected_transport_url",
            }
            with patch.object(audit, "verify_production_scope", return_value=lineage):
                validation = audit.validate_audit_ledger(output, ledger)
            self.assertEqual(validation["status"], "validated")

            ledger, lineage = self.synthetic_ledger(output)
            accepted_pmcid = "PMC12532455"
            accepted_route = ledger["sources"][accepted_pmcid]["routes"][3]
            accepted_route["transport"]["redirected"] = True
            accepted_route["transport"]["attempts"][-1]["redirected"] = True
            with patch.object(audit, "verify_production_scope", return_value=lineage):
                with self.assertRaisesRegex(
                    audit.AuditInvariantError, "explicitly rejected"
                ):
                    audit.validate_audit_ledger(output, ledger)

    def test_rejects_pdf_fallback_after_prior_valid_official_route(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            pmcid = audit.PMC_PDF_BLOCKER
            pmid = audit.EXPECTED_BLOCKERS[pmcid]
            ledger, lineage = self.synthetic_ledger(output, {pmcid})

            jats_raw = complete_jats(pmcid, pmid)
            jats_validation, _ = audit.validate_jats_receipt(
                jats_raw, expected_pmcid=pmcid, expected_pmid=pmid
            )
            helper = ExactPmcPdfFallbackTests(
                "test_probe_seals_link_challenge_solution_cookie_and_exact_pdf_content_type"
            )
            pdf_route, *_ = helper.probe()
            target = ledger["sources"][pmcid]
            target["routes"][0] = {
                "route": "ncbi_entrez_efetch_pmc_xml",
                "transport": audit._receipt_transport(
                    receipt(audit._efetch_url(pmcid, ""), jats_raw)
                ),
                "validation": jats_validation,
            }
            target["routes"][3] = {
                "route": "pmc_canonical_article_html",
                "transport": pdf_route["link_html"]["transport"],
                "validation": pdf_route["link_html"]["semantic_validation"],
            }
            target["routes"][4] = pdf_route
            target["accepted_route"] = "efetch"
            target["accepted_source_format"] = audit.JATS_XML_FORMAT

            with patch.object(
                audit, "verify_production_scope", return_value=lineage
            ), self.assertRaisesRegex(
                audit.AuditInvariantError,
                "PDF fallback ran after a prior valid official route",
            ):
                audit.validate_audit_ledger(output, ledger)

    def test_consumption_rejects_self_declared_lineage_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "candidates.json").write_text("{}")
            (output / "chatgpt_second_screen.json").write_text("{}")
            ledger, _lineage = self.synthetic_ledger(output)
            ledger["scope"]["source_manifest_digest"] = "e" * 64

            def fixed_hash(path):
                return (
                    audit.EXPECTED_CANDIDATE_SHA256
                    if Path(path).name == "candidates.json"
                    else audit.EXPECTED_SECOND_SHA256
                )

            with patch.object(audit, "file_sha256", side_effect=fixed_hash):
                with self.assertRaisesRegex(
                    audit.AuditInvariantError, "lineage drift"
                ):
                    audit.validate_audit_ledger(
                        output,
                        ledger,
                        require_blocked_production=False,
                    )

    def test_validates_complete_mixed_jats_and_html_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            accepted_ids = set(audit.EXPECTED_BLOCKERS)
            ledger, lineage = self.synthetic_ledger(output, accepted_ids)
            records = {}
            first_pmcid = next(iter(audit.EXPECTED_BLOCKERS))
            for pmcid, pmid in audit.EXPECTED_BLOCKERS.items():
                if pmcid == first_pmcid:
                    raw = complete_jats(pmcid, pmid)
                    source_format = audit.JATS_XML_FORMAT
                    route = "efetch"
                else:
                    raw = complete_pmc_html(pmcid, pmid)
                    source_format = audit.PMC_HTML_FORMAT
                    route = "html"
                validation, text = audit.validate_complete_source_receipt(
                    raw,
                    source_format=source_format,
                    expected_pmcid=pmcid,
                    expected_pmid=pmid,
                )
                records[pmcid] = {
                    "pmcid": pmcid,
                    "pmid": pmid,
                    "route": route,
                    "source_format": source_format,
                    "validation": validation,
                    "text": text,
                    "raw": raw,
                }
                source = ledger["sources"][pmcid]
                if route == "efetch":
                    source["routes"][0]["validation"] = validation
                    source["routes"][0]["transport"] = audit._receipt_transport(
                        receipt(audit._efetch_url(pmcid, ""), raw)
                    )
                    empty_html, _ = audit.validate_pmc_html_receipt(
                        b"", expected_pmcid=pmcid, expected_pmid=pmid
                    )
                    source["routes"][3]["validation"] = empty_html
                    source["routes"][3]["transport"] = audit._receipt_transport(
                        receipt(audit._pmc_html_url(pmcid), b"", status=404)
                    )
                source["accepted_route"] = route
                source["accepted_source_format"] = source_format
            staging = audit.stage_complete_set(output, records)
            ledger["staging_path"] = str(staging)
            with patch.object(audit, "verify_production_scope", return_value=lineage):
                validation = audit.validate_audit_ledger(output, ledger)
            self.assertTrue(validation["all_sources_complete"])
            self.assertEqual(validation["validated_source_count"], 10)

    def test_validates_complete_ledger_with_exact_pdf_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            ledger, lineage = self.synthetic_ledger(
                output, set(audit.EXPECTED_BLOCKERS)
            )
            helper = ExactPmcPdfFallbackTests(
                "test_probe_seals_link_challenge_solution_cookie_and_exact_pdf_content_type"
            )
            (
                pdf_route,
                _text,
                _raw,
                _calls,
                pdf_validation,
                pdf_text,
                pdf_raw,
            ) = helper.probe()
            target = ledger["sources"][audit.PMC_PDF_BLOCKER]
            target["routes"][3] = {
                "route": "pmc_canonical_article_html",
                "transport": pdf_route["link_html"]["transport"],
                "validation": pdf_route["link_html"]["semantic_validation"],
            }
            target["routes"][4] = pdf_route
            target["accepted_route"] = "pdf"
            target["accepted_source_format"] = audit.PMC_PDF_FORMAT

            records = {}
            for pmcid, pmid in audit.EXPECTED_BLOCKERS.items():
                if pmcid == audit.PMC_PDF_BLOCKER:
                    raw = pdf_raw
                    text = pdf_text
                    validation = pdf_validation
                    source_format = audit.PMC_PDF_FORMAT
                    route = "pdf"
                else:
                    raw = complete_pmc_html(pmcid, pmid)
                    validation, text = audit.validate_pmc_html_receipt(
                        raw, expected_pmcid=pmcid, expected_pmid=pmid
                    )
                    source_format = audit.PMC_HTML_FORMAT
                    route = "html"
                records[pmcid] = {
                    "pmcid": pmcid,
                    "pmid": pmid,
                    "route": route,
                    "source_format": source_format,
                    "validation": validation,
                    "text": text,
                    "raw": raw,
                }
            with patch.object(
                audit.strict_pdf,
                "validate_pmc_pdf_receipt",
                return_value=(pdf_validation, pdf_text),
            ):
                staging = audit.stage_complete_set(output, records)
                ledger["staging_path"] = str(staging)
                with patch.object(
                    audit, "verify_production_scope", return_value=lineage
                ):
                    validation = audit.validate_audit_ledger(output, ledger)
            self.assertTrue(validation["all_sources_complete"])
            self.assertEqual(
                ledger["sources"][audit.PMC_PDF_BLOCKER]["accepted_source_format"],
                audit.PMC_PDF_FORMAT,
            )


class AuditTransactionTests(unittest.TestCase):
    def test_unsealed_interrupted_staging_is_archived_recoverably(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            staging = output / audit.STAGING_DIRNAME
            staging.mkdir()
            (staging / "manifest.json").write_text("{}")
            self.assertIsNone(audit._resume_or_archive_existing_staging(output))
            self.assertFalse(staging.exists())
            archived = list((output / "superseded_generations").iterdir())
            self.assertEqual(len(archived), 1)
            self.assertTrue((archived[0] / "manifest.json").is_file())

    def test_stop_after_first_valid_route_writes_explicit_skips_and_validates(self):
        lineage = audit.expected_repair_lineage()

        def valid_efetch(pmcid, pmid, *, email, user_agent):
            raw = complete_jats(pmcid, pmid)
            validation, text = audit.validate_jats_receipt(
                raw, expected_pmcid=pmcid, expected_pmid=pmid
            )
            return (
                {
                    "route": "ncbi_entrez_efetch_pmc_xml",
                    "transport": audit._receipt_transport(
                        receipt(audit._efetch_url(pmcid, ""), raw)
                    ),
                    "validation": validation,
                },
                text,
                raw,
            )

        blocked_probe = Mock(side_effect=AssertionError("later route must be skipped"))
        with tempfile.TemporaryDirectory() as directory, patch.object(
            audit, "verify_production_scope", return_value=lineage
        ), patch.object(audit, "probe_efetch", side_effect=valid_efetch), patch.object(
            audit, "probe_oai", blocked_probe
        ), patch.object(audit, "probe_s3", blocked_probe), patch.object(
            audit, "probe_pmc_html", blocked_probe
        ):
            output = Path(directory)
            ledger = audit.run_audit(output, probe_all_routes=False)
            self.assertTrue(ledger["all_sources_complete"])
            for record in ledger["sources"].values():
                self.assertEqual(len(record["routes"]), 5)
                self.assertTrue(all(route.get("skipped") for route in record["routes"][1:]))
            blocked_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
