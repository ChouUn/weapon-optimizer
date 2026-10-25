"""Evidence-backed content consistency constraints, separate from raw templates.

The client checks exact template IDs. These additional rules repair clone-filter
propagation and strictly cosmetic variant omissions in modded content; they do
not assert that the client itself infers clone or prefab equivalence.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
from pathlib import Path
import re

import simplejson as json

from .data import Catalog, DataError, _digest


_TGC_ASSEMBLY = "SPT_Runtime/user/mods/TGC-NG/TGC-NG.dll"
_TGC_CONFIG = "SPT_Runtime/user/mods/TGC-NG/db/CustomItems/modTGC_items.json"
_TGC_SHA256 = "d0ba4763bc13445684f30a108b8452b78578ebcbeedc4d39f5e85f70c4e8d1fa"
_COLOR_SUFFIX = re.compile(r"_(?:black|blk|white|wht|red|yellow|ylw|green|grn|blue|blu|tan|fde|olive|od|grey|gray|gry)$")
_COSMETIC_KEYS = {"name", "shortname", "description", "backgroundcolor", "conflictingitems", "prefab"}


def _color_groups(catalog: Catalog) -> tuple[tuple[str, ...], ...]:
    groups: dict[tuple, list[tuple[str, bool]]] = defaultdict(list)
    for tid, item in catalog.items.items():
        if item.get("_type") != "Item":
            continue
        props = catalog.props(tid)
        prefab = props.get("Prefab")
        if not isinstance(prefab, dict) or not isinstance(prefab.get("path"), str):
            continue
        path = prefab["path"].replace("\\", "/")
        if not path.endswith(".bundle"):
            continue
        stem = path[:-7]
        family = _COLOR_SUFFIX.sub("", stem)
        if not family:
            continue
        # Keep every game property and nested slot identity. Similar names,
        # clone parents, or matching stats alone never establish equivalence.
        game_props = {key: value for key, value in props.items() if key.casefold() not in _COSMETIC_KEYS}
        prefab_props = {key: value for key, value in prefab.items() if key != "path"}
        signature = json.dumps([item.get("_parent"), game_props, prefab_props], sort_keys=False, separators=(",", ":"), use_decimal=True)
        groups[(family, signature)].append((tid, family == stem))
    return tuple(sorted(
        tuple(sorted(tid for tid, _ in members))
        for members in groups.values()
        if len(members) > 1 and any(is_base for _, is_base in members) and any(not is_base for _, is_base in members)
    ))


def load_compatibility(installation_root: str | Path, catalog: Catalog) -> dict:
    """Collect verified clone propagation evidence without modifying templates."""
    root = Path(installation_root).resolve()
    assembly_path = root / _TGC_ASSEMBLY
    config_path = root / _TGC_CONFIG
    sources = []
    clone_rules = []
    if assembly_path.is_file() or config_path.is_file():
        if not assembly_path.is_file() or not config_path.is_file():
            raise DataError("TGC compatibility evidence requires both its assembly and item configuration")
        try:
            assembly = assembly_path.read_bytes()
            payload = config_path.read_bytes()
        except OSError as exc:
            raise DataError(f"cannot read clone compatibility evidence: {exc}") from None
        assembly_digest = hashlib.sha256(assembly).hexdigest()
        if assembly_digest != _TGC_SHA256:
            raise DataError("unverified TGC CopyToFilter assembly; re-inspect its clone conflict propagation before enrichment")
        try:
            entries = json.loads(payload.decode("utf-8-sig"), use_decimal=True)
        except (ValueError, UnicodeError) as exc:
            raise DataError(f"invalid TGC compatibility configuration: {exc}") from None
        if not isinstance(entries, dict):
            raise DataError("TGC item configuration must be an object")
        aliases = []
        for clone_id, config in entries.items():
            if not isinstance(config, dict):
                raise DataError(f"invalid TGC item configuration: {clone_id}")
            source_id = config.get("itemTplToClone")
            if not isinstance(source_id, str):
                raise DataError(f"TGC clone has no itemTplToClone: {clone_id}")
            if clone_id in catalog.items:
                if source_id not in catalog.items:
                    raise DataError(f"TGC clone {clone_id} references missing source template {source_id}")
                aliases.append({"source_id": source_id, "clone_id": clone_id})
        sources = [
            {"path": _TGC_ASSEMBLY, "sha256": assembly_digest, "method": "TGC.TGC::CopyToFilter", "il": "0019-002b owner exclusion; 00fc-0120 incoming conflict propagation"},
            {"path": _TGC_CONFIG, "sha256": hashlib.sha256(payload).hexdigest()},
        ]
        clone_rules.append({
            "kind": "incoming-clone-conflicts",
            "consumer": "TGC.TGC::CopyToFilter",
            "aliases": sorted(aliases, key=lambda row: row["clone_id"]),
            "excluded_owners": sorted(tid for tid in entries if tid in catalog.items),
            "source_paths": [_TGC_ASSEMBLY, _TGC_CONFIG],
        })
    return {
        "schema_version": 1,
        "catalog_sha256": _digest(catalog.items),
        "policy": "verified incoming clone propagation and strict cosmetic-variant conflict union",
        "sources": sources,
        "clone_rules": clone_rules,
        "color_groups": [list(group) for group in _color_groups(catalog)],
        "color_rule": "same parent and full prefab family with an unsuffixed base; all noncosmetic properties and other prefab fields identical",
    }


class Compatibility:
    """One effective conflict relation shared by the solver and evaluator."""

    def __init__(self, catalog: Catalog):
        metadata = catalog.compatibility
        if metadata is not None and (not isinstance(metadata, dict) or metadata.get("schema_version") != 1):
            raise DataError("unsupported compatibility evidence schema")
        if metadata is not None and metadata.get("catalog_sha256") != _digest(catalog.items):
            raise DataError("compatibility evidence does not describe this items snapshot")
        colors = _color_groups(catalog)
        equivalents = {tid: group for group in colors for tid in group}
        rules = metadata.get("clone_rules", []) if metadata is not None else []
        if not isinstance(rules, list):
            raise DataError("compatibility clone_rules must be a list")
        prepared_rules = []
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("kind") != "incoming-clone-conflicts":
                raise DataError("unsupported clone compatibility rule")
            aliases = rule.get("aliases")
            excluded = rule.get("excluded_owners")
            if not isinstance(aliases, list) or not isinstance(excluded, list) or any(not isinstance(tid, str) for tid in excluded):
                raise DataError("invalid clone compatibility aliases or excluded owners")
            edges: dict[str, set[str]] = defaultdict(set)
            for alias in aliases:
                if not isinstance(alias, dict):
                    raise DataError("clone compatibility alias must be an object")
                source, clone = alias.get("source_id"), alias.get("clone_id")
                if not isinstance(source, str) or not isinstance(clone, str) or source not in catalog.items or clone not in catalog.items:
                    raise DataError("clone compatibility alias references an unknown template")
                edges[source].add(clone)
            prepared_rules.append((edges, frozenset(excluded)))
        self._targets: dict[str, frozenset[str]] = {}
        for tid in catalog.items:
            # Color equivalence unions outgoing conflict declarations, but does
            # not transfer stats or turn the two variants into a conflict pair.
            owners = equivalents.get(tid, (tid,))
            targets = set()
            for owner in owners:
                pending = list(catalog.conflicts(owner))
                reached = set()
                while pending:
                    target = pending.pop()
                    if target in reached:
                        continue
                    reached.add(target)
                    pending.extend(equivalents.get(target, ()))
                    for edges, excluded in prepared_rules:
                        if owner not in excluded:
                            pending.extend(edges.get(target, ()))
                targets.update(reached)
            self._targets[tid] = frozenset(targets)

    def conflicts(self, template_id: str) -> frozenset[str]:
        try:
            return self._targets[template_id]
        except KeyError:
            raise DataError(f"unknown compatibility template: {template_id}") from None
