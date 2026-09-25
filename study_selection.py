"""Read-only, report-level selection counts from the current screening audit.

The saved pipeline screens exposure–cancer contexts and conditionally reviews
full text. It is not a complete PRISMA retrieval ledger. Missing retrieval and
study-linkage counts remain unknown; context counts are never called studies.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from automation.sync_sources import load_overlay


ROOT = Path(__file__).resolve().parent
AUDIT_DIR = Path('data/claude_gemini_screening/all_history_through_2026-08-29')
RETAINED_STATUSES = {'INCLUDE_META', 'ELIGIBLE_NOT_POOLABLE', 'ELIGIBLE_SUBTYPE_REVIEW'}
REASON_LABELS = {
    'S2-REVIEW': 'Review or meta-analysis without an eligible primary estimate',
    'S2-NONPRIMARY': 'Not an eligible primary study',
    'S2-ANIMAL': 'Animal, laboratory, or preclinical study',
    'S2-EXPOSURE-MISMATCH': 'Exposure outside the review scope',
    'S2-COMPOUND': 'Target exposure inseparable from a mixture',
    'S2-CANCER-MISMATCH': 'Cancer outside the review scope',
    'S2-WRONG-OUTCOME': 'Outcome outside the review scope',
    'S2-POSTDIAGNOSIS': 'Exposure assessed only after diagnosis',
    'S2-BENIGN-ONLY': 'Benign or nonmalignant disease only',
    'S2-MENDELIAN-RANDOMIZATION': 'Mendelian randomization or genetic instruments',
    'S2-GENOTYPE-INTERACTION': 'Genotype-specific effect or gene–exposure interaction',
    'S2-GENETIC-FOCUS': 'Genetic focus without a population-wide exposure estimate',
    'S2-DIAGNOSTIC-MECHANISTIC': 'Diagnostic or mechanistic research',
    'S2-NO-TARGET-ESTIMATE': 'No separable target incidence estimate',
    'S2-DUPLICATE': 'Duplicate or overlapping analysis at eligibility review',
}


def _report_counts(first_rows, final_rows):
    """Partition PMIDs, retaining a report if any reviewed context is retained."""
    first_contexts = {}
    for row in first_rows:
        context_id = row['context_id']
        if context_id in first_contexts:
            raise ValueError('Duplicate first-screen context in selection audit')
        if row['decision'] not in {'PASS', 'FAIL'}:
            raise ValueError('Unknown first-screen decision in selection audit')
        if not str(row.get('pmid') or '').strip():
            raise ValueError('Missing PubMed identifier in selection audit')
        first_contexts[context_id] = row

    final_contexts = {}
    by_report = defaultdict(list)
    for row in final_rows:
        context_id = row['context_id']
        if row['final_status'] not in RETAINED_STATUSES | {'EXCLUDE'}:
            raise ValueError('Unknown eligibility status in selection audit')
        prior = first_contexts.get(context_id)
        if not prior or str(prior['pmid']) != str(row['pmid']):
            raise ValueError('Eligibility cohort does not match first-screen passes')
        # Some saved projections contain first-screen exclusions as well as
        # detailed decisions, and multiple estimate rows for one context.
        if prior['decision'] == 'FAIL':
            if row['final_status'] != 'EXCLUDE':
                raise ValueError('Retained report context did not pass first screening')
            continue
        final_contexts[context_id] = row
        by_report[str(row['pmid'])].append(row)

    passed_contexts = {key for key, row in first_contexts.items() if row['decision'] == 'PASS'}
    if set(final_contexts) != passed_contexts:
        raise ValueError('Eligibility audit does not cover every first-screen pass')

    screened = {str(row['pmid']) for row in first_rows}
    assessed = set(by_report)
    retained = {
        pmid for pmid, rows in by_report.items()
        if any(row['final_status'] in RETAINED_STATUSES for row in rows)
    }
    poolable = {
        pmid for pmid, rows in by_report.items()
        if any(row['final_status'] == 'INCLUDE_META' for row in rows)
    }
    excluded = assessed - retained
    reason_counts = Counter()
    for pmid in excluded:
        codes = {row.get('exclusion_code') or 'UNSPECIFIED' for row in by_report[pmid]}
        if len(codes) > 1:
            label = 'Multiple reasons across exposure or cancer contexts'
        else:
            code = next(iter(codes))
            label = REASON_LABELS.get(code, 'Other recorded eligibility reason' if code != 'UNSPECIFIED' else 'Reason not recorded')
        reason_counts[label] += 1

    return {
        'screened': len(screened),
        'screening_excluded': len(screened - assessed),
        'assessed': len(assessed),
        'eligibility_excluded': len(excluded),
        'included_reports': len(retained),
        'poolable_reports': len(poolable),
        'nonpoolable_reports': len(retained - poolable),
        'screening_contexts': len(first_contexts),
        'eligibility_contexts': len(final_contexts),
        'reasons': [{'label': label, 'count': count} for label, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))],
    }


def _read_packet(path, label):
    raw = path.read_bytes()
    return json.loads(raw), {
        'label': label,
        'path': str(path.relative_to(ROOT)),
        'sha256': hashlib.sha256(raw).hexdigest(),
    }


SOURCE_ALIASES = {'urinary': 'metaPee', 'prostate': 'MetaVir'}
URINARY_PARENTS = {
    'urothelial_carcinoma_of_the_bladder': 'bladder_cancer',
    'squamous_cell_carcinoma_of_the_bladder': 'bladder_cancer',
    'adenocarcinoma_of_the_bladder': 'bladder_cancer',
    'small_cell_carcinoma_of_the_bladder': 'bladder_cancer',
    'renal_cell_carcinoma': 'kidney_and_renal_pelvis_cancer',
    'clear_cell_renal_cell_carcinoma': 'renal_cell_carcinoma',
    'papillary_renal_cell_carcinoma': 'renal_cell_carcinoma',
    'chromophobe_renal_cell_carcinoma': 'renal_cell_carcinoma',
    'urothelial_carcinoma_of_the_renal_pelvis': 'kidney_and_renal_pelvis_cancer',
    'renal_sarcoma': 'kidney_and_renal_pelvis_cancer',
}


def _source_name(source):
    source = SOURCE_ALIASES.get(source, source)
    if source not in {'metaPee', 'MetaVir'}:
        raise ValueError('Unknown study-selection cohort')
    return source


@lru_cache(maxsize=2)
def _selection_audit(source):
    """Load and reconcile the immutable context-level screening records."""
    source = _source_name(source)
    source_root = ROOT / 'sources' / source
    first, first_provenance = _read_packet(
        source_root / AUDIT_DIR / 'r1_selected_decisions.json',
        'First-stage screening decisions',
    )
    final_path = 'combined/effective_results.json' if source == 'metaPee' else 'combined/final_screening.json'
    final, final_provenance = _read_packet(
        source_root / final_path,
        'Current eligibility and inclusion decisions',
    )
    first_contexts = {}
    for row in first['results']:
        context_id = row.get('context_id')
        if not context_id or context_id in first_contexts:
            raise ValueError('Duplicate or missing first-screen context in selection audit')
        if row.get('decision') not in {'PASS', 'FAIL'}:
            raise ValueError('Unknown first-screen decision in selection audit')
        first_contexts[context_id] = row
    covered_passes = set()
    for row in final['results']:
        prior = first_contexts.get(row.get('context_id'))
        if not prior or str(prior.get('pmid')) != str(row.get('pmid')):
            raise ValueError('Eligibility cohort does not match first screening')
        if row.get('final_status') not in RETAINED_STATUSES | {'EXCLUDE'}:
            raise ValueError('Unknown eligibility status in selection audit')
        if prior['decision'] == 'PASS':
            covered_passes.add(row['context_id'])
        elif row['final_status'] != 'EXCLUDE':
            raise ValueError('Retained context did not pass first screening')
    expected_passes = {key for key, row in first_contexts.items() if row['decision'] == 'PASS'}
    if covered_passes != expected_passes:
        raise ValueError('Eligibility audit does not cover every first-screen pass')
    provenance = [first_provenance, final_provenance]
    for relative, label in (
        ('evidence-revisions/2026-09-14/revision.json', 'Applied human review decisions'),
        ('evidence-revisions/2026-09-14-dataset-deduplication/revision.json', 'Applied duplicate-article decisions'),
    ):
        path = ROOT / relative
        if path.exists():
            revision, revision_provenance = _read_packet(path, label)
            if source in revision.get('sources', {}):
                provenance.append(revision_provenance)
    return {
        'first': tuple(first['results']),
        'final': tuple(final['results']),
        'first_contexts': first_contexts,
        'provenance': tuple(provenance),
    }


@lru_cache(maxsize=2)
def _disease_contexts(source):
    source = _source_name(source)
    catalog = json.loads((ROOT / 'sources' / source / 'static/seer-baseline.json').read_text())
    labels = {row['id']: row['label'] for row in catalog['diseases']}
    if source != 'metaPee':
        return {label: (label,) for label in labels.values()}
    children = defaultdict(list)
    for child, parent in URINARY_PARENTS.items():
        if child in labels and parent in labels:
            children[parent].append(child)

    def descendants(scope):
        result = []
        for child in children.get(scope, []):
            result.append(child)
            result.extend(descendants(child))
        return result

    return {
        label: tuple([label] + [labels[item] for item in descendants(scope)])
        for scope, label in labels.items()
    }


@lru_cache(maxsize=2)
def _current_pool_contexts(source):
    """Read direct-exposure caches and keep only contexts with a current pool."""
    source = _source_name(source)
    root = (
        ROOT / 'sources/metaPee/combined/provisional_ui/subexposure_views/no/Cached_results'
        if source == 'metaPee' else ROOT / 'sources/MetaVir/Subexposure_results/no'
    )
    contexts = _disease_contexts(source)
    result = []
    for path in sorted(root.glob('*/*_core.json')):
        payload = json.loads(path.read_text())
        disease = str(payload.get('disease') or '').strip()
        exposure = str(payload.get('exposure') or '').strip()
        if not disease or not exposure or disease == 'Aggressive prostate cancer' or not payload.get('headline'):
            continue
        poolable = set()
        for row in payload.get('studies') or []:
            selected = row.get('subexposure_pooling_selected') is True
            scope_selected = (
                row.get('disease_hierarchy_pooling_selected')
                if source == 'metaPee' else row.get('disease_scope_pooling_selected')
            )
            pmid = str(row.get('PMID') or '').strip()
            if pmid and selected and scope_selected is not False:
                poolable.add(pmid)
        if poolable:
            result.append((disease, exposure, contexts.get(disease, (disease,)), tuple(sorted(poolable))))
    return tuple(sorted(result, key=lambda item: (item[0].casefold(), item[1].casefold())))


@lru_cache(maxsize=2)
def _selection_catalog(source):
    """Map diseases to direct exposures with a current saved pooled result."""
    choices = defaultdict(list)
    for disease, exposure, _contexts, _pmids in _current_pool_contexts(_source_name(source)):
        choices[disease].append(exposure)
    return tuple((disease, tuple(exposures)) for disease, exposures in choices.items())


def selection_catalog(source):
    """Return fresh disease/exposure choices suitable for public selectors."""
    return {disease: list(exposures) for disease, exposures in _selection_catalog(_source_name(source))}


def _reason_label(codes):
    if not codes:
        return 'Reason not recorded'
    if len(codes) > 1:
        return 'Multiple recorded exclusion criteria'
    code = next(iter(codes))
    return REASON_LABELS.get(code, {
        'S1-NOT-DIRECT': 'No direct nutritional exposure–cancer evidence',
        'CURATOR-REDUNDANT-DATASET': 'Duplicate or redundant dataset',
        'HUMAN-NO-DIRECT-EXPOSURE-ESTIMATE': 'No direct exposure estimate',
    }.get(code, 'Other recorded eligibility criterion'))


def _context_counts(audit, diseases, exposure, current_pool_pmids):
    diseases = set(diseases)
    first = [
        row for row in audit['first']
        if row.get('cancer') in diseases and row.get('exposure') == exposure
    ]
    context_ids = {row['context_id'] for row in first}
    final = [row for row in audit['final'] if row.get('context_id') in context_ids]
    first_by_pmid = defaultdict(list)
    for row in first:
        first_by_pmid[str(row['pmid'])].append(row)
    screened = set(first_by_pmid)
    assessed = {
        pmid for pmid, rows in first_by_pmid.items()
        if any(row['decision'] == 'PASS' for row in rows)
    }
    final_by_pmid = defaultdict(list)
    for row in final:
        if audit['first_contexts'][row['context_id']]['decision'] == 'PASS':
            final_by_pmid[str(row['pmid'])].append(row)

    retained = {
        pmid for pmid, rows in final_by_pmid.items()
        if any(row['final_status'] in RETAINED_STATUSES for row in rows)
    }
    poolable = set(current_pool_pmids)
    if not poolable <= retained:
        raise ValueError('Current pooled reports do not reconcile with the screening audit')

    excluded = assessed - retained
    reason_counts = Counter()
    for pmid in excluded:
        codes = {
            row.get('exclusion_code') or 'UNSPECIFIED'
            for row in final_by_pmid.get(pmid, [])
            if row.get('final_status') == 'EXCLUDE'
        }
        if codes == {'UNSPECIFIED'}:
            codes.clear()
        reason_counts[_reason_label(codes)] += 1
    initial_excluded = screened - assessed
    initial_reasons = []
    if initial_excluded:
        initial_reasons.append({
            'label': 'No direct nutritional exposure–cancer evidence at initial screening',
            'count': len(initial_excluded),
        })
    return {
        'screened': len(screened),
        'screening_excluded': len(initial_excluded),
        'assessed': len(assessed),
        'eligibility_excluded': len(excluded),
        'included_reports': len(retained),
        'poolable_reports': len(poolable),
        'nonpoolable_reports': len(retained - poolable),
        'initial_reasons': initial_reasons,
        'reasons': [
            {'label': label, 'count': count}
            for label, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


@lru_cache(maxsize=256)
def _build_selection_flow(source, disease=None, exposure=None):
    source = _source_name(source)
    choices = selection_catalog(source)
    preferred = 'Bladder cancer' if source == 'metaPee' else 'Prostate cancer'
    selected_disease = disease or (preferred if preferred in choices else next(iter(choices), ''))
    exposures = choices.get(selected_disease, [])
    if not exposures:
        return {
            'available': False,
            'disease': selected_disease,
            'exposure': None,
            'exposures': [],
            'notes': [],
        }
    selected_exposure = exposure if exposure in exposures else exposures[0]
    audit = _selection_audit(source)
    selected_context = next(
        item for item in _current_pool_contexts(source)
        if item[0] == selected_disease and item[1] == selected_exposure
    )
    return {
        'available': True,
        'disease': selected_disease,
        'exposure': selected_exposure,
        'exposures': exposures,
        'context_diseases': list(selected_context[2]),
        'include_subexposures': 'no',
        'unit': 'Unique reports (PubMed IDs)',
        'scope_note': '',
        'notes': [],
        'provenance': list(audit['provenance']),
        **_context_counts(audit, selected_context[2], selected_exposure, selected_context[3]),
    }


def build_selection_flow(source, disease=None, exposure=None):
    """Return a fresh exposure-specific flow without changing evidence files."""
    source = _source_name(source)
    flow = deepcopy(
        _build_selection_flow(source)
        if disease is None and exposure is None else
        _build_selection_flow(source, disease, exposure)
    )
    monthly = load_overlay(ROOT, source)
    if monthly:
        if 'cohort_description' in flow:
            flow['cohort_description'] = flow['cohort_description'].replace('Current ', 'Historical ', 1)
        flow['monthly'] = {key: monthly.get(key) for key in ('search_through', 'human_review_pending', 'run_id')}
        flow.setdefault('notes', []).append(
            'Monthly searching is complete through ' + monthly['search_through'] +
            '. New screening records remain separate until the monthly review is released. ' +
            str(monthly.get('human_review_pending', 0)) + ' exposure–disease contexts await human review.'
        )
    return flow
