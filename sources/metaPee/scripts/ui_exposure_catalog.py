"""Build the read-only UI exposure vocabulary without changing search aliases.

Screening owns direct contexts.  This module only exposes the collision-deduped
current/additional canonical union and its display aliases.  Hierarchy parents
are canonical UI entries; child memberships are applied after screening and
must never become parent search aliases here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


_RESERVED_ADDITIONAL_KEYS = {
    "version",
    "schema_version",
    "description",
    "hierarchy",
    "edges",
}


def _load_json(path: str | Path, default: Any) -> Any:
    source = Path(path)
    if not source.is_file():
        return default
    with source.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _normal_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _additional_entries(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        for wrapper in ("exposures", "entries", "additional_exposures"):
            if isinstance(raw.get(wrapper), (dict, list)):
                raw = raw[wrapper]
                break
    if isinstance(raw, list):
        entries: dict[str, Any] = {}
        for item in raw:
            if isinstance(item, str):
                name, entry = item.strip(), {}
            elif isinstance(item, dict):
                name = str(
                    item.get("exposure")
                    or item.get("name")
                    or item.get("canonical")
                    or item.get("canonical_name")
                    or ""
                ).strip()
                entry = item
            else:
                continue
            if name:
                entries[name] = entry
        return entries
    if not isinstance(raw, dict):
        return {}
    return {
        str(name).strip(): entry
        for name, entry in raw.items()
        if str(name).strip() and str(name) not in _RESERVED_ADDITIONAL_KEYS
    }


def _combined_additional_entries(
    paths: str | Path | Iterable[str | Path],
) -> dict[str, Any]:
    """Load additional catalogs in order and fail on canonical collisions."""
    catalog_paths = (
        [Path(paths)]
        if isinstance(paths, (str, Path))
        else [Path(path) for path in paths]
    )
    result: dict[str, Any] = {}
    canonical_by_key: dict[str, tuple[str, Path]] = {}
    for path in catalog_paths:
        for name, entry in _additional_entries(_load_json(path, {})).items():
            key = _normal_key(name)
            if key in canonical_by_key:
                prior_name, prior_path = canonical_by_key[key]
                raise ValueError(
                    "Additional exposure catalogs duplicate canonical exposure "
                    f"{name!r}: {prior_path} ({prior_name!r}) and {path}"
                )
            canonical_by_key[key] = (name, path)
            result[name] = entry
    return result


def _hierarchy_parent_names(raw: Any) -> list[str]:
    parents: list[str] = []
    if isinstance(raw, dict) and isinstance(raw.get("edges"), list):
        raw = raw["edges"]
    if isinstance(raw, dict):
        for values in raw.values():
            candidates = values if isinstance(values, list) else [values]
            parents.extend(str(value).strip() for value in candidates if str(value).strip())
    elif isinstance(raw, list):
        for edge in raw:
            if isinstance(edge, dict):
                parent = str(edge.get("parent") or "").strip()
            elif isinstance(edge, (list, tuple)) and len(edge) == 2:
                parent = str(edge[1]).strip()
            else:
                continue
            if parent:
                parents.append(parent)
    return parents


def _append_unique(
    output: list[str],
    seen: dict[str, str],
    values: Iterable[Any],
) -> None:
    for value in values:
        name = str(value or "").strip()
        key = _normal_key(name)
        if not key or key in seen:
            continue
        seen[key] = name
        output.append(name)


def build_ui_exposure_catalog(
    current_path: str | Path,
    additional_paths: str | Path | Iterable[str | Path],
    hierarchy_path: str | Path,
) -> list[str]:
    """Return current + additional + hierarchy-parent names exactly once."""
    current = _load_json(current_path, [])
    if not isinstance(current, list):
        raise ValueError("The current exposure catalog must be a JSON array")
    additional = _combined_additional_entries(additional_paths)
    parents = _hierarchy_parent_names(_load_json(hierarchy_path, {}))

    output: list[str] = []
    seen: dict[str, str] = {}
    _append_unique(output, seen, current)
    _append_unique(output, seen, additional)
    _append_unique(output, seen, sorted(parents, key=str.casefold))
    return output


def _alias_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _ui_alias_entry(entry: Any) -> dict[str, str]:
    if not isinstance(entry, dict):
        return {"core": _alias_text(entry), "downstream": ""}
    # Guarded abbreviations are intentionally not promoted to ordinary UI/search
    # aliases.  In particular, bare NAD/NADH terms stay out of broad matching.
    return {
        "core": _alias_text(entry.get("core") or entry.get("aliases")),
        "downstream": _alias_text(entry.get("downstream") or entry.get("related")),
        "excluded": _alias_text(entry.get("excluded")),
    }


def build_ui_synonym_catalog(
    synonyms_path: str | Path,
    current_path: str | Path,
    additional_paths: str | Path | Iterable[str | Path],
    hierarchy_path: str | Path,
) -> dict[str, Any]:
    """Return display aliases without merging additional terms into collisions."""
    current_synonyms = _load_json(synonyms_path, {})
    if not isinstance(current_synonyms, dict):
        raise ValueError("The current synonym catalog must be a JSON object")
    output = {}
    for name, entry in current_synonyms.items():
        normalized = dict(entry) if isinstance(entry, dict) else {
            "core": _alias_text(entry),
            "downstream": "",
        }
        for field in ("core", "downstream", "excluded"):
            if isinstance(normalized.get(field), (list, tuple, set)):
                normalized[field] = _alias_text(normalized[field])
        output[str(name)] = normalized
    existing = {_normal_key(name) for name in output}

    additional = _combined_additional_entries(additional_paths)
    for name, entry in additional.items():
        key = _normal_key(name)
        if not key or key in existing:
            continue
        output[name] = _ui_alias_entry(entry)
        existing.add(key)

    for name in build_ui_exposure_catalog(
        current_path,
        additional_paths,
        hierarchy_path,
    ):
        key = _normal_key(name)
        if key in existing:
            continue
        output[name] = {"core": name, "downstream": "", "excluded": ""}
        existing.add(key)
    return output
