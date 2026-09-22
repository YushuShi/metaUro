# Summary consolidation

The user requested one current copy of every disease summary workbook and PDF
under `summary/`, with no metaPee/MetaVir folder split. The `yes/` and `no/`
directories preserve the two related-exposure settings; localized PDFs retain
their language directories.

`inventory.json` identifies the original files and workbook values before the
migration. `validation.json` compares all rebuilt workbooks with those values.
Pooled estimates, intervals, counts and row membership are unchanged. Parent
urinary-scope I2/Egger diagnostics now retain the complete saved headline values,
including the correction formerly applied only to runtime downloads. Tiny
prostate p-value differences reflect Excel numeric serialization.

`revision.json` records each removed path and changed source generator with its
before/after checksum. `prior-release-manifest.json` preserves the preceding
release inventory. Original tracked files can be recovered from the parent Git
commit. No old summary binary copies are retained in the current repository.

The current summary manifests are maintained in `summary/` and verified directly.
They are deliberately outside the immutable study-evidence inventory so future
summary refreshes do not rewrite the baseline manifest.
