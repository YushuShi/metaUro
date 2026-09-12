from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER_PATH = REPO_ROOT / "scripts" / "run_bacterial_alias_delta.py"
SPEC = importlib.util.spec_from_file_location("bacterial_alias_delta_driver", DRIVER_PATH)
assert SPEC and SPEC.loader
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def minimal_record(pmid: str) -> dict:
    return {
        "pmid": pmid,
        "pmcid": "",
        "doi": "",
        "published_date": "2020-01-01",
        "journal_publication_date": "2020-01-01",
        "electronic_publication_date": "Unknown",
        "title": f"Delta article {pmid}",
        "abstract": "The abbreviated bacterial species is evaluated for disease incidence.",
        "mesh": [],
        "publication_types": ["Journal Article"],
        "authors": [],
        "journal": "Test Journal",
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


class BacterialAliasDeltaFollowOnTests(unittest.TestCase):
    def test_sealed_delta_packet_and_sources_validate(self) -> None:
        packet = driver.validate_delta_packet()
        self.assertEqual(packet["new_context_count"], len(packet["new_contexts"]))
        self.assertIs(packet["source_packets_unchanged"], True)
        self.assertIs(packet["active_screening_checkpoints_rewritten"], False)

    def test_metadata_builder_preserves_exact_scope_and_records_no_esearch(self) -> None:
        packet = driver.validate_delta_packet()
        records = {pmid: minimal_record(pmid) for pmid in packet["new_context_pmids"]}
        candidates, receipt = driver.build_candidate_packet(
            packet,
            records,
            request_count=1,
            ncbi_status="configured_not_validated",
        )
        self.assertEqual(
            {article["pmid"] for article in candidates["articles"]},
            set(packet["new_context_pmids"]),
        )
        self.assertEqual(
            sum(len(article["candidate_contexts"]) for article in candidates["articles"]),
            packet["new_context_count"],
        )
        self.assertEqual(receipt["esearch_requests"], 0)
        self.assertEqual(receipt["requested_pmids"], receipt["returned_pmids"])
        self.assertEqual(len(receipt["context_map"]), packet["new_context_count"])

    def test_metadata_builder_fails_closed_on_missing_or_extra_pmid(self) -> None:
        packet = driver.validate_delta_packet()
        records = {pmid: minimal_record(pmid) for pmid in packet["new_context_pmids"]}
        records.pop(next(iter(records)))
        records["999999999"] = minimal_record("999999999")
        with self.assertRaisesRegex(RuntimeError, "exact delta PMID set"):
            driver.build_candidate_packet(
                packet,
                records,
                request_count=1,
                ncbi_status="configured_not_validated",
            )

    def test_driver_exposes_neither_discovery_nor_apply_stage(self) -> None:
        source = DRIVER_PATH.read_text(encoding="utf-8")
        choices = source.split("choices=(", 1)[1].split("),", 1)[0]
        self.assertNotIn('"discover"', choices)
        self.assertNotIn('"apply"', choices)
        self.assertIn("monthly.entrez_fetch(requested)", source)
        self.assertNotIn("monthly.entrez_search", source)


if __name__ == "__main__":
    unittest.main()
