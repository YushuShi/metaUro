import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import send_monthly_failure_email as failure_email


class CapturingSMTP:
    messages = []

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

    def send_message(self, message):
        self.messages.append(message)
        return {}


class MonthlyFailureEmailTests(unittest.TestCase):
    def setUp(self):
        CapturingSMTP.messages.clear()

    def test_redacts_assignments_headers_urls_and_known_token_shapes(self):
        raw = """
CHATGPT_API_KEY=chat-secret
"DEEPSEEK_API_KEY": "deep-secret",
NCBI_API_KEY: 'ncbi-secret'
Authorization: Bearer bearer.secret.value
request=https://example.test/?api_key=query-secret&format=json
SMTP_PASSWORD=smtp-secret
standalone sk-abcdefghijklmnop github_pat_abcdefghijklmnop AKIA1234567890ABCDEF
ordinary diagnostic remains visible
"""
        redacted = failure_email.redact_log_text(raw)

        for secret in (
            "chat-secret",
            "deep-secret",
            "ncbi-secret",
            "bearer.secret.value",
            "query-secret",
            "smtp-secret",
            "sk-abcdefghijklmnop",
            "github_pat_abcdefghijklmnop",
            "AKIA1234567890ABCDEF",
        ):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count(failure_email.REDACTION_MARKER), 9)
        self.assertIn("ordinary diagnostic remains visible", redacted)

    def test_email_contains_only_redacted_log_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "monthly.log"
            log_path.write_text(
                "earlier context\nDEEPSEEK_API_KEY=deep-secret\n"
                "Authorization: Bearer bearer-secret\nfinal diagnostic\n",
                encoding="utf-8",
            )
            environment = {
                "SMTP_HOST": "smtp.example.test",
                "SMTP_FROM": "alerts@example.test",
                "SMTP_PORT": "587",
                "SMTP_USE_TLS": "false",
                "SMTP_USE_SSL": "false",
            }
            argv = [
                "send_monthly_failure_email.py",
                "--log",
                str(log_path),
                "--workflow-url",
                "https://github.example/actions/1",
            ]
            with (
                patch.dict("os.environ", environment, clear=True),
                patch.object(failure_email.smtplib, "SMTP", CapturingSMTP),
                patch("sys.argv", argv),
            ):
                self.assertEqual(failure_email.main(), 0)

        self.assertEqual(len(CapturingSMTP.messages), 1)
        body = CapturingSMTP.messages[0].get_content()
        self.assertNotIn("deep-secret", body)
        self.assertNotIn("bearer-secret", body)
        self.assertIn("DEEPSEEK_API_KEY=[REDACTED]", body)
        self.assertIn("Authorization: Bearer [REDACTED]", body)
        self.assertIn("final diagnostic", body)

    def test_redaction_happens_before_tail_is_selected(self):
        secret = "x" * (failure_email.MAX_LOG_CHARACTERS + 100)
        raw = f"CHATGPT_API_KEY={secret}\nfinal diagnostic\n"

        redacted = failure_email.redact_log_text(raw)
        tail = redacted[-failure_email.MAX_LOG_CHARACTERS:]

        self.assertNotIn("x" * 100, tail)
        self.assertIn("CHATGPT_API_KEY=[REDACTED]", tail)
        self.assertIn("final diagnostic", tail)


if __name__ == "__main__":
    unittest.main()
