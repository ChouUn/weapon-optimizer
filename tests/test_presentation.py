"""Searchable localized names and the requested attachment-tree output contract."""
from __future__ import annotations

import unittest

from weapon_optimizer.data import DataError
from weapon_optimizer.presentation import render_build
from test_optimizer import catalog_with, item, node, slot


class TreePresentationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog_with(
            item("guard"), item("english_part"),
            weapon_slots=[slot("mod_handguard", "guard"), slot("mod_scope_001", "english_part")],
        )
        self.catalog.locale.update({
            "weapon Name": '<b><color=#ffffff>AK-101 Unique</color></b>',
            "weapon ShortName": "AK-101 >SMG & WWII<",
            "guard Name": "English guard",
            "guard ShortName": "English short",
            "english_part Name": "Original English Name (Blue tape)",
            "english_part ShortName": "GF MOD3",
        })
        self.catalog.locale_ch.update({
            "guard Name": "Zenit B-10M 导轨护木+ B-19 上导轨组合",
            "guard ShortName": "B10M+B19",
        })

    def test_exact_chinese_and_english_fallback_names_keep_the_assembly(self):
        build = node("weapon", None, node("guard", "mod_handguard"), node("english_part", "mod_scope_001"))
        self.assertEqual(
            render_build(self.catalog, build),
            "[武器] AK-101 >SMG & WWII<〔AK-101 Unique〕\n"
            "├── [护木] B10M+B19〔Zenit B-10M 导轨护木+ B-19 上导轨组合〕\n"
            "└── [瞄具] GF MOD3〔Original English Name (Blue tape)〕",
        )

    def test_missing_short_name_falls_back_per_key_not_to_a_translated_full_name(self):
        del self.catalog.locale_ch["guard ShortName"]
        self.assertEqual(self.catalog.name("guard", language="ch", short=True), "English short")
        self.assertEqual(self.catalog.name("guard", language="ch"), "Zenit B-10M 导轨护木+ B-19 上导轨组合")
        self.catalog.locale["guard ShortName"] = "<b></b>"
        self.assertEqual(self.catalog.name("guard", language="ch", short=True), "Zenit B-10M 导轨护木+ B-19 上导轨组合")

    def test_english_selection_does_not_leak_chinese_item_names(self):
        self.assertEqual(self.catalog.name("guard", language="en", short=True), "English short")
        self.assertEqual(self.catalog.name("guard", language="en"), "English guard")

    def test_repeated_templates_remain_separate_and_unknown_slots_stay_identifiable(self):
        build = node("weapon", None,
                     node("english_part", "mod_custom_001"),
                     node("english_part", "mod_custom_002"))
        self.assertEqual(
            render_build(self.catalog, build),
            "[武器] AK-101 >SMG & WWII<〔AK-101 Unique〕\n"
            "├── [mod_custom_001] GF MOD3〔Original English Name (Blue tape)〕\n"
            "└── [mod_custom_002] GF MOD3〔Original English Name (Blue tape)〕",
        )

    def test_cycles_and_shared_nodes_are_rejected_instead_of_looping_or_deduplicating(self):
        shared = node("guard", "mod_handguard")
        for build in (node("weapon", None, shared, shared), node("weapon")):
            if not build["children"]:
                build["children"].append(build)
            with self.subTest(build=build["template_id"]), self.assertRaises(DataError):
                render_build(self.catalog, build)
