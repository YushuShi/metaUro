"""Apply approved measurement labels to saved effective layers, fully offline.

Original model and human-review packets are read-only; edited effective files are
backed up byte-for-byte. Statistical and screening fields are verified unchanged.
"""
from pathlib import Path
import copy,hashlib,json,sys
from collections import Counter
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import dietary_only_policy as policy
OUT=ROOT/'outputs/dietary_only_2026-09-05'
FIELDS=set(policy.ALIASES)|set(policy.AUDIT_FIELDS)
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def without_measurement(value):
    if isinstance(value,dict):return {k:without_measurement(v) for k,v in value.items() if k not in FIELDS}
    if isinstance(value,list):return [without_measurement(v) for v in value]
    return value

def transform(payload, exposure=None):
    result=copy.deepcopy(payload)
    if not isinstance(result,dict):return result
    exposure=exposure or policy.exposure_for(result)
    if isinstance(result.get('studies'),list):
        result['studies']=[policy.apply_row(row,exposure) for row in result['studies']]
    for field in ['results','results_by_context']:
        values=result.get(field)
        if isinstance(values,list):result[field]=[policy.apply_record(row) for row in values]
        elif isinstance(values,dict):result[field]={k:policy.apply_record(row) for k,row in values.items()}
    return result

def main():
    targets=set()
    for name in ['Cached_results','ClaudeGemini_results','combined']:
        targets.update((ROOT/name).glob('*/*.json'))
    for folder in [ROOT/'outputs/poolability_policy_2026-09-05',ROOT/'outputs/fair_display_2026-09-05']:
        for pattern in ['*_effective_results.json','*_poolability.json']:targets.update(folder.glob(pattern))
    targets.update((ROOT/'data/claude_gemini_screening').glob('*/poolability_effective_results.json'))
    protected={str(p.relative_to(ROOT)):sha(p) for p in (ROOT/'data').rglob('*.json') if p not in targets and 'before' not in p.parts}
    changes=[];counts=Counter()
    for path in sorted(targets):
        before=json.loads(path.read_text());exposure=before.get('exposure') if isinstance(before,dict) else None
        if path.parent.parent.name in ['Cached_results','ClaudeGemini_results','combined']:exposure=exposure or path.parent.name
        after=transform(before,exposure)
        if after==before:continue
        if without_measurement(after)!=without_measurement(before):raise RuntimeError('Nonmeasurement mutation: '+str(path))
        backup=OUT/'before'/path.relative_to(ROOT);backup.parent.mkdir(parents=True,exist_ok=True)
        if not backup.exists():backup.write_bytes(path.read_bytes())
        path.write_text(json.dumps(after,ensure_ascii=False,allow_nan=False,indent=2)+'\n')
        rows=after.get('studies',after.get('results',[]));rows=rows.values() if isinstance(rows,dict) else rows
        n=sum(bool((row.get('extraction') or row).get('exposure_measurement_overridden')) for row in rows if isinstance(row,dict))
        counts[path.relative_to(ROOT).parts[0]]+=n
        changes.append({'path':str(path.relative_to(ROOT)),'before_sha256':sha(backup),'after_sha256':sha(path),'overridden_rows':n})
    changed_raw=[p for p,h in protected.items() if sha(ROOT/p)!=h]
    if changed_raw:raise RuntimeError('Protected packet changed: '+str(changed_raw))
    report={'policy_id':policy.config()['policy_id'],'approved_exposures':len(policy.config()['exposures']),
            'changed_files':len(changes),'overridden_rows_by_layer':dict(counts),'changes':changes,
            'new_gateway_calls':0,'non_measurement_fields_unchanged':True,'protected_raw_files':len(protected),
            'raw_sources_verified_unchanged':True,'protected_raw_sha256':protected}
    OUT.mkdir(parents=True,exist_ok=True);(OUT/'application_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ['changes','protected_raw_sha256']}),flush=True)
if __name__=='__main__':main()
