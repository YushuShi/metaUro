# Repeated sample size and effect size in metaUro

Audit date: 14 September 2026. Evidence was read from the installed metaUro release after the 164-row human-review update. **No evidence was changed or removed.**

**89 matching evidence pairs were found in 51 exposure–disease combinations with related exposures enabled. With related exposures disabled, 46 pairs remain in 36 combinations.** The 46 are included in the 89; do not add these counts.

The primary results compare the exact numeric values used by the main evidence table before display rounding. Each pair has the same sample size and effect size within the same displayed exposure and disease scope.

| Collection | Related exposures | Matching groups | Matching pairs | Exposure–disease combinations | Same PMID pairs | Different PMID pairs |
|---|---|---:|---:|---:|---:|---:|
| Urinary tract | yes | 49 | 59 | 37 | 58 | 1 |
| Prostate | yes | 30 | 30 | 14 | 23 | 7 |
| Urinary tract | no | 33 | 33 | 26 | 32 | 1 |
| Prostate | no | 13 | 13 | 10 | 8 | 5 |

A group is one shared sample-size/effect-size value within a combination. A group with four rows produces six pairs. Counts are scope-specific: evidence displayed in both a parent and a narrower disease scope appears in each scope’s audit.

## Different-PMID matches

The eight different-PMID matches below represent four distinct PMID-pair relationships. Related-exposure inheritance repeats some relationships under PUFA. These deserve publication/cohort checks; identical numbers alone do not prove that two rows should be removed.

| Match | Exposure | Disease scope | Sample size | Effect | PMIDs | Same complete CI? | Related exposure settings |
|---|---|---|---:|---|---|---|---|
| DUP-006 | Lycopene | Prostate cancer | 404 | OR 0.18 | 17392149 / 15514967 | No | no; yes |
| DUP-012 | Omega-3 fatty acids | Prostate cancer | 253,138 | HR 1.03 | 38343844 / 39417685 | No | no; yes |
| DUP-015 | Omega-6 fatty acids | Prostate cancer | 253,138 | HR 1.01 | 38343844 / 39417685 | Yes | no; yes |
| DUP-021 | Polyunsaturated fat (PUFA) | Prostate cancer | 253,138 | HR 1.01 | 38343844 / 39417685 | Yes | yes |
| DUP-022 | Polyunsaturated fat (PUFA) | Prostate cancer | 253,138 | HR 1.03 | 38343844 / 39417685 | No | yes |
| DUP-025 | Selenium | Prostate cancer | 35,533 | HR 1.04 | 20924966 / 19066370 | No | no; yes |
| DUP-029 | Vitamin E | Prostate cancer | 35,533 | HR 1.13 | 20924966 / 19066370 | Yes | no; yes |
| DUP-034 | Alcohol | Kidney and renal pelvis cancer | 477,325 | HR 0.91 | 25866035 / 28159493 | Yes | no; yes |

Local publication metadata and saved evidence indicate:

- **Alcohol / kidney and renal pelvis cancer, PMIDs 25866035 and 28159493:** the second record is marked as a comment and refers to the original EPIC article. Both saved rows have permissive pooling-selection flags, making this a priority for review.
- **Omega-3 and omega-6 / prostate cancer, PMIDs 38343844 and 39417685:** saved titles, authors, cohort and sample size match; metadata identifies preprint and published versions. The same pairs also appear under inherited PUFA.
- **Selenium and vitamin E / prostate cancer, PMIDs 20924966 and 19066370:** saved metadata identifies a review of SELECT and the original trial; the review rows are display-only/unpooled.
- **Lycopene / prostate cancer, PMIDs 17392149 and 15514967:** saved population details describe the same Hangzhou 130-case/274-control sample; comparison/model details need review, and the CIs differ.

See [detailed review notes](review_notes.md) for the local evidence supporting these interpretations and representative same-PMID cases.

## Same-PMID matches

81 of the 89 pairs share a PMID. They include parent/child disease projections, related exposures, unpooled evidence rows, and separate subgroup estimates. For example, PMID 18420355 has male and female alcohol strata with the same saved N=6,177 and OR=0.7. These are explicitly different sex strata and cannot be called accidental duplicates from the two matching numbers alone.

The pair file includes comparison text, population, timing, stage, original context, record identifiers, confidence intervals, exposure provenance, and saved pooling/display flags. Missing pooling flags are left blank and should not be interpreted as false. Actual pooling also depends on the analysis settings and source selection rules.

## Coverage and comparison method

- Audited all current **combined** serving caches: 233 urinary and 246 prostate exposure–disease combinations per setting, for 479 combinations per setting. The separate historical ChatGPT-only view was not mixed into the reviewed combined evidence.
- Compared all saved evidence rows, including display-only/unpooled rows and available quality levels, before optional user filters. Rows lacking a finite positive sample size or effect size were excluded from numerical matching.
- Matched exact decimal numeric values, ignoring only number formatting such as commas/trailing zeros. No three-decimal rounding or tolerance was used. Effect type and CIs were not required to match, but their equality is recorded.
- Urinary main-table effects use `Original Effect Size` when present, otherwise `Effect Size`; prostate main-table effects use `Effect Size`. Table sample size uses `Participants` when present, otherwise `Sample Size`; there were no inconsistent aliases in the audited rows.
- Also audited stored analysis values (`Sample Size` + `Effect Size`). That basis finds **84 pairs in 46 combinations** with related exposures enabled, or **41 pairs in 31 combinations** with them disabled. The difference reflects direction-adjusted urinary estimates.
- All/core cache variants were checked for identical evidence arrays and counted once; only exclude-meta=true serving variants exist.
- The 1,916 cache files read by the audit were rehashed after scanning and were unchanged. Independent urinary and prostate audits agree with the counts above.

Saved cache files cover six urinary disease scopes plus general and aggressive prostate cancer. The configured scopes with no saved serving cache were: squamous cell carcinoma of the bladder, adenocarcinoma of the bladder, small cell carcinoma of the bladder, chromophobe renal cell carcinoma, urothelial carcinoma of the renal pelvis, and renal sarcoma. These were not treated as evidence containing no duplicates; there was no saved evidence to compare.

## Files

- [matching_evidence_pairs.csv](matching_evidence_pairs.csv): **89 main-table matching pairs**, one row per pair; filter `modes` for no/yes.
- [analysis_value_matches.csv](analysis_value_matches.csv): **84 pairs** matching stored analysis values.
- [scope_coverage.csv](scope_coverage.csv): every scanned source/setting/scope/basis, including zero-match combinations.
- [audit.json](audit.json): full match receipts, group occurrences, coverage and source-file SHA-256 hashes. Its 93 pair/value entries retain different values for the same record pair when the table and analysis directions differ.
- [audit_duplicate_sample_effect.py](audit_duplicate_sample_effect.py): reproducible read-only scanner.

Re-run from the metaUro folder:

```bash
python3 reports/duplicate_sample_size_effect_2026-09-14/audit_duplicate_sample_effect.py --root . --out reports/duplicate_sample_size_effect_2026-09-14
```

The scanner regenerates the CSV/JSON audit outputs. This narrative review documents the 14 September 2026 findings and should be reviewed if the evidence changes.
