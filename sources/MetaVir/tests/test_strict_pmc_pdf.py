import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import strict_pmc_pdf as pdf


def pinned_pdf_fixture():
    raw = b"%PDF-1.7\n" + (b"strict complete article bytes\n" * 2000) + b"%%EOF\n"
    identity = dict(pdf.EXPECTED_PDF_IDENTITIES["PMC11160038"])
    identity["sha256"] = hashlib.sha256(raw).hexdigest()
    repeated = " Cohort methods and quantitative evidence." * 140
    pages = [
        f"{identity['title']}\nMethods\n{repeated}",
        f"Results\n{repeated}",
        repeated,
        f"Discussion\n{repeated}",
        repeated,
        repeated,
        repeated,
    ]
    text = "\n\n".join(pages)
    identity.update(
        {
            "poppler_version": "test-version",
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "page_sha256": [
                hashlib.sha256(page.encode()).hexdigest() for page in pages
            ],
            "allowed_external_urls": ["mailto:bounded@example.invalid"],
        }
    )
    inspection = {
        "poppler_version": "test-version",
        "info": {
            "Encrypted": "no",
            "Pages": "7",
            "JavaScript": "no",
            "Form": "none",
        },
        "custom": {
            "Title": identity["title"],
            "WPS-ARTICLEDOI": identity["doi"],
        },
        "javascript": "",
        "external_urls": ["mailto:bounded@example.invalid"],
        "embedded_file_count": 0,
        "pages": pages,
        "text": text,
        "text_sha256": identity["text_sha256"],
        "page_sha256": identity["page_sha256"],
    }
    return raw, identity, inspection


def columnar_pdf_fixture():
    raw = b"%PDF-1.7\n" + (b"strict columnar article bytes\n" * 2000) + b"%%EOF\n"
    identity = dict(pdf.EXPECTED_PDF_IDENTITIES["PMC11159243"])
    identity["sha256"] = hashlib.sha256(raw).hexdigest()
    repeated = " Quantitative case-control evidence." * 200
    pages = [
        f"{identity['title']}\nMethods\n{repeated}",
        repeated,
        f"Discussion\n{repeated}\nResults\n{repeated}",
        repeated,
        repeated,
    ]
    text = "\n\n".join(pages)
    identity.update(
        {
            "poppler_version": "test-version",
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "page_sha256": [hashlib.sha256(page.encode()).hexdigest() for page in pages],
            "page_characters": [len(page) for page in pages],
            "required_section_offsets": {
                section: text.find(section)
                for section in pdf.REQUIRED_NARRATIVE_SECTIONS
            },
            "allowed_external_urls": [],
        }
    )
    inspection = {
        "poppler_version": "test-version",
        "info": {
            "Encrypted": "no",
            "Pages": "5",
            "JavaScript": "no",
            "Form": "none",
        },
        "custom": {
            "Title": identity["title"],
            "WPS-ARTICLEDOI": identity["doi"],
        },
        "javascript": "",
        "external_urls": [],
        "embedded_file_count": 0,
        "pages": pages,
        "text": text,
        "text_sha256": identity["text_sha256"],
        "page_sha256": identity["page_sha256"],
    }
    return raw, identity, inspection


class StrictPmcPdfTests(unittest.TestCase):
    def test_accepts_only_pinned_complete_identity_and_narrative(self):
        raw, identity, inspection = pinned_pdf_fixture()
        with patch.dict(
            pdf.EXPECTED_PDF_IDENTITIES, {"PMC11160038": identity}, clear=True
        ), patch.object(pdf, "_inspect_pdf_with_poppler", return_value=inspection):
            validation, text = pdf.validate_pmc_pdf_receipt(
                raw,
                expected_pmcid="PMC11160038",
                expected_pmid="14720329",
            )
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["page_count"], 7)
        self.assertGreaterEqual(
            validation["extracted_characters"], pdf.MIN_PDF_EXTRACTED_CHARACTERS
        )
        self.assertTrue(all(validation["required_sections"].values()))
        self.assertIn("Methods", text)

    def test_rejects_unpinned_or_truncated_pdf_before_parsing(self):
        raw, identity, _inspection = pinned_pdf_fixture()
        cases = [
            (raw.replace(b"article", b"Article", 1), "pdf_sha256_not_exactly_pinned"),
            (raw[:-6], "pdf_container_not_closed"),
        ]
        for candidate, reason in cases:
            with self.subTest(reason=reason), patch.dict(
                pdf.EXPECTED_PDF_IDENTITIES,
                {"PMC11160038": identity},
                clear=True,
            ), patch.object(
                pdf,
                "_inspect_pdf_with_poppler",
                side_effect=AssertionError("must not parse"),
            ):
                validation, text = pdf.validate_pmc_pdf_receipt(
                    candidate,
                    expected_pmcid="PMC11160038",
                    expected_pmid="14720329",
                )
            self.assertFalse(validation["valid"])
            self.assertEqual(validation["reason"], reason)
            self.assertEqual(text, "")

    def test_scope_is_exactly_bounded(self):
        with self.assertRaises(pdf.StrictPmcPdfError):
            pdf.expected_pdf_identity("PMC2781742", "19808637")
        with self.assertRaises(pdf.StrictPmcPdfError):
            pdf.expected_pdf_identity("PMC11159243", "14720329")
        with self.assertRaises(pdf.StrictPmcPdfError):
            pdf.expected_pdf_identity("PMC11160038", "15016323")

    def test_second_identity_is_exactly_pinned_and_url_resolution_is_unique(self):
        identity = pdf.expected_pdf_identity("PMC11159243", "15016323")
        self.assertEqual(
            identity["url"],
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC11159243/pdf/CAS-95-238.pdf",
        )
        self.assertEqual(identity["pages"], 5)
        self.assertEqual(
            identity["title"],
            "A case-control study of diet and prostate cancer in Japan: "
            "possible protective effect of traditional Japanese diet",
        )
        self.assertEqual(identity["doi"], "10.1111/j.1349-7006.2004.tb02209.x")
        self.assertEqual(
            identity["page_characters"], [8232, 7802, 8186, 11326, 12696]
        )
        self.assertEqual(identity["allowed_external_urls"], [])
        self.assertEqual(
            identity["required_section_offsets"],
            {"Methods": 1870, "Results": 19431, "Discussion": 16921},
        )
        self.assertEqual(
            identity["sha256"],
            "af6aa8c11c266247ba7699a8c2acc17451d69972fa4a4e7c5072bc2227755701",
        )
        self.assertEqual(
            identity["text_sha256"],
            "9fc7aa75f73416552c7831701b1ea14e9756bfe8c0d4397e50e8daf5e890364e",
        )
        self.assertEqual(
            identity["page_sha256"],
            [
                "292e1b329491355ca38d9535a025bfcd4b75c9b3b0fb50efac7279f4011af899",
                "11252ddf44870ee65931f217c3722cdd89aee47692a3ebeb23514da0597b86bb",
                "73a9caa0872b6e77cf815d7f5ccb5fb776c85b8106f7715df97d9edda3417c32",
                "7fd3075dc21ef9a9263e1d518ca887521787bb216a350be328a4534570b9660e",
                "44888c352e326774d3d89bdffd04437e29480a98b8559649810148dd29b368be",
            ],
        )
        pmcid, resolved = pdf.expected_pdf_identity_for_url(identity["url"])
        self.assertEqual(pmcid, "PMC11159243")
        self.assertIs(resolved, identity)
        with self.assertRaises(pdf.StrictPmcPdfError):
            pdf.expected_pdf_identity_for_url(identity["url"] + "?download=1")

    def test_columnar_identity_uses_exact_offsets_not_generic_section_order(self):
        raw, identity, inspection = columnar_pdf_fixture()
        self.assertGreater(
            identity["required_section_offsets"]["Results"],
            identity["required_section_offsets"]["Discussion"],
        )
        with patch.dict(
            pdf.EXPECTED_PDF_IDENTITIES, {"PMC11159243": identity}, clear=True
        ), patch.object(pdf, "_inspect_pdf_with_poppler", return_value=inspection):
            validation, text = pdf.validate_pmc_pdf_receipt(
                raw,
                expected_pmcid="PMC11159243",
                expected_pmid="15016323",
            )
        self.assertTrue(validation["valid"])
        self.assertTrue(validation["required_section_order_valid"])
        self.assertEqual(
            validation["required_section_offsets"],
            identity["required_section_offsets"],
        )
        self.assertEqual(text, inspection["text"])

        wrong_offsets = dict(identity)
        wrong_offsets["required_section_offsets"] = {
            **identity["required_section_offsets"],
            "Results": identity["required_section_offsets"]["Results"] + 1,
        }
        with patch.dict(
            pdf.EXPECTED_PDF_IDENTITIES,
            {"PMC11159243": wrong_offsets},
            clear=True,
        ), patch.object(pdf, "_inspect_pdf_with_poppler", return_value=inspection):
            rejected, rejected_text = pdf.validate_pmc_pdf_receipt(
                raw,
                expected_pmcid="PMC11159243",
                expected_pmid="15016323",
            )
        self.assertFalse(rejected["valid"])
        self.assertEqual(
            rejected["reason"], "pdf_required_narrative_section_offsets_differ"
        )
        self.assertEqual(rejected_text, "")

    def test_columnar_identity_rejects_page_character_or_page_hash_drift(self):
        raw, identity, inspection = columnar_pdf_fixture()
        changed_cases = [
            (
                {
                    **inspection,
                    "page_sha256": [
                        "0" * 64,
                        *inspection["page_sha256"][1:],
                    ],
                },
                "pdf_page_text_digests_differ",
            ),
            (
                {
                    **inspection,
                    "pages": [
                        inspection["pages"][0] + "x",
                        *inspection["pages"][1:],
                    ],
                },
                "pdf_page_text_character_counts_differ",
            ),
        ]
        for changed, reason in changed_cases:
            with self.subTest(reason=reason), patch.dict(
                pdf.EXPECTED_PDF_IDENTITIES,
                {"PMC11159243": identity},
                clear=True,
            ), patch.object(pdf, "_inspect_pdf_with_poppler", return_value=changed):
                validation, text = pdf.validate_pmc_pdf_receipt(
                    raw,
                    expected_pmcid="PMC11159243",
                    expected_pmid="15016323",
                )
            self.assertFalse(validation["valid"])
            self.assertEqual(validation["reason"], reason)
            self.assertEqual(text, "")

    def test_rejects_changed_text_digest_or_active_content_inventory(self):
        raw, identity, inspection = pinned_pdf_fixture()
        cases = [
            ({**inspection, "text_sha256": "0" * 64}, "pdf_extracted_text_digest_differs"),
            (
                {**inspection, "external_urls": ["https://example.invalid/"]},
                "pdf_active_content_inventory_differs",
            ),
        ]
        for changed, reason in cases:
            with self.subTest(reason=reason), patch.dict(
                pdf.EXPECTED_PDF_IDENTITIES,
                {"PMC11160038": identity},
                clear=True,
            ), patch.object(pdf, "_inspect_pdf_with_poppler", return_value=changed):
                validation, text = pdf.validate_pmc_pdf_receipt(
                    raw,
                    expected_pmcid="PMC11160038",
                    expected_pmid="14720329",
                )
            self.assertFalse(validation["valid"])
            self.assertEqual(validation["reason"], reason)
            self.assertEqual(text, "")

    def test_pdfinfo_field_parser_rejects_malformed_and_duplicate_rows(self):
        self.assertEqual(
            pdf._colon_fields("Title: Exact\n\nPages: 7\n", source="pdfinfo"),
            {"Title": "Exact", "Pages": "7"},
        )
        for malformed in (
            "Title: Exact\nnot-a-field\n",
            "Title: First\nTitle: Second\n",
            "Title: First\ntitle: Second\n",
            ": missing key\n",
        ):
            with self.subTest(malformed=malformed), self.assertRaises(
                pdf.StrictPmcPdfError
            ):
                pdf._colon_fields(malformed, source="pdfinfo")

    def test_pdfinfo_url_parser_requires_exact_header_and_every_row(self):
        valid = (
            f"{pdf.PDFINFO_URL_HEADER}\n"
            "   1  Annotation    mailto:first@example.invalid\n"
            "   7  Annotation    https://example.invalid/path?q=1\n"
        )
        self.assertEqual(
            pdf._parse_pdfinfo_urls(valid),
            ["mailto:first@example.invalid", "https://example.invalid/path?q=1"],
        )
        malformed_values = (
            "Page Type URL\n   1  Annotation    mailto:x@example.invalid\n",
            f"{pdf.PDFINFO_URL_HEADER}\nnot a complete row\n",
            f"{pdf.PDFINFO_URL_HEADER}\n   1  Action    https://example.invalid\n",
            f"{pdf.PDFINFO_URL_HEADER}\n   0  Annotation    https://example.invalid\n",
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed), self.assertRaises(
                pdf.StrictPmcPdfError
            ):
                pdf._parse_pdfinfo_urls(malformed)

    def test_poppler_tools_must_share_directory_and_exact_version(self):
        def matching_process(command, **_kwargs):
            tool_name = Path(command[0]).name
            return SimpleNamespace(
                returncode=99 if tool_name == "pdfdetach" else 0,
                stdout=b"",
                stderr=(
                    f"{tool_name} version {pdf.PINNED_POPPLER_VERSION}\n"
                ).encode(),
            )

        with patch.object(
            pdf.shutil, "which", side_effect=lambda name: f"/one/bin/{name}"
        ), patch.object(pdf.subprocess, "run", side_effect=matching_process):
            tools = pdf._resolve_pinned_poppler_tools()
        self.assertEqual(set(tools), set(pdf.REQUIRED_POPPLER_TOOLS))
        self.assertEqual({path.parent for path in tools.values()}, {Path("/one/bin")})

        mixed = {
            "pdfinfo": "/one/bin/pdfinfo",
            "pdftotext": "/two/bin/pdftotext",
            "pdfdetach": "/one/bin/pdfdetach",
        }
        with patch.object(pdf.shutil, "which", side_effect=mixed.get), patch.object(
            pdf.subprocess,
            "run",
            side_effect=AssertionError("mixed directories must fail before version calls"),
        ), self.assertRaises(pdf.StrictPmcPdfError):
            pdf._resolve_pinned_poppler_tools()

        def mismatched_process(command, **_kwargs):
            tool_name = Path(command[0]).name
            version = (
                "25.12.0"
                if tool_name == "pdftotext"
                else pdf.PINNED_POPPLER_VERSION
            )
            return SimpleNamespace(
                returncode=99 if tool_name == "pdfdetach" else 0,
                stdout=b"",
                stderr=f"{tool_name} version {version}\n".encode(),
            )

        with patch.object(
            pdf.shutil, "which", side_effect=lambda name: f"/one/bin/{name}"
        ), patch.object(
            pdf.subprocess, "run", side_effect=mismatched_process
        ), self.assertRaises(pdf.StrictPmcPdfError):
            pdf._resolve_pinned_poppler_tools()

        def duplicate_banner_process(command, **_kwargs):
            tool_name = Path(command[0]).name
            banner = f"{tool_name} version {pdf.PINNED_POPPLER_VERSION}\n"
            return SimpleNamespace(
                returncode=99 if tool_name == "pdfdetach" else 0,
                stdout=banner.encode(),
                stderr=banner.encode(),
            )

        with patch.object(
            pdf.shutil, "which", side_effect=lambda name: f"/one/bin/{name}"
        ), patch.object(
            pdf.subprocess, "run", side_effect=duplicate_banner_process
        ), self.assertRaises(pdf.StrictPmcPdfError):
            pdf._resolve_pinned_poppler_tools()


class PmcPowTests(unittest.TestCase):
    def challenge_html(self, challenge="boundedChallenge:abc123", difficulty="4"):
        return f'''<html><head><title>Preparing to download ...</title></head>
        <body><h1>Preparing to download ...</h1></body>
        <script>
        const POW_CHALLENGE = "{challenge}"
        const POW_DIFFICULTY = "{difficulty}"
        const POW_COOKIE_NAME = "cloudpmc-viewer-pow"
        const POW_COOKIE_PATH = "/"
        </script></html>'''.encode()

    def test_validates_solves_and_revalidates_pow(self):
        raw = self.challenge_html()
        validation, challenge = pdf.validate_pow_challenge_receipt(raw)
        self.assertTrue(validation["valid"])
        solution = pdf.solve_pow_challenge(challenge)
        pdf.validate_pow_solution_against_receipt(
            solution, validation, challenge, raw
        )
        self.assertEqual(
            pdf.pow_cookie_value(solution),
            f"{solution['challenge']},{solution['nonce']}",
        )

    def test_rejects_duplicate_or_wrong_cookie_scope(self):
        duplicate = self.challenge_html() + self.challenge_html()
        validation, _ = pdf.validate_pow_challenge_receipt(duplicate)
        self.assertFalse(validation["valid"])
        wrong_cookie = self.challenge_html().replace(
            b'"cloudpmc-viewer-pow"', b'"other-cookie"'
        )
        validation, _ = pdf.validate_pow_challenge_receipt(wrong_cookie)
        self.assertFalse(validation["valid"])

    def test_rejects_tampered_solution(self):
        _, challenge = pdf.validate_pow_challenge_receipt(self.challenge_html())
        solution = pdf.solve_pow_challenge(challenge)
        solution["nonce"] += 1
        with self.assertRaises(pdf.StrictPmcPdfError):
            pdf.validate_pow_solution(solution)

    def test_rejects_solution_swapped_from_another_valid_challenge(self):
        receipt_raw = self.challenge_html(challenge="receiptChallenge:abc123")
        receipt_validation, receipt_challenge = pdf.validate_pow_challenge_receipt(
            receipt_raw
        )
        _, other_challenge = pdf.validate_pow_challenge_receipt(
            self.challenge_html(challenge="differentChallenge:xyz789")
        )
        solution = pdf.solve_pow_challenge(other_challenge)
        with self.assertRaises(pdf.StrictPmcPdfError):
            pdf.validate_pow_solution_against_receipt(
                solution, receipt_validation, receipt_challenge, receipt_raw
            )

    def test_rejects_fabricated_or_body_mismatched_pow_receipt(self):
        raw = self.challenge_html(challenge="receiptChallenge:abc123")
        validation, challenge = pdf.validate_pow_challenge_receipt(raw)
        solution = pdf.solve_pow_challenge(challenge)
        fabricated = {
            key: value
            for key, value in validation.items()
            if key not in {"bytes", "sha256", "reason"}
        }
        with self.assertRaises(pdf.StrictPmcPdfError):
            pdf.validate_pow_solution_against_receipt(
                solution, fabricated, challenge, raw
            )
        changed_raw = raw.replace(b"</html>", b" \n</html>")
        changed_validation, changed_challenge = pdf.validate_pow_challenge_receipt(
            changed_raw
        )
        self.assertTrue(changed_validation["valid"])
        self.assertEqual(changed_challenge, challenge)
        with self.assertRaises(pdf.StrictPmcPdfError):
            pdf.validate_pow_solution_against_receipt(
                solution, validation, challenge, changed_raw
            )


if __name__ == "__main__":
    unittest.main()
