"""Local Chinese evidence and saved-build rendering without game/server access."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from weapon_optimizer.cli import main
from weapon_optimizer.data import DataError, SUPPRESSOR_CATEGORY, _digest, save_snapshot, solve_context
from weapon_optimizer.evaluate import evaluate_build
from weapon_optimizer.local import load_chinese_locale
from weapon_optimizer.optimizer import optimize
from test_optimizer import catalog_with, item, node, slot


class LocalPresentationTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[3]
        self.temporary = tempfile.TemporaryDirectory(prefix="optimizer-locale-", dir=workspace / ".tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = catalog_with(item("guard"), weapon_slots=[slot("mod_handguard", "guard")])
        self.catalog.locale.update({"weapon Name": "Weapon", "weapon ShortName": "W", "guard Name": "English guard", "guard ShortName": "Guard"})
        self.base = self.root / "SPT_Runtime/SPT_Data/database/locales/global/ch.json"
        self.base.parent.mkdir(parents=True)
        self.base.write_text(json.dumps({"guard Name": "基础护木", "guard ShortName": "基础"}), encoding="utf-8")

    def mod_locale(self, name, extension="json"):
        path = self.root / f"SPT_Runtime/user/mods/{name}/db/CustomLocales/ch.{extension}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_mod_chinese_overrides_base_and_jsonc_keeps_literal_comment_markers(self):
        path = self.mod_locale("synthetic", "jsonc")
        path.write_text('''{
            // item names, not a translation service
            "guard Name": "原名 // /* 保留 */",
            "guard ShortName": "B10M+B19", /* comment */
            "not-an-item Name": "不进入物品表",
        }''', encoding="utf-8-sig")
        labels, sources = load_chinese_locale(self.root, self.catalog)
        self.assertEqual(labels, {"guard Name": "原名 // /* 保留 */", "guard ShortName": "B10M+B19"})
        self.assertEqual({source["path"] for source in sources}, {self.base.relative_to(self.root).as_posix(), path.relative_to(self.root).as_posix()})

    def test_conflicting_mod_names_require_evidence_of_order_instead_of_guessing(self):
        for name, label in (("first", "甲"), ("second", "乙")):
            self.mod_locale(name).write_text(json.dumps({"guard Name": label}), encoding="utf-8")
        with self.assertRaises(DataError):
            load_chinese_locale(self.root, self.catalog)

    def test_render_command_uses_saved_locales_not_stale_build_names(self):
        self.catalog.locale_ch.update({"guard Name": "Zenit B-10M 导轨护木+ B-19 上导轨组合", "guard ShortName": "B10M+B19"})
        snapshot = self.root / "snapshot.json"
        save_snapshot(self.catalog, snapshot)
        build = node("weapon", None, node("guard", "mod_handguard"))
        build["children"][0]["name"] = "stale manually translated name"
        result_path = self.root / "build.json"
        result_path.write_text(json.dumps({"status": "feasible", "solve_context": solve_context(self.catalog, "templates"), "solutions": [{"build": build, "stats": evaluate_build(self.catalog, build), "proof": {"status": "feasible"}}]}), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["render", "--snapshot", str(snapshot), "--build", str(result_path)])
        self.assertEqual(code, 0)
        self.assertIn("[护木] B10M+B19〔Zenit B-10M 导轨护木+ B-19 上导轨组合〕", output.getvalue())
        self.assertNotIn("stale manually translated name", output.getvalue())
        self.assertNotIn("无本地中文条目", output.getvalue())

    def test_saved_result_is_rejected_after_a_new_conflict_is_declared(self):
        build = node("weapon", None, node("guard", "mod_handguard"))
        stats = evaluate_build(self.catalog, build)
        result_path = self.root / "old-build.json"
        result_path.write_text(json.dumps({"status": "optimal", "solutions": [{"build": build, "stats": stats, "proof": {"status": "optimal"}}]}), encoding="utf-8")
        self.catalog.props("weapon")["ConflictingItems"] = ["guard"]
        snapshot = self.root / "current.json"
        save_snapshot(self.catalog, snapshot)
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["render", "--snapshot", str(snapshot), "--build", str(result_path)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_arguments")

    def test_render_revalidates_suppressor_requirement_even_for_unshown_solutions(self):
        catalog = catalog_with(
            item("quiet", SUPPRESSOR_CATEGORY, Recoil=-10),
            item("brake", Recoil=-20),
            weapon_slots=[slot("mod_muzzle", "quiet", "brake")],
        )
        catalog.locale["weapon Name"] = "Weapon"
        snapshot = self.root / "muzzle-snapshot.json"
        save_snapshot(catalog, snapshot)
        result = optimize(catalog, "weapon", acquisition="templates", require_suppressor=True, limit=1)
        result_path = self.root / "suppressed.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        arguments = ["render", "--snapshot", str(snapshot), "--build", str(result_path), "--solution", "1"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(arguments), 0)

        # Keep the tree and recomputed stats valid; only the declared hard
        # requirement is violated in a solution outside the requested rank.
        unsuppressed = node("weapon", None, node("brake", "mod_muzzle"))
        result["solutions"].append({
            "rank": 2, "build": unsuppressed, "stats": evaluate_build(catalog, unsuppressed),
            "proof": {"status": "optimal"},
        })
        result_path.write_text(json.dumps(result), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(arguments), 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_arguments")

    def test_locale_only_changes_do_not_invalidate_optimality(self):
        result = optimize(self.catalog, "weapon", acquisition="templates", time_limit=1)
        self.assertEqual(result["status"], "optimal")
        result_path = self.root / "solved.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        self.catalog.locale_ch["weapon Name"] = "仅修改显示名称"
        snapshot = self.root / "renamed.json"
        save_snapshot(self.catalog, snapshot)
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["render", "--snapshot", str(snapshot), "--build", str(result_path)])
        self.assertEqual(code, 0)
        self.assertIn("状态：optimal", output.getvalue())
        self.assertIn("仅修改显示名称", output.getvalue())

    def test_render_rejects_stale_optimality_when_search_space_expands(self):
        for changed in ("items", "acquisition", "compatibility"):
            with self.subTest(changed=changed):
                catalog = catalog_with(
                    item("guard", Ergonomics=5),
                    item("better", Ergonomics=0 if changed == "items" else 10),
                    item("original"),
                    weapon_slots=[slot("mod_handguard", "guard", "better")], base_ergo=20,
                )
                catalog.locale["weapon Name"] = "Weapon"
                mode = "templates"
                if changed == "acquisition":
                    mode = "required"
                    catalog.acquisition = {"schema_version": 1, "catalog_sha256": _digest(catalog.items), "source_files": [], "items": {
                        tid: {"status": "unproven", "sources": [], "blocked_channels": []} if tid == "better" else {
                            "status": "conditional", "blocked_channels": [], "sources": [{"channel": "trader", "conditions": [{"kind": "trader_loyalty", "minimum_level": 1}]}]
                        } for tid in catalog.items if catalog.items[tid].get("_type") == "Item"
                    }}
                if changed == "compatibility":
                    catalog.props("weapon")["ConflictingItems"] = ["original"]
                    catalog.compatibility = {"schema_version": 1, "catalog_sha256": _digest(catalog.items), "clone_rules": [{
                        "kind": "incoming-clone-conflicts", "aliases": [{"source_id": "original", "clone_id": "better"}], "excluded_owners": []
                    }]}
                result = optimize(catalog, "weapon", acquisition=mode, limit=1, time_limit=1)
                self.assertEqual(result["status"], "optimal")
                self.assertEqual(result["solutions"][0]["build"]["children"][0]["template_id"], "guard")
                result_path = self.root / f"old-{changed}.json"
                result_path.write_text(json.dumps(result), encoding="utf-8")
                original_snapshot = self.root / f"original-{changed}.json"
                save_snapshot(catalog, original_snapshot)
                original_output = io.StringIO()
                with redirect_stdout(original_output):
                    original_code = main(["render", "--snapshot", str(original_snapshot), "--build", str(result_path)])
                self.assertEqual(original_code, 0)
                if changed == "items":
                    catalog.props("better")["Ergonomics"] = 10
                elif changed == "acquisition":
                    catalog.acquisition["items"]["better"] = catalog.acquisition["items"]["guard"]
                else:
                    catalog.compatibility["clone_rules"] = []
                self.assertEqual(evaluate_build(catalog, result["solutions"][0]["build"]), result["solutions"][0]["stats"])
                snapshot = self.root / f"new-{changed}.json"
                save_snapshot(catalog, snapshot)
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(["render", "--snapshot", str(snapshot), "--build", str(result_path)])
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output.getvalue())["error"]["code"], "data_error")
