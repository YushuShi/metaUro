# Saved temporary combined UI view

Pending human-review pairs use the complete eligible pipeline when the other is incomplete or excluded; when both are complete, ChatGPT is selected. The higher saved JBI assessment is used while pending. Independent scientific and cohort restrictions still govern pooling. These choices do not resolve reviews.

`effective_results.json` retains adjudicated records and adds explicit provisional candidates; `review_audit.json` records selections and actual UI outcomes. Both hierarchy modes are saved under `subexposure_views`. Original tracks and authoritative human/Astra combined results are unchanged. The app refuses stale views when a protected source changes. Rebuild offline with `python scripts/build_provisional_review_ui.py --output <new-empty-audit-folder> --apply`. Review pending metadata is retained in files without adding visible UI badges.
