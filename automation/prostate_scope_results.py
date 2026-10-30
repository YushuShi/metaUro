"""Rebuild the prostate evidence restriction from the current parent caches."""
from __future__ import annotations
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import warnings
from prostate_scopes import GENERAL, LABEL, SCOPE_ID, VERSION, project_payload, install_calculations


def calculate(parent, memo):
    import pandas as pd
    import meta_analysis
    from scripts.build_combined_results import numeric_frame
    from scripts.subexposure_policy import pooling_rows
    from scripts.monthly_evidence_update import json_compatible
    install_calculations(meta_analysis)
    child = project_payload(parent)
    signature = hashlib.sha256(json.dumps([parent['exposure'],child['studies']],sort_keys=True).encode()).hexdigest()
    if signature not in memo:
        rows = child['studies']
        candidates = pooling_rows(rows)
        frame = numeric_frame(candidates)
        if not frame.empty:
            frame = meta_analysis.filter_curated_meta_analysis_exclusions(frame, LABEL, parent['exposure'], 'Incidence')
            if hasattr(meta_analysis, 'human_pooling_rows'):
                frame = meta_analysis.human_pooling_rows(frame)
        if not frame.empty:
            with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
                warnings.simplefilter('ignore')
                result = meta_analysis.perform_meta_analysis(frame, LABEL,
                    parent['exposure'], outcome='Incidence', exclude_meta=True,
                    df_all=numeric_frame(rows), generate_plots=False)
            if result.get('error') or result.get('success') is False:
                raise ValueError(f"Failed derived prostate pool: {parent['exposure']}: {result.get('error')}")
        else:
            result = {'headline': None, 'summary_html':'<p>No studies meet the pooling criteria.</p>'}
        for field in ('studies','screening_stats'):
            result.pop(field, None)
        # Interactive plots are rendered with the visitor's actual filters.
        for field in ('plot_url','funnel_plot_url','baujat_plot_url'):
            result[field] = None
        memo[signature] = (json_compatible(result), len(frame))
    child.update(copy.deepcopy(memo[signature][0]))
    if isinstance(child.get('subexposure_projection'), dict):
        child['subexposure_projection']['pooling_rows'] = memo[signature][1]
    return child


def materialize(repo, destination=None):
    """Write additive derived caches; never change parent rows or their gates."""
    repo = Path(repo)
    destination = Path(destination or repo)
    memo = {}; report = []
    for profile in ('combined','Subexposure_results/no','Subexposure_results/yes'):
        for path in sorted((repo/profile).glob('*/prostate_cancer_incidence_true_*.json')):
            parent = json.loads(path.read_text())
            child = calculate(parent, memo)
            relative = path.relative_to(repo).with_name(path.name.replace('prostate_cancer_', SCOPE_ID+'_', 1))
            target = destination/relative
            raw = json.dumps(child,indent=2,ensure_ascii=False,allow_nan=False)+'\n'
            if not target.exists() or target.read_text() != raw:
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text(raw)
            report.append({'file':str(relative),'parent_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'retained_rows':len(child['studies']),
                'excluded_aggressive_rows':child['evidence_scope_projection']['excluded_aggressive_rows']})
    return report
