# Current MetaVir comparison

The comparison uses current saved ChatGPT and Claude+Gemini evidence for 6,314 shared reviewed contexts. Only total N, point estimate, and CI determine numeric disagreement; stored numeric formatting and compatible rounding are ignored. Source exclusion is recorded as an unavailable counterpart rather than an extra numeric key.

There are 499 current discrepancy contexts (641 historical field mappings). Neither the context set nor the field set is a subset of the previous set. Prior Fable/combined final decisions are reused for 478 covered contexts; explicit manual decisions take precedence. The 18 newly uncovered contexts retain both source candidates without pooling them.

For agreeing key bundles, 156 p-value conflicts were reconciled using the two-sided Wald test. Another 4 could not be computed from the saved bundle. The p-value audit records when neither reported value matches; no source is declared correct in those cases.

Missing nonkey fields are filled with field-level donor provenance. An existing JBI assessment is preserved as one assessment; a missing whole assessment may be copied from the other source.

Machine-readable outputs:
- `comparison_rows.json`: input for the final article CSV.
- `p_resolutions.json`: p-value audit, including both original values and the Wald result.
- `metadata_fills.json`: missing nonkey fields and their source.
- `summary.json`: counts, exact subset checks and source hashes.
- `../../combined/final_screening.json`: installed combined screening records.
- `../../combined/_pending_review.json`: current remaining review queue.
- `../../combined/_history/before_current_key_comparison_2026-09-05/`: prior combined files, preserved byte-for-byte.

Future refresh: `python scripts/build_combined_results.py` followed by `python scripts/rebuild_subexposure_results.py`. Historical Fable/model packets stay unchanged. No new model or gateway calls were made.
