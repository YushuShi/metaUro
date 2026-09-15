# Monthly source refresh

The source repositories run NCBI searches and their three screening rounds on the 28th. This application refreshes after those workflows, at 16:47, 20:47, and 23:47 UTC on the 28th, with one recovery attempt at 04:47 UTC on the 29th. GitHub may delay scheduled jobs. A failed source release leaves the current application intact; it can be retried manually after the source finishes.

Private source checkouts use separate read-only deploy keys. The application uses GitHub's built-in token to commit its own generated outputs. No search or paid model calls are duplicated here. The workflow's manual `preflight` mode verifies checkout access and release readiness without changing evidence.

Both urinary and prostate releases must pass checksum and completion checks. New caches and plots are stored in versioned `updates/` overlays; the original `sources/` evidence and its checksum manifest remain unchanged. The application shows the updated results and offers the monthly human-review CSV from the study-selection page. Historical flow counts retain their original scope; the monthly review is identified separately.
