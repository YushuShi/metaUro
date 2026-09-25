# Monthly source refresh

The source repositories run NCBI searches and their three screening rounds on the 28th. This application refreshes after those workflows, at 16:47, 20:47, and 23:47 UTC on the 28th, with one recovery attempt at 04:47 UTC on the 29th. GitHub may delay scheduled jobs. A failed source release leaves the current application intact; it can be retried manually after the source finishes.

Private source checkouts use separate read-only deploy keys. The application uses GitHub's built-in token to commit its own generated outputs. No search or paid model calls are duplicated here. The workflow's manual `preflight` mode verifies checkout access and release readiness without changing evidence.

Both urinary and prostate releases must pass checksum and completion checks. New caches and plots are stored in versioned `updates/` overlays; the original `sources/` evidence and its checksum manifest remain unchanged. The application shows the updated results and offers the monthly human-review CSV from the study-selection page. Historical flow counts retain their original scope; the monthly review is identified separately.

## Operator workflow

The default local checkout layout is `.sources/metaPee` and `.sources/MetaVir`.
The directory is ignored by Git. Use preflight first; it verifies both release
identities, their shared publication cutoff, source packets, review artifacts,
application gates, UI outputs, checksums, and clean source checkouts without
changing metaUro.

```bash
python -m automation.monthly_refresh --preflight
python -m automation.monthly_refresh
```

For a checkout elsewhere, pass `--repos-root /path/to/parent`. For a delayed
retry or backfill, pin the expected publication date so the result does not
depend on the day the command runs:

```bash
python -m automation.monthly_refresh --cutoff YYYY-MM-DD --preflight
python -m automation.monthly_refresh --cutoff YYYY-MM-DD
```

The update command validates both sources before publishing either one. It then
installs additive overlays, runs the Python regression suite once against the
installed view, and leaves `sources/` untouched. Repeating the same verified
release is a no-op. Unchanged overlay artifacts and review CSVs reuse their
already-installed bytes instead of creating another monthly copy.

## Release contract

Each source checkout must provide `data/monthly_automation/state.json` pointing
to a completed, checksummed release manifest. The manifest must identify the
expected repository and cutoff, contain the raw screening packet receipts, a
nonempty human-review artifact inventory, the application-gate receipt, and
checksums for every served UI artifact. Both source checkouts must be clean and
committed. A missing, incomplete, mismatched, or modified release is rejected;
the current application pointer remains in place.

`updates/manifest.json` is the publication pointer. Files under
`updates/<source>/versions/` are immutable content for that pointer. Git history
provides rollback: revert the monthly refresh commit rather than editing an
installed overlay or the preserved source snapshot in place.

## Manual-review files and local cleanup

Completed review files become durable only after the source project archives
them with a checksum, decision checkpoint, and application audit. Temporary
files awaiting that process belong in the ignored `review-inbox/`, not at the
repository root. After incorporation is verified, remove the inbox copy; do
not remove the archived file, checkpoint, audit, or evidence revision.

The ignored `runtime/`, `test-deps/`, Python caches, diagnostic scratch
directories, and SQLite `-wal`/`-shm` files are disposable. Do not bulk-delete
date-named files inside `sources/`, `reports/`, or `evidence-revisions/`: those
trees are part of the evidence and provenance contract even when they are not
loaded by the web application.
