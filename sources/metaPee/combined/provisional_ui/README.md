# Saved combined UI view

All supplied human reviews are resolved. This view is rebuilt from the authoritative combined records and retains their human decisions and existing scientific and quality criteria. No temporary pipeline selections remain.

Both exposure hierarchy modes and disease-parent projections are saved under `subexposure_views`. The readiness manifest verifies the immutable model packets, canonical combined data, and human-decision checkpoint. Rebuild offline with `python scripts/build_provisional_review_ui.py --output <new-empty-audit-folder> --apply`.
