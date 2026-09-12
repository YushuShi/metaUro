# Offline track reconciliation — metaPee

Completed 2026-09-05 using the saved current effective ChatGPT and Claude+Gemini results. No new gateway calls were made. Original model packets, effective screening tracks, and previous ChatGPT/Claude+Gemini caches remain unchanged; 6,543 SHA-256 checks passed.

## Outputs

- `combined/effective_results.json`: 2,126 contexts, with human review precedence, numeric agreements, and both candidates for unresolved key disagreements.
- `combined/<exposure>/*_true_core.json` and `*_true_all.json`: 390 files across 195 scopes; 887 visible estimates and 224 selected estimates. 102 scopes have computed headlines. These new combined files contain no stale plot URLs; existing track UI routing is unchanged.
- `outputs/track_comparison_2026-09-05/discrepancies.json`: 260 estimate pairs across 222 contexts and 138 PMIDs. Each row includes both saved numeric bundles, source estimate indexes, gender, stratum, subgroup, comparison type, and adjustment model.
- `outputs/track_comparison_2026-09-05/p_resolutions.json`: 77 p-only audits, including both reported p values and their relations, the computed two-sided Wald p value, matching source, and a reason when calculation is unavailable. 55 were calculated and applied; 22 lack a supported, valid Wald bundle.
- Existing combined versions were backed up before overwrite under `outputs/track_comparison_2026-09-05/before/combined`.

## Policy and provenance

Key comparison uses total N, effect estimate, and CI only. Counts compare exactly; ratio rounding follows the shared policy. Saved values are compared before any display direction harmonization. Missing nonkey fields are filled from the other source, with Claude+Gemini preferred for agreed numeric bundles. There are 3,695 root metadata fills and 798 estimate metadata fills. Numeric derivation methods and imputation provenance are protected from cross-source filling. Original source receipts are retained as source provenance when a derived combined estimate changes metadata or p value.

Wald p values use a log scale and null 1 for identified RR/OR/HR, and a linear scale and null 0 for supported additive effect types. Unsupported or inconsistent effect scales remain explicitly unavailable. Source p values are retained in the audit. Explicit saved inversion flags are applied once to app-facing ratio estimates and reversed CI bounds; the discrepancy report continues to show the original saved values.

All 368 unresolved candidates are visible but unpooled. All 120 FAIR rows are visible but unpooled. Scientific, clinical, hierarchy, curated exclusion, and sample overlap restrictions remain active. JBI item grades were not synthesized across reviewers. 0 human-reviewed contexts retain their original decisions. 

## Verification

- 11 focused adapter tests passed.
- All protected source hashes match the initial manifest.
- Every scope with selected estimates has a computed headline; zero engine errors.
- All/core cache pairs are byte-identical; selected study labels are unique.
- Review candidates and FAIR rows are never selected; explicit reciprocal transforms were checked.
- Original coherent JBI and human-review dictionaries match their authoritative source.
- `validation.json` records counts and stable artifact SHA-256 values.
