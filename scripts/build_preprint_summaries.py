#!/usr/bin/env python3
"""Explicit offline reviewed-preprint summaries in the sole summary output tree."""
import argparse
from build_cohort_summaries import build
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',choices=('MetaVir','metaPee'),required=True);p.add_argument('--mode',choices=('yes','no'),required=True);p.add_argument('--keep-latest-cohort',action='store_true');p.add_argument('--languages',nargs='+');a=p.parse_args()
    build(a.source,a.mode,a.languages,keep_latest_cohort=a.keep_latest_cohort,include_preprints=True)
