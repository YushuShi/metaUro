"""PMC-first, receipt-backed Europe PMC preprint source fallback.

No HTTP client or model is created here. Callers inject the existing strict
availability authorizer and an HTTP transport. A verified body is source evidence
for a later native scientific R3 gate, never a scientific inclusion decision.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlparse, urljoin
from xml.etree import ElementTree as ET

try:
    from .exact_match import (build_query, extract_author_surnames, extract_first_author,
                             match_candidate, normalize_surname, normalize_title)
except ImportError:  # Standalone CLI/test use; deployments copy both sibling files.
    from exact_match import (build_query, extract_author_surnames, extract_first_author,
                             match_candidate, normalize_surname, normalize_title)

SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
APOSTROPHES = "'’‘"
NAVY = "#000080"


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _doi(value):
    value = str(value or "").strip().lower()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value).rstrip(".,; ")


def _public_https(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_global
    except ValueError:
        return "." in parsed.hostname


def title_queries(title, surname):
    """Original full title first, then apostrophe-only equivalent typography."""
    titles = [title]
    if any(c in title for c in APOSTROPHES):
        straight = title.translate({ord(c): "'" for c in APOSTROPHES})
        titles.extend(straight.replace("'", c) for c in APOSTROPHES)
    unique = list(dict.fromkeys(titles))
    if any(normalize_title(t) != normalize_title(title) for t in unique):
        raise ValueError("Title words changed")
    return [build_query(t, surname) for t in unique]


class ResponseCache:
    """Append-only response snapshots and content-addressed bytes.

    http_get(url, accept=...) -> {status, body:bytes, headers, final_url}.
    No cookies/authorization/request headers are persisted. A refresh appends a
    snapshot; previously captured bytes and receipts are never overwritten.
    """
    def __init__(self, directory, http_get, *, refresh=False):
        self.root = Path(directory)
        self.http_get = http_get
        self.refresh = refresh
        self.network_calls = 0
        self.receipts = []

    def capture(self, url, *, accept, purpose):
        if not _public_https(url):
            raise ValueError("Non-public HTTPS source URL")
        request = {"url": url, "accept": accept, "purpose": purpose}
        key = _digest(_canonical(request))
        folder = self.root / "requests" / key
        if not self.refresh:
            for path in sorted(folder.glob("*.json"), reverse=True):
                receipt = json.loads(path.read_text())
                blob = self.root / receipt["body_path"]
                if receipt.get("request") == request and blob.is_file():
                    raw = blob.read_bytes()
                    if _digest(raw) == receipt["body_sha256"] and receipt.get("status") == 200:
                        self.receipts.append(str(path.relative_to(self.root)))
                        return raw, receipt
        self.network_calls += 1
        try:
            response = self.http_get(url, accept=accept)
            raw = response["body"]
            if not isinstance(raw, bytes):
                raise TypeError("HTTP response body must be bytes")
            status = response.get("status")
            final = response.get("final_url") or url
            if not _public_https(final):
                raise ValueError("Non-public HTTPS redirect")
            headers = {str(k).lower(): str(v) for k, v in response.get("headers", {}).items()
                       if str(k).lower() in {"content-type", "date", "etag", "last-modified"}}
            error_type = None
        except Exception as error:
            raw, status, final, headers, error_type = b"", None, url, {}, type(error).__name__
        body_hash = _digest(raw)
        blob = self.root / "blobs" / body_hash
        blob.parent.mkdir(parents=True, exist_ok=True)
        if blob.exists():
            if _digest(blob.read_bytes()) != body_hash:
                raise ValueError("Cached response checksum mismatch")
        else:
            with blob.open("xb") as handle:
                handle.write(raw)
        receipt = {"schema_version": 1, "request": request, "status": status,
            "final_url": final, "response_headers": headers, "body_sha256": body_hash,
            "body_path": str(blob.relative_to(self.root)), "bytes": len(raw),
            "captured_at": datetime.now(timezone.utc).isoformat(), "error_type": error_type,
            "model_calls": 0, "scientific_inclusion_authorized": False}
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{time.time_ns()}-{_digest(_canonical(receipt))}.json"
        with path.open("x") as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        self.receipts.append(str(path.relative_to(self.root)))
        return raw, receipt

    def search(self, query):
        results, cursor = [], "*"
        while True:
            url = SEARCH + "?" + urlencode({"query": query, "format": "json", "resultType": "core",
                "pageSize": 1000, "cursorMark": cursor, "synonym": "false"})
            raw, receipt = self.capture(url, accept="application/json", purpose="metadata")
            if receipt["status"] != 200:
                raise ValueError("Metadata HTTP request failed")
            data = json.loads(raw)
            if data.get("errCode") or "resultList" not in data:
                raise ValueError("Metadata response invalid")
            page = data["resultList"].get("result", []) or []
            results.extend(page)
            if len(results) >= int(data.get("hitCount", 0)):
                return results
            following = data.get("nextCursorMark")
            if not page or not following or following == cursor:
                raise ValueError("Metadata pagination incomplete")
            cursor = following


def _links(record, source):
    return {str(x["id"]) for x in (record.get("commentCorrectionList") or {}).get("commentCorrection", [])
            if x.get("source") == source and x.get("id") and
            str(x.get("type", "")).casefold() in {"preprint in", "preprint of"}}


def _same_surname(published, candidate):
    first = extract_first_author(published)
    return bool(first["normalized_surname"] and any(a["normalized_surname"] == first["normalized_surname"]
                for a in extract_author_surnames(candidate)))


def _contains_surname(author_text, surname):
    # Whole surname only, without initials or other coauthor constraints.
    haystack = " " + normalize_title(author_text) + " "
    needle = normalize_title(surname)
    return bool(needle and " " + needle + " " in haystack)


class _ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.parts, self.headings = {}, [], []
        self.depth = self.article_depth = self.skip_depth = 0
        self.heading_depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta":
            self.meta.setdefault(attributes.get("name", "").lower(), []).append(attributes.get("content", ""))
        if tag in {"meta", "br", "hr", "img", "link", "input"}:
            return
        self.depth += 1
        if tag == "article" and not self.article_depth:
            self.article_depth = self.depth
        if re.fullmatch(r"h[1-6]", tag) and self.article_depth:
            self.heading_depth = self.depth
        if tag in {"script", "style", "nav", "aside"} or any("abstract" in attributes.get(k, "").lower()
                for k in ("class", "id", "role", "itemprop")):
            if not self.skip_depth:
                self.skip_depth = self.depth

    def handle_endtag(self, tag):
        if tag in {"meta", "br", "hr", "img", "link", "input"}:
            return
        if self.depth == self.skip_depth:
            self.skip_depth = 0
        if self.depth == self.article_depth:
            self.article_depth = 0
        if self.depth == self.heading_depth:
            self.heading_depth = 0
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.article_depth and not self.skip_depth:
            self.parts.append(data)
            if self.heading_depth:
                self.headings.append(data)


def validate_body(raw, candidate, *, extract_pdf_text=None):
    """Conservative full-body and exact PPR identity validation.

    JATS IDs/title/structured authors are read only from front matter, never
    reference lists. HTML needs citation metadata and an actual article body.
    PDF requires an injected extractor; its identity is limited to first 2 pages.
    Missing identity or content stays held, regardless of HTTP success.
    """
    expected_doi = _doi(candidate.get("doi"))
    if not expected_doi:
        return {"verified": False, "reason": "candidate_DOI_missing"}
    title, doi, authors, text, fmt, headings, body_version = "", "", [], "", "unknown", [], None
    try:
        if raw.startswith(b"%PDF-"):
            fmt = "pdf"
            if extract_pdf_text is None:
                return {"verified": False, "reason": "PDF_text_extractor_required", "format": fmt}
            text = extract_pdf_text(raw)
            if not isinstance(text, str):
                raise ValueError("PDF extractor did not return text")
            header = "\f".join(text.split("\f")[:2])[:16000]
            title = candidate.get("title", "") if normalize_title(candidate.get("title")) in normalize_title(header) else ""
            dois = {_doi(x) for x in re.findall(r"10\.\d{4,9}/[^\s<>\"{}]+", header, re.I)}
            doi = expected_doi if expected_doi in dois else ""
            authors = [a["surname"] for a in extract_author_surnames(candidate)
                       if a["surname"] and _contains_surname(header, a["surname"])]
            headings = re.findall(r"(?im)^\s*(?:\d+[.\d]*\s+)?((?:materials\s+and\s+)?methods?|results?)\s*$", text)
            version_match = re.search(r"(?im)^\s*(?:preprint\s+)?version\s*[: ]\s*(\d+)\s*$", header)
            body_version = version_match[1] if version_match else None
        elif raw.lstrip().startswith(b"<"):
            try:
                article = ET.fromstring(raw)
            except ET.ParseError:
                article = None
            if article is not None and article.tag.split("}")[-1] == "article":
                fmt = "jats_xml"
                # Normalize namespaces for unambiguous front/body queries.
                for element in article.iter():
                    element.tag = element.tag.split("}")[-1]
                front = article.find("./front/article-meta")
                body = article.find("./body")
                if front is not None:
                    title_element = front.find("./title-group/article-title")
                    title = " ".join(title_element.itertext()) if title_element is not None else ""
                    doi = next((_doi("".join(e.itertext())) for e in front.findall("./article-id")
                                if e.get("pub-id-type", "").lower() == "doi"), "")
                    authors = ["".join(e.itertext()) for e in front.findall("./contrib-group/contrib/name/surname")]
                    version_element = front.find("./article-version")
                    if version_element is not None:
                        body_version = "".join(version_element.itertext()).strip()
                if body is not None:
                    for parent in list(body.iter()):
                        for child in list(parent):
                            if child.tag == "abstract" or any("abstract" in child.get(k, "").lower()
                                    for k in ("sec-type", "section-type", "role")):
                                parent.remove(child)
                    text = "\n".join(" ".join(e.itertext()) for e in body if e.tag != "abstract")
                    headings = [" ".join(e.itertext()) for e in body.findall(".//sec/title")]
            else:
                fmt = "html"
                parser = _ArticleHTML()
                parser.feed(raw.decode("utf-8", errors="replace"))
                title = (parser.meta.get("citation_title") or [""])[0]
                doi = _doi((parser.meta.get("citation_doi") or [""])[0])
                authors = parser.meta.get("citation_author", [])
                text = "\n".join(parser.parts)
                headings = parser.headings
                body_version = (parser.meta.get("citation_version") or [None])[0]
        else:
            return {"verified": False, "reason": "unsupported_or_malformed_body"}
    except (ValueError, ET.ParseError):
        return {"verified": False, "reason": "body_parse_failed", "format": fmt}
    candidate_first = extract_first_author(candidate)["surname"]
    expected_version = str(candidate.get("versionNumber") or "").strip()
    if expected_version and body_version is not None and str(body_version).strip() != expected_version:
        return {"verified": False, "reason": "preprint_version_mismatch", "format": fmt,
                "candidate_version": expected_version, "body_version": body_version}
    author_equal = any(_contains_surname(a, candidate_first) for a in authors) if candidate_first else False
    identity = bool(normalize_title(title) and normalize_title(title) == normalize_title(candidate.get("title"))
                    and doi == expected_doi and author_equal)
    full_body = bool(len(text.strip()) >= 500 and any(re.search(r"\bmethods?\b", h, re.I) for h in headings)
                     and any(re.search(r"\bresults?\b", h, re.I) for h in headings))
    return {"verified": identity and full_body,
        "reason": "verified_preprint_body" if identity and full_body else "body_identity_unverified" if not identity else "abstract_only_or_incomplete_body",
        "format": fmt, "doi_equal": doi == expected_doi, "title_equal": normalize_title(title) == normalize_title(candidate.get("title")),
        "author_surname_equal": author_equal, "full_body": full_body,
        "body_version": body_version, "version_verified": bool(expected_version and str(body_version) == expected_version),
        "text": text if identity and full_body else None, "text_sha256": _digest(text.encode()) if identity and full_body else None}


def recover_preprint(published, *, pmc_availability, authorize_retrieval, http_get,
                     cache_dir, extract_pdf_text=None, refresh=False):
    """Return source candidates/bodies; never authorize scientific inclusion/R3.

    `authorize_retrieval(pmid)` adapts preprint_retrieval_gate.retrieval_decision;
    it must validate fresh exact-PMID and saved-any-track bound proofs. Inputs and
    existing saved journal/extraction data are never changed.
    """
    pmid = str(published.get("pmid") or published.get("id") or "")
    result = {"schema_version": 1, "published_pmid": pmid, "status": "held", "reason": None,
        "candidates": [], "sources": [], "errors": [], "receipts": [], "network_calls": 0,
        "model_calls": 0, "model_dispatch_authorized": False, "scientific_inclusion_authorized": False}
    if pmc_availability == "available":
        result.update(status="not_needed", reason="published_PMC_full_text_available")
        return result
    if not pmid.isdigit() or pmc_availability not in {"pmcid_missing", "full_text_unavailable"}:
        result["reason"] = "exact_PMID_and_known_PMC_unavailability_required"
        return result
    try:
        authorization = authorize_retrieval(pmid)
        if (str(authorization.get("pmid")) != pmid or authorization.get("preprint_fetch_allowed") is not True
            or authorization.get("fresh_availability") != pmc_availability
            or not re.fullmatch(r"[0-9a-f]{64}", authorization.get("gate_sha256", ""))
            or len(authorization.get("bound_reports") or []) < 2):
            raise ValueError("Incomplete source availability authorization")
    except Exception as error:
        result.update(reason="source_availability_authorization_denied")
        result["errors"].append({"stage": "authorization", "error_type": type(error).__name__})
        return result
    result["source_availability_gate"] = authorization
    first = extract_first_author(published)
    if first["confidence"] != "structured" or not normalize_title(published.get("title")):
        result["reason"] = "structured_published_first_surname_and_title_required"
        return result
    cache = ResponseCache(cache_dir, http_get, refresh=refresh)
    candidates = {}

    def add(candidate, origin, med):
        if candidate.get("source") != "PPR" or not re.fullmatch(r"PPR\d+", str(candidate.get("id", ""))):
            return
        exact = match_candidate(published, candidate)
        reciprocal = candidate["id"] in _links(med, "PPR") and pmid in _links(candidate, "MED")
        accepted_identity = exact["matched"] or (reciprocal and _same_surname(published, candidate))
        key = (candidate["id"], str(candidate.get("versionNumber") or ""), _doi(candidate.get("doi")))
        if key not in candidates:
            candidates[key] = {"record": candidate, "discovery_methods": [], "metadata_identity_verified": accepted_identity,
                "candidate_only": True, "reciprocal_PMID_link": reciprocal, "exact_title_author_match": exact}
        if origin not in candidates[key]["discovery_methods"]:
            candidates[key]["discovery_methods"].append(origin)

    med = published
    try:
        current = cache.search(f"EXT_ID:{pmid} AND SRC:MED")
        med = next((r for r in current if r.get("source") == "MED" and str(r.get("id")) == pmid), None)
        if (med is None or normalize_title(med.get("title")) != normalize_title(published.get("title"))
            or normalize_surname(extract_first_author(med)["surname"]) != first["normalized_surname"]):
            raise ValueError("Current PMID identity metadata differs")
        if pmc_availability == "pmcid_missing" and med.get("pmcid"):
            result.update(reason="fresh_MED_now_has_PMCID_refresh_availability_gate", receipts=cache.receipts,
                          network_calls=cache.network_calls)
            return result
        for preprint_id in sorted(_links(med, "PPR")):
            if not re.fullmatch(r"PPR\d+", preprint_id):
                continue
            for candidate in cache.search(f"EXT_ID:{preprint_id} AND SRC:PPR"):
                if candidate.get("id") == preprint_id:
                    add(candidate, "PMID_link", med)
    except Exception as error:
        result["errors"].append({"stage": "PMID_linked_metadata", "error_type": type(error).__name__})
        result.update(status="lookup_error", reason="current_PMID_metadata_or_link_lookup_failed",
                      receipts=cache.receipts, network_calls=cache.network_calls)
        return result
    try:
        for query in title_queries(published["title"], first["surname"]):
            for candidate in cache.search(query):
                add(candidate, "exact_title_surname", med or published)
    except Exception as error:
        result["errors"].append({"stage": "exact_title_metadata", "error_type": type(error).__name__})
    for value in candidates.values():
        candidate = value["record"]
        result["candidates"].append(value)
        if not value["metadata_identity_verified"] or not _doi(candidate.get("doi")):
            continue
        urls = [("https://www.ebi.ac.uk/europepmc/webservices/rest/" + candidate["id"] + "/fullTextXML", "EuropePMC_fullTextXML")]
        urls.extend((link.get("url", ""), "advertised_" + link.get("documentStyle", "unknown"))
                    for link in (candidate.get("fullTextUrlList") or {}).get("fullTextUrl", [])
                    if link.get("documentStyle") in {"pdf", "xml", "html", "full_text", "doi"})
        visited = set()
        while urls:
            url, body_origin = urls.pop(0)
            if url in visited or not _public_https(url):
                continue
            visited.add(url)
            try:
                raw, receipt = cache.capture(url, accept="application/pdf,application/xml,text/html", purpose="preprint_body")
                if receipt["status"] != 200:
                    continue
                validation = validate_body(raw, candidate, extract_pdf_text=extract_pdf_text)
                value.setdefault("body_checks", []).append({"url": url, **{k: v for k, v in validation.items() if k != "text"}})
                if not validation["verified"]:
                    # Follow only an explicit advertised PDF from a DOI-verified
                    # landing page. Do not guess paths or crawl arbitrary links.
                    if raw.lstrip().startswith(b"<"):
                        landing = _ArticleHTML()
                        landing.feed(raw.decode("utf-8", errors="replace"))
                        if _doi((landing.meta.get("citation_doi") or [""])[0]) == _doi(candidate["doi"]):
                            urls.extend((urljoin(receipt["final_url"], link), "DOI_verified_citation_pdf_url")
                                        for link in landing.meta.get("citation_pdf_url", [])
                                        if _public_https(urljoin(receipt["final_url"], link)))
                    continue
                provenance = {"published_pmid": pmid, "journal_publication_date": published.get("firstPublicationDate"),
                    "preprint_id": candidate["id"], "doi": candidate.get("doi"), "version": candidate.get("versionNumber"),
                    "posted_date": candidate.get("firstPublicationDate"), "source_url": receipt["final_url"],
                    "license": candidate.get("license"),
                    "body_discovery": body_origin,
                    "source_sha256": receipt["body_sha256"], "text_sha256": validation["text_sha256"],
                    "metadata_sha256": _digest(_canonical(candidate)), "discovery_methods": value["discovery_methods"],
                    "version_metadata": candidate.get("versionList"), "body_version": validation["body_version"],
                    "body_version_verified": validation["version_verified"],
                    "source_availability_gate_sha256": authorization["gate_sha256"]}
                result["sources"].append({"preprint_derived": True, "preprint_source": provenance,
                    "source_label": "Preprint", "source_color": NAVY, "css_class": "preprint-derived",
                    "Link": receipt["final_url"], "PublishedJournalLink": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "PublicationDate": published.get("firstPublicationDate"), "body_path": receipt["body_path"],
                    "text": validation["text"], "format": validation["format"], "identity_verified": True,
                    "native_scientific_R3_gate_still_required": True, "scientific_inclusion_authorized": False})
                break
            except Exception as error:
                result["errors"].append({"stage": "preprint_body", "preprint_id": candidate["id"], "error_type": type(error).__name__})
    result["receipts"] = cache.receipts
    result["network_calls"] = cache.network_calls
    result.update(status="body_verified" if result["sources"] else "candidate_only" if candidates else "lookup_error" if result["errors"] else "no_match",
                  reason="source_only_native_R3_gate_required" if result["sources"] else "no_verified_preprint_body")
    return result
