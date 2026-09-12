# Monthly evidence workflow

The monthly job discovers one immutable PubMed candidate set and currently runs
the ChatGPT primary/canonical track only.

| Track | Role | First title/abstract/MeSH screen | Detailed title/abstract/MeSH screen | Conditional PMC full text |
| --- | --- | --- | --- | --- |
| ChatGPT | Primary/canonical | `gpt-5.6-luna` | `gpt-5.6-sol` | `gpt-5.6-luna` |
| DeepSeek | Validation pending Cornell V4 access | — | — | — |

ChatGPT calls use the Responses API with a strict JSON schema through the
Cornell LiteLLM gateway, authenticated by `CORNELL_API_KEY` and
`CORNELL_API_BASE_URL`; direct-provider endpoints are not used. No DeepSeek
client is constructed until Cornell enables V4 Flash/Pro. Local
`mykey.env` files may retain the aliases `Cornell_API_Key` and `API_Base_URI`.
The three ChatGPT stages are production-pinned to the exact model slugs in the
table; environment variables cannot substitute older or adjacent models.
The public [LiteLLM model catalog](https://models.litellm.ai/) documents model
capabilities, but Cornell's authenticated `/models` response is authoritative
for which aliases the gateway credential may actually call.

After the Sol screen, deterministic two-sided Wald completion may recover a p
value from a reported effect and 95% CI, or a 95% CI from a reported effect and
two-sided p value, only when all inputs refer to the same estimate, model,
stratum, outcome, and comparison. It improves quantitative completeness, never
the JBI appraisal itself. Third-round Luna full text is requested exactly when
the context is included, has a PMCID, and at least one gate remains true: JBI is
**Fair**, a required quantitative field remains missing or invalid, or exposure
classification remains ambiguous. A missing PMCID, an NCBI error, malformed
XML, or a partial PMC record is persisted as an unavailable-source result and
does not abort the run.
A numeric extraction is paired with the most relevant verbatim source window
containing that value. Hyphenated CI ranges, spaced thousands, and common
decimal typography are normalized for matching. A total sample may also be
validated from separately reported case/control, matched-pair, or study-arm
counts when its exact derivation is retained in the evidence location.
Complete Moderate and Good studies can be pooled; Fair studies can only become
poolable after full-text appraisal upgrades them to Moderate or Good.

For a recoverable Cornell gateway error, malformed model response, or strict
validation failure, the current PMID batch or PMCID is durably deferred instead
of aborting the stage. Screening/extraction continues through later work, then
revisits every deferred location at the end of that stage.
`screening_problem_ledger.json` retains both pending and resolved events,
including stage, model, location, explicit PMID, exposure, disease scope,
timestamps, problem type, and the sanitized error. A dependent stage cannot
start while a required deferred result remains unresolved.

For recovery from a grouped Sol failure, the historical runner accepts
`SECOND_SCREEN_BATCH_SIZE=1`. The same digest-bound checkpoint is reused, each
remaining PMID is attempted independently, and older grouped ledger entries are
resolved as all of their individual contexts complete.

Mendelian-randomization and genotype-interaction reports are exclusions with
the explicit codes `S2-MENDELIAN-RANDOMIZATION` and
`S2-GENOTYPE-INTERACTION`. Every exclusion, including a full-text exclusion,
retains its PMID and one-sentence reason in `exclusions.json`.

Every saved quantitative field carries its own original-article excerpt,
source, URL, and location. Saved rows also retain PMCID, population, location,
and ethnicity. For MetaVir, Sol records location and ethnicity only when the
article states them in field-specific abstract evidence; either field is saved
as `NA` when Sol cannot find it. Country and population labels are never mapped
to an ethnicity, and third-round Luna cannot infer or overwrite either Sol
field.

The final artifacts are:

- `chatgpt_{first_screen,second_screen,full_text_review,final_screening}.json`
- `validation_status.json` — DeepSeek validation state and required future V4 models
- `final_screening.json` — the ChatGPT-primary/canonical packet used for cache updates
- `exclusions.json` — all canonical exclusions
- `discrepancies.json` and `modelDiscrepancies.txt` — explicit pending-validation audit; these contain comparisons only after validation becomes available
- `exposure_subtype_review.json` — related Tomato evidence that is retained but
  not pooled automatically across whole-food, dietary-component, biomarker, and
  supplement strata
- `cost.txt` — all configured exposures with actual response-token allocations
  by screening stage and Luna/Sol model. Cached input is priced separately;
  shared-call tokens are allocated by exposure context count without changing
  the exact call totals. Dollar amounts use supplied public list prices and may
  differ from Cornell gateway billing. The repository-root copy is atomically
  refreshed after each retained response so an interrupted run remains auditable.
- `screening_problem_ledger.json` — recoverable-problem PMID batches or PMCIDs
  with explicit PMID, exposure, and disease scope plus their deferred/resolved
  audit history; present only after at least one such problem

Run the complete previous-month workflow with:

```bash
python scripts/monthly_evidence_update.py \
  --previous-month --stage all \
  --report-dir data/monthly_reports --monthly-report-subdir
```

For the complete historical evidence base through June 2026, use the resumable
historical mode. It records a null lower publication-date bound, enforces the
inclusive upper bound both in PubMed and after metadata retrieval, and writes
to `data/monthly_updates/all_history_through_2026-06-30/`:

```bash
python scripts/monthly_evidence_update.py \
  --all-history-through 2026-06-30 --stage discover \
  --report-dir data/monthly_reports --monthly-report-subdir
python scripts/monthly_evidence_update.py \
  --all-history-through 2026-06-30 --stage first-screen --batch-size 20 \
  --report-dir data/monthly_reports --monthly-report-subdir
python scripts/monthly_evidence_update.py \
  --all-history-through 2026-06-30 --stage second-screen --batch-size 8 \
  --report-dir data/monthly_reports --monthly-report-subdir
```

Continue with `report`, `full-text`, `finalize`, `apply`, and `cost-report`.
Historical mode rejects `--force`; repeating a stage resumes only from
digest-bound checkpoints.

The 2026-08-29 exposure revision uses two separate fail-safe runs. Expanded
aliases, new current exposures, and the complete additional catalog require an
all-history search; `--revision-aware` is rejected without
`--all-history-through`:

```bash
python scripts/monthly_evidence_update.py \
  --revision-aware --all-history-through 2026-08-29 --stage all \
  --report-dir data/revision_reports --monthly-report-subdir
```

Apply is authoritative for every directly searched exposure/cancer context:
old direct rows are replaced by the exact newly screened-in set, including an
empty set. Every displaced row retains a status and reason in both the cache's
revision audit and `revision_removed_records.json`; all/core writes are planned
before mutation and rolled back together on failure. A parent not searched by
the revision is different: its unchanged direct evidence is preserved and only
inherited rows from revised children are replaced. K1/K2/K3 refreshes therefore
add their screened child evidence to general Vitamin K without erasing general
Vitamin K's direct evidence.

Nicotinic acid and nicotinamide evidence inherit into Vitamin B3. Nicotinamide
riboside is only metabolically related and never rolls up into Vitamin B3 (or
NAD+/NADH/NMN/NMNH); the non-inheriting relationship and hierarchy checksum are
recorded in `data/exposure_revision_manifest.json`.

Narrowed Fish oil, Omega-3, and Tomato classifications use an included-only
packet. This mode performs zero PubMed ESearch calls, EFetches only PMIDs already
in the union of all/core source caches, records explicit source/approved-target
roles, and screens them through the same exact pipeline. Fish oil requires
literal fish-oil wording and Tomato requires tomato wording. EPA/DHA-only
evidence can be accepted in its atomic exposure and then reaches Omega-3 by
post-screen hierarchy rollup; it is not labeled direct Omega-3 without omega-3
or n-3 family wording. Apply validates the current candidate→Luna→Sol→conditional
Luna→final lineage and source/target scope before any mutation, writes accepted
targets first, and removes a direct source effect only after the same PMID/effect
is verified in a target cache. Every retained or moved record receives an explicit reason in
`reclassification_applied_changes.json`:

```bash
python scripts/monthly_evidence_update.py \
  --revision-reclassification --stage all \
  --report-dir data/revision_reports --monthly-report-subdir
```

`scripts/run_historical_backfill.sh 2026-06-30` performs that same staged,
resumable sequence and runs the historical artifact validator at the end.

Only `final_screening.json` can mutate the ChatGPT base in `Cached_results`; the
apply stage rejects any packet that is not explicitly marked
`canonical_track: chatgpt`. The scheduled job then rebuilds the public
`combined/` projection and regenerates summaries from that projection.
