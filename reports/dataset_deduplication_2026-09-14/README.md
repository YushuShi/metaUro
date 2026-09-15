# Duplicate check after the selected-article update

The curator's six exposure/disease decisions have been applied in staging. The re-audit of the current combined serving caches finds **zero pairs of different PubMed articles with exactly equal sample size and effect size**, in either related-exposure setting and on both the table-value and analysis-value bases.

| Related exposures | Different-article matches before | Different-article matches after |
|---|---:|---:|
| Enabled | 8 | 0 |
| Disabled | 6 | 0 |

The eight original matches represented four article relationships across seven displayed exposure/disease combinations; the six direct decisions also resolve the two inherited PUFA appearances. See [the applied decisions](../../evidence-revisions/2026-09-14-dataset-deduplication/README.md).

The remaining 81 table-value matches with related exposures enabled (40 when disabled) all share a PMID. They were outside the clarified different-article deduplication request and remain unchanged. This exact-value re-audit does not establish that every possible overlapping cohort has been found; reports can use different follow-up, subsets, models or numerical values.

The scan covers 479 saved exposure/disease combinations per setting, including empty saved combinations. It compares complete finite positive numerical values before display rounding, includes unpooled/display rows, checks both exposure modes, and confirms identical core/all evidence arrays before counting them once. All 1,916 scanned cache files were unchanged during the audit.

- [Audit, groups and source SHA-256 hashes](audit.json)
- [Remaining same-PMID table-value matches](matching_evidence_pairs.csv)
- [Remaining same-PMID analysis-value matches](analysis_value_matches.csv)
- [Full scope coverage](scope_coverage.csv)
- [Reproducible scanner](../duplicate_sample_size_effect_2026-09-14/audit_duplicate_sample_effect.py)

The original audit is preserved in the adjacent duplicate_sample_size_effect_2026-09-14 folder as a record of the evidence before the curator's exclusions.
