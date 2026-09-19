# MetaVir

MetaVir is a prostate-cancer nutrition evidence platform. It searches and
screens epidemiologic evidence for nutritional exposures, performs
random-effects meta-analysis on eligible studies, and serves the saved results
through a multilingual Flask application.

This repository starts with a clean prostate evidence store. Saved cancer
results from the source project are intentionally not included.

## Prostate-specific behavior

- Disease scope includes **Prostate cancer** and its indented subcategory,
  **Aggressive prostate cancer**.
- Adjusted and unadjusted OR, RR, and HR labels are normalized to their base
  measure; OR and HR estimates are converted to RR using a **13.2% lifetime
  incidence**.
- Sample-size fallbacks that estimate case counts use the same 13.2% baseline.
- The interface uses MetaVir's blue theme.
- The public app is cache-only. New evidence reaches the web platform only
  through the audited monthly workflow.

Aggressive prostate cancer evidence is classified by `gpt-5.6-sol` from the
included articles' abstracts, with original-article excerpts retained for
estimate mapping. An article mentioning aggressive disease does not make its
overall estimate an aggressive estimate. Aggressive-only evidence also appears
in the general scope; when an article supplies separate overall and aggressive
estimates, each stays in its own scope. Paired prospective-cohort estimates use
the same total participant denominator, with endpoint-specific case counts.
Overlapping estimates are retained for inspection and selected once for pooling.

SEER does not publish a matching age/interval probability for this broad
aggressive definition in the reviewed public tables. Users must provide the
aggressive incidence probability in Settings for baseline-dependent conversions
and prospective planning. Missing aggressive incidence never inherits the
general prostate baseline. Saved summaries without a baseline use the app's
explicit HR ≈ RR ≈ OR identity approximation. See
[the incidence source review](docs/aggressive-prostate-incidence.md).

To refresh the abstract review and rebuild both evidence tracks and their
summary/subexposure views:

```bash
python scripts/aggressive_prostate_review.py
python scripts/review_aggressive_case_counts.py
python scripts/build_aggressive_prostate_results.py
```

The review resumes unchanged packets, stores model provenance and missing-abstract
uncertainty under `data/aggressive_prostate_review/`, and requires no new model
calls during projection. Run it after rebuilding the base/combined evidence.

## Evidence screening

Each monthly PubMed candidate packet is screened by the ChatGPT primary/base
track:

| Track | Role | First screen | Detailed screen | Conditional PMC full text |
| --- | --- | --- | --- | --- |
| ChatGPT | Primary/base | `gpt-5.6-luna` | `gpt-5.6-sol` | `gpt-5.6-luna` |
| DeepSeek | Validation pending Cornell V4 access | — | — | — |

The full-text step runs for Fair JBI ratings or missing key evidence when a
usable PMCID full text is available. Excluded records retain their PMID and a
one-sentence reason. Included quantitative fields retain original-article
supporting excerpts and sources, and saved studies include an ethnicity field.
ChatGPT writes the audited base caches in `Cached_results/`. The default public
UI at `/` serves `combined/`, a reproducible projection that applies evidence-backed
title-, abstract-, and MeSH-only `claude-fable-5-1` adjudications to discrepancies
between ChatGPT and the Claude+Gemini validation results. Fields that Fable
marked unsure, insufficient, or in need of manual review retain the ChatGPT
value provisionally and remain listed in `combined/_pending_review.json`.
The separate `/chatgpt` UI reads only `Cached_results/`; its API requests carry
an explicit track marker and cannot load combined caches. DeepSeek never writes
evidence to either cache.

Both incidence tracks require total sample size, an eligible ratio effect, and
a usable confidence interval or an exact same-estimate p-value from which a
Wald interval can be calculated. Reported cases, exposure units/scale,
adjustment model/stratum, and direct-measurement metadata are optional.
Missing cases use the existing labeled lifetime-risk estimate; missing
directions default to high versus low or yes versus no and show a
**Direction imputed** badge. Known reversed comparisons retain their inversion.
Moderate/Good JBI and disease/design eligibility rules still apply.

Incidence means time to first prostate-cancer diagnosis. For every HR (and any
other follow-up estimate), the modeled failure event must be identified.
Mortality, fatal/lethal disease determined by later death or metastasis,
postdiagnosis progression, recurrence, and survival are excluded even when the
cohort was cancer-free at entry. Separately reported total, advanced-stage-at-
diagnosis, and high-grade diagnosis estimates remain eligible. Reviewed legacy
exceptions are recorded in `data/incidence_endpoint_review/` and replayed by
the disease-scope projection without changing historical model responses.

To apply this policy to saved evidence and regenerate both tracks' exposure
plots plus all localized combined summary/heterogeneity plots, without new
model calls:

```bash
python scripts/rebuild_poolability_results.py
```

The original screening packets remain unchanged. Reclassified ChatGPT and
Claude+Gemini projections and per-study audits are saved under
`outputs/poolability_policy_2026-09-05/`. ChatGPT plots use
`static/chatgpt/`; combined plots use `static/<exposure>/`. Endpoint-policy
rebuilds use the isolated `incidence_endpoint_v1` namespaces so an audited
release never overwrites prior figure bytes.

## Prostate cancer survival module

The survival workflow is additive and isolated from the incidence workflow: it
does not overwrite the existing `Cached_results/` incidence caches or their
plots. Run its complete pipeline with, for example:

```bash
python scripts/prostate_survival.py all \
  --start-date 2000-01-01 --end-date 2026-08-31
```

The available stages are `discover`, `first-screen`, `second-screen`,
`full-text`, `finalize`, `analyze`, and `all`. By default, artifacts are written
under `data/survival_updates/<window>/`. Discovery searches the collision-checked
union of every canonical exposure and alias in `data/synonyms_cache.json` and
`data/additionalExposure.json` while reusing the existing PubMed query and
metadata helpers.

Screening uses lenient `gpt-5.6-luna`, detailed `gpt-5.6-sol`, and a final
`gpt-5.6-luna` PMC extraction only when an otherwise eligible record has a PMCID
and key quantitative information is missing. Eligible participants must already
have prostate cancer. Incidence-only populations, reviews and published
meta-analyses, and other non-primary or incompatible reports are excluded.
Every exclusion is retained in `exclusions.json` with its PMID and a one-sentence
reason.

Each study/effect row retains PMID, optional PMCID, publication year, cohort,
country, ethnicity, sample size, number of events, the authors' original stage
description, mapped stage, disease setting, study population, exact primary
treatment (or `unclear`), exposure and matched alias, assessment method, outcome
and its definition, follow-up, effect measure and estimate, confidence limits,
reference group, exact exposure contrast and comparison type, CI confidence
level, and adjustment variables. `exposure_timing` is one of `prediagnosis`,
`postdiagnosis`, `both`, or `unclear`. Mapped stage is one of `localized`,
`locally_advanced`, `nonmetastatic`, `metastatic`, `mixed`, or `unclear`;
`disease_setting` is one of `active_surveillance`, `after_prostatectomy`,
`after_radiotherapy`, `biochemical_recurrence`,
`metastatic_hormone_sensitive`, `castration_resistant`, `mixed`, or `unclear`.
Outcomes are stored as separate overall/all-cause, prostate-cancer-specific,
progression-free, metastasis-free, and biochemical-recurrence-free survival
categories, with the reported definition preserved.

Only adjusted hazard ratios are pooled. The module analyzes `log(HR)` with
standard error `(log(upper_ci) - log(lower_ci)) / 3.92`, grouping first by
exposure, exposure timing, and outcome. Within those groups, incompatible
comparison types, continuous increments or serving scales, and progression
definitions remain separate, and only reported 95% CIs use that formula. Thus
prediagnosis, postdiagnosis, `both`, and `unclear` estimates remain separate;
overall/all-cause and prostate-cancer-specific mortality remain separate; and
ORs or RRs are never combined with HRs. Stage- or disease-setting strata are
added only when at least two compatible studies are available. Within an
analysis, overlapping reports from the same cohort are counted once after
timing, outcome, exposure contrast, stage, disease setting, and clearly
disjoint populations are checked for compatibility. The more completely
adjusted and informative overlapping estimate is retained, and every dropped
report remains auditable by PMID and reason.

The repository-root `cost.txt` tracks actual gateway-reported token usage and
estimated Luna/Sol list-price cost for every exposure. It updates during a run;
Cornell's billed institutional price may differ from the displayed estimate.

In addition to the bounded monthly job, the pipeline supports a resumable
historical backfill with `--all-history-through YYYY-MM-DD`. Historical packets
record no lower publication-date limit and are kept in a separate
`all_history_through_YYYY-MM-DD` directory, so they cannot overwrite monthly
artifacts.

The exposure catalog includes Tomato, Pepper (Capsicum), and Ketogenic diet.
Their curated synonym groups are stored in `data/synonyms_cache.json`.

## Cross-exposure summary

The header's **Summary** link opens `/summary`, between the language selector
and About. It presents protective and harmful forest plots across nutritional
exposure categories, dietary-intake-only versions, and heterogeneity
diagnostics. Downloadable MetaFemina-style workbooks and the PDF figures are
stored in `Plot/` and can be rebuilt from the saved cache with:

```bash
python scripts/build_summary_plots.py
```

The scheduled monthly workflow rebuilds `combined/` and then runs this command,
so the summary page and workbooks stay synchronized with the public combined
projection.

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

Open `http://127.0.0.1:5002` for the combined UI or
`http://127.0.0.1:5002/chatgpt` for the ChatGPT-only UI.

Each meta-analysis repository uses a distinct default local port so multiple apps
can run together. Use the address printed at startup. Setting `PORT` overrides
this default; choose an unused port.

The app reads saved analyses from `combined/`. The monthly pipeline writes its
ChatGPT base to `Cached_results/`, then `scripts/build_combined_results.py`
rebuilds the public projection and preserves curator choices from
`combined/_manual_overrides.json`.

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
scripts/prostate_survival.py     isolated diagnosed-patient survival workflow
scripts/build_combined_results.py
                                adjudicated public-cache projection
scripts/build_summary_plots.py  cross-exposure workbooks and summary figures
Cached_results/                ChatGPT base evidence caches
combined/                      adjudicated caches used by the web application
Plot/                          summary workbooks, PDFs, and UI manifest
data/                          curated synonyms and review-state seed
static/ and templates/         multilingual web interface
tests/                         unit and regression tests
```
