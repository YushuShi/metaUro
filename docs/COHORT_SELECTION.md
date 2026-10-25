# Optional cohort selection

Include-all remains the default for displays, pooling and sample-size planning.
Settings and Summary provide an optional latest-publication selection within
each poolable comparison. NHS, NHS II and NHS III are separate cohorts. Unknown
identities/dates and tied or overlapping date ranges remain included conservatively.
Inseparable multi-cohort estimates stay whole; older reports overlapping an accepted
newer cohort are omitted from the optional analysis. No recorded evidence is
deleted; older records remain available in study details.

`data/cohort_registry.json` adds cohort identities and original publication-date
precision without changing the immutable `sources/` snapshots. Runtime adapters
select only the final working pooling frame after existing eligibility checks.
Recorded names are reused; missing names are screened from titles and abstracts
using GPT-6 Luna with xhigh reasoning. Audits retain evidence and model charges.

`data/cohort_record_metadata.json` permanently stores study/cohort names for
**each saved analysis record**, including records outside pooling. Stable record
IDs and source-row references link the metadata to the original evidence. Each
entry includes names, evidence, provenance, publication date and its precision,
and an explicit unknown/not-screened status where a name is unavailable. This
metadata is saved independently of whether the UI displays it. Original cache
bytes remain unchanged.

Generate the separate summary variants for each source and exposure mode:

```sh
python scripts/build_cohort_summaries.py --source metaPee --mode yes
python scripts/build_cohort_summaries.py --source metaPee --mode no
python scripts/build_cohort_summaries.py --source MetaVir --mode yes
python scripts/build_cohort_summaries.py --source MetaVir --mode no
```

The only published output paths are `summary/{yes,no}/cohort_latest/`. They contain
cross-exposure forest and heterogeneity PDFs, workbooks and source manifests.
Selection audits identify earlier and retained PMIDs; the output fingerprint
includes the cohort registry. Summary requests with `latest_cohort=yes` verify
and refresh this separate namespace before serving a plot or workbook.

Refresh cohort metadata before rebuilding filtered summaries following monthly
evidence updates. New records without a saved identity remain included under
the existing rules. Code and generated results must be published together to
`V2`, preserving the existing release and immutable-snapshot policies.
