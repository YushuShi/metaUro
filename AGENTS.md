# Repository instructions

## Published results are release artifacts

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
