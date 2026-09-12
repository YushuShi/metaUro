Quantitative poolability policy applied 2026-09-05

Total N remains required. Cases are optional; when the repository has a matching outcome lifetime risk, the existing UI stores and labels Estimated Cases. Missing unit, scale, model, stratum/subgroup, and direct measurement do not prevent pooling. Exact cases and the old default minimum case-count cutoff are no longer required for default pooling or summaries.

A ratio estimate and usable confidence interval, or an exact two-sided coefficient p-value, provide quantitative uncertainty. Missing CI confidence level defaults to 95%; explicit non95 intervals are transformed to95 under the Wald assumption with original bounds/level retained. A missing ratio point is the geometric midpoint of its CI. Explicit log-scale intervals/points are exponentiated. Bounds, trend/interaction/global tests and one-sided p-values are not silently interpreted as exact coefficient p-values.

Missing direction defaults to higher versus lower (yes versus no for binary comparisons) with direction_imputed provenance and a UI marker. Explicit reported inversions are preserved. Adjusted aOR/aRR/aHR aliases map to OR/RR/HR and matched adjusted estimates are preferred. Original model packets, receipts and human overrides remain the source evidence.

JBI thresholds, actual exposure/outcome scope, incident-versus-diagnostic restrictions and human exclusions remain unchanged. Full-text review is required for remaining quantitative gaps or the existing Fair JBI rule. Operational review failures are not marked as new model completions.

Rebuild offline after future accepted screening or manual corrections:
python scripts/rebuild_relaxed_poolability.py
