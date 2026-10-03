"""Refresh affected Bladder cancer summaries with the existing calculation path."""
from pathlib import Path
import json
import pandas as pd
from scripts import build_summary_plots as summary
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data/quantitative_audits/trend_p_ci_2026-09-27'


def main():
    payload=[];reports=[]
    from scripts import plot_localization
    ui_catalog=ROOT/'static/i18n-translations.json'
    if ui_catalog.exists():
        for label,translations in json.loads(ui_catalog.read_text()).items():
            if isinstance(translations,dict):
                for language,value in translations.items():
                    plot_localization.DISEASE_LABELS.setdefault(label,{}).setdefault(language,value)
    overrides=ROOT/'combined/astra_adjudication/plot_translation_overrides.json'
    if overrides.exists():
        catalog=plot_localization._site_catalog(str(ROOT.resolve()))
        for label,translations in json.loads(overrides.read_text())['translations'].items():
            for language,value in translations.items():
                catalog.setdefault(label,{}).setdefault(language,value)
                if hasattr(plot_localization,'GUT_EXPOSURE_LABELS'):
                    plot_localization.GUT_EXPOSURE_LABELS.setdefault(label,{}).setdefault(language,value)
    original_inventory=summary.inventory
    summary.inventory=lambda root:{k:v for k,v in original_inventory(root).items() if k=='Bladder cancer'}
    def capture(path,rows):
        frame=pd.DataFrame(rows,columns=summary.COLUMNS)
        payload.append({'mode':str(path.parent.relative_to(ROOT)).replace('/','_'),'filename':path.name,
                        'outputPath':str(path),'columns':list(frame.columns),
                        'rows':json.loads(frame.to_json(orient='records',double_precision=15)),
                        'previousRows':len(pd.read_excel(path)) if path.exists() else 0})
    summary.workbook=capture
    for mode in ['no','yes']:
        base=ROOT/'combined/provisional_ui/subexposure_views'/mode;plot=base/'Plot'
        prior=json.loads((plot/'summary_manifest.json').read_text())
        current=summary.build(base/'Cached_results',plot)
        prior.setdefault('scopes',{}).update(current['scopes']);prior['trend_ci_correction_version']='trend-p-ci-2026-09-27'
        (plot/'summary_manifest.json').write_text(json.dumps(prior,indent=2,ensure_ascii=False)+'\n')
        reports.append({'mode':mode,'scopes':list(current['scopes'])})
    (OUT/'workbook_payload.json').write_text(json.dumps(payload,indent=2,ensure_ascii=False)+'\n')
    (OUT/'summary_refresh_report.json').write_text(json.dumps(reports,indent=2)+'\n')
    print('Workbook payload',len(payload),flush=True)

if __name__=='__main__':main()
