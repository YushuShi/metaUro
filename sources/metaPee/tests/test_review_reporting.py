import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import REPORTER_COOKIE_NAME, app, send_developer_notification


STUDY = {
    "PMID": "123",
    "Effect Size": 1.10,
    "Lower CI": 1.01,
    "Upper CI": 1.20,
    "Cases": 132,
    "Sample Size": 1000,
    "Design": "Cohort",
    "Timing": "Prospective",
    "comparison_type": "Highest versus lowest",
    "exposure_measurement_type": "dietary_intake",
    "Authors": "Example AB",
}

REPORT = {
    "pmid": "123",
    "disease": "Bladder cancer",
    "exposure": "Tomato",
    "outcome": "Incidence",
}


class ReviewReportingTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True

    def _client(self):
        client = app.test_client()
        client.get("/")
        return client

    @staticmethod
    def _store(directory):
        path = Path(directory) / "verifications.json"
        path.write_text("{}", encoding="utf-8")
        return str(path)

    def test_render_blueprint_declares_durable_review_storage_and_secrets(self):
        root = Path(__file__).resolve().parents[1]
        blueprint = (root / "render.yaml").read_text(encoding="utf-8")
        script = (root / "static" / "script.js").read_text(encoding="utf-8")

        self.assertIn("mountPath: /var/data", blueprint)
        self.assertIn("value: /var/data/verifications.json", blueprint)
        self.assertIn("key: REPORTER_ID_SECRET", blueprint)
        self.assertIn("window.verifyStudy = async (pmid, btn)", script)
        self.assertIn("window.excludeStudy = async (pmid, btn)", script)
        self.assertNotIn("outcome, subcategory", script)

    def test_missing_or_tampered_reporter_cookie_is_rejected(self):
        missing = app.test_client().post("/exclude", json=REPORT)
        self.assertEqual(missing.status_code, 428)

        tampered_client = self._client()
        tampered_client.set_cookie(REPORTER_COOKIE_NAME, "A" * 24)
        tampered = tampered_client.post("/exclude", json=REPORT)
        self.assertEqual(tampered.status_code, 428)

    def test_cross_site_review_request_is_rejected_without_cors(self):
        response = self._client().post(
            "/exclude",
            json=REPORT,
            headers={"Origin": "https://malicious.example"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_verify_is_idempotent_and_two_browsers_trigger_review(self):
        first_client = self._client()
        second_client = self._client()

        with tempfile.TemporaryDirectory() as tempdir:
            store = self._store(tempdir)
            with (
                patch("app.VERIFICATIONS_FILE", store),
                patch("app.find_review_study", return_value=STUDY),
                patch("app.send_developer_notification", return_value=(True, None)) as notify,
            ):
                first = first_client.post("/verify", json=REPORT)
                duplicate = first_client.post("/verify", json=REPORT)
                second = second_client.post("/verify", json=REPORT)
                persisted = json.loads(Path(store).read_text(encoding="utf-8"))

        self.assertEqual(first.get_json()["count"], 1)
        self.assertEqual(duplicate.get_json()["count"], 1)
        self.assertEqual(second.get_json()["count"], 2)
        self.assertTrue(second.get_json()["notification_sent"])
        self.assertFalse(second.get_json()["results_changed"])
        saved = persisted["123"]["contexts"]["bladder_cancer_tomato_incidence"]
        self.assertEqual(saved["submissions"][0]["data"]["Effect Size"], 1.10)
        notify.assert_called_once()

    def test_exclusion_flags_are_idempotent_and_persist_bladder_context(self):
        first_client = self._client()
        second_client = self._client()

        with tempfile.TemporaryDirectory() as tempdir:
            store = self._store(tempdir)
            with (
                patch("app.VERIFICATIONS_FILE", store),
                patch("app.find_review_study", return_value=STUDY),
                patch("app.send_developer_notification", return_value=(True, None)) as notify,
            ):
                first = first_client.post("/exclude", json=REPORT)
                duplicate = first_client.post("/exclude", json=REPORT)
                second = second_client.post("/exclude", json=REPORT)

        self.assertEqual(first.get_json()["context_key"], "bladder_cancer_tomato_incidence")
        self.assertEqual(first.get_json()["exclusions"], 1)
        self.assertTrue(duplicate.get_json()["already_flagged"])
        self.assertEqual(duplicate.get_json()["exclusions"], 1)
        self.assertEqual(second.get_json()["exclusions"], 2)
        self.assertTrue(second.get_json()["notification_sent"])
        self.assertFalse(second.get_json()["results_changed"])
        notify.assert_called_once()

    def test_transport_details_are_not_returned_to_browser(self):
        first_client = self._client()
        second_client = self._client()
        with tempfile.TemporaryDirectory() as tempdir:
            store = self._store(tempdir)
            with (
                patch("app.VERIFICATIONS_FILE", store),
                patch("app.find_review_study", return_value=STUDY),
                patch(
                    "app.send_developer_notification",
                    return_value=(False, "SMTP account sender@example.test rejected"),
                ),
            ):
                first_client.post("/exclude", json=REPORT)
                response = second_client.post("/exclude", json=REPORT)

        payload = response.get_json()
        self.assertEqual(payload["notification_error_code"], "email_unavailable")
        self.assertNotIn("notification_error", payload)
        self.assertNotIn("sender@example.test", response.get_data(as_text=True))

    def test_corrupt_review_store_returns_server_error(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = Path(tempdir) / "verifications.json"
            store.write_text("{bad json", encoding="utf-8")
            with (
                patch("app.VERIFICATIONS_FILE", str(store)),
                patch("app.find_review_study", return_value=STUDY),
            ):
                response = self._client().post("/exclude", json=REPORT)

        self.assertEqual(response.status_code, 500)

    def test_smtp_partial_recipient_rejection_is_not_marked_sent(self):
        class RejectingSMTP:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def starttls(self):
                pass

            def login(self, *_args):
                pass

            def send_message(self, _message):
                return {"shiyushu2006@gmail.com": (550, b"rejected")}

        with (
            patch.dict(
                "os.environ",
                {
                    "SMTP_HOST": "smtp.example.test",
                    "SMTP_FROM": "sender@example.test",
                    "SMTP_USERNAME": "sender@example.test",
                    "SMTP_PASSWORD": "password",
                },
                clear=False,
            ),
            patch("app.smtplib.SMTP", RejectingSMTP),
        ):
            sent, error = send_developer_notification(
                "exclusion_flags", "Tomato", "Bladder cancer", "Incidence", "123"
            )

        self.assertFalse(sent)
        self.assertIn("rejected 1 review recipient", error)


if __name__ == "__main__":
    unittest.main()
