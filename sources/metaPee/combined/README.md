# metaPee combined evidence

`effective_results.json` is the current combined evidence. Raw ChatGPT and Claude+Gemini screening results remain separately preserved.

## Authorized PMC full-text phase — 2026-09-07

GPT-6 Astra reviewed available PMC article text with the user's authorization. PDF-only articles were skipped. This bounded pass contains 113 estimate-review rows: 85 supported corrections were applied, including 20 partial quantitative bundles that remain visible and unpoolable; 28 rows were retained. Of those retained rows, 3 already have human authority. 45 rows from this pass still need follow-up. These are estimate-row counts, not unique article counts or the full outstanding queue, which also contains articles without usable PMC text.

`astra_fulltext/current_decisions.json` stores source-linked decisions and durable record snapshots. `astra_fulltext/review_dispositions.json` covers every original review ID in this pass. Existing human decisions and unrelated resolved estimates are preserved. Numeric sample-size/effect/CI bundles are replaced atomically using verified article evidence; compatible missing non-key metadata may be retained, with a fill receipt. Existing JBI and scientific restrictions remain in force: FAIR evidence and incomplete quantitative bundles are displayed without contributing to pooling.

The full-text overlay is replayed by future combined rebuilds only while both original saved screening records have the same fingerprints. A changed source requires fresh adjudication. Both original screening tracks and historical abstract-phase audit artifacts remain unchanged. This saved combined release does not change the public UI's selected evidence source.

Both `subexposure_views/yes` and `subexposure_views/no` contain regenerated combined caches and summary forest/heterogeneity figures. Application backups, source hashes, live-data checks, and plot-build reports are in [`../../screening_status_reports/astra_fulltext_adjudication_2026-09-07/applications/metaPee/`](../../screening_status_reports/astra_fulltext_adjudication_2026-09-07/applications/metaPee/). `final_verification.json` verifies 9 human-reviewed contexts, 2,042 unrelated contexts, and 66 durable snapshots. The shared report directory contains the current article-level and remaining-review exports.
