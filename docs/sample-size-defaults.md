# Precomputed sample size plots

The default disease catalog and the four panels for each disease are saved in
`data/sample_size_defaults.json`. They come from the same API used for custom
calculations. The browser draws the interactive plots from the saved points.
Changing only the maximum sample size filters these points without re-pooling
studies. Changes to planning or evidence settings use the live calculation.

Generate the snapshot from the hydrated release checkout before serving traffic:

    python sample_size_defaults.py --profile uro

The deployment build runs this automatically. A Git checkout matches the
snapshot to its tracked release tree, excluding the snapshot itself. If the
release changes, the build regenerates it. When no suitable snapshot is
available, the API uses the live handler. The no-Git container build generates a
snapshot directly from the evidence copied into the image.

To publish a refreshed snapshot in Git, stage and commit the producing code and
release inputs first, generate the snapshot, and add it to that same release
commit (amend before pushing). Don't generate it from a checkout containing
unreleased edits or evidence.
