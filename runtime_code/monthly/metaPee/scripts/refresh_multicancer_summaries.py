"""Refresh the affected summary rows and plots, preserving all other results."""
from pathlib import Path
import json,sys,copy
from openpyxl import load_workbook
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import build_summary_plots as summary
from scripts import multicancer_evidence as review
OUT=review.DIRECTORY

def read_rows(path):
 if not path.exists():return []
 book=load_workbook(path,read_only=True,data_only=True)
 try:
  values=list(book.worksheets[0].values)
  return [dict(zip(values[0],row)) for row in values[1:] if row[0] is not None]
 finally:book.close()

def main():
 payload=[];reports=[]
 from scripts import plot_localization
 for name in ['static/i18n-translations.json']:
  path=ROOT/name
  if path.exists():
   for label,translations in json.loads(path.read_text()).items():
    if isinstance(translations,dict):
     for language,value in translations.items():plot_localization.DISEASE_LABELS.setdefault(label,{}).setdefault(language,value)
 overrides=ROOT/'combined/astra_adjudication/plot_translation_overrides.json'
 if overrides.exists():
  catalog=plot_localization._site_catalog(str(ROOT.resolve()))
  for label,translations in json.loads(overrides.read_text())['translations'].items():
   for language,value in translations.items():
    catalog.setdefault(label,{}).setdefault(language,value)
    if hasattr(plot_localization,'GUT_EXPOSURE_LABELS'):plot_localization.GUT_EXPOSURE_LABELS.setdefault(label,{}).setdefault(language,value)
 original_inventory=summary.inventory;original_summary=summary.summarize_cache
 diseases={'Prostate cancer'} if ROOT.name=='MetaVir' else {'Bladder cancer','Renal cell carcinoma','Kidney and renal pelvis cancer'}
 exposures={summary.safe_component(e['record']['exposure']) for e in review.registry()['entries']}
 for mode in ['no','yes']:
  cache=ROOT/f'Subexposure_results/{mode}' if ROOT.name=='MetaVir' else ROOT/f'combined/provisional_ui/subexposure_views/{mode}/Cached_results'
  plot=ROOT/f'Plot/subexposures/{mode}' if ROOT.name=='MetaVir' else cache.parent/'Plot'
  prior=json.loads((plot/'summary_manifest.json').read_text());old={}
  for disease in diseases:
   for dataset in ['combined','dietary']:
    file=plot/f'exposures_meta_analysis_{summary.safe_component(disease)}_{dataset}.xlsx'
    old[(disease,dataset)]={row['Exposure']:row for row in read_rows(file)}
  def selective(path,disease,exposure,dietary):
   if exposure not in exposures:
    preserved=copy.deepcopy(old[(disease,'dietary' if dietary else 'combined')].get(exposure))
    if preserved:preserved['category']=summary.exposure_category(exposure)
    return preserved
   return original_summary(path,disease,exposure,dietary)
  summary.summarize_cache=selective
  summary.inventory=lambda root,**kw:{k:v for k,v in original_inventory(root,**kw).items() if k in diseases}
  def capture(path,rows,*args):
   payload.append({'mode':mode,'filename':path.name,'outputPath':str(path),'columns':summary.COLUMNS,'rows':rows,'previousRows':len(read_rows(path)),'affectedExposures':sorted(exposures)})
  summary.workbook=capture
  current=summary.build(cache,plot)
  prior.setdefault('scopes',{}).update(current['scopes']);prior['evidence_revision']=review.VERSION
  if current.get('combined_manifest_sha256'):prior['combined_manifest_sha256']=current['combined_manifest_sha256']
  (plot/'summary_manifest.json').write_text(json.dumps(prior,indent=2,ensure_ascii=False)+'\n')
  reports.append({'mode':mode,'scopes':list(current['scopes'])})
 (OUT/'workbook_payload.json').write_text(json.dumps(payload,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
 (OUT/'summary_refresh_report.json').write_text(json.dumps(reports,indent=2)+'\n')
 print('Workbook payload',len(payload),flush=True)
if __name__=='__main__':main()
