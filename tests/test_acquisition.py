"""Deterministic acquisition behavior; fixtures live only under workspace .tmp."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import simplejson as json

from weapon_optimizer.acquisition import Availability, load_acquisition, read_json_file
from weapon_optimizer.data import Catalog, DataError, MOD_CATEGORY, WEAPON_CATEGORY, _digest


def template(tid, *, parent=MOD_CATEGORY, **props):
    return {"_id": tid, "_name": tid, "_parent": parent, "_type": "Item",
            "_props": {"CanSellOnRagfair": True, "QuestItem": False, **props}}


def catalog_with(*parts):
    items = {
        MOD_CATEGORY: {"_id": MOD_CATEGORY, "_type": "Node", "_parent": "", "_props": {}},
        WEAPON_CATEGORY: {"_id": WEAPON_CATEGORY, "_type": "Node", "_parent": "", "_props": {}},
        "weapon": template("weapon", parent=WEAPON_CATEGORY),
        "roubles": template("roubles", parent=""),
    }
    items.update((item["_id"], item) for item in parts)
    return Catalog(items)


class AcquisitionBehaviorTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[3]
        self.temporary = tempfile.TemporaryDirectory(prefix="acquisition-fixture-", dir=workspace / ".tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write("SPT_Runtime/SPT_Data/configs/item.json", {"blacklist": []})
        self.ragfair = {"dynamic": {"blacklist": {"enableBsgList": True, "enableQuestList": True,
            "custom": [], "enableCustomItemCategoryList": False, "customItemCategoryList": []},
            "offerItemCount": {"default": {"min": 1, "max": 3}}, "showDefaultPresetsOnly": True},
            "tieredFlea": {"enabled": False}}
        self.write("SPT_Runtime/SPT_Data/configs/ragfair.json", self.ragfair)
        self.globals = {"config": {"RagFair": {"enabled": True, "minUserLevel": 15}}, "ItemPresets": {}}
        self.write("SPT_Runtime/SPT_Data/database/globals.json", self.globals)
        self.write("SPT_Runtime/SPT_Data/database/templates/handbook.json", {"Items": []})
        self.write("SPT_Runtime/SPT_Data/database/templates/prices.json", {})

    def write(self, path, document):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, use_decimal=True), encoding="utf-8")
        return path

    def priced(self, *ids):
        self.write("SPT_Runtime/SPT_Data/database/templates/handbook.json",
                   {"Items": [{"Id": tid, "Price": 1000} for tid in ids]})

    def trader(self, items, *, level=3, quests=None, custom=False, trader_id="trader", nickname="Synthetic"):
        directory = "SPT_Runtime/user/mods/Synthetic/db/TraderIndependent" if custom else f"SPT_Runtime/SPT_Data/database/traders/{trader_id}"
        self.write(directory + "/Base.json", {"_id": trader_id, "nickname": nickname, "unlockedByDefault": False})
        roots = [item["_id"] for item in items if item.get("parentId") == "hideout"]
        self.write(directory + "/Assort.json", {"items": items,
            "barter_scheme": {iid: [[{"_tpl": "roubles", "count": 5000}]] for iid in roots},
            "loyal_level_items": {iid: level for iid in roots}})
        if quests:
            self.write(directory + "/QuestAssort.json", quests)

    def availability(self, catalog):
        evidence = load_acquisition(self.root, catalog)
        return Availability(catalog, evidence)

    def test_dynamic_supply_does_not_require_an_assort_and_uses_runtime_sell_flag(self):
        catalog = catalog_with(template("dynamic"))
        before = deepcopy(catalog.items)
        self.write("SPT_Runtime/user/mods/Synthetic/db/CustomItems/parts.json", {
            "dynamic": {"itemTplToClone": "source", "overrideProperties": {"CanSellOnRagfair": False},
                        "handbookPriceRoubles": 9000, "addtoTraders": False}})
        availability = self.availability(catalog)
        self.assertIn("dynamic", availability.allowed_ids)
        result = availability.acquisition_status("dynamic")
        self.assertEqual(result["status"], "conditional")
        self.assertEqual([source["channel"] for source in result["sources"]], ["dynamic_flea"])
        self.assertEqual(result["sources"][0]["conditions"][0]["minimum_level"], 15)
        self.assertEqual(catalog.items, before)

    def test_fractional_handbook_price_without_dynamic_source_is_unproven(self):
        catalog = catalog_with(template("part"))
        self.write("SPT_Runtime/SPT_Data/database/templates/handbook.json",
                   {"Items": [{"Id": "part", "Price": Decimal("0.5")}]})
        availability = self.availability(catalog)
        self.assertNotIn("part", availability.allowed_ids)
        self.assertIn("no_positive_registered_price_evidence",
                      availability.acquisition_status("part")["blocked_channels"][0]["reasons"])

    def test_fractional_handbook_price_falls_back_to_positive_dynamic_price(self):
        catalog = catalog_with(template("part"))
        self.write("SPT_Runtime/SPT_Data/database/templates/handbook.json",
                   {"Items": [{"Id": "part", "Price": Decimal("0.5")}]})
        self.write("SPT_Runtime/SPT_Data/database/templates/prices.json", {"part": Decimal("0.25")})
        availability = self.availability(catalog)
        self.assertIn("part", availability.allowed_ids)
        evidence = availability.acquisition_status("part")["sources"][0]["positive_price_evidence"]
        self.assertEqual([(entry["price_type"], entry["value"]) for entry in evidence], [("dynamic", Decimal("0.25"))])

    def test_handbook_price_of_one_is_valid_and_precedes_dynamic_price(self):
        catalog = catalog_with(template("part"))
        self.write("SPT_Runtime/SPT_Data/database/templates/handbook.json", {"Items": [{"Id": "part", "Price": 1}]})
        self.write("SPT_Runtime/SPT_Data/database/templates/prices.json", {"part": 1000})
        availability = self.availability(catalog)
        self.assertIn("part", availability.allowed_ids)
        evidence = availability.acquisition_status("part")["sources"][0]["positive_price_evidence"]
        self.assertEqual([(entry["price_type"], entry["value"]) for entry in evidence], [("static_handbook", 1)])

    def test_mod_registration_separates_handbook_threshold_and_dynamic_price(self):
        catalog = catalog_with(template("part"))
        cases = [
            ({"handbookPriceRoubles": Decimal("0.5")}, None),
            ({"handbookPriceRoubles": 1}, "static_handbook"),
            ({"fleaPriceRoubles": Decimal("0.5")}, "dynamic"),
            ({"handbookPriceRoubles": Decimal("0.5"), "fleaPriceRoubles": Decimal("0.25")}, "dynamic"),
            ({"handbookPriceRoubles": 1, "fleaPriceRoubles": 1000}, "static_handbook"),
            ({"handbookPriceRoubles": 1, "registerInHandbook": False}, None),
            ({"fleaPriceRoubles": Decimal("0.5"), "registerInFleaPrices": False}, None),
        ]
        for config, expected_type in cases:
            with self.subTest(config=config):
                self.write("SPT_Runtime/user/mods/Synthetic/db/CustomItems/part.json", {"part": config})
                availability = self.availability(catalog)
                self.assertEqual("part" in availability.allowed_ids, expected_type is not None)
                if expected_type is not None:
                    evidence = availability.acquisition_status("part")["sources"][0]["positive_price_evidence"]
                    self.assertEqual({entry["price_type"] for entry in evidence}, {expected_type})

    def test_global_and_mod_blacklists_deny_only_unproven_dynamic_paths(self):
        catalog = catalog_with(template("global"), template("mod_blocked"), template("not_blocked"))
        self.priced("global", "mod_blocked", "not_blocked")
        self.write("SPT_Runtime/SPT_Data/configs/item.json", {"blacklist": ["global"]})
        self.write("SPT_Runtime/user/mods/Synthetic/db/CustomItems/parts.json", {
            "mod_blocked": {"itemTplToClone": "source", "addtoItemBlacklist": True},
            "not_blocked": {"itemTplToClone": "source", "addtoTraders": False}})
        availability = self.availability(catalog)
        self.assertNotIn("global", availability.allowed_ids)
        self.assertNotIn("mod_blocked", availability.allowed_ids)
        self.assertIn("not_blocked", availability.allowed_ids)
        for tid in ("global", "mod_blocked"):
            result = availability.acquisition_status(tid)
            self.assertEqual(result["status"], "unproven")
            self.assertIn("not_an_absolute", result["reason"])
            self.assertIn("global_item_blacklist", result["blocked_channels"][0]["reasons"])

    def test_independent_trader_and_removable_descendants_are_proven_with_conditions(self):
        catalog = catalog_with(template("part", CanSellOnRagfair=False),
                               template("locked", CanSellOnRagfair=False, IsUnremovable=True),
                               template("orphan", CanSellOnRagfair=False))
        self.write("SPT_Runtime/SPT_Data/configs/item.json", {"blacklist": ["part"]})
        self.trader([
            {"_id": "sale", "_tpl": "weapon", "parentId": "hideout", "upd": {"UnlimitedCount": True}},
            {"_id": "child", "_tpl": "part", "parentId": "sale", "slotId": "mod_stock"},
            {"_id": "locked-child", "_tpl": "locked", "parentId": "sale", "slotId": "mod_mount"},
            {"_id": "orphan-child", "_tpl": "orphan", "parentId": "absent", "slotId": "mod_muzzle"},
        ], quests={"success": {"sale": "quest-unlock"}}, custom=True)
        availability = self.availability(catalog)
        self.assertIn("part", availability.allowed_ids)
        self.assertNotIn("locked", availability.allowed_ids)
        self.assertNotIn("orphan", availability.allowed_ids)
        source = availability.acquisition_status("part")["sources"][0]
        self.assertEqual(source["channel"], "trader_dismantle")
        self.assertEqual(source["root_template_id"], "weapon")
        self.assertEqual(source["slot_path"], ["mod_stock"])
        conditions = source["conditions"]
        self.assertTrue(any(c["kind"] == "trader_loyalty" and c["minimum_level"] == 3 for c in conditions))
        self.assertTrue(any(c["kind"] == "quest" and c["quest_id"] == "quest-unlock" for c in conditions))
        self.assertTrue(any(c["kind"] == "trader_unlock" for c in conditions))

    def test_custom_schemes_override_inline_loyalty_and_barter_per_offer(self):
        catalog = catalog_with(template("part", CanSellOnRagfair=False))
        self.trader([], custom=True)
        self.write("SPT_Runtime/user/mods/Synthetic/db/CustomItems/part.json", {
            "part": {"itemTplToClone": "source", "addtoTraders": True,
                     "traders": {"trader": {"sale": {"barterSettings": {"loyalLevel": 4,
                         "unlimitedCount": True, "stackObjectsCount": 99},
                         "barters": [{"_tpl": "roubles", "count": 10000}]}}}}})
        self.write("SPT_Runtime/user/mods/Synthetic/db/CustomAssortSchemes/trader.json", {
            "trader": {"items": [], "loyal_level_items": {"sale": 2},
                          "barter_scheme": {"sale": [[{"_tpl": "roubles", "count": 20000, "sptQuestLocked": True}]]}}})
        result = self.availability(catalog).acquisition_status("part")
        self.assertEqual(result["status"], "conditional")
        source = result["sources"][0]
        loyalty = next(c for c in source["conditions"] if c["kind"] == "trader_loyalty")
        self.assertEqual(loyalty["minimum_level"], 2)
        self.assertIn("CustomAssortSchemes", loyalty["evidence"]["path"])
        barter = next(c for c in source["conditions"] if c["kind"] == "barter")
        self.assertEqual(barter["alternatives"][0][0]["count"], 20000)
        self.assertTrue(any(c["kind"] == "quest_locked_barter" for c in source["conditions"]))

    def test_canonical_ref_key_does_not_depend_on_the_arena_nickname(self):
        roubles = "5449016a4bdc2d6f028b456f"
        catalog = catalog_with(template("part", CanSellOnRagfair=False), template(roubles, parent=""))
        ref_id = "6617beeaa9cfa777ca915b7c"
        self.trader([], trader_id=ref_id, nickname="Arena")
        self.write("SPT_Runtime/user/mods/Synthetic/db/CustomItems/part.json", {
            "part": {"addtoTraders": True, "traders": {"REF": {"sale": {
                "barterSettings": {"loyalLevel": 2, "unlimitedCount": True},
                "barters": [{"_tpl": "MONEY_ROUBLES", "count": Decimal("2.49")}]}}}}})
        availability = self.availability(catalog)
        self.assertIn("part", availability.allowed_ids)
        source = availability.acquisition_status("part")["sources"][0]
        self.assertEqual(source["trader_id"], ref_id)
        barter = next(condition for condition in source["conditions"] if condition["kind"] == "barter")
        self.assertEqual(barter["alternatives"], [[{"_tpl": roubles, "count": Decimal("2.49")}]])

    def test_assortment_without_sibling_base_can_resolve_another_mods_registered_trader(self):
        catalog = catalog_with(template("part", CanSellOnRagfair=False))
        self.trader([], custom=True)
        self.write("SPT_Runtime/user/mods/AssortAddon/db/traders/trader/assort.json", {
            "items": [{"_id": "sale", "_tpl": "part", "parentId": "hideout", "upd": {"StackObjectsCount": 4}}],
            "loyal_level_items": {"sale": 2}, "barter_scheme": {"sale": [[{"_tpl": "roubles", "count": 10}]]}})
        source = self.availability(catalog).acquisition_status("part")["sources"][0]
        self.assertEqual(source["trader_id"], "trader")
        self.assertIn("AssortAddon", source["evidence"]["path"])

    def test_unresolved_assortment_is_diagnosed_without_creating_a_trader(self):
        catalog = catalog_with(template("part", CanSellOnRagfair=False))
        self.write("SPT_Runtime/user/mods/Unknown/db/traders/unknown/assort.json", {
            "items": [{"_id": "sale", "_tpl": "part", "parentId": "hideout", "upd": {"UnlimitedCount": True}}],
            "loyal_level_items": {"sale": 1}, "barter_scheme": {"sale": [[{"_tpl": "roubles", "count": 1}]]}})
        availability = self.availability(catalog)
        self.assertNotIn("part", availability.allowed_ids)
        self.assertTrue(any(value["kind"] == "unresolved_assortment" for value in availability.evidence["diagnostics"]))

    def test_nickname_is_not_an_unrecognized_wtt_trader_key_fallback(self):
        catalog = catalog_with(template("part", CanSellOnRagfair=False))
        self.trader([], custom=True)
        self.write("SPT_Runtime/user/mods/Synthetic/db/CustomAssortSchemes/trader.json", {
            "Synthetic": {"items": [{"_id": "sale", "_tpl": "part", "parentId": "hideout", "upd": {"UnlimitedCount": True}}],
                          "loyal_level_items": {"sale": 1}, "barter_scheme": {"sale": [[{"_tpl": "roubles", "count": 1}]]}}})
        availability = self.availability(catalog)
        self.assertNotIn("part", availability.allowed_ids)
        self.assertTrue(any(value["kind"] == "unresolved_trader" for value in availability.evidence["diagnostics"]))

    def test_unresolved_barter_requirement_is_not_an_acquisition_source(self):
        catalog = catalog_with(template("part", CanSellOnRagfair=False))
        self.trader([], custom=True)
        self.write("SPT_Runtime/user/mods/Synthetic/db/CustomAssortSchemes/trader.json", {
            "trader": {"items": [{"_id": "sale", "_tpl": "part", "parentId": "hideout", "upd": {"UnlimitedCount": True}}],
                       "loyal_level_items": {"sale": 1}, "barter_scheme": {"sale": [[{"_tpl": "UNKNOWN_SYMBOL", "count": 1}]]}}})
        availability = self.availability(catalog)
        self.assertNotIn("part", availability.allowed_ids)
        self.assertTrue(any(value["kind"] == "unproven_barter_alternative" for value in availability.evidence["diagnostics"]))

    def test_unknown_or_unpriced_items_never_silently_pass(self):
        catalog = catalog_with(template("unpriced"), template("not_sellable", CanSellOnRagfair=False))
        self.priced("not_sellable")
        availability = self.availability(catalog)
        self.assertNotIn("unpriced", availability.allowed_ids)
        self.assertNotIn("not_sellable", availability.allowed_ids)
        with self.assertRaises(DataError):
            Availability(catalog, None)
        with self.assertRaises(DataError):
            Availability(catalog, {"schema_version": 1, "catalog_sha256": "different", "items": {}, "source_files": []})

    def test_category_blacklist_is_direct_parent_and_quest_validity_is_independent(self):
        catalog = catalog_with(template("direct"))
        catalog.items.update({
            "subcategory": {"_id": "subcategory", "_parent": MOD_CATEGORY, "_type": "Node", "_props": {}},
            "nested": template("nested", parent="subcategory"),
            "quest": template("quest", parent="subcategory", QuestItem=True),
        })
        catalog = Catalog(catalog.items)
        self.priced("direct", "nested", "quest")
        self.ragfair["dynamic"]["blacklist"].update({"enableCustomItemCategoryList": True,
                                                    "customItemCategoryList": [MOD_CATEGORY], "enableQuestList": False})
        self.write("SPT_Runtime/SPT_Data/configs/ragfair.json", self.ragfair)
        availability = self.availability(catalog)
        self.assertNotIn("direct", availability.allowed_ids)
        self.assertIn("nested", availability.allowed_ids)
        self.assertNotIn("quest", availability.allowed_ids)

    def test_dynamic_composite_source_does_not_apply_child_blacklist_to_the_root(self):
        catalog = catalog_with(template("part", CanSellOnRagfair=False))
        self.priced("weapon")
        self.write("SPT_Runtime/SPT_Data/configs/item.json", {"blacklist": ["part"]})
        self.globals["ItemPresets"] = {"preset": {"_id": "preset", "_encyclopedia": "weapon", "_items": [
            {"_id": "root", "_tpl": "weapon"},
            {"_id": "part-instance", "_tpl": "part", "parentId": "root", "slotId": "mod_stock"}]}}
        self.write("SPT_Runtime/SPT_Data/database/globals.json", self.globals)
        availability = self.availability(catalog)
        self.assertIn("part", availability.allowed_ids)
        self.assertEqual(availability.acquisition_status("part")["sources"][0]["channel"], "dynamic_preset_dismantle")
        self.write("SPT_Runtime/SPT_Data/configs/item.json", {"blacklist": ["part", "weapon"]})
        self.assertNotIn("part", self.availability(catalog).allowed_ids)

    def test_conflicting_scheme_order_is_diagnosed_not_guessed(self):
        catalog = catalog_with(template("part", CanSellOnRagfair=False))
        self.trader([{"_id": "sale", "_tpl": "part", "parentId": "hideout", "upd": {"UnlimitedCount": True}}])
        for name, level in (("one", 2), ("two", 4)):
            self.write(f"SPT_Runtime/user/mods/Synthetic/db/CustomAssortSchemes/{name}.json", {
                "trader": {"items": [], "loyal_level_items": {"sale": level}, "barter_scheme": {}}})
        with self.assertRaisesRegex(DataError, "override order"):
            load_acquisition(self.root, catalog)

    def test_evidence_round_trip_is_offline_and_source_hashes_are_auditable(self):
        catalog = catalog_with(template("part"))
        self.priced("part")
        evidence = load_acquisition(self.root, catalog)
        copied = json.loads(json.dumps(evidence, use_decimal=True), use_decimal=True)
        for source in evidence["source_files"]:
            self.assertEqual(source["sha256"], hashlib.sha256((self.root / source["path"]).read_bytes()).hexdigest())
        self.assertEqual(copied["catalog_sha256"], _digest(catalog.items))
        with patch.object(Path, "read_bytes", side_effect=AssertionError("replay must not read files")):
            replay = Availability(catalog, copied)
            self.assertIn("part", replay.allowed_ids)

    def test_jsonc_retains_urls_comment_text_escapes_and_decimal_precision(self):
        path = self.root / "strings.jsonc"
        path.write_text('\ufeff{\n// comment\n"url":"https://example.invalid/a//b",\n'
                        '"text":"/* literal */ and \\\"quoted\\\"", /* block */\n'
                        '"number":1.0000000000000001,"array":[1,2,],\n}', encoding="utf-8")
        document = read_json_file(path)
        self.assertEqual(document["url"], "https://example.invalid/a//b")
        self.assertEqual(document["text"], '/* literal */ and "quoted"')
        self.assertEqual(document["number"], Decimal("1.0000000000000001"))
        self.assertEqual(document["array"], [1, 2])
        path.write_text('{"bad":1, /* unclosed', encoding="utf-8")
        with self.assertRaisesRegex(DataError, "unterminated"):
            read_json_file(path)


if __name__ == "__main__":
    unittest.main()
