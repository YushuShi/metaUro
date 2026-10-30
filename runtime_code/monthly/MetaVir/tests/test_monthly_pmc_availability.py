"""All original-PMC formats and metadata lag; injected offline HTTP only."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from automation.preprint_recovery.pmc_availability import probe_article


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.calls = []
        self.med = {"source": "MED", "id": "1", "title": "Original study", "inPMC": "N"}
        self.pubmed_pmc, self.converter_pmc = None, None
        self.routes = {}

    def tearDown(self):
        self.directory.cleanup()

    def get(self, url, **kwargs):
        self.calls.append(url)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if "europepmc" in url and query.get("resultType") == ["core"]:
            return {"status": 200, "body": json.dumps({"resultList": {"result": [self.med]}}).encode(), "headers": {}, "final_url": url}
        if query.get("db") == ["pubmed"]:
            ids = f'<ArticleId IdType="pmc">{self.pubmed_pmc}</ArticleId>' if self.pubmed_pmc else ""
            body = ('<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID></MedlineCitation>'
                    '<PubmedData><ArticleIdList>' + ids + '</ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>').encode()
            return {"status": 200, "body": body, "headers": {}, "final_url": url}
        if "idconv" in url:
            record = {"pmid": 1, "requested-id": "1"}
            record.update({"pmcid": self.converter_pmc} if self.converter_pmc else
                          {"status": "error", "errmsg": "Identifier not found in PMC"})
            return {"status": 200, "body": json.dumps({"status": "ok", "records": [record]}).encode(), "headers": {}, "final_url": url}
        kind = "xml" if query.get("db") == ["pmc"] else "epmc" if "fullTextXML" in url else "pdf" if "/pdf/" in url else "html"
        status, body = self.routes.get(kind, (404, b""))
        return {"status": status, "body": body, "headers": {}, "final_url": url}

    def jats(self, body, pmid="1", pdf=False):
        link = '<self-uri content-type="pdf" xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="original.pdf"/>' if pdf else ""
        return ('<article><front><article-meta><article-id pub-id-type="pmc">PMC7</article-id>'
                '<article-id pub-id-type="pmid">' + pmid + '</article-id>' + link + '</article-meta></front>' + body + '</article>').encode()

    def run_probe(self, extractor=None):
        return probe_article("1", self.med, http_get=self.get, cache_dir=Path(self.directory.name), extract_pdf_text=extractor)

    def test_missing_requires_both_fresh_identifier_crosschecks(self):
        result = self.run_probe()
        self.assertEqual(result["availability"], "pmcid_missing")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(result["proofs"]), 3)

    def test_pubmed_identifier_lag_finds_genuine_short_original_body(self):
        self.pubmed_pmc = "PMC7"
        self.routes["xml"] = (200, self.jats('<body><p>' + 'A genuine short complete article. ' * 3 + '</p></body>'))
        result = self.run_probe()
        self.assertEqual(result["availability"], "available")
        self.assertTrue(result["verified_full_pmc_available"])
        self.assertEqual(result["fresh_pubmed_pmcids"], ["PMC7"])

    def test_idconv_lag_and_canonical_PDF_are_checked(self):
        self.converter_pmc = "PMC7"
        self.routes["pdf"] = (200, b"%PDF-1.4 original")
        result = self.run_probe(lambda raw: 'Original study ' + 'Article text. ' * 20)
        self.assertEqual(result["availability"], "available")
        self.assertTrue(any(url.endswith('/articles/PMC7/pdf/') for url in self.calls))

    def test_all_four_original_source_routes_terminal_missing_can_allow_fallback(self):
        self.med["pmcid"] = "PMC7"
        result = self.run_probe()
        self.assertEqual(result["availability"], "full_text_unavailable")
        self.assertFalse(result["transient_error"])
        self.assertEqual(len(result["proofs"]), 7)
        self.assertTrue(any(url.endswith('/articles/PMC7/pdf/') for url in self.calls))

    def test_PDF_access_or_transport_error_is_unknown_not_unavailable(self):
        self.med["pmcid"] = "PMC7"
        self.routes["pdf"] = (403, b"challenge")
        result = self.run_probe()
        self.assertEqual(result["availability"], "unknown")
        self.assertTrue(result["transient_error"])

    def test_wrong_original_article_identity_holds(self):
        self.med["pmcid"] = "PMC7"
        self.routes["xml"] = (200, self.jats('<body><p>' + 'Wrong paper body. ' * 20 + '</p></body>', pmid="2"))
        self.assertEqual(self.run_probe()["availability"], "unknown")

    def test_abstract_methods_results_are_not_complete_original_body(self):
        self.med["pmcid"] = "PMC7"
        self.routes["xml"] = (200, self.jats('<body><sec sec-type="abstract"><title>Methods and Results</title><p>' + 'Abstract only. ' * 100 + '</p></sec></body>'))
        self.assertEqual(self.run_probe()["availability"], "full_text_unavailable")

    def test_article_owned_scanned_PDF_blocks_even_without_text_extractor(self):
        self.med["pmcid"] = "PMC7"
        self.routes["xml"] = (200, self.jats('', pdf=True))
        self.routes["pdf"] = (200, b"%PDF-1.4 scanned")
        self.assertEqual(self.run_probe()["availability"], "available")

    def test_resume_rechecks_newly_available_PMC_instead_of_cached_missing(self):
        self.assertEqual(self.run_probe()["availability"], "pmcid_missing")
        self.pubmed_pmc = "PMC7"
        self.routes["xml"] = (200, self.jats('<body><p>' + 'Now available original article. ' * 5 + '</p></body>'))
        self.assertEqual(self.run_probe()["availability"], "available")

    def test_genuine_HTML_without_Methods_Results_still_blocks_preprint(self):
        self.med["pmcid"] = "PMC7"
        self.routes["html"] = (200, ('<html><head><meta name="citation_pmid" content="1">'
            '<meta name="citation_pmcid" content="PMC7"><meta name="citation_title" content="Original study">'
            '</head><body><article><p>' + 'A genuine short communication. ' * 5 + '</p></article></body></html>').encode())
        self.assertEqual(self.run_probe()["availability"], "available")


if __name__ == '__main__':
    unittest.main()
