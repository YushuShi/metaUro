"""Rebuild affected Uro summaries from its conserved evidence, transactionally."""
from __future__ import annotations
import argparse, copy, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from automation.import_multicancer_review import AUDIT, VERSION, SCOPES
from automation.sync_sources import assembled_source, read, write, sha
from automation.preserve_multicancer_workbooks import preserve, digest
from automation.refresh_multicancer_results import clean
import summary_outputs


def main(source):
    import pandas as pd
    from openpyxl import load_workbook
    source_root=assembled_source(ROOT,source)
    os.environ['METAURO_SUMMARY_SOURCE_ROOT']=str(source_root)
    sys.path[:0]=[str(ROOT/'runtime_code'/source),str(source_root)]
    from scripts import build_summary_plots as summary
    from scripts import plot_localization
    from display_localization import install_plot_localization
    install_plot_localization(plot_localization)
    payload=[];preservation=[];verification=[]
    original_inventory=summary.inventory;original_summarize=summary.summarize_cache
    original_finalize=summary.finalize_build
    for mode in ('no','yes'):
        cache=summary_outputs.cache_dir(ROOT,source,mode)
        plot=ROOT/'summary'/mode
        affected={}
        for path in cache.glob('*/*_true_core.json'):
            data=read(path);disease=data.get('disease') or data.get('cancer')
            if summary.safe_component(disease) not in SCOPES[source]:continue
            if any(r.get('evidence_revision')==VERSION and r.get('policy_poolable') is True
                and r.get('subexposure_pooling_selected') is not False
                and r.get('disease_hierarchy_pooling_selected') is not False for r in data.get('studies',[])):
                affected.setdefault(disease,set()).add(summary.safe_component(data['exposure']))
        old={}
        for disease in affected:
            for dataset in ('combined','dietary'):
                path=plot/f'exposures_meta_analysis_{summary.safe_component(disease)}_{dataset}.xlsx'
                book=load_workbook(path,read_only=True,data_only=True)
                try:
                    values=list(book.worksheets[0].values)
                    old[(disease,dataset)]={r[0]:dict(zip(values[0],r)) for r in values[1:] if r[0] is not None}
                finally:book.close()
        def selective(path,disease,exposure,dietary):
            if exposure not in affected.get(disease,set()):
                row=copy.deepcopy(old[(disease,'dietary' if dietary else 'combined')].get(exposure))
                if row:row['category']=summary.exposure_category(exposure)
                return row
            return original_summarize(path,disease,exposure,dietary)
        summary.summarize_cache=selective
        summary.inventory=lambda root,**kw:{k:v for k,v in original_inventory(root,**kw).items() if k in affected}
        mode_payload=[];native={}
        def capture(path,rows,*args):
            disease=next(d for d in affected if f'_{summary.safe_component(d)}_' in path.name)
            native[str(path)]=path.read_bytes()
            item={'mode':mode,'filename':path.name,'outputPath':str(path),'columns':list(summary.COLUMNS),
                'rows':clean([{key:row.get(key) for key in summary.COLUMNS} for row in rows]),
                'previousRows':len(old[(disease,'dietary' if '_dietary.' in path.name else 'combined')]),
                'affectedExposures':sorted(affected[disease])}
            mode_payload.append(item)
        summary.workbook=capture
        def finish(root,stage,manifest):
            previous=read(stage/'summary_manifest.json')
            owned=previous['sources'][source]['scopes']
            manifest['scopes']={**{k:previous['scopes'][k] for k in owned},**manifest['scopes']}
            manifest['evidence_revision']=VERSION
            manifest['source_commit']=read(AUDIT/'source_release.json')[source]['commit']
            with tempfile.TemporaryDirectory(prefix='multicancer-uro-workbooks-') as temp:
                runtime=Path(temp)
                (runtime/'node_modules').symlink_to(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules',target_is_directory=True)
                shutil.copyfile(ROOT/'automation/build_trend_ci_workbooks.mjs',runtime/'build.mjs')
                file=runtime/'payload.json';write(file,mode_payload)
                subprocess.run(['node',str(runtime/'build.mjs'),str(file),str(stage)],check=True)
            for item in mode_payload:
                path=Path(item['outputPath']);authored=path.read_bytes()
                final,receipt=preserve(native[str(path)],authored,item)
                path.write_bytes(final)
                preservation.append({'source':source,'mode':mode,'file':path.name,'before_sha256':digest(native[str(path)]),
                    'authored_sha256':digest(authored),'after_sha256':sha(path),**receipt})
                item['outputPath']=str(ROOT/'summary'/mode/path.name)
                payload.append(item)
            for path in stage.rglob('*.xlsx.inspect.ndjson'):path.unlink()
            return original_finalize(root,stage,manifest)
        summary.finalize_build=finish
        summary.build(cache,plot)
        print(source,mode,'central summary transaction complete',flush=True)
    write(AUDIT/f'workbook_payload_{source}.json',payload)
    write(AUDIT/f'workbook_preservation_{source}.json',preservation)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True,choices=['MetaVir','metaPee']);main(parser.parse_args().source)
