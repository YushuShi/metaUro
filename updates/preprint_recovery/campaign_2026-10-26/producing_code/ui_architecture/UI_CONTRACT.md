# Preprint UI integration contract

`include_preprints` is an independent Boolean, false by default. It enables a
reviewed source-view override, not an additional independent report of the same
article. Models and Europe PMC run offline in the recovery pipeline; the UI
only reads released local data.

## Source and identity

The existing journal article PMID and journal publication date remain the
bibliographic/cohort-latest anchor. The PPR ID, DOI, version, posting date and
source SHA are separate preprint provenance. Every recovered row is explicitly
labelled as preprint-derived, not peer-reviewed full-text evidence. Verified
cohort names/date fields travel on the recovered native rows; the global cohort
registry/sidecar/helper must stay unchanged.

`data/preprint_recovery/active_manifest.json` holds the complete per-repository
view dataset. Onco uses its nested data directory. Uro uses
`updates/preprint_recovery/active_manifest.json`; immutable `sources/` is never
rewritten. `ui_manifest_contract.example.json` is an illustrative draft; the
producer supplies actual native statuses and canonical IDs.

Selectors must explicitly bind canonical source repo, disease, exposure,
outcome and subexposure mode. Source variant, subtype and native generation
may be additional exact fields. Canonicalization belongs to each native adapter;
the portable gate never guesses from a PMID, title, author or approximate text.
Explicit alias maps are allowed, such as the MetaCutis source-key alias.

`replace` requires original record IDs. `add` requires a resolved native selector
and new recovery record ID and may represent a context absent from saved UI
cache rows. IDs must be stable and unique per native effect/source version.
Per-recovered-row `preprint_original_record_ids` maps multi-row replacements;
otherwise a partial original-stratum selection must not expand to all recovered
strata. Conflicting version/replacement claims are rejected, not double-counted.

Settled/adjudicated final **include** decisions can enter a source view. Pending,
disputed and manual-review contexts remain recorded in the recovery dataset but
cannot override the live source. Final exclusion decisions likewise remain audit
evidence; native producers can provide accepted display-only/nonpoolable rows
under an included context, with the native engine retaining its eligibility rules.

Baseline bindings should use the exact saved native cache/config or published
source-generation manifests, not runtime scratch paths. This prevents a later
peer-reviewed/source revision from being silently overwritten by an old recovery.

## Portable APIs

- `compose_preprint_view(rows, repo_root, enabled=False, selector=None,
  selected_record_ids=None, allow_additions=False) -> (rows, audit)`.
- OFF returns the exact original `rows` object and does not read the manifest.
- Enabled composition never mutates input rows or the released source data.
- All-source analyze uses `allow_additions=True`; reanalysis uses only submitted
  recovered/original IDs and never revives an unselected context.
- Recovered rows are canonicalized from the trusted manifest, not client effect
  values. Repeated composition is idempotent. Original and recovered versions
  of one context never both enter analysis.
- `preprint_manifest_stamp(repo_root)` supplies the data-generation cache key.
- `preprint_variant_root(root, enabled)` produces a sibling opt-in namespace,
  composed with `cohort_latest`; OFF returns the exact original root/path.
- A local settings-status endpoint reports released/available reviewed context
  and row counts. Zero recoveries is an honest supported state, with no live calls.

A valid empty complete manifest supports the zero-recovery state. An enabled
request with a corrupt/stale dataset fails closed. Default OFF ignores an absent
or damaged optional recovery dataset. A correct UI reloads the complete base
view when the source setting changes, so stale recovered rows do not survive
turning the setting off.

## Placement and modes

Compose the source rows before quality/screening/poolability filters, case/N
projection and native meta-analysis. The native policy remains authoritative.
Then apply existing cohort-latest selection to the final eligible comparison.
Do not add recovered rows at a final pooling-only gate: excluded/empty contexts
can otherwise never recover and displayed counts can disagree with analysis.

Query links, form hidden fields, exports and cache identities retain
`include_preprints` together with disease/outcome, source variant, language,
subexposure mode and cohort-latest mode. Interactive plots may re-project
released rows under current settings; no runtime recovery/model calls occur.
Saved summary workbooks/forests require separately released opt-in assets.
A complete release with no applicable recovered context explicitly reuses unchanged base assets. Missing or stale applicable opt-in summary assets are unavailable, never default fallback.

Recommended paths are `Plot/preprints_included/…` and
`Plot/preprints_included/cohort_latest/…`, with native mode roots preserved.
Uro remains exclusively under `summary/{yes,no}/preprints_included/…`.
Separate summary seals bind the recovery manifest SHA, base-source generation,
mode/selector, code and cohort policy metadata. Keep asset seals separate from
the recovery manifest to avoid a circular hash dependency.

## Distinct native paths

- Common seven: `analysis_settings_backend.py` + `static/settings.js` +
  `_subexposure_toggle.html`; shared request settings/case projection adapter.
  Cached `/analyze` responses are reprojected in `install_app` after-request.
  `/reanalyze` quality filters can run before `perform_meta_analysis`, so source
  composition also needs the request boundary before those filters. Additions
  must work when the original cached `studies` list is empty.
- Femina additionally has ChatGPT/combined variants and subtype routes,
  `scripts/summary_analysis.py`, `build_subexposure_outputs.py` and
  `rebuild_summary_release.py`.
- Gut has verified nonmalignant/IBD policy views. New opt-in assets cannot use
  the immutable policy inventory; extend/compose the additive seal guard and
  bypass neither original default policy nor IBD latest integrity.
- Breath has cancer/nonmalignant views and smoking/ethnicity/gender summaries.
- Heart: `incidence_settings.normalize_settings`, native analyze/reanalyze,
  `cohort_pooling`, `cvd_summary` and native CVD dot projections/exports.
- Onco is the actual UI under `metaMeta/metaOnco` (no separate root metaMeta UI).
  `analysis.project_snapshot` consumes exact selected evidence, including source
  repo and exposure/cancer cell ownership. A recovery can create a previously
  absent pair; projection cannot iterate only old pooled result pairs. Both
  native row adapters and projection/cache keys need the recovery stamp.
- Neuro: `case_settings`, `/analyze` subexposure result builder and `/reanalyze`
  have early screening gates/no-pool branches. Compose before those branches;
  summary API and export builders have separate native paths/outcomes.
- Uro proxies two preserved engines via `source_worker.py`; inject root-local
  recovery access explicitly because a backend `__file__` is an assembled or
  runtime snapshot path. Include source ownership in every selector. Do not edit
  immutable snapshots or generate summaries outside the sole summary tree.
  `summary_outputs.ensure_current` currently auto-builds stale outputs: optional
  preprint requests must never trigger a default rebuild or default fallback.

## Dirty work and release constraints

The full pre-edit status and selected source hashes are saved in
`ui_repository_inventory.json`. Existing unrelated manual-review migrations,
monthly workflow edits, and dirty raw cache/plot files must stay untouched.
Femina and Uro releases target V2. Any new/changed data, summary datasets,
workbooks, figures, seals and producing code are one scientific release.

## Source marking and native receipts

Recovered evidence rows and source details use the `preprint-derived` class and
navy `#000080`, only from the authoritative `preprint_derived` provenance flag.
The visible article link says Preprint and opens the actual PPR source; journal
anchor links/dates are retained separately. Native interactive forest labels and
optional workbook exposure labels use navy only for recovered/selected sources.
Plain CSV exports append source/version/anchor fields when recovered rows exist;
CSV has no color representation. Ordinary peer-reviewed evidence keeps its style.

The additive producer `scripts/build_preprint_summaries.py` stages native caches,
calls the existing native summary engine, verifies source immutability and
atomically promotes only `preprints_included`. `preprint_build_receipt.json`
contains exact `native_pooling_cells` for combined/dietary questions, with final
native selected rows/IDs, evaluated IDs, source paths, pooled N, headline, row SHA
and recovery-manifest SHA. No heuristic eligibility or repeated analysis pass is
used. This receipt is the source for the derivative Onco view.

The portable gate rejects stale incoming recovered rows if their reviewed
context disappeared, the manifest is missing or the source selector changed.
Default OFF continues to return the original input object without reading data;
request adapters reject stale recovered submissions when that source is off.

## Article source eligibility

Every activated context must bind a sanitized per-repository article availability
proof by file SHA, exact PMID and canonical record SHA. The record must explicitly
allow preprint recovery and model dispatch, show a completed check across saved
tracks, and show no available PMC full text. Available, unknown, unbound and
unchecked articles fail closed. This is a local release-proof check, not a live
article lookup. Empty complete manifests need no source-eligibility record.

Final common-app summaries are built in a clean published-HEAD tree plus the
approved task code and frozen recovery data, then only optional outputs are
promoted. This keeps unrelated working-cache changes, including the preexisting
Gut vitamin-D cache change, out of published optional figures.
