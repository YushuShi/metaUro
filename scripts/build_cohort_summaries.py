#!/usr/bin/env python3
"""Build optional cohort-selected summaries without changing source snapshots."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def build(source, mode, languages=None):
    os.environ['METAURO_KEEP_LATEST_COHORT'] = 'yes'
    os.environ['MPLBACKEND'] = 'Agg'
    sys.path.insert(0, str(ROOT))
    from automation.sync_sources import assembled_source
    from summary_outputs import cache_dir, summary_dir
    source_root = assembled_source(ROOT, source)
    os.environ['METAURO_SUMMARY_SOURCE_ROOT'] = str(source_root)
    sys.path[:0] = [str(ROOT / 'runtime_code' / source), str(source_root)]
    import meta_analysis
    import analysis_settings_backend as backend
    import pandas as pd

    spec = importlib.util.spec_from_file_location('metauro_cohort_summary_builder',
        ROOT / 'sources' / source / 'scripts' / 'build_summary_plots.py')
    renderer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = renderer
    spec.loader.exec_module(renderer)
    audits = []
    original = renderer.summarize_cache

    def summarize(path, disease, exposure, dietary):
        with backend.latest_cohort_pooling(True) as context:
            row = original(path, disease, exposure, dietary)
        audits.append({'disease':disease,'exposure':exposure,'dataset':'dietary' if dietary else 'combined',**context.get('audit',{})})
        if row is None:
            return None
        selected = pd.DataFrame(context.get('rows', []))
        if selected.empty:
            return None
        cases = renderer.analysis_cases(selected, disease)
        row['number studies'] = len(selected)
        row['total N'] = int(pd.to_numeric(selected['Sample Size'], errors='coerce').fillna(0).sum())
        row['total Cases'] = int(cases.sum()) if cases.notna().all() else None
        return row

    renderer.summarize_cache = summarize
    from matplotlib.figure import Figure
    original_save = Figure.savefig

    def save_latest_figure(figure, *args, **kwargs):
        if not any(text.get_text() == 'Latest publication per cohort' for text in figure.texts):
            figure.text(.01, .005, 'Latest publication per cohort', ha='left', va='bottom', fontsize=7, color='#607D8B')
        return original_save(figure, *args, **kwargs)

    Figure.savefig = save_latest_figure
    original_finalize = renderer.finalize_build

    def finalize(source_root, folder, manifest):
        import hashlib
        from cohort_summary_support import bind_cohort_manifest
        bind_cohort_manifest(manifest, ROOT)
        manifest['cohort_selection_audits'] = audits
        manifest['keep_latest_cohort'] = True
        manifest['cohort_policy'] = 'Latest definitely dated publication per identified cohort; numbered cohorts, ties and unknowns retained separately.'
        return original_finalize(source_root, folder, manifest)

    renderer.finalize_build = finalize
    original_workbook = renderer.workbook

    def workbook(path, rows, *args, **kwargs):
        original_workbook(path, rows, *args, **kwargs)
        from openpyxl import load_workbook
        book = load_workbook(path)
        sheet = book.create_sheet('Cohort selection')
        sheet.append(['Keep latest publication per cohort', 'Yes'])
        sheet.append(['Unknown cohort/date or tied publication dates', 'Retained'])
        sheet.append(['Recorded source evidence', 'All records preserved; include-all is the default.'])
        sheet.append(['Disease','Exposure','Earlier PMID','Retained PMID','Cohorts'])
        disease = args[0] if args else kwargs.get('disease')
        dataset = 'dietary' if '_dietary' in Path(path).stem else 'combined'
        for audit in audits:
            if disease and audit['disease'] != disease or audit['dataset'] != dataset: continue
            for item in audit.get('excluded', []):
                sheet.append([audit['disease'], audit['exposure'], item['pmid'], item['retained_pmid'], '; '.join(item['cohort_ids'])])
        sheet.column_dimensions['A'].width = 48
        sheet.column_dimensions['B'].width = 80
        book.save(path)

    renderer.workbook = workbook
    return renderer.build(cache_dir(ROOT, source, mode), summary_dir(ROOT, source, mode), languages)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=('metaPee', 'MetaVir'), required=True)
    parser.add_argument('--mode', choices=('yes', 'no'), required=True)
    parser.add_argument('--languages', nargs='+')
    args = parser.parse_args()
    manifest = build(args.source, args.mode, tuple(args.languages) if args.languages else None)
    print(json.dumps({'source': args.source, 'mode': args.mode, 'scopes': len(manifest['scopes']), 'keep_latest_cohort': True}))
