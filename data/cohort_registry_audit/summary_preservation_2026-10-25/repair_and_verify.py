#!/usr/bin/env python3
"""Verify a cleanup-only repair and rebind complete saved summary publications."""
from pathlib import Path
from io import BytesIO
from copy import deepcopy
from contextlib import contextmanager
import ast
import datetime
import hashlib
import json
import math
import os
import subprocess
import sys

ROOT=Path(os.environ.get('METAURO_REPO_ROOT',Path(__file__).resolve().parents[3])).resolve()
AUDIT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import summary_outputs as outputs
from openpyxl import load_workbook

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text())
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')
def blob(ref,path):return subprocess.check_output(['git','show',ref+':'+path],cwd=ROOT)

def workbook(data):
    book=load_workbook(BytesIO(data),data_only=False,read_only=True)
    return {sheet.title:list(sheet.values) for sheet in book}

def cleanup_only_proof(ref):
    old=ast.parse(blob(ref,'summary_outputs.py').decode());new=ast.parse((ROOT/'summary_outputs.py').read_text())
    functions=lambda module:{node.name:node for node in module.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
    a,b=functions(old),functions(new)
    changed=[name for name in set(a)|set(b) if name not in a or name not in b or ast.dump(a[name],include_attributes=False)!=ast.dump(b[name],include_attributes=False)]
    assert set(changed)=={'finalize_build','_build_lock','_cohort_audit_groups'},changed
    cleaned=deepcopy(b['finalize_build']);removed=0
    for node in ast.walk(cleaned):
        if isinstance(node,ast.For) and node.body and isinstance(node.body[0],ast.If):
            first=node.body[0]
            if "'cohort_latest'" in ast.unparse(first.test) and len(first.body)==1 and isinstance(first.body[0],ast.Continue):
                node.body.pop(0);removed+=1
    metadata_names={'audit_groups','audit_history','incoming_audits'}
    def audit_statement(node):
        if isinstance(node,ast.Assign):return any(isinstance(name,ast.Name) and name.id in metadata_names for target in node.targets for name in ast.walk(target))
        if isinstance(node,ast.If):return any(isinstance(name,ast.Name) and name.id in metadata_names for name in ast.walk(node.test))
        return False
    cleaned.body=[node for node in cleaned.body if not audit_statement(node)]
    assert removed==2 and ast.dump(cleaned,include_attributes=False)==ast.dump(a['finalize_build'],include_attributes=False)
    builder_old=ast.parse(blob(ref,'scripts/build_cohort_summaries.py').decode())
    builder_new=ast.parse((ROOT/'scripts/build_cohort_summaries.py').read_text());builder_assignments=0
    for node in ast.walk(builder_new):
        if isinstance(node,ast.FunctionDef):
            kept=[]
            for statement in node.body:
                if isinstance(statement,ast.Assign) and len(statement.targets)==1 and isinstance(statement.targets[0],ast.Subscript) and ast.unparse(statement.targets[0])=="manifest['cohort_selection_audits_by_source']":
                    builder_assignments+=1
                else:kept.append(statement)
            node.body=kept
    assert builder_assignments==1 and ast.dump(builder_old,include_attributes=False)==ast.dump(builder_new,include_attributes=False)
    protected=['sources','runtime_code','updates','automation/sync_sources.py',
               'cohort_identity.py','cohort_summary_support.py','display_localization.py','prostate_scopes.py',
               'data/cohort_registry.json','data/cohort_record_metadata.json']
    changed_inputs=subprocess.check_output(['git','diff','--name-only',ref,'--',*protected],cwd=ROOT,text=True).splitlines()
    assert not changed_inputs,changed_inputs
    return {'changed_functions':sorted(changed),'finalize_build_only_adds_publication_guards_and_source_audit_metadata_merge':True,
            'lock_change_only_serializes_shared_mode_publications':True,'all_other_summary_output_functions_AST_identical':True,
            'cohort_builder_only_adds_explicit_source_audit_mapping':True,
            'eligibility_renderer_source_and_metadata_paths_unchanged_from_release':protected}

def verify_manifests():
    reports={}
    for mode in outputs.MODES:
        for latest in [False,True]:
            folder=outputs.summary_dir(ROOT,'metaPee',mode,latest);manifest=outputs.read_manifest(folder/'summary_manifest.json')
            assert len(manifest.get('scopes',{}))==9 and set(manifest['sources'])==set(outputs.SOURCES)
            declared=outputs.declared_artifacts(manifest);assert declared==set(manifest['artifacts'])
            for relative,item in manifest['artifacts'].items():assert sha(folder/relative)==item['sha256'],str(folder/relative)
            if latest:
                for field,path in [('cohort_registry_sha256','data/cohort_registry.json'),('cohort_record_metadata_sha256','data/cohort_record_metadata.json'),('cohort_policy_sha256','cohort_identity.py')]:
                    assert manifest[field]==sha(ROOT/path),(field,folder)
                groups=manifest['cohort_selection_audits_by_source']
                assert set(groups)==set(outputs.SOURCES)
                assert manifest['cohort_selection_audits']==[audit for source in sorted(groups) for audit in groups[source]]
                assert all(groups[source]==manifest['sources'][source]['metadata']['cohort_selection_audits'] for source in outputs.SOURCES)
            reports[str(folder.relative_to(ROOT))]={'scopes':len(manifest['scopes']),'artifacts':len(declared),'all_checksums_valid':True,'cohort_bindings_valid':True if latest else None,
                'cohort_audit_contexts_by_source':{source:len(records) for source,records in groups.items()} if latest else None}
    return reports

def main():
    baseline=read(AUDIT/'preexisting_summary_inventory.json');ref=baseline['head_commit'];proof=cleanup_only_proof(ref)
    historical=read(ROOT/'data/cohort_registry_audit/summary_build_receipt.json')
    source_hashes=historical['source_cache_sha256'];metadata_hashes=historical['cohort_inputs_sha256']
    assert all(sha(ROOT/path)==expected for path,expected in source_hashes.items())
    assert all(sha(ROOT/path)==expected for path,expected in metadata_hashes.items())
    restored=[]
    for relative in baseline['missing_tracked_files']:
        path=ROOT/relative
        if not path.exists():
            data=blob(ref,relative);assert hashlib.sha256(data).hexdigest()==baseline['head_sha256'][relative]
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);restored.append(relative)
        assert sha(path)==baseline['head_sha256'][relative],relative
    # Every file present before repair remains byte-for-byte preserved.
    assert all(sha(ROOT/path)==expected for path,expected in baseline['working_sha256'].items() if not path.endswith('/summary_manifest.json'))
    scientific=[];numeric=[];exact=0
    for relative in sorted(baseline['head_sha256']):
        if not relative.endswith('.xlsx'):continue
        a,b=workbook(blob(ref,relative)),workbook((ROOT/relative).read_bytes());assert set(a)==set(b)
        differences=[]
        for sheet,rows in a.items():
            assert len(rows)==len(b[sheet]),(relative,sheet)
            for ri,(x,y) in enumerate(zip(rows,b[sheet])):
                assert len(x)==len(y)
                for ci,(before,after) in enumerate(zip(x,y)):
                    if before==after:continue
                    assert isinstance(before,(int,float)) and isinstance(after,(int,float)),(relative,sheet,ri,ci,before,after)
                    assert math.isclose(before,after,rel_tol=1e-8,abs_tol=1e-12),(relative,sheet,ri,ci,before,after)
                    column=rows[0][ci];assert column not in {'number studies','total N','total Cases'}
                    differences.append({'sheet':sheet,'row':ri+1,'column':column,'before':before,'after':after})
        if not differences:exact+=1
        else:
            assert '/cohort_latest/' not in relative,relative
            numeric.append({'path':relative,'cells':differences})
        scientific.append(relative)
    for mode in outputs.MODES:
        folder=outputs.summary_dir(ROOT,'metaPee',mode,True);manifest=read(folder/'summary_manifest.json')
        groups,history=outputs._cohort_audit_groups(manifest)
        manifest['cohort_selection_audits_by_source']=groups
        manifest['cohort_selection_audits']=[audit for source in sorted(groups) for audit in groups[source]]
        if history:manifest['cohort_selection_audit_history']=history
        write(folder/'summary_manifest.json',manifest)
    before=verify_manifests();fingerprints={};input_files={};old_sha=hashlib.sha256(blob(ref,'summary_outputs.py')).hexdigest();real_sha=outputs.sha
    for mode in outputs.MODES:
        for source in outputs.SOURCES:
            seen={}
            def capture(path):
                value=real_sha(path);seen[str(Path(path).relative_to(ROOT))]=value;return value
            outputs.sha=capture
            try:current=outputs.input_fingerprint(ROOT,source,mode)
            finally:outputs.sha=real_sha
            outputs.sha=lambda path:old_sha if Path(path)==ROOT/'summary_outputs.py' else real_sha(path)
            try:previous=outputs.input_fingerprint(ROOT,source,mode)
            finally:outputs.sha=real_sha
            fingerprints[source+'_'+mode]={'current':current,'same_inputs_with_release_cleanup_code':previous}
            input_files.update(seen)
    receipt_relative='data/cohort_registry_audit/summary_preservation_2026-10-25/verification.json'
    for mode in outputs.MODES:
        with outputs._build_lock(ROOT/'summary'/mode):
            for latest in [False,True]:
                folder=outputs.summary_dir(ROOT,'metaPee',mode,latest);manifest=read(folder/'summary_manifest.json')
                for source in outputs.SOURCES:manifest['sources'][source]['input_sha256']=fingerprints[source+'_'+mode]['current']
                manifest['publication_maintenance_receipt']=receipt_relative
                write(folder/'summary_manifest.json',manifest)
                assert all(outputs._current(folder,fingerprints[source+'_'+mode]['current'],source) for source in outputs.SOURCES)
    after=verify_manifests()
    assert all(sha(ROOT/path)==expected for path,expected in baseline['working_sha256'].items() if not path.endswith('/summary_manifest.json'))
    assert all(sha(ROOT/path)==expected for path,expected in source_hashes.items())
    assert all(sha(ROOT/path)==expected for path,expected in metadata_hashes.items())
    restoration=read(AUDIT/'restored_missing_outputs.json')
    receipt={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'passed','baseline_release_commit':ref,
        'cause':'Base summary cleanup recursed into the independent cohort_latest publication, removing nested latest assets. Later single-source refresh restored urinary assets only.',
        'repair':'Preserve nested latest publication during base cleanup; serialize base/latest builds using the same mode lock; restore only absent checksum-bound release assets; preserve source-specific cohort audits and flat aggregation, retaining superseded or unassignable legacy records with provenance; rebind fingerprints after invariance verification.',
        'restored_files':restoration['restored_from_checksum_bound_HEAD'],'overwritten_present_artifacts':[],
        'dirty_snapshot':{'path':baseline['snapshot'],'sha256':baseline['snapshot_sha256']},
        'existing_present_artifacts_bytes_unchanged':True,'cleanup_and_producer_invariance':proof,
        'scientific_workbooks_checked':len(scientific),'exact_workbooks':exact,'latest_workbook_and_cohort_exclusion_cells_exact':True,
        'numeric_drift_in_preexisting_default_workbooks':numeric,'numeric_tolerance':{'relative':1e-8,'absolute':1e-12},
        'study_sample_and_case_counts_exact':True,'pooled_row_selection_invariant':'Eligibility/renderer code, source-cache bytes, cohort registry, record metadata, and cohort policy are unchanged; cleanup/locking edits only affect publication files.',
        'source_cache_sha256':source_hashes,'cohort_inputs_sha256':metadata_hashes,'producer_input_sha256':input_files,
        'producer_fingerprints':fingerprints,'manifest_verification':after,
        'manifest_sha256':{folder+'/summary_manifest.json':sha(ROOT/folder/'summary_manifest.json') for folder in after},
        'producing_repair_script_sha256':sha(__file__),'no_model_calls':True,'no_source_or_cohort_metadata_changes':True}
    write(AUDIT/'verification.json',receipt)
    print(json.dumps({'status':receipt['status'],'restored_files':len(receipt['restored_files']),'workbooks_checked':len(scientific),'exact_workbooks':exact,'floating_drift_workbooks':len(numeric),'source_cache_files_verified':len(source_hashes),'publication_variants':after},indent=2))

if __name__=='__main__':main()
