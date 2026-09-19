# Sol abstract review summary

Final evidence snapshot: 2026-09-07 21:57:21 UTC. All **581 included PMIDs** have a validated Sol review; **564 have a published abstract available**, and 17 records have no abstract in the retrieved PubMed XML. The corpus contains **1,294 distinct saved estimate contexts**, with identical all/core and inherited-exposure copies collapsed. Sources: [manifest.json](manifest.json), [articles.json](articles.json), and [review.json](review.json).

| Article classification | Articles |
| --- | ---: |
| General prostate cancer only | 363 |
| Both general and aggressive incidence | 164 |
| Aggressive incidence only | 26 |
| Uncertain | 28 |

Thus 190 articles have an explicitly supported aggressive analysis in their abstracts. Article classification is separate from eligibility of any individual estimate.

| Saved estimate endpoint | Unique contexts |
| --- | ---: |
| General | 1,125 |
| Aggressive | 110 |
| Nonaggressive | 14 |
| Survival | 1 |
| Uncertain | 44 |

The review also retained **174 additional endpoint entries**: 59 numerical aggressive estimates, 7 numerical general estimates, and 108 qualitative aggressive entries with null effect/CI values. Five proposed additions failed validation and were retained separately for audit. Qualitative entries are display-only; these totals do not assert that every numerical entry passes the application's pooling, comparison, quality, or duplicate-population rules.

## Endpoint definitions and safeguards

This section describes the frozen version-1 model review. Its included
definitions followed each paper's wording and included lethal/fatal disease.
The authoritative diagnosis-only checkpoint recorded on 2026-09-19 now
excludes reviewed fatal/lethal, prostate-cancer-death, and postdiagnosis
progression estimates while retaining distinct total, advanced-at-diagnosis,
and high-grade diagnosis estimates. See
`../incidence_endpoint_review/current_exclusions.json`.

A background mention or unspecified stage/grade stratification is insufficient.
For current incidence projection, mortality and a lethal/fatal endpoint defined
by a later event are also insufficient regardless of cancer-free cohort entry.
Every HR must model time to first diagnosis. Focused adjudication, literal
numeric-extraction safeguards, and the original model provenance remain
preserved in this historical review snapshot.

The model returned `gpt-5.6-sol` in all **214 retained responses**. Provider-reported usage totals **1,309,966 input tokens** (including 229,886 cached input tokens) and **577,265 output tokens** (including 235,803 reasoning tokens). There are no pending PMIDs or recorded terminal failures. See [responses](responses/) for original outputs and receipts.

## Focused direct-exposure coverage audit

A subsequent Sol audit examined 14 articles with an aggressive article classification but no subtype row, covering 26 included direct exposures. It added one supported qualitative entry: **Genistein, PMID 19536097**. Twenty-two direct-exposure contexts did not establish an aggressive association in the abstract; three remained uncertain (Citrulline, PMID 28676103; Pyruvate and Glycine, PMID 32698845). These gaps often reflect an aggressive result for a different exposure: saturated fatty acids rather than individual foods in PMID 2403069, a prevention-compliance score rather than alcohol in PMID 32183345, or periodontitis rather than alcohol in PMID 40254848. The last paper's wording “Gleason grades 3–5” also leaves the grading system unresolved, so its article classification is uncertain.

These findings explain why not every article about aggressive disease contributes aggressive evidence for its currently included exposure. See [coverage_review.json](coverage_review.json) for literal excerpts and per-exposure reasons, [coverage_reviews](coverage_reviews/) for individual decisions, and [before_coverage_review](before_coverage_review/) for preserved prior reviews. Coverage request digests include their exact prompt and schema. The final JSON snapshot includes this completed coverage revision.

## Disease inheritance and incidence policy

Aggressive evidence also belongs under general prostate cancer when it is the article's available endpoint. When the article supplies separate general and aggressive evidence for the same exposure, the general scope uses its separate general estimate and the aggressive scope uses its aggressive estimate, preventing duplicate subtype inheritance into the general analysis. Paired endpoints in a full prospective cohort share the same analytic participant N; case counts may differ. Nested case-control and case-cohort denominators are not automatically replaced by the full source-cohort population. Application projection is defined in [aggressive_prostate_policy.py](../../scripts/aggressive_prostate_policy.py).

Use an exact, directly published SEER age-specific probability if one matches the aggressive endpoint and requested population/follow-up. The current source review did not identify such a published table, so users must provide the aggressive cumulative incidence risk (%) for their selected starting age, follow-up and population. General prostate incidence, stage percentages, and prostate mortality are not substituted. Blank values mean unavailable, not zero. See [the SEER availability review](../../docs/aggressive-prostate-incidence.md) and [seer-baseline.json](../../static/seer-baseline.json).

## PubMed records without abstracts

These 17 records remain uncertain for abstract-based subtype assignment; an absence of an abstract is not evidence of an aggressive endpoint. Official retrieved records are preserved in [pubmed_abstracts.xml](pubmed_abstracts.xml) and [pubmed_abstracts.json](pubmed_abstracts.json).

- [PMID 1627305](https://pubmed.ncbi.nlm.nih.gov/1627305/): Beta-carotene, vitamin A and prostatic cancer.
- [PMID 9116086](https://pubmed.ncbi.nlm.nih.gov/9116086/): Dietary beta-carotene, vitamin C, and risks of prostate cancer.
- [PMID 11829049](https://pubmed.ncbi.nlm.nih.gov/11829049/): Calcium and prostate cancer.
- [PMID 12145029](https://pubmed.ncbi.nlm.nih.gov/12145029/): Dairy products, calcium, and prostate cancer risk in the Physicians' Health Study.
- [PMID 12497123](https://pubmed.ncbi.nlm.nih.gov/12497123/): Serum and tissue lycopene and biomarkers of oxidation in prostate cancer patients: a case control study.
- [PMID 14565221](https://pubmed.ncbi.nlm.nih.gov/14565221/): Calcium intake and prostate cancer risk in a long-term aging study: The Baltimore Logitudinal Study of Aging.
- [PMID 14652294](https://pubmed.ncbi.nlm.nih.gov/14652294/): Null association between prostate cancer and serum folate, vitamin B(6), vitamin B(12), and homocysteine.
- [PMID 15592035](https://pubmed.ncbi.nlm.nih.gov/15592035/): Vitamin E supplements and risk of prostate cancer in U.S. men.
- [PMID 15598793](https://pubmed.ncbi.nlm.nih.gov/15598793/): Cohort study of tofu intake and prostate cancer: no apparent association.
- [PMID 15669769](https://pubmed.ncbi.nlm.nih.gov/15669769/): alpha-linolenic acid and the risk of prostate cancer.
- [PMID 15758731](https://pubmed.ncbi.nlm.nih.gov/15758731/): Alcohol consumption and risk of prostate cancer in middle-aged men.
- [PMID 17932370](https://pubmed.ncbi.nlm.nih.gov/17932370/): Soy isoflavone consumption is not associated with increased risk of advanced prostate cancer.
- [PMID 18086789](https://pubmed.ncbi.nlm.nih.gov/18086789/): Lack of association between serum levels of 25-hydroxyvitamin D and the subsequent risk of prostate cancer in Finnish men.
- [PMID 19208211](https://pubmed.ncbi.nlm.nih.gov/19208211/): Association of self-reported consumption of cooked meat, fish, seafood and eggs with prostate cancer risk among Nigerians.
- [PMID 19211812](https://pubmed.ncbi.nlm.nih.gov/19211812/): Plasma selenium concentration and prostate cancer risk.
- [PMID 19570483](https://pubmed.ncbi.nlm.nih.gov/19570483/): Early results of the selenium and vitamin E prostate cancer prevention study.
- [PMID 30250306](https://pubmed.ncbi.nlm.nih.gov/30250306/): Early-life alcohol intake and high-grade prostate cancer.

## Reproducibility note

The primary resume digest includes the model/version, article title, full abstract and deduplicated estimate contexts. It does **not independently hash the system prompt**; a future primary-prompt change must also advance `VERSION` to force a new review. The full primary-run instructions and tightened instructions are saved in [initial_system_prompt.txt](initial_system_prompt.txt) and [current_system_prompt.txt](current_system_prompt.txt). Focused adjudications additionally use a revision in their response-cache key and preserve that revision, exact system prompt and input digest in their response receipts. [README.md](README.md) documents the file contract and resume commands. The final validation version is `literal-endpoint-numeric-citations-v2`; review and manifest JSON files are the frozen inputs for projection.

## Endpoint case counts

A separate focused Sol check reviewed the case counts for all 110 saved
aggressive-estimate contexts. It confirmed 69 exact endpoint counts and left
41 unavailable when the supplied source did not establish a literal matching
count. Eleven confirmed values differ from the saved overall case total.
For example, the advanced endpoints for PMIDs 8105097, 15213050 and 23549401
use 126, 448 and 2,930 cases, respectively; their overall totals are not used as
aggressive counts. Cohort participant totals are preserved separately.

The evidence and provenance are in [case_counts.json](case_counts.json).
Its 28 Sol responses used 136,108 input tokens and 21,594 output tokens
(including 5,231 reasoning tokens). The final cross-scope validation is saved
in [validation_report.json](validation_report.json).
