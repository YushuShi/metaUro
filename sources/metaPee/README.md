# MetaPee

MetaPee is a urinary-system-cancer nutrition evidence platform. It searches
and screens epidemiologic evidence for nutritional exposures, performs
random-effects meta-analysis on eligible studies, and serves the saved results
through a multilingual Flask application.

This repository starts with a clean urinary-system-cancer evidence store;
source-project results are intentionally not included.

## Urinary-system-cancer behavior

- Supported bladder, kidney, and renal-pelvis disease names, aliases, and
  sex-specific and combined lifetime incidence values are defined in
  `bladder_kidney_cancer_lifetime_incidence_aliases.csv`.
- Adjusted and unadjusted OR, RR, and HR labels are normalized to their base
  measure. The original reported measure is retained, and OR/HR estimates are
  converted to RR with the selected disease's **gender-combined lifetime
  incidence** for now.
- Sample-size fallbacks that estimate case counts use the same disease-specific
  combined baseline.
- The interface uses MetaPee's orange theme.
- The public app is cache-only. New evidence reaches the web platform only
  through the audited monthly workflow.

## Evidence screening

Each monthly PubMed candidate packet is currently screened by the ChatGPT
primary track:

| Track | Role | First screen | Detailed screen | Conditional PMC full text |
| --- | --- | --- | --- | --- |
| ChatGPT | Primary/canonical | `gpt-5.6-luna` | `gpt-5.6-sol` | `gpt-5.6-luna` |
| DeepSeek | Validation pending Cornell V4 access | — | — | — |

The full-text step runs with Luna for Fair JBI ratings or missing key evidence
when a usable PMCID full text is available. Excluded records retain their PMID
and a one-sentence reason. Included quantitative fields retain original-article
supporting excerpts and sources. Numeric evidence may be written in words, and
sample totals may be supported by separately reported component counts; dash
characters are never treated as minus signs without evidence. When an excerpt
cannot be matched reliably, the extracted key value remains the reference and
the excerpt is temporarily `NA`. Saved studies also include gender, smoking
status, and ethnicity fields, using `NA` when the article does not provide the
information. Separate rows are created for gender-specific results and for
dietary versus biospecimen exposure measurements. ChatGPT is canonical for
saved web results. DeepSeek never writes evidence to the web cache. Until
Cornell enables DeepSeek V4 Flash/Pro, no DeepSeek client is constructed and
each run records validation as pending rather than claiming agreement or
producing a fabricated discrepancy comparison.

When a ratio effect and a two-sided p-value are reported without a confidence
interval, the pipeline imputes a 95% confidence interval with Wald's method. If
the ratio effect and 95% confidence interval are reported without a p-value, it
imputes a two-sided p-value with the same log-scale Wald method. Reported values
are never overwritten, and every imputation retains method provenance.

The repository-root `cost.txt` tracks actual gateway-reported token usage and
estimated Luna/Sol list-price cost for every exposure. It updates during a run;
Cornell's billed institutional price may differ from the displayed estimate.

In addition to the bounded monthly job, the pipeline supports a resumable
historical backfill with `--all-history-through YYYY-MM-DD`. Historical packets
record no lower publication-date limit and are kept in a separate
`all_history_through_YYYY-MM-DD` directory, so they cannot overwrite monthly
artifacts.

The exposure catalog includes Sesame, Fo-ti, Oleic acid, Omega-9 fatty acids,
and Avocado. Their curated synonym groups, together with all other exposure
aliases, are stored in `data/synonyms_cache.json`.

## Cross-exposure summary

The header's **Summary** link opens `/summary`, between the language selector
and About. It presents protective and harmful forest plots across nutritional
exposure categories, dietary-intake-only versions, and heterogeneity
diagnostics. Downloadable summary workbooks and the PDF figures are stored in
`Plot/` and can be rebuilt from the saved cache with:

```bash
python scripts/build_summary_plots.py
```

The scheduled monthly workflow runs this command after applying new evidence,
so the summary page and workbooks stay synchronized with `Cached_results/`.

See [MONTHLY_UPDATE.md](MONTHLY_UPDATE.md) and
[docs/monthly-evidence-workflow.md](docs/monthly-evidence-workflow.md) for the
pipeline contract and generated artifacts.

## Local setup

Requires Python 3.12 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5007`.

Each meta-analysis repository uses a distinct default local port so multiple apps
can run together. Use the address printed at startup. Setting `PORT` overrides
this default; choose an unused port.

The app reads the saved combined UI view in `combined/provisional_ui/`, including
both subexposure modes and their summary figures. `Cached_results/` and
`ClaudeGemini_results/` retain the individual pipelines, while `combined/` retains
the adjudicated evidence. Pending human reviews are listed in
`combined/human_review/`. The provisional view uses ChatGPT when both pipelines
have complete quantitative values, the eligible complete pipeline when only one
does, and the higher saved JBI assessment while human review is pending.

## Configuration

For local PubMed discovery and monthly screening, keep the following entries in
the ignored `mykey.env` file. `python-dotenv` reads this file directly; do not
shell-source it.

```text
NCBI_API_KEY=
NCBI_EMAIL=
Cornell_API_Key =
API_Base_URI =
```

The ChatGPT track uses the Cornell LiteLLM gateway credential and base URI.
GitHub Actions uses the normalized secret names `NCBI_API_KEY`, `NCBI_EMAIL`,
`CORNELL_API_KEY`, and `CORNELL_API_BASE_URL`.

Crowdsourced review reports require a stable `REPORTER_ID_SECRET` and durable
`VERIFICATIONS_FILE`. SMTP settings are optional locally and required if review
thresholds or workflow failures should email maintainers:

```text
REPORTER_ID_SECRET=
VERIFICATIONS_FILE=/path/to/verifications.json
SMTP_HOST=
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
```

`render.yaml` declares a persistent Render disk for review state. Before
scaling to multiple web instances, move that state to a shared transactional
store.

## Tests

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## Repository layout

```text
app.py                         Flask web application
meta_analysis.py               statistical pooling and diagnostic plots
effect_measure.py              OR/HR-to-RR conversion
scripts/dual_screening.py      independent model-track implementation
scripts/monthly_evidence_update.py
                                monthly discovery, screening, audit, and apply
scripts/build_summary_plots.py  cross-exposure workbooks and summary figures
Cached_results/                generated urinary-system-cancer evidence caches
Plot/                          summary workbooks, PDFs, and UI manifest
data/                          curated synonyms and review-state seed
static/ and templates/         multilingual web interface
tests/                         unit and regression tests
```
