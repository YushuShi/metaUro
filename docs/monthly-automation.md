# Autonomous monthly evidence update

metaUro searches PubMed, screens and extracts new evidence, calculates its urinary and prostate results, and publishes the complete release itself. The workflow has one checkout: metaUro. It does not access live metaPee/MetaVir repositories or websites. Those names now identify the two preserved analysis scopes.

The checked-in `runtime_code/monthly/` snapshot contains the screening engines, scientific policies, catalogs and provenance hashes. Writable workspaces are copied from metaUro's immutable `sources/` plus its current `updates/` overlays. Baseline files are never overwritten or hardlinked into writable workspaces.

## Services and screening

Set `CORNELL_API_KEY`, `NCBI_EMAIL`, and either `CORNELL_API_BASE_URL` or `API_BASE_URI` in this repository's Actions secrets. `NCBI_API_KEY` is optional: an absent or rejected key uses the paced public NCBI service. No source deploy keys or Tencent credentials are needed.

A gateway model listing verifies all five required model IDs before screening. The primary pipeline uses GPT 5.6 Luna for R1, GPT 5.6 Sol for R2 and GPT 5.6 Luna for source-gated R3. The independent pipeline shares the actual current-window R1 decisions, then uses Claude Opus 5 for R2 and Gemini 3.7 Flash for R3. Actual model identities, source proofs, responses and billing receipts are preserved. It never reseeds the independent review from historical all-history generations.

Gpt-6-astra supplies recommendations for actual disagreements. Disputed contexts remain withheld from new automatic application until an explicit reviewed revision; a model recommendation or an edited CSV does not silently approve evidence. Root `monthlyHumanReview/<run_id>_<scope>.csv` queues show each pipeline's decisions, effect/CI/sample/quality claims and reasons alongside the disagreement fields and human annotation columns. Sequential R2/R3 comparisons are identified separately from independent pipeline comparisons.

## Run and resume

The schedule runs at 16:47 UTC on the 28th; GitHub can delay scheduled jobs. Manual dispatch defaults to a read-only service preflight. Select `update` for the complete pipeline.

```bash
python -m automation.autonomous_monthly preflight --gateway
python -m automation.autonomous_monthly prepare
python -m automation.autonomous_monthly publish
```

Preparation fixes a window ending on the last complete UTC day. Conservative initial watermarks are June 30 for prostate and August 22 for urinary evidence; later manual additions do not imply a completed full-catalog search. After a completed release, both scopes advance through the same actual cutoff. An interrupted window remains pinned until completion.

An unsuccessful workflow uploads its raw screening, source/model receipts and review queues as `metauro-monthly-checkpoint`. The next update restores a checkpoint only when its runtime and input overlay hashes still match. Recovery makes no new model decisions and does not advance the search watermark. Local workspaces under `runtime/monthly/` are scratch; durable audit packets are retained under `updates/monthly/<run_id>/`.

## Publication and consumer contract

Both scopes must complete discovery, both screening pipelines, adjudication, application and validation before either publication pointer changes. New overlays retain historical study estimates and their scientific gates. Display-only additions do not recalculate an existing pool. Summary workbooks/PDFs are generated only under `summary/{yes,no}`. A summary or regression failure restores the previous two-scope pointer and summaries, leaving the paid receipts available for recovery.

The release keeps existing parent-pool winners when new correlated estimates arrive. Newly extracted aggressive-prostate estimates remain visible but held until their diagnosis-defined endpoint and endpoint-specific counts have a validated review receipt. These downstream policy holds are included in the exported human-review CSV, separately labeled from model disagreements; the original native application CSV and gate remain intact.

The full application suite is run for this code release. Recurring evidence publication runs the native packet validators, fixture-based recovery/source/uncertainty/release tests, historical-row conservation checks and summary manifest verification. Historical live-data count assertions are not used to reject a month's legitimate new evidence.

`updates/monthly/state.json` records a completed release, its actual per-scope search cutoffs, run ID and checksummed release receipt. Read-only consumers can call:

```python
from automation.autonomous_monthly import validate_completed_release
release = validate_completed_release(repo_root, require_through="2026-09-27")
```

The function returns `None` when there is no autonomous monthly release. It rejects corrupt/incomplete releases and cutoffs older than the requested minimum. A later completed cutoff is accepted unchanged. The verified export is reconstructible using `automation.sync_sources.assembled_source(repo_root, scope)` and the central `summary/` collection; this performs no review or pooling. Use an isolated Python process to avoid module-name collisions with another application.

The workflow commits the full `updates/`, `summary/`, `monthlyHumanReview/` and `runtime_code/` trees, including deletion awareness. Git history provides release rollback. Historical `sources/`, `evidence-manifest.json`, revision receipts and review decisions remain preserved. The old source-import utilities are retained solely for explicit legacy maintenance; they are not part of the monthly workflow.
