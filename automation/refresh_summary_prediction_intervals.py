"""Redraw centralized summary forests from unchanged saved workbooks."""
from __future__ import annotations
import argparse,hashlib,io,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import pandas as pd
from matplotlib.figure import Figure
from automation.sync_sources import assembled_source
from runtime_code.summary_pi_style import valid_bounds,install
from display_localization import install_plot_localization
VERSION='separate-summary-pi-2026-09-27'
AUDIT=ROOT/'evidence-revisions/summary-pi-2026-09-27'
plots=None
def render_manifest(plot,manifest,report,scopes=None):
    for key,scope in manifest['scopes'].items():
        if scopes is not None and key not in scopes:continue
        for dataset,book in scope['workbooks'].items():
            if not book.get('available'):continue
            path=plot/book['filename'];rows=pd.read_excel(path).to_dict('records')
            for row in rows:
                for field,value in list(row.items()):
                    if pd.isna(value):row[field]=None
                for field in ['number estimates','number studies','total N','total Cases']:
                    value=row.get(field)
                    if value is not None and float(value).is_integer():row[field]=int(value)
                row['category']=plots.exposure_category(row['Exposure'])
            for direction in ['protective','harmful']:
                name='forest-'+direction+('-dietary' if dataset=='dietary' else '')
                meta=scope['plots'].get(name)
                if not meta or not meta.get('available'):continue
                for locale,filename in meta.get('locales',{'en':meta['filename']}).items():
                    output=plot/filename
                    if not output.exists():raise FileNotFoundError(output)
                    original_save=Figure.savefig
                    def buffered_save(fig,path,*args,**kwargs):
                        buffer=io.BytesIO();original_save(fig,buffer,*args,**{**kwargs,'format':Path(path).suffix.lstrip('.')});Path(path).write_bytes(buffer.getvalue())
                    Figure.savefig=buffered_save
                    try:
                        count=plots.forest_plot(output,rows,scope['label'],direction,dataset=='dietary',locale)
                    finally:Figure.savefig=original_save
                    selected=plots._ordered_plot_rows(rows,direction)
                    valid=sum(valid_bounds(row.get('lower PI RR'),row.get('upper PI RR')) is not None for row in selected)
                    report.append({'file':str(output.relative_to(ROOT)),'source_workbook':str(path.relative_to(ROOT)),'rows':count,'valid_prediction_intervals':valid,'locale':locale,'sha256':hashlib.sha256(output.read_bytes()).hexdigest()})

def main():
    global plots
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True,choices=['metaPee','MetaVir']);args=parser.parse_args()
    source=assembled_source(ROOT,args.source)
    sys.path[:0]=[str(ROOT/'runtime_code'/args.source),str(source)]
    from scripts import build_summary_plots,plot_localization
    plots=build_summary_plots
    install_plot_localization(plot_localization)
    if hasattr(plots,'load_existing_translation_overrides'):plots.load_existing_translation_overrides()
    install(plots)
    report=[]
    for mode in ['no','yes']:
        plot=ROOT/'summary'/mode;path=plot/'summary_manifest.json';manifest=json.loads(path.read_text())
        plots.SUBEXPOSURE_MODE=mode
        render_manifest(plot,manifest,report,manifest['sources'][args.source]['scopes'])
        print(args.source,mode,'forest-only refresh complete',flush=True)
    AUDIT.mkdir(parents=True,exist_ok=True);(AUDIT/f'render_report_{args.source}.json').write_text(json.dumps(report,indent=2)+'\n');print('Rendered',len(report),'forest PDFs',flush=True)
if __name__=='__main__':main()
