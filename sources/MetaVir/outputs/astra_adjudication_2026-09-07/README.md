# Abstract-only Astra application — MetaVir

Validated gpt-6-astra decisions were applied only to combined results. The original ChatGPT and Claude+Gemini screening tracks remain unchanged. Existing human/manual decisions retain priority. Automated Fable decisions are preserved where Astra cannot resolve the disagreement.

- Original discrepancy rows: 499
- Applied contexts: 257
- Protected human/manual contexts: 4, all unchanged
- Selected source bundles still missing quantitative information: 159, all eligible but not poolable
- Changes to extraction, JBI, or status in unapplied contexts: 0

Each original review ID has an application disposition in `combined/astra_adjudication/review_dispositions.json`. Follow-up covers uncertain judgments, application holds, and remaining quantitative gaps. Full details and the source excerpt are kept in the checkpoint; the consolidated review CSV is produced in the workspace screening-status report directory.

`combined/astra_title_abstract_checkpoint.json` is replayed by future combined builds. A replay check reproduced all 35,545 installed context records. Original combined records are backed up under this directory's `before_application/` folder. The source models' results are never overwritten by this application.

Only high-confidence decisions with independently validated title/abstract quotes were eligible for application. Blinded A/B labels are mapped using the collector's explicit selected pipeline. Incomplete source bundles may be selected but remain unpooled; custom NEITHER corrections require coherent sample size, effect, CI, and effect type. Uncertain direction or an inconsistent source bundle remains for review.

No model calls or original-paper full-text access occur in these integration scripts. Existing scientific and JBI pooling gates are retained.
