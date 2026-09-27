# Summary forest prediction interval presentation release — September 27, 2026

Existing valid saved 95% prediction intervals are shown as a heavier dark dashed line below each confidence interval, with endpoint caps. Arrows identify intervals clipped at the established axis boundaries. The localized caption says that the dashed line below the CI is the 95% prediction interval where available. Missing, nonfinite, nonpositive or reversed ratio intervals are not drawn; no PI is calculated or invented by this refresh.

## Scope and inputs

Refreshed **210 PDF forests and 0 existing PNG companions**, covering en, ko, nl, zh-CN, zh-TW. Both centralized summary modes cover urinary and prostate sources. No source snapshot was rewritten. The runtime adapter installs before the immutable source builder runs, and all four source/mode fingerprints are current in summary_bindings.json. The finalizer requires complete forest exports and unchanged nonforest checksums before rebinding manifests.

The refresher reads saved full-precision summaries in Femina and unchanged published summary workbooks in Pee/Uro. Pooled analyses, extraction decisions, workbooks, diagnostic scatterplots, individual-study plots, and evidence inputs are not rebuilt. Existing totals, disease/analysis notes, categories, and subexposure footers are preserved.

## Verification

- `protected_before.json` records the initial commit and scientific/nonforest artifact hashes.
- `verification.json` confirms **32,763 byte-identical protected files**, with only 3 permitted presentation manifest changes. It binds the producing code and requires exact active forest coverage.
- `render_report.json` and the source/tag reports bind every refreshed forest to its final SHA-256 and count of valid saved PIs.
- `pdf_validation.json` confirms each exported PDF contains exactly the expected number of dashed PI strokes. English and Dutch captions are text-checked. Existing CJK fonts emit vector glyphs that PDF text extraction omits, so representative Chinese/Korean captions were inspected visually.
- Passed 5 focused PI/runtime-signature tests and all 14 existing summary-output tests. Changed Python modules compile and `git diff --check` passes.
- Representative dense parent panels, shorter disease panels, translated figures and footers were visually inspected. Audit previews and process logs remain outside the repository.

## Reproduction

Run the repository's `refresh_summary_prediction_intervals.py` for the active roots/sources, then `verify_summary_prediction_intervals.py .` and `verify_summary_prediction_interval_pdfs.py .`. Uro also requires `finalize_summary_prediction_intervals.py` after both source export reports finish. Use the existing configured Matplotlib cache and set numerical-library thread limits to 1 for offline exports.
