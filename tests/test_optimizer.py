"""Behavior regressions using isolated synthetic templates and a tiny tree oracle."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from fractions import Fraction
from itertools import product
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from weapon_optimizer.data import Catalog, DataError, _digest
from weapon_optimizer.evaluate import evaluate_build
from weapon_optimizer.optimizer import optimize as optimize_builds
from weapon_optimizer.compatibility import Compatibility, load_compatibility


ITEM = "54009119af1c881c07000029"
WEAPON = "5422acb9af1c889c16000029"
MOD = "5448fe124bdc2da5018b4567"
MAGAZINE = "5448bc234bdc2d3c308b4569"
LAUNCHER = "55818b014bdc2ddc698b456b"
AMMO = "5485a8684bdc2da71d8b4567"
CYLINDER = "610720f290b75a49ff2e5e25"
SPRING_CYLINDER = "627a137bf21bc425b06ab944"


def optimize(catalog, weapon_id, **options):
    """These synthetic solver fixtures deliberately study raw template choices."""
    return optimize_builds(catalog, weapon_id, acquisition="templates", **options)


def slot(name, *allowed, required=False):
    return {
        "_id": name + "_id",
        "_name": name,
        "_required": required,
        "_props": {"filters": [{"Filter": list(allowed)}]},
    }


def item(template_id, parent=MOD, **props):
    return {
        "_id": template_id,
        "_name": template_id,
        "_parent": parent,
        "_type": "Item",
        "_props": {"Slots": [], "ConflictingItems": [], **props},
    }


def node(template_id, slot_name=None, *children):
    return {"template_id": template_id, "slot": slot_name, "children": list(children)}


def catalog_with(*templates, weapon_slots=(), base_ergo=50):
    entries = {}
    for template_id, parent in (
        (ITEM, ""), (WEAPON, ITEM), (MOD, ITEM), (MAGAZINE, MOD), (LAUNCHER, MOD), (AMMO, ITEM),
        (CYLINDER, MAGAZINE), (SPRING_CYLINDER, CYLINDER),
    ):
        entries[template_id] = {
            "_id": template_id, "_name": template_id, "_parent": parent,
            "_type": "Node", "_props": {},
        }
    entries["weapon"] = item(
        "weapon", WEAPON, RecoilForceUp=100, RecoilForceBack=200,
        Ergonomics=base_ergo, Slots=list(weapon_slots),
    )
    entries.update((template["_id"], template) for template in templates)
    return Catalog(entries)


def fingerprint(build):
    return (
        build["template_id"], build["slot"],
        tuple(sorted(fingerprint(child) for child in build["children"])),
    )


def flatten(build):
    yield build
    for child in build["children"]:
        yield from flatten(child)


def enumerate_trees(catalog, template_id="weapon", slot_name=None, active=()):
    """Enumerate full choices before filtering conflicts; never prune by stats."""
    if template_id in active:
        raise ValueError("template cycle in oracle")
    active = (*active, template_id)
    choices = []
    for parent_slot in catalog.slots(template_id):
        alternatives = [] if parent_slot.get("_required") else [None]
        for candidate in catalog.candidates(parent_slot):
            if catalog.is_a(candidate, AMMO):
                continue
            alternatives.extend(enumerate_trees(catalog, candidate, parent_slot["_name"], active))
        choices.append(alternatives)
    for combination in product(*choices):
        yield node(template_id, slot_name, *(child for child in combination if child is not None))


def oracle(catalog, *, min_capacity=0, launcher="any"):
    """Independent objective/constraint calculation over every tiny build."""
    result = []
    compatibility = Compatibility(catalog)
    for build in enumerate_trees(catalog):
        nodes = list(flatten(build))
        counts = Counter(part["template_id"] for part in nodes)
        if any(
            counts[conflict] > (1 if conflict == template_id else 0)
            for template_id in counts
            for conflict in compatibility.conflicts(template_id)
        ):
            continue
        props = [catalog.props(part["template_id"]) for part in nodes[1:] if catalog.is_mod(part["template_id"])]
        recoil = sum((Fraction(str(part.get("Recoil", 0))) for part in props), Fraction(0))
        base = Fraction(str(catalog.props("weapon")["Ergonomics"]))
        ergo_mod = sum((Fraction(str(part.get("Ergonomics", 0))) for part in props), Fraction(0))
        ergo_raw = base * (1 + ergo_mod / max(Fraction(1), base))
        ergo = min(Fraction(100), ergo_raw)
        magazine = next((part for part in build["children"] if part["slot"] == "mod_magazine"), None)
        capacity = 0
        if magazine is not None:
            magazine_id = magazine["template_id"]
            if catalog.is_a(magazine_id, CYLINDER):
                capacity = sum(parent_slot["_name"].startswith("camora_") for parent_slot in catalog.slots(magazine_id))
            else:
                capacity = catalog.props(magazine_id)["Cartridges"][0]["_max_count"]
        has_launcher = any(catalog.is_a(part["template_id"], LAUNCHER) for part in nodes[1:])
        if capacity < min_capacity or (launcher == "required" and not has_launcher) or (launcher == "forbidden" and has_launcher):
            continue
        result.append({
            "build": build, "recoil": recoil, "ergo": ergo,
            "ergo_raw": ergo_raw, "capacity": capacity,
            "has_launcher": has_launcher, "part_count": len(nodes) - 1,
        })
    return result


def tradeoff_catalog():
    return catalog_with(
        item("recoil", Recoil=-20, Ergonomics=-10),
        item("recoil_twin", Recoil=-20, Ergonomics=-10),
        item("middle", Recoil=-12, Ergonomics=-5),
        item("ergo", Recoil=-1, Ergonomics=10),
        item("dominated", Recoil=-10, Ergonomics=-10),
        weapon_slots=[slot("mod_stock", "recoil", "recoil_twin", "middle", "ergo", "dominated", required=True)],
    )


def combination_catalog():
    return catalog_with(
        item("receiver", Ergonomics=1, Slots=[slot("mod_muzzle", "brake", "balanced", required=True)]),
        item("brake", Recoil=-12, Ergonomics=-4, ConflictingItems=["launcher"]),
        item("balanced", Recoil=-4, Ergonomics=8),
        item("adapter", Recoil=10, Ergonomics=-15, Slots=[slot("mod_mount", "payback", required=True)]),
        item("payback", Recoil=-30, Ergonomics=35, ConflictingItems=["mag20"]),
        item("mag20", MAGAZINE, Ergonomics=2, Cartridges=[{"_max_count": 20}]),
        item("mag30", MAGAZINE, Ergonomics=-3, Cartridges=[{"_max_count": 30}]),
        item("launcher", LAUNCHER, Recoil=-8, Ergonomics=-20),
        item("small", Recoil=-1, Ergonomics=2),
        weapon_slots=[
            slot("mod_reciever", "receiver", "adapter", required=True),
            slot("mod_magazine", "mag20", "mag30"),
            slot("mod_launcher", "launcher"),
            slot("mod_tactical_000", "small"),
            slot("mod_tactical_001", "small"),
        ],
    )


class EvaluateBuildTests(unittest.TestCase):
    def test_template_formula_counts_mod_descendants_only(self):
        catalog = catalog_with(
            item("mount", Recoil=-10, Ergonomics=6, Slots=[slot("mod_muzzle", "brake", required=True)]),
            item("brake", Recoil=-5, Ergonomics=4),
            item("ordinary", ITEM, Recoil=-80, Ergonomics=90),
            weapon_slots=[slot("mod_mount", "mount", required=True), slot("mod_equipment", "ordinary")],
        )
        build = node("weapon", None, node("mount", "mod_mount", node("brake", "mod_muzzle")), node("ordinary", "mod_equipment"))
        stats = evaluate_build(catalog, build)
        self.assertEqual(stats["recoil_modifier_percent"], -15)
        self.assertAlmostEqual(stats["recoil_vertical"], 85)
        self.assertAlmostEqual(stats["recoil_horizontal"], 170)
        self.assertAlmostEqual(stats["recoil_total"], 255)
        self.assertAlmostEqual(stats["ergonomics_raw"], 60)
        self.assertEqual(stats["part_count"], 3)
        self.assertEqual(stats["magazine_capacity"], 0)
        self.assertFalse(stats["has_launcher"])

    def test_ergo_benefit_caps_but_raw_and_recoil_do_not(self):
        catalog = catalog_with(item("part", Recoil=-120, Ergonomics=80), weapon_slots=[slot("mod_stock", "part")])
        stats = evaluate_build(catalog, node("weapon", None, node("part", "mod_stock")))
        self.assertAlmostEqual(stats["ergonomics_raw"], 130)
        self.assertEqual(stats["ergonomics_effective"], 100)
        self.assertAlmostEqual(stats["recoil_total"], -60)

    def test_base_ergo_below_one_uses_client_denominator(self):
        for base, expected in ((0.5, 1.5), (0, 0), (1, 3), (-158, -474)):
            with self.subTest(base=base):
                catalog = catalog_with(item("part", Ergonomics=2), weapon_slots=[slot("mod_stock", "part")], base_ergo=base)
                stats = evaluate_build(catalog, node("weapon", None, node("part", "mod_stock")))
                self.assertAlmostEqual(stats["ergonomics_raw"], expected)

    def test_negative_ergo_is_not_clamped_to_zero(self):
        catalog = catalog_with(item("part", Ergonomics=-70), weapon_slots=[slot("mod_stock", "part")])
        stats = evaluate_build(catalog, node("weapon", None, node("part", "mod_stock")))
        self.assertEqual(stats["ergonomics_raw"], -20)
        self.assertEqual(stats["ergonomics_effective"], -20)

    def test_repeated_template_in_different_slots_counts_each_instance(self):
        catalog = catalog_with(item("part", Recoil=-2, Ergonomics=3), weapon_slots=[slot("left", "part"), slot("right", "part")])
        stats = evaluate_build(catalog, node("weapon", None, node("part", "left"), node("part", "right")))
        self.assertEqual(stats["part_count"], 2)
        self.assertEqual(stats["recoil_modifier_percent"], -4)
        self.assertAlmostEqual(stats["ergonomics_raw"], 56)

    def test_required_slots_exist_only_with_their_parent(self):
        catalog = catalog_with(
            item("parent", Slots=[slot("child", "leaf", required=True)]), item("leaf"),
            weapon_slots=[slot("parent", "parent")],
        )
        evaluate_build(catalog, node("weapon"))
        with self.assertRaisesRegex(ValueError, "required"):
            evaluate_build(catalog, node("weapon", None, node("parent", "parent")))
        evaluate_build(catalog, node("weapon", None, node("parent", "parent", node("leaf", "child"))))

    def test_root_required_slot_cannot_be_omitted(self):
        catalog = catalog_with(item("part"), weapon_slots=[slot("required", "part", required=True)])
        with self.assertRaisesRegex(ValueError, "required"):
            evaluate_build(catalog, node("weapon"))

    def test_orphan_unknown_slot_and_duplicate_occupancy_are_rejected(self):
        catalog = catalog_with(item("part"), item("other"), weapon_slots=[slot("known", "part")])
        for build in (
            node("weapon", None, node("part", "orphan")),
            node("weapon", None, node("part", "known"), node("part", "known")),
            node("weapon", None, node("other", "known")),
        ):
            with self.subTest(build=build), self.assertRaises(ValueError):
                evaluate_build(catalog, build)

    def test_ammo_chambers_and_cartridges_are_not_attachment_slots(self):
        catalog = catalog_with(item("round", AMMO))
        catalog.props("weapon")["Chambers"] = [slot("patron_in_weapon", "round")]
        for ammo_slot in ("patron_in_weapon", "cartridges"):
            with self.subTest(ammo_slot=ammo_slot), self.assertRaises(ValueError):
                evaluate_build(catalog, node("weapon", None, node("round", ammo_slot)))

    def test_rounds_in_regular_cylinder_slots_are_also_rejected(self):
        catalog = catalog_with(
            item("round", AMMO),
            item("cylinder", CYLINDER, Cartridges=[], Slots=[slot(f"camora_{index:03}", "round") for index in range(5)]),
            weapon_slots=[slot("mod_magazine", "cylinder", required=True)],
        )
        evaluate_build(catalog, node("weapon", None, node("cylinder", "mod_magazine")))
        with self.assertRaisesRegex(ValueError, "unloaded"):
            evaluate_build(catalog, node("weapon", None, node("cylinder", "mod_magazine", node("round", "camora_000"))))

    def test_invalid_root_unknown_template_and_malformed_tree_are_rejected(self):
        catalog = catalog_with(item("part"))
        bad_builds = [
            None, node("missing"), node("part"), node("weapon", "not_root"),
            {"template_id": "weapon", "slot": None},
            {"template_id": "weapon", "slot": None, "children": {}},
            node("weapon", None, None),
            {"template_id": "weapon", "slot": None, "children": [], "child": node("part")},
            {"template_id": "weapon", "slot": None, "children": [], "Chambers": [node("part")]},
            {"template_id": "weapon", "slot": None, "children": [], "Cartridges": [node("part")]},
        ]
        for build in bad_builds:
            with self.subTest(build=build), self.assertRaises(ValueError):
                evaluate_build(catalog, build)

    def test_empty_filter_rejects_and_category_exclusions_apply(self):
        empty = slot("empty")
        broad = slot("broad", MOD)
        broad["_props"]["filters"][0]["ExcludedFilter"] = [LAUNCHER]
        catalog = catalog_with(item("part"), item("launcher", LAUNCHER), weapon_slots=[empty, broad])
        evaluate_build(catalog, node("weapon", None, node("part", "broad")))
        for build in (
            node("weapon", None, node("part", "empty")),
            node("weapon", None, node("launcher", "broad")),
        ):
            with self.subTest(build=build), self.assertRaises(ValueError):
                evaluate_build(catalog, build)

    def test_outer_filter_entries_intersect_instead_of_union(self):
        filtered = slot("filtered", MOD)
        filtered["_props"]["filters"].append({"Filter": ["one"]})
        catalog = catalog_with(item("one"), item("two"), weapon_slots=[filtered])
        evaluate_build(catalog, node("weapon", None, node("one", "filtered")))
        with self.assertRaises(ValueError):
            evaluate_build(catalog, node("weapon", None, node("two", "filtered")))

    def test_empty_outer_filter_is_unrestricted_but_unknown_ids_are_not(self):
        unrestricted = slot("free")
        unrestricted["_props"]["filters"] = []
        unknown = slot("unknown", "does_not_exist")
        catalog = catalog_with(item("part"), weapon_slots=[unrestricted, unknown])
        evaluate_build(catalog, node("weapon", None, node("part", "free")))
        with self.assertRaises(ValueError):
            evaluate_build(catalog, node("weapon", None, node("part", "unknown")))

    def test_reverse_and_deep_conflicts_are_global(self):
        for conflict_on in ("leaf", "other"):
            with self.subTest(conflict_on=conflict_on):
                leaf = item("leaf", ConflictingItems=["other"] if conflict_on == "leaf" else [])
                other = item("other", ConflictingItems=["leaf"] if conflict_on == "other" else [])
                catalog = catalog_with(
                    item("parent", Slots=[slot("deep", "leaf", required=True)]), leaf, other,
                    weapon_slots=[slot("parent", "parent"), slot("other", "other")],
                )
                build = node("weapon", None, node("parent", "parent", node("leaf", "deep")), node("other", "other"))
                with self.assertRaisesRegex(ValueError, "conflict"):
                    evaluate_build(catalog, build)

    def test_root_and_child_conflicts_apply_in_both_directions(self):
        for conflict_on in ("weapon", "part"):
            with self.subTest(conflict_on=conflict_on):
                catalog = catalog_with(item("part"), weapon_slots=[slot("part", "part")])
                catalog.props(conflict_on)["ConflictingItems"] = ["part" if conflict_on == "weapon" else "weapon"]
                with self.assertRaisesRegex(ValueError, "conflict"):
                    evaluate_build(catalog, node("weapon", None, node("part", "part")))

    def test_conflicting_items_are_exact_templates_not_categories(self):
        catalog = catalog_with(
            item("part", ConflictingItems=[LAUNCHER]), item("launcher", LAUNCHER),
            weapon_slots=[slot("part", "part"), slot("launcher", "launcher")],
        )
        stats = evaluate_build(catalog, node("weapon", None, node("part", "part"), node("launcher", "launcher")))
        self.assertTrue(stats["has_launcher"])

    def test_self_conflict_rejects_two_instances_not_one(self):
        catalog = catalog_with(item("part", ConflictingItems=["part"]), weapon_slots=[slot("a", "part"), slot("b", "part")])
        evaluate_build(catalog, node("weapon", None, node("part", "a")))
        with self.assertRaisesRegex(ValueError, "conflict"):
            evaluate_build(catalog, node("weapon", None, node("part", "a"), node("part", "b")))

    def test_capacity_uses_only_root_magazine_and_first_cartridge_stack(self):
        catalog = catalog_with(
            item("mag20", MAGAZINE, Cartridges=[{"_max_count": 20}, {"_max_count": 900}]),
            item("mag100", MAGAZINE, Cartridges=[{"_max_count": 100}]),
            item("mount", Slots=[slot("mod_magazine", "mag100", required=True)]),
            weapon_slots=[slot("mod_magazine", "mag20"), slot("mod_mount", "mount")],
        )
        nested = node("mount", "mod_mount", node("mag100", "mod_magazine"))
        stats = evaluate_build(catalog, node("weapon", None, node("mag20", "mod_magazine"), nested))
        self.assertEqual(stats["magazine_capacity"], 20)
        self.assertEqual(evaluate_build(catalog, node("weapon", None, nested))["magazine_capacity"], 0)

    def test_cylinder_and_spring_cylinder_capacity_counts_camora_slots(self):
        for category, cartridges in ((CYLINDER, []), (SPRING_CYLINDER, [{"_max_count": 999}])):
            with self.subTest(category=category):
                catalog = catalog_with(
                    item("round", AMMO), item("part"),
                    item("cylinder", category, Cartridges=cartridges, Slots=[
                        *(slot(f"camora_{index:03}", "round") for index in range(5)),
                        slot("other_slot", "part"),
                    ]),
                    weapon_slots=[slot("mod_magazine", "cylinder", required=True)],
                )
                stats = evaluate_build(catalog, node("weapon", None, node("cylinder", "mod_magazine")))
                self.assertEqual(stats["magazine_capacity"], 5)

    def test_launcher_linked_weapon_is_not_implicitly_attached(self):
        linked = item("linked", WEAPON, Ergonomics=999, RecoilForceUp=999, RecoilForceBack=999, Slots=[slot("vital", "unavailable", required=True)])
        catalog = catalog_with(
            item("launcher", LAUNCHER, LinkedWeapon="linked", Recoil=-8, Ergonomics=-35), linked,
            weapon_slots=[slot("mod_launcher", "launcher")],
        )
        stats = evaluate_build(catalog, node("weapon", None, node("launcher", "mod_launcher")))
        self.assertTrue(stats["has_launcher"])
        self.assertEqual(stats["part_count"], 1)
        self.assertEqual(stats["recoil_modifier_percent"], -8)
        self.assertAlmostEqual(stats["ergonomics_raw"], 15)

    def test_cyclic_build_objects_raise_value_error(self):
        catalog = catalog_with(item("part", Slots=[slot("joint", "part")]), weapon_slots=[slot("joint", "part")])
        part = node("part", "joint")
        part["children"].append(part)
        with self.assertRaisesRegex(ValueError, "cycle"):
            evaluate_build(catalog, node("weapon", None, part))

    def test_evaluation_does_not_mutate_catalog_or_build(self):
        catalog = combination_catalog()
        build = next(enumerate_trees(catalog))
        items_before, build_before = deepcopy(catalog.items), deepcopy(build)
        evaluate_build(catalog, build)
        self.assertEqual(catalog.items, items_before)
        self.assertEqual(build, build_before)


class OptimizerBehaviorTests(unittest.TestCase):
    def assert_solution_is_valid(self, catalog, solution, *, min_capacity=0, launcher="any"):
        stats = evaluate_build(catalog, solution["build"])
        for key in (
            "recoil_modifier_percent", "recoil_vertical", "recoil_horizontal", "recoil_total",
            "ergonomics_raw", "ergonomics_effective", "magazine_capacity", "has_launcher", "part_count",
        ):
            with self.subTest(stat=key):
                if isinstance(stats[key], float):
                    self.assertAlmostEqual(solution["stats"][key], stats[key])
                else:
                    self.assertEqual(solution["stats"][key], stats[key])
        self.assertGreaterEqual(stats["magazine_capacity"], min_capacity)
        if launcher != "any":
            self.assertEqual(stats["has_launcher"], launcher == "required")
        return stats

    def test_top_k_matches_exhaustive_oracle_for_all_objectives(self):
        catalog = combination_catalog()
        for objective, wr, we in (("weighted", "1", "1"), ("weighted", "1/3", "2/7"), ("recoil", "1", "1"), ("ergo", "1", "1")):
            with self.subTest(objective=objective, recoil_weight=wr, ergo_weight=we):
                result = optimize(catalog, "weapon", objective=objective, recoil_weight=wr, ergo_weight=we, limit=7)
                self.assertEqual(result["status"], "optimal")
                expected = oracle(catalog)
                key = {
                    "weighted": lambda row: Fraction(wr) * -row["recoil"] + Fraction(we) * row["ergo"],
                    "recoil": lambda row: -row["recoil"],
                    "ergo": lambda row: row["ergo"],
                }[objective]
                expected_scores = sorted((key(row) for row in expected), reverse=True)[:7]
                actual_scores = []
                valid_fingerprints = {fingerprint(row["build"]) for row in expected}
                actual_fingerprints = []
                for solution in result["solutions"]:
                    stats = self.assert_solution_is_valid(catalog, solution)
                    self.assertEqual(solution["proof"]["status"], "optimal")
                    row = {"recoil": Fraction(str(stats["recoil_modifier_percent"])), "ergo": Fraction(str(stats["ergonomics_effective"]))}
                    actual_scores.append(key(row))
                    weighted_score = Fraction(wr) * -row["recoil"] + Fraction(we) * row["ergo"]
                    self.assertAlmostEqual(solution["score"], float(weighted_score))
                    actual_fingerprints.append(fingerprint(solution["build"]))
                self.assertEqual(actual_scores, expected_scores)
                self.assertEqual(len(set(actual_fingerprints)), len(actual_fingerprints))
                self.assertTrue(set(actual_fingerprints) <= valid_fingerprints)

    def test_capacity_boundary_and_launcher_constraints_match_oracle(self):
        catalog = combination_catalog()
        for capacity, launcher in product((0, 20, 30, 31), ("any", "required", "forbidden")):
            with self.subTest(capacity=capacity, launcher=launcher):
                expected = oracle(catalog, min_capacity=capacity, launcher=launcher)
                result = optimize(catalog, "weapon", min_capacity=capacity, launcher=launcher, limit=2)
                if not expected:
                    self.assertEqual(result["status"], "infeasible")
                    self.assertEqual(result["solutions"], [])
                    continue
                self.assertEqual(result["status"], "optimal")
                scores = sorted((-row["recoil"] + row["ergo"] for row in expected), reverse=True)[:2]
                actual = []
                for solution in result["solutions"]:
                    stats = self.assert_solution_is_valid(catalog, solution, min_capacity=capacity, launcher=launcher)
                    actual.append(-stats["recoil_modifier_percent"] + stats["ergonomics_effective"])
                self.assertEqual(actual, [float(score) for score in scores])

    def test_all_distinct_tied_builds_are_returned_without_repetition(self):
        catalog = catalog_with(item("a", Recoil=-5), item("b", Recoil=-5), weapon_slots=[slot("choice", "a", "b", required=True)])
        result = optimize(catalog, "weapon", limit=5)
        self.assertEqual(result["status"], "optimal")
        builds = [solution["build"] for solution in result["solutions"]]
        self.assertEqual(len(builds), 2)
        self.assertEqual({build["children"][0]["template_id"] for build in builds}, {"a", "b"})
        self.assertEqual(len({fingerprint(build) for build in builds}), 2)
        self.assertTrue(result["search"]["exhausted"])
        for solution in result["solutions"]:
            self.assert_solution_is_valid(catalog, solution)

    def test_lower_rank_optimum_is_not_labeled_original_global_optimum(self):
        catalog = catalog_with(
            item("a", Recoil=-5), item("b", Recoil=-5), item("c", Recoil=-1),
            weapon_slots=[slot("choice", "a", "b", "c", required=True)],
        )
        result = optimize(catalog, "weapon", limit=3)
        solutions = result["solutions"]
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(len(solutions), 3)
        self.assertEqual([solution["proof"]["original_global_optimum"] for solution in solutions], [True, True, False])
        self.assertEqual([solution["proof"]["scope"] for solution in solutions], ["all_builds", "remaining_builds", "remaining_builds"])
        self.assertGreater(solutions[1]["score"], solutions[2]["score"])

    def test_expired_deadline_is_unknown_not_a_claim_of_optimality(self):
        result = optimize(combination_catalog(), "weapon", time_limit=1e-12, limit=3)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["solutions"], [])
        self.assertFalse(result["search"]["ranking_proven"])

    def test_pareto_includes_unsupported_points_once_and_in_order(self):
        catalog = tradeoff_catalog()
        rows = oracle(catalog)
        points = {(row["recoil"], row["ergo"]) for row in rows}
        expected = sorted(point for point in points if not any(
            other[0] <= point[0] and other[1] >= point[1] and other != point for other in points
        ))
        result = optimize(catalog, "weapon", mode="pareto", limit=10)
        self.assertEqual(result["status"], "optimal")
        actual = []
        for solution in result["solutions"]:
            stats = self.assert_solution_is_valid(catalog, solution)
            self.assertEqual(solution["proof"]["status"], "optimal")
            actual.append((stats["recoil_modifier_percent"], stats["ergonomics_effective"]))
        self.assertEqual(actual, [(float(recoil), float(ergo)) for recoil, ergo in expected])
        self.assertEqual(actual, [(-20, 40), (-12, 45), (-1, 60)])
        self.assertTrue(result["search"]["exhausted"])

    def test_same_template_can_be_optimal_in_two_slots(self):
        catalog = catalog_with(item("part", Recoil=-3, Ergonomics=2), weapon_slots=[slot("left", "part"), slot("right", "part")])
        result = optimize(catalog, "weapon", limit=1)
        stats = self.assert_solution_is_valid(catalog, result["solutions"][0])
        self.assertEqual(stats["part_count"], 2)
        self.assertEqual(stats["recoil_modifier_percent"], -6)
        self.assertEqual(Counter(part["template_id"] for part in result["solutions"][0]["build"]["children"]), {"part": 2})

    def test_parent_loss_does_not_prune_a_profitable_required_subtree(self):
        catalog = catalog_with(
            item("plain", Recoil=-5, Ergonomics=5),
            item("adapter", Recoil=10, Ergonomics=-10, Slots=[slot("deep", "payoff", required=True)]),
            item("payoff", Recoil=-30, Ergonomics=30),
            weapon_slots=[slot("choice", "plain", "adapter", required=True)],
        )
        result = optimize(catalog, "weapon", limit=1)
        stats = self.assert_solution_is_valid(catalog, result["solutions"][0])
        self.assertEqual(result["solutions"][0]["build"]["children"][0]["template_id"], "adapter")
        self.assertEqual(stats["recoil_modifier_percent"], -20)
        self.assertAlmostEqual(stats["ergonomics_raw"], 70)

    def test_descendant_compatibility_uses_the_selected_parent(self):
        catalog = catalog_with(
            item("parent_a", Recoil=-10, Slots=[slot("shared", "leaf_a", required=True)]),
            item("parent_b", Slots=[slot("shared", "leaf_b", required=True)]),
            item("leaf_a"), item("leaf_b", Recoil=-30),
            weapon_slots=[slot("choice", "parent_a", "parent_b", required=True)],
        )
        result = optimize(catalog, "weapon", limit=1)
        build = result["solutions"][0]["build"]
        stats = self.assert_solution_is_valid(catalog, result["solutions"][0])
        self.assertEqual(build["children"][0]["template_id"], "parent_b")
        self.assertEqual(build["children"][0]["children"][0]["template_id"], "leaf_b")
        self.assertEqual(stats["recoil_modifier_percent"], -30)

    def test_required_slot_depends_on_which_parent_occupies_the_shared_path(self):
        catalog = catalog_with(
            item("parent_a", Recoil=-10, Slots=[slot("shared", "leaf_a", required=True)]),
            item("parent_b", Recoil=-1, Slots=[slot("shared", "leaf_b")]),
            item("leaf_a", Ergonomics=-80), item("leaf_b", Ergonomics=-5),
            weapon_slots=[slot("choice", "parent_a", "parent_b", required=True)],
        )
        result = optimize(catalog, "weapon", limit=1)
        parent = result["solutions"][0]["build"]["children"][0]
        self.assertEqual(parent["template_id"], "parent_b")
        self.assertEqual(parent["children"], [])
        self.assert_solution_is_valid(catalog, result["solutions"][0])

    def test_optional_parent_does_not_require_its_absent_child(self):
        catalog = catalog_with(
            item("parent", Ergonomics=-5, Slots=[slot("child", "leaf", required=True)]),
            item("leaf", Ergonomics=1), weapon_slots=[slot("parent", "parent")],
        )
        result = optimize(catalog, "weapon", limit=1)
        self.assertEqual(result["solutions"][0]["build"]["children"], [])
        self.assert_solution_is_valid(catalog, result["solutions"][0])

    def test_beyond_ergo_cap_does_not_beat_recoil_or_add_dead_weight(self):
        catalog = catalog_with(
            item("cap", Ergonomics=60), item("excess", Ergonomics=80, Recoil=1),
            item("neutral"),
            weapon_slots=[slot("choice", "cap", "excess", required=True), slot("extra", "neutral")],
        )
        result = optimize(catalog, "weapon", limit=1)
        stats = self.assert_solution_is_valid(catalog, result["solutions"][0])
        self.assertEqual(stats["ergonomics_effective"], 100)
        self.assertEqual(stats["recoil_modifier_percent"], 0)
        self.assertEqual(stats["part_count"], 1)
        self.assertEqual(result["solutions"][0]["build"]["children"][0]["template_id"], "cap")

    def test_low_base_ergo_uses_scaled_gain_in_optimization(self):
        catalog = catalog_with(
            item("recoil", Recoil=-1), item("ergo", Ergonomics=1.5),
            weapon_slots=[slot("choice", "recoil", "ergo", required=True)], base_ergo=0.5,
        )
        result = optimize(catalog, "weapon", limit=1)
        self.assertEqual(result["solutions"][0]["build"]["children"][0]["template_id"], "recoil")
        self.assert_solution_is_valid(catalog, result["solutions"][0])

    def test_negative_base_ergo_matches_client_formula_in_search_and_pareto(self):
        catalog = catalog_with(
            item("drag", Ergonomics=-2), item("bonus", Ergonomics=1, Recoil=-20),
            weapon_slots=[slot("choice", "drag", "bonus", required=True)], base_ergo=-158,
        )
        for objective in ("ergo", "weighted"):
            with self.subTest(objective=objective):
                result = optimize(catalog, "weapon", objective=objective, limit=1)
                solution = result["solutions"][0]
                self.assertEqual(solution["build"]["children"][0]["template_id"], "drag")
                self.assertEqual(solution["stats"]["ergonomics_raw"], 158)
                self.assertEqual(solution["stats"]["ergonomics_effective"], 100)
        front = optimize(catalog, "weapon", mode="pareto", limit=10)
        self.assertTrue(front["search"]["exhausted"])
        self.assertEqual(
            [(s["stats"]["recoil_modifier_percent"], s["stats"]["ergonomics_effective"]) for s in front["solutions"]],
            [(-20, -316), (0, 100)],
        )

    def test_small_decimal_stat_advantages_are_not_rounded_away(self):
        catalog = catalog_with(
            item("recoil", Recoil=-0.0004), item("ergo", Ergonomics=0.0003),
            weapon_slots=[slot("choice", "recoil", "ergo", required=True)],
        )
        result = optimize(catalog, "weapon", limit=1)
        self.assertEqual(result["solutions"][0]["build"]["children"][0]["template_id"], "recoil")
        stats = self.assert_solution_is_valid(catalog, result["solutions"][0])
        self.assertEqual(stats["recoil_modifier_percent"], -0.0004)

    def test_root_capacity_does_not_use_a_large_nested_magazine(self):
        catalog = catalog_with(
            item("small", MAGAZINE, Cartridges=[{"_max_count": 20}, {"_max_count": 900}]),
            item("large", MAGAZINE, Cartridges=[{"_max_count": 100}]),
            item("mount", Slots=[slot("mod_magazine", "large", required=True)]),
            weapon_slots=[slot("mod_magazine", "small", required=True), slot("mod_mount", "mount", required=True)],
        )
        self.assertEqual(optimize(catalog, "weapon", min_capacity=20, limit=1)["status"], "optimal")
        result = optimize(catalog, "weapon", min_capacity=21, limit=1)
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["solutions"], [])

    def test_nested_launcher_satisfies_required_and_can_be_forbidden(self):
        catalog = catalog_with(
            item("mount", Slots=[slot("mod_launcher", "launcher")]),
            item("launcher", LAUNCHER, Recoil=-8, Ergonomics=-35, LinkedWeapon="linked"),
            item("linked", WEAPON, RecoilForceUp=999, RecoilForceBack=999, Ergonomics=999),
            weapon_slots=[slot("mod_mount", "mount")],
        )
        for launcher in ("required", "forbidden"):
            with self.subTest(launcher=launcher):
                result = optimize(catalog, "weapon", launcher=launcher, limit=1)
                self.assertEqual(result["status"], "optimal")
                stats = self.assert_solution_is_valid(catalog, result["solutions"][0], launcher=launcher)
                self.assertEqual(stats["part_count"], 2 if launcher == "required" else 0)

    def test_required_empty_filter_has_no_solution(self):
        catalog = catalog_with(item("irrelevant", Ergonomics=999), weapon_slots=[slot("impossible", required=True)])
        result = optimize(catalog, "weapon", limit=3)
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["solutions"], [])

    def test_deep_reverse_conflict_forces_the_compatible_alternative(self):
        for conflicting_template in ("leaf", "other"):
            with self.subTest(conflicting_template=conflicting_template):
                catalog = catalog_with(
                    item("parent", Slots=[slot("deep", "leaf", required=True)]),
                    item("leaf", Recoil=-20, ConflictingItems=["other"] if conflicting_template == "leaf" else []),
                    item("other", ConflictingItems=["leaf"] if conflicting_template == "other" else []),
                    item("safe", Recoil=-1),
                    weapon_slots=[slot("choice", "parent", "safe", required=True), slot("other", "other", required=True)],
                )
                result = optimize(catalog, "weapon", limit=1)
                stats = self.assert_solution_is_valid(catalog, result["solutions"][0])
                self.assertEqual(stats["recoil_modifier_percent"], -1)
                self.assertNotIn("leaf", [part["template_id"] for part in flatten(result["solutions"][0]["build"])])

    def test_root_conflicts_exclude_a_better_attachment(self):
        for conflict_on in ("weapon", "best"):
            with self.subTest(conflict_on=conflict_on):
                catalog = catalog_with(
                    item("best", Recoil=-20), item("safe", Recoil=-1),
                    weapon_slots=[slot("choice", "best", "safe", required=True)],
                )
                catalog.props(conflict_on)["ConflictingItems"] = ["best" if conflict_on == "weapon" else "weapon"]
                result = optimize(catalog, "weapon", limit=1)
                stats = self.assert_solution_is_valid(catalog, result["solutions"][0])
                self.assertEqual(stats["recoil_modifier_percent"], -1)

    def test_reachable_template_cycle_is_explicitly_rejected(self):
        catalog = catalog_with(
            item("a", Slots=[slot("next", "b")]), item("b", Slots=[slot("next", "a")]),
            weapon_slots=[slot("entry", "a")],
        )
        with self.assertRaisesRegex(ValueError, "(?i)cycl"):
            optimize(catalog, "weapon", limit=1)

    def test_unloaded_top_k_does_not_enumerate_optional_cylinder_rounds(self):
        catalog = catalog_with(
            item("round", AMMO),
            item("cylinder", CYLINDER, Cartridges=[], Slots=[slot(f"camora_{index:03}", "round") for index in range(5)]),
            weapon_slots=[slot("mod_magazine", "cylinder", required=True)],
        )
        expected = oracle(catalog)
        result = optimize(catalog, "weapon", limit=10)
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(len(result["solutions"]), len(expected))
        self.assertEqual(len(result["solutions"]), 1)
        self.assertTrue(result["search"]["exhausted"])
        stats = self.assert_solution_is_valid(catalog, result["solutions"][0])
        self.assertEqual(stats["magazine_capacity"], 5)
        self.assertEqual(stats["part_count"], 1)

    def test_cylinder_capacity_constraint_has_an_inclusive_boundary(self):
        for category in (CYLINDER, SPRING_CYLINDER):
            with self.subTest(category=category):
                catalog = catalog_with(
                    item("round", AMMO),
                    item("cylinder", category, Cartridges=[], Slots=[slot(f"camora_{index:03}", "round") for index in range(5)]),
                    weapon_slots=[slot("mod_magazine", "cylinder", required=True)],
                )
                at_boundary = optimize(catalog, "weapon", min_capacity=5, limit=1)
                self.assertEqual(at_boundary["status"], "optimal")
                self.assert_solution_is_valid(catalog, at_boundary["solutions"][0], min_capacity=5)
                over_boundary = optimize(catalog, "weapon", min_capacity=6, limit=1)
                self.assertEqual(over_boundary["status"], "infeasible")
                self.assertEqual(over_boundary["solutions"], [])

    def test_required_ammunition_slot_is_not_silently_ignored(self):
        catalog = catalog_with(
            item("round", AMMO),
            item("cylinder", CYLINDER, Cartridges=[], Slots=[slot(f"camora_{index:03}", "round", required=True) for index in range(5)]),
            weapon_slots=[slot("mod_magazine", "cylinder", required=True)],
        )
        self.assertEqual(oracle(catalog), [])
        result = optimize(catalog, "weapon", limit=1)
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["solutions"], [])

    def test_single_objective_breaks_ties_with_the_other_dimension(self):
        catalog = catalog_with(
            item("a", Recoil=-10, Ergonomics=3), item("b", Recoil=-10, Ergonomics=8),
            item("c", Recoil=-5, Ergonomics=8),
            weapon_slots=[slot("choice", "a", "b", "c", required=True)],
        )
        for objective in ("recoil", "ergo"):
            with self.subTest(objective=objective):
                result = optimize(catalog, "weapon", objective=objective, limit=1)
                self.assertEqual(result["solutions"][0]["build"]["children"][0]["template_id"], "b")
                self.assert_solution_is_valid(catalog, result["solutions"][0])

    def test_optimization_does_not_mutate_input_templates(self):
        catalog = combination_catalog()
        before = deepcopy(catalog.items)
        optimize(catalog, "weapon", limit=1)
        self.assertEqual(catalog.items, before)

    def test_zero_base_recoil_gives_no_benefit_to_modifier_only_improvements(self):
        catalog = catalog_with(
            item("recoil", Recoil=-90, Ergonomics=-10), item("ergo", Ergonomics=5),
            weapon_slots=[slot("choice", "recoil", "ergo", required=True)],
        )
        catalog.props("weapon").update(RecoilForceUp=0, RecoilForceBack=0)
        for objective in ("recoil", "ergo", "weighted"):
            with self.subTest(objective=objective):
                result = optimize(catalog, "weapon", objective=objective, limit=1)
                solution = result["solutions"][0]
                self.assertEqual(result["status"], "optimal")
                self.assertEqual(solution["build"]["children"][0]["template_id"], "ergo")
                self.assertEqual(solution["stats"]["recoil_total"], 0)
                self.assertEqual(solution["score"], 55)
        front = optimize(catalog, "weapon", mode="pareto", limit=10)
        self.assertEqual(len(front["solutions"]), 1)
        self.assertTrue(front["search"]["exhausted"])

    def test_signed_base_recoil_orders_by_actual_recoil_total(self):
        catalog = catalog_with(
            item("lower", Recoil=20), item("higher", Recoil=-20),
            weapon_slots=[slot("choice", "lower", "higher", required=True)],
        )
        catalog.props("weapon").update(RecoilForceUp=-100, RecoilForceBack=-200)
        for objective in ("recoil", "ergo", "weighted"):
            with self.subTest(objective=objective):
                solution = optimize(catalog, "weapon", objective=objective, limit=1)["solutions"][0]
                self.assertEqual(solution["build"]["children"][0]["template_id"], "lower")
                self.assertEqual(solution["stats"]["recoil_total"], -360)
                self.assertEqual(solution["score"], 70)


def clone_evidence(catalog, *aliases, excluded=()):
    return {
        "schema_version": 1,
        "catalog_sha256": _digest(catalog.items),
        "clone_rules": [{
            "kind": "incoming-clone-conflicts",
            "aliases": [{"source_id": source, "clone_id": clone} for source, clone in aliases],
            "excluded_owners": list(excluded),
        }],
    }


def color_stock(template_id, prefab, **props):
    return item(template_id, Prefab={"path": prefab, "rcid": ""}, Recoil=-4, Ergonomics=2, **props)


class CompatibilityBehaviorTests(unittest.TestCase):
    def test_compatibility_captured_for_different_properties_is_rejected(self):
        catalog = catalog_with(item("original"), item("clone"), weapon_slots=[slot("stock", "clone")])
        catalog.compatibility = clone_evidence(catalog, ("original", "clone"))
        catalog.props("clone")["Recoil"] = -1
        with self.assertRaisesRegex(DataError, "items snapshot"):
            evaluate_build(catalog, node("weapon", None, node("clone", "stock")))
        with self.assertRaisesRegex(DataError, "items snapshot"):
            optimize(catalog, "weapon", limit=1)

    def test_variant_property_order_difference_is_not_normalized(self):
        base = color_stock("base", "stocks/mini.bundle", ConflictingItems=["pad"])
        variant = color_stock("variant", "stocks/mini_red.bundle")
        variant["_props"] = dict(reversed(list(variant["_props"].items())))
        catalog = catalog_with(base, variant, item("pad"), weapon_slots=[slot("stock", "variant"), slot("pad", "pad")])
        stats = evaluate_build(catalog, node("weapon", None, node("variant", "stock"), node("pad", "pad")))
        self.assertEqual(stats["recoil_modifier_percent"], -4)

    def test_late_loaded_conflict_expands_to_clone_and_clone_of_clone(self):
        catalog = catalog_with(
            item("original"), item("clone", Recoil=-10), item("clone2", Recoil=-20),
            item("late", ConflictingItems=["original"]), item("safe", Recoil=-1),
            weapon_slots=[slot("stock", "clone", "clone2", "safe", required=True), slot("late", "late", required=True)],
        )
        catalog.compatibility = clone_evidence(catalog, ("original", "clone"), ("clone", "clone2"))
        for template_id in ("clone", "clone2"):
            with self.subTest(template_id=template_id), self.assertRaisesRegex(ValueError, "conflict"):
                evaluate_build(catalog, node("weapon", None, node(template_id, "stock"), node("late", "late")))
        result = optimize(catalog, "weapon", limit=1)
        self.assertEqual({part["template_id"] for part in result["solutions"][0]["build"]["children"]}, {"late", "safe"})
        self.assertEqual(result["solutions"][0]["stats"]["recoil_modifier_percent"], -1)

    def test_clones_without_propagation_evidence_do_not_inherit_conflicts(self):
        catalog = catalog_with(
            item("original"), item("clone", Recoil=-10), item("late", ConflictingItems=["original"]),
            weapon_slots=[slot("stock", "clone", required=True), slot("late", "late", required=True)],
        )
        stats = evaluate_build(catalog, node("weapon", None, node("clone", "stock"), node("late", "late")))
        self.assertEqual(stats["recoil_modifier_percent"], -10)
        self.assertEqual(optimize(catalog, "weapon", limit=1)["status"], "optimal")

    def test_clone_propagation_preserves_consumer_owner_exclusions(self):
        catalog = catalog_with(
            item("original"), item("clone"), item("owner", ConflictingItems=["original"]),
            weapon_slots=[slot("stock", "clone", required=True), slot("owner", "owner", required=True)],
        )
        catalog.compatibility = clone_evidence(catalog, ("original", "clone"), excluded=("owner",))
        evaluate_build(catalog, node("weapon", None, node("clone", "stock"), node("owner", "owner")))
        self.assertEqual(optimize(catalog, "weapon", limit=1)["status"], "optimal")

    def test_color_variants_union_missing_incoming_and_outgoing_conflicts(self):
        catalog = catalog_with(
            color_stock("black", "stocks/mini.bundle", ConflictingItems=["pad"]),
            color_stock("yellow", "stocks/mini_ylw.bundle"),
            color_stock("red", "stocks/mini_red.bundle"), item("pad", Recoil=-2),
            item("blocker", ConflictingItems=["black"]),
            weapon_slots=[slot("stock", "yellow", "red"), slot("pad", "pad"), slot("blocker", "blocker")],
        )
        for variant, other in product(("yellow", "red"), ("pad", "blocker")):
            with self.subTest(variant=variant, other=other), self.assertRaisesRegex(ValueError, "conflict"):
                evaluate_build(catalog, node("weapon", None, node(variant, "stock"), node(other, other)))
        for solution in optimize(catalog, "weapon", limit=8)["solutions"]:
            evaluate_build(catalog, solution["build"])
            installed = {part["template_id"] for part in flatten(solution["build"])}
            self.assertFalse(installed & {"yellow", "red"} and installed & {"pad", "blocker"})

    def test_noncosmetic_changes_or_missing_base_do_not_merge_variants(self):
        for difference in ("recoil", "directory", "prefab_extra", "no_base", "unknown_suffix"):
            with self.subTest(difference=difference):
                base = color_stock("base", "stocks/mini.bundle", ConflictingItems=["pad"])
                variant = color_stock("variant", "stocks/mini_red.bundle")
                if difference == "recoil":
                    variant["_props"]["Recoil"] = -5
                elif difference == "directory":
                    variant["_props"]["Prefab"]["path"] = "other/mini_red.bundle"
                elif difference == "prefab_extra":
                    variant["_props"]["Prefab"]["rcid"] = "different"
                elif difference == "no_base":
                    base["_props"]["Prefab"]["path"] = "stocks/mini_ylw.bundle"
                else:
                    variant["_props"]["Prefab"]["path"] = "stocks/mini_short.bundle"
                catalog = catalog_with(base, variant, item("pad"), weapon_slots=[slot("stock", "variant", required=True), slot("pad", "pad", required=True)])
                evaluate_build(catalog, node("weapon", None, node("variant", "stock"), node("pad", "pad")))
                self.assertEqual(optimize(catalog, "weapon", limit=1)["status"], "optimal")

    def test_identical_unsuffixed_prefab_alone_is_not_clone_evidence(self):
        catalog = catalog_with(
            color_stock("one", "stocks/mini.bundle", ConflictingItems=["pad"]),
            color_stock("two", "stocks/mini.bundle"), item("pad"),
            weapon_slots=[slot("stock", "two", required=True), slot("pad", "pad", required=True)],
        )
        stats = evaluate_build(catalog, node("weapon", None, node("two", "stock"), node("pad", "pad")))
        self.assertEqual(stats["recoil_modifier_percent"], -4)

    def test_loader_records_clone_propagation_and_replays_without_source_files(self):
        workspace = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory(prefix="optimizer-compatibility-", dir=workspace / ".tmp") as temporary:
            root = Path(temporary)
            mod = root / "SPT_Runtime" / "user" / "mods" / "TGC-NG"
            (mod / "db" / "CustomItems").mkdir(parents=True)
            assembly = b"synthetic consumer identity; never executed"
            (mod / "TGC-NG.dll").write_bytes(assembly)
            config = mod / "db" / "CustomItems" / "modTGC_items.json"
            config.write_text(json.dumps({"clone": {"itemTplToClone": "original"}}), encoding="utf-8")
            catalog = catalog_with(
                item("original"), item("clone"), item("late", ConflictingItems=["original"]),
                weapon_slots=[slot("stock", "clone", required=True), slot("late", "late", required=True)],
            )
            with patch("weapon_optimizer.compatibility._TGC_SHA256", hashlib.sha256(assembly).hexdigest()):
                catalog.compatibility = load_compatibility(root, catalog)
            self.assertEqual({record["sha256"] for record in catalog.compatibility["sources"]}, {
                hashlib.sha256(assembly).hexdigest(), hashlib.sha256(config.read_bytes()).hexdigest(),
            })
            config.unlink()
            (mod / "TGC-NG.dll").unlink()
            with self.assertRaisesRegex(ValueError, "conflict"):
                evaluate_build(catalog, node("weapon", None, node("clone", "stock"), node("late", "late")))

    def test_loader_rejects_unverified_consumer_instead_of_guessing_semantics(self):
        workspace = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory(prefix="optimizer-compatibility-", dir=workspace / ".tmp") as temporary:
            root = Path(temporary)
            mod = root / "SPT_Runtime" / "user" / "mods" / "TGC-NG"
            (mod / "db" / "CustomItems").mkdir(parents=True)
            (mod / "TGC-NG.dll").write_bytes(b"unknown consumer")
            (mod / "db" / "CustomItems" / "modTGC_items.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(DataError, "unverified"):
                load_compatibility(root, catalog_with())

    def test_reported_ums_moe_pad_chain_is_rejected_for_all_colors(self):
        moe, modded, pad = "56eabf3bd2720b75698b4569", "672e2e75d41d0e93c918096a", "58d2912286f7744e27117493"
        variants = {
            "698b338649b46ae2d0092e82": "stocks/stock_ar15_phase5_ums.bundle",
            "698b358b49b46ae2d0092e86": "stocks/stock_ar15_phase5_ums_red.bundle",
            "698b3592e700c6d632003753": "stocks/stock_ar15_phase5_ums_ylw.bundle",
        }
        black = next(iter(variants))
        catalog = catalog_with(
            item(moe), item(modded, Slots=[slot("mod_stock", pad)]), item(pad),
            *(color_stock(tid, path, ConflictingItems=[moe, pad] if tid == black else [moe]) for tid, path in variants.items()),
            weapon_slots=[slot("mod_stock_000", modded), slot("mod_stock_003", *variants)],
        )
        catalog.compatibility = clone_evidence(catalog, (moe, modded), excluded=(modded,))
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, "conflict"):
                evaluate_build(catalog, node("weapon", None, node(modded, "mod_stock_000", node(pad, "mod_stock")), node(variant, "mod_stock_003")))
        # Either assembly by itself remains legal, including the MOE pad.
        evaluate_build(catalog, node("weapon", None, node(modded, "mod_stock_000", node(pad, "mod_stock"))))
        evaluate_build(catalog, node("weapon", None, node(black, "mod_stock_003")))
        for solution in optimize(catalog, "weapon", limit=10)["solutions"]:
            evaluate_build(catalog, solution["build"])
            installed = {part["template_id"] for part in flatten(solution["build"])}
            self.assertFalse(modded in installed and installed & variants.keys())


def obtainable(catalog, allowed):
    entries = {}
    for tid, template in catalog.items.items():
        if template.get("_type") == "Item":
            entries[tid] = {
                "status": "conditional" if tid in allowed else "unproven",
                "sources": [{"channel": "fixture", "conditions": [{"kind": "loyalty_level", "minimum": 2}]}] if tid in allowed else [],
                "blocked_channels": [], "conditions": [],
            }
    return {"schema_version": 1, "catalog_sha256": _digest(catalog.items), "source_files": [], "items": entries}


class AcquisitionOptimizerTests(unittest.TestCase):
    def test_required_is_default_and_missing_evidence_fails_before_solving(self):
        catalog = catalog_with(item("part"), weapon_slots=[slot("part", "part")])
        with self.assertRaises(DataError):
            optimize_builds(catalog, "weapon", limit=1)

    def test_unproven_attachment_is_filtered_but_user_selected_root_need_not_be_obtainable(self):
        catalog = catalog_with(item("unproven", Recoil=-30), item("obtainable", Recoil=-2), weapon_slots=[slot("part", "unproven", "obtainable", required=True)])
        catalog.acquisition = obtainable(catalog, {"obtainable"})
        result = optimize_builds(catalog, "weapon", limit=2)
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(len(result["solutions"]), 1)
        self.assertEqual(result["constraints"]["acquisition"], "required")
        solution = result["solutions"][0]
        self.assertEqual(solution["build"]["children"][0]["template_id"], "obtainable")
        self.assertEqual(solution["acquisition"][0]["status"], "conditional")
        self.assertEqual(solution["acquisition"][0]["sources"][0]["conditions"], [{"kind": "loyalty_level", "minimum": 2}])
        research = optimize(catalog, "weapon", limit=1)
        self.assertEqual(research["solutions"][0]["build"]["children"][0]["template_id"], "unproven")
        self.assertEqual(research["solutions"][0]["acquisition"][0]["status"], "not_checked")

    def test_unproven_required_descendant_prevents_its_parent_branch(self):
        catalog = catalog_with(
            item("parent", Recoil=-30, Slots=[slot("deep", "missing", required=True)]), item("missing"), item("safe", Recoil=-1),
            weapon_slots=[slot("part", "parent", "safe", required=True)],
        )
        catalog.acquisition = obtainable(catalog, {"parent", "safe"})
        result = optimize_builds(catalog, "weapon", limit=1)
        self.assertEqual(result["solutions"][0]["build"]["children"][0]["template_id"], "safe")
        evaluate_build(catalog, result["solutions"][0]["build"])


if __name__ == "__main__":
    unittest.main()
