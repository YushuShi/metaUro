# Aggressive prostate cancer abstract review

This directory contains the Sol review requested for the included prostate-incidence evidence in `Cached_results` and `combined`. The review runner never edits those caches.

- `articles.json`: complete input corpus, full available PubMed abstracts, abstract provenance, stable estimate-row IDs, and original file/index occurrences.
- `coverage_review.json`, `coverage_reviews/`, and `before_coverage_review/`: focused direct-exposure coverage decisions, literal support/exclusion reasons, and preserved pre-coverage reviews.
- `reviews/<PMID>.json`: validated article and estimate classifications, literal endpoint excerpts, new abstract-supported estimates, and validation findings.
- `review.json`: aggregate validated reviews consumed by the disease-scope projection.
- `manifest.json`: corpus completeness, classification counts, gateway model IDs, and provider-reported token usage.
- `responses/`: original Sol JSON responses and usage receipts, including unsuccessful validation attempts when a call returned output.
- `pubmed_abstracts.xml` and `pubmed_abstracts.json`: official PubMed records requested for articles lacking a locally cached abstract. No-abstract records remain uncertain.
- `initial_system_prompt.txt`, `current_system_prompt.txt`, and `response_schema.json`: review prompt and output contracts. Later focused adjudication responses retain their exact system prompt and request digest.

## Interpretation

Article-level `both` says the abstract studies general and aggressive incident disease. It does not mean that a saved overall effect can be relabeled aggressive. Each saved estimate is separately classified using its abstract and existing field-level evidence; endpoint support from existing full-text excerpts is identified explicitly. Original estimates and their reported comparison orientation are preserved in the corpus. New estimates retain the paper's original effect orientation and require the effect and both CI numbers in a literal abstract quote. Aggressive associations without those numbers are represented with null effect/CI and must remain display-only.

Definitions are study-specific. Explicit aggressive, high-grade/high-risk,
advanced, metastatic/distant, extraprostatic, and equivalent pathological
endpoints may qualify only when defined at first diagnosis. A mention in
background, a generic statement about stratification by unspecified
stage/grade, fatal/lethal disease determined by later death or metastasis, or
survival after diagnosis does not establish aggressive incidence. Missing or
insufficient abstract evidence is uncertain.

The retained `review.json` is the frozen version-1 model snapshot and therefore
still documents its historical lethal/fatal classifications. The later human
endpoint decision is authoritative in
`../incidence_endpoint_review/current_exclusions.json`; projection replays that
checkpoint and preserves excluded source rows in cache-level audit state. A
future model review uses the version-2 prompt in code and the same diagnosis-
only contract.

Total analytic N and cases are different quantities. Extracted numeric counts must occur in the cited abstract text. Full prospective cohort analyses sharing participants may share total N across general/aggressive endpoints; nested case-control and case-cohort denominators are not automatically forced to the parent cohort population. Original model flags changed by validation remain auditable.

## Resume and validation

```sh
python scripts/aggressive_prostate_review.py --workers 12 --batch-size 3
python scripts/aggressive_prostate_review.py --revalidate --prepare-only
```

Resume is keyed by model/version and a digest of the title, complete abstract, and deduplicated estimate contexts. The `evidence_row_id` helper ignores disease annotations and exposure inheritance copies, and unwraps `disease_scope_original_row`. Additional generated rows are excluded from the input corpus. Every review retains a gateway receipt path and the abstract digest. `complete: true` means every corpus PMID has a current validated Sol response, including uncertainty for absent abstracts; it does not imply every estimate is poolable.
