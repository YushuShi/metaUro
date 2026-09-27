"""Rebuild corrected urinary summaries atomically in the central output tree."""
from pathlib import Path
import copy,importlib.util,json,os,shutil,subprocess,sys,tempfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from automation.sync_sources import assembled_source,sha
import summary_outputs
SOURCE=assembled_source(ROOT,'metaPee')
os.environ['METAURO_SUMMARY_SOURCE_ROOT']=str(SOURCE)
sys.path[:0]=[str(ROOT/'runtime_code/metaPee'),str(SOURCE)]
from scripts import build_summary_plots as summary
import pandas as pd
OUT=ROOT/'evidence-revisions/trend-p-ci-2026-09-27'


def main():
    payload=[];checks=[]
    original_inventory=summary.inventory
    summary.inventory=lambda root:{k:v for k,v in original_inventory(root).items() if k=='Bladder cancer'}
    def capture(path,rows):
        frame=pd.DataFrame(rows,columns=summary.COLUMNS)
        payload.append({'mode':summary.SUBEXPOSURE_MODE,'filename':path.name,'outputPath':str(path),'columns':list(frame.columns),
                        'rows':json.loads(frame.to_json(orient='records',double_precision=15)),
                        'previousRows':len(pd.read_excel(path)) if path.exists() else 0})
    summary.workbook=capture
    original_finalize=summary.finalize_build
    def finish(source_root,plot_root,manifest):
        # Retain other scopes belonging to this same source during a targeted
        # Bladder cancer rebuild; the central finalizer already retains MetaVir.
        old=json.loads((plot_root/'summary_manifest.json').read_text())
        owned=old.get('sources',{}).get('metaPee',{}).get('scopes',[])
        manifest['scopes']={**{k:old['scopes'][k] for k in owned if k in old['scopes']},**manifest['scopes']}
        current=[item for item in payload if Path(item['outputPath']).parent==plot_root]
        local=OUT/('workbook_payload_'+summary.SUBEXPOSURE_MODE+'.json');local.write_text(json.dumps(current,indent=2)+'\n')
        with tempfile.TemporaryDirectory(prefix='metauro-workbook-') as folder:
            runtime=Path(folder);(runtime/'node_modules').symlink_to(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules',target_is_directory=True)
            shutil.copyfile(ROOT/'automation/build_trend_ci_workbooks.mjs',runtime/'build.mjs')
            subprocess.run(['node',str(runtime/'build.mjs'),str(local),str(plot_root)],check=True)
        verified=json.loads((OUT/'workbook_verification.json').read_text());checks.extend(verified)
        return original_finalize(source_root,plot_root,manifest)
    summary.finalize_build=finish
    for mode in ['no','yes']:
        summary.build(summary_outputs.cache_dir(ROOT,'metaPee',mode),ROOT/'summary'/mode)
        # The prostate cache bytes are unchanged, and the audit found no invalid
        # current prostate interval. Bind its unchanged artifacts to this shared
        # guard/overlay generation after verifying their published checksums.
        path=ROOT/'summary'/mode/'summary_manifest.json';manifest=json.loads(path.read_text())
        prostate=manifest['sources']['MetaVir']
        for name in prostate['artifacts']:
            assert sha(path.parent/name)==manifest['artifacts'][name]['sha256'],name
        prostate['input_sha256']=summary_outputs.input_fingerprint(ROOT,'MetaVir',mode)
        manifest['trend_ci_correction_version']='trend-p-ci-2026-09-27'
        path.write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    for item in payload:
        item['outputPath']=str(ROOT/'summary'/item['mode']/item['filename'])
    (OUT/'workbook_payload.json').write_text(json.dumps(payload,indent=2)+'\n')
    (OUT/'workbook_verification.json').write_text(json.dumps(checks,indent=2)+'\n')
    print('Completed',len(payload),'central workbooks',flush=True)

if __name__=='__main__':main()
