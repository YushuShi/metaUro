#!/usr/bin/env python3
"""Strict, bounded validation helpers for official PMC PDF fallbacks."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any


PMC_PDF_FORMAT = "pmc_pdf"
POW_COOKIE_NAME = "cloudpmc-viewer-pow"
EXPECTED_POW_DIFFICULTY = 4
MAX_POW_NONCE = 5_000_000
MIN_PDF_BYTES = 50_000
MIN_PDF_EXTRACTED_CHARACTERS = 40_000
MIN_PDF_PAGE_CHARACTERS = 3_000
REQUIRED_NARRATIVE_SECTIONS = ("Methods", "Results", "Discussion")
PINNED_POPPLER_VERSION = "26.07.0"
REQUIRED_POPPLER_TOOLS = ("pdfinfo", "pdftotext", "pdfdetach")
PDFINFO_URL_HEADER = "Page  Type          URL"
ALLOWED_OFFICIAL_PDF_CONTENT_TYPES = (
    "application/pdf",
    "application/pdf; charset=utf-8",
)

# This module is intentionally bounded to exact blocker identities whose
# canonical PMC HTML is an incomplete shell while PMC supplies the complete
# article as a linked PDF.  The raw digest prevents a different PDF from being
# substituted merely because its metadata happens to match.
EXPECTED_PDF_IDENTITIES: dict[str, dict[str, Any]] = {
    "PMC11160038": {
        "pmid": "14720329",
        "url": (
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC11160038/"
            "pdf/CAS-95-65.pdf"
        ),
        "title": (
            "Serum phytoestrogens and prostate cancer risk in a nested "
            "case-control study among Japanese men"
        ),
        "doi": "10.1111/j.1349-7006.2004.tb03172.x",
        "sha256": "bbc21c16144488e55f52bd85924e1eae10337ce4768c4fb43c94b9f55b66b345",
        "pages": 7,
        "poppler_version": PINNED_POPPLER_VERSION,
        "text_sha256": "8a0cdb68f7f8ecf7ec89b949aa577a4379f87c30f07d9a6f4dd7374a159d6132",
        "page_sha256": [
            "05c498d6926cb0da0904e0d4cefed794067acb3a87797450fb6156249619fa43",
            "3de08b562221f49c64eff06c21326caba3f55991cb02792626049538f7933505",
            "e607455d9595d41207206ff87b68f3eae0cba0c99a82f33be12eb02e90dd51ed",
            "9d823a79f93250571eee2b01d7dbaadc64dd3574584be97bbb89340728843ba4",
            "27a0b68929d6dc25597a4331dfbdc069e2608bc72984ca83f76172f2e8636fff",
            "2a38a0836c3283a805376c09bfad94c820f963419787c028f899a5853d15f8a7",
            "767a6212c39f139e7e7feb8265d4e2e00eb93f6da844e896c1b4d23f0dd7052b",
        ],
        "allowed_external_urls": [
            "mailto:kozasa@basic.kpu-m.ac.jp",
            "mailto:kozasa@basic.kpu-m.ac.jp",
        ],
        "html_shell_reason": "non_abstract_narrative_not_substantive",
    },
    "PMC11159243": {
        "pmid": "15016323",
        "url": (
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC11159243/"
            "pdf/CAS-95-238.pdf"
        ),
        "title": (
            "A case-control study of diet and prostate cancer in Japan: "
            "possible protective effect of traditional Japanese diet"
        ),
        "doi": "10.1111/j.1349-7006.2004.tb02209.x",
        "sha256": "af6aa8c11c266247ba7699a8c2acc17451d69972fa4a4e7c5072bc2227755701",
        "pages": 5,
        "poppler_version": PINNED_POPPLER_VERSION,
        "text_sha256": "9fc7aa75f73416552c7831701b1ea14e9756bfe8c0d4397e50e8daf5e890364e",
        "page_sha256": [
            "292e1b329491355ca38d9535a025bfcd4b75c9b3b0fb50efac7279f4011af899",
            "11252ddf44870ee65931f217c3722cdd89aee47692a3ebeb23514da0597b86bb",
            "73a9caa0872b6e77cf815d7f5ccb5fb776c85b8106f7715df97d9edda3417c32",
            "7fd3075dc21ef9a9263e1d518ca887521787bb216a350be328a4534570b9660e",
            "44888c352e326774d3d89bdffd04437e29480a98b8559649810148dd29b368be",
        ],
        "page_characters": [8232, 7802, 8186, 11326, 12696],
        # Poppler's layout-preserving extraction visits the right-column
        # Discussion heading before the left-column Results heading.  Bind the
        # exact offsets rather than weakening the narrative-section check to
        # accept arbitrary orderings.
        "required_section_offsets": {
            "Methods": 1870,
            "Results": 19431,
            "Discussion": 16921,
        },
        "allowed_external_urls": [],
        "html_shell_reason": "insufficient_body_paragraphs",
    },
}

_HYPHEN_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


class StrictPmcPdfError(RuntimeError):
    """Raised when a sealed PMC PDF or proof-of-work claim is malformed."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_identity_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return one_line(normalized.translate(_HYPHEN_TRANSLATION)).casefold()


def normalize_doi(value: Any) -> str:
    normalized = one_line(value).casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
    return normalized


def expected_pdf_identity(pmcid: str, pmid: str) -> dict[str, Any]:
    identity = EXPECTED_PDF_IDENTITIES.get(str(pmcid))
    if not isinstance(identity, dict) or identity.get("pmid") != str(pmid):
        raise StrictPmcPdfError("PMC PDF fallback is outside the exact bounded scope")
    return identity


def expected_pdf_identity_for_url(url: str) -> tuple[str, dict[str, Any]]:
    """Resolve one exact pinned identity for a cookie-bearing PDF URL."""
    matches = [
        (pmcid, identity)
        for pmcid, identity in EXPECTED_PDF_IDENTITIES.items()
        if isinstance(identity, dict) and identity.get("url") == str(url)
    ]
    if len(matches) != 1:
        raise StrictPmcPdfError("PMC PDF URL is outside the exact bounded scope")
    return matches[0]


def _normalized_page_text(value: str) -> str:
    lines = [
        line.rstrip()
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n".join(lines).strip()


def _run_pdf_tool(command: list[str], *, timeout: float = 30.0) -> str:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=timeout,
        env=environment,
    )
    if completed.returncode != 0:
        detail = one_line(completed.stderr.decode("utf-8", errors="replace"))
        raise StrictPmcPdfError(
            f"PDF tool failed ({command[0]}, exit {completed.returncode}): {detail}"
        )
    return completed.stdout.decode("utf-8", errors="strict")


def _colon_fields(value: str, *, source: str) -> dict[str, str]:
    """Parse strict ``key: value`` tool output without dropping any row."""
    fields: dict[str, str] = {}
    seen_keys: set[str] = set()
    for line_number, line in enumerate(value.splitlines(), start=1):
        if not line.strip():
            continue
        if ":" not in line:
            raise StrictPmcPdfError(
                f"Malformed {source} row {line_number}: missing colon"
            )
        key, content = line.split(":", 1)
        normalized_key = key.strip()
        if not normalized_key:
            raise StrictPmcPdfError(
                f"Malformed {source} row {line_number}: empty key"
            )
        canonical_key = normalized_key.casefold()
        if canonical_key in seen_keys:
            raise StrictPmcPdfError(
                f"Duplicate {source} field: {normalized_key}"
            )
        seen_keys.add(canonical_key)
        fields[normalized_key] = content.strip()
    return fields


def _parse_pdfinfo_urls(value: str) -> list[str]:
    """Parse the pinned ``pdfinfo -url`` table and reject every unknown row."""
    lines = value.splitlines()
    if not lines or lines[0] != PDFINFO_URL_HEADER:
        raise StrictPmcPdfError("Unexpected pdfinfo -url header")
    urls: list[str] = []
    for line_number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        match = re.fullmatch(
            r"\s*([1-9][0-9]*)\s+(Annotation)\s+(\S(?:.*\S)?)\s*", line
        )
        if match is None:
            raise StrictPmcPdfError(
                f"Malformed pdfinfo -url row {line_number}"
            )
        urls.append(match.group(3))
    return urls


def _poppler_tool_version(executable: Path, tool_name: str) -> str:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    completed = subprocess.run(
        [str(executable), "-v"],
        check=False,
        capture_output=True,
        timeout=10.0,
        env=environment,
    )
    version_material = "\n".join(
        (
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )
    )
    version_matches = re.findall(
        rf"(?m)^{re.escape(tool_name)} version ([0-9]+(?:\.[0-9]+)*)\r?$",
        version_material,
    )
    # Poppler 26.07.0's pdfdetach prints a valid ``-v`` banner but exits 99;
    # the other two tools exit 0.  Bind both the exact banner and the exact
    # tool-specific exit behavior so an arbitrary failing command is rejected.
    allowed_return_codes = {99} if tool_name == "pdfdetach" else {0}
    if completed.returncode not in allowed_return_codes or len(version_matches) != 1:
        raise StrictPmcPdfError(
            f"Unable to identify the Poppler {tool_name} version"
        )
    return version_matches[0]


def _resolve_pinned_poppler_tools() -> dict[str, Path]:
    """Resolve one exact Poppler installation, never a mixed PATH toolchain."""
    tools: dict[str, Path] = {}
    for tool_name in REQUIRED_POPPLER_TOOLS:
        executable = shutil.which(tool_name)
        if not executable:
            raise StrictPmcPdfError(f"Required Poppler tool is missing: {tool_name}")
        tools[tool_name] = Path(executable).resolve()
    tool_directories = {path.parent for path in tools.values()}
    if len(tool_directories) != 1:
        raise StrictPmcPdfError(
            "Poppler tools do not resolve to one shared installation directory"
        )
    for tool_name, executable in tools.items():
        observed = _poppler_tool_version(executable, tool_name)
        if observed != PINNED_POPPLER_VERSION:
            raise StrictPmcPdfError(
                f"Poppler {tool_name} version differs from the exact pin"
            )
    return tools


def _inspect_pdf_with_poppler(pdf_bytes: bytes) -> dict[str, Any]:
    """Parse the pinned PDF with a pinned Poppler text/toolchain contract."""
    tools = _resolve_pinned_poppler_tools()
    with tempfile.TemporaryDirectory(prefix="metavir-pmc-pdf-") as directory:
        path = Path(directory) / "source.pdf"
        path.write_bytes(pdf_bytes)
        info = _colon_fields(
            _run_pdf_tool([str(tools["pdfinfo"]), str(path)]),
            source="pdfinfo",
        )
        custom = _colon_fields(
            _run_pdf_tool([str(tools["pdfinfo"]), "-custom", str(path)]),
            source="pdfinfo -custom",
        )
        javascript = _run_pdf_tool(
            [str(tools["pdfinfo"]), "-js", str(path)]
        )
        url_output = _run_pdf_tool(
            [str(tools["pdfinfo"]), "-url", str(path)]
        )
        attachment_output = _run_pdf_tool(
            [str(tools["pdfdetach"]), "-list", str(path)]
        )
        extracted = _run_pdf_tool(
            [
                str(tools["pdftotext"]),
                "-layout",
                "-enc",
                "UTF-8",
                str(path),
                "-",
            ]
        )
    pages = [
        _normalized_page_text(page)
        for page in extracted.replace("\r\n", "\n").replace("\r", "\n").split("\f")
        if page.strip()
    ]
    external_urls = _parse_pdfinfo_urls(url_output)
    attachment_match = re.fullmatch(
        r"\s*(\d+) embedded files?\s*", attachment_output
    )
    if attachment_match is None:
        raise StrictPmcPdfError("Unable to parse the PDF embedded-file inventory")
    text = "\n\n".join(pages).strip()
    return {
        "poppler_version": PINNED_POPPLER_VERSION,
        "info": info,
        "custom": custom,
        "javascript": javascript.strip(),
        "external_urls": external_urls,
        "embedded_file_count": int(attachment_match.group(1)),
        "pages": pages,
        "text": text,
        "text_sha256": sha256_bytes(text.encode("utf-8")),
        "page_sha256": [sha256_bytes(page.encode("utf-8")) for page in pages],
    }


def validate_pmc_pdf_receipt(
    pdf_bytes: bytes,
    *,
    expected_pmcid: str,
    expected_pmid: str,
) -> tuple[dict[str, Any], str]:
    """Validate one exact official PMC PDF and deterministically extract text."""
    identity = expected_pdf_identity(expected_pmcid, expected_pmid)
    result: dict[str, Any] = {
        "valid": False,
        "bytes": len(pdf_bytes),
        "sha256": sha256_bytes(pdf_bytes),
        "pdf_header": False,
        "closed_eof": False,
        "encrypted": None,
        "page_count": 0,
        "page_characters": [],
        "extracted_characters": 0,
        "metadata_title": "",
        "metadata_doi": "",
        "title_in_first_page": False,
        "required_sections": {},
        "required_section_offsets": {},
        "required_section_order_valid": False,
        "poppler_version": "",
        "text_sha256": "",
        "page_sha256": [],
        "javascript_characters": None,
        "embedded_file_count": None,
        "external_urls": [],
        "active_content_policy_valid": False,
        "reason": "",
    }
    if len(pdf_bytes) < MIN_PDF_BYTES:
        result["reason"] = "pdf_below_minimum_bytes"
        return result, ""
    result["pdf_header"] = pdf_bytes.startswith(b"%PDF-")
    result["closed_eof"] = pdf_bytes.rstrip().endswith(b"%%EOF")
    if not result["pdf_header"] or not result["closed_eof"]:
        result["reason"] = "pdf_container_not_closed"
        return result, ""
    if result["sha256"] != identity["sha256"]:
        result["reason"] = "pdf_sha256_not_exactly_pinned"
        return result, ""
    try:
        inspection = _inspect_pdf_with_poppler(pdf_bytes)
        info = inspection["info"]
        custom = inspection["custom"]
        page_text = inspection["pages"]
        extracted_text = inspection["text"]
        result["encrypted"] = one_line(info.get("Encrypted")).casefold() != "no"
        result["page_count"] = int(info.get("Pages") or 0)
        result["metadata_title"] = one_line(custom.get("Title") or "")
        result["metadata_doi"] = normalize_doi(
            custom.get("WPS-ARTICLEDOI") or ""
        )
        result["page_characters"] = [len(value) for value in page_text]
        result["extracted_characters"] = len(extracted_text)
        result["poppler_version"] = inspection["poppler_version"]
        result["text_sha256"] = inspection["text_sha256"]
        result["page_sha256"] = inspection["page_sha256"]
        result["javascript_characters"] = len(inspection["javascript"])
        result["embedded_file_count"] = inspection["embedded_file_count"]
        result["external_urls"] = inspection["external_urls"]
        result["title_in_first_page"] = bool(
            page_text
            and normalize_identity_text(identity["title"])
            in normalize_identity_text(page_text[0])
        )
        section_offsets = {
            section: extracted_text.find(section)
            for section in REQUIRED_NARRATIVE_SECTIONS
        }
        section_presence = {
            section: offset >= 0 for section, offset in section_offsets.items()
        }
        result["required_sections"] = section_presence
        result["required_section_offsets"] = section_offsets
        pinned_section_offsets = identity.get("required_section_offsets")
        result["required_section_order_valid"] = (
            section_offsets == pinned_section_offsets
            if isinstance(pinned_section_offsets, dict)
            else list(section_offsets.values())
            == sorted(offset for offset in section_offsets.values() if offset >= 0)
        )
        # This deliberately bounded policy inventories the Poppler-exposed
        # JavaScript, form, embedded-file, and external-URL surfaces.  It is not
        # represented as an exhaustive PDF object/action inventory; the exact
        # raw SHA-256 pin is the integrity boundary for every other object.
        result["active_content_policy_valid"] = bool(
            one_line(info.get("JavaScript")).casefold() == "no"
            and one_line(info.get("Form")).casefold() == "none"
            and result["javascript_characters"] == 0
            and result["embedded_file_count"] == 0
            and result["external_urls"] == identity["allowed_external_urls"]
        )
    except Exception as exc:
        result["reason"] = f"strict_pdf_parse_failed: {one_line(exc)}"
        return result, ""

    if result["encrypted"]:
        result["reason"] = "encrypted_pdf_rejected"
        return result, ""
    if result["page_count"] != identity["pages"]:
        result["reason"] = "pdf_page_count_differs"
        return result, ""
    if result["poppler_version"] != identity["poppler_version"]:
        result["reason"] = "pdf_poppler_version_differs"
        return result, ""
    if normalize_identity_text(result["metadata_title"]) != normalize_identity_text(
        identity["title"]
    ):
        result["reason"] = "pdf_metadata_title_mismatch"
        return result, ""
    if result["metadata_doi"] != normalize_doi(identity["doi"]):
        result["reason"] = "pdf_metadata_doi_mismatch"
        return result, ""
    if not result["title_in_first_page"]:
        result["reason"] = "pdf_first_page_title_mismatch"
        return result, ""
    if result["text_sha256"] != identity["text_sha256"]:
        result["reason"] = "pdf_extracted_text_digest_differs"
        return result, ""
    if result["page_sha256"] != identity["page_sha256"]:
        result["reason"] = "pdf_page_text_digests_differ"
        return result, ""
    if "page_characters" in identity and result["page_characters"] != identity[
        "page_characters"
    ]:
        result["reason"] = "pdf_page_text_character_counts_differ"
        return result, ""
    if not result["active_content_policy_valid"]:
        result["reason"] = "pdf_active_content_inventory_differs"
        return result, ""
    if any(value < MIN_PDF_PAGE_CHARACTERS for value in result["page_characters"]):
        result["reason"] = "pdf_page_text_below_minimum"
        return result, ""
    if result["extracted_characters"] < MIN_PDF_EXTRACTED_CHARACTERS:
        result["reason"] = "pdf_extracted_text_below_minimum"
        return result, ""
    if not all(result["required_sections"].values()):
        result["reason"] = "pdf_required_narrative_sections_missing"
        return result, ""
    if not result["required_section_order_valid"]:
        result["reason"] = (
            "pdf_required_narrative_section_offsets_differ"
            if isinstance(identity.get("required_section_offsets"), dict)
            else "pdf_required_narrative_sections_missing"
        )
        return result, ""
    result["valid"] = True
    result["reason"] = "validated_exact_complete_official_pmc_pdf"
    return result, extracted_text


_POW_ASSIGNMENTS = {
    "challenge": re.compile(r'const\s+POW_CHALLENGE\s*=\s*"([A-Za-z0-9:_-]{16,256})"'),
    "difficulty": re.compile(r'const\s+POW_DIFFICULTY\s*=\s*"([1-6])"'),
    "cookie_name": re.compile(r'const\s+POW_COOKIE_NAME\s*=\s*"([A-Za-z0-9_-]+)"'),
    "cookie_path": re.compile(r'const\s+POW_COOKIE_PATH\s*=\s*"(/)"'),
}


def validate_pow_challenge_receipt(html_bytes: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the exact PMC proof-of-work bootstrap without executing its JS."""
    result: dict[str, Any] = {
        "valid": False,
        "bytes": len(html_bytes),
        "sha256": sha256_bytes(html_bytes),
        "reason": "",
    }
    try:
        html = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        result["reason"] = f"pow_challenge_not_utf8: {one_line(exc)}"
        return result, {}
    if html.count("Preparing to download ...") != 2:
        result["reason"] = "pow_challenge_page_identity_mismatch"
        return result, {}
    values: dict[str, str] = {}
    for field, pattern in _POW_ASSIGNMENTS.items():
        matches = pattern.findall(html)
        if len(matches) != 1:
            result["reason"] = f"pow_{field}_count_not_one"
            return result, {}
        values[field] = matches[0]
    if values["cookie_name"] != POW_COOKIE_NAME or values["cookie_path"] != "/":
        result["reason"] = "pow_cookie_scope_mismatch"
        return result, {}
    if int(values["difficulty"]) != EXPECTED_POW_DIFFICULTY:
        result["reason"] = "pow_difficulty_differs_from_exact_contract"
        return result, {}
    challenge = {
        "challenge": values["challenge"],
        "difficulty": int(values["difficulty"]),
        "cookie_name": values["cookie_name"],
        "cookie_path": values["cookie_path"],
    }
    result.update(
        {
            "valid": True,
            "reason": "validated_official_pmc_pdf_pow_challenge",
            "challenge_sha256": sha256_bytes(values["challenge"].encode("utf-8")),
            "difficulty": challenge["difficulty"],
            "cookie_name": challenge["cookie_name"],
            "cookie_path": challenge["cookie_path"],
        }
    )
    return result, challenge


def solve_pow_challenge(challenge: dict[str, Any]) -> dict[str, Any]:
    value = str(challenge.get("challenge") or "")
    difficulty = challenge.get("difficulty")
    if not value or isinstance(difficulty, bool) or not isinstance(difficulty, int):
        raise StrictPmcPdfError("PMC PDF proof-of-work challenge is malformed")
    prefix = "0" * difficulty
    for nonce in range(MAX_POW_NONCE + 1):
        digest = sha256_bytes(f"{value}{nonce}".encode("utf-8"))
        if digest.startswith(prefix):
            return {
                "challenge": value,
                "challenge_sha256": sha256_bytes(value.encode("utf-8")),
                "difficulty": difficulty,
                "nonce": nonce,
                "solution_sha256": digest,
                "cookie_name": POW_COOKIE_NAME,
            }
    raise StrictPmcPdfError("PMC PDF proof-of-work exceeded its bounded nonce search")


def validate_pow_solution(solution: Any) -> None:
    if not isinstance(solution, dict):
        raise StrictPmcPdfError("PMC PDF proof-of-work solution is missing")
    challenge = str(solution.get("challenge") or "")
    difficulty = solution.get("difficulty")
    nonce = solution.get("nonce")
    if (
        not challenge
        or isinstance(difficulty, bool)
        or not isinstance(difficulty, int)
        or difficulty != EXPECTED_POW_DIFFICULTY
        or isinstance(nonce, bool)
        or not isinstance(nonce, int)
        or not 0 <= nonce <= MAX_POW_NONCE
        or solution.get("cookie_name") != POW_COOKIE_NAME
        or solution.get("challenge_sha256")
        != sha256_bytes(challenge.encode("utf-8"))
    ):
        raise StrictPmcPdfError("PMC PDF proof-of-work solution fields differ")
    digest = sha256_bytes(f"{challenge}{nonce}".encode("utf-8"))
    if solution.get("solution_sha256") != digest or not digest.startswith(
        "0" * difficulty
    ):
        raise StrictPmcPdfError("PMC PDF proof-of-work solution does not verify")


def validate_pow_solution_against_receipt(
    solution: Any,
    challenge_validation: Any,
    challenge: Any,
    raw_challenge_html: bytes,
) -> None:
    """Bind one solution to the exact validated challenge transport body."""
    if not isinstance(raw_challenge_html, bytes):
        raise StrictPmcPdfError(
            "PMC PDF proof-of-work receipt body must be raw bytes"
        )
    recomputed_validation, recomputed_challenge = validate_pow_challenge_receipt(
        raw_challenge_html
    )
    if (
        challenge_validation != recomputed_validation
        or challenge != recomputed_challenge
    ):
        raise StrictPmcPdfError(
            "PMC PDF proof-of-work receipt does not exactly revalidate"
        )
    validate_pow_solution(solution)
    if (
        recomputed_validation.get("valid") is not True
        or solution.get("challenge") != recomputed_challenge.get("challenge")
        or solution.get("challenge_sha256")
        != recomputed_validation.get("challenge_sha256")
        or solution.get("difficulty") != recomputed_challenge.get("difficulty")
        or solution.get("difficulty") != recomputed_validation.get("difficulty")
        or solution.get("cookie_name") != recomputed_challenge.get("cookie_name")
        or solution.get("cookie_name") != recomputed_validation.get("cookie_name")
        or recomputed_challenge.get("cookie_path")
        != recomputed_validation.get("cookie_path")
    ):
        raise StrictPmcPdfError(
            "PMC PDF proof-of-work solution is not bound to its receipt"
        )


def pow_cookie_value(solution: dict[str, Any]) -> str:
    validate_pow_solution(solution)
    return f"{solution['challenge']},{solution['nonce']}"
