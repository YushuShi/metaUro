"""Metadata-only, exact title + published-first-author-surname candidate matching.

This module performs no I/O. A match is a search candidate, never identity,
full-text retrieval, screening, or extraction approval. AUTH searches the
published first author's surname anywhere in a preprint's author list; author
order and other authors are deliberately not search constraints.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urlencode

POLICY_VERSION = "exact-title-first-surname-v1"
SEARCH_ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _plain(value):
    value = html.unescape(str(value or ""))
    parser = _TextOnly()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def normalize_title(value):
    """Ignore case, Unicode presentation, whitespace and punctuation only.

    Preserve every word, number, accent and mathematical symbol. Do not remove
    subtitles, preprint labels, stopwords, or change spelling/token order.
    Punctuation becomes a word boundary rather than joining adjacent words.
    """
    value = unicodedata.normalize("NFKC", _plain(value)).casefold()
    return " ".join("".join(" " if unicodedata.category(c).startswith("P") else c
                            for c in value).split())


def normalize_surname(value):
    """Conservative surname equality; retain particles, accents and hyphens."""
    value = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
    value = value.translate(str.maketrans({"’": "'", "‘": "'", "ʼ": "'",
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "−": "-"}))
    return " ".join(value.split()).strip(" .,")


def _author_sequence(record):
    author_list = record.get("authorList")
    if isinstance(author_list, dict) and "author" in author_list:
        authors = author_list["author"]
        return (authors if isinstance(authors, list) else [authors]), "authorList.author"
    for key in ("author", "authors"):
        authors = record.get(key)
        if isinstance(authors, list):
            return authors, key
    if isinstance(record.get("first_author"), dict):
        return [record["first_author"]], "first_author"
    # Europe PMC's documented authorString is surname + initials, comma-delimited.
    text = record.get("authorString")
    if isinstance(text, str) and text.strip():
        return [x.strip() for x in text.split(",") if x.strip()], "authorString"
    return [], "missing"


def _surname(author, source):
    if isinstance(author, dict):
        for key in ("familyName", "lastName", "family", "surname"):
            value = author.get(key)
            if isinstance(value, str) and value.strip():
                surname = value.strip()
                return {"surname": surname, "normalized_surname": normalize_surname(surname),
                        "source": source + "." + key, "confidence": "structured"}
        # Full given names do not have a universally reliable surname position.
        text = author.get("fullName") or author.get("name")
    else:
        text = author
    if isinstance(text, str):
        # Recognize only explicit surname + uppercase initials; never guess the
        # last token of an unstructured full name or a corporate author.
        text = text.strip().rstrip(".")
        match = re.fullmatch(r"(.+?)\s+([A-Z](?:[A-Z. -]*[A-Z.])?)", text)
        if match and any(c.isalpha() for c in match[1]):
            surname = match[1].strip()
            if not re.search(r"\b(consortium|group|collaboration|collaborators|investigators|committee)\b",
                             surname, re.I):
                return {"surname": surname, "normalized_surname": normalize_surname(surname),
                        "source": source + ".surname_initials", "confidence": "parsed_initials"}
    return {"surname": None, "normalized_surname": None, "source": source,
            "confidence": "unresolved"}


def extract_first_author(record):
    """Return surname of actual first listed author, without skipping groups."""
    authors, source = _author_sequence(record)
    return _surname(authors[0], source + "[0]") if authors else _surname(None, "missing")


def extract_author_surnames(record):
    """Return ordered surname facts, including unresolved author positions."""
    authors, source = _author_sequence(record)
    return [dict(_surname(a, f"{source}[{i}]"), position=i + 1)
            for i, a in enumerate(authors)]


def _phrase(value):
    # Quote all user/title text; never allow a title to introduce query syntax.
    value = " ".join(_plain(value).split())
    if not value:
        raise ValueError("A nonempty exact title and first-author surname are required")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(title, surname):
    """A supported Europe PMC phrase query; local exact validation is mandatory."""
    if not normalize_title(title) or not normalize_surname(surname):
        raise ValueError("A nonempty exact title and first-author surname are required")
    return f"TITLE:{_phrase(title)} AND AUTH:{_phrase(surname)} AND SRC:PPR"


def search_url(title, surname, *, cursor_mark="*", page_size=100):
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    return SEARCH_ENDPOINT + "?" + urlencode({"query": build_query(title, surname),
        "format": "json", "resultType": "core", "pageSize": page_size,
        "cursorMark": cursor_mark})


def match_candidate(published, ppr):
    """Strict local metadata validation of one returned SRC:PPR candidate.

    `matched_author_positions` is one-based. `ppr_first_author_equal` is a
    separate note; a matching non-first-position author still meets this search
    policy. Results never authorize R3 or imply article identity is settled.
    """
    first = extract_first_author(published)
    authors = extract_author_surnames(ppr)
    title = normalize_title(published.get("title"))
    candidate_title = normalize_title(ppr.get("title"))
    title_equal = bool(title and candidate_title and title == candidate_title)
    positions = [a["position"] for a in authors if first["normalized_surname"]
                 and a["normalized_surname"] == first["normalized_surname"]]
    source = str(ppr.get("source", "")).upper()
    source_is_preprint = source == "PPR"
    matched = bool(source_is_preprint and title_equal and positions)
    reason = ("exact_title_and_published_first_surname" if matched else
              "candidate_not_SRC_PPR" if not source_is_preprint else
              "published_first_author_surname_unresolved" if not first["normalized_surname"] else
              "title_not_exact" if not title_equal else
              "published_first_surname_absent_from_candidate")
    return {"policy_version": POLICY_VERSION, "candidate_only": True,
        "matched": matched, "reason": reason, "title_equal": title_equal,
        "published_title_normalized": title, "ppr_title_normalized": candidate_title,
        "published_first_author": first,
        "ppr_first_author": authors[0] if authors else extract_first_author(ppr),
        "matched_author_positions": positions,
        "ppr_first_author_equal": 1 in positions,
        "source_is_preprint": source_is_preprint,
        "candidate_id": ppr.get("id"), "candidate_doi": ppr.get("doi"),
        "published_pmid": published.get("pmid") or published.get("id"),
        "author_metadata_complete": bool(authors and all(a["normalized_surname"] for a in authors))}
