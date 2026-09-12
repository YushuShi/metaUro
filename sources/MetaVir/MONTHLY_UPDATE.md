# MetaVir monthly evidence update

The scheduled job screens PubMed records published during the previous calendar
month for nutritional exposures and prostate-cancer incidence. It is a
prostate-only, Python-only pipeline: it does not build workbooks, run R, create
cancer-subtype outputs, or maintain a separate Mendelian-randomization stream.

## Screening contract

One immutable title/abstract/MeSH candidate packet is currently screened by the
ChatGPT primary track:

- ChatGPT primary/canonical: `gpt-5.6-luna` first screen, `gpt-5.6-sol` detailed screen, and
  `gpt-5.6-luna` conditional full-text review.
- DeepSeek validation is pending until Cornell enables V4 Flash/Pro. No
  DeepSeek request is made in the current default mode.

The full-text stage runs when JBI is Fair or a key quantitative field is
missing, and only when usable article text can be retrieved through PMCID.
ChatGPT is the primary source for the base `Cached_results/` cache. After the
monthly apply stage, `scripts/build_combined_results.py` rebuilds the public
`combined/` projection, reapplies safe Fable adjudications and curator
overrides, and regenerates its analyses. Unresolved discrepancy fields retain
the ChatGPT value provisionally. DeepSeek never updates caches. Each run
explicitly records DeepSeek validation as pending and does not imply agreement
while validation results are unavailable.

All exclusions retain PMID plus a one-sentence reason. Included quantitative
fields retain article excerpts and their source locations. Adjusted and
unadjusted OR, RR, and HR labels are normalized to OR, RR, and HR. Saved study
rows also retain population and ethnicity; ethnicity is `NA` when the article
does not provide field-specific evidence for an auditable imputation.
For numeric fields, the validator locates the extracted value in the supplied
abstract and saves the most relevant verbatim sentence. It recognizes common
range and thousands-separator typography. When a total sample is not printed
directly, separately reported case/control or study-arm counts may support it
only when their audited sum exactly equals the extracted total.

For ratio effects (OR, RR, IRR, or HR), deterministic post-processing fills a
wholly missing 95% confidence interval from the effect and an invertible
two-sided p-value using a Wald calculation on the log scale. It also fills a
missing two-sided p-value from a coherent effect and 95% confidence interval.
Every computed field retains method provenance; reported values are never
overwritten.

## Run locally

```bash
python scripts/monthly_evidence_update.py \
  --previous-month \
  --stage all \
  --report-dir data/monthly_reports \
  --monthly-report-subdir
```

You can replace `--previous-month` with explicit `--start-date YYYY-MM-DD` and
`--end-date YYYY-MM-DD`. Stage-level resumability is available through
`--stage`; run `--help` for the supported values.

Recoverable Cornell gateway, malformed-response, and strict-validation problems
do not stop the main pass. The affected PMID batch or PMCID is written to
`screening_problem_ledger.json` with its PMID, exposure, and disease scope;
later work in the same stage continues, and deferred locations are revisited
after that stage's main pass. The next dependent stage begins only after every
required context has a validated result. If a deferred location still fails on
the revisit, rerunning the same command resumes only unresolved contexts.

For a one-time search of the complete PubMed history through an inclusive end
date, use `--all-history-through`. This is a true semantic no-lower-limit run:
the candidate packet stores `publication_date_start: null`. PubMed requires an
earliest-date transport sentinel for its upper date bound to take effect, and
every fetched publication date is checked again locally. The historical mode
uses a separate `all_history_through_YYYY-MM-DD` directory and deliberately
rejects `--force`; rerun the same stage command to resume its validated
checkpoints.

Use separate stage invocations so the inexpensive first screen and detailed
screen retain their intended batch sizes:

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

Then run `report`, `full-text`, `finalize`, `apply`, and `cost-report` in that
order with the same historical and report arguments. Do not add `--force`.
The equivalent resumable end-to-end runner is:

```bash
scripts/run_historical_backfill.sh 2026-06-30
```

It uses Luna batches of 20, Sol batches of 8, and validates the completed
historical artifact graph after cache application.

## Required configuration

- NCBI: `NCBI_API_KEY` and `NCBI_EMAIL`.
- LLM gateway: `CORNELL_API_KEY` and `CORNELL_API_BASE_URL`. ChatGPT uses this
  Cornell LiteLLM gateway; direct-provider keys are not used.
- Local `mykey.env` may use the existing aliases `Cornell_API_Key` and
  `API_Base_URI`; GitHub Actions uses the normalized names above.
- Failure alerts in GitHub Actions: the `SMTP_*` secrets declared in the
  workflow.

The three ChatGPT model slugs can be overridden with the corresponding
`CHATGPT_*_MODEL` environment variables.

## Durable outputs

Each bounded run writes `data/monthly_updates/START_END/`; a historical run
writes `data/monthly_updates/all_history_through_END/`. Both contain the shared
candidate packet, all four ChatGPT stage packets, PMCID source status,
the ChatGPT-primary/canonical final packet, exclusions, pending validation status,
subtype-safety review, and applied changes. Human-readable summaries,
exclusions, discrepancies, and a cost report go to the matching
`data/monthly_reports/START_END/` directory. The repository-root `cost.txt` is
also rewritten after each retained ChatGPT response. It lists every configured
exposure, including zero-cost/no-candidate exposures, and allocates actual
gateway-reported tokens by exposure, stage, and Luna/Sol model. Dollar amounts
are public-list-price estimates and may differ from Cornell billing.
`screening_problem_ledger.json` is created after the first recoverable screening
problem and retains both unresolved and successfully revisited locations.

The web application does not read `Cached_results/` directly. It reads
`combined/`, including `_merge_manifest.json`, `_adjudication_ledger.json`, and
`_pending_review.json`; curator decisions belong in
`combined/_manual_overrides.json` so later monthly rebuilds preserve them.

If a grouped Sol batch remains unresolved, resume the historical runner with
`SECOND_SCREEN_BATCH_SIZE=1`. This isolates each PMID so one invalid extraction
cannot block the other papers from that batch. Completed individual records
automatically close their older grouped problem entries.

Raw PMC text is transient and is removed after screening. API keys are never
written to an artifact. See
[docs/monthly-evidence-workflow.md](docs/monthly-evidence-workflow.md) for the
field-level contract.
