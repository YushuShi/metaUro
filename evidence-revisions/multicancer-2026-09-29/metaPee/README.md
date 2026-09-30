# Primary-source enrichment, September 2026

17 source-reviewed rows: 16 poolable, 1 displayed with an overlap hold. Each registry entry binds one PMID, exact cancer site, exposure, sex/risk set, effect and reported CI to its primary table and an existing or newly documented JBI assessment. Original model extraction packets are unchanged.

The male Uruguay bladder estimate overlaps PMID25081704 (same 225 cases); its female estimate remains eligible. The existing 0.51 (0.24–1.10) renal estimate is female bacon, not male total processed meat, and is explicitly corrected and held because Table 4 does not establish its contrast.

PMIDs 10760833 and 31029049 have no usable verified primary CI packet in this review and are not added. The secondary RCC quotation for 10760833 is corroboration only. PMID19640186 remains excluded from new pools by its existing Fair assessment and cohort overlap. Total-cancer results are never assigned to individual cancer sites. EPIC, UK Biobank, NIH-AARP, MCCS and Uruguay populations were checked against the current site/exposure pools. UK Biobank broader kidney and RCC endpoints are never selected together in the parent kidney pool.

Artificially sweetened beverages are distinct from SSB under the existing selectable canonical exposure **Non-nutritive sweetener intake**. Hodge's site-specific exposure is retained in the primary-source extraction; no ASB estimate is relabelled as SSB. Its new 8/11 JBI appraisal is Moderate (72.73%); the unvalidated soft-drink question and unclear follow-up completeness are recorded. Overall N=35,593 is used only for the mixed-sex kidney cohort and never assigned to male aggressive prostate.

## Reproduction and release checks

From the pre-release commit, run `scripts/apply_multicancer_evidence.py` once with the repository's Python environment. The final registry already contains every hold; `scripts/refresh_multicancer_holds.py` (metaPee only) records the stricter male-bladder overlap decision discovered after the initial staged rebuild. `scripts/refresh_multicancer_summaries.py` recalculates affected exposure rows while preserving unrelated workbook rows and regenerates their scientific PDFs.

`scripts/build_multicancer_workbooks.mjs` authors workbook values using the Artifact Tool runtime and `workbook_payload.json`. `scripts/preserve_multicancer_workbooks.py` preserves native OOXML styles and untouched parts from the pre-release Git HEAD. Final workbooks were imported again with Artifact Tool, compared cell-for-cell, scanned for errors and rendered; receipts are `workbook_preservation.json` and `workbook_verification.json`. Other sheets, unrelated summary rows and native package parts are preserved.

`tests/test_multicancer_evidence.py` checks source identity, gates, disjoint sexes, unknown-source rejection, cohort overlap and exact-site selection. `scripts/seal_multicancer_release.py` checks old screening records and scientific study tuples against pre-release HEAD, verifies every local plot URL, binds active manifests, and seals all changed code/results in `release_manifest.json`. `conservation.json` records the historical evidence checks. JSON uses readable indentation; line-count reductions from temporary minification were removed before release.

The preserved base screening build ID describes its original adjudication release. The source-review revision and artifact hashes identify these additions separately. Historical model/frozen-review provenance is not rewritten as a new model run. No network/model calls occur during numerical replay.
