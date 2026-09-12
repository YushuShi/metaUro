# metaUro

Explore nutrition and urologic cancer evidence across 14 disease scopes, including bladder, kidney, renal pelvis, prostate, and supported subtypes.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5012. Production: `gunicorn --workers 1 --threads 4 --timeout 240 app:app`. A Render configuration is included. The first analysis starts the relevant Python engine; subsequent requests reuse it. Each source serializes calculations to protect its original global state and plotting code. Different sources can run concurrently. Allow sufficient memory for two scientific Python engines and their evidence.

## Interface

- **Evidence:** select any of the 14 disease scopes; cross-source selection routes to the appropriate engine. Related-exposure inclusion, study filters, temporary study selection, analysis, plots, and CSV downloads remain available.
- **Summary:** original saved cross-exposure plots and workbooks for both collections. These exports retain their original settings.
- **Settings:** a dedicated responsive page for exact SEER population/age/interval risk, per-disease manual reference risk, RR/HR/OR display, forest labels, sample-size planning, and exposure hierarchy. Preferences persist only in this browser, independently for each source. Reset affects only the current source. Invalid stored preferences recover to defaults.
- **About:** explains disease coverage, analysis settings, and the alternate prostate evidence view.

Settings apply to request-local interactive calculations, not saved summaries. Unsupported histologies and aggressive prostate do not borrow a parent baseline. The interface shows unavailable baselines and the engine's identity approximation explicitly.

## Evidence preservation

`sources/metaPee/` and `sources/MetaVir/` are byte-for-byte snapshots of the local projects, including uncommitted and untracked saved results, screening decisions, plots, data, audit outputs, original source code, and tests. Their engines run in separate processes so incompatible modules and disease policies cannot collide. The combined interface is implemented outside these snapshots; no evidence files or statistical source modules were edited.

The import excludes credentials, Git metadata, Python/tool caches, runtime logs, temporary directories, and machine-specific ignore/attribute files. `evidence-manifest.json` lists every copied file, its size and SHA-256, and exclusion rules. No credentials are required to explore saved evidence. Snapshot text normalization is disabled in Git to retain exact bytes.

Some OneDrive-only PNGs could not be read directly. `evidence-import-recovery.json` records their recovery from existing local copies or Git blobs, accepted only when their byte size and computed OneDrive content fingerprint matched the metadata for the exact original file. It records SHA-256 for the recovered bytes. OneDrive's QuickXorHash is an integrity fingerprint, not a cryptographic proof of equality. No plots were regenerated and older nonmatching versions were rejected.

The metaUro worker rejects writes into either snapshot. Persistent verification/exclusion actions are disabled; temporary study selection and reanalysis remain available. Runtime logs and plotting-library caches live under ignored `runtime/`. Original refresh workflows are archived inside the snapshots and are not active GitHub workflows in this repository.

```bash
python scripts/verify_evidence.py
# Optional: compare against the original projects as well:
python scripts/verify_evidence.py --original-parent /path/containing/metaPee/and/MetaVir
```

## Validation

```bash
pip install pytest
pytest tests
node tests/settings.test.js static/analysis-settings.js
node tests/navigation.test.js
python scripts/verify_evidence.py
```

The source projects' historical tests are preserved under their own snapshots; run metaUro tests from the repository root. Runtime writes do not modify source evidence. The GitHub repository is private.
