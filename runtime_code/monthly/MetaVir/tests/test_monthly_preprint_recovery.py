"""Offline integration checks: no network, no paid gateway dispatch."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from automation import monthly_preprint_recovery as monthly
from automation.preprint_recovery import preprint_screening as producer


class MonthlyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "metaPee"
        self.run = self.root / "data/monthly_updates/2026-10-01_2026-10-25"
        self.run.mkdir(parents=True)
        self.row = {"pmid": "1", "context_id": "1|Red meat|Bladder cancer|Incidence", "exposure": "Red meat",
                    "disease": "Bladder cancer", "outcome": "Incidence", "eligibility": "INCLUDE", "needs_full_text": True}
        monthly.save(self.run / "second_screen.json", {"results": [self.row]})
        monthly.save(self.run / "final_screening.json", {"results": [dict(self.row, final_status="ELIGIBLE_NOT_POOLABLE")]})
        self.med = {"id": "1", "pmid": "1", "source": "MED", "title": "Study title", "inPMC": "N",
                    "authorList": {"author": [{"lastName": "Smith"}]}, "firstPublicationDate": "2021-01-01"}
        self.calls = {"http": 0, "recovery": 0, "model_jobs": 0}

    def tearDown(self):
        self.directory.cleanup()

    def services(self, *, fail_model=False):
        def get(url, **kwargs):
            self.calls["http"] += 1
            return {"status": 200, "body": json.dumps({"resultList": {"result": [self.med]}}).encode()}

        def recover(med, **kwargs):
            self.calls["recovery"] += 1
            proof = kwargs["authorize_retrieval"]("1")
            self.assertTrue(proof["preprint_fetch_allowed"])
            self.assertEqual(kwargs["pmc_availability"], "pmcid_missing")
            cache = Path(kwargs["cache_dir"])
            (cache / "blobs").mkdir(parents=True, exist_ok=True)
            raw, text = b"raw paper", "Methods and Results " * 400
            (cache / "blobs/raw").write_bytes(raw)
            candidate = {"source": "PPR", "id": "PPR10", "title": med["title"],
                         "authorList": med["authorList"], "doi": "10.1/example", "versionNumber": 1}
            return {"status": "body_verified", "candidates": [{"record": candidate}], "errors": [],
                    "sources": [{"text": text, "body_path": "blobs/raw", "preprint_source": {
                        "preprint_id": "PPR10", "doi": "10.1/example", "version": 1,
                        "source_url": "https://example.org/paper", "source_sha256": hashlib.sha256(raw).hexdigest(),
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "discovery_methods": ["exact_title_surname"]}}]}

        def run_job(client, job, output):
            producer.validate_source(job)
            folder = Path(output) / "jobs/job1"
            folder.mkdir(parents=True, exist_ok=True)
            (Path(output) / "calls").mkdir(exist_ok=True)
            if fail_model:
                raise RuntimeError("Synthetic acknowledged model failure")
            if not (folder / "result.json").exists():
                self.calls["model_jobs"] += 1
            result = {"job_id": "job1", "status": "reviewed", "reviews": {
                "luna": {"complete_bundle": [1, 2, 3]}, "gemini": {"complete_bundle": [4, 5, 6]}},
                "adjudication": {"results": [{"context_id": self.row["context_id"], "decision": "INCLUDE_META",
                                               "confidence": "high", "reason": "Saved whole reviewer bundle"}]}}
            monthly.save(folder / "result.json", result)
            return result

        return {"http_get": get, "recover_preprint": recover, "run_job": run_job,
                "probe_pmc": lambda pmid, med, **kwargs: {
                    "pmid": pmid, "actual_source_check_complete": not bool(med.get("pmcid")),
                    "identity_verified": not bool(med.get("pmcid")), "transient_error": bool(med.get("pmcid")),
                    "verified_full_pmc_available": False,
                    "availability": "unknown" if med.get("pmcid") else "pmcid_missing",
                    "pmcids": [med["pmcid"]] if med.get("pmcid") else [],
                    "proofs": [{"route": "synthetic_fresh_PubMed_and_idconv"}],
                    "fresh_pubmed_pmcids": [], "fresh_idconv_pmcids": []},
                "client_factory": lambda: object(), "protocol": lambda *a, **k: {
                    "native_criteria": "Native criteria preserved", "native_extraction_fields": ["effect_size"],
                    "native_extraction_schema": {}},
                "cost_report": lambda output: {"gateway_reported_cost_usd": 0.25, "unknown_cost_calls": 1,
                                               "dispatches": 2, "calls": []}}

    def test_missing_PMC_runs_real_source_guard_separate_R3_and_retains_complete_bundles(self):
        source_before = monthly.sha(self.run / "second_screen.json")
        existing = self.root / "data/preprint_recovery/active_manifest.json"
        monthly.save(existing, {"status": "complete", "contexts": [{"old": "retained"}]})
        old_sha = monthly.sha(existing)
        binding = monthly.recover_run(self.root, self.run, services=self.services())
        manifest = json.loads((self.root / binding["manifest_path"]).read_text())
        result = json.loads((self.root / manifest["jobs"][0]["result_path"]).read_text())
        self.assertEqual(result["reviews"]["gemini"]["complete_bundle"], [4, 5, 6])
        self.assertEqual(manifest["cost"]["unknown_cost_calls"], 1)
        self.assertEqual(manifest["cost"]["gateway_reported_cost_usd"], 0.25)
        self.assertEqual(monthly.sha(existing), old_sha)
        self.assertEqual(monthly.sha(self.run / "second_screen.json"), source_before)
        self.assertEqual(self.calls["model_jobs"], 1)
        self.assertTrue(manifest["holds"])
        public_bytes = "\n".join(p.read_text() for p in (self.run / "preprint_recovery").rglob("*.json"))
        self.assertNotIn("Methods and Results Methods and Results", public_bytes)
        self.assertFalse(list((self.run / "preprint_recovery").rglob("*.txt")))

    def test_any_track_full_PMC_bypasses_all_transports_and_models(self):
        monthly.save(self.run / "monthly_parallel/gemini_r3_results.json", {"results": [
            dict(self.row, pmcid="PMC1", pmc_source_status="full_text", text_sha256="a" * 64, third_round_status="COMPLETED")]})
        result = monthly.recover_run(self.root, self.run, services={})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.calls, {"http": 0, "recovery": 0, "model_jobs": 0})

    def test_saved_source_manifest_without_PMID_uses_exact_PMCID_mapping(self):
        row = dict(self.row, pmcid="PMC1")
        monthly.save(self.run / "second_screen.json", {"results": [row]})
        monthly.save(self.root / "combined/old/full_text_source_manifest.json", {
            "sources": {"PMC1": {"status": "full_text", "text_sha256": "a" * 64}}})
        with self.assertRaises(monthly.RecoveryOperationalHold):
            monthly.recover_run(self.root, self.run, services={})
        self.assertEqual(self.calls, {"http": 0, "recovery": 0, "model_jobs": 0})

    def test_current_source_manifest_does_not_masquerade_as_completed_native_R3(self):
        monthly.save(self.run / "full_text_sources.json", {"results": [
            dict(self.row, pmcid="PMC1", pmc_source_status="full_text", text_sha256="a" * 64)]})
        with self.assertRaises(monthly.RecoveryOperationalHold):
            monthly.recover_run(self.root, self.run, services={})
        self.assertEqual(self.calls, {"http": 0, "recovery": 0, "model_jobs": 0})

    def test_newly_accessible_original_requires_native_R3_resume_not_false_completion(self):
        service = self.services()
        service["probe_pmc"] = lambda *a, **k: {"availability": "available", "pmcids": ["PMC1"], "proofs": [{"route": "verified_original"}]}
        with self.assertRaises(monthly.RecoveryOperationalHold):
            monthly.recover_run(self.root, self.run, services=service)
        manifest = json.loads((self.run / "preprint_recovery/recovery_manifest.json").read_text())
        self.assertIn("resume native full-text R3", manifest["holds"][0]["reason"])
        self.assertEqual(self.calls["recovery"], 0)
        self.assertEqual(self.calls["model_jobs"], 0)

    def test_PMCID_present_unknown_holds_before_preprint_fetch_and_model(self):
        self.med["pmcid"] = "PMC1"
        with self.assertRaises(monthly.RecoveryOperationalHold):
            monthly.recover_run(self.root, self.run, services=self.services())
        self.assertEqual(self.calls["recovery"], 0)
        self.assertEqual(self.calls["model_jobs"], 0)
        manifest = json.loads((self.run / "preprint_recovery/recovery_manifest.json").read_text())
        self.assertEqual(manifest["status"], "operational_hold")

    def test_unknown_bill_after_failed_job_not_erased(self):
        with self.assertRaises(monthly.RecoveryOperationalHold):
            monthly.recover_run(self.root, self.run, services=self.services(fail_model=True))
        manifest = json.loads((self.run / "preprint_recovery/recovery_manifest.json").read_text())
        self.assertEqual(manifest["cost"]["unknown_cost_calls"], 1)
        self.assertEqual(manifest["cost"]["gateway_reported_cost_usd"], 0.25)

    def test_resume_reuses_pinned_metadata_and_model_job(self):
        service = self.services()
        monthly.recover_run(self.root, self.run, services=service)
        monthly.recover_run(self.root, self.run, services=service)
        self.assertEqual(self.calls["http"], 1)
        self.assertEqual(self.calls["model_jobs"], 1)

    def test_no_R3_need_or_human_override_makes_no_external_calls(self):
        for changed in (dict(self.row, needs_full_text=False), dict(self.row, human_adjudication={"decision": "keep"})):
            with self.subTest(changed=changed):
                monthly.save(self.run / "second_screen.json", {"results": [changed]})
                # Independent temporary run IDs keep immutable checkpoints exact.
                run = self.root / "data/monthly_updates" / ("no-work-" + str(len(changed)))
                run.mkdir(exist_ok=True)
                monthly.save(run / "second_screen.json", {"results": [changed]})
                monthly.recover_run(self.root, run, services={})
        self.assertEqual(self.calls, {"http": 0, "recovery": 0, "model_jobs": 0})

    def test_future_initial_budget_is_65536_and_part_of_paid_request_fingerprint(self):
        received = []
        def create(**kwargs):
            received.append(kwargs)
            response = SimpleNamespace(model=kwargs["model"], usage=None, choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"ok":true}'), finish_reason="stop")])
            return SimpleNamespace(parse=lambda: response, headers={"x-response-cost": "0.0"})
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=create))))
        client.with_options = lambda **kwargs: client
        folder = Path(self.directory.name) / "fake_calls"
        job = {"source": {"text_sha256": "a" * 64}, "contexts": [{"context_id": "ctx"}]}
        value, response_path = producer.call_model(client, folder, "luna", "system", "prompt", job)
        self.assertEqual(value, {"ok": True})
        self.assertEqual(received[0]["max_completion_tokens"], 65536)
        dispatch = json.loads(Path(response_path).with_name("dispatch.json").read_text())
        self.assertEqual(dispatch["max_completion_tokens"], 65536)
        request = {k: v for k, v in dispatch.items() if k not in {"created_at", "request_sha256"}}
        self.assertEqual(producer.digest(request), dispatch["request_sha256"])


if __name__ == "__main__":
    unittest.main()
