"""Read-only audit of exact repeated N/effect values in metaUro's combined views.

Usage: python audit_duplicate_sample_effect.py --root /path/to/metaUro --out /path/to/report
Only the report directory is written. No evidence is changed.
"""
import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal, InvalidOperation
import hashlib
from itertools import combinations
import json
from pathlib import Path


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip().replace(',', ''))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def numeric_text(value):
    result = number(value)
    return format(result.normalize(), 'f') if result is not None else ''


def fallback(row, *fields):
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip().casefold() not in {'', 'na', 'n/a', 'nan', 'none', 'null', '-'}:
            return value
    return None


def bundle(row, source, basis):
    original = source == 'urinary' and basis == 'table'
    prefix = 'Original ' if original else ''
    sample = fallback(row, 'Participants', 'Sample Size') if basis == 'table' else row.get('Sample Size')
    return {
        'sample_size': numeric_text(sample),
        'effect_size': numeric_text(fallback(row, prefix + 'Effect Size', 'Effect Size')),
        'effect_type': str(fallback(row, prefix + 'Effect Type', 'Effect Type') or ''),
        'lower_ci': numeric_text(fallback(row, prefix + 'Lower CI', 'Lower CI')),
        'upper_ci': numeric_text(fallback(row, prefix + 'Upper CI', 'Upper CI')),
    }


def receipt(row, source, basis):
    return {
        'pmid': str(row.get('PMID') or ''),
        'record_id': str(row.get('Record ID') or row.get('Effect ID') or ''),
        'context_id': str(row.get('context_id') or ''),
        'study': str(row.get('Study') or ''),
        'title': str(row.get('Title') or row.get('Reference') or ''),
        'direct_exposure': str(fallback(row, 'Direct Exposure', 'direct_exposure', 'subexposure_source') or ''),
        'disease_evidence_source': str(fallback(row, 'Disease Evidence Source', 'Disease Scope', 'Disease Endpoint') or ''),
        'comparison': str(row.get('comparison_type') or ''),
        'population': str(row.get('Population') or ''),
        'timing': str(row.get('Timing') or ''),
        'stage': str(row.get('Stage') or ''),
        'quality': str(row.get('Quality Score') or ''),
        'final_status': str(row.get('final_status') or ''),
        'display_only': row.get('display_only'),
        'primary_estimate': row.get('Primary Estimate'),
        'policy_poolable': row.get('policy_poolable'),
        'pool_in_parent_meta_analysis': row.get('pool_in_parent_meta_analysis'),
        'subexposure_inherited': row.get('subexposure_inherited'),
        'disease_hierarchy_inherited': row.get('disease_hierarchy_inherited'),
        'scientific_pooling_eligible': row.get('scientific_pooling_eligible'),
        'subexposure_pooling_selected': row.get('subexposure_pooling_selected'),
        'disease_scope_pooling_selected': row.get('disease_scope_pooling_selected', row.get('disease_hierarchy_pooling_selected')),
        **bundle(row, source, basis),
    }


def stable(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def identity(row):
    # Ignore projection/pooling flags that legitimately differ between yes/no.
    fields = ['pmid', 'record_id', 'context_id', 'direct_exposure', 'disease_evidence_source',
              'comparison', 'population', 'timing', 'stage', 'sample_size', 'effect_size',
              'effect_type', 'lower_ci', 'upper_ci']
    return hashlib.sha256(stable({field: row[field] for field in fields}).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    root, out = args.root.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        'urinary': 'sources/metaPee/combined/provisional_ui/subexposure_views/{mode}/Cached_results',
        'prostate': 'sources/MetaVir/Subexposure_results/{mode}',
    }
    checksums, coverage, occurrences, merged = {}, [], [], {}
    for source, template in paths.items():
        for mode in ('no', 'yes'):
            directory = root / template.format(mode=mode)
            for path in sorted(directory.glob('*/*_incidence_true_core.json')):
                raw = path.read_bytes()
                checksums[str(path.relative_to(root))] = hashlib.sha256(raw).hexdigest()
                packet = json.loads(raw, parse_float=Decimal)
                rows = packet.get('studies', [])
                alternate = path.with_name(path.name.replace('_core.json', '_all.json'))
                alt_raw = alternate.read_bytes()
                checksums[str(alternate.relative_to(root))] = hashlib.sha256(alt_raw).hexdigest()
                assert rows == json.loads(alt_raw, parse_float=Decimal).get('studies', []), 'Different all/core evidence: ' + str(path)
                exposure = packet.get('exposure') or path.parent.name
                disease = packet.get('disease') or packet.get('cancer')
                assert disease, path
                for basis in ('table', 'analysis'):
                    indexed = defaultdict(list)
                    valid = 0
                    for position, row in enumerate(rows):
                        evidence = receipt(row, source, basis)
                        n, effect = number(evidence['sample_size']), number(evidence['effect_size'])
                        if n is None or n <= 0 or effect is None or effect <= 0:
                            continue
                        valid += 1
                        evidence['row_number'] = position + 1
                        indexed[(evidence['sample_size'], evidence['effect_size'])].append(evidence)
                    groups = [(key, items) for key, items in indexed.items() if len(items) > 1]
                    coverage.append(dict(source=source, mode=mode, exposure=exposure, disease=disease,
                                         basis=basis, evidence_rows=len(rows), rows_with_numeric_n_and_effect=valid,
                                         matching_groups=len(groups), matching_pairs=sum(len(x)*(len(x)-1)//2 for _,x in groups),
                                         cache_path=str(path.relative_to(root))))
                    for (n, effect), items in groups:
                        occurrences.append(dict(source=source, mode=mode, exposure=exposure, disease=disease,
                                                basis=basis, sample_size=n, effect_size=effect, rows=items))
                        for left, right in combinations(items, 2):
                            if identity(left) > identity(right):
                                left, right = right, left
                            key = (source, exposure, disease, n, effect, identity(left), identity(right))
                            if key not in merged:
                                merged[key] = dict(source=source, exposure=exposure, disease=disease,
                                    sample_size=n, effect_size=effect, modes=set(), bases=set(),
                                    same_pmid=bool(left['pmid']) and left['pmid']==right['pmid'],
                                    same_effect_type=bool(left['effect_type']) and left['effect_type'].casefold()==right['effect_type'].casefold(),
                                    same_complete_ci=bool(left['lower_ci'] and left['upper_ci']) and
                                        (left['lower_ci'],left['upper_ci'])==(right['lower_ci'],right['upper_ci']),
                                    left=left, right=right, cache_paths=set(), occurrences=[])
                            found=merged[key]
                            found['modes'].add(mode)
                            found['bases'].add(basis)
                            found['cache_paths'].add(str(path.relative_to(root)))
                            found['occurrences'].append(dict(mode=mode,basis=basis,left_row=left['row_number'],right_row=right['row_number'],
                                left_pooling=left['scientific_pooling_eligible'],right_pooling=right['scientific_pooling_eligible']))
    pairs=[]
    for index, key in enumerate(sorted(merged), 1):
        pair=merged[key]
        for field in ('modes','bases','cache_paths'):
            pair[field]=sorted(pair[field])
        pair['match_id']=f'DUP-{index:03d}'
        pairs.append(pair)
    summary={}
    for source in paths:
        for mode in ('no','yes'):
            for basis in ('table','analysis'):
                subset=[r for r in coverage if (r['source'],r['mode'],r['basis'])==(source,mode,basis)]
                match=[r for r in pairs if r['source']==source and mode in r['modes'] and basis in r['bases']
                       and any(x['mode']==mode and x['basis']==basis for x in r['occurrences'])]
                summary[f'{source}/{mode}/{basis}']={
                    'scope_combinations':len(subset), 'evidence_rows':sum(r['evidence_rows'] for r in subset),
                    'numeric_rows':sum(r['rows_with_numeric_n_and_effect'] for r in subset),
                    'combinations_with_matches':sum(r['matching_groups']>0 for r in subset),
                    'matching_groups':sum(r['matching_groups'] for r in subset),
                    'matching_pairs':sum(r['matching_pairs'] for r in subset),
                    'same_pmid_pairs':sum(r['same_pmid'] for r in match),
                    'different_pmid_pairs':sum(not r['same_pmid'] for r in match),
                }
    for relative, expected in checksums.items():
        assert hashlib.sha256((root/relative).read_bytes()).hexdigest()==expected, 'Evidence changed during audit: '+relative
    audit={'root':str(root),'method':'Exact numeric N and effect equality, excluding missing/nonpositive values; current combined serving evidence, all saved quality levels; both hierarchy modes; all/core rows verified equal and counted once.',
           'basis_definitions':{'table':'Urinary: Original Effect Size fallback Effect Size; prostate: Effect Size. N: Participants fallback Sample Size.',
                                'analysis':'Stored Sample Size and Effect Size before optional user risk/effect conversion.'},
           'summary':summary,'distinct_pair_value_matches_across_modes_and_bases':len(pairs),
           'pairs':pairs,'group_occurrences':occurrences,'coverage':coverage,'source_file_sha256':checksums}
    (out/'audit.json').write_text(json.dumps(audit,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
    for basis, filename in [('table','matching_evidence_pairs.csv'),('analysis','analysis_value_matches.csv')]:
        with (out/filename).open('w',newline='',encoding='utf-8-sig') as handle:
            columns=['match_id','source','exposure','disease','sample_size','effect_size','modes','bases','same_pmid','same_effect_type','same_complete_ci']
            evidence_columns=[key for key in receipt({},'urinary','table') if key not in {'sample_size','effect_size'}]
            writer=csv.DictWriter(handle,fieldnames=columns+[side+'_'+key for side in ('left','right') for key in evidence_columns]+['cache_paths'])
            writer.writeheader()
            for pair in pairs:
                if basis not in pair['bases']:
                    continue
                row={key:'; '.join(pair[key]) if isinstance(pair[key],list) else pair[key] for key in columns}
                row['modes']='; '.join(sorted({item['mode'] for item in pair['occurrences'] if item['basis']==basis}))
                for side in ('left','right'):
                    row.update({side+'_'+key:pair[side][key] for key in evidence_columns})
                row['cache_paths']='; '.join(pair['cache_paths'])
                writer.writerow(row)
    with (out/'scope_coverage.csv').open('w',newline='',encoding='utf-8-sig') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(coverage[0]))
        writer.writeheader()
        writer.writerows(coverage)
    print(json.dumps({'summary':summary,'merged_pairs':len(pairs)},indent=2))


if __name__=='__main__':
    main()
