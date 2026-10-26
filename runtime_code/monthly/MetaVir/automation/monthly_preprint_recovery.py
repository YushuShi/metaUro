"""Separate, resumable monthly preprint R3; native journal packets stay intact.

Raw bodies/prompts/responses belong in .monthly-checkpoint, never the public run.
This module is clone-local and requires no sibling repository or campaign files.
"""
from __future__ import annotations
import collections
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

VERSION = "monthly-separate-preprint-r3-v1"
MODELS = ("gpt-6-luna", "gemini-3.8-flash", "gpt-6-astra")
SECOND_NAMES = ("second_screen.json", "chatgpt_second_screen.json",
                "monthly_parallel/claude_r2_results.json", "monthly_parallel/claude_r2_progress.json")
SOURCE_NAMES = ("full_text_review.json", "chatgpt_full_text_review.json", "full_text_sources.json", "full_text_source_manifest.json",
                "pmc_source_status.json", "monthly_parallel/gemini_r3_results.json",
                "monthly_parallel/gemini_r3_progress.json")
REVIEW_NAMES = ("full_text_review.json", "chatgpt_full_text_review.json",
                "monthly_parallel/gemini_r3_results.json", "monthly_parallel/gemini_r3_progress.json")
HUMAN_FIELDS = ("human_adjudication", "manual_review", "manual_adjudication",
                "manual_full_text_adjudication", "manual_full_text_review", "combined_manual_authority")


class RecoveryOperationalHold(RuntimeError):
    """Preserved operational/source errors must prevent a completed watermark."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value, *, immutable=False):
    path = Path(path)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if immutable and path.exists():
        if path.read_bytes() != raw:
            raise RecoveryOperationalHold("A pinned recovery input changed; retain the checkpoint for review")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def rows(document):
    if isinstance(document, list):
        return [r for r in document if isinstance(r, dict)]
    for name in ("results", "results_by_context", "contexts", "records"):
        value = document.get(name)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        if isinstance(value, dict):
            return [dict(r, context_id=r.get("context_id", key)) for key, r in value.items() if isinstance(r, dict)]
    return []


def protected(row):
    return any(row.get(k) for k in HUMAN_FIELDS) or str(row.get("decision_origin", "")).lower() in {
        "human", "manual", "human_manual_review"}


def excluded(row):
    return any(str(row.get(k, "")).upper() in {"EXCLUDE", "EXCLUDED", "FAIL"}
               for k in ("eligibility", "decision", "final_status"))


def collect_contexts(root, run):
    """Only exact current native R2/R3 requirements, after native adjudication."""
    documents, bindings = {}, {}
    for name in (*SECOND_NAMES, *SOURCE_NAMES, "final_screening.json", "candidates.json"):
        path = run / name
        if path.is_file() and not path.is_symlink():
            documents[name] = json.loads(path.read_text())
            bindings[str(path.relative_to(root))] = sha(path)
    final = {r.get("context_id"): r for r in rows(documents.get("final_screening.json", {}))}
    unreviewed = {r.get("context_id") for name in SOURCE_NAMES for r in rows(documents.get(name, {}))
                  if r.get("pmc_source_status") in {"no_pmcid", "unavailable", "retrieval_error", "front_matter_only"}}
    articles = {str(a["pmid"]): a for a in documents.get("candidates.json", {}).get("articles", [])}
    result = {}
    for name in SECOND_NAMES:
        for index, row in enumerate(rows(documents.get(name, {}))):
            cid = row.get("context_id")
            if not cid or protected(row) or excluded(row):
                continue
            current = final.get(cid, {})
            if protected(current) or excluded(current) or current.get("r3_not_required_after_policy") is True:
                continue
            # Neuro's current R3 packet itself proves its native gate was true;
            # do not reconstruct quantitative requirements with a heuristic.
            if row.get("needs_full_text") is not True and cid not in unreviewed:
                continue
            pmid = str(row.get("pmid") or row.get("PMID") or "")
            if not pmid.isdigit():
                raise RecoveryOperationalHold("A source-required R2 context has no exact PMID")
            parts = str(cid).split("|")
            article = articles.get(pmid, {})
            context = result.setdefault(cid, {
                "repository": root.name, "context_id": cid, "pmid": pmid,
                "pmcid": row.get("pmcid") or article.get("pmcid") or "",
                "exposure": row.get("exposure") or (parts[1] if len(parts) > 1 else None),
                "disease": row.get("disease") or row.get("cancer") or (parts[2] if len(parts) > 2 else None),
                "outcome": row.get("outcome") or row.get("endpoint") or (parts[3] if len(parts) > 3 else "Incidence"),
                "human_protected": False, "dispatch_authorized": True, "r2_records": [],
            })
            if context["pmid"] != pmid:
                raise RecoveryOperationalHold("Native context has conflicting PMID identity")
            context["r2_records"].append({"source_path": str((run / name).relative_to(root)),
                                          "source_sha256": bindings[str((run / name).relative_to(root))],
                                          "row_index": index, "saved_record": row})
    return list(result.values()), documents, bindings


def _positive_pmc(node, wanted, inherited_pmid="", inherited_pmc="", identifier_map=None):
    """Conservative available-source blockers; never infer unavailability."""
    if isinstance(node, list):
        for child in node:
            yield from _positive_pmc(child, wanted, inherited_pmid, inherited_pmc, identifier_map)
    elif isinstance(node, dict):
        if node.get("preprint_derived") is True:
            return
        pmid = str(node.get("pmid") or node.get("PMID") or inherited_pmid)
        pmc = str(node.get("pmcid") or node.get("PMCID") or node.get("source_identifier") or inherited_pmc)
        if not pmid and identifier_map and len(identifier_map.get(pmc, set())) == 1:
            pmid = next(iter(identifier_map[pmc]))
        status = str(node.get("pmc_source_status") or node.get("status") or "").lower()
        known_available = (node.get("verified_full_pmc_available_any_track") is True
                           or node.get("saved_any_track_full_text_available") is True
                           or node.get("verified_full_pmc_available") is True)
        native_full = re.fullmatch(r"PMC\d+", pmc, re.I) and status in {
            "full_text", "verified_full_text", "excluded_pdf_only_user_policy"}
        if pmid in wanted and (known_available or native_full):
            yield pmid, {"pmcid": pmc or None, "status": status or "verified_any_track_full_PMC",
                         "source_sha256": node.get("text_sha256") or node.get("pmc_source_sha256") or node.get("sha256")}
        skip = {"extraction", "study", "studies", "supporting_text", "text", "body", "abstract", "Abstract",
                "prompt", "messages", "raw_output", "headers", "response_headers", "request_headers"}
        for key, value in node.items():
            if key not in skip and isinstance(value, (dict, list)):
                pid = str(key) if str(key).isdigit() else pmid
                pc = str(key) if re.fullmatch(r"PMC\d+", str(key), re.I) else pmc
                yield from _positive_pmc(value, wanted, pid, pc, identifier_map)


def source_availability(root, run, contexts, documents):
    wanted = {c["pmid"] for c in contexts}
    if not wanted:
        return collections.defaultdict(list), {}
    identifier_map = collections.defaultdict(set)
    for context in contexts:
        if context.get("pmcid"):
            identifier_map[str(context["pmcid"]).upper()].add(context["pmid"])
    available, inputs = collections.defaultdict(list), {}
    for name, document in documents.items():
        path = run / name
        for pmid, proof in _positive_pmc(document, wanted, identifier_map=identifier_map):
            available[pmid].append(dict(proof, path=str(path.relative_to(root)), artifact_sha256=sha(path)))
    # Saved globally checked positive PMC certificates are safe to reuse as
    # blockers. A historical negative certificate is never a fresh negative.
    prior = root / "data/preprint_recovery"
    prior_paths = set()
    for name in ("article_preprint_source_gate.json", "authorized_linked_article_gate.json"):
        prior_paths.update(prior.rglob(name) if prior.exists() else [])
    # Search positive certificates in every saved source track, including
    # historical source manifests and independent-review receipts. Never reuse
    # historical negatives as evidence of current source unavailability.
    for folder in (root / "data", root / "combined", root / "Cached_results", root / "ClaudeGemini_results"):
        if not folder.is_dir():
            continue
        for name in {*[Path(v).name for v in SOURCE_NAMES], "*.receipt.json"}:
            prior_paths.update(folder.rglob(name))
    for path in sorted(prior_paths):
        if not path.is_file() or path.is_symlink():
            continue
        if path.is_relative_to(run) and "preprint_recovery" in path.relative_to(run).parts:
            continue
        inputs[str(path.relative_to(root))] = sha(path)
        for pmid, proof in _positive_pmc(json.loads(path.read_text()), wanted, identifier_map=identifier_map):
            available[pmid].append(dict(proof, path=str(path.relative_to(root)), artifact_sha256=sha(path)))
    return available, inputs


def http_get(url, *, accept="application/json"):
    """Bounded public-only transport; no authentication headers or cookies."""
    if not str(url).startswith("https://"):
        raise ValueError("Public HTTPS required")
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"Accept": accept, "User-Agent": "meta-analysis-monthly-preprint/1"}), timeout=45) as response:
                body = response.read(64 * 1024 * 1024 + 1)
                if len(body) > 64 * 1024 * 1024:
                    raise ValueError("Public source exceeds bounded retrieval size")
                return {"status": response.status, "body": body, "headers": dict(response.headers), "final_url": response.url}
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                return {"status": error.code, "body": b"", "headers": {}, "final_url": url}
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise RecoveryOperationalHold("Metadata/source transport failed")


def pdf_text(raw):
    with tempfile.TemporaryDirectory(prefix="monthly-preprint-pdf-") as folder:
        source, target = Path(folder) / "source.pdf", Path(folder) / "source.txt"
        source.write_bytes(raw)
        subprocess.run(["pdftotext", "-layout", str(source), str(target)], check=True,
                       capture_output=True, timeout=90)
        return target.read_text()


def default_services():
    from .preprint_recovery import europepmc_fallback as recovery, preprint_screening as screening, native_protocol, pmc_availability

    def client():
        from openai import OpenAI
        key = (os.getenv("CORNELL_API_KEY") or os.getenv("Cornell_API_Key") or "").strip()
        base = (os.getenv("CORNELL_API_BASE_URL") or os.getenv("API_Base_URI") or os.getenv("API_BASE_URI") or "").strip()
        if not key or not base:
            raise RecoveryOperationalHold("Preprint R3 gateway configuration is missing")
        value = OpenAI(api_key=key, base_url=base, max_retries=0, timeout=600)
        if not set(MODELS).issubset({r.id for r in value.models.list()}):
            raise RecoveryOperationalHold("Required approved preprint R3 models are unavailable")
        return value
    return {"http_get": http_get, "recover_preprint": recovery.recover_preprint,
            "run_job": screening.run_job, "cost_report": screening.cost_report,
            "protocol": native_protocol.protocol, "client_factory": client, "extract_pdf_text": pdf_text,
            "probe_pmc": pmc_availability.probe_article}


def metadata(pmid, private, get):
    path = private / "metadata" / (pmid + ".json")
    if path.exists():
        packet = json.loads(path.read_text())
        if digest(packet["record"]) != packet["record_sha256"]:
            raise RecoveryOperationalHold("Pinned published metadata changed")
        return packet["record"], path
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode({
        "query": f"EXT_ID:{pmid} AND SRC:MED", "format": "json", "resultType": "core", "pageSize": 10})
    response = get(url, accept="application/json")
    if response.get("status") != 200:
        raise RecoveryOperationalHold("Fresh exact-PMID MED metadata unavailable")
    document = json.loads(response["body"])
    found = [r for r in document.get("resultList", {}).get("result", [])
             if str(r.get("id")) == pmid and r.get("source") == "MED"]
    if len(found) != 1:
        raise RecoveryOperationalHold("Fresh MED response has no unique exact PMID")
    record = found[0]
    packet = {"record": record, "record_sha256": digest(record), "source_url": url,
              "response_sha256": hashlib.sha256(response["body"]).hexdigest(), "status": 200}
    save(path, packet, immutable=True)
    return record, path


def _safe_csv(path, items):
    fields = ["repository", "pmid", "preprint_id", "context_id", "exposure", "disease", "outcome",
              "decision", "confidence", "reason", "action"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for item in items:
        values = {}
        for key in fields:
            value = str(item.get(key) or "")
            values[key] = "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
        writer.writerow(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stream.getvalue())


def _public_result(result):
    # Complete normalized scientific bundles retained, raw model output and
    # full source text remain in the private checkpoint only.
    return {key: result[key] for key in ("status", "job_id", "protocol_version", "adjudication_policy",
            "producer_source_sha256", "reviews", "adjudication", "problems") if key in result}


def recover_run(repo_root, run_dir, reports_dir=None, *, services=None):
    root, run = Path(repo_root).resolve(), Path(run_dir).resolve()
    run.relative_to(root / "data/monthly_updates")
    run_id = run.name
    public = run / "preprint_recovery"
    private = root / ".monthly-checkpoint/preprint_recovery" / run_id
    contexts, documents, input_bindings = collect_contexts(root, run)
    available, availability_inputs = source_availability(root, run, contexts, documents)
    input_bindings.update(availability_inputs)
    code_paths = [root / "automation/monthly_preprint_recovery.py", root / "automation/monthly_pipeline.py",
                  root / "automation/gateway_preflight.py"]
    code_paths += sorted((root / "automation/preprint_recovery").glob("*.py"))
    producing_code = {str(path.relative_to(root)): sha(path) for path in code_paths if path.is_file()}
    input_bindings.update(producing_code)
    save(private / "native_inputs.json", {"version": VERSION, "input_bindings": input_bindings,
                                         "contexts": contexts}, immutable=True)
    service = services
    articles, jobs, holds, decisions, operational, public_files = [], [], [], [], [], {}
    client = None

    def publish(relative, value):
        path = public / relative
        save(path, value)
        public_files[str(path.relative_to(root))] = sha(path)
        return path

    def hold(context, reason, *, error=False, pid=None):
        item = {k: context.get(k) for k in ("repository", "pmid", "context_id", "exposure", "disease", "outcome")}
        item.update(reason=reason, action="No default result or source-view activation; inspect preserved recovery evidence.")
        if pid:
            item["preprint_id"] = pid
        holds.append(item)
        if error:
            operational.append(item)

    grouped = collections.defaultdict(list)
    for context in contexts:
        grouped[context["pmid"]].append(context)
    for pmid, selected in sorted(grouped.items(), key=lambda pair: int(pair[0])):
        if available.get(pmid):
            articles.append({"pmid": pmid, "status": "not_needed", "reason": "verified_full_PMC_available_any_track",
                             "proofs": available[pmid]})
            reviewed = {row.get("context_id") for name in REVIEW_NAMES for row in rows(documents.get(name, {}))
                        if row.get("third_round_status") == "COMPLETED"
                        or (row.get("pmc_source_status") == "full_text" and (
                            row.get("full_text_status") in {"INCLUDE_META", "ELIGIBLE_NOT_POOLABLE", "EXCLUDE", "INCLUDE", "ELIGIBLE_SUBTYPE_REVIEW"}
                            or (isinstance(row.get("native_decision"), dict)
                                and row["native_decision"].get("decision") in {"include", "exclude"}
                                and row.get("source_receipt") and row.get("full_text_characters_reviewed", 0) > 0)))}
            for context in selected:
                if context["context_id"] not in reviewed:
                    hold(context, "Original PMC full text is available; resume native full-text R3 for this exact context", error=True)
            continue
        try:
            if service is None:
                service = default_services()
            med, med_path = metadata(pmid, private, service["http_get"])
            actual = service["probe_pmc"](pmid, med, http_get=service["http_get"],
                cache_dir=private / "PMC_availability" / pmid, extract_pdf_text=service.get("extract_pdf_text"),
                saved_pmcids=[c["pmcid"] for c in selected if c.get("pmcid")])
            probe_path = private / "gates" / (pmid + "_fresh_probe_" + digest(actual) + ".json")
            save(probe_path, actual, immutable=True)
            state, pmcids = actual["availability"], actual.get("pmcids", [])
            if state in {"pmcid_missing", "full_text_unavailable"} and not (
                    str(actual.get("pmid")) == pmid and actual.get("actual_source_check_complete") is True
                    and actual.get("identity_verified") is True and actual.get("transient_error") is False
                    and actual.get("verified_full_pmc_available") is False and actual.get("proofs")):
                state = "unknown"
            if state == "available":
                facts = {"pmid": pmid, "status": "not_needed", "reason": "fresh_original_PMC_full_text_available",
                         "proofs": actual["proofs"]}
                articles.append(facts)
                publish("articles/" + pmid + ".json", facts)
                for context in selected:
                    hold(context, "Original PMC is now accessible; resume native full-text R3 with the preserved source proof", error=True)
                continue
            any_path = private / "gates" / (pmid + "_any_track.json")
            save(any_path, {"pmid": pmid, "native_input_bindings": input_bindings,
                           "verified_full_PMC_available": False, "proofs": available.get(pmid, [])}, immutable=True)
            gate_record = {"pmid": pmid, "availability": state, "pmcids": pmcids,
                           "fresh_epmc_pmcid": actual.get("fresh_epmc_pmcid", med.get("pmcid")),
                           "fresh_pubmed_pmcids": actual.get("fresh_pubmed_pmcids", []),
                           "fresh_idconv_pmcids": actual.get("fresh_idconv_pmcids", []),
                           "preprint_fetch_allowed": state in {"pmcid_missing", "full_text_unavailable"},
                           "model_dispatch_authorized": state in {"pmcid_missing", "full_text_unavailable"},
                           "saved_any_track_checked": True, "saved_any_track_full_text_available": False,
                           "saved_any_track_record_sha256": sha(any_path),
                           "proofs": [{"path": str(probe_path.relative_to(root)), "sha256": sha(probe_path),
                                       "record_sha256": digest(actual), "checks": actual["proofs"]}]}
            gate_path = private / "gates" / (pmid + "_article_" + digest(gate_record) + ".json")
            save(gate_path, {"schema_version": 1, "records": [gate_record]}, immutable=True)
            if state == "unknown":
                articles.append({"pmid": pmid, "status": "held", "reason": "Fresh_PMC_metadata_or_source_availability_unverified"})
                for context in selected:
                    hold(context, "Fresh identifier or actual original-PMC availability probe incomplete", error=True)
                continue
            authorization = {"pmid": pmid, "preprint_fetch_allowed": True, "fresh_availability": state,
                             "gate_sha256": sha(gate_path), "bound_reports": [
                                 {"path": str(med_path), "sha256": sha(med_path)},
                                 {"path": str(any_path), "sha256": sha(any_path)},
                                 {"path": str(probe_path), "sha256": sha(probe_path)}]}
            recovered = service["recover_preprint"](med, pmc_availability=state,
                authorize_retrieval=lambda identifier, proof=authorization: proof if identifier == pmid else {},
                http_get=service["http_get"], cache_dir=private / "source_cache" / pmid,
                extract_pdf_text=service.get("extract_pdf_text"))
            save(private / "retrieval" / (pmid + ".json"), recovered)
            facts = {"pmid": pmid, "status": recovered["status"], "reason": recovered.get("reason"),
                     "sources": [s["preprint_source"] for s in recovered.get("sources", [])],
                     "candidate_ids": [v["record"]["id"] for v in recovered.get("candidates", [])],
                     "errors": recovered.get("errors", []), "PMC_gate": gate_record}
            articles.append(facts)
            publish("articles/" + pmid + ".json", facts)
            if recovered["status"] in {"lookup_error", "held"} or recovered.get("errors"):
                for context in selected:
                    hold(context, "Preprint metadata/source operation incomplete", error=True)
                continue
            sources = recovered.get("sources", [])
            if not sources:
                if recovered["status"] != "no_match":
                    for context in selected:
                        hold(context, "No verified complete legal preprint body; source candidates remain audit-only")
                continue
            if len(sources) != 1:
                for context in selected:
                    hold(context, "Multiple verified preprint sources/versions require explicit source selection")
                continue
            source_view = sources[0]
            source_meta = source_view["preprint_source"]
            pid = source_meta["preprint_id"]
            candidate = next(v["record"] for v in recovered["candidates"] if v["record"]["id"] == pid)
            source_root = private / "source_jobs" / (pmid + "_" + pid)
            text_path, published_path, candidate_path = source_root / "text.txt", source_root / "published.json", source_root / "preprint.json"
            source_root.mkdir(parents=True, exist_ok=True)
            if not text_path.exists():
                text_path.write_text(source_view["text"])
            if sha(text_path) != source_meta["text_sha256"]:
                raise RecoveryOperationalHold("Verified preprint source text changed")
            save(published_path, med, immutable=True)
            save(candidate_path, candidate, immutable=True)
            auth_path = source_root / "authorization.json"
            save(auth_path, {"pmid": pmid, "preprint_id": pid, "body_fetch_authorized": True,
                             "model_dispatch_authorized": True, "native_R3_authorized": True,
                             "policy": VERSION, "native_contexts": [c["context_id"] for c in selected]}, immutable=True)
            proof_path = source_root / "title_author_proof.json"
            save(proof_path, {"pmid": pmid, "preprint_id": pid,
                 "published_metadata": {"path": str(published_path), "sha256": sha(published_path)},
                 "preprint_metadata": {"path": str(candidate_path), "sha256": sha(candidate_path)},
                 "authorization": {"path": str(auth_path), "sha256": sha(auth_path)}}, immutable=True)
            # Exact title/surname is independently recomputed by the producer;
            # linked-only differing titles keep their real relation type.
            methods = source_meta.get("discovery_methods", [])
            source = {"published_pmid": pmid, "published_title": med.get("title"), "preprint_id": pid,
                      "preprint_doi": source_meta.get("doi"), "preprint_version": source_meta.get("version"),
                      "preprint_posted_date": source_meta.get("posted_date"), "retrieval_url": source_meta["source_url"],
                      "text_path": str(text_path), "text_sha256": sha(text_path),
                      "raw_path": str(private / "source_cache" / pmid / source_view["body_path"]),
                      "raw_source_sha256": source_meta["source_sha256"], "full_text_verified": True,
                      "license": source_meta.get("license"), "preprint_derived": True}
            if "exact_title_surname" in methods:
                source.update(discovery_method="exact_title_first_author_surname",
                              title_author_proof={"path": str(proof_path), "sha256": sha(proof_path)})
            else:
                relation = next((v for v in (med.get("commentCorrectionList") or {}).get("commentCorrection", [])
                                 if v.get("id") == pid and v.get("type") == "Preprint in"
                                 and v.get("source") == "PPR"), None)
                if relation is None:
                    for context in selected:
                        hold(context, "Linked-only discovery lacks the exact saved Europe PMC Preprint in relation", pid=pid)
                    continue
                source.update(discovery_method="PMID_link", relation={"type": relation["type"], "id": pid})
            protocols = [service["protocol"](root.name, c["outcome"], c["disease"], repo_root=root) for c in selected]
            job = {"repository": root.name, "source": source, "contexts": selected,
                   "native_criteria": "\n\n".join("CONTEXT " + c["context_id"] + "\n" + p["native_criteria"] for c, p in zip(selected, protocols)),
                   "native_criteria_sources": [v for p in protocols for v in p.get("native_criteria_sources", [])],
                   "native_extraction_fields": list(dict.fromkeys(v for p in protocols for v in p.get("native_extraction_fields", []))),
                   "native_extraction_schema": protocols[0].get("native_extraction_schema", {}),
                   "pmc_availability_gate": {"path": str(gate_path), "sha256": sha(gate_path),
                                             "pmid": pmid, "record_sha256": digest(gate_record)}}
            if client is None:
                client = service["client_factory"]()
            result = service["run_job"](client, job, private / "screening")
            result_path = publish("jobs/" + result["job_id"] + "/result.json", _public_result(result))
            jobs.append({"job_id": result["job_id"], "pmid": pmid, "preprint_id": pid,
                         "context_ids": [c["context_id"] for c in selected], "status": result["status"],
                         "result_path": str(result_path.relative_to(root)), "result_sha256": sha(result_path),
                         "private_result_sha256": sha(private / "screening/jobs" / result["job_id"] / "result.json")})
            if result["status"] != "reviewed":
                for context in selected:
                    hold(context, "Independent preprint R3/adjudication operational or validation hold", error=True, pid=pid)
                continue
            for decision in result["adjudication"]["results"]:
                context = next(c for c in selected if c["context_id"] == decision["context_id"])
                item = {k: context.get(k) for k in ("repository", "pmid", "context_id", "exposure", "disease", "outcome")}
                decisions.append(dict(item, preprint_id=pid, **{k: decision.get(k) for k in ("decision", "confidence", "reason")}))
                if decision.get("decision") in {"INCLUDE_META", "ELIGIBLE_NOT_POOLABLE"}:
                    hold(context, "Complete preprint R3 retained; exact current native optional cell/record mapping requires reviewed conversion", pid=pid)
                elif decision.get("decision") not in {"EXCLUDE"}:
                    hold(context, "Scientific disagreement remains in manual review; no estimate activated", pid=pid)
        except Exception as error:
            for context in selected:
                hold(context, "Recovery operation failed: " + type(error).__name__, error=True)
    if service is not None and (private / "screening").exists():
        full_cost = service["cost_report"](private / "screening")
    else:
        full_cost = {"gateway_reported_cost_usd": 0, "unknown_cost_calls": 0, "dispatches": 0, "calls": []}
    cost = {k: full_cost.get(k) for k in ("gateway_reported_cost_usd", "unknown_cost_calls", "dispatches")}
    public_cost = dict(cost, note="Unknown costs are not zero; repository copies are not additional charges.",
        calls=[{k: c.get(k) for k in ("role", "request_sha256", "model", "reasoning_effort", "context_ids",
                "response_received", "usage", "gateway_reported_cost_usd")} for c in full_cost.get("calls", [])])
    publish("public/model_cost_and_attempts.json", public_cost)
    publish("public/decisions.json", {"schema_version": 1, "records": decisions})
    publish("public/holds.json", {"schema_version": 1, "records": holds})
    for name, items in (("decisions", decisions), ("holds", holds)):
        path = root / "manualReview/preprint_recovery/monthly" / (run_id + "_" + name + ".csv")
        _safe_csv(path, items)
        public_files[str(path.relative_to(root))] = sha(path)
    changed = [p for p, expected in input_bindings.items() if sha(root / p) != expected]
    if changed:
        operational.append({"reason": "Native source input changed during separate recovery"})
    manifest = {"schema_version": 1, "version": VERSION, "repository": root.name, "run_id": run_id,
                "status": "operational_hold" if operational else "complete", "input_bindings": input_bindings,
                "producing_code": producing_code,
                "candidate_contexts": len(contexts), "articles": articles, "jobs": jobs, "cost": cost,
                "holds": holds, "operational_holds": len(operational), "owned_public_files": public_files,
                "default_results_changed": False, "source_view_default": False,
                "private_audit_policy": "Raw source bodies, prompts and response bytes retained in .monthly-checkpoint/preprint_recovery; never public release inputs."}
    manifest_path = public / "recovery_manifest.json"
    save(manifest_path, manifest)
    if operational:
        raise RecoveryOperationalHold("Monthly preprint recovery has preserved operational holds; watermark must not advance")
    return {"schema_version": 1, "status": "complete", "manifest_path": str(manifest_path.relative_to(root)),
            "manifest_sha256": sha(manifest_path), "owned_public_files": public_files}
