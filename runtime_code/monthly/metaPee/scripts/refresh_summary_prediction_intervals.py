"""Redraw summary forests from unchanged published workbook values."""
from __future__ import annotations
import argparse, hashlib, io, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import pandas as pd
from matplotlib.figure import Figure
from scripts import build_summary_plots as plots
from scripts.summary_pi_style import valid_bounds
VERSION='separate-summary-pi-2026-09-27'
AUDIT=ROOT/'data/quantitative_audits/summary_pi_2026-09-27'
ROOTS=['combined/provisional_ui/subexposure_views/no/Plot','combined/provisional_ui/subexposure_views/yes/Plot']

def load_labels():
    from scripts import plot_localization
    ui=ROOT/'static/i18n-translations.json'
    if ui.exists():
        for label,translations in json.loads(ui.read_text()).items():
            if isinstance(translations,dict):
                for locale,value in translations.items():
                    plot_localization.DISEASE_LABELS.setdefault(label,{}).setdefault(locale,value)
    path=ROOT/'combined/astra_adjudication/plot_translation_overrides.json'
    if path.exists():
        catalog=plot_localization._site_catalog(str(ROOT.resolve()))
        for label,translations in json.loads(path.read_text())['translations'].items():
            for locale,value in translations.items():
                catalog.setdefault(label,{}).setdefault(locale,value)
                if hasattr(plot_localization,'GUT_EXPOSURE_LABELS'):
                    plot_localization.GUT_EXPOSURE_LABELS.setdefault(label,{}).setdefault(locale,value)

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
    parser=argparse.ArgumentParser();parser.add_argument('--roots',nargs='+',default=ROOTS);parser.add_argument('--tag',default='all');args=parser.parse_args();report=[]
    load_labels()
    for relative in args.roots:
        plot=ROOT/relative;path=plot/'summary_manifest.json';manifest=json.loads(path.read_text())
        plots.SUBEXPOSURE_MODE=next((part for part in reversed(plot.parts) if part in ['yes','no']),None)
        render_manifest(plot,manifest,report)
        manifest['forest_prediction_interval_style']={'version':VERSION,'source':'unchanged published summary workbook values','line':'dark dashed line below CI, with endpoint caps; arrows mark clipping','missing_bounds':'not drawn'}
        path.write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
        print(relative,'forest-only refresh complete',flush=True)
    AUDIT.mkdir(parents=True,exist_ok=True);(AUDIT/f'render_report_{args.tag}.json').write_text(json.dumps(report,indent=2)+'\n');print('Rendered',len(report),'forest PDFs',flush=True)
if __name__=='__main__':main()
