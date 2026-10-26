"""Fresh article-level PMC availability; no preprint or model requests.

Every transport is injected. PMID/PMCID mismatch, challenges and transient
failures are unknown, never permission to fetch a preprint.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urljoin, urlsplit
import xml.etree.ElementTree as ET

from .europepmc_fallback import ResponseCache, _ArticleHTML
from .exact_match import normalize_title


def pmcid(value):
    match = re.fullmatch(r"(?:PMC)?(\d+)(?:\.\d+)?", str(value or "").strip(), re.I)
    return "PMC" + match[1] if match else None


def _jats(raw, pmc, pmid):
    try:
        article = ET.fromstring(raw)
    except ET.ParseError:
        return {"state": "unknown", "reason": "not_verified_JATS"}, []
    for element in article.iter():
        element.tag = element.tag.split("}")[-1]
    if article.tag != "article":
        children = article.findall("./article")
        if len(children) != 1:
            return {"state": "unknown", "reason": "no_unique_main_article"}, []
        article = children[0]
    front = article.find("./front/article-meta")
    if front is None:
        return {"state": "unknown", "reason": "missing_article_identity"}, []
    ids = {e.get("pub-id-type", "").lower(): "".join(e.itertext()).strip()
           for e in front.findall("./article-id")}
    if pmcid(ids.get("pmc") or ids.get("pmcid")) != pmc or ids.get("pmid") != pmid:
        return {"state": "unknown", "reason": "PMC_PMID_identity_mismatch"}, []
    pdfs = []
    for element in front.findall("./self-uri"):
        if element.get("content-type") == "pdf" or element.get("xlink:href", "").lower().endswith(".pdf"):
            href = next((v for k, v in element.attrib.items() if k.split("}")[-1] == "href"), "")
            if href:
                pdfs.append(href)
    body = article.find("./body")
    if body is not None:
        for parent in list(body.iter()):
            for child in list(parent):
                if child.tag == "abstract" or any("abstract" in child.get(k, "").lower() for k in ("sec-type", "role")):
                    parent.remove(child)
        text = " ".join(body.itertext()).strip()
        # Source availability is broader than numerical extraction safety: a
        # genuine short full article still blocks recovery from a preprint.
        if len(text) >= 50 and body.findall(".//p"):
            return {"state": "available", "reason": "identity_verified_main_article_body",
                    "identity_verified": True, "body_characters": len(text)}, pdfs
        if text:
            return {"state": "unknown", "reason": "nonempty_main_body_requires_source_review",
                    "identity_verified": True}, pdfs
    return {"state": "unavailable", "reason": "identity_verified_front_matter_only",
            "identity_verified": True}, pdfs


def _html(raw, pmc, pmid, med):
    parser = _ArticleHTML()
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
    except Exception:
        return {"state": "unknown", "reason": "malformed_PMC_HTML"}, []
    meta = parser.meta
    pids = meta.get("citation_pmid", [])
    pcids = meta.get("citation_pmcid", []) + meta.get("ncbi_pcid", [])
    title = (meta.get("citation_title") or [""])[0]
    raw_pmc = pmc.encode().lower() in raw.lower()
    identity = (pmid in pids and (pmc in {pmcid(x) for x in pcids} or raw_pmc)
                and normalize_title(title) == normalize_title(med.get("title")))
    if not identity:
        return {"state": "unknown", "reason": "PMC_HTML_identity_unverified"}, []
    pdfs = meta.get("citation_pdf_url", [])
    text = " ".join(parser.parts)
    if len(text.strip()) >= 50:
        return {"state": "available", "reason": "identity_verified_main_article_HTML",
                "identity_verified": True, "body_characters": len(text)}, pdfs
    if text.strip():
        return {"state": "unknown", "reason": "nonempty_PMC_HTML_body_requires_source_review",
                "identity_verified": True}, pdfs
    return {"state": "unavailable", "reason": "identity_verified_HTML_without_main_body",
            "identity_verified": True}, pdfs


def probe_article(pmid, published_core, *, http_get, cache_dir, extract_pdf_text=None, saved_pmcids=()):
    """Cross-check all fresh identifier routes, then every original PMC format."""
    pmid = str(pmid)
    # Availability is checked again on resume; paid reviewer request reuse is
    # keyed independently to its unchanged source/context/prompt. A newly
    # available original PMC article must stop future preprint retrieval.
    cache = ResponseCache(cache_dir, http_get, refresh=True)
    checks, unknown = [], False
    found = {pmcid(v) for v in [published_core.get("pmcid"), *saved_pmcids]
             if re.fullmatch(r"PMC\d+(?:\.\d+)?", str(v or ""), re.I)}
    pubmed_ids, converted_ids = [], []
    fresh_med_pmc = published_core.get("pmcid")

    def capture(url, accept, route):
        nonlocal unknown
        raw, receipt = cache.capture(url, accept=accept, purpose="original_PMC_availability")
        entry = {"route": route, "url": url, "status": receipt["status"],
                 "response_sha256": receipt["body_sha256"], "body_path": receipt["body_path"]}
        checks.append(entry)
        if receipt["status"] not in {200, 404, 410}:
            unknown = True
        return raw, entry

    try:
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode({
            "query": f"EXT_ID:{pmid} AND SRC:MED", "format": "json", "resultType": "core", "pageSize": 10})
        raw, entry = capture(url, "application/json", "fresh_EuropePMC_MED")
        document = json.loads(raw) if entry["status"] == 200 else {}
        matches = [r for r in document.get("resultList", {}).get("result", []) if r.get("source") == "MED" and str(r.get("id")) == pmid]
        if len(matches) != 1:
            unknown = True
        else:
            med = matches[0]
            fresh_med_pmc = med.get("pmcid")
            if pmcid(fresh_med_pmc):
                found.add(pmcid(fresh_med_pmc))
            if str(med.get("inPMC", "")).upper() == "Y" and not fresh_med_pmc:
                unknown = True
            entry["identity_verified"] = True
    except Exception:
        unknown = True
    try:
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode({"db": "pubmed", "id": pmid, "retmode": "xml"})
        raw, entry = capture(url, "application/xml", "fresh_PubMed_ArticleIdList")
        document = ET.fromstring(raw) if entry["status"] == 200 else None
        articles = document.findall("./PubmedArticle") if document is not None else []
        article = next((a for a in articles if a.findtext("./MedlineCitation/PMID") == pmid), None)
        if article is None:
            unknown = True
        else:
            pubmed_ids = [pmcid(e.text) for e in article.findall("./PubmedData/ArticleIdList/ArticleId") if e.get("IdType") == "pmc"]
            found.update(v for v in pubmed_ids if v)
            entry["identity_verified"] = True
    except Exception:
        unknown = True
    try:
        url = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?" + urlencode({"ids": pmid, "idtype": "pmid", "format": "json", "tool": "meta_analysis_monthly"})
        raw, entry = capture(url, "application/json", "fresh_PMC_id_converter")
        document = json.loads(raw) if entry["status"] == 200 else {}
        matches = [r for r in document.get("records", []) if str(r.get("requested-id")) == pmid and str(r.get("pmid", pmid)) == pmid]
        if document.get("status") != "ok" or len(matches) != 1:
            unknown = True
        else:
            record = matches[0]
            converted = pmcid(record.get("pmcid"))
            if converted:
                converted_ids.append(converted)
                found.add(converted)
            elif record.get("status") != "error" or record.get("errmsg") != "Identifier not found in PMC":
                unknown = True
            entry["identity_verified"] = True
    except Exception:
        unknown = True
    if str(published_core.get("inPMC", "")).upper() == "Y" and not found:
        unknown = True
    if not found:
        return {"pmid": pmid, "availability": "unknown" if unknown else "pmcid_missing", "pmcids": [],
                "fresh_epmc_pmcid": fresh_med_pmc,
                "fresh_pubmed_pmcids": pubmed_ids, "fresh_idconv_pmcids": converted_ids,
                "actual_source_check_complete": not unknown, "identity_verified": not unknown,
                "transient_error": unknown, "verified_full_pmc_available": False, "proofs": checks}
    available = False
    for identifier in sorted(found):
        owned_pdfs, route_states = [], []
        routes = [
            ("PMC_EFetch_XML", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode({"db": "pmc", "id": identifier[3:], "retmode": "xml"})),
            ("EuropePMC_original_fullTextXML", f"https://www.ebi.ac.uk/europepmc/webservices/rest/{identifier}/fullTextXML"),
            ("PMC_article_HTML", f"https://pmc.ncbi.nlm.nih.gov/articles/{identifier}/"),
        ]
        for route, url in routes:
            try:
                raw, entry = capture(url, "application/xml,text/html", route)
                if entry["status"] in {404, 410}:
                    route_states.append("unavailable")
                    continue
                if entry["status"] != 200:
                    route_states.append("unknown")
                    continue
                proof, pdfs = (_html(raw, identifier, pmid, published_core) if route == "PMC_article_HTML"
                               else _jats(raw, identifier, pmid))
                entry.update(proof)
                route_states.append(proof["state"])
                if proof["state"] == "available":
                    available = True
                    break
                if proof.get("identity_verified"):
                    for link in pdfs:
                        if not urlsplit(link).scheme:
                            link = urljoin(f"https://pmc.ncbi.nlm.nih.gov/articles/{identifier}/pdf/", link)
                        parsed = urlsplit(link)
                        if parsed.scheme == "https" and parsed.hostname in {"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
                            owned_pdfs.append(link)
            except Exception:
                route_states.append("unknown")
        if available:
            break
        canonical_pdf = f"https://pmc.ncbi.nlm.nih.gov/articles/{identifier}/pdf/"
        for url in sorted(set([*owned_pdfs, canonical_pdf])):
            try:
                raw, entry = capture(url, "application/pdf", "identity_bound_article_owned_PMC_PDF")
                if entry["status"] == 200 and raw.startswith(b"%PDF-"):
                    # A genuine article-owned scanned PDF is available even
                    # when native extraction/OCR policy cannot parse its text.
                    if url in owned_pdfs:
                        entry.update(state="available", identity_verified=True, article_owned_link_verified=True)
                        available = True
                        break
                    # A canonical PDF namespace is not an independently bound
                    # article-owned link. Prove its body identity or hold it.
                    if extract_pdf_text is not None:
                        text = extract_pdf_text(raw)
                        if (normalize_title(published_core.get("title"))
                                and normalize_title(published_core.get("title")) in normalize_title(text[:20000])
                                and len(str(text).strip()) >= 100):
                            entry.update(state="available", identity_verified=True, canonical_PMC_PDF_title_verified=True)
                            available = True
                            break
                    route_states.append("unknown")
                    continue
                route_states.append("unavailable" if entry["status"] in {404, 410} else "unknown")
            except Exception:
                route_states.append("unknown")
        unknown |= any(s == "unknown" for s in route_states)
        if available:
            break
    state = "available" if available else "unknown" if unknown else "full_text_unavailable"
    return {"pmid": pmid, "availability": state, "pmcids": sorted(found),
            "fresh_epmc_pmcid": fresh_med_pmc, "fresh_pubmed_pmcids": pubmed_ids,
            "fresh_idconv_pmcids": converted_ids, "actual_source_check_complete": not unknown or available,
            "identity_verified": not unknown or available, "transient_error": unknown and not available,
            "verified_full_pmc_available": available, "proofs": checks}
