# Monthly human review

The autonomous metaUro pipeline writes one CSV per source scope here:
`<start>_<end>_metaPee.csv` and `<start>_<end>_MetaVir.csv`.
The scope names identify the two local urinary datasets, not external services.

Each row identifies the PMID, disease, exposure, comparison, evidence source,
actual model names, decisions, effect estimates, confidence intervals, sample
counts, and quality assessments from both review tracks. The primary track is
GPT R2/R3; the independent validation track is Claude Opus 5 R2 and Gemini 3.7
Flash R3, using the same verified R1 candidates. Sequential rounds within one
track are labeled as stages, never as independent reviewers.

`disagreement_fields`, `human_review_reason`, the complete claim JSON columns,
and the Astra recommendation show why the case remains unresolved. Unknown
model names remain empty. Human review fields (`human_review_status`,
`human_decision`, `reviewer`, `comment`) are for the reviewer; editing the CSV
alone cannot change a pool. Cases requiring human adjudication remain blocked
until a separately validated evidence revision is applied.

The queue is written before the optional Astra adjudication call and copied
into the versioned release receipts. A failed workflow also preserves it in its
checkpoint artifact. An empty CSV still contains the complete column header.
No CSV is fabricated for a monthly search that has not run.

CSV row endings and line breaks inside cells use LF before hashing. Quoted
multiline evidence and reviewer comments retain their text. Before Git
publication, `automation.autonomous_monthly verify-index` checks that every
required release file has exactly the validated bytes in Git's staging area;
line-ending conversion or clean filters cannot silently invalidate a receipt.
