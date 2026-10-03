"""Bounded NCBI throttling fallback shared by native monthly request loops."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import os
import re
import threading
import time

_THROTTLED_KEYS: set[str] = set()
_LOCK = threading.Lock()
_COOLDOWN_UNTIL = 0.0
MAX_RETRY_AFTER_SECONDS = 60.0


def _chain(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def http_status(error):
    for exc in _chain(error):
        for attr in ('code', 'status', 'status_code'):
            value = getattr(exc, attr, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        match = re.search(r'\b(?:HTTP(?:\s+(?:request\s+)?(?:returned|error))?|status(?:\s+code)?)\D{0,30}([45]\d{2})\b', str(exc), re.I)
        if match:
            return int(match.group(1))
    return None


def is_throttled(api_key):
    if not api_key:
        return False
    digest = hashlib.sha256(str(api_key).strip().encode()).hexdigest()
    with _LOCK:
        return digest in _THROTTLED_KEYS


def configure_key(entrez, api_key):
    """Atomically select a key without ever restoring a throttled credential."""
    key = str(api_key or '').strip() or None
    with _LOCK:
        digest = hashlib.sha256(key.encode()).hexdigest() if key else None
        entrez.api_key = None if digest in _THROTTLED_KEYS else key
        return entrez.api_key


def request_key(entrez):
    """Capture the exact outgoing credential under the fallback latch lock."""
    with _LOCK:
        key = str(entrez.api_key or '').strip() or None
        digest = hashlib.sha256(key.encode()).hexdigest() if key else None
        if digest in _THROTTLED_KEYS:
            entrez.api_key = key = None
        return key


def wait_for_cooldown():
    """All workers honor the latest server hold; sleeping never holds the lock."""
    waited = False
    while True:
        with _LOCK:
            if _COOLDOWN_UNTIL <= 0:
                return waited
            remaining = _COOLDOWN_UNTIL - time.monotonic()
        if remaining <= 0:
            return waited
        waited = True
        time.sleep(remaining)


def retryable_error(error, fallback):
    """Recognize transport envelopes, without promoting deterministic 4xx."""
    status = http_status(error)
    if status is not None:
        return status in {408, 425, 429} or 500 <= status <= 599
    return bool(fallback(error))


def request_interval(standard, *, keyed):
    """Reserve headroom when multiple monthly repositories share one key."""
    monthly = os.environ.get('MONTHLY_AUTOMATION_REQUIRE_GATE', '').strip().lower() == 'true'
    return max(float(standard), 1.0) if keyed and monthly else float(standard)


def retry_after_seconds(error):
    for exc in _chain(error):
        headers = getattr(exc, 'headers', None)
        value = headers.get('Retry-After') if headers is not None else None
        if value is None:
            continue
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                stamp = parsedate_to_datetime(str(value))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                seconds = (stamp - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                continue
        if not 0 <= seconds < float('inf'):
            continue
        if seconds > MAX_RETRY_AFTER_SECONDS:
            # Do not violate a longer server hold or silently wait indefinitely.
            raise RuntimeError('NCBI Retry-After exceeds the bounded retry window; resume later')
        return max(1.0, seconds)
    return 2.0


def handle_throttle(error, attempted_api_key, entrez):
    """Back off every 429; a keyed request may then retry without that key."""
    global _COOLDOWN_UNTIL
    if http_status(error) != 429:
        return False
    delay = retry_after_seconds(error)
    with _LOCK:
        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + delay)
    if attempted_api_key:
        digest = hashlib.sha256(str(attempted_api_key).strip().encode()).hexdigest()
        with _LOCK:
            first = digest not in _THROTTLED_KEYS
            _THROTTLED_KEYS.add(digest)
            if entrez.api_key == attempted_api_key:
                entrez.api_key = None
        if first:
            print('WARNING: NCBI key throughput was throttled (HTTP 429); '
                  'backing off before using the slower public endpoint for this process.', flush=True)
    wait_for_cooldown()
    return bool(attempted_api_key)


def validate_key(entrez, *, term, wait, key_error, retryable):
    """Validate the configured key, retrying the same query publicly on 429."""
    original_key = entrez.api_key
    last_error = None
    for attempt in range(4):
        attempted = entrez.api_key
        try:
            wait()
            wait_for_cooldown()
            attempted = request_key(entrez)
            with entrez.esearch(db='pubmed', term=term, retmax=0, api_key=attempted) as handle:
                entrez.read(handle)
            return 'throttled_http_429' if is_throttled(original_key) else 'working'
        except Exception as exc:
            last_error = exc
            status = http_status(exc)
            if status in {400, 429}:
                changed_key = key_error(exc, attempted)
                if status == 400 and changed_key:
                    return 'rejected_http_400'
                if status == 429 and changed_key:
                    continue
            if retryable_error(exc, retryable) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            break
    raise RuntimeError('NCBI key/public-endpoint validation failed after bounded retries') from last_error
