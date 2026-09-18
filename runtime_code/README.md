These UI and request-calculation modules update the interactive case filter
without changing the imported evidence in `sources/` or its integrity manifest.
Each file starts from the matching source application's recorded revision and
adds the same minimum-case and current-baseline behavior as that application.
The source worker loads the Python module here first; the wrapper serves the
JavaScript here and the shared `static/case-counts.js` helper.

`manifest.json` records the original snapshot hashes and the updated code hashes.
Estimates are calculated per request from the selected baseline. Reported cases
and historical evidence caches remain unchanged.
