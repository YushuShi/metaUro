"""Source-reviewed additions and scoped corrections, replayed by saved builders.

Immutable model packets are never changed. Registry rows are complete atomic
estimates; missing quality and independent-cohort holds cannot be relaxed by a
cache rebuild. Primary effect/CI values are never reconstructed from trend p.
"""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
from functools import lru_cache
ROOT = Path(__file__).resolve().parents[1]
VERSION = 'multicancer-2026-09-29'
DIRECTORY = ROOT / 'data/multicancer_review_2026-09-29'

@lru_cache(maxsize=1)
def registry():
    result = json.loads((DIRECTORY / 'registry.json').read_text())
    for relative, digest in result['source_review_inputs'].items():
        if hashlib.sha256((DIRECTORY / relative).read_bytes()).hexdigest() != digest:
            raise ValueError('Source review changed: ' + relative)
    ids = [entry['id'] for entry in result['entries']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate reviewed estimate identity')
    for entry in result['entries']:
        row, record = entry['row'], entry['record']
        if row['context_id'] != entry['id'] or record['context_id'] != entry['id']:
            raise ValueError('Registry identity mismatch')
        e = record['extraction']
        for dest, src in [('Effect Size','effect_size'),('Lower CI','ci_lower'),('Upper CI','ci_upper'),('Sample Size','total_n'),('Cases','cases')]:
            if row[dest] != e[src]:raise ValueError('Registry numerical mismatch')
        if row['policy_poolable'] and (entry.get('hold') or row['Quality Score'] not in {'Good','Moderate'} or not row['Sample Size']):
            raise ValueError('Unresolved evidence became poolable')
    return result

def rows(exposure=None, disease=None):
    return [copy.deepcopy(entry['row']) for entry in registry()['entries']
            if (exposure is None or entry['record']['exposure']==exposure)
            and (disease is None or entry['record']['disease']==disease)]

def record_row(record):
    if record.get('evidence_revision') != VERSION:return None
    matches=[entry for entry in registry()['entries'] if entry['id']==record.get('context_id')]
    if len(matches)!=1:raise ValueError('Unknown reviewed context')
    entry=matches[0]
    for key in ('pmid','exposure','disease','extraction','jbi'):
        if record.get(key)!=entry['record'].get(key):raise ValueError('Reviewed source bundle changed: '+key)
    row=copy.deepcopy(entry['row'])
    # An additional independent restriction may only reduce eligibility.
    if any(record.get(k) is False for k in ('scientific_pooling_eligible','manual_poolable','policy_poolable')):
        row.update(policy_poolable=False,**{'Primary Estimate':False})
    return row

def _bacon_effect(effect, lo, hi):
    return effect==0.51 and lo==0.24 and hi==1.1

def correct_row(raw):
    """Hold the exact erroneously labelled female-bacon Table4 estimate."""
    row=copy.deepcopy(raw)
    if ROOT.name!='metaPee' or str(row.get('PMID'))!='23011480':return row
    if not _bacon_effect(row.get('Effect Size'),row.get('Lower CI'),row.get('Upper CI')):return row
    if row.get('Direct Exposure')!='Processed meat intake':return row
    if row.get('bacon_source_correction'):
        if row.get('policy_poolable') is not False:raise ValueError('Bacon hold was relaxed')
        return row
    row['bacon_source_correction']={'version':VERSION,'original_fields':{k:copy.deepcopy(row.get(k)) for k in ('Gender','Sample Size','Cases','Study','comparison_type','exposure_subtype','policy_poolable','Primary Estimate')},'primary_source':'https://pmc.ncbi.nlm.nih.gov/articles/PMC3493769/','location':'Table 4, kidney, women (footnote c), bacon','reason':'This is female bacon, not total processed meat. Table4 contrast/unit is unspecified; hold from pooling.'}
    row.update(Gender='Female',**{'Sample Size':929,'Cases':37,'Source Exposure':'Bacon','Study':'De Stefani E et al. (2012) [PMID: 23011480] · Female · Bacon (contrast not reported)'},exposure_subtype='bacon',comparison_type='unclear',policy_poolable=False,manual_poolable=False,scientific_pooling_eligible=False,display_only=True,final_status='ELIGIBLE_NOT_POOLABLE',display_only_reason=row['bacon_source_correction']['reason'])
    row['Primary Estimate']=False
    return row

def effective_records(existing):
    """Append only new source-review contexts; never replace human/model facts."""
    result=copy.deepcopy(existing)
    by_id={r['context_id']:r for r in result}
    for entry in registry()['entries']:
        if entry['id'] in by_id:
            record_row(by_id[entry['id']])
        else:result.append(copy.deepcopy(entry['record']))
    return result

def apply_rows(existing, exposure, disease, *, add=True):
    result=[correct_row(r) for r in existing]
    ids={r.get('Effect ID'):r for r in result}
    for row in rows(exposure,disease) if add else []:
        if row['Effect ID'] in ids:
            old=ids[row['Effect ID']]
            for key in ('PMID','Effect Size','Lower CI','Upper CI','Sample Size','Cases','Gender','Direct Exposure'):
                if old.get(key)!=row.get(key):raise ValueError('Published reviewed estimate changed: '+key)
        else:result.append(row)
    return result
