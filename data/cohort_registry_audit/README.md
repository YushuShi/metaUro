# Cohort identity audit

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
