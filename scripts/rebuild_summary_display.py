"""Re-render Chinese summary PDFs from their current published workbooks only."""
from pathlib import Path
import argparse,hashlib,importlib.util,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from display_localization import install_plot_localization

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',choices=['metaPee','MetaVir'],required=True);args=parser.parse_args()
    source_root=ROOT/'sources'/args.source
    sys.path.insert(0,str(source_root))
    spec=importlib.util.spec_from_file_location('summary_display_builder',source_root/'scripts/build_summary_plots.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    from scripts import plot_localization
    install_plot_localization(plot_localization)
    import pandas as pd
    from summary_outputs import input_fingerprint
    for mode in ('yes','no'):
        directory=ROOT/'summary'/mode;path=directory/'summary_manifest.json';manifest=json.loads(path.read_text())
        builder.SUBEXPOSURE_MODE=mode
        before={str(p.relative_to(ROOT)):sha(p) for p in directory.glob('*.xlsx')}
        scopes=manifest['sources'][args.source]['scopes']
        changed=[]
        for scope in scopes:
            config=manifest['scopes'][scope];disease=config['label'];frames={}
            for kind,entry in config['workbooks'].items():
                if not entry.get('available'):continue
                records=pd.read_excel(directory/entry['filename']).to_dict('records')
                for row in records:
                    row['category']=builder.exposure_category(builder.safe_component(row['Exposure']))
                    for key in ('number studies','total N','total Cases'):
                        row[key]=int(row[key]) if pd.notna(row.get(key)) else None
                frames[kind]=records
            for kind,entry in config['plots'].items():
                if not entry.get('available'):continue
                dietary='dietary' in kind;rows=frames['dietary' if dietary else 'combined']
                for locale in ('zh-CN','zh-TW'):
                    target=directory/entry['locales'][locale]
                    if kind.startswith('forest-'):
                        count=builder.forest_plot(target,rows,disease,'protective' if 'protective' in kind else 'harmful',dietary,locale)
                    else:
                        count=builder.heterogeneity_plot(target,rows,disease,'egger' if kind.startswith('egger') else 'effect',locale)
                    assert count==entry['exposure_count'],(scope,kind,count,entry['exposure_count'])
                    target.with_suffix('.png').unlink(missing_ok=True)
                    manifest['artifacts'][str(target.relative_to(directory))]={'sha256':sha(target),'bytes':target.stat().st_size}
                    changed.append(str(target.relative_to(directory)))
            print(f'{args.source}/{mode}/{scope}',flush=True)
        assert before=={str(p.relative_to(ROOT)):sha(p) for p in directory.glob('*.xlsx')}
        manifest['sources'][args.source]['input_sha256']=input_fingerprint(ROOT,args.source,mode)
        manifest.setdefault('presentation',{})[args.source]={'chinese_terminology':'营养因子 / 營養因子','workbooks_unchanged':True,'artifacts':changed}
        path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':main()
