"""Add eligible Fair studies to saved tables without recalculating pooled effects."""
from pathlib import Path
import copy
import hashlib
import json
import sys
from collections import Counter, defaultdict
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import rebuild_relaxed_poolability as build
from scripts.display_eligibility import eligible_fair
from scripts import monthly_evidence_update as monthly
import meta_analysis
AUDIT=ROOT/'outputs/fair_display_2026-09-05'

def main():
    AUDIT.mkdir(parents=True,exist_ok=True)
    report={}
    for track, directory in [('chatgpt','Cached_results'),('claude_gemini','ClaudeGemini_results')]:
        packet=build.read(ROOT/'outputs/poolability_policy_2026-09-05'/f'{track}_effective_results.json')
        groups=defaultdict(list)
        for item in packet.get('results',[]):
            if not eligible_fair(item): continue
            disease=item.get('disease') or item.get('cancer')
            endpoint=item.get('endpoint') or item.get('outcome') or 'Incidence'
            if meta_analysis.is_curated_meta_analysis_exclusion(item.get('pmid'),disease,item.get('exposure'),endpoint): continue
            groups[(item['exposure'],disease,endpoint)].append(item)
        stats=Counter(); pooled_changes=[]
        for (exposure,disease,endpoint),items in groups.items():
            rows=[]
            for item in items:
                for e in build.extraction_list(item) or [{}]:
                    row=monthly.study_row(dict(item,extraction=e))
                    row['policy_track']=track
                    rows.append(row)
            for path in build.cache_paths(exposure,disease,endpoint):
                if track!='chatgpt':path=ROOT/directory/path.relative_to(ROOT/'Cached_results')
                existed=path.exists()
                cache=build.read(path)
                backup=AUDIT/'before'/path.relative_to(ROOT)
                if existed and not backup.exists():
                    backup.parent.mkdir(parents=True,exist_ok=True);backup.write_bytes(path.read_bytes())
                before={k:v for k,v in cache.items() if k!='studies'}
                context_ids={r['context_id'] for r in rows}
                pmids={r['PMID'] for r in rows}
                kept=[r for r in cache.get('studies',[]) if not (str(r.get('Quality Score','')).casefold()=='fair' and (r.get('context_id') in context_ids or str(r.get('PMID')) in pmids))]
                cache['studies']=kept+rows
                if not existed:
                    cache.update(exposure=exposure,disease=disease,cancer=disease,endpoint=endpoint,outcome=endpoint,canonical_track=track,
                        summary_html='<p>Eligible studies are listed below. No studies meet the default pooling criteria.</p>',
                        headline=None,screening_stats=None,plot_url=None,funnel_plot_url=None,baujat_plot_url=None)
                elif before!={k:v for k,v in cache.items() if k!='studies'}:
                    pooled_changes.append(str(path))
                build.write(path,cache)
                stats['cache_files']+=1
                stats['visible_fair_rows']+=len(rows)
                stats['new_cache_scopes']+=int(not existed)
            stats['eligible_fair_contexts']+=len(items)
        report[track]={**dict(stats),'pooled_field_changes':pooled_changes}
    build.write(AUDIT/'validation_summary.json',report)
    print(json.dumps(report,indent=2))
if __name__=='__main__':main()
