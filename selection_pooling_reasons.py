"""Summarize recorded pooling restrictions without changing evidence decisions."""
from collections import Counter, defaultdict

UNKNOWN = 'Specific pooling reason not recorded'
LABELS = {
    10: 'Awaiting review or adjudication',
    20: 'Overlapping population or another estimate selected',
    30: 'Quality below the current pooling threshold',
    40: 'Missing quantitative information',
    50: 'Quantitative validation requirements not met',
    60: 'Disease subtype or exposure classification requires review',
}


def _codes(row):
    """Only affirmative saved flags/fields are used, never a missing pool flag."""
    codes = set()
    if any(row.get(key) is True for key in ('manual_review_pending', 'key_comparison_pending', 'awaiting_human_review', 'review_pending')):
        codes.add(10)
    if any(row.get(key) is True for key in ('hierarchy_overlap_unpooled', 'superseded_by_adjusted', 'overlap_unpooled')):
        codes.add(20)
    quality = str(row.get('Quality Score') or row.get('jbi_quality') or (row.get('jbi') or {}).get('grade') or '').casefold()
    if quality in {'fair', 'poor', 'low'}:
        codes.add(30)
    quantitative_fields = {'ci_lower', 'ci_upper', 'effect_size', 'effect_type', 'total_n', 'cases', 'sample_size', 'standard_error'}
    missing_keys = row.get('missing_key_fields') or []
    missing_keys = [missing_keys] if isinstance(missing_keys, str) else missing_keys
    blank = lambda value: value is None or str(value).strip().casefold() in {'', '-', 'na', 'n/a', 'none', 'nan', 'null'}
    missing_saved_value = any(key in row and blank(row[key]) for key in ('Effect Size', 'Effect Type', 'Sample Size'))
    no_saved_uncertainty = any(key in row for key in ('Lower CI', 'Upper CI', 'SE', 'P Value')) and all(blank(row.get(key)) for key in ('Lower CI', 'Upper CI', 'SE', 'P Value'))
    has_saved_values = any(key in row for key in ('Effect Size', 'Effect Type', 'Sample Size'))
    recorded_missing = not has_saved_values and (any(str(key).casefold() in quantitative_fields for key in missing_keys) or row.get('missing_quantities') or row.get('quantitative_information_complete') is False)
    if missing_saved_value or no_saved_uncertainty or recorded_missing:
        codes.add(40)
    if row.get('quantitative_valid') is False or row.get('numeric_validity_passed') is False:
        codes.add(50)
    if row.get('final_status') == 'ELIGIBLE_SUBTYPE_REVIEW':
        codes.add(60)
    # Structured policy issues carry the reason even when the display cache
    # has only a generic not-poolable flag.
    issues = (row.get('poolability_policy') or {}).get('remaining_issues') or []
    issues = [issues] if isinstance(issues, str) else issues
    for issue in issues:
        text = str(issue).casefold()
        if not has_saved_values and text in {'ci_lower', 'ci_upper', 'effect_size', 'effect_type', 'total_n', 'cases', 'sample_size', 'standard_error'}:
            codes.add(40)
        elif quality not in {'good', 'moderate'} and ('jbi grade' in text or 'quality threshold' in text):
            codes.add(30)
        elif 'invalid' in text or 'must be positive' in text or 'confidence interval' in text:
            codes.add(50)
    for field in ('disease_hierarchy_pooling_reason', 'disease_scope_pooling_reason', 'hierarchy_overlap_reason', 'subexposure_pooling_reason'):
        text = str(row.get(field) or '').casefold()
        unselected = any(row.get(key) is False for key in ('disease_hierarchy_pooling_selected', 'disease_scope_pooling_selected', 'subexposure_pooling_selected'))
        if unselected and ('overlap' in text or 'another estimate' in text or 'same publication' in text) and not text.startswith('selected eligible'):
            codes.add(20)
    return codes


def pooling_reason_counts(retained, pooled, current_rows=(), assessment_rows=()):
    """One reason category per retained, unpooled PMID, with current priority.

    A reason shared by every estimate is preferred. If recorded restrictions
    differ, list those together. An unexplained estimate keeps the publication
    unexplained; older assessment grades never override a current cache grade.
    """
    current, assessed = defaultdict(list), defaultdict(list)
    for rows, target in ((current_rows, current), (assessment_rows, assessed)):
        for row in rows:
            if str(row.get('final_status') or row.get('meta_analysis_status') or '').upper() == 'EXCLUDE':
                continue
            target[str(row.get('PMID') or row.get('pmid') or '')].append(row)
    counts = Counter()
    for pmid in set(map(str, retained)) - set(map(str, pooled)):
        saved = current.get(pmid, [])
        records = saved or assessed.get(pmid, [])
        reasons = [_codes(row) for row in records]
        common = set.intersection(*reasons) if reasons else set()
        if common:
            label = LABELS[min(common)]
        elif reasons and all(reasons):
            label = 'Different restrictions across estimates: ' + '; '.join(LABELS[code].lower() for code in sorted(set.union(*reasons)))
        else:
            label = UNKNOWN
        counts[label] += 1
    return [{'label': label, 'count': count} for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
