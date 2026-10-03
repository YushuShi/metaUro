from scripts import dietary_only_policy as dietary_policy
from flask import has_request_context, Flask, render_template, request, jsonify, send_from_directory, redirect
from contextlib import contextmanager
import meta_analysis
import os
import pandas as pd
import numpy as np
import json
import hashlib
import re
import secrets
import smtplib
import tempfile
import threading
import time
from email.message import EmailMessage
from functools import partial
from datetime import datetime
from itsdangerous import BadSignature, URLSafeSerializer
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None

print = partial(print, flush=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, 'metavir_runtime.log')

def log_event(message):
    """Write progress to stdout and the project refresh log."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"

    # Hide verbose cache lookup prints from console unless DEBUG=true
    debug_enabled = os.environ.get('DEBUG', 'false').lower() == 'true'
    is_verbose = "Checking cache candidate:" in message or "Cache hit found:" in message
    if not is_verbose or debug_enabled:
        print(line)

    # Test clients should never mutate the tracked runtime audit log.
    flask_app = globals().get('app')
    if flask_app is not None and flask_app.testing:
        return

    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except Exception:
        pass

# Define absolute paths for templates and static files based on current directory
try:
    TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
    STATIC_DIR = os.path.join(BASE_DIR, 'static')
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)

    app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
except Exception as e:
    # Fallback if abspath fails (rare)
    app = Flask(__name__)

CACHE_DIR = os.path.join(BASE_DIR, 'combined')
CHATGPT_CACHE_DIR = os.path.join(BASE_DIR, 'Cached_results')
DATA_DIR = os.path.join(BASE_DIR, 'data')
PLOT_DIR = os.path.join(BASE_DIR, 'Plot')
SUMMARY_MANIFEST_FILE = os.path.join(PLOT_DIR, 'summary_manifest.json')


def subexposure_mode():
    from scripts.subexposure_policy import include_mode
    if not has_request_context(): return 'yes'
    payload=request.get_json(silent=True) or {}
    return 'yes' if include_mode(payload.get('include_subexposures', request.args.get('include_subexposures'))) else 'no'


def active_cache_dir():
    mode_root=os.path.join(BASE_DIR,'Subexposure_results',subexposure_mode())
    # Temporary fixture/alternate roots remain independent of production projections.
    original=os.path.join(BASE_DIR,'combined')
    return mode_root if os.path.abspath(CACHE_DIR)==os.path.abspath(original) and os.path.isdir(mode_root) else CACHE_DIR


def active_plot_dir():
    mode_root=os.path.join(PLOT_DIR,'subexposures',subexposure_mode())
    return mode_root if os.path.isdir(mode_root) else PLOT_DIR


def active_summary_manifest():
    return os.path.join(active_plot_dir(),'summary_manifest.json')


@app.before_request
def validate_subexposure_mode():
    try: subexposure_mode()
    except ValueError as exc: return jsonify({'error':str(exc)}),400


@app.context_processor
def subexposure_template_context():
    return {'include_subexposures':subexposure_mode()}
BUNDLED_VERIFICATIONS_FILE = os.path.join(DATA_DIR, 'verifications.json')
# Production deployments should point this at durable storage (for example a
# mounted Render disk). The bundled JSON remains the local-development default
# and the seed for an empty external store.
VERIFICATIONS_FILE = os.environ.get('VERIFICATIONS_FILE', BUNDLED_VERIFICATIONS_FILE)

REPORTER_COOKIE_NAME = "metavir_reporter"
REPORTER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2
REPORTER_ID_SECRET = os.environ.get("REPORTER_ID_SECRET") or secrets.token_hex(32)
REPORTER_ID_SECRET_IS_EPHEMERAL = not bool(os.environ.get("REPORTER_ID_SECRET"))
_REPORTER_SERIALIZER = URLSafeSerializer(
    REPORTER_ID_SECRET, salt="metavir-anonymous-reviewer-v1"
)
NOTIFICATION_RETRY_SECONDS = max(
    1, int(os.environ.get("NOTIFICATION_RETRY_SECONDS", "300"))
)
_VERIFICATIONS_THREAD_LOCK = threading.RLock()

DEFAULT_DISEASE = 'Prostate cancer'
AGGRESSIVE_DISEASE = 'Aggressive prostate cancer'
AGGRESSIVE_SUBCATEGORY = 'aggressive_prostate_cancer'

SUMMARY_PLOT_NAMES = {
    'forest-protective',
    'forest-harmful',
    'forest-protective-dietary',
    'forest-harmful-dietary',
    'effect-heterogeneity',
    'egger-heterogeneity',
}
SUMMARY_PLOT_LANGUAGES = frozenset({'zh-CN', 'zh-TW', 'nl', 'ko'})


def _localized_summary_plot_path(filename, language):
    """Resolve a generated locale variant without accepting path input."""
    if language not in SUMMARY_PLOT_LANGUAGES:
        return filename
    return os.path.join('locales', language, os.path.basename(filename))

def get_scope_config():
    """Return the supported prostate endpoints and their display hierarchy."""
    return {
        'prostate': {
            'key': 'prostate',
            'site_id': 'prostate',
            'label': DEFAULT_DISEASE,
            'disease': DEFAULT_DISEASE,
            'lifetime_risk_percent': 13.2,
            'subcategories': [{
                'key': AGGRESSIVE_SUBCATEGORY,
                'label': AGGRESSIVE_DISEASE,
                'disease': DEFAULT_DISEASE,
                'lifetime_risk_percent': None,
            }],
        }
    }


def get_major_scope_key(disease, scopes=None):
    scopes = scopes or get_scope_config()
    requested = str(disease or '').strip().lower()
    for key, scope in scopes.items():
        if requested in {key.lower(), scope['label'].lower(), scope['disease'].lower()}:
            return key
    return None


def resolve_disease_scope(disease=DEFAULT_DISEASE, subcategory=None):
    """Resolve only supported endpoints, never dropping a requested subtype."""
    requested = str(disease or DEFAULT_DISEASE).strip().casefold()
    subtype = str(subcategory or '').strip().casefold()
    aggressive_names = {AGGRESSIVE_DISEASE.casefold(), AGGRESSIVE_SUBCATEGORY}
    if requested in aggressive_names:
        if subtype and subtype not in aggressive_names:
            raise ValueError('This cancer subcategory is not supported in MetaVir.')
        return AGGRESSIVE_DISEASE
    if not get_major_scope_key(disease) and requested != 'prostate_cancer':
        raise ValueError('MetaVir supports prostate cancer only.')
    if not subtype:
        return DEFAULT_DISEASE
    if subtype in aggressive_names:
        return AGGRESSIVE_DISEASE
    raise ValueError('This cancer subcategory is not supported in MetaVir.')


def load_summary_manifest():
    """Load generated cross-exposure plot metadata without trusting paths from requests."""
    manifest = load_json(active_summary_manifest(), {})
    combined_manifest = load_combined_manifest()
    combined_manifest_path = os.path.join(active_cache_dir(), '_merge_manifest.json')
    try:
        with open(combined_manifest_path, 'rb') as handle:
            combined_manifest_digest = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        combined_manifest_digest = None
    if (
        not combined_manifest
        or manifest.get('canonical_track') != 'combined'
        or manifest.get('combined_build_id') != combined_manifest.get('build_id')
        or manifest.get('combined_manifest_sha256') != combined_manifest_digest
        or not isinstance(manifest.get('scopes'), dict)
    ):
        return {'scopes': {}}
    return manifest


def load_combined_manifest():
    """Return the current deterministic combined-build manifest or fail closed."""
    manifest = load_json(os.path.join(active_cache_dir(), '_merge_manifest.json'), {})
    if (
        not isinstance(manifest, dict)
        or manifest.get('schema_version') != 1
        or manifest.get('canonical_track') != 'combined'
        or not str(manifest.get('build_id') or '').strip()
    ):
        return {}
    return manifest


def is_current_combined_cache(payload, manifest=None):
    """Require a cache to belong to the exact combined build served by the UI."""
    if not isinstance(payload, dict):
        return False
    manifest = manifest or load_combined_manifest()
    projection = payload.get('combined_projection')
    return bool(
        manifest
        and payload.get('canonical_track') == 'combined'
        and isinstance(projection, dict)
        and projection.get('build_id') == manifest.get('build_id')
    )


def summary_scope_for_label(label, manifest=None):
    manifest = manifest or load_summary_manifest()
    key = safe_path_component(label)
    scope = manifest.get('scopes', {}).get(key)
    return key, scope if isinstance(scope, dict) else None


DEVELOPER_NOTIFICATION_EMAILS = (
    "yus4011@med.cornell.edu",
    "shiyushu2006@gmail.com",
    "margauxdelporte@gmail.com",
)

CONSENSUS_FIELDS = (
    "Effect Size",
    "Lower CI",
    "Upper CI",
    "Cases",
    "Sample Size",
    "exposure_measurement_type",
)

# Model calls are isolated to audited workflows. The public web app is
# intentionally cache-only; the main UI and summary serve combined evidence.
READ_ONLY_MODE = True
SUPPORTED_EVIDENCE_TRACKS = frozenset({'chatgpt', 'combined'})


def normalize_evidence_track(value):
    """Return a supported UI evidence track or reject untrusted input."""
    track = str(value or 'combined').strip().casefold()
    if track not in SUPPORTED_EVIDENCE_TRACKS:
        raise ValueError("Evidence track must be 'chatgpt' or 'combined'.")
    return track


def evidence_cache_dir(track):
    """Map a validated evidence track to its read-only cache root."""
    return CHATGPT_CACHE_DIR if track == 'chatgpt' else active_cache_dir()


def project_chatgpt_subexposures(result, exposure, filename):
    """Project saved ChatGPT rows at read time, without using combined caches."""
    from scripts import subexposure_policy as policy
    hierarchy = load_json(os.path.join(DATA_DIR, 'exposure_hierarchy.json'), {})
    sources = {exposure: result.get('studies', [])}
    if subexposure_mode() == 'yes':
        for child in policy.descendants(exposure, hierarchy):
            path = os.path.join(CHATGPT_CACHE_DIR, policy.key(child), filename)
            payload = load_json(path, {})
            if is_cache_for_evidence_track(payload, 'chatgpt'):
                sources[child] = payload.get('studies', [])
    rows = policy.project_rows(sources, exposure, hierarchy, include=subexposure_mode())
    for row in rows:
        if row['subexposure_inherited']:
            row['Hierarchy Inherited'] = True
            if row['subexposure_pooling_selected']:
                row['pool_in_parent_meta_analysis'] = True
    result['studies'] = rows
    result['include_subexposures'] = subexposure_mode()
    result['subexposure_projection'] = {
        'mode': subexposure_mode(), 'source_track': 'chatgpt',
        'table_rows': len(rows), 'policy': 'transitive-child-closure-distinct-populations-v1'}
    return result


def is_cache_for_evidence_track(payload, track, combined_manifest=None):
    """Fail closed when a cache does not belong to the requested UI track."""
    if track == 'combined':
        return is_current_combined_cache(payload, combined_manifest)
    return bool(
        isinstance(payload, dict)
        and payload.get('canonical_track') == 'chatgpt'
        and not payload.get('combined_projection')
    )

def _safe_email_value(value, fallback="Not available"):
    """Return a one-line value that is safe to place in an email body."""
    text = re.sub(r"[\r\n]+", " ", str(value or "")).strip()
    return text or fallback

def first_author_last_name(study_data):
    """Extract the first author's last name from a saved study record."""
    study_data = study_data or {}
    authors = _safe_email_value(study_data.get("Authors"), fallback="")
    if authors:
        first_author = authors.split(",", 1)[0].strip()
        last_name = re.sub(r"(?:\s+[A-Z][A-Z.'-]*)+$", "", first_author).strip()
        if last_name:
            return last_name

    study_label = _safe_email_value(study_data.get("Study"), fallback="")
    if study_label:
        author_part = re.split(r"\s+et\s+al\.?|\s+\(\d{4}\)", study_label, maxsplit=1, flags=re.IGNORECASE)[0]
        last_name = re.sub(r"(?:\s+[A-Z][A-Z.'-]*)+$", "", author_part).strip()
        if last_name:
            return last_name
    return "Not available"

def _normalise_consensus_value(value):
    if value is None:
        return "none"
    text = str(value).strip().lower()
    return "none" if text in {"", "null", "none", "nan"} else text

def matching_submission_signature(submissions):
    """Return a stable signature when two distinct reporters submit a match."""
    seen = {}
    for submission in submissions or []:
        values = tuple(
            _normalise_consensus_value((submission.get("data") or {}).get(field))
            for field in CONSENSUS_FIELDS
        )
        reporter_hash = submission.get("reporter_hash")
        if not reporter_hash:
            continue
        if values in seen and seen[values] != reporter_hash:
            payload = json.dumps(dict(zip(CONSENSUS_FIELDS, values)), sort_keys=True)
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
        seen[values] = reporter_hash
    return None


def build_context_key(disease, exposure, outcome, evidence_track='combined'):
    """Build the one canonical key used by reads and crowdsourced writes."""
    canonical_exp = (
        meta_analysis.get_canonical_name(exposure)
        if exposure else "unknown_exposure"
    )
    parts = [disease or DEFAULT_DISEASE, canonical_exp, outcome or "incidence"]
    if evidence_track != 'combined':
        parts.append(normalize_evidence_track(evidence_track))
    return "_".join(str(part).strip() for part in parts).lower().replace(" ", "_")


def _decode_reporter_cookie():
    """Return the server-signed anonymous browser ID, or ``None``."""
    signed_value = str(request.cookies.get(REPORTER_COOKIE_NAME) or "").strip()
    if not signed_value:
        return None
    try:
        payload = _REPORTER_SERIALIZER.loads(signed_value)
    except BadSignature:
        return None
    raw_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(raw_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", raw_id):
        return None
    return raw_id


def _new_reporter_cookie():
    raw_id = secrets.token_urlsafe(24)
    return raw_id, _REPORTER_SERIALIZER.dumps({"id": raw_id})


def _reporter_identity():
    """Return a stable signed-browser identity, rejecting missing cookies."""
    raw_id = _decode_reporter_cookie()
    if raw_id is None:
        return None
    return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()


def _set_reporter_cookie(response, signed_value):
    force_secure = os.environ.get("REPORTER_COOKIE_SECURE", "false").lower() == "true"
    response.set_cookie(
        REPORTER_COOKIE_NAME,
        signed_value,
        max_age=REPORTER_COOKIE_MAX_AGE,
        httponly=True,
        secure=force_secure or request.is_secure,
        samesite="Lax",
    )
    return response


def _json_response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    return response


def _review_request_context(data):
    """Resolve untrusted review input to the same canonical scope as analyze."""
    disease = resolve_disease_scope(data.get('disease', DEFAULT_DISEASE), data.get('subcategory'))

    exposure = str(data.get("exposure") or "").strip()
    if not exposure or len(exposure) > 160:
        raise ValueError("Invalid exposure")
    outcome = str(data.get("outcome") or "incidence").strip().lower()
    if outcome != "incidence":
        raise ValueError("MetaVir supports prostate cancer incidence only")
    return disease, exposure, "Incidence"


def _review_origin_allowed():
    """Reject browser review mutations initiated by another web origin."""
    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").lower()
    if fetch_site == "cross-site":
        return False
    origin = str(request.headers.get("Origin") or "").strip()
    if not origin:
        return True
    try:
        return urlsplit(origin).netloc.casefold() == request.host.casefold()
    except ValueError:
        return False

def find_saved_study(pmid, exposure=None, evidence_track='combined'):
    """Find local author metadata when the browser did not send a study record."""
    evidence_track = normalize_evidence_track(evidence_track)
    combined_manifest = load_combined_manifest() if evidence_track == 'combined' else None
    if evidence_track == 'combined' and not combined_manifest:
        return {}
    cache_dir = evidence_cache_dir(evidence_track)
    safe_exposure = safe_path_component(meta_analysis.get_canonical_name(exposure)) if exposure else None
    search_root = os.path.join(cache_dir, safe_exposure) if safe_exposure else cache_dir
    if not os.path.isdir(search_root):
        return {}
    for root, _, files in os.walk(search_root):
        for filename in files:
            if not filename.endswith(".json"):
                continue
            cached = load_json(os.path.join(root, filename), {})
            if not is_cache_for_evidence_track(
                cached, evidence_track, combined_manifest
            ):
                continue
            for study in cached.get("studies", []) if isinstance(cached, dict) else []:
                if str(study.get("PMID")) == str(pmid):
                    return study
    return {}


def find_review_study(
    pmid, disease, exposure, outcome, evidence_track='combined'
):
    """Resolve a review target from the exact saved UI context."""
    evidence_track = normalize_evidence_track(evidence_track)
    combined_manifest = load_combined_manifest() if evidence_track == 'combined' else None
    if evidence_track == 'combined' and not combined_manifest:
        return {}
    cache_dir = evidence_cache_dir(evidence_track)
    candidates = (
        get_cache_path(disease, exposure, outcome, True, False, cache_dir=cache_dir),
        get_cache_path(disease, exposure, outcome, True, True, cache_dir=cache_dir),
    )
    for cache_path in candidates:
        payload = load_json(cache_path, {})
        if not is_cache_for_evidence_track(
            payload, evidence_track, combined_manifest
        ):
            continue
        for study in payload.get("studies", []) if isinstance(payload, dict) else []:
            if str(study.get("PMID")) == str(pmid):
                return study
    return {}

def send_developer_notification(event_type, exposure, disease, outcome, pmid, study_data=None):
    """Email developers about a crowdsourced review trigger without changing results."""
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_username = os.environ.get("SMTP_USERNAME")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    smtp_from = os.environ.get("SMTP_FROM") or smtp_username
    use_ssl = os.environ.get("SMTP_USE_SSL", "false").lower() == "true"
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
    safe_pmid = _safe_email_value(pmid)

    if not smtp_host or not smtp_from:
        message = "SMTP_HOST and SMTP_FROM (or SMTP_USERNAME) must be configured"
        log_event(f"[MetaVir] Developer notification not sent for PMID {safe_pmid}: {message}")
        return False, message
    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        message = "SMTP_PORT must be an integer"
        log_event(f"[MetaVir] Developer notification not sent for PMID {safe_pmid}: {message}")
        return False, message

    event_label = {
        "matching_submissions": "Two matching crowdsourced submissions",
        "exclusion_flags": "Two or more crowdsourced exclusion flags",
    }.get(event_type, "Crowdsourced review trigger")
    author_last_name = first_author_last_name(study_data)

    email = EmailMessage()
    email["Subject"] = f"MetaVir review requested: PMID {safe_pmid}"
    email["From"] = smtp_from
    email["To"] = ", ".join(DEVELOPER_NOTIFICATION_EMAILS)
    email.set_content(
        "A crowdsourced report requires developer review. Results were not changed.\n\n"
        f"Trigger: {event_label}\n"
        f"Exposure: {_safe_email_value(exposure)}\n"
        f"Disease: {_safe_email_value(disease)}\n"
        f"Outcome: {_safe_email_value(outcome)}\n"
        f"PMID: {safe_pmid}\n"
        f"First author last name: {_safe_email_value(author_last_name)}\n"
        f"PubMed: https://pubmed.ncbi.nlm.nih.gov/{safe_pmid}/\n"
    )

    try:
        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_class(smtp_host, smtp_port, timeout=15) as server:
            if use_tls and not use_ssl:
                server.starttls()
            if smtp_username and smtp_password:
                server.login(smtp_username, smtp_password)
            refused = server.send_message(email)
            if refused:
                raise RuntimeError(f"SMTP rejected {len(refused)} review recipient(s)")
        log_event(f"[MetaVir] Developer notification sent for PMID {safe_pmid}: {event_type}")
        return True, None
    except Exception as exc:
        log_event(f"[MetaVir] Developer notification failed for PMID {safe_pmid}: {exc}")
        return False, str(exc)

def safe_path_component(value):
    """Return a stable filesystem-safe path component for cache names."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", str(value).lower())).strip("_")

def get_cache_path(
    disease, exposure, outcome, exclude_meta, use_downstream=False, cache_dir=None
):
    """Construct a file path for the specific analysis cache and resolve synonyms."""
    # Resolve canonical name first to avoid DHEA vs dehydroepiandrosterone duplicate caches
    canonical_exposure = meta_analysis.get_canonical_name(exposure)
    safe_exposure = safe_path_component(canonical_exposure)

    downstream_tag = "all" if use_downstream else "core"
    safe_analysis = safe_path_component(
        f"{disease}_{outcome}_{exclude_meta}_{downstream_tag}"
    )

    exposure_dir = os.path.join(cache_dir or active_cache_dir(), safe_exposure)
    return os.path.join(exposure_dir, f"{safe_analysis}.json")

def load_json(filepath, default_val):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading {filepath}: {e}")
            return default_val
    return default_val

def save_json(filepath, data):
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(sanitize_data(data), f, indent=4)
    except Exception as e:
        print(f"Error saving {filepath}: {e}")


def _load_verifications_for_update():
    """Load the writable review store, seeding it from bundled history."""
    source = VERIFICATIONS_FILE
    if not os.path.isfile(source) and source != BUNDLED_VERIFICATIONS_FILE:
        source = BUNDLED_VERIFICATIONS_FILE
    if not os.path.isfile(source):
        return {}
    with open(source, 'r', encoding='utf-8') as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("The verification store must contain a JSON object")
    return payload


def load_verifications():
    try:
        return _load_verifications_for_update()
    except Exception as exc:
        log_event(f"[MetaVir] Could not read verification store: {exc}")
        return {}


def _atomic_save_verifications(data):
    """Durably replace the review JSON or raise instead of claiming success."""
    directory = os.path.dirname(VERIFICATIONS_FILE) or BASE_DIR
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=directory,
            prefix='.verifications-', suffix='.tmp', delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(sanitize_data(data), handle, indent=4)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, VERIFICATIONS_FILE)
        temporary_path = None
        if hasattr(os, 'O_DIRECTORY'):
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


@contextmanager
def locked_verifications():
    """Serialize cross-thread/process review updates and commit atomically."""
    directory = os.path.dirname(VERIFICATIONS_FILE) or BASE_DIR
    os.makedirs(directory, exist_ok=True)
    lock_path = f"{VERIFICATIONS_FILE}.lock"
    with _VERIFICATIONS_THREAD_LOCK:
        with open(lock_path, 'a+', encoding='utf-8') as lock_handle:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                payload = _load_verifications_for_update()
                yield payload
                _atomic_save_verifications(payload)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _verification_entry(verifications, pmid):
    current = verifications.get(pmid)
    if not isinstance(current, dict):
        current = {
            "context_exclusions": {},
            "contexts": {},
        }
        verifications[pmid] = current
    current.setdefault("context_exclusions", {})
    current.setdefault("contexts", {})
    return current


def _verification_context(entry, context_key):
    context = entry["contexts"].setdefault(context_key, {
        "submissions": [],
        "consensus_data": None,
        "notifications": {},
    })
    context.setdefault("submissions", [])
    context.setdefault("notifications", {})
    context["consensus_data"] = None
    return context


def _claim_notification(state, event_id):
    """Persist a delivery claim before SMTP is contacted.

    A fresh pending claim prevents parallel requests from sending the same
    message.  An abandoned claim can be retried after the bounded lease.
    """
    now = time.time()
    if state.get("sent_at"):
        return False
    pending_at = state.get("pending_at_epoch")
    try:
        pending_is_fresh = pending_at is not None and (
            now - float(pending_at) < NOTIFICATION_RETRY_SECONDS
        )
    except (TypeError, ValueError):
        pending_is_fresh = False
    if state.get("pending_event_id") and pending_is_fresh:
        return False
    try:
        if now < float(state.get("next_retry_at_epoch") or 0):
            return False
    except (TypeError, ValueError):
        pass
    state["event_id"] = event_id
    state["pending_event_id"] = event_id
    state["pending_at_epoch"] = now
    state["attempts"] = int(state.get("attempts", 0)) + 1
    return True


def _finish_notification(state, event_id, sent, error=None):
    """Record an SMTP result without persisting sensitive transport errors."""
    if state.get("pending_event_id") != event_id:
        return
    state.pop("pending_event_id", None)
    state.pop("pending_at_epoch", None)
    state["last_attempt_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if sent:
        state["sent_at"] = state["last_attempt_at"]
        state["last_error_code"] = None
        state.pop("next_retry_at_epoch", None)
    else:
        state["last_error_code"] = "delivery_failed"
        state["next_retry_at_epoch"] = time.time() + NOTIFICATION_RETRY_SECONDS
        if error:
            log_event(f"[MetaVir] Notification delivery detail: {error}")

def sanitize_data(data):
    """Recursively replace NaN/Inf values and convert numpy types for JSON compatibility."""
    if isinstance(data, dict):
        return {k: sanitize_data(v) for k, v in data.items()}

    elif isinstance(data, list):
        return [sanitize_data(x) for x in data]
    elif isinstance(data, (np.bool_,)):
        return bool(data)
    elif isinstance(data, (np.integer,)):
        return int(data)
    elif isinstance(data, (np.floating,)):
        val = float(data)
        if np.isnan(val) or np.isinf(val):
            return None
        return val
    elif isinstance(data, float):
        if np.isnan(data) or np.isinf(data):
            return None
    return data

@app.route('/')
def index():
    return render_evidence_index('combined')


@app.route('/chatgpt')
def chatgpt_index():
    return render_evidence_index('chatgpt')


@app.route('/combined')
def combined_index():
    return render_evidence_index('combined')


def render_evidence_index(evidence_track):
    """Render one explicitly labeled evidence view over the shared UI."""
    try:
        disease = resolve_disease_scope(request.args.get('scope', DEFAULT_DISEASE), request.args.get('subcategory'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    response = app.make_response(
        render_template(
            'index.html',
            scope_config=get_scope_config(),
            selected_scope_label=disease,
            selected_scope_key=safe_path_component(disease),
            evidence_track=evidence_track,
        )
    )
    if _decode_reporter_cookie() is None:
        _, signed_value = _new_reporter_cookie()
        _set_reporter_cookie(response, signed_value)
    return response


@app.route('/api/review-readiness')
def review_readiness():
    """Expose non-secret deployment checks for the review workflow."""
    smtp_from = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USERNAME")
    return jsonify({
        "durable_store_configured": os.path.abspath(VERIFICATIONS_FILE) != os.path.abspath(BUNDLED_VERIFICATIONS_FILE),
        "reporter_secret_configured": not REPORTER_ID_SECRET_IS_EPHEMERAL,
        "smtp_configured": bool(os.environ.get("SMTP_HOST") and smtp_from),
        "review_recipient_count": len(DEVELOPER_NOTIFICATION_EMAILS),
    })

@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/study-selection')
def study_selection():
    from study_selection import build_selection_flow, selection_catalog
    try:
        disease = resolve_disease_scope(request.args.get('scope', DEFAULT_DISEASE), request.args.get('subcategory'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    choices = selection_catalog('MetaVir')
    flow = build_selection_flow(disease, request.args.get('exposure'))
    scopes = [{'id': safe_path_component(label), 'label': label} for label in choices]
    return render_template('study_selection.html', flow=flow, selection_scopes=scopes,
                           current={'id': safe_path_component(disease), 'label': disease})


@app.route('/summary')
def summary():
    """Show the current cross-exposure prostate-cancer evidence summary."""
    try:
        disease = resolve_disease_scope(request.args.get('scope', DEFAULT_DISEASE), request.args.get('subcategory'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    manifest = load_summary_manifest()
    scope_key, summary_scope = summary_scope_for_label(disease, manifest)
    plot_version = None
    if os.path.isfile(active_summary_manifest()):
        plot_version = int(os.path.getmtime(active_summary_manifest()))
    return render_template(
        'summary.html',
        selected_scope_key=scope_key,
        selected_scope_label=disease,
        scope_config=get_scope_config(),
        summary_scope=summary_scope,
        eligibility_note=manifest.get('eligibility_note'),
        generated_at=manifest.get('generated_at'),
        plot_version=plot_version,
    )


@app.route('/summary/plots/<scope_key>/<plot_name>')
def summary_plot(scope_key, plot_name):
    """Serve only generated PDF plots declared in the summary manifest."""
    if plot_name not in SUMMARY_PLOT_NAMES:
        return jsonify({"error": "Summary plot not found."}), 404
    scope = load_summary_manifest().get('scopes', {}).get(scope_key, {})
    plot = (scope.get('plots') or {}).get(plot_name, {})
    english_filename = os.path.basename(str(plot.get('filename') or ''))
    if not plot.get('available') or not english_filename:
        return jsonify({"error": "Summary plot is not available yet."}), 404
    language = request.args.get('lang', '').strip()
    localized_filename = _localized_summary_plot_path(english_filename, language)
    declared_locales = plot.get('locales') or {}
    localized_is_declared = (
        language in SUMMARY_PLOT_LANGUAGES
        and declared_locales.get(language) == localized_filename
    )
    filename = (
        localized_filename
        if localized_is_declared and os.path.isfile(os.path.join(active_plot_dir(), localized_filename))
        else english_filename
    )
    if not os.path.isfile(os.path.join(active_plot_dir(), filename)):
        return jsonify({"error": "Summary plot is not available yet."}), 404
    response = send_from_directory(active_plot_dir(), filename, mimetype='application/pdf')
    response.headers['Cache-Control'] = 'no-store, no-cache, max-age=0, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/summary/workbooks/<scope_key>/<dataset>')
def summary_workbook(scope_key, dataset):
    """Download a generated combined or dietary cross-exposure workbook."""
    if dataset not in {'combined', 'dietary'}:
        return jsonify({"error": "Summary workbook not found."}), 404
    scope = load_summary_manifest().get('scopes', {}).get(scope_key, {})
    workbook = (scope.get('workbooks') or {}).get(dataset, {})
    filename = os.path.basename(str(workbook.get('filename') or ''))
    if not workbook.get('available') or not filename or not os.path.isfile(os.path.join(active_plot_dir(), filename)):
        return jsonify({"error": "Summary workbook is not available yet."}), 404
    return send_from_directory(
        active_plot_dir(),
        filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name="subexposures-" + subexposure_mode() + "_" + filename,
    )

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.json or {}
    try:
        evidence_track = normalize_evidence_track(data.get('evidence_track'))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    cache_dir = evidence_cache_dir(evidence_track)
    disease = data.get('disease', DEFAULT_DISEASE)
    exposure = data.get('exposure', 'Coffee')
    outcome = data.get('outcome', 'Incidence')
    exclude_meta = data.get('exclude_meta', False)
    # The UI toggle is removed, so we default to False for downstream terms
    use_downstream = data.get('use_downstream', False)
    force_refresh = data.get('force_refresh', False)
    try:
        disease = resolve_disease_scope(disease, data.get('subcategory'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if str(outcome).strip().casefold() != "incidence":
        return jsonify({"error": "MetaVir supports prostate cancer incidence only."}), 400
    outcome = "Incidence"
    combined_manifest = load_combined_manifest() if evidence_track == 'combined' else None
    if evidence_track == 'combined' and not combined_manifest:
        return jsonify({
            "error": "The adjudicated combined evidence build is unavailable."
        }), 503
    if evidence_track == 'chatgpt' and not os.path.isdir(cache_dir):
        return jsonify({
            "error": "The ChatGPT-only evidence cache is unavailable."
        }), 503

    started_at = time.time()
    log_event(
        f"[MetaVir] Analyze request: disease={disease}, exposure={exposure}, "
        f"outcome={outcome}, exclude_meta={exclude_meta}, "
        f"force_refresh={force_refresh}, canonical_track={evidence_track}"
    )

    best_cache_path = None
    if not force_refresh:
        # Try requested downstream setting first, then fallback to opposite
        for ds_flag in [use_downstream, not use_downstream]:
            p_path = get_cache_path(
                disease, exposure, outcome, exclude_meta, ds_flag,
                cache_dir=cache_dir,
            )
            log_event(f"[MetaVir] Checking cache candidate: {p_path}")
            if os.path.exists(p_path):
                best_cache_path = p_path
                log_event(f"[MetaVir] Cache hit found: {best_cache_path}")
            if best_cache_path:
                break
    else:
        log_event("[MetaVir] Force refresh requested but refresh is workflow-only.")
        return jsonify({"error": "Refreshing evidence is available only through the audited monthly workflow."}), 400

    if not best_cache_path:
        log_event("[MetaVir] No cached result found for request in read-only mode.")
        return jsonify({
            "error": (
                "No saved evidence is available for this exposure yet. "
                "MetaVir displays only results from the audited monthly workflow."
            )
        }), 400

    if best_cache_path:
        log_event(
            f"[MetaVir] Returning {evidence_track} cached results from {best_cache_path}"
        )
        result = load_json(best_cache_path, {})
        if not is_cache_for_evidence_track(
            result, evidence_track, combined_manifest
        ):
            log_event(f"[MetaVir] Rejected non-canonical cache: {best_cache_path}")
            track_label = (
                "the current adjudicated combined screening build"
                if evidence_track == 'combined'
                else "the ChatGPT-only screening build"
            )
            return jsonify({
                "error": f"The saved result is not part of {track_label}."
            }), 409
        if evidence_track == 'chatgpt':
            result = project_chatgpt_subexposures(result, exposure, os.path.basename(best_cache_path))
        result['disease'] = disease
        screening_stats = result.get("screening_stats")
        if not screening_stats and "studies" in result:
            screening_stats = {
                "total_fetched": "N/A",
                "after_prefilter": "N/A",
                "prefilter_skip": {},
                "llm_screened_in": len(result["studies"]),
                "llm_screened_out": 0,
                "consensus_bypassed": 0,
                "extracted": len(result["studies"])
            }
        if screening_stats:
            log_event("\n" + "="*60)
            log_event("[MetaVir] SCREENING & PIPELINE STATISTICS SUMMARY (LOADED FROM CACHE):")
            log_event(f"  - Total Articles Fetched from PubMed: {screening_stats.get('total_fetched', 0)}")
            log_event(f"  - Articles Remaining After Pre-filter: {screening_stats.get('after_prefilter', 0)}")
            skips = screening_stats.get('prefilter_skip', {})
            if skips:
                skips_str = ", ".join([f"{k}={v}" for k, v in skips.items() if v > 0])
                if skips_str:
                    log_event(f"    (Pre-filter skips: {skips_str})")
            log_event(f"  - LLM Screening:")
            log_event(f"    * Screened IN / Accepted:   {screening_stats.get('llm_screened_in', 0)}")
            log_event(f"    * Screened OUT / Rejected:  {screening_stats.get('llm_screened_out', 0)}")
            log_event(f"    * Consensus Bypassed:       {screening_stats.get('consensus_bypassed', 0)}")
            log_event(f"  - Final Extracted Studies: {screening_stats.get('extracted', 0)}")
            log_event("="*60 + "\n")

    # Inject advisory crowdsourcing counts. Reports never alter study data or results.
    review_store_available = True
    try:
        verifications = _load_verifications_for_update()
    except Exception as exc:
        review_store_available = False
        verifications = {}
        log_event(f"[MetaVir] Review status unavailable during analyze: {exc}")
    result['review_store_available'] = review_store_available
    context_key = build_context_key(
        disease, exposure, outcome, evidence_track=evidence_track
    )

    if "studies" in result:
        saved_quality_by_pmid = {}
        for study in result['studies']:
            if study.get('Ethnicity') in (None, '', '-'):
                study['Ethnicity'] = study.get('ethnicity') or 'Mixed'
            if 'exposure_measurement_type' not in study or study['exposure_measurement_type'] in [None, '', '-']:
                study['exposure_measurement_type'] = 'unclear'
            if 'exposure_measurement_supporting_text' not in study or study['exposure_measurement_supporting_text'] is None:
                study['exposure_measurement_supporting_text'] = ''
            if 'extraction_supporting_text' not in study or not isinstance(study.get('extraction_supporting_text'), dict):
                study['extraction_supporting_text'] = {
                    "sample_size": "",
                    "effect_size": "",
                    "effect_direction": "",
                    "p_value": "",
                    "confidence_interval": "",
                    "outcome_definition": "",
                    "exposure_definition": ""
                }
            else:
                est = study['extraction_supporting_text']
                for k in ["sample_size", "effect_size", "effect_direction", "p_value", "confidence_interval", "outcome_definition", "exposure_definition"]:
                    if k not in est or est[k] is None:
                        est[k] = ""
            pmid = str(study.get('PMID'))
            if (
                not isinstance(study.get('JBI'), dict)
                or not study.get('JBI')
                or not study.get('Quality Score')
            ):
                if pmid not in saved_quality_by_pmid:
                    saved_quality_by_pmid[pmid] = find_saved_study(
                        pmid, exposure, evidence_track=evidence_track
                    )
                saved_study = saved_quality_by_pmid[pmid]
                for field in ('JBI', 'Quality Score', 'Quality %'):
                    if (not study.get(field)) and saved_study.get(field) not in (None, ''):
                        study[field] = saved_study[field]
            v_info = verifications.get(pmid, {})

            study['exclusions'] = 0
            study['exclusion_flags'] = 0

            if not isinstance(v_info, dict):
                v_info = {}
            context_excl = v_info.get('context_exclusions', {})
            study['exclusion_flags'] = context_excl.get(context_key, 0)

            contexts = v_info.get('contexts', {})
            current_context_data = contexts.get(context_key, {})

            if current_context_data:
                submissions = current_context_data.get('submissions', [])
            else:
                submissions = []

            study['verifications'] = len(submissions)
            if matching_submission_signature(submissions) or study['exclusion_flags'] >= 2:
                study['verification_status'] = 'review_requested'
            elif study['verifications'] > 0:
                study['verification_status'] = 'partial'
            else:
                study['verification_status'] = 'unverified'

        # Rebuild presentation-only Egger guidance for every saved cache so
        # legacy wording never requires rewriting authoritative result files.
        if result.get("headline"):
            headline = result["headline"]
            headline["funnel_interpretation"] = meta_analysis.get_funnel_interpretation(
                disease,
                exposure,
                len(result["studies"]),
                headline.get("eggers_p"),
                headline.get("eggers_intercept"),
            )

    log_event(
        f"[MetaVir] Analyze complete in {time.time() - started_at:.1f}s: "
        f"studies={len(result.get('studies', []))}, error={result.get('error')}"
    )
    result['evidence_track'] = evidence_track
    result = dietary_policy.apply_record(result, exposure)
    if isinstance(result, dict) and isinstance(result.get("studies"), list):
        result["studies"] = [dietary_policy.apply_row(row, exposure) for row in result["studies"]]
    return jsonify(sanitize_data(result))

@app.route('/verify', methods=['POST'])
def verify():
    if not _review_origin_allowed():
        return _json_response({"error": "Cross-site review requests are not allowed"}, status=403)
    data = request.get_json(silent=True) or {}
    try:
        evidence_track = normalize_evidence_track(data.get('evidence_track'))
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)
    pmid = str(data.get('pmid') or '').strip()
    if not re.fullmatch(r"\d{1,12}", pmid):
        return _json_response({"error": "A valid PMID is required"}, status=400)
    reporter_hash = _reporter_identity()
    if reporter_hash is None:
        return _json_response({
            "error": "Your review session is missing or invalid. Refresh the page and try again."
        }, status=428)
    try:
        disease, exposure, outcome = _review_request_context(data)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)
    context_key = build_context_key(
        disease, exposure, outcome, evidence_track=evidence_track
    )
    study_data = find_review_study(
        pmid, disease, exposure, outcome, evidence_track=evidence_track
    )
    if not study_data:
        return _json_response({"error": "The article is not in this saved evidence set"}, status=404)

    should_notify = False
    notification_event_id = None
    notification_already_sent = False
    notification_pending = False

    try:
        with locked_verifications() as verifications:
            entry = _verification_entry(verifications, pmid)
            context = _verification_context(entry, context_key)

            if study_data:
                metrics = {
                    "Effect Size": study_data.get("Effect Size"),
                    "Lower CI": study_data.get("Lower CI"),
                    "Upper CI": study_data.get("Upper CI"),
                    "Cases": study_data.get("Cases"),
                    "Sample Size": study_data.get("Sample Size"),
                    "Design": study_data.get("Design"),
                    "Timing": study_data.get("Timing"),
                    "Comparison Type": study_data.get("comparison_type"),
                    "exposure_measurement_type": study_data.get("exposure_measurement_type"),
                }
                submission = next((
                    item for item in context["submissions"]
                    if item.get("reporter_hash") == reporter_hash
                ), None)
                if submission is None:
                    context["submissions"].append({
                        "reporter_hash": reporter_hash,
                        "data": metrics,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                else:
                    submission["data"] = metrics
                    submission["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            matching_signature = matching_submission_signature(context["submissions"])
            notifications = context["notifications"]
            notified_signatures = notifications.setdefault(
                "matching_submission_signatures", []
            )
            matching_state = notifications.setdefault("matching_submissions", {})
            signature_events = matching_state.setdefault("events", {})
            notification_already_sent = bool(
                matching_signature and matching_signature in notified_signatures
            )
            if matching_signature and not notification_already_sent:
                event_state = signature_events.setdefault(matching_signature, {})
                notification_event_id = hashlib.sha256(
                    f"matching|{pmid}|{context_key}|{matching_signature}".encode("utf-8")
                ).hexdigest()
                should_notify = _claim_notification(
                    event_state, notification_event_id
                )
                notification_pending = not should_notify

            total_count = len(context["submissions"])
            review_requested = matching_signature is not None
    except Exception as exc:
        log_event(f"[MetaVir] Verification write failed for PMID {pmid}: {exc}")
        return _json_response({
            "success": False,
            "error": "The verification could not be saved. Please try again.",
        }, status=500)

    notification_sent = False
    notification_error_code = None
    if should_notify and notification_event_id:
        notification_sent, transport_error = send_developer_notification(
            "matching_submissions", exposure, disease, outcome, pmid, study_data
        )
        notification_error_code = None if notification_sent else "email_unavailable"
        try:
            with locked_verifications() as verifications:
                entry = _verification_entry(verifications, pmid)
                context = _verification_context(entry, context_key)
                notifications = context["notifications"]
                notified_signatures = notifications.setdefault(
                    "matching_submission_signatures", []
                )
                matching_state = notifications.setdefault("matching_submissions", {})
                event_state = matching_state.setdefault("events", {}).setdefault(
                    matching_signature, {}
                )
                _finish_notification(
                    event_state, notification_event_id, notification_sent, transport_error
                )
                if notification_sent and matching_signature not in notified_signatures:
                    notified_signatures.append(matching_signature)
        except Exception as exc:
            # The report and pending delivery claim were already committed.
            # Keep the claim so an immediate retry cannot duplicate the email.
            log_event(
                f"[MetaVir] Could not persist verification email result for PMID {pmid}: {exc}"
            )
            notification_pending = True

    status = "review_requested" if review_requested else "partial"
    return _json_response({
        "success": True,
        "count": total_count,
        "status": status,
        "consensus_reached": False,
        "review_requested": review_requested,
        "notification_sent": notification_sent,
        "notification_already_sent": notification_already_sent,
        "notification_pending": notification_pending,
        "notification_error_code": notification_error_code,
        "results_changed": False,
    })

@app.route('/exclude', methods=['POST'])
def exclude():
    if not _review_origin_allowed():
        return _json_response({"error": "Cross-site review requests are not allowed"}, status=403)
    data = request.get_json(silent=True) or {}
    try:
        evidence_track = normalize_evidence_track(data.get('evidence_track'))
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)
    pmid = str(data.get('pmid') or '').strip()
    if not re.fullmatch(r"\d{1,12}", pmid):
        return _json_response({"error": "A valid PMID is required"}, status=400)
    reporter_hash = _reporter_identity()
    if reporter_hash is None:
        return _json_response({
            "error": "Your review session is missing or invalid. Refresh the page and try again."
        }, status=428)
    try:
        disease, exposure, outcome = _review_request_context(data)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)
    context_key = build_context_key(
        disease, exposure, outcome, evidence_track=evidence_track
    )
    study_data = find_review_study(
        pmid, disease, exposure, outcome, evidence_track=evidence_track
    )
    if not study_data:
        return _json_response({"error": "The article is not in this saved evidence set"}, status=404)

    should_notify = False
    notification_event_id = None
    notification_pending = False

    try:
        with locked_verifications() as verifications:
            entry = _verification_entry(verifications, pmid)
            context = _verification_context(entry, context_key)
            reports = context.setdefault("exclusion_reports", [])
            if not isinstance(reports, list):
                reports = []
                context["exclusion_reports"] = reports

            already_flagged = any(
                report.get("reporter_hash") == reporter_hash for report in reports
            )
            if not already_flagged:
                reports.append({
                    "reporter_hash": reporter_hash,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })

            exclusion_count = len(reports)
            entry["context_exclusions"][context_key] = exclusion_count

            notifications = context["notifications"]
            notification_state = notifications.setdefault("exclusion_flags", {})
            if not isinstance(notification_state, dict):
                notification_state = {}
                notifications["exclusion_flags"] = notification_state
            notification_already_sent = bool(notification_state.get("sent_at"))
            if len(reports) >= 2 and not notification_already_sent:
                notification_event_id = hashlib.sha256(
                    f"exclude|{pmid}|{context_key}".encode("utf-8")
                ).hexdigest()
                should_notify = _claim_notification(
                    notification_state, notification_event_id
                )
                notification_pending = not should_notify
    except Exception as exc:
        log_event(f"[MetaVir] Exclusion flag write failed for PMID {pmid}: {exc}")
        return _json_response({
            "success": False,
            "error": "The flag could not be saved. Please try again.",
        }, status=500)

    notification_sent = False
    notification_error_code = None
    if should_notify and notification_event_id:
        notification_sent, transport_error = send_developer_notification(
            "exclusion_flags", exposure, disease, outcome, pmid, study_data
        )
        notification_error_code = None if notification_sent else "email_unavailable"
        try:
            with locked_verifications() as verifications:
                entry = _verification_entry(verifications, pmid)
                context = _verification_context(entry, context_key)
                notifications = context["notifications"]
                notification_state = notifications.setdefault("exclusion_flags", {})
                _finish_notification(
                    notification_state, notification_event_id,
                    notification_sent, transport_error,
                )
                if notification_sent:
                    # Retain the historical marker for readers during a rolling
                    # deployment while the nested state becomes canonical.
                    notifications["exclusion_flags_sent_at"] = notification_state["sent_at"]
        except Exception as exc:
            log_event(
                f"[MetaVir] Could not persist exclusion email result for PMID {pmid}: {exc}"
            )
            notification_pending = True

    return _json_response({
        "success": True,
        "pmid": pmid,
        "context_key": context_key,
        "exclusions": exclusion_count,
        "already_flagged": already_flagged,
        "review_requested": exclusion_count >= 2,
        "notification_sent": notification_sent,
        "notification_already_sent": notification_already_sent,
        "notification_pending": notification_pending,
        "notification_error_code": notification_error_code,
        "results_changed": False,
    })

def _fair_quality_source_rows(data, disease, exposure, evidence_track):
    """Read the same authoritative evidence used by the Extracted Studies table."""
    cache_dir = evidence_cache_dir(evidence_track)
    manifest = load_combined_manifest() if evidence_track == 'combined' else None
    downstream = data.get('use_downstream', False)
    for flag in (downstream, not downstream):
        path = get_cache_path(disease, exposure, 'Incidence', data.get('exclude_meta', False),
                              flag, cache_dir=cache_dir)
        if not os.path.isfile(path):
            continue
        result = load_json(path, {})
        if not is_cache_for_evidence_track(result, evidence_track, manifest):
            break
        if evidence_track == 'chatgpt':
            result = project_chatgpt_subexposures(result, exposure, os.path.basename(path))
        return [dietary_policy.apply_row(row, exposure) for row in result.get('studies', [])]
    raise ValueError('Saved evidence is unavailable for this Fair-quality selection. Reload the analysis.')


@app.route('/reanalyze', methods=['POST'])
def reanalyze():
    data = request.json or {}
    try:
        evidence_track = normalize_evidence_track(data.get('evidence_track'))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    studies = data.get('studies', [])
    try:
        disease = resolve_disease_scope(data.get('disease', DEFAULT_DISEASE), data.get('subcategory'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if str(data.get('outcome', 'Incidence')).strip().casefold() != "incidence":
        return jsonify({"error": "MetaVir supports prostate cancer incidence only."}), 400
    exposure = data.get('exposure', 'Custom Exposure')
    quality_filter = data.get('quality_filter', 'Moderate+')

    from scripts.fair_quality_selection import is_fair_opt_in, bind_fair_rows
    if is_fair_opt_in(data):
        try:
            studies = bind_fair_rows(studies,
                _fair_quality_source_rows(data, disease, exposure, evidence_track), exposure)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

    if quality_filter == 'Moderate+':
        studies = [
            study for study in studies
            if str(study.get('Quality Score') or '').strip().lower() in {'good', 'moderate'}
        ]

    studies = [
        study for study in studies
        if meta_analysis.is_eligible_effect_type(study.get('Effect Type'))
    ]

    if not studies:
        return jsonify({"error": "No studies meet the selected analysis criteria."}), 400

    # Convert list of dicts to DataFrame
    studies = [dietary_policy.apply_row(row, exposure) for row in studies]
    df = pd.DataFrame(studies)

    # Ensure numeric columns are floats
    numeric_cols = ['Effect Size', 'Lower CI', 'Upper CI', 'SE']
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    import hashlib
    projection_id = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]
    plot_namespace = 'subexposures/' + subexposure_mode() + '/interactive/' + projection_id
    result = meta_analysis.perform_meta_analysis(
        df, disease, exposure, outcome="Incidence",
        exclude_meta=data.get('exclude_meta', False),
        plot_namespace=plot_namespace,
    )
    if isinstance(result, dict):
        result['evidence_track'] = evidence_track
        result['disease'] = disease
    result = dietary_policy.apply_record(result, exposure)
    if isinstance(result, dict) and isinstance(result.get("studies"), list):
        result["studies"] = [dietary_policy.apply_row(row, exposure) for row in result["studies"]]
    return jsonify(sanitize_data(result))

def get_last_updated_time(cache_dir=None):
    """Find the latest modification time in one evidence cache root."""
    cache_dir = cache_dir or active_cache_dir()
    latest_time = 0.0
    if os.path.exists(cache_dir):
        for root, dirs, files in os.walk(cache_dir):
            for file in files:
                if file.endswith('.json'):
                    fp = os.path.join(root, file)
                    try:
                        mtime = os.path.getmtime(fp)
                        if mtime > latest_time:
                            latest_time = mtime
                    except Exception:
                        pass
    return latest_time if latest_time > 0 else None

@app.route('/api/config')
def get_config():
    """Return read-only status, last updated time, and the list of pre‑cached exposures."""
    try:
        evidence_track = normalize_evidence_track(request.args.get('track'))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    exposures = []
    cache_dir = evidence_cache_dir(evidence_track)
    combined_manifest = load_combined_manifest() if evidence_track == 'combined' else {}
    cache_ready = (
        bool(combined_manifest)
        if evidence_track == 'combined'
        else os.path.isdir(cache_dir)
    )
    if cache_ready and os.path.exists(cache_dir):
        for d in os.listdir(cache_dir):
            d_path = os.path.join(cache_dir, d)
            if os.path.isdir(d_path):
                try:
                    if any(f.endswith('.json') for f in os.listdir(d_path)):
                        exposures.append(d)
                except Exception:
                    pass
    return jsonify({
        "read_only": READ_ONLY_MODE,
        "cached_exposures": sorted(exposures),
        "last_updated": get_last_updated_time(cache_dir) if cache_ready else None,
        "canonical_track": evidence_track if cache_ready else None,
        "combined_build_id": (
            combined_manifest.get("build_id")
            if evidence_track == 'combined' and cache_ready
            else None
        ),
    })

def load_additional_exposure_catalog():
    """Return the collision-checked V1 + V2 additional-exposure union."""
    result = {}
    canonical_by_key = {}
    for filename in ('additionalExposure.json', 'additionalExposureV2.json'):
        path = os.path.join(DATA_DIR, filename)
        raw = load_json(path, {})
        if not isinstance(raw, dict):
            raise ValueError(f'{filename} must contain a JSON object')
        for raw_name, entry in raw.items():
            name = str(raw_name).strip()
            if not name:
                continue
            key = name.casefold()
            if key in canonical_by_key:
                prior_name, prior_filename = canonical_by_key[key]
                raise ValueError(
                    'Additional exposure catalogs duplicate canonical exposure '
                    f'{name!r}: {prior_filename} ({prior_name!r}) and {filename}'
                )
            canonical_by_key[key] = (name, filename)
            result[name] = entry
    return result


@app.route('/api/synonyms')
def get_synonyms():
    """Return current and additional reviewed vocabulary for UI synonym hints."""
    raw_synonyms = load_json(os.path.join(DATA_DIR, 'synonyms_cache.json'), {})
    synonyms = {}
    if isinstance(raw_synonyms, dict):
        for name, raw in raw_synonyms.items():
            entry = raw if isinstance(raw, dict) else {'core': raw}
            normalized = dict(entry)
            for field in ('core', 'downstream', 'excluded'):
                value = normalized.get(field)
                if isinstance(value, list):
                    normalized[field] = ', '.join(map(str, value))
            synonyms[str(name)] = normalized
    additional = load_additional_exposure_catalog()
    for name, raw in additional.items():
        entry = raw if isinstance(raw, dict) else {'core': raw}
        normalized = {}
        for source, target in (
            ('core', 'core'),
            ('guarded', 'downstream'),
            ('downstream', 'downstream'),
        ):
            value = entry.get(source)
            if value and target not in normalized:
                normalized[target] = (
                    ', '.join(map(str, value)) if isinstance(value, list) else str(value)
                )
        normalized.setdefault('core', str(name))
        normalized.setdefault('downstream', '')
        synonyms.setdefault(str(name), normalized)
    return jsonify(synonyms)


@app.route('/api/exposures')
def get_exposures():
    """Return the collision-checked union used by the final exposure UI."""
    current = load_json(os.path.join(STATIC_DIR, 'exposures.json'), [])
    additional = load_additional_exposure_catalog()
    values = [str(value) for value in current] if isinstance(current, list) else []
    seen = {value.casefold() for value in values}
    for name in additional:
        canonical = str(name)
        if canonical.casefold() not in seen:
            values.append(canonical)
            seen.add(canonical.casefold())
    return jsonify(values)

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)

# Recompute saved evidence under request-local baseline/display settings.
from analysis_settings_backend import install_app as install_analysis_settings
install_analysis_settings(app, meta_analysis, __file__)

if __name__ == '__main__':
    # Keep local apps on separate ports; preserve PORT for hosted deployments.
    port = int(os.environ.get('PORT', 5002))
    host = '0.0.0.0' if 'PORT' in os.environ else '127.0.0.1'
    print(f'Starting MetaVir. Open http://127.0.0.1:{port}', flush=True)
    # Disable the reloader so that mid-analysis .pyc file generation in stdlib (like xml.sax) doesn't restart the server
    app.run(host=host, port=port, debug=False, use_reloader=False)
