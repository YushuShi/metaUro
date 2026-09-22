# Current metaUro summaries

This is the single output directory for all urinary and prostate disease summary
workbooks and PDF figures. Disease names identify the files; there are no separate
metaPee or MetaVir output directories.

- `yes/`: include related exposures (the app default).
- `no/`: selected exposure only.
- `yes/locales/` and `no/locales/`: translated PDFs (`ko`, `nl`, `zh-CN`, `zh-TW`).

Each mode has one `summary_manifest.json` containing the disease scopes, available
files, checksums, and evidence/code fingerprints. Combined and dietary workbooks
are different analyses. English PDFs sit beside their workbooks.

To update all summary workbooks and PDFs after editing saved evidence or analysis
code, run from the metaUro repository:

```sh
python summary_outputs.py
```

Use `--force` to rebuild even when inputs are unchanged. The application checks
the requested summary's inputs and rebuilds stale outputs before displaying or
downloading them. Monthly refreshes also update this directory and publish it
alongside the complete `updates/` overlay. The source builders' old output paths
are redirected here, including explicit `--plot-root` arguments within metaUro.

Interactive per-request analysis settings do not overwrite the published default
summary. To change that default, update the saved evidence or calculation code
and run the refresh command (or open the summary page).

The audited migration in `evidence-revisions/summary-consolidation-2026-09-22/`
records removed paths and their hashes. Previous files remain recoverable from
Git history. New summary files are validated by the manifests here, separately
from the imported evidence snapshots.
