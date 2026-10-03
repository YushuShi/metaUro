"""Read-only publication counts from MetaVir's tracked screening and pooled evidence."""
from collections import Counter, defaultdict
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from selection_pooling_reasons import pooling_reason_counts

ROOT = Path(__file__).resolve().parent
AUDIT_DIR = Path('data/claude_gemini_screening/all_history_through_2026-08-29')
RETAINED_STATUSES = {'INCLUDE_META', 'ELIGIBLE_NOT_POOLABLE', 'ELIGIBLE_SUBTYPE_REVIEW'}

def _source_name(source):
    if source != 'MetaVir':
        raise ValueError('Unknown study-selection cohort')
    return source

def _disease_contexts(source):
    catalog = json.loads((ROOT / 'static/seer-baseline.json').read_text())
    return {row['label']: (row['label'],) for row in catalog['diseases']}

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

def _read_packet(path, label):
    raw = path.read_bytes()
    return json.loads(raw), {
        'label': label,
        'path': str(path.relative_to(ROOT)),
        'sha256': hashlib.sha256(raw).hexdigest(),
    }

@lru_cache(maxsize=2)
def _selection_audit(source):
    """Load and reconcile the immutable context-level screening records."""
    source = _source_name(source)
    source_root = ROOT
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
def _current_pool_packets(source):
    """Read the saved pool and its recorded pooling restrictions together."""
    source = _source_name(source)
    root = (
        ROOT / 'sources/metaPee/combined/provisional_ui/subexposure_views/no/Cached_results'
        if source == 'metaPee' else ROOT / 'Subexposure_results/no'
    )
    result = {}
    for path in sorted(root.glob('*/*_core.json')):
        payload, provenance = _read_packet(path, 'Current direct-exposure pool and recorded restrictions')
        disease = str(payload.get('disease') or '').strip()
        exposure = str(payload.get('exposure') or '').strip()
        if not disease or not exposure or disease == 'Aggressive prostate cancer' or not payload.get('headline'):
            continue
        result[(disease, exposure)] = (payload, provenance)
    return result

@lru_cache(maxsize=2)
def _current_pool_contexts(source):
    """Keep only contexts with an actual saved pool."""
    source = _source_name(source)
    contexts = _disease_contexts(source)
    result = []
    for (disease, exposure), (payload, _provenance) in _current_pool_packets(source).items():
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

def _context_counts(audit, diseases, exposure, current_pool_pmids, current_rows=()):
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
        'nonpoolable_reasons': pooling_reason_counts(
            retained, poolable, current_rows,
            [row for rows in final_by_pmid.values() for row in rows if row['final_status'] in RETAINED_STATUSES],
        ),
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
    packet, pool_provenance = _current_pool_packets(source)[(selected_disease, selected_exposure)]
    try:
        counts = _context_counts(audit, selected_context[2], selected_exposure, selected_context[3], packet.get('studies') or [])
    except ValueError as exc:
        if str(exc) != 'Current pooled reports do not reconcile with the screening audit':
            raise
        # Later saved estimates cannot be assigned invented screening decisions.
        return {'available': False, 'disease': selected_disease,
                'exposure': selected_exposure, 'exposures': exposures, 'notes': []}
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
        'provenance': [*audit['provenance'], pool_provenance],
        **counts,
    }


def build_selection_flow(disease=None, exposure=None):
    return deepcopy(_build_selection_flow('MetaVir', disease, exposure))
