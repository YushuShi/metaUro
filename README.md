# MetaUro

The **Minimum cases** control defaults to **0** (no case-count cutoff). A positive
cutoff uses reported cases, or a labelled estimate of `round(N × selected baseline
risk)` when cases are unreported. Estimates update with the applicable sex,
race/ethnicity, starting-age and observation-period settings. Missing N or an
unavailable baseline leaves the estimate unknown; a cutoff of 0 still passes the
case-count filter. Other study eligibility rules continue to apply. Visitor
estimates do not overwrite reported counts or saved evidence caches.

Explore nutrition and urologic cancer evidence across 14 disease scopes, including bladder, kidney, renal pelvis, prostate, and supported subtypes.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5012. Production: `gunicorn --workers 1 --threads 4 --timeout 240 app:app`. A Render configuration is included. The first analysis starts the relevant Python engine; subsequent requests reuse it. Each source serializes calculations to protect its original global state and plotting code. Different sources can run concurrently. Allow sufficient memory for two scientific Python engines and their evidence.

## Interface

- **Evidence:** select any of the 14 disease scopes; cross-source selection routes to the appropriate engine. Related-exposure inclusion, study filters, temporary study selection, analysis, plots, and CSV downloads remain available.
- **Summary:** saved cross-exposure plots and workbooks for both collections, rebuilt when reviewed evidence changes. These exports retain their saved analysis settings.
- **Study selection:** the top-right link opens `/study-selection`, with separate urinary and prostate review flows adapted from PRISMA 2020 (Page, McKenzie, et al., 2021). Counts are derived read-only from saved screening decisions and deduplicated by PMID across exposure/cancer contexts. These are report counts, not distinct-study counts. Detailed assessment includes conditional full-text review; unavailable identification, retrieval, and study-linkage totals remain explicitly unknown. Prostate inclusion uses the canonical screening record; any additional provisional display reports are counted separately.
- **Settings:** a dedicated responsive page for exact SEER population/age/interval risk, per-disease manual reference risk, RR/HR/OR display, forest labels, sample-size planning, and exposure hierarchy. Preferences persist only in this browser, independently for each source. Reset affects only the current source. Invalid stored preferences recover to defaults.
- **About:** explains disease coverage, analysis settings, and the alternate prostate evidence view.

Settings apply to request-local interactive calculations, not saved summaries. Unsupported histologies and aggressive prostate do not borrow a parent baseline. The interface shows unavailable baselines and the engine's identity approximation explicitly.

## Evidence preservation

`sources/metaPee/` and `sources/MetaVir/` began as byte-for-byte snapshots of the local projects, including uncommitted and untracked saved results, screening decisions, plots, data, audit outputs, original source code, and tests. Their engines run in separate processes so incompatible modules and disease policies cannot collide. Authorized human review updates are applied to these local evidence collections with decision-level provenance; the separate original projects and original model-screening packets remain unchanged.

The import excludes credentials, Git metadata, Python/tool caches, runtime logs, temporary directories, and machine-specific ignore/attribute files. `evidence-manifest.json` lists every active evidence file, its size and SHA-256, and exclusion rules. The original import manifest and before/after revision hashes are preserved under `evidence-revisions/`. No credentials are required to explore saved evidence. Snapshot text normalization is disabled in Git to retain exact bytes.

Some OneDrive-only PNGs could not be read directly during the original import. `evidence-import-recovery.json` records their recovery from existing local copies or Git blobs, accepted only when their byte size and computed OneDrive content fingerprint matched the metadata for the exact original file. It records SHA-256 for the recovered bytes. OneDrive's QuickXorHash is an integrity fingerprint, not a cryptographic proof of equality. That import did not regenerate plots; subsequent authorized evidence revisions rebuild affected outputs.

The metaUro worker rejects writes into either snapshot. Persistent verification/exclusion actions are disabled; temporary study selection and reanalysis remain available. Runtime logs and plotting-library caches live under ignored `runtime/`. Original refresh workflows are archived inside the snapshots and are not active GitHub workflows in this repository.

metaUro now searches and updates both scopes independently, using its checked-in
runtime and validated overlays instead of editing the snapshots or checking out
external repositories. See the [monthly update runbook](docs/monthly-automation.md) for
preflight, deterministic retry, validation, rollback, and cleanup instructions.

```bash
python scripts/verify_evidence.py
# Optional: compare the preserved import baseline against the original projects:
python scripts/verify_evidence.py --original-parent /path/containing/metaPee/and/MetaVir
```

## Human review update — 14 September 2026

The completed 8 September review CSVs supplied by the curator contain 68 urinary-cancer and 96 prostate-cancer review rows. Decisions are applied by context and estimate identity, with the original review text retained. Source selections use the chosen pipeline's estimate bundle; explicit corrections, reciprocal transformations, duplicate removals and unavailable estimates receive individual application receipts. Numerical results are not inferred from a statement of no association.

`evidence-revisions/2026-09-14/revision.json` records input hashes, changed files and source-specific audit locations. The source collections retain durable human-review decisions and regenerated application caches and summaries. Historical model packets remain available for comparison. The application continues to reject evidence writes during normal use; restart any running metaUro process after installing an evidence revision so its workers load the new release.

## Duplicate-article decisions — 14 September 2026

The curator retained PMID 25866035 for Alcohol / Kidney and renal pelvis cancer, PMID 15514967 for Lycopene / Prostate cancer, PMID 39417685 for Omega-3 and Omega-6 fatty acids / Prostate cancer, and PMID 19066370 for Selenium and Vitamin E / Prostate cancer. The corresponding duplicate-article evidence is excluded from these combinations and their applicable inherited exposure views. Other exposure and disease contexts remain intact. These later decisions take precedence over earlier model and human-review choices without rewriting their historical receipts.

`evidence-revisions/2026-09-14-dataset-deduplication/` records the scope, before/after hashes, source audits and verification results. The previous release manifest is preserved alongside the original import manifest.

## Validation

```bash
python -m pip install pytest
pytest tests
node tests/settings.test.js static/analysis-settings.js
node tests/navigation.test.js
python scripts/verify_evidence.py
```

The source projects' historical tests are preserved under their own snapshots; run metaUro tests from the repository root. Runtime writes do not modify source evidence. The GitHub repository is private.
