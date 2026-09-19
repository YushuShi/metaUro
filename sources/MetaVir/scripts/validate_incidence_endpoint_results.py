#!/usr/bin/env python3
"""Validate exact diagnosis-endpoint replay without broader row changes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import incidence_endpoint_policy as endpoint_policy  # noqa: E402
from scripts.replay_incidence_endpoint_decisions import CACHE_ROOTS  # noqa: E402


RETAINED_SOURCE_ROWS = {
    "042075784190b9f1125f00baaf055d59ef792cadf18e841d7d716df373e42a47",
    "cb1570761e974f5b4786190a42c258fbf6fc4780ba9f6a08677728a0a32f32a3",
    "7cdb9e60edffb190242bdeab1295063094c48447cf509eedb5fab14c49e62bd2",
    "a6f05c54153d9125d73b5db0809dd1b0c81dff5b487849be79828f9fce10535a",
    "529ca1f2e76aa18d51f0b2f064312ce96f971714f927ffe5a015dd249a247601",
    "b965b466fe8b6476b5bc9928ff834c1b7ae7e2a87023bf567b84d06a124a6f75",
    "cbf316d5711a0f734e14bdf8d9530856d8ce9c58ac72fc0f545bafbe31451cf7",
    "766cf5b7b71322938dd5ff9f12d9ac213a5799b9861f34143068bced1b317fe6",
    "e3e8aefe037dd54cb7e1ccd59f2ed21767f337a338f5a5950d2f494973194c6d",
}
# This high-grade diagnosis row was added after metaUro's embedded MetaVir
# snapshot was sealed.  Require it whenever present, but do not pretend an
# endpoint replay can restore evidence that the destination never contained.
OPTIONAL_OLDER_SNAPSHOT_CONTROLS = {
    "cbf316d5711a0f734e14bdf8d9530856d8ce9c58ac72fc0f545bafbe31451cf7",
}


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate() -> dict[str, Any]:
    checkpoint = endpoint_policy.load_checkpoint()
    expected = {endpoint_policy.record_id(record) for record in checkpoint["records"]}
    audit = read(ROOT / "data/incidence_endpoint_review/replay_audit.json")
    assert audit["expected_record_count"] == len(expected)
    assert audit["matched_record_count"] == len(expected)
    assert audit["unexpected_removed_rows"] == 0
    assert audit["added_rows"] == 0
    assert audit["retained_row_mutations"] == 0

    records_by_root: dict[str, list[str]] = {}
    retained: set[str] = set()
    rows_checked = 0
    all_core_pairs = 0
    for root_relative, _namespace in CACHE_ROOTS:
        records: set[str] = set()
        for all_path in sorted((ROOT / root_relative).glob("*/*_incidence_true_all.json")):
            payload = read(all_path)
            core = all_path.with_name(all_path.name.replace("_all.json", "_core.json"))
            if core.is_file():
                assert payload.get("studies") == read(core).get("studies"), all_path
                all_core_pairs += 1
            for row in payload.get("studies") or []:
                rows_checked += 1
                assert endpoint_policy.matching_record(row, checkpoint) is None, all_path
                source_id = str((row.get("disease_scope_review") or {}).get("source_row_id") or "")
                if source_id in RETAINED_SOURCE_ROWS:
                    retained.add(source_id)
            records.update(
                item["record_id"]
                for item in (payload.get("incidence_endpoint_exclusions") or {}).get("records", [])
            )
        records_by_root[root_relative] = sorted(records)
    assert set(records_by_root["Cached_results"]) | set(records_by_root["combined"]) == expected
    required_controls = RETAINED_SOURCE_ROWS - OPTIONAL_OLDER_SNAPSHOT_CONTROLS
    assert required_controls <= retained, sorted(required_controls - retained)
    assert retained <= RETAINED_SOURCE_ROWS, sorted(retained - RETAINED_SOURCE_ROWS)

    workbook_count = 0
    pdf_count = 0
    for plot_relative, mode in (
        ("Plot", None),
        ("Plot/subexposures/yes", "yes"),
        ("Plot/subexposures/no", "no"),
    ):
        plot_root = ROOT / plot_relative
        manifest = read(plot_root / "summary_manifest.json")
        if mode is not None:
            assert manifest.get("include_subexposures") == mode
        for scope in manifest["scopes"].values():
            for item in scope["workbooks"].values():
                if item.get("available"):
                    assert (plot_root / item["filename"]).is_file()
                    workbook_count += 1
            for item in scope["plots"].values():
                for relative in item.get("locales", {}).values():
                    assert (plot_root / relative).is_file()
                    pdf_count += 1

    report = {
        "passed": True,
        "policy_version": endpoint_policy.VERSION,
        "expected_record_count": len(expected),
        "removed_occurrences": audit["removed_occurrences"],
        "unexpected_removed_rows": 0,
        "added_rows": 0,
        "retained_row_mutations": 0,
        "retained_diagnosis_controls": len(retained),
        "diagnosis_controls_not_in_snapshot": sorted(RETAINED_SOURCE_ROWS - retained),
        "rows_checked": rows_checked,
        "all_core_pairs_checked": all_core_pairs,
        "summary_workbooks_checked": workbook_count,
        "localized_summary_pdfs_checked": pdf_count,
        "records_by_root": records_by_root,
    }
    (ROOT / "data/incidence_endpoint_review/replay_validation.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    validate()
