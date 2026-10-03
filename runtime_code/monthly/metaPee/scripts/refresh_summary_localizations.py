"""Refresh Chinese summary PDFs from published workbooks without pooling or changing data."""
from __future__ import annotations
import argparse, hashlib, json, os, sys, tempfile
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import build_summary_plots as plots
LOCALES=('zh-CN','zh-TW')

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def default_roots():
    pointer=ROOT/'data/local_screening_active.json'
    if pointer.exists():
        view=json.loads(pointer.read_text())['view_id']
        return [ROOT/'data/local_screening_views'/view/'Plot'/('subexposures_'+mode) for mode in ('yes','no')]
    return [ROOT/'combined/provisional_ui/subexposure_views'/mode/'Plot' for mode in ('yes','no')]

def refresh(plot_root):
    manifest_path=plot_root/'summary_manifest.json'; manifest=json.loads(manifest_path.read_text())
    inputs={manifest_path:digest(manifest_path)}; jobs=[]
    for scope in manifest['scopes'].values():
        datasets={}
        for name,metadata in scope['workbooks'].items():
            path=plot_root/metadata['filename']; inputs[path]=digest(path)
            rows=pd.read_excel(path).to_dict('records')
            for row in rows:
                row['category']=plots.exposure_category(plots.safe_component(row['Exposure']))
                for column in ('number studies','number estimates','total N','total Cases'):
                    if column in row and pd.notna(row[column]): row[column]=int(row[column])
            datasets[name]=rows
        for name,metadata in scope['plots'].items():
            if not metadata.get('available'): continue
            for locale in LOCALES:
                filename=metadata.get('locales',{}).get(locale)
                if not filename: raise ValueError(f'Missing declared locale: {scope["label"]}/{name}/{locale}')
                jobs.append((name,metadata,locale,filename,scope['label'],datasets))
    with tempfile.TemporaryDirectory(prefix='meta-summary-locales-') as temporary:
        staged=Path(temporary)/plot_root.relative_to(ROOT)
        outputs=[]
        for name,metadata,locale,filename,label,datasets in jobs:
            path=staged/filename
            if hasattr(plots,'SUBEXPOSURE_MODE'):
                plots.SUBEXPOSURE_MODE='yes' if 'yes' in plot_root.parts else 'no'
            if name.startswith('forest-'):
                direction='protective' if 'protective' in name else 'harmful';dietary=name.endswith('-dietary')
                count=plots.forest_plot(path,datasets['dietary' if dietary else 'combined'],label,direction,dietary,locale)
            else:
                count=plots.heterogeneity_plot(path,datasets['combined'],label,'egger' if name.startswith('egger') else 'effect',locale)
            if count!=metadata['exposure_count'] or not path.is_file():
                raise ValueError(f'Saved plot count changed: {filename}: {count} != {metadata["exposure_count"]}')
            outputs.append((path,plot_root/filename))
        if any(digest(path)!=checksum for path,checksum in inputs.items()): raise ValueError('Saved numerical source changed')
        for staged_path,target in outputs:
            target.parent.mkdir(parents=True,exist_ok=True); os.replace(staged_path,target)
    audit={'schema_version':1,'builder':'scripts/refresh_summary_localizations.py','locales':list(LOCALES),'scientific_inputs_unchanged':True,
           'source_sha256':{str(path.relative_to(ROOT)):sha for path,sha in inputs.items()},
           'output_sha256':{str(target.relative_to(ROOT)):digest(target) for _,target in outputs}}
    (plot_root/'localization_refresh_audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n')
    print(str(plot_root.relative_to(ROOT)),len(outputs),'Chinese PDFs refreshed',flush=True)
    return len(outputs)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--plot-root',type=Path,action='append');args=parser.parse_args()
    for path in args.plot_root or default_roots(): refresh(path.resolve())
