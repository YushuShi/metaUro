# Combined MetaVir results

This directory is the production evidence cache used by the MetaVir UI.
`Cached_results/` contains only ChatGPT evidence under the current pooling policy. These caches
are rebuilt from the selected ChatGPT lineage, evidence-backed Claude+Gemini
substitutions chosen by `claude-fable-5-1`, and any curator entries in
`_manual_overrides.json`.

The current policy requires total sample size, a ratio effect, and a usable CI
or exact same-estimate p-value; cases and exposure/model metadata are optional.
Unstated comparison directions default to high versus low or yes versus no and
are marked `direction_imputed`. Original model packets remain unchanged.
`_poolability_audit.json` records current poolability and remaining issues.

`_pending_review.json` lists unresolved fields. To resolve one, add an entry to
the `overrides` array in `_manual_overrides.json` with its `context_id`,
`discrepant_field`, and `selected_pipeline` (`CHATGPT`, `CLAUDE_GEMINI`,
`NO_CHANGE`, or `CUSTOM`). Then run:

```bash
python scripts/build_combined_results.py
python scripts/build_summary_plots.py
```

Do not edit the derived exposure cache files directly; the next rebuild will
replace them.

Every `CUSTOM` entry needs `custom_value` and `custom_reason`. A custom sample
size must be a positive integer and include `custom_evidence.sample_size` with
non-empty `excerpt` and `source` fields. A custom screening exclusion also
needs `custom_exclusion_code`. A custom estimate is an atomic bundle: provide
`effect_size`, `effect_type`, `ci_lower`, `ci_upper`, `p_value`,
`comparison_type`, `needs_inversion`, and `cases`, plus evidence for the effect,
comparison, and each supplied CI/p-value. If both estimate discrepancy rows are
listed for a context, add identical CUSTOM bundles for both so no value is mixed
with another pipeline's estimate or evidence.

Automatic pipeline winners must include a verbatim supporting excerpt from the
title, abstract, or MeSH. Compact/provider-filtered answers without that evidence
remain provisional ChatGPT values in `_pending_review.json`.
