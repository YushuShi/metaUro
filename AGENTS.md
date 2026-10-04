# Repository instructions

## Published results are release artifacts

- Publish updated results and their producing code to `V2` (user instruction,
  October 3, 2026). Keep the monthly scheduler on GitHub's default branch
  configured to check out and publish `V2`; scheduler-only changes on `main`
  must not introduce new result artifacts.

- `summary/` is the sole location for current disease summary XLSX and PDF
  outputs, with shared `yes/` and `no/` mode directories for all disease scopes.
  Update generators and downloads through `summary_outputs.py`; never recreate
  old summary files in `sources/`, `runtime/`, or `updates/`. Commit all changed
  summary files and manifests together. The explicitly requested September 22,
  2026 migration is recorded as a deliberate baseline relocation in
  `evidence-revisions/summary-consolidation-2026-09-22/`.

- `updates/` is the authoritative, versioned monthly overlay. Commit every
  changed, added, or intentionally deleted result, review table, checksum, and
  manifest below it in the same commit and push as the code that produced it.
- `runtime_code/` and `runtime_code/manifest.json` are published runtime
  snapshots and must be committed whenever they change.
- `sources/` and `evidence-manifest.json` are immutable baseline snapshots.
  Never rewrite them for a monthly update; publish validated changes through
  `updates/`. Preserve tracked `evidence-revisions/` and `reports/` that audit a
  deliberate baseline or release change.
- Before pushing, inspect staged and unstaged changes and verify that every
  task-related cache, result, plot, workbook, and manifest is included. The
  monthly workflow must continue to stage the complete `updates/` tree with
  deletion awareness.

## Cleanup boundaries

- `.sources/`, `runtime/`, `review-inbox/`, dependency/test caches, logs, and
  temporary validation copies are scratch. Do not treat immutable `sources/`,
  published overlays, revision receipts, or tracked validation reports as
  cleanup targets.
