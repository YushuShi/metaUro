#!/usr/bin/env python3
"""Audit pooled publications, reuse saved cohort names, and screen missing names.

Saved cohort fields and explicit named-study extraction evidence are reused;
author citations in `Study` never count. Valid PMID answers are reused across
repositories. Existing scientific caches are read only. Every paid attempt,
including omitted-result recoveries, retains its raw response and cost receipt.
New calls require a source-bound native-engine witness of actual pooling of at
least two eligible estimates, with latest-cohort selection disabled.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import re
import threading
import os
import uuid
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(os.environ.get('COHORT_WORKSPACE_ROOT',Path(__file__).resolve().parents[1])).resolve()
WORK = Path(os.environ.get('COHORT_WORK_DIR',Path(__file__).resolve().parent)).resolve()
REPOS = ['MetaVir', 'metaThyroid', 'metaGut', 'metaFemina', 'metaHeart',
         'metaBreath', 'metaPee', 'metaNeuro', 'MetaCutis_et_ Capillus', 'metaUro']
MODEL = 'gpt-6-luna'
LOCK = threading.Lock()
NAME_KEYS = {'cohort_name','cohort_names','study_name','study_names','study_cohort',
             'cohort_id','cohort','trial_name','trial_acronym','cohort_acronym'}
SYSTEM = '''Identify the named human cohort, study or trial used in each supplied publication.
Use only its supplied title and abstract; never use outside knowledge or invent a name.
Return JSON {"results":[{"pmid":"...","status":"identified|not_reported|ambiguous",
"cohort_names":[{"name":"exact named cohort","canonical_name":"standard full name with round",
"round":null,"evidence":"verbatim short text from supplied title/abstract"}],"reason":"short"}]}.
Separate distinct cohorts and numbered rounds: Nurses' Health Study (NHS), NHS II,
and NHS III are three different cohorts. Keep any number/roman-numeral suffix in
canonical_name and round. Use "Nurses' Health Study", "Nurses' Health Study II",
"Nurses' Health Study III" as their canonical names. Numbered generations of any
other study likewise remain distinct. Acronyms and spelled-out versions of the
same named cohort have one canonical_name. Do not merge separately named cohorts
or infer numbered rounds when none is given. Include named trial populations too.
If a paper pools several named cohorts list every cohort explicitly reported.
Only list cohorts that supplied participants or estimates to THIS publication.
Exclude cohorts mentioned solely in background, prior studies, citations, or
comparisons. In particular 'a recent study in cohort X ... We assessed ... cohort Y'
means only cohort Y contributed to the current study. Evidence must be a single
verbatim contiguous substring, without ellipses, rewrites or joined fragments.
An author/year citation, generic population description, hospital name alone,
country, or phrase 'a prospective cohort study' is not a study name. If no named
study appears use not_reported and an empty list; ambiguous identity uses ambiguous.
Each result must match its supplied PMID exactly. Preserve all supplied PMIDs.'''
SYSTEM += '''
Database, administrative claims, EHR, linked-record platform and case-ascertainment
registry names alone do not establish a stable participant cohort (e.g. TriNetX,
CPRD/GPRD, NHIRD, THIN or generic insurance databases). Preserve these separately
in reported_source_names, using the same name/canonical_name/round/evidence fields.
If no independently named cohort is identified, use status ambiguous and an empty
cohort_names list. Explicitly defined cohorts such as UK Biobank, FinnGen, named
family registries, NHIS Health Screening Cohort or National Sample Cohort remain
valid cohort names. Never infer identical participants solely from database use.
'''


def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def write(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+f'.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def pmid_of(row): return str(row.get('PMID') or row.get('pmid') or '').strip()
def meaningful(value): return value is not None and str(value).strip().lower() not in {'','na','n/a','none','null','unknown','not reported','not available'}
def numeric(value):
    try:return math.isfinite(float(value))
    except (ValueError,TypeError):return False


def poolable(row):
    if any(row.get(k) is True for k in ['display_only','ui_display_only','manual_review_pending','full_text_pooling_block','manual_review_hold','superseded_by_adjusted','hierarchy_overlap_unpooled','hierarchy_source_conflict']):return False
    if str(row.get('Quality Score') or '').strip().lower() not in {'good','moderate'}:return False
    if not numeric(row.get('Sample Size')) or float(row['Sample Size'])<=0:return False
    for key in ['policy_poolable','scientific_pooling_eligible','manual_poolable',
                'manual_pooling_eligible','subexposure_pooling_selected','disease_scope_pooling_selected',
                'pool_in_parent_meta_analysis','included_in_parent_meta_analysis','Primary Estimate',
                'pool','pool_eligible','hierarchy_source_poolable']:
        if row.get(key) is False:return False
    if str(row.get('final_status','')).upper() in {'EXCLUDE','ELIGIBLE_NOT_POOLABLE','PENDING_MANUAL_REVIEW'}:return False
    return (all(numeric(row.get(k)) for k in ['Effect Size','Lower CI','Upper CI'])
            and float(row['Upper CI'])>float(row['Lower CI']))


def cache_roots(repo):
    r=ROOT/repo
    if repo=='metaFemina':candidates=list(r.glob('data/subexposure_views/*/Cached_results'))+list(r.glob('chatgpt/subexposure_views/*/Cached_results'))
    elif repo=='metaThyroid':candidates=list(r.glob('combined/provisional_ui/Subexposure_views/*/Cached_results')) or [r/'combined/provisional_ui/Cached_results']
    elif repo=='metaNeuro':candidates=[r/'Cached_results']+list(r.glob('data/metaneuro/*'))
    elif repo=='MetaVir':candidates=list(r.glob('Subexposure_results/*')) or [r/'combined']
    else:candidates=list(r.glob('combined/provisional_ui/subexposure_views/*/Cached_results'))
    if repo=='metaBreath':
        pointer=r/'combined/nonmalignant_active_view.json'
        if pointer.exists():candidates+=list((r/'combined'/read(pointer)['namespace']).glob('subexposure_views/*/Cached_results'))
    if repo=='metaGut':
        campaign=r/'combined/nonmalignant_extension_2026-09-10'
        pointer=campaign/'active_interim_view.json'
        if pointer.exists():candidates+=list((campaign/read(pointer)['view_path']).glob('subexposure_views/*/Cached_results'))
    if repo=='metaHeart':
        pointer=read(r/'data/local_screening_active.json')
        candidates+=[r/'data/local_screening_views'/pointer['view_id']/'Cached_results']
    if repo=='metaUro':
        # Published source snapshots are retained; overlay cache paths are
        # enumerated from the checksum-bound current overlay, never all history.
        candidates=[]
        for source in ['MetaVir','metaPee']:
            roots=list((r/'sources'/source).glob('combined/provisional_ui/subexposure_views/*/Cached_results'))
            roots+=list((r/'sources'/source).glob('Subexposure_results/*'))
            candidates+=roots or [r/'sources'/source/'combined']
            overlay=r/'updates'/source
            candidates+=list(overlay.glob('**/subexposure_views/*/Cached_results'))
            candidates+=list(overlay.glob('**/Subexposure_results/*'))
    return sorted(set(p for p in candidates if p.is_dir()))


def names_from(row, source):
    result=[]
    for key,value in row.items():
        k=re.sub('[ -]+','_',str(key).strip().lower())
        if k in NAME_KEYS and meaningful(value):
            vals=value if isinstance(value,list) else [value]
            for name in vals:
                if isinstance(name,dict):name=name.get('canonical_name') or name.get('name')
                if isinstance(name,str) and meaningful(name):
                    result.append({'name':name,'canonical_name':name,'round':None,
                                   'evidence':f'Saved {key}: {name}','source':source,'field':key})
        elif k in {'extraction','combined_source_extraction'} and isinstance(value,dict):
            result.extend(names_from(value,source))
    return result


SAVED_ALIASES = [
 (r"Nurses[’'\s]*Health Study\s*(?:III|II|I|3|2|1)?",lambda m: "Nurses' Health Study"+({'2':' II','II':' II','3':' III','III':' III'}.get((re.search(r'(III|II|I|[123])$',m.group(0).strip()) or [''])[0],''))),
 (r'\bNHS\s*(?:III|II|3|2)\b',lambda m:"Nurses' Health Study"+(' III' if re.search('III|3',m.group()) else ' II')),
 (r'Health Professionals Follow[ -]up Study|\bHPFS\b','Health Professionals Follow-up Study'),
 (r'\bUK Biobank\b|\bUKB\b','UK Biobank'),
 (r'NIH[–-]AARP(?: Diet and Health Study)?','NIH-AARP Diet and Health Study'),
 (r'Prostate,? Lung,? Colorectal(?:,? and)? Ovarian(?: Cancer)? Screening Trial|\bPLCO\b','PLCO Cancer Screening Trial'),
 (r'European Prospective Investigation into Cancer and Nutrition(?:[ -](?:Norfolk|Oxford|Potsdam|Heidelberg|Spain|Italy|Germany|Prospect|MORGEN))?|\bEPIC(?:[ -](?:Norfolk|Oxford|Potsdam|Heidelberg|Spain|Italy|Germany|Prospect|MORGEN))?\b',None),
 (r"Women[’'s]+ Health Initiative(?: Observational Study)?|\bWHI(?:[ -]OS)?\b",None),
 (r"(?<!Shanghai )(?<!Iowa )(?<!Black )(?<!York )(?<!Mexico )Women[’'s]+ Health Study", "Women's Health Study"),
 (r"Iowa Women[’'s]+ Health Study", "Iowa Women's Health Study"),
 (r"Black Women[’'s]+ Health Study", "Black Women's Health Study"),
 (r"New York Women[’'s]+ Health Study", "New York Women's Health Study"),
 (r"New Mexico Women[’'s]+ Health Study", "New Mexico Women's Health Study"),
 (r'\bMultiethnic Cohort(?: Study)?\b|\bMEC\b','Multiethnic Cohort Study'),
 (r'Netherlands Cohort Study(?: on Diet and Cancer)?|\bNLCS\b','Netherlands Cohort Study'),
 (r'Alpha[ -]Tocopherol[ ,–-]+Beta[ -]Carotene(?: Cancer Prevention)?(?: Study)?|\bATBC\b','ATBC Cancer Prevention Study'),
 (r'Cancer Prevention Study[ -]*(?:II|I|2|1)(?: Nutrition Cohort)?|\bCPS[ -]*(?:II|I|2|1)(?: Nutrition Cohort)?\b',None),
 (r'Adventist Health Study[ -]*(?:2|1)|\bAHS[ -]*(?:2|1)\b',None),
 (r"Shanghai Women[’'s]+ Health Study|\bSWHS\b", "Shanghai Women's Health Study"),
 (r"Shanghai Men[’'s]+ Health Study|\bSMHS\b", "Shanghai Men's Health Study"),
 (r'Japan Public Health Center[ -]based Prospective Study(?: on Cancer and Cardiovascular Diseases?)?(?:\s*(?:III|II|I)\b)?|\bJPHC(?:[ -]*(?:III|II|I))?\b',None),
 (r'Japan Collaborative Cohort Study|\bJACC\b','Japan Collaborative Cohort Study'),
 (r'Singapore Chinese Health Study','Singapore Chinese Health Study'),
 (r'Melbourne Collaborative Cohort Study|\bMCCS\b','Melbourne Collaborative Cohort Study'),
 (r'\bSU[ .]?VI[ .]?MAX\b','SU.VI.MAX'),
 (r'NutriNet[ -]Sant[ée]','NutriNet-Santé'),
 (r'Golestan Cohort Study','Golestan Cohort Study'),
 (r'\bHUNT[ -]?[1234]\b',None),
 (r'Rotterdam Study','Rotterdam Study'),
 (r'Atherosclerosis Risk in Communities(?: Study)?|\bARIC\b','Atherosclerosis Risk in Communities Study'),
 (r'(?:(?:Third|Second) Generation )?Framingham(?: Offspring)?(?: Heart)? Study',None),
 (r'Multi[ -]Ethnic Study of Atherosclerosis|\bMESA\b','Multi-Ethnic Study of Atherosclerosis'),
 (r'Coronary Artery Risk Development in Young Adults(?: Study)?|\bCARDIA\b','CARDIA'),
 (r'Cardiovascular Health Study','Cardiovascular Health Study'),
 (r'\bPREDIMED\b','PREDIMED'),
 (r'\bORDET\b','ORDET'),
 (r'Kuopio Isch(?:a)?emic Heart Disease(?: \(KIHD\))? Risk Factor Study|\bKIHD\b','Kuopio Ischaemic Heart Disease Risk Factor Study'),
 (r'Age,? Gene/Environment Susceptibility(?:[– -]Reykjavik)?|\bAGES[– -]Reykjavik\b','AGES-Reykjavik'),
 (r'Vitamins and Lifestyle(?: Study)?','Vitamins and Lifestyle Study'),
]


def saved_text_names(row, source):
    """Reuse unambiguous named cohorts already saved in extraction evidence."""
    bits=[]
    for key in ['Population','Design','extraction_evidence','extraction_supporting_text']:
        val=row.get(key)
        if isinstance(val,dict):val={k:v for k,v in val.items() if k.lower() in {'population','sample_size','total_n','participants','design'}}
        if isinstance(val,(dict,list)):bits.append(json.dumps(val,ensure_ascii=False))
        elif isinstance(val,str):bits.append(val)
    text='\n'.join(bits);names={}
    for pattern,canonical in SAVED_ALIASES:
        for match in re.finditer(pattern,text,re.I):
            # A bare acronym must retain its proper capitalization. In
            # particular anatomical "cardia" is not the CARDIA cohort.
            if re.fullmatch(r'[A-Za-z]{2,8}',match.group()) and not match.group().isupper():continue
            if match.group()=='CARDIA' and (re.search(r'(?:gastric|non[- ]?)\s*$',text[max(0,match.start()-20):match.start()],re.I) or re.match(r'\s+(?:adenocarcinoma|carcinoma|cancer)\b',text[match.end():],re.I)):
                continue
            label=canonical(match) if callable(canonical) else canonical or match.group()
            if canonical is None:
                if re.search(r'EPIC|European Prospective',label,re.I):
                    suffix=re.search(r'(Norfolk|Oxford|Potsdam|Heidelberg|Spain|Italy|Germany|Prospect|MORGEN)(?: cohort)?$',label,re.I)
                    label='EPIC'+('-'+suffix.group().title() if suffix else '')
                elif re.search(r'WHI|Women',label,re.I):label="Women's Health Initiative"+(' Observational Study' if re.search('Observational|OS$',label,re.I) else '')
                elif re.search(r'CPS|Cancer Prevention',label,re.I):
                    round_='II' if re.search(r'(?:II|2)\b',label,re.I) else 'I'
                    label='Cancer Prevention Study '+round_+(' Nutrition Cohort' if 'nutrition' in label.lower() else '')
                elif re.search(r'Adventist|AHS',label,re.I):label='Adventist Health Study '+('2' if '2' in label else '1')
                elif re.search(r'JPHC|Japan Public',label,re.I):
                    suffix=re.search(r'(II|I)$',label.strip(),re.I);label='JPHC'+(' '+suffix.group().upper() if suffix else '')
                elif re.search('HUNT',label,re.I):label=re.sub('[ -]','',label.upper())
                elif re.search('Framingham',label,re.I) and 'generation' not in label.lower():label='Framingham'+(' Offspring' if 'offspring' in label.lower() else '')+' Heart Study'
            suffix_context=text[match.end():match.end()+40]
            suffix=re.match(r'\s+(?:study\s+)?(?:cohorts?\s+)?(IV|III|II|I|[1234])\b',suffix_context,re.I)
            if suffix and not re.search(r'\b(?:IV|III|II|I|[1234])$',label,re.I):
                round_={'1':'I','2':'II','3':'III','4':'IV'}.get(suffix.group(1).upper(),suffix.group(1).upper())
                if not (label=="Nurses' Health Study" and round_=='I'):label+=' '+round_
            if label=='Framingham Heart Study' and re.match(r'\s*(?:\(|,)?\s*offspring\b',suffix_context,re.I):label='Framingham Offspring Heart Study'
            names[label]={'name':match.group(),'canonical_name':label,'round':None,
                          'evidence':match.group()+(suffix.group() if suffix else ''),'source':source,'field':'saved_population_or_extraction_evidence'}
    # Coordinated suffixes share a single written study name: "Nurses' Health
    # Study I and II" explicitly identifies two cohorts, never only the first.
    for match in re.finditer(r"(?:Nurses[’'\s]*Health Stud(?:y|ies)|\bNHS)\s*(?:cohorts?\s*)?\(?\s*((?:III|II|I|[123])\s*(?:,?\s*and|/|,|&)\s*(?:III|II|I|[123])(?:\s*(?:,?\s*and|/|,|&)\s*(?:III|II|I|[123]))?)\b",text,re.I):
        for suffix in re.findall(r'\b(?:III|II|I|[123])\b',match.group(1),re.I):
            suffix={'1':'I','2':'II','3':'III'}.get(suffix.upper(),suffix.upper())
            label="Nurses' Health Study"+(' '+suffix if suffix!='I' else '')
            names[label]={'name':label,'canonical_name':label,'round':suffix,'evidence':match.group(),'source':source,'field':'saved_population_or_extraction_evidence'}
    for match in re.finditer(r'\bJPHC\s*(?:study\s*)?(?:cohorts?\s*)?((?:II|I|[12])\s*(?:,?\s*and|/|,|&)\s*(?:II|I|[12]))\b',text,re.I):
        names.pop('JPHC',None)
        for suffix in re.findall(r'\b(?:II|I|[12])\b',match.group(1),re.I):
            suffix={'1':'I','2':'II'}.get(suffix.upper(),suffix.upper());label='JPHC '+suffix
            names[label]={'name':label,'canonical_name':label,'round':suffix,'evidence':match.group(),'source':source,'field':'saved_population_or_extraction_evidence'}
    return list(names.values())


def metadata_items(value):
    if isinstance(value,list):return value
    if not isinstance(value,dict):return []
    for key in ['articles','records','results','studies']:
        rows=value.get(key)
        if isinstance(rows,list):return rows
        if isinstance(rows,dict):return list(rows.values())
    return list(value.values()) if all(isinstance(v,dict) for v in value.values()) else []


def add_metadata(records, value, source):
    for item in metadata_items(value):
        if not isinstance(item,dict):continue
        pmid=pmid_of(item)
        if pmid not in records:continue
        target=records[pmid]
        title=item.get('title') or item.get('Title') or item.get('Reference')
        abstract=item.get('abstract') or item.get('Abstract')
        if meaningful(title) and not target.get('title'):target['title']=str(title)
        if meaningful(abstract) and len(str(abstract))>len(target.get('abstract','')):
            target['abstract']=str(abstract);target['abstract_source']=source
        for key in ([] if target.get('publication_date_raw') else ['published_date','publication_date','Publication Date','PubDate','date','Year','year']):
            date=item.get(key)
            if meaningful(date):
                date=str(date)
                if re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?',date):
                    if len(date)>len(target.get('publication_date','')):
                        target['publication_date']=date;target['publication_date_source']=source
                    break
        target['saved_names'].extend(names_from(item,source))


def prepare():
    records=read(WORK/'pooled_publications.json') if (WORK/'pooled_publications.json').exists() else {};counts={}
    original_pmids=set(records)
    for repo in REPOS:
        pmids=set();rows_count=0;fields=Counter();files_count=0
        for root in cache_roots(repo):
            for path in sorted(root.glob('*/*.json')):
                try:payload=read(path)
                except (ValueError,OSError):continue
                if not isinstance(payload,dict):continue
                files_count+=1
                for row in payload.get('studies',[]):
                    if not isinstance(row,dict) or not poolable(row):continue
                    pmid=pmid_of(row)
                    if not pmid:continue
                    rows_count+=1;pmids.add(pmid)
                    entry=records.setdefault(pmid,{'pmid':pmid,'repositories':[],'title':'','abstract':'','saved_names':[],'publication_date':''})
                    if repo not in entry['repositories']:entry['repositories'].append(repo)
                    source=str(path.relative_to(ROOT))
                    if not entry['title']:entry['title']=str(row.get('Reference') or row.get('Title') or '')
                    add_metadata(records,[row],source)
                    entry['saved_names'].extend(saved_text_names(row,source))
                    fields.update(k for k in row if 'cohort' in k.lower() or ('study' in k.lower() and k!='Study'))
        counts[repo]={'poolable_row_occurrences':rows_count,'unique_pooled_pmids':len(pmids),'cache_files_read':files_count,'dedicated_name_fields':dict(fields)}
        print(repo,counts[repo],flush=True)
    onco_ids=set()
    for path in (ROOT/'metaMeta/metaOnco/data').glob('metaonco*.json'):
        payload=read(path)
        for res in payload.get('results',[]):
            for typ in ['combined','dietary']:
                for row in (res.get(typ) or {}).get('evidence',[]):
                    pmid=pmid_of(row)
                    if not pmid:continue
                    onco_ids.add(pmid)
                    entry=records.setdefault(pmid,{'pmid':pmid,'repositories':[],'title':'','abstract':'','saved_names':[],'publication_date':''})
                    if 'metaMeta' not in entry['repositories']:entry['repositories'].append('metaMeta')
    counts['metaMeta']={'unique_pooled_pmids':len(onco_ids)}
    # Candidate packets contain the original abstracts. Never inspect active IBD
    # run packets; that campaign is deferred until extraction is published.
    for repo in REPOS:
        r=ROOT/repo
        paths=list(r.glob('data/monthly_updates/*/candidates.json'))
        paths+=list(r.glob('data/*/articles.json'))
        paths+=list(r.glob('data/*/pubmed_records.json'))
        paths+=list(r.glob('data/*/*/pubmed_metadata_resolution.json'))
        paths+=list(r.glob('data/*/candidate*.json'))
        paths+=list(r.glob('combined/*screening.json'))
        if repo=='metaFemina':paths+=list(r.glob('combined/screening/*screen*.json'))
        if repo=='metaUro':
            paths+=list(r.glob('sources/*/data/monthly_updates/*/candidates.json'))
            paths+=list(r.glob('updates/monthly/*/*/screening/candidates.json'))
            paths+=list(r.glob('updates/monthly/*/*/screening/final_screening.json'))
        for path in sorted(set(paths)):
            if 'ibd_extension_2026-10-24' in str(path):continue
            if path.stat().st_size>300_000_000:continue
            try:add_metadata(records,read(path),str(path.relative_to(ROOT)))
            except (ValueError,OSError):continue
    saved=0
    for record in records.values():
        names={x['canonical_name']:x for x in record['saved_names']}
        record['saved_names']=list(names.values())
        if names:saved+=1
        date=record.get('publication_date','')
        record['publication_date_precision']={4:'year',7:'month',10:'day'}.get(len(date),'unknown')
    audit={'schema_version':1,'created_at':now(),'repositories':counts,'unique_pooled_pmids':len(records),
           'saved_structured_cohort_names':saved,'missing_names_requiring_luna':len(records)-saved,
           'with_abstract':sum(bool(r['abstract']) for r in records.values()),
           'with_precise_publication_date':sum(r['publication_date_precision'] in {'day','month'} for r in records.values()),
           'deferred_campaigns':[{'repo':'metaGut','campaign':'ibd_extension_2026-10-24','reason':'R3 incomplete; source preparation blocked for PMC1027200, PMC1596421; no authoritative release yet'}],
           'cost':{'new_model_calls':0,'gateway_cost_usd':'0'}}
    write(WORK/'pooled_publications.json',records);write(WORK/'audit.json',audit)
    write(WORK/'additional_pmids.json',sorted(set(records)-original_pmids))
    print(json.dumps(audit,indent=2),flush=True)


def configure_client():
    # This workflow uses the authorized local Cornell gateway only. Never copy
    # secrets into artifacts or process arguments/environment.
    from dotenv import dotenv_values
    from openai import OpenAI
    from urllib.parse import urlsplit
    values=dotenv_values(ROOT/'metaGut/mykey.env',interpolate=False)
    key=next((values.get(k) for k in ['CORNELL_API_KEY','Cornell_API_Key'] if values.get(k)),None)
    base=next((values.get(k) for k in ['CORNELL_API_BASE_URL','API_BASE_URI','API_Base_URI'] if values.get(k)),None)
    if not key or not base:raise RuntimeError('Cornell gateway credentials are not configured')
    parts=urlsplit(base)
    if parts.scheme!='https' or parts.hostname!='api.ai.it.cornell.edu' or parts.username or parts.password:
        raise RuntimeError('Gateway must use the approved Cornell HTTPS host')
    client=OpenAI(api_key=key,base_url=base,max_retries=0,timeout=240)
    values.clear();return client


def _receipt_root(repository):
    if repository in {'metaOnco','metaMeta','metaMeta/metaOnco'}:return ROOT/'metaMeta/metaOnco'
    if repository not in REPOS:raise ValueError('Unrecognized exact-pooling receipt repository')
    return ROOT/repository


def compile_exact_pooling_scope():
    """Bind the native engine's exact default PMID union to source witnesses."""
    eligible=set();standalone_eligible=set();witnesses=defaultdict(list);receipts=[];seen_repos=set();missing_witnesses=[];hashes={};policy_files={}
    for path in sorted(WORK.glob('actual_pooled_*.json')):
        payload=read(path);repository=payload.get('repository')
        if not repository or not isinstance(payload.get('pmids'),list):continue
        try:root=_receipt_root(repository)
        except ValueError:continue
        normalized='metaMeta' if repository in {'metaOnco','metaMeta','metaMeta/metaOnco'} else repository
        standalone_eligible.update(str(x) for x in payload['pmids'])
        strict=payload.get('pmids_with_multirow_estimate_witness',payload.get('multistudy_pmids'))
        if not isinstance(strict,list):continue
        seen_repos.add(normalized);pmids={str(x) for x in strict};eligible.update(pmids)
        by_pmid=payload.get('witnesses') or payload.get('witness_contexts_by_pmid') or {}
        sources=payload.get('source_files') or payload.get('source_sha256') or {}
        policy_bindings={**(payload.get('engine_files') or {}),**(payload.get('eligibility_policy_sha256') or {}),
                         **(payload.get('eligibility_dependency_sha256') or {})}
        revalidation=payload.get('eligibility_invariance_revalidation') or {}
        if revalidation.get('proof_path') and revalidation.get('proof_sha256'):
            policy_bindings[revalidation['proof_path']]=revalidation['proof_sha256']
        for relative,expected in policy_bindings.items():
            file=Path(relative);file=file if file.is_absolute() else root/file
            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=expected:raise RuntimeError('Actual pooling eligibility code changed: '+str(file))
            policy_files[str(file)]=expected
        variants=payload.get('proven_equivalent_dependency_sha256') or {}
        if variants:
            proof=payload.get('release_reproducibility_proof') or {}
            file=root/str(proof.get('path',''))
            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=proof.get('sha256'):
                raise RuntimeError('Release dependency equivalence proof missing or changed')
            checked=read(file)
            if checked.get('status')!='passed' or not all(checked.get(key) for key in ['resolved_active_plans_equal','eligible_pmids_equal','strict_pooled_pmids_equal','source_hashes_equal_and_unchanged']) or checked.get('context_differences'):
                raise RuntimeError('Release dependency equivalence proof did not pass')
            if digest(sorted(str(x) for x in payload['pmids']))!=checked.get('eligible_pmids_sha256') or digest(sorted(pmids))!=checked.get('strict_pooled_pmids_sha256'):
                raise RuntimeError('Release equivalence pooling scope changed; recapture and revalidate')
            proven_sources=checked.get('audited_source_sha256') or {}
            for relative,expected in sources.items():
                expected=expected.get('sha256') if isinstance(expected,dict) else expected
                if proven_sources.get(relative)!=expected:raise RuntimeError('Release equivalence source scope changed: '+relative)
            policy_files[str(file)]=proof['sha256']
            for relative,allowed in variants.items():
                file=root/relative;actual=hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None
                if actual not in allowed:raise RuntimeError('Unproven release dependency changed: '+str(file))
                policy_files[str(file)]=actual
        for pmid in pmids:
            items=by_pmid.get(pmid,[])
            if isinstance(items,dict):
                context=items.get('engine_context',{})
                if context.get('cache_path'):items=[{**items,'cache_path':context['cache_path']}]
                elif items.get('source_records'):
                    items=[{**items,'cache_path':source['cache_path'],'source_row_index':source.get('row_index')} for source in items['source_records'] if source.get('cache_path')]
                else:items=[items]
            for item in items:
                if not isinstance(item,dict):continue
                pooled_n=item.get('pooled_n',item.get('max_pooled_n',item.get('pooled_estimate_rows',(item.get('engine_context') or {}).get('pooled_n'))))
                if not numeric(pooled_n) or float(pooled_n)<2:continue
                relative=item.get('cache_path') or item.get('path') or item.get('source_snapshot')
                if not relative:continue
                source=Path(relative)
                if not source.is_absolute():
                    source=(ROOT/source) if (ROOT/source).is_file() else root/source
                if not source.is_file():continue
                saved=sources.get(relative) or sources.get(str(source)) or sources.get(str(source.relative_to(root)) if source.is_relative_to(root) else '')
                expected=saved.get('sha256') if isinstance(saved,dict) else saved
                if not expected:continue
                if str(source) not in hashes:hashes[str(source)]=hashlib.sha256(source.read_bytes()).hexdigest()
                actual=hashes[str(source)]
                if actual!=expected:raise RuntimeError('Exact pooled witness source changed: '+str(source))
                witnesses[pmid].append({'repository':normalized,'receipt':path.name,'source_file':str(source),
                                       'source_sha256':expected,'context':item})
            if not witnesses[pmid]:missing_witnesses.append({'repository':normalized,'pmid':pmid,'receipt':path.name})
        receipts.append({'path':path.name,'repository':normalized,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'pmids':len(pmids)})
    missing_repos=sorted(set([*REPOS,'metaMeta'])-seen_repos)
    witnesses={pmid:items for pmid,items in witnesses.items() if items}
    unresolved=sorted(eligible-set(witnesses))
    engine_files=dict(policy_files)
    for repository in seen_repos:
        root=_receipt_root(repository)
        for filename in ['meta_analysis.py','analysis_settings_backend.py','source_selection.py','snapshot_projection.py']:
            file=root/filename
            if file.is_file():engine_files[str(file)]=hashlib.sha256(file.read_bytes()).hexdigest()
    result={'schema_version':1,'generated_at':now(),'minimum_pooled_estimates':2,
            'policy':'Only PMIDs in actual native statistical pooling with at least two final eligible estimates, latest off, may be sent to a model. Standalone one-row estimates never authorize screening.',
            'status':'verified' if not missing_repos and not unresolved else 'incomplete','pmids':sorted(eligible),
            'eligible_pmids_including_standalone':sorted(standalone_eligible),'singleton_only_pmids':sorted(standalone_eligible-eligible),
            'witnesses':dict(witnesses),'receipts':receipts,'engine_files':engine_files,'missing_repositories':missing_repos,
            'missing_witness_pmids':unresolved,'missing_witness_details':missing_witnesses}
    write(WORK/'exact_pooling_scope.json',result)
    print({'status':result['status'],'pmids':len(eligible),'missing_repositories':missing_repos,'missing_witness_pmids':len(unresolved)},flush=True)
    return result


def prepare_exact_pooling_publications():
    """Import all exact-engine pooled PMIDs, preserving previously paid metadata."""
    scope=read(WORK/'exact_pooling_scope.json')
    if scope.get('status')!='verified':raise RuntimeError('Complete exact engine scope is required before preparing new screening inputs')
    records=read(WORK/'pooled_publications.json');before=set(records)
    for entry in records.values():entry['actual_pooled_repositories']=[]
    by_repo=defaultdict(set)
    for pmid,items in scope['witnesses'].items():
        for item in items:by_repo[item['repository']].add(pmid)
    for repo,pmids in by_repo.items():
        for pmid in pmids:
            entry=records.setdefault(pmid,{'pmid':pmid,'repositories':[],'title':'','abstract':'','saved_names':[],'publication_date':''})
            if repo not in entry['repositories']:entry['repositories'].append(repo)
            entry['actual_pooled_repositories']=sorted({*entry.get('actual_pooled_repositories',[]),repo})
        if repo=='metaMeta':continue
        for cache_root in cache_roots(repo):
            for path in cache_root.glob('*/*.json'):
                try:payload=read(path)
                except (ValueError,OSError):continue
                if not isinstance(payload,dict):continue
                for row in payload.get('studies',[]):
                    if not isinstance(row,dict) or pmid_of(row) not in pmids:continue
                    source=str(path.relative_to(ROOT));entry=records[pmid_of(row)]
                    add_metadata(records,[row],source)
                    names={n['canonical_name']:n for n in entry['saved_names']}
                    names.update({n['canonical_name']:n for n in names_from(row,source)+saved_text_names(row,source)})
                    entry['saved_names']=list(names.values())
        root=ROOT/repo
        metadata=list(root.glob('data/monthly_updates/*/candidates.json'))+list(root.glob('data/*/pubmed_records.json'))+list(root.glob('data/*/*/pubmed_metadata_resolution.json'))
        for path in metadata:
            if 'ibd_extension_2026-10-24' in str(path) or path.stat().st_size>300_000_000:continue
            add_metadata(records,read(path),str(path.relative_to(ROOT)))
    for pmid,entry in records.items():
        entry['actual_pooled_anywhere']=pmid in scope['pmids']
        entry['actual_eligible_including_standalone']=pmid in scope.get('eligible_pmids_including_standalone',scope['pmids'])
        entry['standalone_only_anywhere']=pmid in scope.get('singleton_only_pmids',[])
        entry.setdefault('publication_date_precision',{4:'year',7:'month',10:'day'}.get(len(entry.get('publication_date') or ''),'unknown'))
    write(WORK/'pooled_publications.json',records)
    prior=read(WORK/'exact_scope_added_publications.json') if (WORK/'exact_scope_added_publications.json').exists() else {}
    additions=sorted(set(prior.get('new_pmids',[])) | (set(records)-before))
    write(WORK/'exact_scope_added_publications.json',{'at':now(),'new_pmids':additions,
          'initial_new_names_requiring_screening':prior.get('initial_new_names_requiring_screening',prior.get('new_names_requiring_screening',[])),
          'strict_actual_pooled_pmids':len(scope['pmids']),
          'added_pmids_strictly_pooled':sorted(set(additions)&set(scope['pmids'])),
          'added_pmids_standalone_only':sorted(set(additions)&set(scope.get('singleton_only_pmids',[]))),
          'new_names_requiring_screening':[pmid for pmid in scope['pmids'] if not records[pmid]['saved_names'] and not (WORK/'answers'/f'{pmid}.json').exists()]})


def assert_exact_screening_scope(articles, scope):
    if scope.get('status')!='verified' or scope.get('minimum_pooled_estimates')!=2:raise RuntimeError('Strict multirow engine pooling receipts are incomplete; paid screening is disabled')
    for path,expected in scope.get('engine_files',{}).items():
        if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Engine eligibility code changed; recollect exact pooling receipts before screening')
    for article in articles:
        valid=False
        for witness in scope.get('witnesses',{}).get(article['pmid'],[]):
            context=witness.get('context',{})
            pooled_n=context.get('pooled_n',context.get('max_pooled_n',context.get('pooled_estimate_rows',(context.get('engine_context') or {}).get('pooled_n'))))
            if not numeric(pooled_n) or float(pooled_n)<2:continue
            path=Path(witness['source_file'])
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==witness['source_sha256']:
                valid=True;break
        if not valid:raise RuntimeError('No unchanged actual pooled engine witness for PMID '+article['pmid'])


def refresh_exact_pooling_flags():
    """Refresh strict scope after receipt-only changes without rescanning inputs."""
    scope=read(WORK/'exact_pooling_scope.json')
    if scope.get('status')!='verified' or scope.get('minimum_pooled_estimates')!=2:raise RuntimeError('Verified strict scope is required')
    records=read(WORK/'pooled_publications.json');strict=set(scope['pmids'])
    if strict-set(records):raise RuntimeError('Newly pooled papers require prepare-exact before flag refresh')
    eligible=set(scope.get('eligible_pmids_including_standalone',strict));standalone=set(scope.get('singleton_only_pmids',[]))
    for pmid,entry in records.items():
        repos=sorted({w['repository'] for w in scope.get('witnesses',{}).get(pmid,[])})
        entry.update(actual_pooled_anywhere=pmid in strict,actual_pooled_repositories=repos,
                     actual_eligible_including_standalone=pmid in eligible,standalone_only_anywhere=pmid in standalone)
        entry['repositories']=sorted(set(entry['repositories'])|set(repos))
    write(WORK/'pooled_publications.json',records)
    path=WORK/'exact_scope_added_publications.json';prior=read(path) if path.exists() else {}
    added=set(prior.get('new_pmids',[]))
    prior.update(at=now(),strict_actual_pooled_pmids=len(strict),
                 initial_new_names_requiring_screening=prior.get('initial_new_names_requiring_screening',prior.get('new_names_requiring_screening',[])),
                 added_pmids_strictly_pooled=sorted(added&strict),added_pmids_standalone_only=sorted(added&standalone),
                 new_names_requiring_screening=[pmid for pmid in strict if not records[pmid]['saved_names'] and not (WORK/'answers'/f'{pmid}.json').exists()])
    write(path,prior)
    print({'preserved_publications':len(records),'strictly_pooled':len(strict),'standalone_only':len(standalone),
           'missing_name_assessments':len(prior['new_names_requiring_screening'])},flush=True)


def audit_paid_screening_scope():
    """Report historical incidental calls without concealing any paid attempt."""
    scope=read(WORK/'exact_pooling_scope.json')
    if scope.get('status')!='verified':raise RuntimeError('Complete exact engine scope is required for a final financial scope audit')
    eligible=set(scope['pmids']);calls=[read(p) for p in (WORK/'calls').glob('*.json')]
    paid={pmid for call in calls for pmid in call['pmids']};incidental=sorted(paid-eligible)
    mixed=[];allocation=Decimal(0)
    for call in calls:
        outside=sorted(set(call['pmids'])-eligible)
        if not outside:continue
        cost=call.get('gateway_cost_usd')
        estimated=Decimal(str(cost))*Decimal(len(outside))/Decimal(len(call['pmids'])) if cost is not None else None
        if estimated is not None:allocation+=estimated
        mixed.append({'call_id':call['call_id'],'incidental_nonpooled_pmids':outside,'paper_input_count':len(call['pmids']),
                      'whole_call_actual_cost_usd':cost,'count_weighted_incidental_cost_estimate_usd':str(estimated) if estimated is not None else None})
    additions=read(WORK/'exact_scope_added_publications.json') if (WORK/'exact_scope_added_publications.json').exists() else {}
    added=set(additions.get('new_pmids',[]));standalone=set(scope.get('singleton_only_pmids',[]))
    records=read(WORK/'pooled_publications.json')
    covered={pmid for pmid in eligible if records.get(pmid,{}).get('saved_names') or (WORK/'answers'/f'{pmid}.json').exists()}
    result={'at':now(),'minimum_pooled_estimates':2,'exact_pooling_scope_sha256':hashlib.sha256((WORK/'exact_pooling_scope.json').read_bytes()).hexdigest(),'exact_engine_pooled_unique_pmids':len(eligible),
            'eligible_unique_pmids_including_standalone':len(scope.get('eligible_pmids_including_standalone',eligible)),
            'standalone_only_unique_pmids':len(standalone),'strict_pooled_name_assessment_covered':len(covered),
            'strict_pooled_name_assessment_coverage_fraction':len(covered)/len(eligible) if eligible else None,
            'strict_pooled_missing_name_assessment_pmids':sorted(eligible-covered),'actual_screened_unique_pmids':len(paid),
            'screened_currently_pooled_unique_pmids':len(paid&eligible),'incidental_nonpooled_only_unique_pmids':len(incidental),
            'incidental_nonpooled_only_pmids':incidental,'zero_nonpooled_only_dispatches':not incidental,
            'count_weighted_incidental_cost_estimate_usd':str(allocation),'calls_with_incidental_inputs':mixed,
            'allocation_note':'Mixed-call charges are actual; incidental cost allocation is an estimate weighted by PMID input count, not separately billed per paper.',
            'corrective_policy':'The initial broad numeric/flag inventory included incidental inputs and missed some pooled publications. Future screening requires an unchanged native final-engine witness with n>=2 estimates and latest selection off. Standalone n=1 results never authorize calls. Existing paid answers and charges remain preserved.',
            'exact_eligibility_additions':{'unique_pmids':len(added),'strictly_pooled_pmids':sorted(added&eligible),
                'standalone_only_pmids':sorted(added&standalone),'already_screened_standalone_only_pmids':sorted(added&standalone&paid)},
            'last_three_exactly_pooled':{pmid:pmid in eligible for pmid in ['32606938','39109892','18537156']},'cost':cost_summary()}
    write(WORK/'paid_screening_scope_audit.json',result)
    print({k:v for k,v in result.items() if k not in {'calls_with_incidental_inputs','incidental_nonpooled_only_pmids','cost'}},flush=True)
    return result


def retrieve_metadata(all_records=False,unverified_only=False):
    """Supplement missing abstract/date metadata from the public PubMed record."""
    import calendar
    import time
    import xml.etree.ElementTree as ET
    import requests
    records=read(WORK/'pooled_publications.json')
    requested=[k for k,v in records.items() if (not v.get('publication_date_raw') if unverified_only else all_records or not v['abstract'] or v['publication_date_precision'] not in {'day','month'})]
    receipts=[]
    for offset in range(0,len(requested),100):
        ids=requested[offset:offset+100];cache=WORK/'pubmed_metadata'/f'{digest(ids)}.xml'
        if cache.exists():data=cache.read_bytes()
        else:
            response=requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi',params={'db':'pubmed','id':','.join(ids),'retmode':'xml','tool':'CohortMetadataAudit','email':'yus4011@med.cornell.edu'},timeout=90)
            if response.status_code!=200:raise RuntimeError(f'PubMed metadata HTTP {response.status_code}')
            data=response.content;ET.fromstring(data)
            cache.parent.mkdir(parents=True,exist_ok=True);cache.write_bytes(data)
            time.sleep(.4)
        xml=ET.fromstring(data);resolved=[]
        for article in xml.findall('PubmedArticle'):
            pmid=article.findtext('./MedlineCitation/PMID')
            if pmid not in records:continue
            entry=records[pmid];resolved.append(pmid)
            abstract='\n'.join(''.join(n.itertext()) for n in article.findall('./MedlineCitation/Article/Abstract/AbstractText'))
            if not entry['abstract'] and abstract:entry['abstract']=abstract;entry['abstract_source']=str(cache.relative_to(ROOT))
            if not entry['title']:entry['title']=''.join(article.find('./MedlineCitation/Article/ArticleTitle').itertext())
            node=article.find('./MedlineCitation/Article/Journal/JournalIssue/PubDate')
            year=node.findtext('Year') if node is not None else None
            month=node.findtext('Month') if node is not None else None
            day=node.findtext('Day') if node is not None else None
            medline_date=node.findtext('MedlineDate') if node is not None else None
            original={'Year':year,'Month':month,'Day':day,'MedlineDate':medline_date}
            if not year and medline_date:
                year=(re.search(r'\b(1[89]\d\d|20\d\d)\b',medline_date) or [None])[0]
            if month and not month.isdigit():month=str(next((i for i in range(1,13) if month[:3].casefold()==calendar.month_abbr[i].casefold()),''))
            if year:
                date=year+(f'-{int(month):02}' if month else '')+(f'-{int(day):02}' if day and month else '')
                if all_records or unverified_only or len(date)>len(entry.get('publication_date','')):
                    entry['publication_date']=date;entry['publication_date_source']=str(cache.relative_to(ROOT));entry['publication_date_precision']={4:'year',7:'month',10:'day'}[len(date)]
                    entry['publication_date_raw']=original
        receipts.append({'pmids_requested':ids,'pmids_resolved':resolved,'source':str(cache.relative_to(ROOT)),'sha256':hashlib.sha256(data).hexdigest()})
        print('PubMed metadata',offset+len(ids),'/',len(requested),flush=True)
        write(WORK/'pooled_publications.json',records)
    write(WORK/'metadata_receipts.json',{'updated_at':now(),'receipts':receipts,'missing_abstract_pmids':[k for k,v in records.items() if not v['abstract']]})


def enrich_row_identities():
    records=read(WORK/'pooled_publications.json');assignments=defaultdict(dict);all_names=defaultdict(dict);count=0
    scope=read(WORK/'exact_pooling_scope.json') if (WORK/'exact_pooling_scope.json').exists() else {}
    # Name metadata may reuse all saved eligible records, including standalone
    # estimates. Only the separate strict multirow scope authorizes model calls.
    exact_pmids=set(scope.get('eligible_pmids_including_standalone',scope.get('pmids',[]))) if scope.get('status')=='verified' else set()
    for repo in REPOS:
        for root in cache_roots(repo):
            for path in sorted(root.glob('*/*.json')):
                try:payload=read(path)
                except (ValueError,OSError):continue
                if not isinstance(payload,dict):continue
                for row in payload.get('studies',[]):
                    if not isinstance(row,dict) or (not poolable(row) and pmid_of(row) not in exact_pmids):continue
                    pmid=pmid_of(row)
                    if pmid not in records:continue
                    source=str(path.relative_to(ROOT));names=names_from(row,source)+saved_text_names(row,source)
                    if not names:continue
                    all_names[pmid].update({n['canonical_name']:n for n in names})
                    for key in ['Effect ID','effect_record_id','Screening Record ID','record_id','context_id']:
                        identity=row.get(key)
                        if not meaningful(identity):continue
                        existing=assignments[pmid].setdefault(str(identity),{})
                        existing.update({n['canonical_name']:n for n in names})
    for pmid,mapping in assignments.items():
        records[pmid]['row_cohort_names']={key:list(names.values()) for key,names in mapping.items()}
        count+=len(mapping)
    for pmid,entry in records.items():
        structured={n['canonical_name']:n for n in entry['saved_names'] if n.get('field')!='saved_population_or_extraction_evidence'}
        structured.update(all_names.get(pmid,{}))
        entry['saved_names']=list(structured.values())
    write(WORK/'pooled_publications.json',records)
    write(WORK/'row_identity_audit.json',{'at':now(),'pmids_with_row_cohort_names':len(assignments),'row_identity_assignments':count,'source':'Exact saved Population and sample-size/design extraction evidence; no additional model calls'})


def validate(results, articles):
    if not isinstance(results,list) or len(results)!=len(articles):raise ValueError('Result count does not match input')
    inputs={a['pmid']:a for a in articles};seen=set()
    for item in results:
        pmid=str(item.get('pmid',''))
        if pmid not in inputs or pmid in seen:raise ValueError('Unexpected or duplicate PMID')
        seen.add(pmid)
        if item.get('status') not in {'identified','not_reported','ambiguous'}:raise ValueError('Invalid status')
        names=item.get('cohort_names')
        if not isinstance(names,list):raise ValueError('Missing cohort_names')
        source=inputs[pmid]['title']+'\n'+inputs[pmid]['abstract']
        for n in names:
            if not all(isinstance(n.get(k),str) and n[k].strip() for k in ['name','canonical_name','evidence']):raise ValueError('Invalid name/evidence')
            if re.sub(r'\s+',' ',n['evidence']).casefold() not in re.sub(r'\s+',' ',source).casefold():raise ValueError('Cohort evidence is not present in supplied title/abstract')
        for n in item.get('reported_source_names',[]):
            if not isinstance(n,dict) or not isinstance(n.get('evidence'),str) or re.sub(r'\s+',' ',n['evidence']).casefold() not in re.sub(r'\s+',' ',source).casefold():raise ValueError('Reported data-source evidence is not present in supplied title/abstract')
        if item['status']=='identified' and not names:raise ValueError('Identified cohort list empty')
        if item['status']=='not_reported' and names:raise ValueError('not_reported has cohort names')
    return results


def repair_saved_answers():
    """Salvage valid individual papers without dispatching another paid call."""
    repaired=[];unresolved=[]
    for target in sorted((WORK/'calls').glob('*.json')):
        call=read(target)
        if call.get('phase') not in {'failed','partially_salvaged'} or not (WORK/'responses'/f"{call['call_id']}.json").exists():continue
        request=read(WORK/'requests'/f"{call['input_digest']}.json")
        response=read(WORK/'responses'/f"{call['call_id']}.json")
        try:results=json.loads(response['choices'][0]['message']['content'])['results']
        except (KeyError,IndexError,ValueError,TypeError):unresolved.extend(call['pmids']);continue
        inputs={x['pmid']:x for x in request['articles']};valid=0
        returned_pmids={str(answer.get('pmid','')) for answer in results}
        omitted=sorted(set(inputs)-returned_pmids)
        unresolved.extend(omitted)
        for answer in results:
            pmid=str(answer.get('pmid',''));article=inputs.get(pmid)
            if not article:continue
            source=article['title']+'\n'+article['abstract']
            for name in answer.get('cohort_names',[]):
                evidence=name.get('evidence','')
                if re.sub(r'\s+',' ',evidence).casefold() in re.sub(r'\s+',' ',source).casefold():continue
                labels=[name.get('name',''),name.get('canonical_name','')]
                labels += [re.sub(r'\([^)]*\)','',s).strip() for s in labels]
                for label in labels:
                    words=re.findall(r'\w+',label)
                    match=re.search(r'\b'+r'\W+'.join(re.escape(w) for w in words)+r'\b',source,re.I) if words else None
                    if match:
                        name.update(model_original_evidence=evidence,evidence=match.group(),evidence_repair='Restricted to exact cohort name verbatim in supplied source');break
                if re.sub(r'\s+',' ',name.get('evidence','')).casefold() not in re.sub(r'\s+',' ',source).casefold() and meaningful(name.get('round')):
                    base=re.sub(r'\s*(?:cohort\s*)?(?:IV|III|II|I|[1234])\s*$','',name.get('name',''),flags=re.I).strip()
                    norm=lambda s:' '.join(re.findall(r'\w+',s.casefold()))
                    for sentence in re.split(r'(?<=[.!?])\s+',source):
                        if norm(base) in norm(sentence) and re.search(r'\b'+re.escape(str(name['round']))+r'\b',sentence,re.I):
                            name.update(model_original_evidence=evidence,evidence=sentence,evidence_repair='Exact source sentence explicitly names the parent study and numbered cohort');break
            try:validate([answer],[article])
            except ValueError:unresolved.append(pmid);continue
            write(WORK/'answers'/f'{pmid}.json',{**answer,'call_id':call['call_id'],'model':MODEL,'reasoning_effort':'xhigh','input_digest':call['input_digest'],'screened_at':call.get('received_at'),'local_validation_salvage':True})
            valid+=1;repaired.append(pmid)
        call.update(phase='partially_salvaged' if valid<len(inputs) else 'salvaged',salvaged_papers=valid,original_response_omitted_pmids=omitted)
        write(target,call)
    write(WORK/'validation_salvage.json',{'at':now(),'salvaged_pmids':repaired,'unresolved_pmids':unresolved,'additional_model_calls':0})
    print({'salvaged':len(repaired),'unresolved':unresolved},flush=True)


def validate_saved_answers():
    """Validate every final saved answer against its exact original paid input."""
    checked=0;errors=[]
    for path in sorted((WORK/'calls').glob('*.json')):
        call=read(path);request=read(WORK/'requests'/f"{call['input_digest']}.json")
        try:
            answers=[read(WORK/'answers'/f"{item['pmid']}.json") for item in request['articles']]
            validate(answers,request['articles']);checked+=len(answers)
        except (ValueError,OSError,KeyError) as exc:
            errors.append({'call_id':call['call_id'],'error':str(exc)});continue
        recovered={answer['pmid']:answer.get('call_id') for answer in answers if answer.get('call_id')!=call['call_id']}
        if recovered:
            call.update(phase='partially_salvaged_recovered',recovered_answer_calls=recovered,original_response_omitted_pmids=sorted(recovered),final_saved_answers_validated=True);write(path,call)
        elif call.get('phase')=='partially_salvaged':
            call.update(phase='salvaged',salvaged_papers=len(answers),final_saved_answers_validated=True);write(path,call)
    report={'at':now(),'model_calls_checked':len(list((WORK/'calls').glob('*.json'))),'publication_answers_checked':checked,'errors':errors}
    write(WORK/'final_answer_validation.json',report)
    if errors:raise RuntimeError('Final saved cohort answers failed source validation')


def cost_summary():
    calls=[]
    for path in sorted((WORK/'calls').glob('*.json')):
        value=read(path)
        if value.get('phase')!='preflight':calls.append(value)
    actual=sum((Decimal(str(x['gateway_cost_usd'])) for x in calls if x.get('gateway_cost_usd') is not None),Decimal(0))
    pmid_inputs=Counter(pmid for call in calls for pmid in call.get('pmids',[]))
    estimated=Decimal(0);estimate_calls=0
    for call in calls:
        usage=call.get('usage') or {}
        cached=(usage.get('prompt_tokens_details') or {}).get('cached_tokens',0)
        tokens=usage.get('prompt_tokens');out=usage.get('completion_tokens')
        if call.get('gateway_cost_usd') is None and tokens is not None and out is not None:
            estimated+=(Decimal(str(tokens-cached))*Decimal('.10')+Decimal(str(cached))*Decimal('.01')+Decimal(str(out))*Decimal('.50'))/Decimal(1000000);estimate_calls+=1
    summary={'model':MODEL,'reasoning_effort':'xhigh','updated_at':now(),'attempts':len(calls),'gateway_cost_usd':str(actual),
             'paper_input_occurrences':sum(pmid_inputs.values()),'unique_pmid_inputs':len(pmid_inputs),
             'repeated_pmid_inputs':{k:v for k,v in pmid_inputs.items() if v>1},
             'recovery_input_occurrences':sum(v-1 for v in pmid_inputs.values()),
             'recovery_reason':'Two original batch responses omitted a PMID result; only missing unsaved answers were requested again. Completed valid answers were never rerun.',
             'estimated_cost_usd':str(estimated),'accounted_cost_usd':str(actual+estimated),'estimated_cost_attempts':estimate_calls,
             'in_flight_attempts':sum(x.get('phase')=='dispatched' for x in calls),
             'unknown_cost_attempts':sum(x.get('phase')!='dispatched' and x.get('gateway_cost_usd') is None and not x.get('usage') for x in calls),
             'input_tokens':sum((x.get('usage') or {}).get('prompt_tokens',0) for x in calls),
             'output_tokens':sum((x.get('usage') or {}).get('completion_tokens',0) for x in calls),
             'reasoning_tokens':sum(((x.get('usage') or {}).get('completion_tokens_details') or {}).get('reasoning_tokens',0) for x in calls),
             'published_standard_rates_per_million_usd':{'input':'.10','cached_input':'.01','output':'.50','cache_write_30m':'.125','cache_write_12h':'.25'},
             'pricing_source':'https://developers.openai.com/api/docs/pricing','pricing_verified_on':'2026-10-25',
             'cost_basis':'Actual Cornell x-litellm-response-cost header; missing charges remain unknown; reasoning tokens are included in output tokens.'}
    write(WORK/'cost_summary.json',summary);return summary


def screen(batch_size=12,workers=6,limit=None,pmid_file=None):
    records=read(WORK/'pooled_publications.json');scope=read(WORK/'exact_pooling_scope.json')
    if scope.get('status')!='verified' or scope.get('minimum_pooled_estimates')!=2:raise RuntimeError('Strict multirow engine pooling receipts are incomplete; no paid calls are allowed')
    client=configure_client()
    available={x.id for x in client.models.list()}
    write(WORK/'model_preflight.json',{'at':now(),'model':MODEL,'available':MODEL in available,'paid_calls':0})
    if MODEL not in available:raise RuntimeError('Requested latest Luna unavailable; no substitution')
    queue=[{'pmid':k,'title':v['title'],'abstract':v['abstract']} for k,v in sorted(records.items()) if not v['saved_names'] and not (WORK/'answers'/f'{k}.json').exists()]
    if pmid_file:
        allowed=set(read(pmid_file));queue=[x for x in queue if x['pmid'] in allowed]
    outside=[item['pmid'] for item in queue if item['pmid'] not in scope.get('witnesses',{})]
    queue=[item for item in queue if item['pmid'] in scope.get('witnesses',{})]
    write(WORK/'screening_scope_gate.json',{'at':now(),'exact_pooling_scope_sha256':hashlib.sha256((WORK/'exact_pooling_scope.json').read_bytes()).hexdigest(),
          'authorized_pmids':[item['pmid'] for item in queue],'blocked_nonpooled_or_unverified_pmids':outside,'model_calls_for_blocked_pmids':0})
    if limit is not None:queue=queue[:limit]
    batches=[queue[i:i+batch_size] for i in range(0,len(queue),batch_size)]
    def call(articles):
        assert_exact_screening_scope(articles,scope)
        unit=digest(articles);callid=unit+'_'+dt.datetime.now().strftime('%Y%m%dT%H%M%S%f')
        target=WORK/'calls'/f'{callid}.json';receipt={'call_id':callid,'model_requested':MODEL,'reasoning_effort':'xhigh','input_digest':unit,'pmids':[a['pmid'] for a in articles],'requested_at':now(),'phase':'dispatched','gateway_cost_usd':None}
        write(WORK/'requests'/f'{unit}.json',{'model':MODEL,'reasoning_effort':'xhigh','max_completion_tokens':12000,'response_format':{'type':'json_object'},'system':SYSTEM,'articles':articles});write(target,receipt)
        try:
            raw=client.chat.completions.with_raw_response.create(model=MODEL,messages=[{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps({'articles':articles},ensure_ascii=False)}],reasoning_effort='xhigh',max_completion_tokens=12000,response_format={'type':'json_object'})
            response=raw.parse();payload=response.model_dump(mode='json')
            usage=payload.get('usage') or {};charge=raw.headers.get('x-litellm-response-cost')
            if charge is not None:
                d=Decimal(charge)
                if d.is_finite() and d>=0:receipt['gateway_cost_usd']=str(d)
            receipt.update({'received_at':now(),'phase':'received','model_reported':payload.get('model'),'usage':usage,'request_id':raw.headers.get('x-request-id')})
            write(WORK/'responses'/f'{callid}.json',payload);write(target,receipt)
            reported=str(payload.get('model') or '')
            if MODEL not in reported:raise ValueError('Gateway returned a different model')
            output=payload['choices'][0]['message']['content'];parsed=json.loads(output)
            result=validate(parsed.get('results'),articles)
            for answer in result:write(WORK/'answers'/f"{answer['pmid']}.json",{**answer,'call_id':callid,'model':MODEL,'input_digest':unit,'screened_at':now()})
            receipt['phase']='accepted';write(target,receipt)
            with LOCK:
                print('accepted',len(articles),'cost',receipt['gateway_cost_usd'],flush=True);cost_summary()
            return True
        except Exception as exc:
            receipt.update({'phase':'failed','error_type':type(exc).__name__,'status_code':getattr(exc,'status_code',None)})
            write(target,receipt)
            with LOCK:cost_summary();print('failed',len(articles),receipt['error_type'],receipt.get('status_code'),flush=True)
            return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        outcomes=list(ex.map(call,batches))
    print(json.dumps({'batches':len(batches),'accepted':sum(outcomes),'failed':len(outcomes)-sum(outcomes),'cost':cost_summary()},indent=2))
    publish()


def publish():
    records=read(WORK/'pooled_publications.json');byrepo=defaultdict(dict);statuses=Counter()
    for pmid,record in records.items():
        if record['saved_names']:
            answer={'status':'identified','cohort_names':record['saved_names'],'provenance':'saved_population_or_extraction_evidence' if any(n.get('field')=='saved_population_or_extraction_evidence' for n in record['saved_names']) else 'saved_structured'}
        elif (WORK/'answers'/f'{pmid}.json').exists():
            answer={**read(WORK/'answers'/f'{pmid}.json'),'provenance':'luna_title_abstract'}
        else:answer={'status':'not_screened_not_pooled' if record.get('actual_pooled_anywhere') is False else 'pending','cohort_names':[],'provenance':'not_screened'}
        names=[]
        for n in answer['cohort_names']:
            names.append(canonical_cohort(n))
        result={**answer,'cohort_names':names,'publication_date':record.get('publication_date') or None,
                'actual_pooled_anywhere':record.get('actual_pooled_anywhere'),
                'actual_pooled_repositories':record.get('actual_pooled_repositories',[]),
                'actual_eligible_including_standalone':record.get('actual_eligible_including_standalone'),
                'standalone_only_anywhere':record.get('standalone_only_anywhere'),
                'publication_date_precision':record['publication_date_precision'],'publication_date_source':record.get('publication_date_source'),
                'publication_date_raw':record.get('publication_date_raw'),
                'screening_input_type':'title_and_abstract' if record['abstract'] else 'title_only_abstract_unavailable',
                'row_cohort_names':{key:[canonical_cohort(n) for n in vals] for key,vals in record.get('row_cohort_names',{}).items()},
                'title':record['title'],'abstract_source':record.get('abstract_source'),'abstract_sha256':hashlib.sha256(record['abstract'].encode()).hexdigest() if record['abstract'] else None}
        statuses[result['status']]+=1
        for repo in record['repositories']:byrepo[repo][pmid]=result
    summary=cost_summary()
    for repo,items in byrepo.items():
        path=ROOT/repo/('metaOnco/data/cohort_registry.json' if repo=='metaMeta' else 'data/cohort_registry.json')
        previous=read(path) if path.exists() else {};existing=previous.get('records',{})
        for pmid,new in items.items():
            old=existing.get(pmid)
            if old:
                history=list(old.get('metadata_history',[]))
                fields=['cohort_names','row_cohort_names','publication_date','publication_date_precision','status','provenance']
                if any(old.get(field)!=new.get(field) for field in fields):history.append({key:value for key,value in old.items() if key!='metadata_history'})
                if history:new={**new,'metadata_history':history}
            existing[pmid]=new
        payload={'schema_version':1,'generated_at':now(),'model':MODEL,'records':dict(sorted(existing.items())),
                 'policy':'Cohort rounds are separate; unknown names/dates retained; source analyses unchanged.',
                 'shared_screening_cost':summary,'deferred_campaigns':read(WORK/'audit.json')['deferred_campaigns'] if repo=='metaGut' else []}
        write(path,payload)
    model_records=[v for k,v in records.items() if not v['saved_names']]
    input_pmids={};input_counts=Counter()
    for call_path in (WORK/'calls').glob('*.json'):
        call=read(call_path);request=read(WORK/'requests'/f"{call['input_digest']}.json")
        for item in request['articles']:
            input_counts['title_and_abstract' if item.get('abstract') else 'title_only']+=1
            input_pmids.setdefault(item['pmid'],False)
            input_pmids[item['pmid']]=input_pmids[item['pmid']] or bool(item.get('abstract'))
    scope=read(WORK/'exact_pooling_scope.json')
    strict=set(scope.get('pmids',[]))
    missing_assessments=[pmid for pmid in strict if not records.get(pmid,{}).get('saved_names') and not (WORK/'answers'/f'{pmid}.json').exists()]
    write(WORK/'publication_report.json',{'generated_at':now(),'total_publications':len(records),
          'saved_names_reused':sum(bool(v['saved_names']) for v in records.values()),
          'saved_structured_names':sum(any(n.get('field')!='saved_population_or_extraction_evidence' for n in v['saved_names']) for v in records.values()),
          'luna_screened_publications':sum((WORK/'answers'/f'{k}.json').exists() for k,v in records.items() if not v['saved_names']),
          'registry_records_using_luna_output':sum((WORK/'answers'/f'{k}.json').exists() for k,v in records.items() if not v['saved_names']),
          'actual_dispatched_unique_pmids':summary['unique_pmid_inputs'],
          'actual_paid_paper_input_occurrences':summary['paper_input_occurrences'],
          'validated_unique_luna_answers':len(list((WORK/'answers').glob('*.json'))),
          'screened_papers_later_reused_saved_name_evidence':sum(bool(v['saved_names']) and (WORK/'answers'/f'{k}.json').exists() for k,v in records.items()),
          'actual_unique_title_and_abstract_inputs':sum(input_pmids.values()),
          'actual_unique_title_only_inputs':sum(not available for available in input_pmids.values()),
          'actual_title_and_abstract_input_occurrences':input_counts['title_and_abstract'],
          'actual_title_only_input_occurrences':input_counts['title_only'],
          'coverage_accounting_note':'Actual paid unique PMID inputs differ from final registry provenance when saved cohort evidence is found later. Missing batch results were recovered separately; valid completed answers were never rerun.',
          'luna_title_and_abstract_inputs':sum(bool(v['abstract']) for v in model_records),
          'luna_title_only_abstract_unavailable_inputs':sum(not v['abstract'] for v in model_records),
          'title_only_pmids':[k for k,v in records.items() if not v['saved_names'] and not v['abstract']],
          'publication_date_precision':dict(Counter(v['publication_date_precision'] for v in records.values())),
          'strict_pooled_publications':len(strict),'minimum_pooled_estimates':2,
          'strict_pooled_name_assessment_covered':len(strict)-len(missing_assessments),
          'strict_pooled_missing_name_assessment_pmids':sorted(missing_assessments),
          'strict_pooled_title_and_abstract_available':sum(bool(records[pmid]['abstract']) for pmid in strict),
          'strict_pooled_title_only_abstract_unavailable':sum(not records[pmid]['abstract'] for pmid in strict),
          'standalone_only_publications':len(scope.get('singleton_only_pmids',[])),
          'source_only_publications':sum(bool(read(WORK/'answers'/f'{pmid}.json').get('reported_source_names')) and not v['saved_names'] and not read(WORK/'answers'/f'{pmid}.json')['cohort_names'] for pmid,v in records.items() if (WORK/'answers'/f'{pmid}.json').exists()),
          'status_counts':dict(statuses),'repositories':{r:len(v) for r,v in byrepo.items()},
          'cost':summary,'deferred_campaigns':read(WORK/'audit.json')['deferred_campaigns'],
          'unknown_identity_policy':'No name reported or ambiguous names remain included; unknown identities are never merged.'})
    print(json.dumps({'status_counts':dict(statuses),'registry_counts':{r:len(v) for r,v in byrepo.items()}},indent=2),flush=True)


def canonical_cohort(name):
    """Unify known spelling/acronym variants while preserving cohort rounds."""
    original=name['canonical_name'];label=original
    matches=saved_text_names({'Population':original},'canonical_alias_policy')
    if len(matches)==1:
        matched=matches[0].get('name','');where=re.search(re.escape(matched),original,re.I) if matched else None
        residual=(original[:where.start()]+' '+original[where.end():]) if where else original
        allowed={'study','cohort','trial','the','prospective','randomized','randomised','follow','up','followup','original',
                 'i','ii','iii','iv','v','1','2','3','4','5','nhs','hpfs','ukb','nih','aarp','plco','epic','whi','os','whs',
                 'mec','nlcs','atbc','cps','ahs','swhs','smhs','jphc','jacc','mccs','aric','mesa','cardia','ordet','kihd'}
        # Only complete, explicit aliases are abbreviated. Unmatched country,
        # region, subcohort and generation qualifiers must remain in the name.
        if where and set(re.findall(r'\w+',residual.casefold())).issubset(allowed):label=matches[0]['canonical_name']
        # Keep an explicitly named, unfamiliar EPIC subcohort distinct from
        # the full parent cohort instead of shortening it to bare EPIC.
        if re.match(r'EPIC[- ]',original,re.I) and label=='EPIC' and not re.fullmatch(r'EPIC(?: study| cohort)?',original,re.I):label=original
        if label=="Women's Health Study" and re.search(r'\S+\s+Women[’\']s Health Study',original,re.I):label=original
    combined=original+' '+str(name.get('name') or '')
    # Preserve geographic qualifiers (e.g. Korean/KNHANES) and explicitly
    # named Framingham generations. Alias matching never uses a substring
    # such as NHANES inside KNHANES.
    framingham_tokens=set(re.findall(r'\w+',original.casefold()))
    if 'framingham' in framingham_tokens and framingham_tokens.issubset({'framingham','heart','study','cohort','original','offspring'}):
        label='Framingham'+(' Offspring' if 'offspring' in framingham_tokens else '')+' Heart Study'
    suffix=name.get('round')
    if not meaningful(suffix):
        match=re.search(r'(?:cohort|study)?\s+(III|II|I|[1234])\s*$',original,re.I)
        if match:suffix=match.group(1)
    if meaningful(suffix):
        suffix={'1':'I','2':'II','3':'III','4':'IV'}.get(str(suffix).upper(),str(suffix).upper())
        # The first NHS cohort is conventionally the unsuffixed NHS.
        if label=="Nurses' Health Study" and suffix=='I':suffix=None
        equivalents={'I':'1','II':'2','III':'3','IV':'4'}
        target=equivalents.get(suffix,suffix)
        existing_rounds=[equivalents.get(token.upper(),token) for token in re.findall(r'\b(?:IV|III|II|I|[0-9]+)\b',label,re.I)]
        existing_rounds.extend(re.findall(r'(?<=[A-Za-z])([0-9]+)\b',label))
        literal_present=bool(suffix and re.search(r'(?<!\w)'+re.escape(str(suffix))+r'(?!\w)',label,re.I))
        if suffix and not literal_present and target not in existing_rounds:label+=' '+suffix
    ascii_label=unicodedata.normalize('NFKD',label).encode('ascii','ignore').decode().casefold()
    return {**name,'canonical_name':label,'cohort_id':re.sub(r'[^a-z0-9]+','_',ascii_label).strip('_'),'round':suffix if meaningful(suffix) else None}


def persist_record_metadata():
    """Persist cohort/date metadata for every saved active study/effect row."""
    import importlib.util
    reports={}
    for repo in [*REPOS,'metaMeta']:
        root=ROOT/repo/('metaOnco' if repo=='metaMeta' else '')
        spec=importlib.util.spec_from_file_location('cohort_helper_'+repo.replace(' ','_'),root/'cohort_identity.py')
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        registry=read(root/'data/cohort_registry.json')['records']
        path=root/'data/cohort_record_metadata.json';previous=read(path) if path.exists() else {}
        output={k:v for k,v in previous.get('records',{}).items()};aliases=dict(previous.get('record_aliases',{}));alias_owners=defaultdict(set);sources={};conflicts=[];row_count=0;active_keys=set()
        if repo=='metaMeta':paths=sorted((root/'data').glob('metaonco*.json'))
        else:paths=sorted(set(p for cache in cache_roots(repo) for p in cache.glob('*/*.json')))
        for cache in paths:
            try:
                raw=cache.read_bytes();payload=json.loads(raw)
            except (ValueError,OSError):continue
            if not isinstance(payload,dict):continue
            relative=str(cache.relative_to(root));source_rows=[]
            if repo=='metaMeta':
                for ri,result in enumerate(payload.get('results',[])):
                    for dataset in ['combined','dietary']:
                        for ei,row in enumerate((result.get(dataset) or {}).get('evidence',[])):
                            source_rows.append((row,{'cache_path':relative,'result_index':ri,'dataset':dataset,'evidence_index':ei,'exposure':result.get('exposure_id'),'disease':result.get('cancer_id')}))
            else:source_rows=[(row,{'cache_path':relative,'row_index':i}) for i,row in enumerate(payload.get('studies',[])) if isinstance(row,dict)]
            if not source_rows:continue
            sources[relative]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'study_rows':len(source_rows)}
            for row,reference in source_rows:
                row_count+=1;key=helper.record_metadata_key(row);seen_in_current_inventory=key in active_keys;active_keys.add(key);pmid=helper._pmid(row);record=registry.get(pmid,{})
                values,provenance=helper._cohort_values(row,record)
                if isinstance(values,str):values=[x.strip() for x in re.split(r';|\|',values) if meaningful(x)]
                elif isinstance(values,dict):values=[values]
                names=[]
                for item in values or []:
                    if isinstance(item,dict):names.append(dict(item))
                    elif meaningful(item):names.append(canonical_cohort({'name':str(item),'canonical_name':str(item),'round':None,'evidence':'Saved explicit cohort name field','source':relative}))
                poolable_here=repo=='metaMeta' or poolable(row)
                if not names and not record and not poolable_here:
                    names=[canonical_cohort(n) for n in names_from(row,relative)+saved_text_names(row,relative)]
                    if names:provenance='saved_nonpooled_row'
                if names:status='identified'
                elif record:status=record.get('status') or 'not_reported'
                else:status='not_screened_not_pooled' if not poolable_here else 'unresolved_missing_pooled_metadata'
                if status=='ambiguous':names=list(record.get('cohort_names') or [])
                date=record.get('publication_date');precision=record.get('publication_date_precision')
                if not date:
                    date=next((str(row[k]).strip() for k in ['publication_date','Publication Date','Year','year'] if meaningful(row.get(k))),None)
                    precision={4:'year',7:'month',10:'day'}.get(len(date or ''),'unknown')
                entry={'record_id':key,'pmid':pmid,'cohort_names':names,
                       'study_names':[n.get('canonical_name') or n.get('name') or n.get('cohort_id') for n in names],
                       'publication_date':date,'publication_date_precision':precision or 'unknown',
                       'publication_date_raw':record.get('publication_date_raw'),'status':status,
                       'provenance':provenance,'publication_provenance':record.get('provenance'),
                       'reported_source_names':record.get('reported_source_names',[]),'cohort_identity_status':status,
                       'source_identity_review':record.get('source_review'),
                       'actual_pooled_publication_anywhere':record.get('actual_pooled_anywhere'),
                       'actual_eligible_publication_including_standalone':record.get('actual_eligible_including_standalone'),
                       'standalone_only_publication':record.get('standalone_only_anywhere'),
                       'cohort_evidence':[{'text':n.get('evidence'),'source':n.get('source'),'field':n.get('field')} for n in names],
                       'source_records':[reference],'poolable_anywhere':poolable_here,
                       'paper_screening_call_id':record.get('call_id'),'model':record.get('model'),
                       'reasoning_effort':'xhigh' if record.get('call_id') else None}
                old=output.get(key)
                if old and not seen_in_current_inventory:
                    fields=['cohort_names','publication_date','publication_date_precision','status','provenance',
                            'reported_source_names','cohort_identity_status','source_identity_review',
                            'actual_pooled_publication_anywhere','actual_eligible_publication_including_standalone','standalone_only_publication']
                    if any(old.get(field)!=entry.get(field) for field in fields):
                        history=list(old.get('metadata_history',[]));history.append({k:v for k,v in old.items() if k not in {'metadata_history','source_records'}})
                        entry['metadata_history']=history
                        entry['source_records']=list(old.get('source_records',[]))
                        if reference not in entry['source_records']:entry['source_records'].append(reference)
                        entry['poolable_anywhere']=bool(old.get('poolable_anywhere')) or poolable_here
                        output[key]=entry;old=entry
                if old:
                    before=helper._cohorts(old.get('cohort_names'));after=helper._cohorts(names)
                    if old.get('pmid') and pmid and old['pmid']!=pmid or before and after and before!=after:
                        conflicts.append({'record_id':key,'previous_pmid':old.get('pmid'),'new_pmid':pmid,'previous_cohort_ids':list(before),'new_cohort_ids':list(after),'source_record':reference})
                        history=list(old.get('metadata_history',[]));history.append({k:v for k,v in entry.items() if k!='source_records'})
                        old['metadata_history']=history
                        # Preserve the previous assignment; conflicting record IDs
                        # are explicitly unresolved rather than silently replaced.
                        old['status']='ambiguous_record_identity_collision'
                        old.setdefault('source_records',[]).append(reference);continue
                    references=old.get('source_records',[])
                    if reference not in references:references.append(reference)
                    if not before and after:
                        history=list(old.get('metadata_history',[]));history.append({k:v for k,v in old.items() if k not in {'source_records','metadata_history'}})
                        entry['metadata_history']=history;entry['source_records']=references
                    else:
                        entry=old;entry['source_records']=references
                    entry['poolable_anywhere']=bool(old.get('poolable_anywhere')) or poolable_here
                output[key]=entry
                for field in ['Effect ID','effect_record_id','Screening Record ID','record_id','context_id']:
                    raw_id=row.get(field)
                    if meaningful(raw_id):
                        alias=str(raw_id) if str(raw_id).startswith('id:') else 'id:'+str(raw_id)
                        alias_owners[alias].add(key)
        for alias,owners in alias_owners.items():
            if len(owners)==1:aliases[alias]=next(iter(owners))
            else:aliases.pop(alias,None)
        # A directly indexed record always wins over an alternate-ID alias.
        aliases={k:v for k,v in aliases.items() if k not in output or k==v}
        payload={'schema_version':1,'generated_at':now(),'repository':repo,'records':dict(sorted(output.items())),
                 'record_aliases':aliases,'ambiguous_record_aliases':sorted(k for k,v in alias_owners.items() if len(v)>1),'source_files':dict(sorted(sources.items())),
                 'registry_sha256':hashlib.sha256((root/'data/cohort_registry.json').read_bytes()).hexdigest(),
                 'record_identity_helper_sha256':hashlib.sha256((root/'cohort_identity.py').read_bytes()).hexdigest(),
                 'key_policy':'cohort_identity.record_metadata_key(row): existing effect/record ID, otherwise normalized scientific-row hash',
                 'preservation_policy':'Additive authoritative metadata overlay; source cache bytes are unchanged. Prior entries/history retained.',
                 'coverage':{'active_source_row_occurrences':row_count,'active_record_keys':len(active_keys),'saved_records_including_history':len(output),'status_counts':dict(Counter(output[k]['status'] for k in active_keys))},
                 'identity_conflicts':conflicts}
        write(path,payload);reports[repo]={**payload['coverage'],'identity_conflicts':len(conflicts),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        print(repo,reports[repo],flush=True)
    write(WORK/'record_metadata_report.json',{'at':now(),'repositories':reports,'additional_model_calls':0})


def verify_record_metadata():
    """Verify complete record coverage/source preservation and unambiguous aliases."""
    import importlib.util
    report={}
    for repo in [*REPOS,'metaMeta']:
        root=ROOT/repo/('metaOnco' if repo=='metaMeta' else '')
        spec=importlib.util.spec_from_file_location('cohort_verify_'+repo.replace(' ','_'),root/'cohort_identity.py')
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        path=root/'data/cohort_record_metadata.json';metadata=read(path);registry=read(root/'data/cohort_registry.json')['records'];owners=defaultdict(set)
        errors=[];checked=0;pooled_checked=0
        for relative,source in metadata['source_files'].items():
            raw=(root/relative).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=source['sha256']:errors.append({'source_changed':relative});continue
            payload=json.loads(raw)
            rows=(row for res in payload.get('results',[]) for dataset in ['combined','dietary'] for row in (res.get(dataset) or {}).get('evidence',[])) if repo=='metaMeta' else payload.get('studies',[])
            for row in rows:
                checked+=1;key=helper.record_metadata_key(row);entry=metadata['records'].get(key)
                if entry is None:errors.append({'missing_metadata':key,'source':relative});continue
                for field in ['Effect ID','effect_record_id','Screening Record ID','record_id','context_id']:
                    if meaningful(row.get(field)):
                        alias=str(row[field]);alias=alias if alias.startswith('id:') else 'id:'+alias;owners[alias].add(key)
                if repo=='metaMeta' or poolable(row):
                    pooled_checked+=1;values,_=helper._cohort_values(row,registry.get(helper._pmid(row),{}))
                    expected=helper._cohorts(values)
                    actual=() if entry['status'].startswith('ambiguous') else helper._cohorts(entry['cohort_names'])
                    if expected!=actual:errors.append({'identity_mismatch':key,'expected':list(expected),'actual':list(actual),'source':relative})
        metadata['record_aliases']={alias:next(iter(keys)) for alias,keys in owners.items() if len(keys)==1 and (alias not in metadata['records'] or alias==next(iter(keys)))}
        metadata['ambiguous_record_aliases']=sorted(alias for alias,keys in owners.items() if len(keys)>1)
        metadata['verification']={'at':now(),'source_row_occurrences_checked':checked,'poolable_row_occurrences_checked':pooled_checked,'source_cache_hashes_unchanged':not any('source_changed' in e for e in errors),'missing_or_mismatched_records':len(errors),'errors':errors}
        write(path,metadata)
        report[repo]={**metadata['verification'],'sidecar_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'ambiguous_aliases_omitted':len(metadata['ambiguous_record_aliases'])}
        print(repo,{k:v for k,v in report[repo].items() if k!='errors'},flush=True)
        if errors:print('ERRORS',errors[:5],flush=True)
    write(WORK/'record_metadata_verification.json',{'at':now(),'repositories':report})
    if any(x['missing_or_mismatched_records'] for x in report.values()):raise RuntimeError('Record metadata verification failed')


def export_evidence():
    """Package public inputs/outputs once and publish durable per-repo receipts."""
    import shutil
    import tarfile
    import xml.etree.ElementTree as ET
    script=Path(__file__).resolve();code_sha=hashlib.sha256(script.read_bytes()).hexdigest()
    shared=ROOT/'metaMeta/metaOnco/data/cohort_registry_audit';shared.mkdir(parents=True,exist_ok=True)
    metadata=[]
    for path in sorted((WORK/'pubmed_metadata').glob('*.xml')):
        xml=ET.fromstring(path.read_bytes())
        metadata.append({'file':'pubmed_metadata/'+path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                         'pmids':[node.text for node in xml.findall('./PubmedArticle/MedlineCitation/PMID')]})
    write(WORK/'metadata_source_index.json',{'at':now(),'source_files':metadata,'publication_date_policy':'Original JournalIssue/PubDate precision, no artificial day/month padding'})
    source_paths=set()
    for row in read(WORK/'pooled_publications.json').values():
        names=list(row['saved_names'])+[n for vals in row.get('row_cohort_names',{}).values() for n in vals]
        source_paths.update(n['source'] for n in names if n.get('source'))
    sources=[]
    for relative in sorted(source_paths):
        path=ROOT/relative
        if not path.is_file():continue
        h=hashlib.sha256()
        with path.open('rb') as handle:
            while chunk:=handle.read(1024*1024):h.update(chunk)
        sources.append({'path':relative,'sha256':h.hexdigest(),'bytes':path.stat().st_size})
    write(WORK/'saved_name_source_hashes.json',{'at':now(),'files':sources})
    include_files=['cohort_backfill.py','audit.json','pooled_publications.json','cost_summary.json','publication_report.json',
                   'model_preflight.json','metadata_receipts.json','metadata_source_index.json','row_identity_audit.json',
                   'source_review_adjudications.json','validation_salvage.json','name_mention_suspects.json','saved_name_source_hashes.json',
                   'record_metadata_report.json','record_metadata_verification.json','record_metadata_final_binding.json','cohort_identity.py','final_answer_validation.json',
                   'exact_pooling_scope.json','paid_screening_scope_audit.json','exact_scope_added_publications.json',
                   'systematic_name_qc.json','database_source_identity_qc.json','database_name_candidates.json',
                   'last_three_pooled_witnesses.json','cohort_backfill_qc_test.py','capture_actual_pooling.py','capture_actual_pooled.py',
                   'screening_scope_gate.json','strict_paid_scope_receipt_verification.json','display_fix_identity_invariance.json']
    include_files+=['pooling_eligibility_invariance_report.json','eligibility_invariance_before.json']
    include_files+=['audit_metagut_release_reproducibility.py','metagut_release_reproducibility_audit.json',
                    'build_latest_summaries.py','Gut_summary_runner_compatibility.json']
    include_files+=sorted(p.name for p in WORK.glob('actual_pooled_*.json'))
    include_dirs=['calls','requests','responses','answers','pubmed_metadata','eligibility_invariance_snapshots',
                  'metagut_release_reproducibility']
    archive=shared/'screening_evidence.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for name in include_files+include_dirs:
            path=WORK/name
            if path.exists():tar.add(path,arcname=name)
    archive_sha=hashlib.sha256(archive.read_bytes()).hexdigest();cost=cost_summary()
    scope_audit=read(WORK/'paid_screening_scope_audit.json')
    scope=read(WORK/'exact_pooling_scope.json')
    repo_paths={repo:ROOT/repo/'data/cohort_registry.json' for repo in REPOS}
    repo_paths['metaMeta']=ROOT/'metaMeta/metaOnco/data/cohort_registry.json'
    for repo,registry in repo_paths.items():
        if not registry.exists():continue
        payload=read(registry);items=payload['records'];folder=registry.parent/'cohort_registry_audit';folder.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(script,folder/'cohort_backfill.py')
        release_proof=None
        if repo=='metaGut' and (WORK/'metagut_release_reproducibility_audit.json').exists():
            proof=WORK/'metagut_release_reproducibility_audit.json'
            shutil.copyfile(proof,folder/'release_reproducibility.json')
            shutil.copyfile(WORK/'audit_metagut_release_reproducibility.py',folder/'audit_release_reproducibility.py')
            release_proof={'path':'data/cohort_registry_audit/release_reproducibility.json',
                           'sha256':hashlib.sha256(proof.read_bytes()).hexdigest(),
                           'status':read(proof)['status'],'shared_archive_evidence_prefix':'metagut_release_reproducibility'}
        pmids=set(items)
        receipts=[call for p in sorted((WORK/'calls').glob('*.json')) if set((call:=read(p)).get('pmids',[])).intersection(pmids)]
        write(folder/'relevant_call_receipts.json',{'cost_scope':'Shared cross-repository job; subset receipts overlap and must not be summed across repositories.',
              'selection_rule':'Every call with an input PMID belonging to this repository, including omitted-result attempts/recoveries and papers later assigned saved-name provenance.','receipts':receipts})
        write(folder/'manifest.json',{'schema_version':1,'at':now(),'repository':repo,'registry_sha256':hashlib.sha256(registry.read_bytes()).hexdigest(),
              'producing_script_sha256':code_sha,'records':len(items),'status_counts':dict(Counter(v['status'] for v in items.values())),
              'release_reproducibility_proof':release_proof,
              'record_metadata_sha256':hashlib.sha256((registry.parent/'cohort_record_metadata.json').read_bytes()).hexdigest() if (registry.parent/'cohort_record_metadata.json').exists() else None,
              'record_metadata_coverage':read(registry.parent/'cohort_record_metadata.json').get('coverage') if (registry.parent/'cohort_record_metadata.json').exists() else None,
              'strict_pooling_scope':{'minimum_pooled_estimates':2,'global_pmids':len(scope['pmids']),
                  'source_bound_scope_sha256':hashlib.sha256((WORK/'exact_pooling_scope.json').read_bytes()).hexdigest(),
                  'repository_witnessed_pmids':sum(any(w['repository']==repo for w in scope['witnesses'].get(pmid,[])) for pmid in scope['pmids']),
                  'name_assessment_coverage_fraction':scope_audit['strict_pooled_name_assessment_coverage_fraction']},
              'historical_incidental_screening':{key:scope_audit[key] for key in ['incidental_nonpooled_only_unique_pmids','count_weighted_incidental_cost_estimate_usd','allocation_note']},
              'shared_cost':cost,'shared_evidence_repository':'metaMeta','shared_evidence_path':'metaOnco/data/cohort_registry_audit/screening_evidence.tar.gz',
              'shared_evidence_sha256':archive_sha,'shared_evidence_bytes':archive.stat().st_size,
              'shared_evidence_contains':'Exact title/abstract inputs, prompts, raw model responses, validated answers, PubMed XML, full call/cost ledger, source reviews and producing code',
              'deferred_campaigns':payload.get('deferred_campaigns',[])})
        (folder/'README.md').write_text('''# Cohort identity audit

The cohort registry adds identity/date metadata without rewriting saved effect
estimates. Explicit names in saved fields, Population, and extraction evidence
were reused. Missing names were screened with `gpt-6-luna`, `xhigh` reasoning;
valid answers were reused across repositories. Two batch omissions required
separate recovery inputs, both included in the attempt and cost ledger.
Numbered cohorts remain separate. Unknown identities, uncertain dates and ties
are retained. An accepted newer publication excludes definitely older overlapping
cohort reports within the actual analysis; combined coefficients remain whole.
Database names alone are preserved as reported sources, with ambiguous cohort
identity, so unrelated database populations cannot be deduplicated by name.

Every active saved effect/study record has durable metadata in
`data/cohort_record_metadata.json`, including source-row references, names,
publication date/precision, status, evidence and provenance. Nonpooled records
reuse known names or remain explicitly unknown without additional screening.

Future paid calls require native-engine receipts proving actual statistical
pooling of at least two final eligible estimates with latest-cohort selection
disabled. Successful one-row standalone estimates do not authorize calls.
`exact_pooling_scope.json` binds this strict union to source and policy hashes.
The initial broader inventory caused incidental screening outside this final
strict union. `paid_screening_scope_audit.json` lists every such PMID and its
count-weighted estimated allocation of mixed-call charges. These historical
charges and answers are preserved transparently. The allocation is an estimate;
gateway whole-call USD is authoritative. The strict pooled union has a saved-name
or valid title/abstract assessment for every publication; no reported name and
ambiguous identity remain valid assessment outcomes and are always retained.

`manifest.json` binds the registry, record metadata, producing code and complete
shared public evidence archive in the metaMeta repository at
`metaOnco/data/cohort_registry_audit/screening_evidence.tar.gz`. The archive holds
exact model inputs/prompts, raw responses, validated answers, original PubMed XML,
all request/cost receipts, name reviews, strict scope receipts and producing code.
`relevant_call_receipts.json` is a subset of the shared ledger; costs overlap
across repositories and must not be summed again.

To reproduce, extract the archive and set `COHORT_WORKSPACE_ROOT` to the directory
containing the repositories and `COHORT_WORK_DIR` to the extracted work directory.
`python cohort_backfill.py publish`, `records`, and `verify-records` regenerate
metadata from saved inputs/answers without model calls. For a future refresh,
collect fresh native engine receipts, run `scope`, `prepare-exact`, `enrich`, and
`retrieve --unverified-only` before `screen`. Screening is fail-closed if strict
witnesses or their source/policy hashes are missing or changed. Offline name/scope
regressions are in `cohort_backfill_qc_test.py`.

metaGut's October 24 IBD campaign remains deferred: R3 is blocked with 14 unresolved
full-text batches after two retry passes and no final extraction/release. The
campaign was not altered or screened by this workflow.
''',encoding='utf-8')
        if release_proof:
            with (folder/'README.md').open('a',encoding='utf-8') as handle:
                handle.write('''
The MetaGut clean-release audit independently assembled the Git index (HEAD plus
this task's staged code) and compared it with the same code plus preexisting IBD
changes, using identical immutable caches and metadata. Every active query was
evaluated with both latest settings, combined/dietary measurement, yes/no hierarchy,
and all/core variants. Numerical summaries, final pooled-row hashes and selected
views matched exactly. The shared archive includes both imported producer snapshots
and full comparison receipts. The scope planner recursively falls back to the
original ui_scope_records API, so the release does not require the unstaged IBD app
helper. The paid guard verifies this proof and pins the exact audited producer
variant present; an unproven code change requires a new native capture.
''')
    print({'shared_archive':str(archive.relative_to(ROOT)),'bytes':archive.stat().st_size,'sha256':archive_sha},flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['prepare','scope','prepare-exact','refresh-scope-flags','audit-paid-scope','retrieve','enrich','repair','validate-answers','screen','publish','records','verify-records','export','cost']);parser.add_argument('--batch-size',type=int,default=12);parser.add_argument('--workers',type=int,default=6);parser.add_argument('--limit',type=int);parser.add_argument('--all-metadata',action='store_true');parser.add_argument('--unverified-only',action='store_true');parser.add_argument('--pmid-file',type=Path)
    args=parser.parse_args()
    if args.stage=='prepare':prepare()
    elif args.stage=='scope':compile_exact_pooling_scope()
    elif args.stage=='prepare-exact':prepare_exact_pooling_publications()
    elif args.stage=='refresh-scope-flags':refresh_exact_pooling_flags()
    elif args.stage=='audit-paid-scope':audit_paid_screening_scope()
    elif args.stage=='retrieve':retrieve_metadata(args.all_metadata,args.unverified_only)
    elif args.stage=='enrich':enrich_row_identities()
    elif args.stage=='repair':repair_saved_answers()
    elif args.stage=='validate-answers':validate_saved_answers()
    elif args.stage=='screen':screen(args.batch_size,args.workers,args.limit,args.pmid_file)
    elif args.stage=='publish':publish()
    elif args.stage=='records':persist_record_metadata()
    elif args.stage=='verify-records':verify_record_metadata()
    elif args.stage=='export':export_evidence()
    else:print(json.dumps(cost_summary(),indent=2))
