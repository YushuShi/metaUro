"""Small internally bound native receipts for read-only release verifier tests."""
from automation.sync_sources import read, sha, write


def add_native_proofs(screening, scope, receipt):
    write(screening/'candidates.json', {'articles':[]})
    write(screening/'first_screen.json', {'results':[]})
    raw = {name:sha(screening/name) for name in ('candidates.json', 'first_screen.json', 'final_screening.json')}
    source = {name:raw[name] for name in ('candidates.json', 'first_screen.json')}
    write(screening/'monthly_parallel/source_receipt.json', {'source_sha256':source})
    status = {'complete':True, 'models':{'r2':'claude-opus-5','r3':'gemini-3.7-flash'}, 'source_sha256':source}
    write(screening/'monthly_parallel/monthly_status.json', status)
    artifacts = {str(p.relative_to(screening)):sha(p) for p in (screening/'monthly_parallel').rglob('*') if p.is_file()}
    gate = read(screening/'monthly_application_gate.json')
    write(screening/'monthly_application_filter.json', {
        'gate_sha256':receipt['application_gate_sha256'],
        'source_final_screening_sha256':gate['source_final_screening_sha256']})
    native = {'repository':scope, 'run_id':receipt['run_id'], 'status':'validated',
        'raw_packet_sha256':raw, 'independent_review':{**status,'artifact_sha256':artifacts},
        'application_gate_sha256':receipt['application_gate_sha256'],
        'review':{'source_sha256':raw},
        'review_artifacts_sha256':{gate['review_csv']:gate['review_csv_sha256'],
                                  gate['adjudication_path']:gate['adjudication_sha256']}}
    write(screening/'monthly_run_manifest.json', native)
    receipt.update(native_manifest_sha256=sha(screening/'monthly_run_manifest.json'),
                   raw_packet_sha256=raw, independent_review=status)
