"""Security and durability primitives for the isolated parallel-review track."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable

from dotenv import dotenv_values


CORNELL_GATEWAY_HOST = "api.ai.it.cornell.edu"
CORNELL_GATEWAY_BASE_PATHS = frozenset({"", "/", "/v1", "/v1/"})
GATEWAY_KEY_NAMES = ("CORNELL_API_KEY", "Cornell_API_Key")
GATEWAY_BASE_URL_NAMES = (
    "CORNELL_API_BASE_URL",
    "Cornell_API_Base_URL",
    "API_Base_URI",
)
NCBI_EMAIL_NAMES = ("NCBI_EMAIL", "PUBMED_EMAIL")
NCBI_KEY_NAMES = ("NCBI_API_KEY", "PUBMED_API_KEY")
ALLOWED_CREDENTIAL_NAMES = frozenset(
    GATEWAY_KEY_NAMES + GATEWAY_BASE_URL_NAMES + NCBI_EMAIL_NAMES + NCBI_KEY_NAMES
)

_REDACTION_MARKER = "[REDACTED]"
_SENSITIVE_LABEL = (
    r"(?:token|x-api-key|[A-Za-z0-9_.-]+cookie|"
    r"[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|auth[_-]?token|session[_-]?token|"
    r"client[_-]?secret|secret|password|passwd|passphrase)"
    r"[A-Za-z0-9_.-]*)"
)
_QUOTED_SECRET = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])([\"']?{_SENSITIVE_LABEL}[\"']?\s*[:=]\s*)"
    r"([\"'])([^\"'\r\n]*)([\"'])"
)
_UNQUOTED_SECRET = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])([\"']?{_SENSITIVE_LABEL}[\"']?\s*[:=]\s*)"
    r"(?![\"'])(\[REDACTED\]|[^\s,;}}\]\)&]+)"
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"([\"']?(?:authorization|proxy[-_ ]authorization)[\"']?\s*[:=]\s*)"
    r"(?:\[REDACTED\]|\"[^\"\r\n]*\"|'[^'\r\n]*'|"
    r"(?:bearer|basic)\s+[^\s,;}}\]]+|[^\s,;}}\]]+)"
)
_BEARER_CREDENTIAL = re.compile(
    r"(?i)(\b(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]+)"
)
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"(?:-----END [^-\r\n]*PRIVATE KEY-----|$)",
    re.DOTALL,
)
_COOKIE_CREDENTIAL = re.compile(
    r"(?im)([\"']?(?:set[-_ ]?cookie|cookie)[\"']?\s*[:=]\s*)"
    r"(?:\[REDACTED\]|[^\r\n]*)"
)


def durable_atomic_write(
    path: Path, writer: Callable[[Any], None], *, binary: bool
) -> None:
    """Publish one complete same-directory file without predictable temp names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        os.fchmod(descriptor, 0o644)
        handle = (
            os.fdopen(descriptor, "wb")
            if binary
            else os.fdopen(descriptor, "w", encoding="utf-8")
        )
        descriptor_open = False
        with handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
            finally:
                os.close(directory_descriptor)
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: Any, *, compact: bool = False) -> None:
    def write(handle: Any) -> None:
        if compact:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    durable_atomic_write(path, write, binary=False)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    durable_atomic_write(path, lambda handle: handle.write(payload), binary=True)


def atomic_write_text(path: Path, payload: str) -> None:
    durable_atomic_write(path, lambda handle: handle.write(payload), binary=False)


def redact_error(error: BaseException | str) -> str:
    """Return a bounded error string with common credentials removed."""
    value = str(error)
    value = _PRIVATE_KEY.sub(_REDACTION_MARKER, value)
    value = _COOKIE_CREDENTIAL.sub(rf"\1{_REDACTION_MARKER}", value)
    value = _AUTHORIZATION_VALUE.sub(rf"\1{_REDACTION_MARKER}", value)
    value = _BEARER_CREDENTIAL.sub(rf"\1{_REDACTION_MARKER}", value)
    value = _QUOTED_SECRET.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}"
            f"{_REDACTION_MARKER}{match.group(4)}"
        ),
        value,
    )
    value = _UNQUOTED_SECRET.sub(rf"\1{_REDACTION_MARKER}", value)
    value = _KNOWN_TOKEN.sub(_REDACTION_MARKER, value)
    return re.sub(r"\s+", " ", value).strip()[:1000]


def authorized_credential_file(path: Path, label: str) -> Path:
    """Validate the exact lexical credential path without following a symlink."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"Authorized {label} credential file is not readable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"Authorized {label} credential file must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"Authorized {label} credential path is not a regular file")
    return path.absolute()


def read_allowed_credentials(
    path: Path,
    *,
    label: str,
    names: Iterable[str] = ALLOWED_CREDENTIAL_NAMES,
) -> dict[str, str]:
    """Read only allowlisted values in place; never export them to os.environ."""
    requested = frozenset(names)
    if not requested <= ALLOWED_CREDENTIAL_NAMES:
        raise RuntimeError("Credential loader requested a non-allowlisted name")
    credential_path = authorized_credential_file(path, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(credential_path, flags)
    except OSError as exc:
        raise RuntimeError("Authorized credential file could not be opened safely") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"Authorized {label} credential path is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            parsed = dotenv_values(stream=handle, interpolate=False)
        return {
            name: str(parsed[name]).strip()
            for name in requested
            if parsed.get(name) is not None and str(parsed[name]).strip()
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if "parsed" in locals():
            parsed.clear()


def first_credential(values: dict[str, str], names: Iterable[str]) -> str:
    return next(
        (str(values.get(name) or "").strip() for name in names if str(values.get(name) or "").strip()),
        "",
    )


def validated_cornell_base_url(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("Cornell gateway base URL is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or str(parsed.hostname or "").casefold() != CORNELL_GATEWAY_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path not in CORNELL_GATEWAY_BASE_PATHS
    ):
        raise RuntimeError(
            "Cornell gateway base URL must use the approved HTTPS host and base path"
        )
    normalized_path = "/v1" if parsed.path.rstrip("/") == "/v1" else ""
    return f"https://{CORNELL_GATEWAY_HOST}{normalized_path}"


@contextlib.contextmanager
def configured_entrez(module: Any, path: Path, *, label: str, tool: str):
    """Scope NCBI credentials to Biopython state without environment mutation."""
    values = read_allowed_credentials(
        path, label=label, names=NCBI_EMAIL_NAMES + NCBI_KEY_NAMES
    )
    email = first_credential(values, NCBI_EMAIL_NAMES)
    requested_key = first_credential(values, NCBI_KEY_NAMES)
    if not email:
        raise RuntimeError(f"NCBI email is not configured in authorized {label} credentials")
    entrez = module.Entrez
    lock = getattr(module, "_ENTREZ_CONFIGURATION_LOCK")
    with lock:
        previous = (
            getattr(entrez, "email", None),
            getattr(entrez, "api_key", None),
            getattr(entrez, "tool", None),
            getattr(entrez, "max_tries", None),
        )
        rejected_digest = getattr(module, "_ENTREZ_REJECTED_KEY_DIGEST", None)
        if (
            requested_key
            and rejected_digest
            and hashlib.sha256(requested_key.encode("utf-8")).hexdigest()
            == rejected_digest
        ):
            requested_key = ""
        entrez.email = email
        entrez.api_key = requested_key or None
        entrez.tool = tool
        entrez.max_tries = 1
        try:
            yield
        finally:
            entrez.email, entrez.api_key, entrez.tool, entrez.max_tries = previous
            values.clear()


def audit_cache_receipt_acceptance(output_dir: Path) -> dict[str, int]:
    """Fail closed if a model cache and its durable receipt disagree."""
    receipt_path = output_dir / "gateway_receipts.json"
    packet = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {"receipts": []}
    receipts: dict[str, dict[str, Any]] = {}
    for row in packet.get("receipts") or []:
        if not isinstance(row, dict) or not str(row.get("receipt_id") or ""):
            raise RuntimeError("Gateway receipt ledger contains a malformed row")
        receipt_id = str(row["receipt_id"])
        if receipt_id in receipts:
            raise RuntimeError("Gateway receipt ledger contains a duplicate receipt_id")
        receipts[receipt_id] = row
    cache_root = output_dir / "model_response_cache"
    caches = sorted(cache_root.glob("*/*.json")) if cache_root.exists() else []
    seen: set[str] = set()
    for path in caches:
        if path.is_symlink():
            raise RuntimeError(f"Model response cache must not be a symlink: {path}")
        record = json.loads(path.read_text(encoding="utf-8"))
        receipt_id = str(record.get("receipt_id") or "")
        receipt = receipts.get(receipt_id)
        if receipt is None or receipt_id in seen:
            raise RuntimeError(f"Model cache has no unique matching receipt: {path}")
        seen.add(receipt_id)
        for field in (
            "stage", "model_requested", "work_unit_key", "attempt",
            "context_ids", "input_digest", "output_sha256", "response_cost_usd",
        ):
            if record.get(field) != receipt.get(field):
                raise RuntimeError(f"Model cache/receipt identity differs for {field}: {path}")
        if record.get("validation_status") != receipt.get("validation_status"):
            raise RuntimeError(f"Model cache/receipt validation status differs: {path}")
        cache_accepted = record.get("scientific_result_accepted")
        receipt_accepted = receipt.get("scientific_result_accepted")
        if not isinstance(cache_accepted, bool) or cache_accepted != receipt_accepted:
            raise RuntimeError(f"Model cache/receipt acceptance differs: {path}")
        if cache_accepted is not (record.get("validation_status") == "valid"):
            raise RuntimeError(f"Model cache acceptance contradicts validation status: {path}")
        if receipt_accepted is not (receipt.get("validation_status") == "valid"):
            raise RuntimeError(f"Gateway receipt acceptance contradicts validation status: {path}")
        output = record.get("output_text")
        if isinstance(output, str) and output:
            if hashlib.sha256(output.encode("utf-8")).hexdigest() != record.get("output_sha256"):
                raise RuntimeError(f"Model cache raw-output digest differs: {path}")
        if cache_accepted:
            results = record.get("validated_results")
            expected = sorted(str(value) for value in record.get("context_ids") or [])
            actual = sorted(
                str(row.get("context_id") or "")
                for row in results or []
                if isinstance(row, dict)
            )
            if not isinstance(results, list) or actual != expected or len(actual) != len(results):
                raise RuntimeError(f"Valid model cache context coverage differs: {path}")
    unmatched = set(receipts) - seen
    undocumented = [
        receipt_id
        for receipt_id in unmatched
        if not (
            receipts[receipt_id].get("billing_status")
            == "unknown_after_process_interruption"
            or receipts[receipt_id].get("response_receipt_status")
            == "unknown_after_process_interruption"
            or receipts[receipt_id].get("validation_status")
            == "unknown_after_process_interruption"
        )
    ]
    if undocumented:
        raise RuntimeError(
            "Gateway receipt has no cache and is not an explicitly documented "
            f"unknown-after-process-interruption receipt: {sorted(undocumented)[:3]}"
        )
    return {
        "cache_files": len(caches),
        "matched_receipts": len(seen),
        "unmatched_receipts": len(unmatched),
    }
