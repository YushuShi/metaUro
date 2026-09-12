#!/usr/bin/env python3
"""Audit the exact incomplete PMC sources blocking the 2026-08-29 MetaVir run.

Only official NCBI/PMC complete-article routes are queried:

* Entrez EFetch for the PMC database;
* the PMC OAI-PMH endpoint with ``metadataPrefix=pmc``;
* the PMC Article Datasets public S3 bucket; and
* the canonical PMC article HTML endpoint.

One additional PDF fallback is permitted only for PMC11160038.  It is reached
only after the same request's canonical HTML has passed every identity and
structure check but failed the substantive non-abstract-narrative check.  The
linked URL, PDF bytes, deterministic Poppler extraction, and proof-of-work
exchange are all independently sealed and revalidated.

The audit is deliberately fail closed.  Network receipts are accepted only when
they contain one well-formed JATS article, one canonical PMC HTML article with a
substantive main body, or the one exact bounded PDF described above.  Every
accepted source must bind the expected PMCID and PMID.  Production full-text
files are staged only if every member of the exact blocker set passes.  A partial
success can therefore never unblock R3.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts import strict_pmc_pdf as strict_pdf

DEFAULT_OUTPUT_DIR = (
    REPO_ROOT / "data" / "monthly_updates" / "all_history_through_2026-08-29"
)
LEDGER_FILENAME = "pmc_complete_source_retrieval_ledger.json"
LEDGER_VALIDATION_FILENAME = "pmc_complete_source_retrieval_ledger_validation.json"
STAGING_DIRNAME = ".complete_pmc_repair_staging"

# This is a bounded repair for one already-sealed production generation.  The
# digests prevent the script from being reused against a silently changed run.
EXPECTED_CANDIDATE_SHA256 = (
    "a4fb5efd02f56634be58a676996bf5418445601ca1cf76b91caf4f50fd15a508"
)
EXPECTED_SECOND_SHA256 = (
    "44dba8ea4ef720a32ac352937dba0c963f927724d39f7388cbc0d767b638fa6c"
)
EXPECTED_SOURCE_STATUS_SHA256 = (
    "f84c42204d60bec59ffac322e271001aef6c4c66c4531496f07c72aa02d61b72"
)
EXPECTED_FULL_TEXT_PROGRESS_SHA256 = (
    "31537b9abb9e05b3c42a787be7db24a20301a4b814f396e18762838818466806"
)
EXPECTED_SOURCE_MANIFEST_DIGEST = (
    "b3246814c06c27daaf71b1fc68d9a41f1a0d0e5803ba324935522f8e2fa9d645"
)
EXPECTED_FULL_TEXT_INPUT_DIGEST = (
    "f52ef4d06e695b1e3467d251f6b208260598842fcac119a11c46b8f412884af4"
)
EXPECTED_BLOCKERS: dict[str, str] = {
    "PMC11160038": "14720329",
    "PMC12034833": "38973750",
    "PMC12532455": "34383904",
    "PMC2781742": "19808637",
    "PMC2950820": "20693267",
    "PMC3735464": "23843441",
    "PMC4691666": "26561618",
    "PMC5241906": "27297429",
    "PMC8505417": "34135472",
    "PMC9814153": "36316560",
}
EXPECTED_CONTEXT_COUNT = 12
EXPECTED_CONTEXT_IDS = (
    "14720329|Genistein|Prostate cancer",
    "19808637|Processed meat intake|Prostate cancer",
    "20693267|Omega-6 fatty acids|Prostate cancer",
    "23843441|Linoleic acid|Prostate cancer",
    "26561618|Vegan diet|Prostate cancer",
    "27297429|Monounsaturated fat (MUFA)|Prostate cancer",
    "27297429|Polyunsaturated fat (PUFA)|Prostate cancer",
    "34135472|Animal protein intake|Prostate cancer",
    "34383904|Plant protein intake|Prostate cancer",
    "36316560|Plant-based diet|Prostate cancer",
    "38973750|Processed meat intake|Prostate cancer",
    "38973750|Whole-grain intake|Prostate cancer",
)


def expected_repair_lineage() -> dict[str, Any]:
    """Return the immutable lineage of the one bounded production repair."""
    return {
        "artifact_sha256": {
            "candidates": EXPECTED_CANDIDATE_SHA256,
            "second": EXPECTED_SECOND_SHA256,
            "source_status": EXPECTED_SOURCE_STATUS_SHA256,
            "full_text_progress": EXPECTED_FULL_TEXT_PROGRESS_SHA256,
        },
        "source_manifest_digest": EXPECTED_SOURCE_MANIFEST_DIGEST,
        "input_digest": EXPECTED_FULL_TEXT_INPUT_DIGEST,
        "context_count": EXPECTED_CONTEXT_COUNT,
        "context_ids": list(EXPECTED_CONTEXT_IDS),
    }


def expected_repair_scope_digest() -> str:
    return stable_json_digest(
        {
            "required_sources": EXPECTED_BLOCKERS,
            **expected_repair_lineage(),
        }
    )

EFETCH_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
OAI_BASE = "https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/"
S3_BASE = "https://pmc-oa-opendata.s3.amazonaws.com"
PMC_ARTICLE_BASE = "https://pmc.ncbi.nlm.nih.gov/articles"
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
MAX_RESPONSE_BYTES = 100 * 1024 * 1024
MIN_EXTRACTED_CHARACTERS = 2_000
MIN_BODY_CHARACTERS = 1_000
MIN_BODY_PARAGRAPHS = 3
PMC_HTML_FORMAT = "pmc_html"
JATS_XML_FORMAT = "jats_xml"
PMC_PDF_FORMAT = strict_pdf.PMC_PDF_FORMAT
PMC_PDF_ROUTE = "pmc_exact_bounded_pdf_fallback"
PMC_PDF_BLOCKER = "PMC11160038"
LEDGER_SCHEMA_VERSION = 5
LEDGER_VALIDATION_SCHEMA_VERSION = 5
RETRIEVAL_VALIDATOR_VERSION = 10
STAGING_MANIFEST_SCHEMA_VERSION = 3


class AuditInvariantError(RuntimeError):
    """Raised when the sealed scope or a receipt violates an invariant."""


@dataclass(frozen=True)
class HttpReceipt:
    url: str
    status: int | None
    body: bytes
    attempts: tuple[dict[str, Any], ...]
    final_url: str = ""
    redirected: bool = False
    content_type: str = ""
    request_accept: str = ""
    request_cookie: str = ""


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def stable_json_digest(value: Any) -> str:
    material = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(material)


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def attribute_by_local_name(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if local_name(key) == name:
            return str(value).strip()
    return ""


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_pmcid(value: Any) -> str:
    match = re.fullmatch(r"(?:PMC)?(\d+)", one_line(value), re.IGNORECASE)
    return f"PMC{match.group(1)}" if match else ""


def normalize_pmid(value: Any) -> str:
    text = one_line(value)
    return text if re.fullmatch(r"\d+", text) else ""


def article_to_text(article: ET.Element) -> str:
    blocks: list[str] = []
    allowed = {
        "article-title",
        "title",
        "p",
        "th",
        "td",
        "caption",
        "preformat",
    }
    for element in article.iter():
        if local_name(element.tag) not in allowed:
            continue
        value = one_line(" ".join(element.itertext()))
        if value and (not blocks or value != blocks[-1]):
            blocks.append(value)
    return "\n".join(blocks)


def _protocol_error(root: ET.Element) -> str:
    errors = []
    for element in root.iter():
        if local_name(element.tag) != "error":
            continue
        code = attribute_by_local_name(element, "code")
        detail = one_line(" ".join(element.itertext()))
        errors.append(": ".join(part for part in (code, detail) if part))
    return "; ".join(errors)


def validate_jats_receipt(
    xml_bytes: bytes,
    *,
    expected_pmcid: str,
    expected_pmid: str,
) -> tuple[dict[str, Any], str]:
    """Validate exact identity and substantive JATS body, returning audit + text."""
    result: dict[str, Any] = {
        "valid": False,
        "bytes": len(xml_bytes),
        "sha256": sha256_bytes(xml_bytes),
        "xml_well_formed": False,
        "article_count": 0,
        "observed_pmcids": [],
        "observed_pmids": [],
        "body_count": 0,
        "body_characters": 0,
        "body_paragraphs": 0,
        "body_sections": 0,
        "extracted_characters": 0,
        "reason": "",
    }
    if not xml_bytes:
        result["reason"] = "empty_response"
        return result, ""
    if len(xml_bytes) > MAX_RESPONSE_BYTES:
        result["reason"] = "response_exceeds_size_limit"
        return result, ""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        result["reason"] = f"malformed_xml: {one_line(exc)}"
        return result, ""
    result["xml_well_formed"] = True
    protocol_error = _protocol_error(root)
    if protocol_error:
        result["protocol_error"] = protocol_error

    articles = [element for element in root.iter() if local_name(element.tag) == "article"]
    result["article_count"] = len(articles)
    if len(articles) != 1:
        result["reason"] = (
            "protocol_error_without_jats_article" if protocol_error else "article_count_not_one"
        )
        return result, ""
    article = articles[0]

    direct_children = list(article)
    fronts = [element for element in article.iter() if local_name(element.tag) == "front"]
    direct_fronts = [
        element for element in direct_children if local_name(element.tag) == "front"
    ]
    bodies = [element for element in article.iter() if local_name(element.tag) == "body"]
    direct_bodies = [
        element for element in direct_children if local_name(element.tag) == "body"
    ]
    result.update(
        {
            "front_count": len(fronts),
            "direct_front_count": len(direct_fronts),
            "body_count": len(bodies),
            "direct_body_count": len(direct_bodies),
        }
    )
    if len(fronts) != 1 or len(direct_fronts) != 1:
        result["reason"] = "front_not_one_direct_article_child"
        return result, ""
    if len(bodies) != 1:
        result["reason"] = "body_count_not_one"
        return result, ""
    if len(direct_bodies) != 1:
        result["reason"] = "body_not_one_direct_article_child"
        return result, ""
    front = direct_fronts[0]
    body = direct_bodies[0]
    article_metas = [
        element
        for element in article.iter()
        if local_name(element.tag) == "article-meta"
    ]
    direct_article_metas = [
        element for element in list(front) if local_name(element.tag) == "article-meta"
    ]
    result.update(
        {
            "article_meta_count": len(article_metas),
            "direct_article_meta_count": len(direct_article_metas),
        }
    )
    if len(article_metas) != 1 or len(direct_article_metas) != 1:
        result["reason"] = "article_meta_not_one_direct_front_child"
        return result, ""
    if direct_children.index(front) > direct_children.index(body):
        result["reason"] = "front_body_order_invalid"
        return result, ""
    article_meta = direct_article_metas[0]

    relevant_ids: list[tuple[ET.Element, str, str]] = []
    for element in article.iter():
        if local_name(element.tag) != "article-id":
            continue
        id_type = attribute_by_local_name(element, "pub-id-type").casefold()
        value = one_line(" ".join(element.itertext()))
        if id_type in {"pmc", "pmcid", "pmid"}:
            relevant_ids.append((element, id_type, value))
    meta_element_ids = {id(element) for element in article_meta.iter()}
    pmcid_ids = [
        (element, value)
        for element, id_type, value in relevant_ids
        if id_type in {"pmc", "pmcid"}
    ]
    pmid_ids = [
        (element, value)
        for element, id_type, value in relevant_ids
        if id_type == "pmid"
    ]
    result["pmcid_id_count"] = len(pmcid_ids)
    result["pmid_id_count"] = len(pmid_ids)
    if len(pmcid_ids) != 1 or id(pmcid_ids[0][0]) not in meta_element_ids:
        result["reason"] = "pmcid_identity_count_or_location_invalid"
        return result, ""
    if len(pmid_ids) != 1 or id(pmid_ids[0][0]) not in meta_element_ids:
        result["reason"] = "pmid_identity_count_or_location_invalid"
        return result, ""
    # PMC JATS exports use both the canonical ``pmcid`` spelling and the
    # shorter legacy ``pmc`` spelling for this exact identifier type.
    observed_pmcids = {normalize_pmcid(pmcid_ids[0][1])}
    observed_pmids = {normalize_pmid(pmid_ids[0][1])}
    observed_pmcids.discard("")
    observed_pmids.discard("")
    result["observed_pmcids"] = sorted(observed_pmcids)
    result["observed_pmids"] = sorted(observed_pmids)
    if observed_pmcids != {expected_pmcid}:
        result["reason"] = "pmcid_identity_mismatch"
        return result, ""
    if observed_pmids != {expected_pmid}:
        result["reason"] = "pmid_identity_mismatch"
        return result, ""

    body_text = one_line(" ".join(body.itertext()))
    paragraphs = [
        one_line(" ".join(element.itertext()))
        for element in body.iter()
        if local_name(element.tag) == "p"
    ]
    paragraphs = [value for value in paragraphs if value]
    preformats = [
        one_line(" ".join(element.itertext()))
        for element in body.iter()
        if local_name(element.tag) == "preformat"
    ]
    preformats = [value for value in preformats if value]
    preformat_characters = sum(len(value) for value in preformats)
    substantive_preformat = bool(
        preformats and preformat_characters >= MIN_BODY_CHARACTERS
    )
    sections = [element for element in body.iter() if local_name(element.tag) == "sec"]
    extracted_text = article_to_text(article).rstrip("\n")
    result.update(
        {
            "body_characters": len(body_text),
            "body_paragraphs": len(paragraphs),
            "body_sections": len(sections),
            "extracted_characters": len(extracted_text),
        }
    )
    # These fields postdate the immutable MetaVir repair ledger.  Keep the
    # legacy validation schema byte-for-byte compatible for ordinary JATS
    # while still sealing the count and size when an OCR-style ``preformat``
    # is substantive enough to replace the paragraph requirement.
    if substantive_preformat:
        result.update(
            {
                "body_preformats": len(preformats),
                "body_preformat_characters": preformat_characters,
            }
        )
    if len(body_text) < MIN_BODY_CHARACTERS:
        result["reason"] = "body_not_substantive"
        return result, ""
    if len(paragraphs) < MIN_BODY_PARAGRAPHS and not substantive_preformat:
        result["reason"] = "insufficient_body_paragraphs"
        return result, ""
    if len(extracted_text) < MIN_EXTRACTED_CHARACTERS:
        result["reason"] = "extracted_text_below_r3_minimum"
        return result, ""
    result["valid"] = True
    result["reason"] = "validated_complete_jats_article_body"
    return result, extracted_text


_HTML_VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_HTML_TEXT_BLOCKS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "th",
    "td",
    "li",
    "caption",
    "figcaption",
    "blockquote",
    "pre",
}
_HTML_ANCILLARY_CLASS_TOKENS = {
    "author-notes",
    "def-list",
    "fn-group",
    "footnotes",
    "glossary",
    "notes",
    "ref-list",
    "references",
}
_HTML_HIDDEN_CLASS_TOKENS = {
    "display-none",
    "hidden",
    "usa-sr-only",
    "visually-hidden",
}
_HTML_ANCILLARY_ROLES = {
    "doc-bibliography",
    "doc-endnotes",
    "menu",
    "menubar",
    "navigation",
}


class _PmcArticleHtmlParser(HTMLParser):
    """Extract only the canonical main article body from a PMC HTML page."""

    def __init__(self, expected_pmcid: str) -> None:
        super().__init__(convert_charrefs=True)
        self.expected_pmcid = expected_pmcid
        self.stack: list[dict[str, Any]] = []
        self.html_active = 0
        self.html_closed_count = 0
        self.head_active = 0
        self.head_count = 0
        self.head_closed_count = 0
        self.document_body_active = 0
        self.document_body_count = 0
        self.document_body_closed_count = 0
        self.document_structure_mismatches = 0
        self.pre_root_content = 0
        self.post_root_content = 0
        self.identity_markup_outside_head = 0
        self.target_container_active = 0
        self.target_main_active = 0
        self.target_article_active = 0
        self.target_body_active = 0
        self.target_container_count = 0
        self.main_count = 0
        self.article_count = 0
        self.body_count = 0
        self.body_paragraphs = 0
        self.body_sections = 0
        self.abstract_active = 0
        self.ancillary_active = 0
        self.non_abstract_narrative_paragraphs = 0
        self.non_abstract_narrative_sections = 0
        self.html_count = 0
        self.citation_pmids: list[str] = []
        self.citation_titles: list[str] = []
        self.citation_dois: list[str] = []
        self.citation_pdf_urls: list[str] = []
        self.canonical_urls: list[str] = []
        self.fulltext_urls: list[str] = []
        self.article_container_ids: list[str] = []
        self.body_text_parts: list[str] = []
        self.non_abstract_narrative_text_parts: list[str] = []
        self.text_blocks: list[str] = []
        self.active_blocks: list[dict[str, Any]] = []
        self.structure_mismatches = 0

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).casefold(): str(value or "") for key, value in attrs}

    @staticmethod
    def _style_hides_content(style: str) -> bool:
        declarations: dict[str, str] = {}
        for declaration in str(style or "").split(";"):
            if ":" not in declaration:
                continue
            key, value = declaration.split(":", 1)
            declarations[key.strip().casefold()] = (
                value.strip().casefold().removesuffix("!important").strip()
            )
        return declarations.get("display") == "none" or declarations.get(
            "visibility"
        ) in {"hidden", "collapse"}

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        is_void: bool,
    ) -> None:
        name = str(tag).casefold()
        attributes = self._attributes(attrs)
        class_tokens = set(attributes.get("class", "").casefold().split())
        target_html = name == "html"
        if not target_html and self.html_count == 0 and self.html_active == 0:
            self.pre_root_content += 1
        if self.html_closed_count:
            self.post_root_content += 1
        if target_html:
            self.html_count += 1
        parent_tag = self.stack[-1]["tag"] if self.stack else ""
        target_head = False
        if name == "head":
            self.head_count += 1
            target_head = (
                self.html_active > 0
                and parent_tag == "html"
                and self.head_count == 1
                and self.document_body_count == 0
            )
            if not target_head:
                self.document_structure_mismatches += 1
        target_document_body = False
        if name == "body":
            self.document_body_count += 1
            target_document_body = (
                self.html_active > 0
                and parent_tag == "html"
                and self.document_body_count == 1
                and self.head_count == 1
                and self.head_closed_count == 1
                and self.head_active == 0
            )
            if not target_document_body:
                self.document_structure_mismatches += 1
        if name == "meta":
            meta_name = attributes.get("name", "").casefold()
            content = one_line(attributes.get("content", ""))
            if meta_name in {
                "citation_doi",
                "citation_pdf_url",
                "citation_pmid",
                "citation_title",
                "citation_fulltext_html_url",
            } and self.head_active <= 0:
                self.identity_markup_outside_head += 1
            elif meta_name == "citation_pmid" and content:
                self.citation_pmids.append(content)
            elif meta_name == "citation_title" and content:
                self.citation_titles.append(content)
            elif meta_name == "citation_doi" and content:
                self.citation_dois.append(content)
            elif meta_name == "citation_pdf_url" and content:
                self.citation_pdf_urls.append(content)
            elif meta_name == "citation_fulltext_html_url" and content:
                self.fulltext_urls.append(content)
        elif name == "link":
            rel_tokens = set(attributes.get("rel", "").casefold().split())
            if "canonical" in rel_tokens and self.head_active <= 0:
                self.identity_markup_outside_head += 1
            elif "canonical" in rel_tokens and attributes.get("href"):
                self.canonical_urls.append(one_line(attributes["href"]))
        article_container = (
            name == "div"
            and "pmc-article-section" in class_tokens
            and attributes.get("data-article-db", "").casefold() == "pmc"
        )
        target_container = False
        if article_container:
            self.article_container_ids.append(one_line(attributes.get("data-article-id")))
            target_container = (
                normalize_pmcid(attributes.get("data-article-id"))
                == self.expected_pmcid
                and self.document_body_active > 0
            )
            if target_container:
                self.target_container_count += 1

        inside_target_container = self.target_container_active > 0 or target_container
        target_main = (
            name == "main"
            and attributes.get("id") == "main-content"
            and inside_target_container
        )
        if target_main:
            self.main_count += 1
        inside_main = self.target_main_active > 0 or target_main
        target_article = name == "article" and inside_main
        if target_article:
            self.article_count += 1
        inside_article = self.target_article_active > 0 or target_article
        target_body = (
            name == "section"
            and inside_article
            and "main-article-body" in class_tokens
        )
        if target_body:
            self.body_count += 1
        inside_body = self.target_body_active > 0 or target_body
        if inside_body and name == "p":
            self.body_paragraphs += 1
        if inside_body and name == "section" and not target_body:
            self.body_sections += 1

        element_id = attributes.get("id", "").casefold()
        role = attributes.get("role", "").casefold()
        aria_label = one_line(attributes.get("aria-label", "")).casefold()
        target_abstract = (
            "abstract" in class_tokens
            or element_id.startswith("abstract")
            or aria_label == "abstract"
        )
        target_ancillary = (
            name == "nav"
            or role in _HTML_ANCILLARY_ROLES
            or bool(class_tokens & _HTML_ANCILLARY_CLASS_TOKENS)
            or bool(class_tokens & _HTML_HIDDEN_CLASS_TOKENS)
            or element_id.startswith(
                ("fn-group", "footnote", "notes", "ref-list", "references")
            )
            or "hidden" in attributes
            or "inert" in attributes
            or attributes.get("aria-hidden", "").casefold() == "true"
            or self._style_hides_content(attributes.get("style", ""))
        )
        inside_abstract = self.abstract_active > 0 or target_abstract
        inside_ancillary = self.ancillary_active > 0 or target_ancillary
        inside_non_abstract_narrative = (
            inside_body and not inside_abstract and not inside_ancillary
        )
        if inside_non_abstract_narrative and name == "p":
            self.non_abstract_narrative_paragraphs += 1
        if (
            inside_non_abstract_narrative
            and name == "section"
            and not target_body
        ):
            self.non_abstract_narrative_sections += 1

        block: dict[str, Any] | None = None
        if inside_body and not inside_ancillary and name in _HTML_TEXT_BLOCKS:
            for parent in self.active_blocks:
                parent["has_child_block"] = True
            block = {"tag": name, "parts": [], "has_child_block": False}
            self.active_blocks.append(block)

        entry = {
            "tag": name,
            "target_html": target_html,
            "target_head": target_head,
            "target_document_body": target_document_body,
            "target_container": target_container,
            "target_main": target_main,
            "target_article": target_article,
            "target_body": target_body,
            "target_abstract": target_abstract,
            "target_ancillary": target_ancillary,
            "block": block,
        }
        if target_html:
            self.html_active += 1
        if target_head:
            self.head_active += 1
        if target_document_body:
            self.document_body_active += 1
        if target_container:
            self.target_container_active += 1
        if target_main:
            self.target_main_active += 1
        if target_article:
            self.target_article_active += 1
        if target_body:
            self.target_body_active += 1
        if target_abstract:
            self.abstract_active += 1
        if target_ancillary:
            self.ancillary_active += 1
        if not is_void:
            self.stack.append(entry)
        else:
            self._finish_entry(entry)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._start(tag, attrs, is_void=str(tag).casefold() in _HTML_VOID_ELEMENTS)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._start(tag, attrs, is_void=True)

    def _finish_entry(self, entry: dict[str, Any]) -> None:
        block = entry.get("block")
        if block is not None:
            if block in self.active_blocks:
                self.active_blocks.remove(block)
            value = one_line(" ".join(str(part) for part in block["parts"]))
            if value and not block["has_child_block"]:
                self.text_blocks.append(value)
        if entry.get("target_body"):
            self.target_body_active -= 1
        if entry.get("target_abstract"):
            self.abstract_active -= 1
        if entry.get("target_ancillary"):
            self.ancillary_active -= 1
        if entry.get("target_article"):
            self.target_article_active -= 1
        if entry.get("target_main"):
            self.target_main_active -= 1
        if entry.get("target_container"):
            self.target_container_active -= 1
        if entry.get("target_document_body"):
            self.document_body_active -= 1
            self.document_body_closed_count += 1
        if entry.get("target_head"):
            self.head_active -= 1
            self.head_closed_count += 1
        if entry.get("target_html"):
            self.html_active -= 1
            self.html_closed_count += 1

    def handle_endtag(self, tag: str) -> None:
        name = str(tag).casefold()
        if self.html_closed_count:
            self.post_root_content += 1
        match_index = next(
            (
                index
                for index in range(len(self.stack) - 1, -1, -1)
                if self.stack[index]["tag"] == name
            ),
            None,
        )
        if match_index is None:
            self.structure_mismatches += 1
            return
        if match_index != len(self.stack) - 1:
            self.structure_mismatches += len(self.stack) - 1 - match_index
        for entry in reversed(self.stack[match_index:]):
            self._finish_entry(entry)
        del self.stack[match_index:]

    def handle_data(self, data: str) -> None:
        if self.html_count == 0 and self.html_active == 0 and data.strip():
            self.pre_root_content += 1
        if self.html_closed_count and data.strip():
            self.post_root_content += 1
        if self.target_body_active <= 0:
            return
        if any(entry["tag"] in {"script", "style", "noscript"} for entry in self.stack):
            return
        if data:
            self.body_text_parts.append(data)
            if self.ancillary_active <= 0:
                for block in self.active_blocks:
                    block["parts"].append(data)
                if self.abstract_active <= 0:
                    self.non_abstract_narrative_text_parts.append(data)

    def close(self) -> None:
        super().close()
        # A canonical PMC response must close the target structures explicitly.
        if (
            self.html_active
            or self.head_active
            or self.document_body_active
            or self.target_container_active
            or self.target_main_active
            or self.target_article_active
            or self.target_body_active
            or self.abstract_active
            or self.ancillary_active
        ):
            self.structure_mismatches += 1


def _canonical_pmc_article_url(pmcid: str) -> str:
    return f"{PMC_ARTICLE_BASE}/{pmcid}/"


def validate_pmc_html_receipt(
    html_bytes: bytes,
    *,
    expected_pmcid: str,
    expected_pmid: str,
) -> tuple[dict[str, Any], str]:
    """Validate canonical PMC HTML identity and its substantive main article body."""
    result: dict[str, Any] = {
        "valid": False,
        "bytes": len(html_bytes),
        "sha256": sha256_bytes(html_bytes),
        "html_parsed": False,
        "html_count": 0,
        "canonical_urls": [],
        "fulltext_urls": [],
        "observed_pmcids": [],
        "observed_pmids": [],
        "article_container_ids": [],
        "main_count": 0,
        "article_count": 0,
        "body_count": 0,
        "body_characters": 0,
        "body_paragraphs": 0,
        "body_sections": 0,
        "non_abstract_narrative_characters": 0,
        "non_abstract_narrative_paragraphs": 0,
        "non_abstract_narrative_sections": 0,
        "extracted_characters": 0,
        "structure_mismatches": 0,
        "reason": "",
    }
    if not html_bytes:
        result["reason"] = "empty_response"
        return result, ""
    if len(html_bytes) > MAX_RESPONSE_BYTES:
        result["reason"] = "response_exceeds_size_limit"
        return result, ""
    try:
        decoded = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        result["reason"] = f"invalid_utf8_html: {one_line(exc)}"
        return result, ""
    parser = _PmcArticleHtmlParser(expected_pmcid)
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        result["reason"] = f"malformed_html: {one_line(exc)}"
        return result, ""
    result["html_parsed"] = True
    expected_url = _canonical_pmc_article_url(expected_pmcid)
    observed_pmcids = {
        normalize_pmcid(value)
        for value in parser.article_container_ids
        if normalize_pmcid(value)
    }
    observed_pmids = {
        normalize_pmid(value)
        for value in parser.citation_pmids
        if normalize_pmid(value)
    }
    body_text = one_line(" ".join(parser.body_text_parts))
    non_abstract_narrative_text = one_line(
        " ".join(parser.non_abstract_narrative_text_parts)
    )
    titles = [one_line(value) for value in parser.citation_titles if one_line(value)]
    text_blocks = [one_line(value) for value in parser.text_blocks if one_line(value)]
    extracted_lines = []
    if titles:
        extracted_lines.append(titles[0])
    extracted_lines.extend(text_blocks)
    extracted_text = "\n".join(extracted_lines).rstrip("\n")
    result.update(
        {
            "html_count": parser.html_count,
            "html_closed_count": parser.html_closed_count,
            "head_count": parser.head_count,
            "head_closed_count": parser.head_closed_count,
            "document_body_count": parser.document_body_count,
            "document_body_closed_count": parser.document_body_closed_count,
            "document_structure_mismatches": parser.document_structure_mismatches,
            "pre_root_content": parser.pre_root_content,
            "post_root_content": parser.post_root_content,
            "identity_markup_outside_head": parser.identity_markup_outside_head,
            "canonical_urls": sorted(set(parser.canonical_urls)),
            "fulltext_urls": sorted(set(parser.fulltext_urls)),
            "observed_pmcids": sorted(observed_pmcids),
            "observed_pmids": sorted(observed_pmids),
            "article_container_ids": sorted(set(parser.article_container_ids)),
            "article_container_count": len(parser.article_container_ids),
            "target_article_container_count": parser.target_container_count,
            "main_count": parser.main_count,
            "article_count": parser.article_count,
            "body_count": parser.body_count,
            "body_characters": len(body_text),
            "body_paragraphs": parser.body_paragraphs,
            "body_sections": parser.body_sections,
            "non_abstract_narrative_characters": len(
                non_abstract_narrative_text
            ),
            "non_abstract_narrative_paragraphs": (
                parser.non_abstract_narrative_paragraphs
            ),
            "non_abstract_narrative_sections": (
                parser.non_abstract_narrative_sections
            ),
            "extracted_characters": len(extracted_text),
            "structure_mismatches": parser.structure_mismatches,
        }
    )
    if parser.html_count != 1:
        result["reason"] = "html_count_not_one"
        return result, ""
    if (
        parser.pre_root_content
        or parser.head_count != 1
        or parser.identity_markup_outside_head
    ):
        result["reason"] = "html_document_identity_outside_single_head"
        return result, ""
    if parser.html_closed_count != 1 or parser.post_root_content:
        result["reason"] = "html_document_not_one_closed_root"
        return result, ""
    if (
        parser.head_closed_count != 1
        or parser.document_body_count != 1
        or parser.document_body_closed_count != 1
        or parser.document_structure_mismatches
    ):
        result["reason"] = "html_document_head_body_structure_invalid"
        return result, ""
    if parser.canonical_urls != [expected_url]:
        result["reason"] = "canonical_url_identity_mismatch"
        return result, ""
    if parser.fulltext_urls != [expected_url]:
        result["reason"] = "fulltext_url_identity_mismatch"
        return result, ""
    if observed_pmcids != {expected_pmcid}:
        result["reason"] = "pmcid_identity_mismatch"
        return result, ""
    if len(parser.article_container_ids) != 1 or parser.target_container_count != 1:
        result["reason"] = "article_container_count_not_one"
        return result, ""
    if observed_pmids != {expected_pmid} or len(parser.citation_pmids) != 1:
        result["reason"] = "pmid_identity_mismatch"
        return result, ""
    if parser.main_count != 1:
        result["reason"] = "main_count_not_one"
        return result, ""
    if parser.article_count != 1:
        result["reason"] = "article_count_not_one"
        return result, ""
    if parser.body_count != 1:
        result["reason"] = "body_count_not_one"
        return result, ""
    if parser.structure_mismatches:
        result["reason"] = "target_html_structure_unbalanced"
        return result, ""
    if len(parser.citation_titles) != 1 or len(titles) != 1:
        result["reason"] = "citation_title_missing"
        return result, ""
    if len(body_text) < MIN_BODY_CHARACTERS:
        result["reason"] = "body_not_substantive"
        return result, ""
    if parser.body_paragraphs < MIN_BODY_PARAGRAPHS:
        result["reason"] = "insufficient_body_paragraphs"
        return result, ""
    if parser.body_sections < 1:
        result["reason"] = "body_sections_missing"
        return result, ""
    if len(non_abstract_narrative_text) < MIN_BODY_CHARACTERS:
        result["reason"] = "non_abstract_narrative_not_substantive"
        return result, ""
    if parser.non_abstract_narrative_paragraphs < MIN_BODY_PARAGRAPHS:
        result["reason"] = "insufficient_non_abstract_narrative_paragraphs"
        return result, ""
    if parser.non_abstract_narrative_sections < 1:
        result["reason"] = "non_abstract_narrative_sections_missing"
        return result, ""
    if len(extracted_text) < MIN_EXTRACTED_CHARACTERS:
        result["reason"] = "extracted_text_below_r3_minimum"
        return result, ""
    result["valid"] = True
    result["reason"] = "validated_complete_canonical_pmc_html_article_body"
    return result, extracted_text


def validate_pdf_link_html_receipt(
    html_bytes: bytes,
    *,
    expected_pmcid: str,
    expected_pmid: str,
) -> dict[str, Any]:
    """Bind an exact PDF fallback to head-scoped PMC link metadata."""
    try:
        identity = strict_pdf.expected_pdf_identity(expected_pmcid, expected_pmid)
    except strict_pdf.StrictPmcPdfError as exc:
        raise AuditInvariantError(str(exc)) from exc
    semantic_validation, semantic_text = validate_pmc_html_receipt(
        html_bytes,
        expected_pmcid=expected_pmcid,
        expected_pmid=expected_pmid,
    )
    result: dict[str, Any] = {
        "valid": False,
        "bytes": len(html_bytes),
        "sha256": sha256_bytes(html_bytes),
        "semantic_html_validation": semantic_validation,
        "semantic_text_characters": len(semantic_text),
        "canonical_urls": [],
        "fulltext_urls": [],
        "citation_pmids": [],
        "citation_titles": [],
        "citation_dois": [],
        "citation_pdf_urls": [],
        "reason": "",
    }
    expected_shell_reason = str(identity.get("html_shell_reason") or "")
    if (
        semantic_validation.get("valid") is not False
        or semantic_validation.get("reason")
        != expected_shell_reason
        or semantic_text
    ):
        result["reason"] = "canonical_html_not_exact_pdf_fallback_shell"
        return result
    try:
        decoded = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        result["reason"] = f"invalid_utf8_pdf_link_html: {one_line(exc)}"
        return result
    parser = _PmcArticleHtmlParser(expected_pmcid)
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        result["reason"] = f"pdf_link_html_parse_failed: {one_line(exc)}"
        return result
    expected_canonical_url = _canonical_pmc_article_url(expected_pmcid)
    result.update(
        {
            "canonical_urls": list(parser.canonical_urls),
            "fulltext_urls": list(parser.fulltext_urls),
            "citation_pmids": list(parser.citation_pmids),
            "citation_titles": list(parser.citation_titles),
            "citation_dois": list(parser.citation_dois),
            "citation_pdf_urls": list(parser.citation_pdf_urls),
        }
    )
    if parser.canonical_urls != [expected_canonical_url]:
        result["reason"] = "pdf_link_canonical_url_not_unique"
        return result
    if parser.fulltext_urls != [expected_canonical_url]:
        result["reason"] = "pdf_link_fulltext_url_not_unique"
        return result
    if [normalize_pmid(value) for value in parser.citation_pmids] != [expected_pmid]:
        result["reason"] = "pdf_link_pmid_not_unique"
        return result
    if len(parser.citation_titles) != 1 or strict_pdf.normalize_identity_text(
        parser.citation_titles[0]
    ) != strict_pdf.normalize_identity_text(identity["title"]):
        result["reason"] = "pdf_link_title_not_exactly_bound"
        return result
    if len(parser.citation_dois) != 1 or strict_pdf.normalize_doi(
        parser.citation_dois[0]
    ) != strict_pdf.normalize_doi(identity["doi"]):
        result["reason"] = "pdf_link_doi_not_exactly_bound"
        return result
    if parser.citation_pdf_urls != [identity["url"]]:
        result["reason"] = "pdf_link_url_not_exactly_bound"
        return result
    result["valid"] = True
    result["reason"] = "validated_exact_head_bound_pmc_pdf_link"
    return result


def _encode_ledger_body(raw: bytes) -> dict[str, Any]:
    return {
        "encoding": "base64",
        "body_base64": base64.b64encode(raw).decode("ascii"),
        "bytes": len(raw),
        "sha256": sha256_bytes(raw),
    }


def _decode_ledger_body(value: Any, *, label: str) -> bytes:
    if not isinstance(value, dict) or value.get("encoding") != "base64":
        raise AuditInvariantError(f"{label} ledger body encoding differs")
    encoded = value.get("body_base64")
    if not isinstance(encoded, str):
        raise AuditInvariantError(f"{label} ledger body is missing")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise AuditInvariantError(f"{label} ledger body is not canonical base64") from exc
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise AuditInvariantError(f"{label} ledger body base64 differs")
    if value.get("bytes") != len(raw) or value.get("sha256") != sha256_bytes(raw):
        raise AuditInvariantError(f"{label} ledger body digest differs")
    return raw


def _receipt_from_transport_body(transport: dict[str, Any], raw: bytes) -> HttpReceipt:
    return HttpReceipt(
        url=str(transport.get("url") or ""),
        status=transport.get("http_status"),
        body=raw,
        attempts=tuple(transport.get("attempts") or ()),
        final_url=str(transport.get("final_url") or ""),
        redirected=transport.get("redirected") is True,
        content_type=str(transport.get("content_type") or ""),
        request_accept=str(transport.get("request_accept") or ""),
        request_cookie=str(transport.get("request_cookie") or ""),
    )


def _redacted_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        query.append(
            (
                key,
                "REDACTED"
                if key.casefold() in {"api_key", "email"}
                else value,
            )
        )
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _content_type_from_headers(headers: Any) -> str:
    if headers is None:
        return ""
    try:
        value = headers.get("Content-Type")
    except AttributeError:
        return ""
    return value if isinstance(value, str) else ""


def http_get(
    url: str,
    *,
    user_agent: str,
    accept: str | None = None,
    cookie_header: str = "",
    follow_redirects: bool = True,
    timeout: float = 60.0,
    max_attempts: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpReceipt:
    """Fetch one official URL with bounded transient-only retries."""
    attempts: list[dict[str, Any]] = []
    redacted_url = _redacted_url(url)
    if cookie_header:
        try:
            _identity_pmcid, identity = strict_pdf.expected_pdf_identity_for_url(url)
        except strict_pdf.StrictPmcPdfError as exc:
            raise AuditInvariantError(
                "PMC PDF cookie escaped its exact URL scope"
            ) from exc
        if (
            url != identity["url"]
            or follow_redirects
            or accept != "application/pdf"
            or not cookie_header.startswith(f"{strict_pdf.POW_COOKIE_NAME}=")
        ):
            raise AuditInvariantError("PMC PDF cookie escaped its exact URL scope")
    for attempt_number in range(1, max_attempts + 1):
        parsed_url = urllib.parse.urlsplit(url)
        selected_accept = accept or (
            "text/html; charset=utf-8"
            if parsed_url.netloc == "pmc.ncbi.nlm.nih.gov"
            and parsed_url.path.startswith("/articles/")
            else "application/xml, application/json"
        )
        headers = {"Accept": selected_accept, "User-Agent": user_agent}
        if cookie_header:
            headers["Cookie"] = cookie_header
        request = urllib.request.Request(
            url,
            headers=headers,
        )
        try:
            opener = (
                urllib.request.urlopen
                if follow_redirects
                else urllib.request.build_opener(_NoRedirectHandler()).open
            )
            with opener(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(response.status)
                raw_final_url = str(response.geturl())
                redirected = raw_final_url != url
                final_url = _redacted_url(raw_final_url)
                content_type = _content_type_from_headers(
                    getattr(response, "headers", None)
                )
            attempts.append(
                {
                    "attempt": attempt_number,
                    "http_status": status,
                    "bytes": len(body),
                    "sha256": sha256_bytes(body),
                    "error": "",
                    "redirected": redirected,
                    "content_type": content_type,
                }
            )
            if len(body) > MAX_RESPONSE_BYTES:
                raise AuditInvariantError(f"Response exceeded {MAX_RESPONSE_BYTES} bytes")
            return HttpReceipt(
                redacted_url,
                status,
                body,
                tuple(attempts),
                final_url=final_url,
                redirected=any(item["redirected"] for item in attempts),
                content_type=content_type,
                request_accept=selected_accept,
                request_cookie=cookie_header,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read(MAX_RESPONSE_BYTES + 1)
            status = int(exc.code)
            raw_final_url = str(exc.geturl())
            redirected = raw_final_url != url or 300 <= status < 400
            final_url = _redacted_url(raw_final_url)
            content_type = _content_type_from_headers(getattr(exc, "headers", None))
            attempts.append(
                {
                    "attempt": attempt_number,
                    "http_status": status,
                    "bytes": len(body),
                    "sha256": sha256_bytes(body),
                    "error": f"HTTPError: {one_line(exc.reason)}",
                    "redirected": redirected,
                    "content_type": content_type,
                }
            )
            if status in RETRYABLE_HTTP_CODES and attempt_number < max_attempts:
                sleep(float(2 ** (attempt_number - 1)))
                continue
            return HttpReceipt(
                redacted_url,
                status,
                body,
                tuple(attempts),
                final_url=final_url,
                redirected=any(item["redirected"] for item in attempts),
                content_type=content_type,
                request_accept=selected_accept,
                request_cookie=cookie_header,
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            attempts.append(
                {
                    "attempt": attempt_number,
                    "http_status": None,
                    "bytes": 0,
                    "sha256": sha256_bytes(b""),
                    "error": f"{type(exc).__name__}: {one_line(exc)}",
                    "redirected": False,
                    "content_type": "",
                }
            )
            if attempt_number < max_attempts:
                sleep(float(2 ** (attempt_number - 1)))
                continue
            return HttpReceipt(
                redacted_url,
                None,
                b"",
                tuple(attempts),
                final_url=redacted_url,
                redirected=any(item["redirected"] for item in attempts),
                content_type="",
                request_accept=selected_accept,
                request_cookie=cookie_header,
            )
    raise AssertionError("bounded retry loop did not return")


def _receipt_transport(receipt: HttpReceipt) -> dict[str, Any]:
    return {
        "url": receipt.url,
        "final_url": receipt.final_url or receipt.url,
        "redirected": receipt.redirected,
        "http_status": receipt.status,
        "content_type": receipt.content_type,
        "request_accept": receipt.request_accept,
        "request_cookie": receipt.request_cookie,
        "attempts": list(receipt.attempts),
    }


def _require_http_success(
    validation: dict[str, Any],
    text: str,
    receipt: HttpReceipt,
) -> tuple[dict[str, Any], str]:
    if (
        receipt.status == 200
        and receipt.redirected is False
        and (receipt.final_url or receipt.url) == receipt.url
    ):
        return validation, text
    rejected = dict(validation)
    rejected["valid"] = False
    rejected["content_validation_reason"] = str(validation.get("reason") or "")
    rejected["http_status"] = receipt.status
    rejected["reason"] = (
        "redirected_transport_url"
        if receipt.redirected
        else "http_status_not_200"
    )
    return rejected, ""


def _validate_transport_receipt(
    transport: Any,
    *,
    expected_urls: set[str],
    validation: dict[str, Any] | None = None,
    require_success: bool = False,
) -> dict[str, Any]:
    """Validate one durable request/effective-URL/attempt/body receipt."""
    if not isinstance(transport, dict):
        raise AuditInvariantError("Official route is missing its transport receipt")
    requested_url = str(transport.get("url") or "")
    final_url = str(transport.get("final_url") or "")
    if requested_url not in expected_urls:
        raise AuditInvariantError("Official route requested an unexpected URL")
    redirected = transport.get("redirected")
    if not isinstance(redirected, bool):
        raise AuditInvariantError("Official route lacks its raw redirect decision")
    if not isinstance(transport.get("content_type"), str):
        raise AuditInvariantError("Official route lacks its response content type")
    if not isinstance(transport.get("request_accept"), str) or not isinstance(
        transport.get("request_cookie"), str
    ):
        raise AuditInvariantError("Official route request-header receipt is malformed")
    attempts = transport.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 4:
        raise AuditInvariantError("Official route has an invalid attempt ledger")
    for index, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, dict) or attempt.get("attempt") != index:
            raise AuditInvariantError("Official route attempt ordering differs")
        status = attempt.get("http_status")
        if status is not None and (
            isinstance(status, bool) or not isinstance(status, int) or status < 100
        ):
            raise AuditInvariantError("Official route attempt has an invalid HTTP status")
        byte_count = attempt.get("bytes")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or byte_count > MAX_RESPONSE_BYTES + 1
        ):
            raise AuditInvariantError("Official route attempt has an invalid byte count")
        digest = attempt.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AuditInvariantError("Official route attempt lacks a body SHA-256")
        if not isinstance(attempt.get("error"), str):
            raise AuditInvariantError("Official route attempt has an invalid error field")
        if not isinstance(attempt.get("redirected"), bool):
            raise AuditInvariantError("Official route attempt lacks a redirect decision")
        if not isinstance(attempt.get("content_type"), str):
            raise AuditInvariantError("Official route attempt lacks a content type")
        if status is not None and not attempt.get("content_type"):
            raise AuditInvariantError("Official HTTP response content type is empty")
        if status is None and attempt.get("content_type"):
            raise AuditInvariantError("Transport failure declared a response content type")
        if status is None and (
            byte_count != 0 or digest != sha256_bytes(b"") or not attempt.get("error")
        ):
            raise AuditInvariantError("Transport failure receipt is internally inconsistent")
        if index < len(attempts) and status not in RETRYABLE_HTTP_CODES and status is not None:
            raise AuditInvariantError("Official route retried a terminal HTTP response")
    final_attempt = attempts[-1]
    any_attempt_redirected = any(item["redirected"] for item in attempts)
    if redirected is not any_attempt_redirected:
        raise AuditInvariantError("Official route redirect decision differs from attempts")
    if not redirected and final_url != requested_url:
        raise AuditInvariantError("Official route effective URL differs without a redirect")
    if redirected:
        if (
            not isinstance(validation, dict)
            or validation.get("valid") is not False
            or validation.get("reason") != "redirected_transport_url"
        ):
            raise AuditInvariantError("Redirected official route was not explicitly rejected")
    if transport.get("http_status") != final_attempt.get("http_status"):
        raise AuditInvariantError("Official route final HTTP status differs from attempts")
    if transport.get("content_type") != final_attempt.get("content_type"):
        raise AuditInvariantError("Official route final content type differs from attempts")
    if require_success:
        if redirected or final_url != requested_url:
            raise AuditInvariantError("Accepted official route followed a redirect")
        if final_attempt.get("http_status") != 200:
            raise AuditInvariantError("Accepted official route did not end with HTTP 200")
    if validation is not None and "sha256" in validation:
        if validation.get("sha256") != final_attempt.get("sha256"):
            raise AuditInvariantError("Content validation is not bound to receipt SHA-256")
        if validation.get("bytes") != final_attempt.get("bytes"):
            raise AuditInvariantError("Content validation is not bound to receipt bytes")
    return final_attempt


def _efetch_expected_urls(pmcid: str) -> set[str]:
    return {
        _redacted_url(_efetch_url(pmcid, "")),
        _redacted_url(_efetch_url(pmcid, "audit@example.invalid")),
    }


def _validate_pdf_route_transport(
    route: dict[str, Any],
    *,
    pmcid: str,
    expected_pmid: str,
) -> None:
    if pmcid != PMC_PDF_BLOCKER or expected_pmid != EXPECTED_BLOCKERS[PMC_PDF_BLOCKER]:
        raise AuditInvariantError("Non-bounded source attempted the PMC PDF route")
    pdf_url = _pdf_fallback_url(pmcid, expected_pmid)
    link_html = route.get("link_html")
    if not isinstance(link_html, dict):
        raise AuditInvariantError("PMC PDF route lacks its exact link HTML")
    link_raw = _decode_ledger_body(link_html.get("body"), label="PMC PDF link HTML")
    link_validation = validate_pdf_link_html_receipt(
        link_raw,
        expected_pmcid=pmcid,
        expected_pmid=expected_pmid,
    )
    if link_validation != link_html.get("validation") or not link_validation["valid"]:
        raise AuditInvariantError("PMC PDF link HTML validation differs")
    if link_html.get("semantic_validation") != link_validation.get(
        "semantic_html_validation"
    ):
        raise AuditInvariantError("PMC PDF link HTML semantic validation differs")
    link_transport = link_html.get("transport")
    _validate_transport_receipt(
        link_transport,
        expected_urls={_pmc_html_url(pmcid)},
        validation=link_validation,
        require_success=True,
    )
    if (
        (link_transport or {}).get("request_cookie") != ""
        or (link_transport or {}).get("request_accept")
        != "text/html; charset=utf-8"
    ):
        raise AuditInvariantError("PMC PDF link HTML request headers differ")

    challenge_record = route.get("pow_challenge")
    if not isinstance(challenge_record, dict):
        raise AuditInvariantError("PMC PDF route lacks its proof-of-work challenge")
    challenge_raw = _decode_ledger_body(
        challenge_record.get("body"), label="PMC PDF proof-of-work challenge"
    )
    raw_challenge_validation, challenge = strict_pdf.validate_pow_challenge_receipt(
        challenge_raw
    )
    challenge_transport = challenge_record.get("transport")
    _validate_transport_receipt(
        challenge_transport,
        expected_urls={pdf_url},
        validation=challenge_record.get("validation") or {},
        require_success=(challenge_record.get("validation") or {}).get("valid")
        is True,
    )
    expected_challenge_validation, _ = _require_http_success(
        raw_challenge_validation,
        "",
        _receipt_from_transport_body(challenge_transport, challenge_raw),
    )
    if (
        challenge_record.get("validation") != expected_challenge_validation
        or challenge_record.get("challenge") != challenge
    ):
        raise AuditInvariantError("PMC PDF proof-of-work challenge receipt differs")
    if (
        (challenge_transport or {}).get("request_accept") != "application/pdf"
        or (challenge_transport or {}).get("request_cookie") != ""
    ):
        raise AuditInvariantError("PMC PDF challenge request headers differ")

    validation = route.get("validation") or {}
    if not expected_challenge_validation.get("valid"):
        if validation != {
            "valid": False,
            "reason": "pmc_pdf_pow_challenge_not_validated",
        } or any(
            field in route
            for field in (
                "pow_solution",
                "pow_solution_error",
                "pdf_transport",
                "pdf_body",
            )
        ):
            raise AuditInvariantError("Invalid PMC PDF challenge advanced to download")
        return

    if "pow_solution_error" in route:
        if validation != {
            "valid": False,
            "reason": "pmc_pdf_pow_solution_not_validated",
        } or any(
            field in route for field in ("pow_solution", "pdf_transport", "pdf_body")
        ):
            raise AuditInvariantError("Failed PMC PDF solution advanced to download")
        if not one_line(route.get("pow_solution_error")):
            raise AuditInvariantError("PMC PDF solution failure lacks an audit reason")
        try:
            strict_pdf.solve_pow_challenge(challenge)
        except strict_pdf.StrictPmcPdfError as exc:
            if one_line(exc) != one_line(route.get("pow_solution_error")):
                raise AuditInvariantError("PMC PDF solution failure reason differs") from exc
        else:
            raise AuditInvariantError(
                "PMC PDF ledger claimed failure for a deterministic valid solution"
            )
        return

    solution = route.get("pow_solution")
    try:
        strict_pdf.validate_pow_solution_against_receipt(
            solution,
            expected_challenge_validation,
            challenge,
            challenge_raw,
        )
        expected_cookie = (
            f"{strict_pdf.POW_COOKIE_NAME}={strict_pdf.pow_cookie_value(solution)}"
        )
    except strict_pdf.StrictPmcPdfError as exc:
        raise AuditInvariantError("PMC PDF proof-of-work solution differs") from exc
    pdf_transport = route.get("pdf_transport")
    _validate_transport_receipt(
        pdf_transport,
        expected_urls={pdf_url},
        validation=validation,
        require_success=validation.get("valid") is True,
    )
    if (
        (pdf_transport or {}).get("request_accept") != "application/pdf"
        or (pdf_transport or {}).get("request_cookie") != expected_cookie
    ):
        raise AuditInvariantError("PMC PDF download request headers differ")
    pdf_raw = _decode_ledger_body(route.get("pdf_body"), label="PMC PDF")
    try:
        strict_validation, strict_text = strict_pdf.validate_pmc_pdf_receipt(
            pdf_raw,
            expected_pmcid=pmcid,
            expected_pmid=expected_pmid,
        )
    except strict_pdf.StrictPmcPdfError as exc:
        raise AuditInvariantError("PMC PDF strict revalidation failed") from exc
    reconstructed_pdf_receipt = _receipt_from_transport_body(pdf_transport, pdf_raw)
    strict_validation, strict_text = _reject_wrong_pdf_content_type(
        strict_validation,
        strict_text,
        reconstructed_pdf_receipt,
    )
    strict_validation, _ = _require_http_success(
        strict_validation,
        strict_text,
        reconstructed_pdf_receipt,
    )
    if strict_validation != validation:
        raise AuditInvariantError("PMC PDF content validation differs")
    if validation.get("valid") is True and (
        (pdf_transport or {}).get("content_type")
        not in strict_pdf.ALLOWED_OFFICIAL_PDF_CONTENT_TYPES
        or (pdf_transport or {}).get("redirected") is not False
        or (pdf_transport or {}).get("final_url") != pdf_url
    ):
        raise AuditInvariantError("Accepted PMC PDF transport is not exact")


def _validate_route_transport(
    route: dict[str, Any],
    *,
    pmcid: str,
    expected_pmid: str,
) -> None:
    """Bind every claimed official route to its exact durable transport receipt."""
    route_name = str(route.get("route") or "")
    validation = route.get("validation") or {}
    accepted = validation.get("valid") is True
    if route_name == "ncbi_entrez_efetch_pmc_xml":
        _validate_transport_receipt(
            route.get("transport"),
            expected_urls=_efetch_expected_urls(pmcid),
            validation=validation,
            require_success=accepted,
        )
        return
    if route_name == "pmc_oai_pmh_metadata_prefix_pmc":
        _validate_transport_receipt(
            route.get("transport"),
            expected_urls={_oai_url(pmcid)},
            validation=validation,
            require_success=accepted,
        )
        return
    if route_name == "pmc_canonical_article_html":
        _validate_transport_receipt(
            route.get("transport"),
            expected_urls={_pmc_html_url(pmcid)},
            validation=validation,
            require_success=accepted,
        )
        return
    if route_name == PMC_PDF_ROUTE:
        _validate_pdf_route_transport(
            route,
            pmcid=pmcid,
            expected_pmid=expected_pmid,
        )
        return
    if route_name != "pmc_article_datasets_s3":
        raise AuditInvariantError(f"Unexpected official route name: {route_name!r}")

    listing_attempt = _validate_transport_receipt(
        route.get("listing_transport"),
        expected_urls={_s3_list_url(pmcid)},
        validation=route.get("listing_validation") or {},
    )
    listing_validation = route.get("listing_validation") or {}
    if not isinstance(listing_validation, dict):
        raise AuditInvariantError("Article Dataset listing validation is malformed")
    versions = listing_validation.get("versions") or []
    if not isinstance(versions, list) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in versions
    ):
        raise AuditInvariantError("Article Dataset version list is malformed")
    if listing_attempt.get("http_status") != 200 and versions:
        raise AuditInvariantError("Failed Article Dataset listing declared versions")
    version_attempts = route.get("version_attempts") or []
    if not isinstance(version_attempts, list):
        raise AuditInvariantError("Article Dataset version attempts are malformed")
    observed_versions: list[int] = []
    for attempt in version_attempts:
        if not isinstance(attempt, dict):
            raise AuditInvariantError("Article Dataset version attempt is malformed")
        version = attempt.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise AuditInvariantError("Article Dataset attempt version is invalid")
        observed_versions.append(version)
        metadata_final = _validate_transport_receipt(
            attempt.get("metadata_transport"),
            expected_urls={f"{S3_BASE}/metadata/{pmcid}.{version}.json"},
            validation=attempt.get("metadata_validation") or {},
            require_success=(attempt.get("metadata_validation") or {}).get("valid")
            is True,
        )
        metadata_validation = attempt.get("metadata_validation") or {}
        if metadata_validation.get("metadata_sha256") is not None and (
            metadata_validation.get("metadata_sha256")
            != metadata_final.get("sha256")
        ):
            raise AuditInvariantError("Article Dataset metadata SHA-256 differs")
        if metadata_validation.get("valid") is True:
            if metadata_final.get("http_status") != 200:
                raise AuditInvariantError("Valid Article Dataset metadata lacked HTTP 200")
            if (
                normalize_pmcid(metadata_validation.get("observed_pmcid")) != pmcid
                or normalize_pmid(metadata_validation.get("observed_pmid"))
                != expected_pmid
            ):
                raise AuditInvariantError("Article Dataset metadata identity differs")
        if "xml_transport" not in attempt:
            if attempt.get("validation", {}).get("valid") is True:
                raise AuditInvariantError("Validated Article Dataset XML lacks transport")
            continue
        md5_validation = attempt.get("md5_validation") or {}
        expected_md5 = str(md5_validation.get("expected") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", expected_md5):
            raise AuditInvariantError("Article Dataset XML receipt lacks expected MD5")
        xml_url = (
            f"{S3_BASE}/{pmcid}.{version}/{pmcid}.{version}.xml?md5={expected_md5}"
        )
        xml_validation = attempt.get("validation") or {}
        _validate_transport_receipt(
            attempt.get("xml_transport"),
            expected_urls={xml_url},
            validation=xml_validation,
            require_success=xml_validation.get("valid") is True,
        )
        observed_md5 = str(md5_validation.get("observed") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", observed_md5):
            raise AuditInvariantError("Article Dataset XML receipt lacks observed MD5")
        if md5_validation.get("valid") is not (observed_md5 == expected_md5):
            raise AuditInvariantError("Article Dataset MD5 decision is inconsistent")
    if observed_versions != versions[: len(observed_versions)]:
        raise AuditInvariantError("Article Dataset version probe order differs")
    accepted_version = route.get("accepted_version")
    if accepted:
        matching = [
            attempt
            for attempt in version_attempts
            if attempt.get("version") == accepted_version
            and (attempt.get("validation") or {}).get("valid") is True
        ]
        if len(matching) != 1 or matching[0].get("validation") != validation:
            raise AuditInvariantError("Accepted Article Dataset version differs")
    elif observed_versions != versions:
        raise AuditInvariantError("Unresolved Article Dataset route skipped a version")


def _efetch_url(pmcid: str, email: str) -> str:
    parameters = {
        "db": "pmc",
        "id": pmcid,
        "retmode": "xml",
        "tool": "metavir_complete_pmc_audit",
    }
    if email:
        parameters["email"] = email
    return f"{EFETCH_BASE}?{urllib.parse.urlencode(parameters)}"


def probe_efetch(
    pmcid: str,
    pmid: str,
    *,
    email: str,
    user_agent: str,
    getter: Callable[..., HttpReceipt] = http_get,
) -> tuple[dict[str, Any], str, bytes]:
    receipt = getter(_efetch_url(pmcid, email), user_agent=user_agent)
    validation, text = validate_jats_receipt(
        receipt.body, expected_pmcid=pmcid, expected_pmid=pmid
    )
    validation, text = _require_http_success(validation, text, receipt)
    return {
        "route": "ncbi_entrez_efetch_pmc_xml",
        "transport": _receipt_transport(receipt),
        "validation": validation,
    }, text, receipt.body


def _oai_url(pmcid: str) -> str:
    parameters = {
        "verb": "GetRecord",
        "identifier": f"oai:pubmedcentral.nih.gov:{pmcid.removeprefix('PMC')}",
        "metadataPrefix": "pmc",
    }
    return f"{OAI_BASE}?{urllib.parse.urlencode(parameters)}"


def probe_oai(
    pmcid: str,
    pmid: str,
    *,
    user_agent: str,
    getter: Callable[..., HttpReceipt] = http_get,
) -> tuple[dict[str, Any], str, bytes]:
    receipt = getter(_oai_url(pmcid), user_agent=user_agent)
    validation, text = validate_jats_receipt(
        receipt.body, expected_pmcid=pmcid, expected_pmid=pmid
    )
    validation, text = _require_http_success(validation, text, receipt)
    return {
        "route": "pmc_oai_pmh_metadata_prefix_pmc",
        "transport": _receipt_transport(receipt),
        "validation": validation,
    }, text, receipt.body


def _pmc_html_url(pmcid: str) -> str:
    return _canonical_pmc_article_url(pmcid)


def probe_pmc_html(
    pmcid: str,
    pmid: str,
    *,
    user_agent: str,
    getter: Callable[..., HttpReceipt] = http_get,
) -> tuple[dict[str, Any], str, bytes]:
    receipt = getter(_pmc_html_url(pmcid), user_agent=user_agent)
    validation, text = validate_pmc_html_receipt(
        receipt.body, expected_pmcid=pmcid, expected_pmid=pmid
    )
    validation, text = _require_http_success(validation, text, receipt)
    return {
        "route": "pmc_canonical_article_html",
        "transport": _receipt_transport(receipt),
        "validation": validation,
    }, text, receipt.body


def _pdf_fallback_url(pmcid: str, pmid: str) -> str:
    try:
        return str(strict_pdf.expected_pdf_identity(pmcid, pmid)["url"])
    except strict_pdf.StrictPmcPdfError as exc:
        raise AuditInvariantError(str(exc)) from exc


def _reject_wrong_pdf_content_type(
    validation: dict[str, Any],
    text: str,
    receipt: HttpReceipt,
) -> tuple[dict[str, Any], str]:
    if receipt.content_type in strict_pdf.ALLOWED_OFFICIAL_PDF_CONTENT_TYPES:
        return validation, text
    rejected = dict(validation)
    rejected["valid"] = False
    rejected["content_validation_reason"] = str(validation.get("reason") or "")
    rejected["observed_content_type"] = receipt.content_type
    rejected["reason"] = "content_type_not_in_exact_pdf_allowlist"
    return rejected, ""


def probe_pmc_pdf(
    pmcid: str,
    pmid: str,
    *,
    link_html_route: dict[str, Any],
    link_html_raw: bytes,
    user_agent: str,
    getter: Callable[..., HttpReceipt] = http_get,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], str, bytes]:
    """Run an exact HTML-linked, proof-of-work PMC PDF fallback."""
    try:
        strict_pdf.expected_pdf_identity(pmcid, pmid)
    except strict_pdf.StrictPmcPdfError as exc:
        raise AuditInvariantError(str(exc)) from exc
    if link_html_route.get("route") != "pmc_canonical_article_html":
        raise AuditInvariantError("PMC PDF fallback lacks its canonical HTML route")
    semantic_validation = link_html_route.get("validation") or {}
    link_validation = validate_pdf_link_html_receipt(
        link_html_raw,
        expected_pmcid=pmcid,
        expected_pmid=pmid,
    )
    if not link_validation["valid"]:
        raise AuditInvariantError(
            f"PMC PDF fallback link HTML failed: {link_validation['reason']}"
        )
    if link_validation.get("semantic_html_validation") != semantic_validation:
        raise AuditInvariantError("PMC PDF fallback semantic HTML validation drifted")
    _validate_transport_receipt(
        link_html_route.get("transport"),
        expected_urls={_pmc_html_url(pmcid)},
        validation=link_validation,
        require_success=True,
    )
    pdf_url = _pdf_fallback_url(pmcid, pmid)
    challenge_receipt = getter(
        pdf_url,
        user_agent=user_agent,
        accept="application/pdf",
        cookie_header="",
        follow_redirects=False,
    )
    challenge_validation, challenge = strict_pdf.validate_pow_challenge_receipt(
        challenge_receipt.body
    )
    challenge_validation, _ = _require_http_success(
        challenge_validation, "", challenge_receipt
    )
    route: dict[str, Any] = {
        "route": PMC_PDF_ROUTE,
        "link_html": {
            "transport": link_html_route["transport"],
            "body": _encode_ledger_body(link_html_raw),
            "semantic_validation": semantic_validation,
            "validation": link_validation,
        },
        "pow_challenge": {
            "transport": _receipt_transport(challenge_receipt),
            "body": _encode_ledger_body(challenge_receipt.body),
            "validation": challenge_validation,
            "challenge": challenge,
        },
    }
    if not challenge_validation.get("valid"):
        route["validation"] = {
            "valid": False,
            "reason": "pmc_pdf_pow_challenge_not_validated",
        }
        return route, "", b""
    try:
        solution = strict_pdf.solve_pow_challenge(challenge)
        strict_pdf.validate_pow_solution_against_receipt(
            solution,
            challenge_validation,
            challenge,
            challenge_receipt.body,
        )
    except strict_pdf.StrictPmcPdfError as exc:
        route["pow_solution_error"] = one_line(exc)
        route["validation"] = {
            "valid": False,
            "reason": "pmc_pdf_pow_solution_not_validated",
        }
        return route, "", b""
    cookie_header = (
        f"{strict_pdf.POW_COOKIE_NAME}={strict_pdf.pow_cookie_value(solution)}"
    )
    sleep(0.36)
    pdf_receipt = getter(
        pdf_url,
        user_agent=user_agent,
        accept="application/pdf",
        cookie_header=cookie_header,
        follow_redirects=False,
    )
    pdf_validation, text = strict_pdf.validate_pmc_pdf_receipt(
        pdf_receipt.body,
        expected_pmcid=pmcid,
        expected_pmid=pmid,
    )
    pdf_validation, text = _reject_wrong_pdf_content_type(
        pdf_validation, text, pdf_receipt
    )
    pdf_validation, text = _require_http_success(pdf_validation, text, pdf_receipt)
    route.update(
        {
            "pow_solution": solution,
            "pdf_transport": _receipt_transport(pdf_receipt),
            "pdf_body": _encode_ledger_body(pdf_receipt.body),
            "validation": pdf_validation,
        }
    )
    return route, text, pdf_receipt.body


def _s3_list_url(pmcid: str) -> str:
    parameters = {
        "list-type": "2",
        "prefix": f"{pmcid}.",
        "delimiter": "/",
        "max-keys": "100",
    }
    return f"{S3_BASE}/?{urllib.parse.urlencode(parameters)}"


def _s3_versions(listing: bytes, pmcid: str) -> tuple[list[int], dict[str, Any]]:
    audit: dict[str, Any] = {
        "xml_well_formed": False,
        "is_truncated": None,
        "prefixes": [],
        "versions": [],
        "reason": "",
    }
    try:
        root = ET.fromstring(listing)
    except ET.ParseError as exc:
        audit["reason"] = f"malformed_s3_listing_xml: {one_line(exc)}"
        return [], audit
    audit["xml_well_formed"] = True
    truncation = next(
        (one_line(element.text) for element in root.iter() if local_name(element.tag) == "IsTruncated"),
        "",
    ).casefold()
    audit["is_truncated"] = truncation == "true"
    prefixes = sorted(
        {
            one_line(element.text)
            for element in root.iter()
            if local_name(element.tag) == "Prefix" and one_line(element.text).endswith("/")
        }
    )
    audit["prefixes"] = prefixes
    if audit["is_truncated"]:
        audit["reason"] = "unexpected_truncated_version_listing"
        return [], audit
    versions = []
    pattern = re.compile(rf"{re.escape(pmcid)}\.(\d+)/")
    for prefix in prefixes:
        match = pattern.fullmatch(prefix)
        if match:
            versions.append(int(match.group(1)))
    versions = sorted(set(versions), reverse=True)
    audit["versions"] = versions
    audit["reason"] = "versions_found" if versions else "no_article_dataset_versions"
    return versions, audit


def _s3_https_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "s3" or parsed.netloc != "pmc-oa-opendata":
        raise AuditInvariantError("Article Dataset URL is not in the official bucket")
    if not parsed.path.startswith("/") or ".." in parsed.path.split("/"):
        raise AuditInvariantError("Article Dataset URL has an unsafe object path")
    return urllib.parse.urlunsplit(
        ("https", "pmc-oa-opendata.s3.amazonaws.com", parsed.path, parsed.query, "")
    )


def _required_md5(url: str) -> str:
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("md5") or []
    if len(values) != 1 or not re.fullmatch(r"[0-9a-fA-F]{32}", values[0]):
        raise AuditInvariantError("Article Dataset XML URL lacks one valid md5 receipt")
    return values[0].lower()


def probe_s3(
    pmcid: str,
    pmid: str,
    *,
    user_agent: str,
    getter: Callable[..., HttpReceipt] = http_get,
) -> tuple[dict[str, Any], str, bytes]:
    listing_receipt = getter(_s3_list_url(pmcid), user_agent=user_agent)
    if listing_receipt.redirected:
        versions, listing_audit = [], {
            "xml_well_formed": False,
            "is_truncated": None,
            "prefixes": [],
            "versions": [],
            "http_status": listing_receipt.status,
            "valid": False,
            "reason": "redirected_transport_url",
        }
    elif listing_receipt.status == 200:
        versions, listing_audit = _s3_versions(listing_receipt.body, pmcid)
    else:
        versions, listing_audit = [], {
            "xml_well_formed": False,
            "is_truncated": None,
            "prefixes": [],
            "versions": [],
            "http_status": listing_receipt.status,
            "valid": False,
            "reason": "http_status_not_200",
        }
    route: dict[str, Any] = {
        "route": "pmc_article_datasets_s3",
        "listing_transport": _receipt_transport(listing_receipt),
        "listing_validation": listing_audit,
        "version_attempts": [],
        "validation": {
            "valid": False,
            "reason": listing_audit["reason"],
        },
    }
    for version in versions:
        prefix = f"{pmcid}.{version}"
        metadata_url = f"{S3_BASE}/metadata/{prefix}.json"
        metadata_receipt = getter(metadata_url, user_agent=user_agent)
        attempt: dict[str, Any] = {
            "version": version,
            "metadata_transport": _receipt_transport(metadata_receipt),
            "metadata_validation": {"valid": False, "reason": ""},
        }
        if metadata_receipt.redirected:
            attempt["metadata_validation"].update(
                {
                    "http_status": metadata_receipt.status,
                    "reason": "redirected_transport_url",
                }
            )
            route["version_attempts"].append(attempt)
            continue
        if metadata_receipt.status != 200:
            attempt["metadata_validation"].update(
                {
                    "http_status": metadata_receipt.status,
                    "reason": "http_status_not_200",
                }
            )
            route["version_attempts"].append(attempt)
            continue
        try:
            metadata = json.loads(metadata_receipt.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            attempt["metadata_validation"]["reason"] = (
                f"invalid_metadata_json: {one_line(exc)}"
            )
            route["version_attempts"].append(attempt)
            continue
        observed_pmcid = normalize_pmcid(metadata.get("pmcid"))
        observed_pmid = normalize_pmid(metadata.get("pmid"))
        attempt["metadata_validation"].update(
            {
                "observed_pmcid": observed_pmcid,
                "observed_pmid": observed_pmid,
                "metadata_sha256": sha256_bytes(metadata_receipt.body),
            }
        )
        if observed_pmcid != pmcid or observed_pmid != pmid:
            attempt["metadata_validation"]["reason"] = "metadata_identity_mismatch"
            route["version_attempts"].append(attempt)
            continue
        if metadata.get("version") != version:
            attempt["metadata_validation"]["reason"] = "metadata_version_mismatch"
            route["version_attempts"].append(attempt)
            continue
        try:
            xml_url = _s3_https_url(str(metadata.get("xml_url") or ""))
            expected_md5 = _required_md5(xml_url)
        except AuditInvariantError as exc:
            attempt["metadata_validation"]["reason"] = one_line(exc)
            route["version_attempts"].append(attempt)
            continue
        expected_object_path = f"/{prefix}/{prefix}.xml"
        if urllib.parse.urlsplit(xml_url).path != expected_object_path:
            attempt["metadata_validation"]["reason"] = "unexpected_xml_object_path"
            route["version_attempts"].append(attempt)
            continue
        attempt["metadata_validation"].update(
            {"valid": True, "reason": "metadata_identity_and_xml_pointer_valid"}
        )
        xml_receipt = getter(xml_url, user_agent=user_agent)
        observed_md5 = hashlib.md5(xml_receipt.body, usedforsecurity=False).hexdigest()
        attempt["xml_transport"] = _receipt_transport(xml_receipt)
        attempt["md5_validation"] = {
            "expected": expected_md5,
            "observed": observed_md5,
            "valid": observed_md5 == expected_md5,
        }
        if xml_receipt.redirected:
            attempt["validation"] = {
                "valid": False,
                "reason": "redirected_transport_url",
                "http_status": xml_receipt.status,
            }
            route["version_attempts"].append(attempt)
            continue
        if observed_md5 != expected_md5:
            attempt["validation"] = {"valid": False, "reason": "xml_md5_mismatch"}
            route["version_attempts"].append(attempt)
            continue
        if xml_receipt.status != 200:
            attempt["validation"] = {
                "valid": False,
                "reason": "http_status_not_200",
                "http_status": xml_receipt.status,
            }
            route["version_attempts"].append(attempt)
            continue
        validation, text = validate_jats_receipt(
            xml_receipt.body, expected_pmcid=pmcid, expected_pmid=pmid
        )
        attempt["validation"] = validation
        route["version_attempts"].append(attempt)
        if validation["valid"]:
            route["validation"] = validation
            route["accepted_version"] = version
            return route, text, xml_receipt.body
    if versions:
        route["validation"] = {
            "valid": False,
            "reason": "no_article_dataset_version_passed_strict_validation",
        }
    return route, "", b""


def _context_ids_from_blockers(progress: dict[str, Any]) -> set[str]:
    return {
        str(context_id)
        for failure in progress.get("source_failures") or []
        for context_id in failure.get("context_ids") or []
    }


def verify_production_scope(output_dir: Path) -> dict[str, Any]:
    required_files = {
        "candidates": output_dir / "candidates.json",
        "second": output_dir / "chatgpt_second_screen.json",
        "source_status": output_dir / "pmc_source_status.json",
        "full_text_progress": output_dir / "chatgpt_full_text_review_progress.json",
    }
    missing = [str(path) for path in required_files.values() if not path.is_file()]
    if missing:
        raise AuditInvariantError(f"Required production artifacts are missing: {missing}")
    hashes = {name: file_sha256(path) for name, path in required_files.items()}
    if hashes["candidates"] != EXPECTED_CANDIDATE_SHA256:
        raise AuditInvariantError("Candidate generation digest is not the sealed repair scope")
    if hashes["second"] != EXPECTED_SECOND_SHA256:
        raise AuditInvariantError("Second-screen digest is not the sealed repair scope")
    if hashes["source_status"] != EXPECTED_SOURCE_STATUS_SHA256:
        raise AuditInvariantError("PMC source-status digest is not the sealed repair scope")
    if hashes["full_text_progress"] != EXPECTED_FULL_TEXT_PROGRESS_SHA256:
        raise AuditInvariantError("Blocked R3 progress digest is not the sealed repair scope")

    progress = load_json(required_files["full_text_progress"])
    source_status = load_json(required_files["source_status"])
    second = load_json(required_files["second"])
    if progress.get("status") != "blocked_incomplete_pmc_source":
        raise AuditInvariantError("Main R3 is not in the expected incomplete-PMC block")
    if (
        progress.get("schema_version") != 2
        or progress.get("track") != "chatgpt"
        or progress.get("model") != "gpt-5.6-luna"
        or progress.get("protocol_version") != 9
        or progress.get("results_by_context") != {}
    ):
        raise AuditInvariantError("Blocked R3 packet semantics differ from repair scope")
    if (
        second.get("model") != "gpt-5.6-sol"
        or progress.get("source_second_model") != second.get("model")
        or progress.get("source_second_input_digest") != second.get("input_digest")
    ):
        raise AuditInvariantError("Blocked R3 lost its exact second-screen lineage")
    source_manifest = progress.get("source_manifest")
    if not isinstance(source_manifest, dict):
        raise AuditInvariantError("Blocked R3 source manifest is malformed")
    computed_manifest_digest = stable_json_digest(source_manifest)
    if (
        computed_manifest_digest != EXPECTED_SOURCE_MANIFEST_DIGEST
        or progress.get("source_manifest_digest") != computed_manifest_digest
        or source_status.get("source_manifest") != source_manifest
        or source_status.get("source_manifest_digest") != computed_manifest_digest
    ):
        raise AuditInvariantError("Blocked R3 source-manifest lineage differs")
    # Recompute the full-text input seal from the current screening contract and
    # the actual fixed second/source packets.  The original blocked generation
    # had no manual full-text adjudications.
    from scripts import dual_screening  # local import avoids a module cycle

    computed_input_digest = dual_screening.full_text_input_digest(
        second,
        dual_screening.CHATGPT_FULL_TEXT_MODEL,
        source_manifest,
        {},
    )
    if (
        computed_input_digest != EXPECTED_FULL_TEXT_INPUT_DIGEST
        or progress.get("input_digest") != computed_input_digest
    ):
        raise AuditInvariantError("Blocked R3 input lineage differs")
    failures = progress.get("source_failures") or []
    observed_pmcids = {str(item.get("pmcid") or "") for item in failures}
    if observed_pmcids != set(EXPECTED_BLOCKERS):
        raise AuditInvariantError("Incomplete PMC blocker set differs from exact repair scope")
    if any(item.get("status") != "partial_record" for item in failures):
        raise AuditInvariantError("Expected every bounded blocker to be a partial_record")
    context_ids = _context_ids_from_blockers(progress)
    if len(context_ids) != EXPECTED_CONTEXT_COUNT:
        raise AuditInvariantError("Incomplete PMC context count differs from exact repair scope")
    if context_ids != set(EXPECTED_CONTEXT_IDS):
        raise AuditInvariantError("Incomplete PMC context identities differ from repair scope")
    for pmcid, expected_pmid in EXPECTED_BLOCKERS.items():
        contexts = {
            context_id
            for item in failures
            if item.get("pmcid") == pmcid
            for context_id in item.get("context_ids") or []
        }
        observed_pmids = {context_id.split("|", 1)[0] for context_id in contexts}
        if observed_pmids != {expected_pmid}:
            raise AuditInvariantError(f"{pmcid} context lineage has the wrong PMID")
    if source_status.get("source_manifest_digest") != progress.get(
        "source_manifest_digest"
    ):
        raise AuditInvariantError("PMC source status and blocked R3 manifest diverge")
    if source_status.get("source_manifest") != progress.get("source_manifest"):
        raise AuditInvariantError("PMC source manifest objects diverge")
    return {
        "artifact_sha256": hashes,
        "source_manifest_digest": computed_manifest_digest,
        "input_digest": computed_input_digest,
        "context_count": len(context_ids),
        "context_ids": list(EXPECTED_CONTEXT_IDS),
    }


def validate_complete_source_receipt(
    raw_bytes: bytes,
    *,
    source_format: str,
    expected_pmcid: str,
    expected_pmid: str,
) -> tuple[dict[str, Any], str]:
    if source_format == JATS_XML_FORMAT:
        return validate_jats_receipt(
            raw_bytes,
            expected_pmcid=expected_pmcid,
            expected_pmid=expected_pmid,
        )
    if source_format == PMC_HTML_FORMAT:
        return validate_pmc_html_receipt(
            raw_bytes,
            expected_pmcid=expected_pmcid,
            expected_pmid=expected_pmid,
        )
    if source_format == PMC_PDF_FORMAT:
        try:
            return strict_pdf.validate_pmc_pdf_receipt(
                raw_bytes,
                expected_pmcid=expected_pmcid,
                expected_pmid=expected_pmid,
            )
        except strict_pdf.StrictPmcPdfError as exc:
            raise AuditInvariantError(str(exc)) from exc
    raise AuditInvariantError(f"Unsupported complete PMC source format: {source_format!r}")


def _source_suffix(source_format: str) -> str:
    suffixes = {
        JATS_XML_FORMAT: ".xml",
        PMC_HTML_FORMAT: ".html",
        PMC_PDF_FORMAT: ".pdf",
    }
    try:
        return suffixes[source_format]
    except KeyError as exc:
        raise AuditInvariantError(
            f"Unsupported complete PMC source format: {source_format!r}"
        ) from exc


def stage_complete_set(
    output_dir: Path,
    validated_sources: dict[str, dict[str, Any]],
) -> Path:
    """Stage text/raw receipts only when the exact complete set is valid."""
    if set(validated_sources) != set(EXPECTED_BLOCKERS):
        raise AuditInvariantError("Refusing to stage an incomplete or expanded PMC set")
    for pmcid, record in validated_sources.items():
        if normalize_pmcid(record.get("pmcid")) != pmcid:
            raise AuditInvariantError(f"Staged source identity mismatch for {pmcid}")
        if normalize_pmid(record.get("pmid")) != EXPECTED_BLOCKERS[pmcid]:
            raise AuditInvariantError(f"Staged source PMID mismatch for {pmcid}")
        if not str(record.get("text") or "") or not bytes(record.get("raw") or b""):
            raise AuditInvariantError(f"Staged source bytes are missing for {pmcid}")
        source_format = str(record.get("source_format") or "")
        strict_validation, strict_text = validate_complete_source_receipt(
            bytes(record["raw"]),
            source_format=source_format,
            expected_pmcid=pmcid,
            expected_pmid=EXPECTED_BLOCKERS[pmcid],
        )
        if not strict_validation["valid"]:
            raise AuditInvariantError(f"Refusing to stage invalid source {pmcid}")
        if str(record["text"]).rstrip("\n") != strict_text:
            raise AuditInvariantError(f"Staged source text does not match receipt for {pmcid}")
        if record.get("validation") != strict_validation:
            raise AuditInvariantError(f"Staged source validation drift for {pmcid}")

    staging = output_dir / STAGING_DIRNAME
    temporary = output_dir / f"{STAGING_DIRNAME}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    manifest: dict[str, Any] = {}
    for pmcid in sorted(validated_sources):
        record = validated_sources[pmcid]
        text = str(record["text"]).rstrip("\n") + "\n"
        raw = bytes(record["raw"])
        source_format = str(record["source_format"])
        suffix = _source_suffix(source_format)
        text_path = temporary / f"{pmcid}.txt"
        raw_path = temporary / f"{pmcid}{suffix}"
        text_path.write_text(text, encoding="utf-8")
        raw_path.write_bytes(raw)
        manifest[pmcid] = {
            "pmid": EXPECTED_BLOCKERS[pmcid],
            "route": record.get("route"),
            "source_format": source_format,
            "text_sha256": file_sha256(text_path),
            "text_characters": len(text.rstrip("\n")),
            "raw_filename": raw_path.name,
            "raw_sha256": file_sha256(raw_path),
            "raw_bytes": len(raw),
            "validation": record["validation"],
        }
    save_json_atomic(
        temporary / "manifest.json",
        {
            "schema_version": STAGING_MANIFEST_SCHEMA_VERSION,
            "created_at": utc_now(),
            "scope_digest": stable_json_digest(EXPECTED_BLOCKERS),
            "sources": manifest,
        },
    )
    if staging.exists():
        raise AuditInvariantError(f"Repair staging already exists: {staging}")
    temporary.replace(staging)
    return staging


def validate_staged_complete_set(
    staging: Path,
    source_records: dict[str, dict[str, Any]],
) -> None:
    if not staging.is_dir() or staging.is_symlink():
        raise AuditInvariantError("Complete PMC repair staging directory is invalid")
    manifest_path = staging / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise AuditInvariantError("Complete PMC repair staging manifest is missing")
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != STAGING_MANIFEST_SCHEMA_VERSION:
        raise AuditInvariantError("Complete PMC repair manifest schema differs")
    if manifest.get("scope_digest") != stable_json_digest(EXPECTED_BLOCKERS):
        raise AuditInvariantError("Complete PMC repair manifest scope differs")
    manifest_sources = manifest.get("sources") or {}
    if set(manifest_sources) != set(EXPECTED_BLOCKERS):
        raise AuditInvariantError("Complete PMC repair manifest source set differs")
    expected_filenames = {"manifest.json"}
    for pmcid, expected_pmid in EXPECTED_BLOCKERS.items():
        entry = manifest_sources[pmcid]
        source_record = source_records[pmcid]
        source_format = str(entry.get("source_format") or "")
        suffix = _source_suffix(source_format)
        expected_raw_name = f"{pmcid}{suffix}"
        expected_text_name = f"{pmcid}.txt"
        if entry.get("pmid") != expected_pmid:
            raise AuditInvariantError(f"Staged PMID differs for {pmcid}")
        if entry.get("route") != source_record.get("accepted_route"):
            raise AuditInvariantError(f"Staged route differs for {pmcid}")
        if source_format != source_record.get("accepted_source_format"):
            raise AuditInvariantError(f"Staged source format differs for {pmcid}")
        if entry.get("raw_filename") != expected_raw_name:
            raise AuditInvariantError(f"Staged raw filename differs for {pmcid}")
        raw_path = staging / expected_raw_name
        text_path = staging / expected_text_name
        if (
            not raw_path.is_file()
            or raw_path.is_symlink()
            or not text_path.is_file()
            or text_path.is_symlink()
        ):
            raise AuditInvariantError(f"Staged source files are invalid for {pmcid}")
        raw = raw_path.read_bytes()
        text = text_path.read_text(encoding="utf-8").rstrip("\n")
        if entry.get("raw_sha256") != file_sha256(raw_path):
            raise AuditInvariantError(f"Staged raw digest differs for {pmcid}")
        if entry.get("raw_bytes") != len(raw):
            raise AuditInvariantError(f"Staged raw byte count differs for {pmcid}")
        if entry.get("text_sha256") != file_sha256(text_path):
            raise AuditInvariantError(f"Staged text digest differs for {pmcid}")
        if entry.get("text_characters") != len(text):
            raise AuditInvariantError(f"Staged text character count differs for {pmcid}")
        validation, strict_text = validate_complete_source_receipt(
            raw,
            source_format=source_format,
            expected_pmcid=pmcid,
            expected_pmid=expected_pmid,
        )
        if not validation["valid"] or validation != entry.get("validation"):
            raise AuditInvariantError(f"Staged validation differs for {pmcid}")
        route_short_names = {
            "ncbi_entrez_efetch_pmc_xml": "efetch",
            "pmc_oai_pmh_metadata_prefix_pmc": "oai",
            "pmc_article_datasets_s3": "s3",
            "pmc_canonical_article_html": "html",
            PMC_PDF_ROUTE: "pdf",
        }
        selected_route = next(
            (
                route
                for route in source_record.get("routes") or []
                if route_short_names.get(str(route.get("route") or ""))
                == entry.get("route")
            ),
            None,
        )
        if selected_route is None or selected_route.get("validation") != validation:
            raise AuditInvariantError(f"Staged route receipt differs for {pmcid}")
        if text != strict_text:
            raise AuditInvariantError(f"Staged text reconstruction differs for {pmcid}")
        expected_filenames.update({expected_raw_name, expected_text_name})
    actual_filenames = {path.name for path in staging.iterdir()}
    if actual_filenames != expected_filenames:
        raise AuditInvariantError("Complete PMC repair staging contains unexpected files")


def retrieval_validation_contract() -> dict[str, Any]:
    identity = strict_pdf.expected_pdf_identity(
        PMC_PDF_BLOCKER, EXPECTED_BLOCKERS[PMC_PDF_BLOCKER]
    )
    return {
        "validator_version": RETRIEVAL_VALIDATOR_VERSION,
        "supported_source_formats": [
            JATS_XML_FORMAT,
            PMC_HTML_FORMAT,
            PMC_PDF_FORMAT,
        ],
        "transport": {
            "exact_requested_url": True,
            "effective_url_must_equal_requested": True,
            "redirects_accepted": False,
            "raw_effective_url_compared_before_redaction": True,
            "rejected_redirect_receipts_recorded": True,
            "http_status_attempts_bound": True,
            "body_sha256_and_bytes_bound": True,
            "content_type_per_attempt_and_receipt_bound": True,
            "request_accept_and_cookie_bound": True,
        },
        "exact_pmcid": True,
        "exact_pmid": True,
        "exactly_one_main_article": True,
        "exactly_one_main_body": True,
        "jats_xml": {
            "exactly_one_jats_article": True,
            "accepted_pmcid_pub_id_types": ["pmcid", "pmc"],
            "exactly_one_body": True,
        },
        "pmc_html": {
            "semantic_validator_version": 7,
            "exact_canonical_url": True,
            "exact_fulltext_url": True,
            "exact_article_container": True,
            "exact_main_element": True,
            "exact_article_element": True,
            "exact_main_article_body": True,
            "non_abstract_narrative_required": True,
            "ancillary_subtrees_excluded": True,
            "hidden_subtrees_excluded": True,
            "narrative_list_caption_blocks_extracted": True,
            "minimum_non_abstract_narrative_characters": MIN_BODY_CHARACTERS,
            "minimum_non_abstract_narrative_paragraphs": MIN_BODY_PARAGRAPHS,
            "minimum_non_abstract_narrative_sections": 1,
        },
        "pmc_pdf": {
            "exact_bounded_pmcid": PMC_PDF_BLOCKER,
            "exact_bounded_pmid": EXPECTED_BLOCKERS[PMC_PDF_BLOCKER],
            "fallback_only": True,
            "required_canonical_html_failure_reason": (
                "non_abstract_narrative_not_substantive"
            ),
            "unique_head_bound_fields": [
                "canonical_url",
                "citation_fulltext_html_url",
                "citation_pmid",
                "citation_title",
                "citation_doi",
                "citation_pdf_url",
            ],
            "pdf_url": identity["url"],
            "title": identity["title"],
            "doi": identity["doi"],
            "raw_sha256": identity["sha256"],
            "pages": identity["pages"],
            "poppler_version": identity["poppler_version"],
            "poppler_tools": list(strict_pdf.REQUIRED_POPPLER_TOOLS),
            "poppler_tools_resolve_to_one_directory": True,
            "every_poppler_tool_version_exactly_pinned": True,
            "text_sha256": identity["text_sha256"],
            "page_sha256": list(identity["page_sha256"]),
            "exact_allowed_content_types": list(
                strict_pdf.ALLOWED_OFFICIAL_PDF_CONTENT_TYPES
            ),
            "accept_header": "application/pdf",
            "proof_of_work": {
                "cookie_name": strict_pdf.POW_COOKIE_NAME,
                "cookie_path": "/",
                "difficulty": strict_pdf.EXPECTED_POW_DIFFICULTY,
                "solution_bound_to_exact_challenge_receipt": True,
                "raw_challenge_body_revalidated_before_solution_use": True,
                "cookie_sent_only_to_exact_pdf_url": True,
            },
            "bounded_active_content_policy": {
                "active_content_policy_valid": True,
                "exhaustive_pdf_object_action_inventory": False,
                "exact_raw_sha256_is_primary_integrity_boundary": True,
                "checked_surfaces": [
                    "document_javascript",
                    "forms",
                    "embedded_files",
                    "external_url_annotations",
                ],
                "javascript_characters": 0,
                "embedded_file_count": 0,
                "external_urls": list(identity["allowed_external_urls"]),
            },
            "required_narrative_sections": list(
                strict_pdf.REQUIRED_NARRATIVE_SECTIONS
            ),
            "minimum_pdf_bytes": strict_pdf.MIN_PDF_BYTES,
            "minimum_pdf_extracted_characters": (
                strict_pdf.MIN_PDF_EXTRACTED_CHARACTERS
            ),
            "minimum_pdf_page_characters": strict_pdf.MIN_PDF_PAGE_CHARACTERS,
        },
        "minimum_body_characters": MIN_BODY_CHARACTERS,
        "minimum_body_paragraphs": MIN_BODY_PARAGRAPHS,
        "minimum_extracted_characters": MIN_EXTRACTED_CHARACTERS,
    }


def validate_audit_ledger(
    output_dir: Path,
    ledger: dict[str, Any],
    *,
    require_blocked_production: bool = True,
) -> dict[str, Any]:
    """Validate the ledger; optionally require the original blocked production state."""
    if require_blocked_production:
        lineage = verify_production_scope(output_dir)
    else:
        candidate_path = output_dir / "candidates.json"
        second_path = output_dir / "chatgpt_second_screen.json"
        if not candidate_path.is_file() or not second_path.is_file():
            raise AuditInvariantError("Repair consumption lost candidate/second artifacts")
        if file_sha256(candidate_path) != EXPECTED_CANDIDATE_SHA256:
            raise AuditInvariantError("Repair consumption candidate digest differs")
        if file_sha256(second_path) != EXPECTED_SECOND_SHA256:
            raise AuditInvariantError("Repair consumption second-screen digest differs")
        lineage = expected_repair_lineage()
    if ledger.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise AuditInvariantError(
            f"PMC retrieval ledger schema_version must be {LEDGER_SCHEMA_VERSION}"
        )
    if ledger.get("official_routes_only") is not True:
        raise AuditInvariantError("PMC retrieval ledger must be official-routes-only")
    if ledger.get("complete_official_pmc_source_required") is not True:
        raise AuditInvariantError("PMC retrieval ledger relaxed complete PMC source")
    if ledger.get("accepted_source_formats") != [
        JATS_XML_FORMAT,
        PMC_HTML_FORMAT,
        PMC_PDF_FORMAT,
    ]:
        raise AuditInvariantError("PMC retrieval ledger source formats differ")
    if ledger.get("abstracts_accepted_as_full_text") is not False:
        raise AuditInvariantError("PMC retrieval ledger accepted abstracts as full text")
    if ledger.get("all_or_none_gate") is not True:
        raise AuditInvariantError("PMC retrieval ledger relaxed the all-or-none gate")
    scope = ledger.get("scope") or {}
    if Path(str(scope.get("output_dir") or "")).resolve() != output_dir.resolve():
        raise AuditInvariantError("PMC retrieval ledger output scope differs")
    if scope.get("required_sources") != EXPECTED_BLOCKERS:
        raise AuditInvariantError("PMC retrieval ledger source scope differs")
    if scope.get("required_source_count") != len(EXPECTED_BLOCKERS):
        raise AuditInvariantError("PMC retrieval ledger source count differs")
    if scope.get("required_context_count") != EXPECTED_CONTEXT_COUNT:
        raise AuditInvariantError("PMC retrieval ledger context count differs")
    if scope.get("scope_digest") != expected_repair_scope_digest():
        raise AuditInvariantError("PMC retrieval ledger scope digest differs")
    for field in (
        "artifact_sha256",
        "source_manifest_digest",
        "input_digest",
        "context_count",
        "context_ids",
    ):
        if scope.get(field) != lineage.get(field):
            raise AuditInvariantError(f"PMC retrieval ledger lineage drift: {field}")

    contract = ledger.get("validation_contract") or {}
    if contract != retrieval_validation_contract():
        raise AuditInvariantError("PMC retrieval ledger validation contract differs")

    sources = ledger.get("sources") or {}
    if set(sources) != set(EXPECTED_BLOCKERS):
        raise AuditInvariantError("PMC retrieval ledger result scope differs")
    accepted_ids: set[str] = set()
    required_route_order = [
        "ncbi_entrez_efetch_pmc_xml",
        "pmc_oai_pmh_metadata_prefix_pmc",
        "pmc_article_datasets_s3",
        "pmc_canonical_article_html",
        PMC_PDF_ROUTE,
    ]
    accepted_route_names = {
        "ncbi_entrez_efetch_pmc_xml": "efetch",
        "pmc_oai_pmh_metadata_prefix_pmc": "oai",
        "pmc_article_datasets_s3": "s3",
        "pmc_canonical_article_html": "html",
        PMC_PDF_ROUTE: "pdf",
    }
    accepted_route_formats = {
        "ncbi_entrez_efetch_pmc_xml": JATS_XML_FORMAT,
        "pmc_oai_pmh_metadata_prefix_pmc": JATS_XML_FORMAT,
        "pmc_article_datasets_s3": JATS_XML_FORMAT,
        "pmc_canonical_article_html": PMC_HTML_FORMAT,
        PMC_PDF_ROUTE: PMC_PDF_FORMAT,
    }
    for pmcid, expected_pmid in EXPECTED_BLOCKERS.items():
        record = sources[pmcid]
        if normalize_pmid(record.get("pmid")) != expected_pmid:
            raise AuditInvariantError(f"PMC retrieval ledger PMID drift for {pmcid}")
        expected_contexts = sorted(
            value for value in lineage["context_ids"] if value.startswith(f"{expected_pmid}|")
        )
        if record.get("context_ids") != expected_contexts:
            raise AuditInvariantError(f"PMC retrieval ledger context drift for {pmcid}")
        routes = record.get("routes") or []
        route_names = [str(route.get("route") or "") for route in routes]
        if route_names != required_route_order:
            raise AuditInvariantError(f"PMC retrieval ledger route coverage differs for {pmcid}")
        prior_valid_route = False
        for route in routes:
            if route.get("skipped") is True:
                standard_prior_skip = (
                    prior_valid_route
                    and route.get("skip_reason") == "prior_official_route_validated"
                    and route.get("validation")
                    == {
                        "valid": False,
                        "reason": "skipped_after_prior_valid_receipt",
                    }
                )
                bounded_outside_skip = (
                    route.get("route") == PMC_PDF_ROUTE
                    and not prior_valid_route
                    and pmcid != PMC_PDF_BLOCKER
                    and route.get("skip_reason")
                    == "outside_exact_bounded_pdf_scope"
                    and route.get("validation")
                    == {
                        "valid": False,
                        "reason": "skipped_outside_exact_bounded_pdf_scope",
                    }
                )
                prerequisite_skip = (
                    route.get("route") == PMC_PDF_ROUTE
                    and not prior_valid_route
                    and pmcid == PMC_PDF_BLOCKER
                    and route.get("skip_reason")
                    == "exact_pdf_fallback_prerequisite_not_met"
                    and route.get("validation")
                    == {
                        "valid": False,
                        "reason": "skipped_pdf_fallback_prerequisite_not_met",
                    }
                    and route.get("observed_canonical_html_reason")
                    == ((routes[3].get("validation") or {}).get("reason") or "")
                    and route.get("observed_canonical_html_reason")
                    != "non_abstract_narrative_not_substantive"
                )
                if not (
                    standard_prior_skip or bounded_outside_skip or prerequisite_skip
                ):
                    raise AuditInvariantError(
                        f"PMC retrieval route skip is invalid for {pmcid}"
                    )
                if any(
                    field in route
                    for field in (
                        "transport",
                        "listing_transport",
                        "version_attempts",
                        "link_html",
                        "pow_challenge",
                        "pow_solution",
                        "pdf_transport",
                        "pdf_body",
                    )
                ):
                    raise AuditInvariantError(
                        f"Skipped PMC route retained transport claims for {pmcid}"
                    )
                continue
            if route.get("skipped") not in (None, False):
                raise AuditInvariantError(f"PMC retrieval skip flag differs for {pmcid}")
            if route.get("route") == PMC_PDF_ROUTE and prior_valid_route:
                raise AuditInvariantError(
                    f"PMC PDF fallback ran after a prior valid official route for {pmcid}"
                )
            _validate_route_transport(
                route,
                pmcid=pmcid,
                expected_pmid=expected_pmid,
            )
            if route.get("route") == PMC_PDF_ROUTE:
                link = route.get("link_html") or {}
                canonical_route = routes[3]
                if (
                    link.get("transport") != canonical_route.get("transport")
                    or link.get("semantic_validation")
                    != canonical_route.get("validation")
                ):
                    raise AuditInvariantError(
                        f"PMC PDF link receipt differs from canonical route for {pmcid}"
                    )
            if (route.get("validation") or {}).get("valid") is True:
                prior_valid_route = True
        valid_routes = [
            route
            for route in routes
            if route.get("skipped") is not True
            and (route.get("validation") or {}).get("valid") is True
        ]
        accepted = bool(record.get("accepted"))
        if accepted != bool(valid_routes):
            raise AuditInvariantError(f"PMC retrieval accepted flag differs for {pmcid}")
        if accepted:
            selected_route = next(
                (
                    route
                    for route in routes
                    if accepted_route_names.get(str(route.get("route") or ""))
                    == record.get("accepted_route")
                ),
                None,
            )
            if selected_route is None or (selected_route.get("validation") or {}).get(
                "valid"
            ) is not True:
                raise AuditInvariantError(f"PMC retrieval selected route is invalid for {pmcid}")
            first_valid_route = valid_routes[0]
            expected_route = accepted_route_names[first_valid_route["route"]]
            if record.get("accepted_route") != expected_route:
                raise AuditInvariantError(f"PMC retrieval accepted route differs for {pmcid}")
            expected_format = accepted_route_formats[first_valid_route["route"]]
            if record.get("accepted_source_format") != expected_format:
                raise AuditInvariantError(f"PMC retrieval source format differs for {pmcid}")
            if (
                record.get("terminal_reason")
                != "complete_strict_pmc_source_receipt_validated"
            ):
                raise AuditInvariantError(f"PMC retrieval terminal reason differs for {pmcid}")
            accepted_ids.add(pmcid)
        else:
            if record.get("accepted_route") is not None:
                raise AuditInvariantError(f"Unresolved PMC has an accepted route: {pmcid}")
            if record.get("accepted_source_format") is not None:
                raise AuditInvariantError(f"Unresolved PMC has a source format: {pmcid}")
            if (
                record.get("terminal_reason")
                != "no_official_complete_pmc_source_route_validated"
            ):
                raise AuditInvariantError(f"PMC retrieval terminal reason differs for {pmcid}")

    unresolved_ids = set(EXPECTED_BLOCKERS) - accepted_ids
    if ledger.get("validated_source_count") != len(accepted_ids):
        raise AuditInvariantError("PMC retrieval validated count differs")
    if ledger.get("validated_source_ids") != sorted(accepted_ids):
        raise AuditInvariantError("PMC retrieval validated source list differs")
    if ledger.get("unresolved_source_ids") != sorted(unresolved_ids):
        raise AuditInvariantError("PMC retrieval unresolved source list differs")
    complete = accepted_ids == set(EXPECTED_BLOCKERS)
    if ledger.get("all_sources_complete") is not complete:
        raise AuditInvariantError("PMC retrieval completion flag differs")
    if ledger.get("production_artifacts_unchanged") is not True:
        raise AuditInvariantError("PMC retrieval production-artifact invariant differs")

    staging = output_dir / STAGING_DIRNAME
    temporary_staging = output_dir / f"{STAGING_DIRNAME}.tmp"
    if complete:
        if ledger.get("decision") != "all_sources_validated_stage_ready":
            raise AuditInvariantError("Complete PMC retrieval decision differs")
        if ledger.get("staging_path") != str(staging) or not staging.is_dir():
            raise AuditInvariantError("Complete PMC retrieval staging is unavailable")
        validate_staged_complete_set(staging, sources)
    else:
        if ledger.get("decision") != "preserve_blocked_incomplete_pmc_source":
            raise AuditInvariantError("Incomplete PMC retrieval decision differs")
        if ledger.get("staging_path") is not None:
            raise AuditInvariantError("Incomplete PMC retrieval recorded a staging path")
        if staging.exists() or temporary_staging.exists():
            raise AuditInvariantError("Incomplete PMC retrieval left source staging behind")

    # These artifacts cannot exist while the exact main R3 gate is blocked.
    if require_blocked_production:
        forbidden = [
            output_dir / "chatgpt_full_text_review.json",
            output_dir / "full_text_review.json",
            output_dir / "chatgpt_final_screening.json",
            output_dir / "final_screening.json",
            output_dir / "applied_changes.json",
        ]
        unexpected = [str(path) for path in forbidden if path.exists()]
        if unexpected:
            raise AuditInvariantError(f"Blocked main run has downstream artifacts: {unexpected}")

    superseded = ledger.get("superseded_ledger")
    if superseded is not None:
        archive_path = Path(str(superseded.get("path") or "")).resolve()
        expected_parent = (output_dir / "superseded_generations").resolve()
        if archive_path.parent != expected_parent or not archive_path.is_file():
            raise AuditInvariantError("Superseded PMC ledger archive path is invalid")
        if file_sha256(archive_path) != superseded.get("sha256"):
            raise AuditInvariantError("Superseded PMC ledger archive digest differs")
    return {
        "schema_version": LEDGER_VALIDATION_SCHEMA_VERSION,
        "validated_at": utc_now(),
        "status": "validated",
        "required_source_count": len(EXPECTED_BLOCKERS),
        "required_context_count": EXPECTED_CONTEXT_COUNT,
        "validated_source_count": len(accepted_ids),
        "unresolved_source_count": len(unresolved_ids),
        "all_sources_complete": complete,
        "decision": ledger.get("decision"),
        "production_block_checked": require_blocked_production,
        "production_block_preserved": require_blocked_production and not complete,
        "production_artifact_sha256": lineage["artifact_sha256"],
    }


def _resume_or_archive_existing_staging(output_dir: Path) -> dict[str, Any] | None:
    """Reuse a fully sealed transaction or preserve an interrupted stage for audit."""
    staging = output_dir / STAGING_DIRNAME
    temporary = output_dir / f"{STAGING_DIRNAME}.tmp"
    if not staging.exists() and not temporary.exists():
        return None
    ledger_path = output_dir / LEDGER_FILENAME
    validation_path = output_dir / LEDGER_VALIDATION_FILENAME
    if staging.is_dir() and not staging.is_symlink() and ledger_path.is_file():
        try:
            ledger = load_json(ledger_path)
            validation = load_json(validation_path) if validation_path.is_file() else {}
            ledger_sha256 = file_sha256(ledger_path)
            if (
                validation.get("status") == "validated"
                and validation.get("all_sources_complete") is True
                and validation.get("ledger_sha256") == ledger_sha256
                and ledger.get("all_sources_complete") is True
            ):
                validate_audit_ledger(
                    output_dir,
                    ledger,
                    require_blocked_production=False,
                )
                return ledger
        except (AuditInvariantError, OSError, ValueError, json.JSONDecodeError):
            pass

    archive_dir = output_dir / "superseded_generations"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for source, label in ((staging, "staging"), (temporary, "temporary_staging")):
        if not source.exists():
            continue
        if not source.is_dir() or source.is_symlink():
            raise AuditInvariantError(f"Interrupted PMC {label} path is unsafe")
        manifest = source / "manifest.json"
        suffix = file_sha256(manifest)[:12] if manifest.is_file() else "no_manifest"
        destination = archive_dir / f"pmc_repair_{label}_unsealed_{suffix}"
        if destination.exists():
            destination = archive_dir / (
                f"pmc_repair_{label}_unsealed_{suffix}_{int(time.time())}"
            )
        source.replace(destination)
    return None


def run_audit(output_dir: Path, *, probe_all_routes: bool = True) -> dict[str, Any]:
    lineage = verify_production_scope(output_dir)
    resumed = _resume_or_archive_existing_staging(output_dir)
    if resumed is not None:
        return resumed
    ledger_path = output_dir / LEDGER_FILENAME
    superseded_ledger: dict[str, Any] | None = None
    if ledger_path.is_file():
        prior_sha256 = file_sha256(ledger_path)
        archive_dir = output_dir / "superseded_generations"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / (
            f"pmc_complete_source_retrieval_ledger_superseded_{prior_sha256[:12]}.json"
        )
        if not archive_path.exists():
            shutil.copy2(ledger_path, archive_path)
        if file_sha256(archive_path) != prior_sha256:
            raise AuditInvariantError("Superseded PMC audit ledger archive digest mismatch")
        superseded_ledger = {
            "path": str(archive_path),
            "sha256": prior_sha256,
            "reason": "rerun_with_current_strict_validator_and_live_official_receipts",
        }
    load_dotenv(REPO_ROOT / "mykey.env", override=True)
    email = str(os.getenv("NCBI_EMAIL") or os.getenv("PUBMED_EMAIL") or "").strip()
    user_agent = "MetaVir-complete-PMC-audit/1.0"
    if email:
        user_agent += f" (mailto:{email})"

    source_records: dict[str, Any] = {}
    accepted: dict[str, dict[str, Any]] = {}
    for index, (pmcid, pmid) in enumerate(sorted(EXPECTED_BLOCKERS.items()), start=1):
        route_records: list[dict[str, Any]] = []
        valid_candidates: list[tuple[str, str, dict[str, Any], str, bytes]] = []
        canonical_html_route: dict[str, Any] | None = None
        canonical_html_raw = b""
        probes: Iterable[
            tuple[str, str, Callable[[], tuple[dict[str, Any], str, bytes]]]
        ] = (
            (
                "efetch",
                JATS_XML_FORMAT,
                lambda pmcid=pmcid, pmid=pmid: probe_efetch(
                    pmcid, pmid, email=email, user_agent=user_agent
                ),
            ),
            (
                "oai",
                JATS_XML_FORMAT,
                lambda pmcid=pmcid, pmid=pmid: probe_oai(
                    pmcid, pmid, user_agent=user_agent
                ),
            ),
            (
                "s3",
                JATS_XML_FORMAT,
                lambda pmcid=pmcid, pmid=pmid: probe_s3(
                    pmcid, pmid, user_agent=user_agent
                ),
            ),
            (
                "html",
                PMC_HTML_FORMAT,
                lambda pmcid=pmcid, pmid=pmid: probe_pmc_html(
                    pmcid, pmid, user_agent=user_agent
                ),
            ),
        )
        for probe_index, (route_name, source_format, probe) in enumerate(probes):
            route, text, raw = probe()
            route_records.append(route)
            if route_name == "html":
                canonical_html_route = route
                canonical_html_raw = raw
            validation = route.get("validation") or {}
            if validation.get("valid"):
                valid_candidates.append(
                    (route_name, source_format, validation, text, raw)
                )
                if not probe_all_routes:
                    for skipped_name, _skipped_format, _skipped_probe in tuple(probes)[
                        probe_index + 1 :
                    ]:
                        long_names = {
                            "efetch": "ncbi_entrez_efetch_pmc_xml",
                            "oai": "pmc_oai_pmh_metadata_prefix_pmc",
                            "s3": "pmc_article_datasets_s3",
                            "html": "pmc_canonical_article_html",
                        }
                        route_records.append(
                            {
                                "route": long_names[skipped_name],
                                "skipped": True,
                                "skip_reason": "prior_official_route_validated",
                                "validation": {
                                    "valid": False,
                                    "reason": "skipped_after_prior_valid_receipt",
                                },
                            }
                        )
                    break
            # Stay beneath the unauthenticated NCBI rate and serialize all routes.
            time.sleep(0.36)
        if valid_candidates:
            route_records.append(
                {
                    "route": PMC_PDF_ROUTE,
                    "skipped": True,
                    "skip_reason": "prior_official_route_validated",
                    "validation": {
                        "valid": False,
                        "reason": "skipped_after_prior_valid_receipt",
                    },
                }
            )
        elif pmcid != PMC_PDF_BLOCKER:
            route_records.append(
                {
                    "route": PMC_PDF_ROUTE,
                    "skipped": True,
                    "skip_reason": "outside_exact_bounded_pdf_scope",
                    "validation": {
                        "valid": False,
                        "reason": "skipped_outside_exact_bounded_pdf_scope",
                    },
                }
            )
        else:
            html_reason = str(
                ((canonical_html_route or {}).get("validation") or {}).get("reason")
                or ""
            )
            if (
                canonical_html_route is not None
                and html_reason == "non_abstract_narrative_not_substantive"
            ):
                pdf_route, pdf_text, pdf_raw = probe_pmc_pdf(
                    pmcid,
                    pmid,
                    link_html_route=canonical_html_route,
                    link_html_raw=canonical_html_raw,
                    user_agent=user_agent,
                )
                route_records.append(pdf_route)
                pdf_validation = pdf_route.get("validation") or {}
                if pdf_validation.get("valid"):
                    valid_candidates.append(
                        (
                            "pdf",
                            PMC_PDF_FORMAT,
                            pdf_validation,
                            pdf_text,
                            pdf_raw,
                        )
                    )
            else:
                route_records.append(
                    {
                        "route": PMC_PDF_ROUTE,
                        "skipped": True,
                        "skip_reason": "exact_pdf_fallback_prerequisite_not_met",
                        "observed_canonical_html_reason": html_reason,
                        "validation": {
                            "valid": False,
                            "reason": "skipped_pdf_fallback_prerequisite_not_met",
                        },
                    }
                )
        if valid_candidates:
            route_name, source_format, validation, text, raw = valid_candidates[0]
            accepted[pmcid] = {
                "pmcid": pmcid,
                "pmid": pmid,
                "route": route_name,
                "source_format": source_format,
                "validation": validation,
                "text": text,
                "raw": raw,
            }
        source_records[pmcid] = {
            "pmid": pmid,
            "context_ids": [
                value for value in lineage["context_ids"] if value.startswith(f"{pmid}|")
            ],
            "routes": route_records,
            "accepted": pmcid in accepted,
            "accepted_route": accepted.get(pmcid, {}).get("route"),
            "accepted_source_format": accepted.get(pmcid, {}).get("source_format"),
            "terminal_reason": (
                "complete_strict_pmc_source_receipt_validated"
                if pmcid in accepted
                else "no_official_complete_pmc_source_route_validated"
            ),
        }
        print(
            f"[{index}/{len(EXPECTED_BLOCKERS)}] {pmcid}/{pmid}: "
            f"{source_records[pmcid]['terminal_reason']}",
            flush=True,
        )

    complete = set(accepted) == set(EXPECTED_BLOCKERS)
    staging_path = stage_complete_set(output_dir, accepted) if complete else None
    post_lineage = verify_production_scope(output_dir)
    if post_lineage != lineage:
        raise AuditInvariantError("Production artifacts changed during read-only retrieval audit")
    ledger = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "created_at": utc_now(),
        "official_routes_only": True,
        "complete_official_pmc_source_required": True,
        "accepted_source_formats": [
            JATS_XML_FORMAT,
            PMC_HTML_FORMAT,
            PMC_PDF_FORMAT,
        ],
        "abstracts_accepted_as_full_text": False,
        "all_or_none_gate": True,
        "scope": {
            "output_dir": str(output_dir),
            "required_sources": EXPECTED_BLOCKERS,
            "required_source_count": len(EXPECTED_BLOCKERS),
            "required_context_count": EXPECTED_CONTEXT_COUNT,
            "scope_digest": expected_repair_scope_digest(),
            **lineage,
        },
        "validation_contract": retrieval_validation_contract(),
        "sources": source_records,
        "validated_source_count": len(accepted),
        "validated_source_ids": sorted(accepted),
        "unresolved_source_ids": sorted(set(EXPECTED_BLOCKERS) - set(accepted)),
        "all_sources_complete": complete,
        "decision": (
            "all_sources_validated_stage_ready"
            if complete
            else "preserve_blocked_incomplete_pmc_source"
        ),
        "staging_path": str(staging_path) if staging_path else None,
        "production_artifacts_unchanged": True,
        "superseded_ledger": superseded_ledger,
    }
    save_json_atomic(ledger_path, ledger)
    validation = validate_audit_ledger(output_dir, ledger)
    validation["ledger_path"] = str(ledger_path)
    validation["ledger_sha256"] = file_sha256(ledger_path)
    save_json_atomic(output_dir / LEDGER_VALIDATION_FILENAME, validation)
    return ledger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--stop-after-first-valid-route",
        action="store_true",
        help="Avoid redundant official-route probes after one strict receipt validates.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ledger = run_audit(
        args.output_dir.resolve(),
        probe_all_routes=not args.stop_after_first_valid_route,
    )
    print(
        json.dumps(
            {
                "ledger": str(args.output_dir / LEDGER_FILENAME),
                "validated_source_count": ledger["validated_source_count"],
                "unresolved_source_ids": ledger["unresolved_source_ids"],
                "decision": ledger["decision"],
            },
            indent=2,
        )
    )
    return 0 if ledger["all_sources_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
