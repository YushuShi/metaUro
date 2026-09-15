# Human review evidence update — 14 September 2026

Applied the supplied human-review results to the evidence collections within metaUro: **164 review rows**, comprising **68 urinary-cancer rows across 56 contexts** and **96 prostate-cancer rows across 96 contexts**. Review rows are decisions, not distinct studies.

## What changed

- Applied the explicitly selected model estimate, numerical correction, reciprocal direction correction, duplicate removal, or context exclusion recorded in each reviewed row.
- Preserved each original CSV byte for byte, the exact review comments, and decision-level before/after records. Original model-screening packets and the separate source projects were not edited.
- Rebuilt the evidence served by the application, both related-exposure settings, summary exports, and study-selection counts.
- Human decisions are replayed after automated reconciliation so a future rebuild retains them. Changes in their source records require reconciliation instead of silently reusing an outdated decision.

## Interpretation of conflicting notes

For PMID 19093214, the Mineral supplements row is excluded according to its own review note. The separately reviewed Multivitamin row retains the selected ChatGPT estimate, 1.2 (95% CI 0.8–1.9). Incidental numbers for other exposures in a note were not substituted into separately identified exposure or time-window records.

The phrase “no association” was not converted to an effect size of 1 and was not used to exclude nonsignificant numerical results. A row selecting a model follows that model’s available evidence; a row rejecting both models without replacement numbers remains qualitative.

## Quantitative details still requiring confirmation

All 164 supplied decisions have been applied. Six prostate records retain specific quantitative follow-up items; no missing number or contrast has been invented to complete them.

| Review ID | PMID / exposure | Detail to confirm |
|---|---|---|
| metaVir-0160 | 18000020 / Calcium | Provide the replacement calcium effect and matching CI, or confirm that no numerical total/nonadvanced prostate-cancer estimate is available. 'Unrelated' alone does not specify an effect size. |
| metaVir-0254 | 22190107 / Dairy | Confirm the sample size and exposure time window for OR 1.38 (0.85–2.25): saved tracks give N=2268 and N=2267 and earlier effects concern a different outcome/contrast. |
| metaVir-0256 | 22527172 / Oats | Provide the analyzed N and exposure time window for corrected oatmeal OR 1.10 (0.83–1.44); the saved full-text review leaves N missing and earlier estimates refer to different life periods. |
| metaVir-0257 | 22527172 / Whole-grain intake | Provide the analyzed N and grain/time-window contrast for corrected whole-grain OR 0.85 (0.65–1.12); the saved full-text review leaves N missing. |
| metaVir-0331 | 26047130 / Vitamin D | Confirm the analyzed N for the Table 1 vitamin-D OR 1.53 (0.70–3.45): overall N=152, but vitamin-D categories total 150; the prior interaction-model estimate is replaced. |
| metaVir-0456 | 41388067 / Selenium | Confirm the comparison/reference group and analyzed N for selenium OR 4.42 (1.68–11.59): prior tracks differ in reference tertile and N=285 versus 188. |

The decision checkpoints retain the current quantitative eligibility for each record; applying a human decision does not by itself guarantee that a record meets all pooling requirements.

## Validation

- All 164 review IDs, context IDs, and exact comments match the supplied CSVs; both archived files retain their original SHA-256 hashes.
- All **37 metaUro Python tests passed**, including corrected estimates and excluded contexts through the live analysis API in both related-exposure modes. All **21 JavaScript checks passed**.
- **55 urinary-source checks** and **15 relevant prostate-source checks passed**. Three historical Astra full-text display tests fail identically in the untouched prostate source snapshot (their fixtures produce no estimate pair); these are pre-existing failures outside this update.
- The update preserves **2,070 unrelated urinary canonical contexts**, **35,449 unrelated prostate canonical contexts**, and **244 aggressive-prostate cache study arrays**. The protected original model packets are unchanged.
- The active release checksum verification passes for **35,088 evidence files** and the revision provenance files.

## Audit locations

- [Revision manifest](revision.json): source hashes and every changed evidence file.
- [Urinary decision audit](../../sources/metaPee/combined/human_review/application_audit_2026-09-14.json).
- [Prostate decision audit](../../sources/MetaVir/combined/human_review/application_audit.json).
- [Prostate follow-up details](../../sources/MetaVir/combined/human_review/follow_up.json).

Restart an already-running metaUro process to load the updated evidence in its workers.
