"""Verify that a summary-forest presentation release preserves scientific inputs."""
from pathlib import Path
import hashlib,json,subprocess,sys

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def verify(root):
    root=Path(root).resolve();uro=root.name=='metaUro'
    audit=root/('evidence-revisions/summary-pi-2026-09-27' if uro else 'data/quantitative_audits/summary_pi_2026-09-27')
    before=json.loads((audit/'protected_before.json').read_text());unchanged=0;metadata=[];errors=[]
    for relative,item in before['files'].items():
        path=root/relative
        if path.is_file() and sha(path)==item['sha256']:unchanged+=1;continue
        permitted=path.suffix=='.json' and ('Plot' in path.parts if not uro else relative.startswith('summary/')) and 'manifest' in path.name
        if uro and relative=='runtime_code/manifest.json':permitted=True
        if not path.is_file() or not permitted:errors.append(relative);continue
        old=json.loads(subprocess.check_output(['git','show',before['head']+':'+relative],cwd=root));new=json.loads(path.read_text())
        new.pop('forest_prediction_interval_style',None)
        if uro and relative.startswith('summary/'):
            assert set(old['artifacts'])==set(new['artifacts']),relative
            for name,entry in new['artifacts'].items():
                if entry!=old['artifacts'][name]:
                    assert Path(name).name.startswith('forest_'),(relative,name)
                    assert entry['sha256']==sha(path.parent/name),(relative,name)
                    assert entry['bytes']==(path.parent/name).stat().st_size,(relative,name)
                    new['artifacts'][name]=old['artifacts'][name]
            for name,entry in new['sources'].items():entry['input_sha256']=old['sources'][name]['input_sha256']
        if uro and relative=='runtime_code/manifest.json':new['shared_files'].pop('summary_pi_style.py',None)
        if new!=old:errors.append(relative)
        else:metadata.append(relative)
    reports=[]
    for path in sorted(audit.glob('render_report_*.json')):reports+=json.loads(path.read_text())
    unique={item['file']:item for item in reports}
    assert unique,'No completed render reports'
    roots = {'metaFemina': ['data/subexposure_views/no/Plot/', 'data/subexposure_views/yes/Plot/',
                           'chatgpt/subexposure_views/no/Plot/', 'chatgpt/subexposure_views/yes/Plot/'],
             'metaPee': ['combined/provisional_ui/subexposure_views/no/Plot/',
                         'combined/provisional_ui/subexposure_views/yes/Plot/'],
             'metaUro': ['summary/no/', 'summary/yes/']}[root.name]
    tracked=subprocess.check_output(['git','ls-files'],cwd=root,text=True).splitlines()
    expected={name for name in tracked if any(name.startswith(prefix) for prefix in roots)
              and Path(name).name.startswith('forest_') and Path(name).suffix in {'.pdf','.png'}}
    if expected!=set(unique):errors.append({'forest_coverage_missing':sorted(expected-set(unique)),
                                            'forest_coverage_extra':sorted(set(unique)-expected)})
    excluded=audit/'excluded_offline_artifacts.json'
    if excluded.exists():
        for item in json.loads(excluded.read_text())['restored_files']:
            if sha(root/item['file'])!=item['restored_sha256']:errors.append('Offline restoration: '+item['file'])
    for item in unique.values():
        if sha(root/item['file'])!=item['sha256']:errors.append('Rendered artifact checksum: '+item['file'])
    result={'status':'passed' if not errors else 'failed','protected_files':len(before['files']),'unchanged_files':unchanged,'presentation_metadata_only':metadata,'numerical_json_workbooks_and_nonforest_plots_unchanged':not errors,'rendered_forests':len(unique),'errors':errors}
    code_paths = {'metaFemina': ['subcategory_analysis.py','scripts/plot_localization.py','scripts/summary_pi_style.py','scripts/refresh_summary_prediction_intervals.py','scripts/verify_summary_prediction_intervals.py'],
                  'metaPee': ['scripts/build_summary_plots.py','scripts/plot_localization.py','scripts/summary_pi_style.py','scripts/refresh_summary_prediction_intervals.py','scripts/verify_summary_prediction_intervals.py'],
                  'metaUro': ['summary_outputs.py','runtime_code/summary_pi_style.py','automation/refresh_summary_prediction_intervals.py','automation/finalize_summary_prediction_intervals.py','automation/verify_summary_prediction_intervals.py']}[root.name]
    result['presentation_code_sha256']={name:sha(root/name) for name in code_paths}
    result['active_plot_roots']=roots
    result['locales']=sorted({item['locale'] for item in unique.values()})
    result['artifact_counts']={suffix:sum(Path(name).suffix==suffix for name in unique) for suffix in ['.pdf','.png']}
    (audit/'verification.json').write_text(json.dumps(result,indent=2)+'\n');(audit/'render_report.json').write_text(json.dumps(list(unique.values()),indent=2)+'\n')
    print(root.name,result['status'],len(unique),'forests',unchanged,'byte-identical protected files',flush=True)
    if errors:raise SystemExit(str(errors))
if __name__=='__main__':verify(sys.argv[1])
