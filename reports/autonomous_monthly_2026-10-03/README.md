# Standalone monthly urinary pipeline validation

This is a code release. No new PubMed search, paid model request, scientific
result, or completed monthly watermark was published during validation.

The full application suite passed 187 tests; the recurring release safeguard
suite passed 43 tests. Browser checks passed 12 settings and 10 navigation
cases. Both vendored native NCBI suites passed 17 tests each, covering
429/public fallback, invalid-key separation, shared cooldown, Retry-After,
and retryable backend errors. These run in the standalone workflow. Published `sources/`, `updates/`, `summary/` and `evidence-manifest.json`
remain identical to the prior commit.

Two isolated, writable scope copies loaded their native engines and independent
review drivers. An empty current-window R1 packet completed Claude/Gemini
validation without any request. Synthetic display-only additions survived
replay with exactly the same output inventory. Synthetic poolable additions
recalculated both scopes with the previous baseline risk preserved. All 410
historical study dictionaries in 16 affected saved caches remained unchanged.
The synthetic PMID values are fixtures and occur only in temporary copies;
no fixture cache, workbook or plot is a release artifact.

`scope_smoke.py` and `pool_smoke.py` preserve the executed offline harnesses.
Create fresh scratch copies with `automation.autonomous_monthly.materialize`,
set `METAURO_REPO_ROOT` and `PYTHONPATH` to the metaUro checkout, then run each
script with the scratch scope copy as its working directory. The pool harness
uses a separate synthetic PMID after the display-only harness. These scripts
write only to materialized scratch copies and do not make network requests.

The initial full suite exposed old failures from the September 30 reviewed
additions. The prior-HEAD checks reproduced the obsolete trend-CI and live
count/heterogeneity assertions. Current checks use declared reviewed additions
and saved diagnostic values. The PRISMA audit now verifies the targeted-review
registry and labels that separate route without fabricating R1 decisions.
The four diagram stages already existed in the prior HEAD template.

Recovery tests cover missing/older release cutoffs, tampered artifacts,
metadata-write rollback, incomplete completed checkpoints, lossless projection
replay, preservation of parent-scope winners, and endpoint policy holds. The
root human-review export adds downstream holds without rewriting the native
review CSV or application gate. New aggressive-prostate estimates require a
validated diagnosis-endpoint and endpoint-specific count review before pooling.

The runtime manifest pins vendored code and catalog bytes. The root workflow
uses one checkout and never calls the legacy source-import pipeline. The first
paid monthly run is coordinated separately after this code release.
