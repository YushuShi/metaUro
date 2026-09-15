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
    'S2-REVIEW': 'Review article',
    'S2-NONPRIMARY': 'Not an eligible primary study',
    'S2-ANIMAL': 'Animal, laboratory, or preclinical research',
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


@lru_cache(maxsize=2)
def _build_selection_flow(source):
    if source not in {'metaPee', 'MetaVir'}:
        raise ValueError('Unknown study-selection cohort')
    urinary = source == 'metaPee'
    source_root = ROOT / 'sources' / source
    first, first_provenance = _read_packet(source_root / AUDIT_DIR / 'r1_selected_decisions.json', 'First-stage screening decisions')
    final_path = 'combined/effective_results.json' if urinary else 'combined/final_screening.json'
    final, final_provenance = _read_packet(source_root / final_path, 'Current eligibility and inclusion decisions')
    lineage, lineage_provenance = _read_packet(source_root / AUDIT_DIR / 'r1_precedence_manifest.json', 'Search-round lineage and precedence')
    provenance = [first_provenance, final_provenance, lineage_provenance]
    provisional_reports = None
    if not urinary:
        projection, projection_provenance = _read_packet(source_root / 'combined/provisional_ui/effective_screening.json', 'Provisional report display audit')
        included_pmids = {str(row['pmid']) for row in final['results'] if row['final_status'] in RETAINED_STATUSES}
        displayed_pmids = {str(row['pmid']) for row in projection['results'] if row['final_status'] in RETAINED_STATUSES}
        provisional_reports = len(displayed_pmids - included_pmids)
        provenance.append(projection_provenance)
    review_path = ROOT / 'evidence-revisions/2026-09-14/revision.json'
    review_note = ''
    if review_path.exists():
        revision, review_provenance = _read_packet(review_path, 'Applied human review decisions')
        if source in revision.get('sources', {}):
            review_note = ' Human review updates applied on 14 September 2026.'
            provenance.append(review_provenance)
    dedup_path = ROOT / 'evidence-revisions/2026-09-14-dataset-deduplication/revision.json'
    if dedup_path.exists():
        revision, dedup_provenance = _read_packet(dedup_path, 'Applied duplicate-article decisions')
        if source in revision.get('sources', {}):
            review_note += ' Duplicate-article decisions applied on 14 September 2026.'
            provenance.append(dedup_provenance)
    return {
        'label': 'Urinary tract cancers' if urinary else 'Prostate cancer',
        'unit': 'Unique reports (PubMed IDs)',
        'cohort_description': (
            'Current urinary tract review, using search rounds ending 22–29 August 2026 and adjudication saved on 7 September 2026.'
            if urinary else
            'Current prostate review, using search rounds through 29 August 2026 and report dispositions saved on 8 September 2026.'
        ) + review_note,
        'identified': None,
        'duplicate_removed': None,
        'automation_removed': None,
        'other_removed': None,
        'reports_sought': None,
        'reports_not_retrieved': None,
        'full_text_assessed': None,
        'full_text_excluded': None,
        'unique_studies': None,
        'provisional_reports': provisional_reports,
        **_report_counts(first['results'], final['results']),
        'notes': [
            'Each PubMed ID is counted once within this review. A report is retained when at least one exposure–cancer context is eligible; it is excluded only when every context is excluded.',
            'Detailed eligibility screening uses titles, abstracts, and metadata, with conditional full-text review. It must not be interpreted as full-text assessment of every report.',
            'Raw identification totals, duplicate removal, and complete full-text retrieval counts are not available for this assembled review cohort. These stages are shown as not recorded, not zero.',
            'Distinct studies cannot be counted reliably because reports from the same underlying study are not fully linked. The diagram therefore reports publications, not studies.',
            'Reports eligible for at least one quantitative synthesis are counted once; this is not the number of studies in an individual forest plot. Eligible reports with no poolable context remain retained.',
            (
                'The review covers all urinary cancer scopes and exposures. Counts do not change with display settings or related-exposure inclusion, and overlapping disease cohorts must not be added together.'
                if urinary else
                'The prostate review includes the shared screening cohort used by the general and aggressive prostate cancer views. Counts do not change with disease, exposure, or display settings.'
            ),
            *([] if urinary or not provisional_reports else [
                f'{provisional_reports} additional reports are displayed provisionally in the analysis and are not counted as included here. Quantitative eligibility here follows the saved adjudicated record; provisional analysis estimates can differ.',
            ]),
        ],
        'provenance': provenance,
        'search_cutoffs': sorted({item['publication_date_end'] for item in lineage['sources']}),
    }


def build_selection_flow(source):
    """Return a fresh public-view model without changing any evidence artifact."""
    source = {'urinary': 'metaPee', 'prostate': 'MetaVir'}.get(source, source)
    flow = deepcopy(_build_selection_flow(source))
    monthly = load_overlay(ROOT, source)
    if monthly:
        flow['cohort_description'] = flow['cohort_description'].replace('Current ', 'Historical ', 1)
        flow['monthly'] = {key: monthly.get(key) for key in ('search_through', 'human_review_pending', 'run_id')}
        flow['notes'].append(
            'Monthly searching is complete through ' + monthly['search_through'] +
            '. These new screening records are reported separately from the historical flow diagram. ' +
            str(monthly.get('human_review_pending', 0)) + ' exposure–disease contexts await human review.'
        )
    return flow
