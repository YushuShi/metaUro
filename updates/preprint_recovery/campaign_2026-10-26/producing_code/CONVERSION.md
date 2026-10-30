# Native opt-in preprint source conversion

This workflow never rewrites default caches, global cohort registries, record
sidecars, model history, or immutable Uro sources. No models run in conversion.

Set `PREPRINT_WORKSPACE_ROOT` and `PREPRINT_WORK_DIR` when running archived code
outside the original workspace. Keep the producing scripts together. Native
numeric policy modules and scientific input files are separately SHA-bound in
each emitted source context.

The release driver supplies a frozen `active_result_index.json`, schema 1, with
`jobs` and `authoritative_context_jobs`. Every job entry binds `job_id`,
`repository`, exact `context_ids`, `result_path` (or workspace-relative
`result_file`), `job_file`, `result_sha256`, and `inputs_sha256`. The authority map
uses `repository|context_id` as keys and selected job IDs as values. Historical
attempts are preserved; only this explicit selection can activate a release.

```sh
python -B native_overlay_converter.py \
  --active-result-index /path/to/frozen/active_result_index.json \
  --output /path/to/new/native-staging
```

Each native application receives a complete `active_manifest.json` and adjacent
`article_availability_gate.json`, including an honest empty manifest when no
recovery can activate. The output root also contains `conversion_receipt.json`
with per-context decisions/holds and unchanged input-file checks. Copy accepted
native manifests/proofs only through the separately verified release process,
to `data/preprint_recovery/` in the source app. The converter does not publish.

Heart resolves its selected source generation through the saved active pointer,
coverage and output-asset receipt hashes, and uses the native canonical-exposure
function without importing the application. Both hierarchy modes include exact
core/all cache aliases. Existing exact child-disease records can be replaced in
their parent display only: all parent pooling flags are explicitly false, and a
child absent from a parent cache is never added. Direct child views retain their
own native eligibility. Source-generation changes require revalidation.

Activation requires a source-bound article-level PMC gate, exact preprint
identity/version, successful native scientific review, a complete selected
reviewer bundle, high-confidence Astra inclusion, and an explicitly empty
final unresolved-discrepancy list. Missing optional statistics remain recorded;
the converter cannot reinterpret an adjudicator's unresolved list. A settled
native nonpoolable article is retained for display with native pooling flags
off. Supplemental contrasts are retained as display-only, rather than counted
as independent populations. The native engine makes final pooling decisions.

For isolated positive QA only, `--qa-preview --only metaGut:38321187` may scan
saved attempts. The manifest is marked QA-only and cannot establish campaign
completion. Stage the adjacent availability proof too; other cache/metadata
bindings continue to point to unchanged real native inputs.

Uro projection reads accepted MetaVir/metaPee manifests and verifies the same
saved cache bytes in its installed source generation:

```sh
python -B uro_overlay_projection.py \
  --native-manifest /native-staging/MetaVir/active_manifest.json \
  --native-manifest /native-staging/metaPee/active_manifest.json \
  --output /path/to/new/uro-staging
```

Its contexts bind durable `sources/` or `updates/` paths resolved by the native
monthly source resolver, plus the installed update index. A differing or absent
source generation remains audit-only unless a separately approved exact-row
lineage exception proves the complete original target records identical,
including source IDs/context/PMID, no human protection, both cache hashes, and
an actual native Uro disposition check. No generic cache-generation bypass is
permitted. The Uro manifest and proof are destined
for `updates/preprint_recovery/`; `sources/` is never altered. Recovered source
rows, original IDs, publication-date precision, cohort rounds, and native
poolability flags are preserved.

Use `--create-lineage-proof --output /path/to/lineage.json` to audit original
records, then `--reviewed-lineage-proof /path/to/lineage.json` for the explicitly
authorized replacement. `--selector-resolution-proof` accepts captured actual
broker selectors only for exact cache-bound case-only aliases. Bind passing
native runtime checks with `--native-admission-policy` and
`--native-tested-manifest`; scientific row fields must equal the tested bundle.

Onco projection additionally requires the native summary producer's exact final
selection receipts and the explicit registered-taxonomy mapping:

```sh
python -B onco_overlay_projection.py \
  --native-manifest /native-staging/metaGut/active_manifest.json \
  --native-receipt /path/to/preprint_build_receipt.json \
  --mapping /path/to/onco_projection_mapping.json \
  --output /path/to/new/onco-staging
```

Repeat manifest/receipt options for other native sources. Every recovered row
must have actually entered the source engine's final eligible comparison. An
existing Onco metric must bind the same published source-cache generation. A
new metric also carries any ordinary peer-reviewed rows selected alongside the
recovered row in `projection_baseline_cells`, with `preprint_derived=false`.
These ordinary rows cannot be dropped or labelled as preprint evidence. Source
generation or taxonomy ambiguity remains a separately reviewable lineage hold.
The single explicitly authorized MetaVir red-meat/prostate append exception
also requires the exact Uro row-lineage and actual native-admission proofs via
`--uro-lineage-proof` and `--uro-native-admission`. The published target PMID
must be absent from that metric; ordinary published estimates remain unchanged.
Native selection receipts must have `keep_latest_cohort=false` so the Onco
setting can perform cohort selection dynamically.

```sh
python -B -m unittest discover -s /path/to/implementation -p 'test_*overlay*.py'
```

The PMC article gate, source collection receipts, raw model responses, failed
attempts, cost ledger, and manual-review exports are separate durable evidence.
They must survive release even when a context cannot activate. Preprints remain
off by default. Journal publication dates anchor cohort ordering; preprint
posting dates are separate provenance.
