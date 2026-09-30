# Reviewed multisite cancer evidence: derivative publication

This versioned overlay appends 25 primary-source estimates from committed MetaVir and metaPee reviews. MetaVir contributes 8 visible rows (3 poolable; 5 held). metaPee contributes 17 visible rows (16 poolable; 1 held). These are derivative copies, not independent studies.

The source commits are MetaVir `c35d4fa3d8c13288958eb8695e4986fc83c93567` and metaPee `81647e523520b26a892049667bcaf0552a614bf6`. Their registries, source evidence, JBI assessment, hold reasons, conservation and validation receipts are retained under the source-named audit directories.

## Conserved metaUro evidence

Current source repositories contain some older endpoint/case-count revisions not present in the immutable metaUro snapshot. A wholesale file replacement was rejected by the conservation check. Only the 25 registered estimates are appended to metaUro's existing cache rows and screening contexts. All older dictionaries remain unchanged; their numerical analyses are recalculated only where a new eligible estimate enters the pool. No source snapshot, original model packet or `evidence-manifest.json` is rewritten. `conservation.json` and `immutable_before.json` identify the retained evidence; `immutable_after.json` verifies the protected bytes.

The earlier mistaken 0.51 renal bacon estimate is absent from metaUro's active tables in both modes. It is not introduced. The male bladder estimate for PMID23011480 is displayed but held because the same 225 male cases already enter through PMID25081704. Female bladder cases remain eligible. The two Hodge aggressive-prostate estimates retain unknown male N and remain held; its mixed-sex kidney cohort uses the reported 35,593. Artificially sweetened beverages are a distinct canonical choice, Non-nutritive sweetener intake, separate from SSB. Other source holds and overlap decisions are unchanged.

## Reproduction

Run `automation/import_multicancer_review.py prepare` from the prior derivative release, then `automation/refresh_multicancer_results.py --source MetaVir` and `--source metaPee` sequentially. The registry is verified against each clean, committed source release. Each calculation uses metaUro's preserved engine and active runtime safeguards, with its saved baseline. Run the importer `publish` action only after the offline calculations complete.

The `summaries` action uses `summary_outputs.py` transactions and finalization. Only affected exposure rows are recalculated. Other exposure rows, other disease scopes, styles and native OOXML package parts are preserved. Current XLSX/PDF outputs exist only under `summary/no` and `summary/yes`. Artifact Tool authors the workbook values; final native workbooks are reimported and verified. No legacy summary files are placed in `updates` or `sources`.

`tests/test_multicancer_release.py` checks source immutability, all overlay hashes, exact old-record conservation, 25-row visibility and 19-row selection, every hold and current central summaries. `automation/verify_multicancer_api.py` verifies ten public analyze requests across both modes and separate ASB/SSB selectors. Numerical rebuilds perform no network screening or model calls.
