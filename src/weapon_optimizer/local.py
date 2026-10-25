"""Explicit, read-only enrichment from a selected local SPT installation."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .acquisition import read_json_file
from .data import Catalog, DataError


def load_chinese_locale(installation_root: str | Path, catalog: Catalog) -> tuple[dict[str, str], list[dict[str, str]]]:
    root = Path(installation_root).resolve()
    runtime = root / "SPT_Runtime"
    base = runtime / "SPT_Data" / "database" / "locales" / "global" / "ch.json"
    if not base.is_file():
        raise DataError(f"Chinese item locale not found: {base}")
    paths = [base, *sorted(
        path
        for pattern in ("*/db/CustomLocales/ch.json", "*/db/CustomLocales/ch.jsonc")
        for path in (runtime / "user" / "mods").glob(pattern)
    )]
    labels: dict[str, str] = {}
    mod_owners: dict[str, Path] = {}
    sources = []
    for index, path in enumerate(paths):
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise DataError(f"cannot read locale {path}: {exc}") from None
        entries = read_json_file(path)
        if not isinstance(entries, dict):
            raise DataError(f"locale must be an object: {path}")
        sources.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(payload).hexdigest()})
        for key, value in entries.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            tid, separator, field = key.partition(" ")
            if not separator or tid not in catalog.items or field not in {"Name", "ShortName"}:
                continue
            if not value.strip():
                continue
            if index and key in mod_owners and labels[key] != value:
                raise DataError(f"conflicting Chinese item locales for {key}: {mod_owners[key]} and {path}")
            labels[key] = value
            if index:
                mod_owners[key] = path
    return labels, sources


def enrich_catalog(catalog: Catalog, installation_root: str | Path) -> Catalog:
    """Keep runtime templates unchanged; add independently hashed local evidence."""
    from .acquisition import load_acquisition
    from .compatibility import load_compatibility

    labels, sources = load_chinese_locale(installation_root, catalog)
    acquisition = load_acquisition(installation_root, catalog)
    compatibility = load_compatibility(installation_root, catalog)
    source: dict[str, Any] = dict(catalog.source)
    source["local_context"] = {
        "kind": "offline-installation-evidence",
        "installation_root": str(Path(installation_root).resolve()),
        "chinese_locales": sources,
        "locale_precedence": "base, then mod item locales; differing mod overrides are rejected",
        "runtime_templates": "unchanged; acquisition and compatibility are separate evidence",
        "profile_state": "not read; source conditions are not current-player unlock guarantees",
    }
    return Catalog(
        catalog.items, catalog.locale, source,
        locale_ch=labels, acquisition=acquisition, compatibility=compatibility,
    )
