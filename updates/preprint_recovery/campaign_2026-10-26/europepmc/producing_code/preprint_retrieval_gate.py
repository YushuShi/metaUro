"""PMC-first gate before linked-preprint requests; no retrieval/model calls here."""
from pathlib import Path
import hashlib,json,os
WORKSPACE=Path(__file__).resolve().parents[2]
DEFAULT_GATE=WORKSPACE/'preprint_recovery_2026-10-26/article_availability_gate/authorized_linked_article_gate.json'
class PreprintRetrievalDenied(ValueError):pass

def _read(path):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError) as error:raise PreprintRetrievalDenied('A complete current PMC/source-availability gate is required before preprint retrieval.') from error

def _records(doc):
    rows=doc.get('records')
    if isinstance(rows,list):return {str(x.get('pmid')):x for x in rows}
    return rows if isinstance(rows,dict) else {}

def _canonical_hash(row):
    return hashlib.sha256(json.dumps(row,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def retrieval_decision(pmid,gate_path=None):
    """Require current exact-PMID identity and saved full-PMC ANY-track absence.

    Positive full-PMC availability and every unknown/pending state are denied.
    The caller still needs the independent native R3 dispatch authorization.
    """
    pmid=str(pmid)
    if not pmid.isdigit():raise PreprintRetrievalDenied('An exact original PMID is required.')
    path=Path(gate_path or os.environ.get('PREPRINT_SOURCE_AVAILABILITY_GATE') or DEFAULT_GATE)
    doc=_read(path);row=_records(doc).get(pmid)
    if doc.get('schema_version')!=1 or not isinstance(row,dict) or str(row.get('pmid'))!=pmid:
        raise PreprintRetrievalDenied('No complete exact-PMID source-availability proof exists.')
    if row.get('availability') not in {'pmcid_missing','full_text_unavailable'}:
        raise PreprintRetrievalDenied('Fresh PMC availability is available or unknown; preprint fallback is blocked.')
    if row.get('saved_any_track_checked') is not True or row.get('saved_any_track_full_text_available') is not False:
        raise PreprintRetrievalDenied('Published PMC full text exists in another track or its availability is unknown.')
    if row.get('preprint_fetch_allowed') is not True:
        raise PreprintRetrievalDenied('This PMID is not authorized for preprint retrieval.')
    reports=doc.get('source_reports')
    if not isinstance(reports,list) or len(reports)<2:
        raise PreprintRetrievalDenied('Current identity and any-track source reports must both be hash-bound.')
    fresh=saved=None;bound=[]
    for item in reports:
        if not isinstance(item,dict) or not item.get('path') or not item.get('sha256'):
            raise PreprintRetrievalDenied('Source proof descriptors are incomplete.')
        source=Path(item['path']);source=source if source.is_absolute() else WORKSPACE/source
        source=source.resolve()
        if not source.is_relative_to(WORKSPACE.resolve()) or not source.is_file():
            raise PreprintRetrievalDenied('Bound source-availability report is unavailable.')
        if hashlib.sha256(source.read_bytes()).hexdigest()!=item['sha256']:
            raise PreprintRetrievalDenied('Bound source-availability report changed; refresh the gate first.')
        proof=_read(source);candidate=_records(proof).get(pmid)
        if not isinstance(candidate,dict):raise PreprintRetrievalDenied('Source proof does not contain the exact original PMID.')
        if 'availability' in candidate and 'identity_sources' in candidate:fresh=candidate
        if 'verified_full_pmc_available_any_track' in candidate:saved=candidate
        bound.append({'path':str(source),'sha256':item['sha256']})
    if fresh is None or saved is None:
        raise PreprintRetrievalDenied('Both fresh PMID/PMC and any-track saved-source proofs are required.')
    if fresh.get('availability')!=row['availability'] or fresh.get('availability') not in {'pmcid_missing','full_text_unavailable'}:
        raise PreprintRetrievalDenied('The authorization disagrees with fresh PMC availability.')
    if row['availability']=='full_text_unavailable':
        proofs=fresh.get('proofs')
        metadata=fresh.get('id_converter_record') or {}
        explicit_not_live=(metadata.get('live') is False or str(metadata.get('live','')).lower()=='false')
        versions=metadata.get('versions') or []
        if versions and any(v.get('live') is not False and str(v.get('live','')).lower()!='false' for v in versions):explicit_not_live=False
        formats={x.get('format'):x for x in proofs if isinstance(x,dict)} if isinstance(proofs,list) else {}
        explicit_all_formats=all(formats.get(kind,{}).get('availability')=='full_text_unavailable' and formats.get(kind,{}).get('identity_verified') is True for kind in ('xml','html','pdf'))
        if not isinstance(proofs,list) or not proofs or any(x.get('body_verified') is True for x in proofs if isinstance(x,dict)) or not(explicit_not_live or explicit_all_formats):
            raise PreprintRetrievalDenied('Explicit actual-full-PMC unavailability proofs are required; API/model/format errors are insufficient.')
    if saved.get('preprint_source_eligible') is not True or saved.get('verified_full_pmc_available_any_track') is not False:
        raise PreprintRetrievalDenied('The any-track audit blocks this preprint fallback.')
    if row.get('any_track_gate_record_sha256')!=_canonical_hash(saved):
        raise PreprintRetrievalDenied('The exact PMID any-track proof record changed.')
    return {'pmid':pmid,'preprint_fetch_allowed':True,'model_dispatch_authorized':False,
            'fresh_availability':row['availability'],'gate_path':str(path),
            'gate_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bound_reports':bound,
            'native_scientific_R3_gate_still_required':True}

def authorize_preprint_retrieval(pmid,gate_path=None):return retrieval_decision(pmid,gate_path)
