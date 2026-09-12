#!/usr/bin/env python3
"""Send a concise GitHub Actions failure alert for the monthly update."""

from __future__ import annotations

import argparse
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path


RECIPIENTS = ("margauxdelporte@gmail.com", "shiyushu2006@gmail.com")
MAX_LOG_CHARACTERS = 16000
REDACTION_MARKER = "[REDACTED]"

_SENSITIVE_LABEL = (
    r"(?:x-api-key|[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|auth[_-]?token|client[_-]?secret|secret|password)"
    r"[A-Za-z0-9_.-]*)"
)
_QUOTED_SECRET = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])([\"']?{_SENSITIVE_LABEL}[\"']?\s*[:=]\s*)"
    r"([\"'])([^\"'\r\n]*)([\"'])"
)
_UNQUOTED_SECRET = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])([\"']?{_SENSITIVE_LABEL}[\"']?\s*[:=]\s*)"
    r"(?![\"'])([^\s,;}}\]\)&]+)"
)
_AUTH_CREDENTIAL = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization)\s*:\s*"
    r"(?:bearer|basic)\s+)([^\s,;]+)"
)
_BEARER_CREDENTIAL = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/=-]+)")
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{16})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_log_text(value: str) -> str:
    """Remove common credential forms before a workflow log leaves CI."""
    value = _PRIVATE_KEY.sub(REDACTION_MARKER, value)
    value = _AUTH_CREDENTIAL.sub(rf"\1{REDACTION_MARKER}", value)
    value = _BEARER_CREDENTIAL.sub(rf"\1{REDACTION_MARKER}", value)
    value = _QUOTED_SECRET.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}"
            f"{REDACTION_MARKER}{match.group(4)}"
        ),
        value,
    )
    value = _UNQUOTED_SECRET.sub(rf"\1{REDACTION_MARKER}", value)
    return _KNOWN_TOKEN.sub(REDACTION_MARKER, value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path)
    parser.add_argument("--workflow-url", default="Not available")
    parser.add_argument("--run-label", default="Monthly MetaPee evidence update")
    args = parser.parse_args()

    smtp_host = os.getenv("SMTP_HOST")
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM") or smtp_username
    if not smtp_host or not smtp_from:
        raise RuntimeError("SMTP_HOST and SMTP_FROM (or SMTP_USERNAME) are required for failure alerts")
    try:
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be an integer") from exc

    log_text = "No workflow log was available."
    if args.log and args.log.exists():
        raw_log = args.log.read_text(encoding="utf-8", errors="replace")
        log_text = redact_log_text(raw_log)[-MAX_LOG_CHARACTERS:]

    message = EmailMessage()
    message["Subject"] = "MetaPee monthly update failed"
    message["From"] = smtp_from
    message["To"] = ", ".join(RECIPIENTS)
    message.set_content(
        f"{args.run_label} failed and requires developer attention.\n\n"
        f"GitHub Actions run: {args.workflow_url}\n\n"
        "The final section of the workflow log follows. It should identify issues such as "
        "an invalid or expired NCBI/model credential, NCBI/PMC retrieval failure, "
        "ChatGPT screening failure, audit validation error, or Git push problem.\n\n"
        "----- LOG TAIL -----\n"
        f"{log_text}\n"
    )

    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(smtp_host, smtp_port, timeout=30) as server:
        if use_tls and not use_ssl:
            server.starttls()
        if smtp_username and smtp_password:
            server.login(smtp_username, smtp_password)
        server.send_message(message)
    print(f"Failure alert sent to {', '.join(RECIPIENTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
