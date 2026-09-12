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
The default model slugs can be overridden with the three `CHATGPT_*_MODEL`
variables declared in the workflow.
The public [LiteLLM model catalog](https://models.litellm.ai/) documents model
capabilities, but Cornell's authenticated `/models` response is authoritative
for which aliases the gateway credential may actually call.

Full text is requested exactly when the detailed-screen JBI grade is **Fair** or
one of effect value/type, either CI bound, total sample size, or case count is
missing. It is sent to the full-text model only when usable text can be fetched
through a PMCID. A missing PMCID, an NCBI error, malformed XML, or a partial PMC
record is persisted as an unavailable-source result and does not abort the run.
A numeric extraction is paired with the most relevant verbatim source window
containing that value. Hyphenated CI ranges, spaced thousands, and common
decimal typography are normalized for matching. A total sample may also be
validated from separately reported case/control, matched-pair, or study-arm
counts when its exact derivation is retained in the evidence location.
Complete Moderate and Good studies can be pooled; Fair studies can only become
poolable after full-text appraisal upgrades them to Moderate or Good.

The detailed screen records gender and smoking status when the supplied article
evidence supports them; otherwise each field is `NA`. Gender-specific results
are separate records. Dietary-intake and human-biospecimen estimates are also
separate records, including when both occur in one article.

The original reported effect label, value, and confidence limits are retained
without conversion. For analysis, eligible ratio measures receive explicit RR
conversion fields and conversion metadata. OR/HR-to-RR conversion currently
uses the selected disease's gender-combined lifetime incidence from
`bladder_kidney_cancer_lifetime_incidence_aliases.csv`. Reported estimates are
never overwritten. If an effect and invertible two-sided p-value are available
without a confidence interval, a 95% confidence interval is imputed with the
log-scale Wald method. If an effect and coherent 95% confidence interval are
available without a p-value, a two-sided p-value is imputed with the same
method; all imputed fields retain provenance.

Numeric facts may appear as words rather than digits. A sample total may be
inferred from separately stated component counts only when the exact arithmetic
is auditable. Range dashes and minus signs are treated distinctly. When a
supporting excerpt cannot be matched reliably, the extracted key value remains
the reference and the excerpt is temporarily `NA`.

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
source, URL, and location. Saved rows also retain PMCID, population, country,
gender, smoking status, and ethnicity.

The final artifacts are:

- `chatgpt_{first_screen,second_screen,full_text_review,final_screening}.json`
- `validation_status.json` — DeepSeek validation state and required future V4 models
- `final_screening.json` — the ChatGPT-primary/canonical packet used for cache updates
- `exclusions.json` — all canonical exclusions
- `discrepancies.json` and `modelDiscrepancies.txt` — explicit pending-validation audit; these contain comparisons only after validation becomes available
- `exposure_subtype_review.json` — related Tomato evidence that is retained but
  not pooled automatically across whole-food, dietary-component, biomarker, and
  supplement strata
- `cost.txt` — one row per configured exposure, with actual Luna and Sol input,
  cached-input, and output tokens and separate model costs. Shared-call tokens
  are allocated by exposure context count without changing the exact call
  totals. Dollar amounts use supplied public list prices and may differ from
  Cornell gateway billing. The repository-root copy is atomically refreshed
  after each retained response so an interrupted run remains auditable.
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

`scripts/run_historical_backfill.sh 2026-06-30` performs that same staged,
resumable sequence and runs the historical artifact validator at the end.

Only `final_screening.json` can mutate `Cached_results`; the apply stage rejects
any packet that is not explicitly marked `canonical_track: chatgpt`.
