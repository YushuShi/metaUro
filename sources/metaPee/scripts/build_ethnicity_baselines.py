#!/usr/bin/env python3
"""Expand exact published SEER baseline tables to the saved race definitions.

Works offline from this repository's frozen official source JSON, source hashes,
all-races table definitions, and existing app disease/sex metadata. It never
estimates subgroup risks or changes saved screening/extraction results.
"""
import argparse
import copy
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re


def load_json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value) if isinstance(value, str) else value


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def slug(value):
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def csv_bytes(rows, fields):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def build(repo):
    base = repo / "incidenceRate/published_seer"
    catalog = load_json(repo / "static/seer-baseline.json")
    original_catalog = copy.deepcopy(catalog)
    formats = load_json(base / "sources/var_formats.json")["VariableFormats"]
    source = catalog["source"]
    assert formats["stat_type"]["10"] == "Being Diagnosed with Cancer (2021-2023)"
    assert source["data_period"] == formats["year_range"]["8"]
    downloads = {item["file"]: item for item in load_json(base / "sources/DOWNLOADS_MANIFEST.json")}
    payloads = {}
    for name, metadata in downloads.items():
        path = base / "sources" / name
        data = path.read_bytes()
        assert len(data) == metadata["bytes"]
        assert hashlib.sha256(data).hexdigest() == metadata["sha256"], name
        payload = load_json(path)
        assert payload["info"]["key-order"] == ["stat_type", "sex", "race", "age_range", "risk_interval", "site"]
        assert payload["info"]["data-fields"] == ["risk", "risk_lower_ci", "risk_upper_ci"]
        payloads[name] = payload["data"]

    catalog["races"] = [{"id": slug(label), "label": label, "seer_code": int(code),
                         "published_definition_available": True}
                        for code, label in formats["race"].items()]
    sexes = {item["label"]: item for item in catalog["sexes"]}
    all_tables = read_csv(base / "TABLE_MANIFEST.csv")
    definitions = [row for row in all_tables if row["race_ethnicity"] == formats["race"]["1"]]
    assert definitions and len({(row["disease_id"], row["sex"]) for row in definitions}) == len(definitions)
    record_fields = list(read_csv(base / "SOURCE_VALUE_RECORDS.csv")[0])
    ages, years, cutoff = catalog["ages"], catalog["years"], catalog["max_end_age"]
    outputs, tables, records, app_records = {}, [], [], []
    for definition in definitions:
        sex = sexes[definition["sex"]]
        site = definition["source_site_code"]
        for race in catalog["races"]:
            filename = f"{definition['disease_id']}__{slug(sex['label'])}__{race['id']}__risk_percent.csv"
            matrix, count = [], 0
            for age in ages:
                row, previous = {"starting_age": age}, None
                for horizon in years:
                    risk = ""
                    if site and definition["status"] == "published_values_available" and age + horizon <= cutoff:
                        name = f"risk_comparison_site_{site}.json"
                        key = f"10_{sex['seer_code']}_{race['seer_code']}_{300 + age // 5}_{horizon}_{site}"
                        point = payloads[name].get(key)
                        if point is not None:
                            assert point["year_range"] == 8 and len(point["data_series"]) == 1
                            risk, low, high = point["data_series"][0]
                            risk = risk if risk is not None else ""
                            if risk != "":
                                value = Decimal(risk)
                                assert 0 <= value < 100
                                assert low is None or Decimal(low) <= value
                                assert high is None or Decimal(high) >= value
                                assert previous is None or previous <= value
                                previous = value
                                records.append({
                                    "repo": catalog["repository"], "file": filename, "disease": definition["disease"],
                                    "disease_id": definition["disease_id"], "sex": sex["label"], "race_ethnicity": race["label"],
                                    "starting_age": age, "follow_up_years": horizon, "risk_percent": risk,
                                    "ci_low": low, "ci_high": high, "source_id": source["id"], "source_file": name,
                                    "source_key": key, "source_sha256": downloads[name]["sha256"],
                                    "source_url": downloads[name]["source_url"], "submission": source["submission"],
                                    "data_period": source["data_period"], "release_date": source["release_date"],
                                    "method": source["method"],
                                })
                                app_records.append({"disease": definition["disease_id"], "sex": sex["id"],
                                                    "race": race["id"], "age": age, "years": horizon,
                                                    "risk_percent": risk, "source": source["id"], "source_key": key})
                                count += 1
                    row[str(horizon)] = risk
                matrix.append(row)
            outputs[base / filename] = csv_bytes(matrix, ["starting_age", *map(str, years)])
            table = dict(definition, file=filename, race_ethnicity=race["label"],
                         populated_cells=count, blank_cells=len(ages) * len(years) - count,
                         status="published_values_available" if count else "unsupported")
            tables.append(table)

    identity = lambda row: (row["disease"], row["sex"], row["race"], row["age"], row["years"])
    lookup = {identity(row): row for row in app_records}
    assert len(lookup) == len(app_records), "Duplicate exact baseline identity"
    # Race expansion cannot silently alter or drop any already published value.
    for record in original_catalog["records"]:
        assert lookup[identity(record)] == record, record
    catalog["records"] = app_records
    outputs[repo / "static/seer-baseline.json"] = json_bytes(catalog)
    outputs[base / "TABLE_MANIFEST.csv"] = csv_bytes(tables, list(all_tables[0]))
    outputs[base / "SOURCE_VALUE_RECORDS.csv"] = csv_bytes(records, record_fields)
    summary = load_json(base / "VALIDATION_RESULTS.json")
    summary.update(matrix_count=len(tables), partially_populated_matrices=sum(int(t["populated_cells"]) > 0 for t in tables),
                   entirely_blank_matrices=sum(int(t["populated_cells"]) == 0 for t in tables),
                   direct_published_values=len(records), matrix_cells_checked=len(tables) * len(ages) * len(years),
                   same_vintage_extraction_checks=len(records), same_vintage_extraction_discrepancies=0,
                   all_values_match_frozen_official_source=True, race_ethnicity_groups=len(catalog["races"]),
                   exact_stratum_selection=True, annual_estimates_created=False,
                   historical_comparison_scope="Preserved historical comparisons; newly exposed race groups are validated against the same frozen official source.")
    outputs[base / "VALIDATION_RESULTS.json"] = json_bytes(summary)
    return outputs, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true", help="Verify reproducible outputs without writing")
    args = parser.parse_args()
    outputs, summary = build(args.repo_root.resolve())
    if args.check:
        different = [str(path.relative_to(args.repo_root.resolve())) for path, value in outputs.items()
                     if not path.exists() or path.read_bytes() != value]
        if different:
            raise SystemExit("Rebuild required: " + ", ".join(different))
    else:
        for path, value in outputs.items():
            path.write_bytes(value)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
