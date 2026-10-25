"""Offline, evidence-backed acquisition paths for weapon templates and parts.

A blacklist closes a particular generator, not all possible ways to obtain an
item. Conditional sources describe unlock requirements without inspecting a
profile. No HTTP, mod execution, game mutation, or inventory access occurs here.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

import simplejson as json

from .data import Catalog, DataError, WEAPON_CATEGORY, _digest

ACQUISITION_SCHEMA_VERSION = 1
# RagfairAssortGenerator.RagfairItemInvalidBaseTypes plus ItemHelper defaults,
# inspected in SPTarkov.Server.Core 4.1. These are category rules, not exceptions
# for individual optimization candidates.
_INVALID_DYNAMIC_BASES = (
    "566965d44bdc2d814c8b4571", "5448bf274bdc2dfc2f8b456a",
    "566abbb64bdc2d144c8b457d", "6050cac987d3f925bf016837",
    "55d720f24bdc2d88028b456d", "567583764bdc2d98058b456e",
    "557596e64bdc2dc2118b4571", "65649eb40bf0ed77b8044453",
)
# WTTServerCommonLib.Models.TraderIds.TraderMap, inspected in its installed DLL.
# Nicknames are not this protocol: Ref's database nickname is "Arena".
_WTT_TRADER_IDS = {
    "prapor": "54cb50c76803fa8b248b4571", "therapist": "54cb57776803fa99248b456e",
    "fence": "579dc571d53a0658a154fbec", "skier": "58330581ace78e27b8b10cee",
    "peacekeeper": "5935c25fb3acc3127c3d8cd9", "mechanic": "5a7c2eca46aef81a7ca2145d",
    "ragman": "5ac3b934156ae10c4430e83c", "jaeger": "5c0647fdd443bc2504c2d371",
    "ref": "6617beeaa9cfa777ca915b7c", "badger": "bd3a8b28356d9c6509966546",
}
# ItemTplResolver resolves these installed inline-barter symbols via ItemTpl.
_CURRENCY_IDS = {
    "money_roubles": "5449016a4bdc2d6f028b456f", "money_dollars": "5696686a4bdc2da3298b456a",
    "money_euros": "569668774bdc2da2298b4568", "money_gp_coin": "5d235b4d86f7742e017bc88a",
}


def _parse_json(raw: bytes, path: Path) -> Any:
    """Remove JSONC syntax lexically, never treating string contents as syntax."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise DataError(f"cannot decode JSON {path}: {exc}") from None
    chars = list(text)
    quoted = False
    escaped = False
    i = 0
    while i < len(chars):
        char = chars[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            i += 1
            continue
        if char == '"':
            quoted = True
            i += 1
            continue
        if char == "/" and i + 1 < len(chars) and chars[i + 1] == "/":
            while i < len(chars) and chars[i] not in "\r\n":
                chars[i] = " "
                i += 1
            continue
        if char == "/" and i + 1 < len(chars) and chars[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                raise DataError(f"unterminated JSONC comment in {path}")
            for j in range(i, end + 2):
                if chars[j] not in "\r\n":
                    chars[j] = " "
            i = end + 2
            continue
        i += 1
    quoted = False
    escaped = False
    for i, char in enumerate(chars):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == ",":
            j = i + 1
            while j < len(chars) and chars[j].isspace():
                j += 1
            if j < len(chars) and chars[j] in "]}":
                chars[i] = " "
    try:
        return json.loads("".join(chars), use_decimal=True, allow_nan=False)
    except (ValueError, json.JSONDecodeError) as exc:
        raise DataError(f"invalid JSON/JSONC in {path}: {exc}") from None


def read_json_file(path: str | Path) -> Any:
    """Read JSON or JSONC, preserving Decimal numbers and object order."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DataError(f"cannot read acquisition evidence {path}: {exc}") from None
    return _parse_json(raw, path)


def _value(mapping: dict, name: str, default: Any = None) -> Any:
    """Read the known, explicitly named mod configuration fields only."""
    result = default
    for key, value in mapping.items():
        if isinstance(key, str) and key.casefold() == name.casefold():
            result = value
    return result


def _positive(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return value > 0
    except TypeError:
        return False


def _valid_static_price(value: Any) -> bool:
    # ItemHelper.GetStaticItemPrice treats handbook prices below one as zero.
    return _positive(value) and value >= 1


def _barter_options(catalog: Catalog, alternatives: Any, diagnostics: list, ref: dict) -> list:
    if not isinstance(alternatives, list):
        return []
    result = []
    for index, option in enumerate(alternatives):
        normalized = []
        if isinstance(option, list) and option:
            for requirement in option:
                if not isinstance(requirement, dict) or not isinstance(requirement.get("_tpl"), str) or not _positive(requirement.get("count")):
                    break
                original = requirement["_tpl"]
                tid = _CURRENCY_IDS.get(original.casefold(), original)
                if tid not in catalog.items or catalog.items[tid].get("_type") != "Item":
                    break
                normalized.append({**requirement, "_tpl": tid})
            else:
                result.append(normalized)
                continue
        diagnostics.append({"kind": "unproven_barter_alternative", "alternative": index, "evidence": ref})
    return result


class Availability:
    """Replay captured acquisition evidence without consulting local files."""

    def __init__(self, catalog: Catalog, evidence: dict | None) -> None:
        if not isinstance(evidence, dict):
            raise DataError("acquisition evidence is missing; capture local evidence or explicitly select template research mode")
        if evidence.get("schema_version") != ACQUISITION_SCHEMA_VERSION:
            raise DataError("unsupported acquisition evidence schema")
        if evidence.get("catalog_sha256") != _digest(catalog.items):
            raise DataError("acquisition evidence does not describe this items snapshot")
        records = evidence.get("items")
        if not isinstance(records, dict) or not isinstance(evidence.get("source_files"), list):
            raise DataError("acquisition evidence is incomplete")
        self.catalog = catalog
        self.evidence = evidence
        self._records = records
        allowed = set()
        for tid, item in catalog.items.items():
            if item.get("_type") != "Item":
                continue
            record = records.get(tid)
            if not isinstance(record, dict) or record.get("status") not in ("available", "conditional", "unproven"):
                raise DataError(f"acquisition evidence is missing a valid status for {tid}")
            if not isinstance(record.get("sources"), list) or not isinstance(record.get("blocked_channels"), list):
                raise DataError(f"acquisition evidence has invalid source details for {tid}")
            if record["status"] != "unproven":
                if not record["sources"]:
                    raise DataError(f"acquisition evidence permits {tid} without a source")
                allowed.add(tid)
        self.allowed_ids = frozenset(allowed)

    def acquisition_status(self, tid: str) -> dict[str, Any]:
        if tid not in self.catalog.items:
            raise DataError(f"unknown template ID: {tid}")
        return self._records.get(tid, {
            "status": "unproven", "sources": [], "blocked_channels": [],
            "conditions": [], "reason": "not_an_item_template",
        })


class _EvidenceReader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.allowed_roots = (
            root / "SPT_Runtime" / "SPT_Data" / "configs",
            root / "SPT_Runtime" / "SPT_Data" / "database",
            root / "SPT_Runtime" / "user" / "mods",
        )
        self.files: dict[str, dict] = {}

    def read(self, path: Path) -> Any:
        resolved = path.resolve()
        if not any(resolved.is_relative_to(allowed) for allowed in self.allowed_roots):
            raise DataError(f"acquisition evidence path escapes the permitted local data directories: {path}")
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            raise DataError(f"cannot read acquisition evidence {path}: {exc}") from None
        relative = path.relative_to(self.root).as_posix()
        self.files[relative] = {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}
        return _parse_json(raw, path)

    def ref(self, path: Path, pointer: str = "") -> dict:
        return {"path": path.relative_to(self.root).as_posix(), "pointer": pointer}


def _json_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted((path for path in directory.rglob("*")
                   if path.is_file() and path.suffix.casefold() in (".json", ".jsonc")),
                  key=lambda path: path.as_posix().casefold())


def _record_source(records: dict, tid: str, source: dict) -> None:
    if tid in records and source not in records[tid]["sources"]:
        records[tid]["sources"].append(source)


def _detachable(catalog: Catalog, tid: str) -> bool:
    props = catalog.props(tid)
    return not props.get("IsUnremovable", False) and not props.get("IsLockedafterEquip", False)


def _contents(catalog: Catalog, items: list, root_id: str, diagnostics: list, ref: dict) -> list[dict]:
    """Only parent-linked descendants of the actual purchased root are sources."""
    by_id = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("_id"), str):
            raise DataError(f"invalid assortment item in {ref['path']}")
        key = item["_id"]
        if key in by_id and by_id[key] != item:
            raise DataError(f"ambiguous duplicate assortment item {key} in {ref['path']}")
        by_id[key] = item
    result = []
    for item in by_id.values():
        tid = item.get("_tpl")
        if tid not in catalog.items or catalog.items[tid].get("_type") != "Item":
            continue
        current = item
        chain = []
        seen = set()
        while current["_id"] != root_id:
            if current["_id"] in seen:
                diagnostics.append({"kind": "assortment_cycle", "evidence": ref, "item_id": item["_id"]})
                break
            seen.add(current["_id"])
            chain.append(current.get("slotId"))
            current = by_id.get(current.get("parentId"))
            if current is None:
                break
        else:
            if item["_id"] == root_id or _detachable(catalog, tid):
                result.append({"template_id": tid, "item_id": item["_id"], "slot_path": list(reversed(chain))})
    return result


def load_acquisition(installation_root: str | Path, catalog: Catalog) -> dict[str, Any]:
    """Capture local SPT 4.1 dynamic/trader acquisition evidence, with no I/O writes.

    ``installation_root`` is explicit; neither the working directory nor profiles
    are searched. Runtime item properties are authoritative. Installed mod data
    is interpreted, never executed; arbitrary binary/runtime changes, loot,
    crafting, current stock, inventory, and character unlocks are not inferred.
    """
    root = Path(installation_root).resolve()
    runtime = root / "SPT_Runtime"
    database = runtime / "SPT_Data" / "database"
    reader = _EvidenceReader(root)
    item_path = runtime / "SPT_Data" / "configs" / "item.json"
    ragfair_path = runtime / "SPT_Data" / "configs" / "ragfair.json"
    globals_path = database / "globals.json"
    handbook_path = database / "templates" / "handbook.json"
    prices_path = database / "templates" / "prices.json"
    item_config = reader.read(item_path)
    ragfair = reader.read(ragfair_path)
    globals_data = reader.read(globals_path)
    handbook = reader.read(handbook_path)
    prices = reader.read(prices_path)
    if not all(isinstance(value, dict) for value in (item_config, ragfair, globals_data, handbook, prices)):
        raise DataError("acquisition configs, handbook, prices, and globals must be objects")
    dynamic = ragfair.get("dynamic")
    if not isinstance(dynamic, dict) or not isinstance(dynamic.get("blacklist"), dict):
        raise DataError("ragfair evidence is missing dynamic blacklist settings")
    diagnostics: list[dict] = []
    records = {tid: {"status": "unproven", "sources": [], "blocked_channels": [], "conditions": []}
               for tid, item in catalog.items.items() if item.get("_type") == "Item"}
    blacklist: dict[str, list[dict]] = defaultdict(list)
    for tid in item_config.get("blacklist", []):
        blacklist[tid].append(reader.ref(item_path, "/blacklist"))
    static_price_evidence: dict[str, list[dict]] = defaultdict(list)
    dynamic_price_evidence: dict[str, list[dict]] = defaultdict(list)
    for entry in handbook.get("Items", []):
        if isinstance(entry, dict) and _valid_static_price(entry.get("Price")):
            static_price_evidence[entry.get("Id")].append({"value": entry["Price"], "price_type": "static_handbook",
                "evidence": reader.ref(handbook_path, "/Items")})
    for tid, value in prices.items():
        if _positive(value):
            dynamic_price_evidence[tid].append({"value": value, "price_type": "dynamic",
                "evidence": reader.ref(prices_path, f"/{tid}")})

    mods = runtime / "user" / "mods"
    mod_files = []
    if mods.is_dir():
        for mod in sorted(mods.iterdir(), key=lambda path: path.name.casefold()):
            if mod.is_dir():
                mod_files.extend(_json_files(mod / "db"))
    custom_items: list[tuple[str, dict, dict]] = []
    scheme_files = []
    assort_files = []
    base_files = []
    quest_files = []
    assortment_directories = {path.parent for path in mod_files if path.stem.casefold() == "assort"}
    for path in mod_files:
        parts = {part.casefold() for part in path.parts}
        if "customitems" in parts:
            document = reader.read(path)
            if not isinstance(document, dict):
                raise DataError(f"CustomItems must be keyed by template ID: {path}")
            for tid, config in document.items():
                if tid not in catalog.items:
                    continue
                if not isinstance(config, dict):
                    raise DataError(f"invalid CustomItems configuration for {tid}: {path}")
                ref = reader.ref(path, f"/{tid}")
                custom_items.append((tid, config, ref))
                if _value(config, "addtoItemBlacklist") is True:
                    blacklist[tid].append(ref)
                for field, registration, evidence, valid, price_type in (
                    ("handbookPriceRoubles", "registerInHandbook", static_price_evidence, _valid_static_price, "static_handbook"),
                    ("fleaPriceRoubles", "registerInFleaPrices", dynamic_price_evidence, _positive, "dynamic"),
                ):
                    value = _value(config, field)
                    if _value(config, registration, True) is not False and valid(value):
                        evidence[tid].append({"value": value, "price_type": price_type, "evidence": {**ref, "field": field}})
        elif "customassortschemes" in parts:
            scheme_files.append(path)
        elif path.stem.casefold() == "base" and path.parent in assortment_directories:
            base_files.append(path)
        elif path.stem.casefold() == "assort":
            assort_files.append(path)
        elif path.stem.casefold() == "questassort":
            quest_files.append(path)

    traders: dict[str, dict] = {}
    sibling_traders: dict[Path, str] = {}
    base_traders = database / "traders"
    vanilla_assorts = []
    vanilla_quests = []
    for directory in sorted(base_traders.iterdir(), key=lambda path: path.name) if base_traders.is_dir() else []:
        if not directory.is_dir():
            continue
        files = {path.stem.casefold(): path for path in directory.iterdir() if path.suffix.casefold() in (".json", ".jsonc")}
        if "base" in files:
            base_files.insert(0, files["base"])
        if "assort" in files:
            vanilla_assorts.append(files["assort"])
        if "questassort" in files:
            vanilla_quests.append(files["questassort"])
    for path in base_files:
        base = reader.read(path)
        tid = base.get("_id") if isinstance(base, dict) else None
        if not isinstance(tid, str):
            raise DataError(f"trader base is missing _id: {path}")
        trader = traders.setdefault(tid, {"items": [], "barter": {}, "levels": {}, "quest": defaultdict(list), "field_sources": {}})
        trader.update({"id": tid, "name": base.get("nickname", base.get("name", tid)),
                       "unlocked_by_default": base.get("unlockedByDefault") is True,
                       "base_evidence": reader.ref(path)})
        sibling_traders[path.parent] = tid

    def trader_id(key: str) -> str | None:
        tid = _WTT_TRADER_IDS.get(key.casefold(), key)
        return tid if tid in traders else None

    def owner(path: Path) -> str | None:
        if path.parent in sibling_traders:
            return sibling_traders[path.parent]
        for parent in path.parents:
            if parent.name in traders:
                return parent.name
        return None

    def merge(trader_key: str, assort: dict, ref: dict, priority: int) -> None:
        tid = trader_id(trader_key)
        if tid is None:
            diagnostics.append({"kind": "unresolved_trader", "trader": trader_key, "evidence": ref})
            return
        trader = traders[tid]
        items = assort.get("items", [])
        if not isinstance(items, list):
            raise DataError(f"assortment items must be a list: {ref['path']}")
        trader["items"].extend((item, ref) for item in items)
        for field, key in (("barter", "barter_scheme"), ("levels", "loyal_level_items")):
            values = assort.get(key, {})
            if not isinstance(values, dict):
                raise DataError(f"assortment {key} must be an object: {ref['path']}")
            for item_id, value in values.items():
                marker = (field, item_id)
                previous = trader["field_sources"].get(marker)
                if previous and trader[field][item_id] != value:
                    previous_path = previous[1]["path"]
                    current_path = ref["path"]
                    prefix = "SPT_Runtime/user/mods/"
                    previous_mod = previous_path[len(prefix):].split("/", 1)[0] if previous_path.startswith(prefix) else None
                    current_mod = current_path[len(prefix):].split("/", 1)[0] if current_path.startswith(prefix) else None
                    known_later = priority > previous[0] and (previous_mod is None or previous_mod == current_mod)
                    if not known_later:
                        raise DataError(f"cannot establish override order for trader {tid}, {key}/{item_id}: {previous_path} and {current_path}")
                trader[field][item_id] = value
                trader["field_sources"][marker] = (priority, ref)

    for path in [*vanilla_assorts, *assort_files]:
        document = reader.read(path)
        if isinstance(document, dict) and "items" in document:
            tid = owner(path)
            if tid:
                merge(tid, document, reader.ref(path), 0)
            else:
                diagnostics.append({"kind": "unresolved_assortment", "evidence": reader.ref(path)})
        elif isinstance(document, dict) and any(key in document for key in ("started", "success", "fail")):
            quest_files.append(path)
        else:
            diagnostics.append({"kind": "unsupported_assortment", "evidence": reader.ref(path)})
    for tid, config, ref in custom_items:
        if _value(config, "addtoTraders") is not True:
            continue
        for trader_key, schemes in (_value(config, "traders", {}) or {}).items():
            for item_id, scheme in schemes.items():
                settings = _value(scheme, "barterSettings", {}) or {}
                barters = _value(scheme, "barters", []) or []
                assort = {"items": [{"_id": item_id, "_tpl": tid, "parentId": "hideout", "slotId": "hideout",
                                     "upd": {"UnlimitedCount": _value(settings, "unlimitedCount", False),
                                             "StackObjectsCount": _value(settings, "stackObjectsCount", 0),
                                             "BuyRestrictionMax": _value(settings, "buyRestrictionMax")}}],
                          "barter_scheme": {item_id: [barters]},
                          "loyal_level_items": {item_id: _value(settings, "loyalLevel")}}
                merge(trader_key, assort, ref, 1)
    for path in scheme_files:
        document = reader.read(path)
        if not isinstance(document, dict):
            raise DataError(f"CustomAssortSchemes must be keyed by trader: {path}")
        for key, assort in document.items():
            if not isinstance(assort, dict):
                raise DataError(f"invalid custom assortment for {key}: {path}")
            merge(key, assort, reader.ref(path, f"/{key}"), 2)
    for path in [*vanilla_quests, *quest_files]:
        document = reader.read(path)
        tid = owner(path)
        if tid is None or not isinstance(document, dict):
            diagnostics.append({"kind": "unresolved_quest_assortment", "evidence": reader.ref(path)})
            continue
        for state, mapping in document.items():
            if isinstance(mapping, dict):
                for offer_id, quest_id in mapping.items():
                    condition = {"kind": "quest", "quest_id": quest_id, "state": state, "evidence": reader.ref(path, f"/{state}/{offer_id}")}
                    traders[tid]["quest"][offer_id].append(condition)

    # GetItemPrice uses a valid static price before considering dynamic prices.
    price_evidence = {**dynamic_price_evidence, **static_price_evidence}

    flea_global = globals_data.get("config", {}).get("RagFair", {})
    black = dynamic["blacklist"]
    dynamic_conditions: dict[str, list] = {}
    dynamic_reasons: dict[str, list] = {}
    for tid in records:
        item = catalog.items[tid]
        props = catalog.props(tid)
        reasons = []
        if not (catalog.is_mod(tid) or catalog.is_a(tid, WEAPON_CATEGORY)):
            reasons.append("outside_weapon_and_mod_dynamic_model")
        if blacklist.get(tid):
            reasons.append("global_item_blacklist")
        if props.get("QuestItem", False):
            reasons.append("quest_item_rejected_by_ItemHelper")
        if any(catalog.is_a(tid, base) for base in _INVALID_DYNAMIC_BASES):
            reasons.append("invalid_dynamic_base_type")
        if not price_evidence.get(tid):
            reasons.append("no_positive_registered_price_evidence")
        if black.get("enableBsgList", True) and props.get("CanSellOnRagfair") is not True:
            reasons.append("runtime_CanSellOnRagfair_not_true")
        if tid in black.get("custom", []):
            reasons.append("ragfair_custom_blacklist")
        if black.get("enableCustomItemCategoryList", False) and item.get("_parent") in black.get("customItemCategoryList", []):
            reasons.append("ragfair_direct_parent_blacklist")
        if black.get("damagedAmmoPacks", False) and item.get("_parent") == "543be5cb4bdc2deb348b4568" and "_damaged" in item.get("_name", ""):
            reasons.append("damaged_ammo_pack")
        if flea_global.get("enabled") is not True:
            reasons.append("flea_disabled_or_enablement_unproven")
        count = dynamic.get("offerItemCount", {}).get(item.get("_parent"), dynamic.get("offerItemCount", {}).get("default", {}))
        if not isinstance(count, dict) or not _positive(count.get("max")):
            reasons.append("dynamic_offer_count_cannot_be_positive")
        tiered = ragfair.get("tieredFlea", {})
        tiered_requirements = {"enabled": tiered.get("enabled", False),
            "template_level": tiered.get("unlocksTpl", {}).get(tid),
            "category_levels": [{"category": base, "minimum_level": level}
                                for base, level in tiered.get("unlocksType", {}).items()
                                if base in catalog.ancestors(tid)]}
        conditions = [{"kind": "flea_access", "minimum_level": flea_global.get("minUserLevel"),
                       "item_level_requirement": props.get("RagfairLevelToTrade", 0),
                       "tiered_flea": tiered_requirements, "current_character_checked": False},
                      {"kind": "generated_stock", "current_offers_checked": False}]
        dynamic_conditions[tid] = conditions
        dynamic_reasons[tid] = reasons
        if reasons:
            records[tid]["blocked_channels"].append({"channel": "dynamic_flea", "reasons": reasons,
                "blacklist_evidence": blacklist.get(tid, []), "rules_evidence": reader.ref(ragfair_path, "/dynamic")})
        else:
            _record_source(records, tid, {"channel": "dynamic_flea", "conditions": conditions,
                "evidence": reader.ref(ragfair_path, "/dynamic"), "positive_price_evidence": price_evidence[tid]})

    offers = []
    for trader in traders.values():
        raw_items = [item for item, _ in trader["items"]]
        by_id = {}
        for item, ref in trader["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("_id"), str):
                raise DataError(f"invalid trader assortment in {ref['path']}")
            previous = by_id.get(item["_id"])
            if previous and previous[0] != item:
                raise DataError(f"ambiguous duplicate trader item {item['_id']}")
            by_id[item["_id"]] = (item, ref)
        for offer_id, (item, ref) in by_id.items():
            if item.get("parentId") != "hideout" or item.get("_tpl") not in catalog.items:
                continue
            barter = _barter_options(catalog, trader["barter"].get(offer_id), diagnostics, ref)
            level = trader["levels"].get(offer_id)
            stock = item.get("upd", {}) or {}
            if not isinstance(barter, list) or not any(isinstance(option, list) and option for option in barter):
                diagnostics.append({"kind": "offer_without_barter_evidence", "offer_id": offer_id, "evidence": ref})
                continue
            if isinstance(level, bool) or not isinstance(level, int) or level < 1:
                diagnostics.append({"kind": "offer_without_loyalty_evidence", "offer_id": offer_id, "evidence": ref})
                continue
            if stock.get("UnlimitedCount") is not True and not _positive(stock.get("StackObjectsCount")):
                continue
            conditions = [{"kind": "trader_loyalty", "trader_id": trader["id"], "minimum_level": level,
                           "evidence": trader["field_sources"][("levels", offer_id)][1], "current_character_checked": False},
                          {"kind": "barter", "alternatives": barter,
                           "evidence": trader["field_sources"][("barter", offer_id)][1]},
                          {"kind": "trader_stock", "configured_unlimited": stock.get("UnlimitedCount") is True,
                           "configured_count": stock.get("StackObjectsCount"), "buy_limit": stock.get("BuyRestrictionMax"),
                           "current_stock_checked": False}]
            if not trader["unlocked_by_default"]:
                conditions.append({"kind": "trader_unlock", "trader_id": trader["id"], "current_character_checked": False})
            conditions.extend(trader["quest"].get(offer_id, []))
            if any(_value(requirement, "sptQuestLocked") is True for option in barter if isinstance(option, list)
                   for requirement in option if isinstance(requirement, dict)):
                conditions.append({"kind": "quest_locked_barter", "quest_id": None, "current_character_checked": False})
            contents = _contents(catalog, raw_items, offer_id, diagnostics, ref)
            offer = {"trader_id": trader["id"], "trader_name": trader["name"], "offer_id": offer_id,
                     "root_template_id": item["_tpl"], "conditions": conditions, "contents": contents, "evidence": ref}
            offers.append(offer)
            for child in contents:
                _record_source(records, child["template_id"], {"channel": "trader" if child["item_id"] == offer_id else "trader_dismantle",
                    "trader_id": trader["id"], "trader_name": trader["name"], "offer_id": offer_id,
                    "root_template_id": item["_tpl"], "slot_path": child["slot_path"], "conditions": conditions, "evidence": ref})

    presets = [(preset, reader.ref(globals_path, f"/ItemPresets/{key}"))
               for key, preset in globals_data.get("ItemPresets", {}).items()]
    for tid, config, ref in custom_items:
        if _value(config, "addWeaponPreset") is True:
            presets.extend((preset, ref) for preset in (_value(config, "weaponPresets", []) or []))
    for preset, ref in presets:
        if not isinstance(preset, dict) or not isinstance(preset.get("_items"), list) or not preset["_items"]:
            continue
        if dynamic.get("showDefaultPresetsOnly", True) and not preset.get("_encyclopedia"):
            continue
        root_item = preset["_items"][0]
        tid = root_item.get("_tpl")
        if tid not in dynamic_reasons or dynamic_reasons[tid]:
            continue
        contents = _contents(catalog, preset["_items"], root_item.get("_id"), diagnostics, ref)
        offers.append({"preset_id": preset.get("_id"), "root_template_id": tid,
                       "contents": contents, "evidence": ref, "channel": "dynamic_preset"})
        for child in contents:
            if child["item_id"] != root_item.get("_id"):
                _record_source(records, child["template_id"], {"channel": "dynamic_preset_dismantle",
                    "preset_id": preset.get("_id"), "root_template_id": tid, "slot_path": child["slot_path"],
                    "conditions": dynamic_conditions[tid], "evidence": ref})
    for record in records.values():
        if record["sources"]:
            record["status"] = "conditional" if all(source.get("conditions") for source in record["sources"]) else "available"
            record["conditions"] = [{"source_index": index, "requirements": source.get("conditions", [])}
                                    for index, source in enumerate(record["sources"])]
        else:
            record["reason"] = "no_proven_source_in_modeled_channels_not_an_absolute_acquisition_ban"
    return {"schema_version": ACQUISITION_SCHEMA_VERSION, "model": "spt-4.1-offline-acquisition",
            "captured_at": datetime.now(timezone.utc).isoformat(), "catalog_sha256": _digest(catalog.items),
            "source_files": list(reader.files.values()), "items": records, "offers": offers,
            "rules": {"dynamic": dynamic, "tiered_flea": ragfair.get("tieredFlea", {}),
                      "global_flea": {key: flea_global.get(key) for key in ("enabled", "minUserLevel")},
                      "global_blacklist": dict(blacklist), "invalid_dynamic_categories": list(_INVALID_DYNAMIC_BASES)},
            "random_reward_sources": "excluded; purchase must identify the acquired item or dismantlable assembly before payment",
            "diagnostics": diagnostics,
            "boundaries": ["item properties come only from the retained runtime catalog",
                "conditional means a configured source, not current-character access or current stock",
                "dynamic generation modeled only for weapon and Mod categories; no seasonal entries occur in the inspected core lists for those categories",
                "local installed mod JSON is interpreted, not executed; arbitrary DLL/config mutations are not replayed",
                "loot, crafting, quest rewards, player inventory, current event/service state and live offers are not inferred",
                "random reward packages are excluded; this does not claim their contents are unobtainable",
                "global/item/flea blacklists close dynamic channels only; proven trader or composite sources remain usable",
                "unproven is excluded and does not claim an item is unobtainable through every possible game channel"]}
