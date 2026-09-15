# Possible shared datasets across different articles

This focused audit follows the clarified question: within the **same displayed exposure and disease scope**, do **two different PubMed articles** have exactly the same sample size and effect size? Same-PMID matches are excluded.

**Four distinct article pairs were identified.** They generate eight exposure-specific matching pairs across seven exposure–disease combinations when related exposures are enabled. With related exposures disabled, six matches remain across six combinations. The two additional matches are the omega-3 and omega-6 results inherited into the PUFA/prostate combination; they are the same article pair.

| Exposure | Disease scope | N | Matching effect | Article PMIDs | Evidence of shared data in saved records |
|---|---|---:|---|---|---|
| Alcohol | Kidney and renal pelvis cancer | 477,325 | HR 0.91 | 25866035 / 28159493 | Original EPIC study and a comment repeating its result. |
| Omega-3 fatty acids | Prostate cancer | 253,138 | HR 1.03 | 38343844 / 39417685 | Preprint and published version of the same UK Biobank study. |
| Omega-6 fatty acids | Prostate cancer | 253,138 | HR 1.01 | 38343844 / 39417685 | Same UK Biobank article pair. |
| Selenium | Prostate cancer | 35,533 | HR 1.04 | 19066370 / 20924966 | Original SELECT trial and a review summarizing that trial. |
| Vitamin E | Prostate cancer | 35,533 | HR 1.13 | 19066370 / 20924966 | Same SELECT article pair. |
| Lycopene | Prostate cancer | 404 | OR 0.18 | 15514967 / 17392149 | Likely same Hangzhou sample: 130 cases and 274 controls; different analyses and CIs. |

The EPIC and UK Biobank pairs warrant particular review because both article representations have permissive saved selection flags. SELECT review rows and both Hangzhou rows are currently unpooled. Saved flags do not by themselves establish every runtime inclusion decision.

These assessments come from local saved publication metadata, abstracts, and evidence provenance. Identical numbers are a screening signal for overlapping data. The Hangzhou model/contrast relationship remains less certain than the primary/comment, trial/review, and preprint/publication relationships. No articles or evidence rows were removed or changed.

The exact-value criterion will miss overlaps where the same cohort is reported with a different sample subset, follow-up, adjusted effect or rounding. The scope is the current reviewed combined evidence, including unpooled rows, not the alternate historical ChatGPT-only view.

[Focused matching-article CSV](possible_shared_datasets.csv) contains all eight matching pairs, article titles, original row/context identifiers, comparisons, confidence intervals, pooling/display flags, and the overlap assessment. [Detailed local-source review](review_notes.md) provides supporting provenance. [Original numerical audit](audit.json) preserves source-file hashes and both effect-value bases.
