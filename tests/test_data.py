"""Regression fixtures are synthetic and all writes stay in workspace .tmp."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib
from decimal import Decimal

from weapon_optimizer import data
from weapon_optimizer.cli import main
from weapon_optimizer.data import Catalog, DataError, load_snapshot, save_snapshot
from weapon_optimizer.evaluate import evaluate_build
from weapon_optimizer.optimizer import optimize
from test_optimizer import catalog_with, item, node, slot


class SnapshotBehaviorTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[3]
        self.temporary = tempfile.TemporaryDirectory(prefix="optimizer-regression-", dir=workspace / ".tmp")
        self.addCleanup(self.temporary.cleanup)
        self.installation = Path(self.temporary.name) / "synthetic-installation"
        self.project = self.installation / "labs" / "weapon-optimizer"
        self.project.mkdir(parents=True)
        (self.installation / ".tmp").mkdir()
        # Simulate an installation entirely inside .tmp, never real production.
        origin = self.project / "src" / "weapon_optimizer" / "data.py"
        origin_patch = patch.object(data, "__file__", str(origin))
        origin_patch.start()
        self.addCleanup(origin_patch.stop)
        self.catalog = catalog_with(item("part"), weapon_slots=[slot("part", "part")])
        self.catalog.locale["weapon Name"] = "Synthetic Weapon"

    def test_existing_snapshot_is_not_overwritten(self):
        target = self.project / "existing.json"
        original = b'{"synthetic":"keep this file"}'
        target.write_bytes(original)
        with self.assertRaises(DataError):
            save_snapshot(self.catalog, target)
        self.assertEqual(target.read_bytes(), original)

    def test_production_and_outside_paths_cannot_be_written(self):
        production = self.installation / "SPT_Runtime" / "user" / "profiles"
        production.mkdir(parents=True)
        existing = production / "synthetic.json"
        original = b'{"synthetic_profile":"untouched"}'
        existing.write_bytes(original)
        new = production / "synthetic-new.json"
        for target in (existing, new):
            with self.subTest(target=target), self.assertRaises(DataError):
                save_snapshot(self.catalog, target)
        self.assertEqual(existing.read_bytes(), original)
        self.assertFalse(new.exists())

    def test_symlink_escape_is_rejected(self):
        production = self.installation / "synthetic-production"
        production.mkdir()
        alias = self.project / "alias"
        try:
            os.symlink(production, alias, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(DataError):
            save_snapshot(self.catalog, alias / "must-not-exist.json")
        self.assertFalse((production / "must-not-exist.json").exists())

    def test_linked_workspace_temp_root_cannot_escape_workspace(self):
        outside = Path(self.temporary.name) / "synthetic-outside"
        outside.mkdir()
        temp_root = self.installation / ".tmp"
        temp_root.rmdir()
        try:
            os.symlink(outside, temp_root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(DataError):
            save_snapshot(self.catalog, temp_root / "must-not-exist.json")
        self.assertFalse((outside / "must-not-exist.json").exists())

    def test_linked_temp_root_cannot_write_production_inside_workspace(self):
        temp_root = self.installation / ".tmp"
        temp_root.rmdir()
        for directory in ("profiles", "mods"):
            with self.subTest(directory=directory):
                production = self.installation / "SPT_Runtime" / "user" / directory
                production.mkdir(parents=True)
                try:
                    os.symlink(production, temp_root, target_is_directory=True)
                except OSError as exc:
                    self.skipTest(f"directory symlinks unavailable: {exc}")
                try:
                    with self.assertRaises(DataError):
                        save_snapshot(self.catalog, temp_root / "must-not-exist.json")
                    self.assertFalse((production / "must-not-exist.json").exists())
                finally:
                    temp_root.unlink()

    def test_decimal_precision_survives_plain_compressed_and_snapshot_paths(self):
        fixture = catalog_with(
            item("precise", Recoil="DECIMAL_LITERAL", Slots=[slot("required", "child", required=True)]),
            item("child"), item("rounded", Recoil=-1.0),
            weapon_slots=[slot("choice", "precise", "rounded", required=True)],
        )
        raw = json.dumps(fixture.items).replace('"DECIMAL_LITERAL"', "-1.0000000000000001").encode()
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                items = data._decode_json(zlib.compress(raw) if compressed else raw, "synthetic-fixture")
                self.assertEqual(items["precise"]["_props"]["Recoil"], Decimal("-1.0000000000000001"))
                catalog = Catalog(items, {"weapon Name": "Synthetic Weapon"})
                target = self.project / f"decimal-{compressed}.json"
                save_snapshot(catalog, target)
                loaded = load_snapshot(target)
                self.assertEqual(loaded.props("precise")["Recoil"], Decimal("-1.0000000000000001"))
                result = optimize(loaded, "weapon", objective="recoil", limit=1, acquisition="templates")
                self.assertEqual(result["status"], "optimal")
                # The slightly better recoil must beat the smaller part count.
                self.assertEqual(result["solutions"][0]["build"]["children"][0]["template_id"], "precise")

    def test_changed_snapshot_content_is_rejected(self):
        target = self.project / "tampered.json"
        save_snapshot(self.catalog, target)
        document = json.loads(target.read_text(encoding="utf-8"))
        document["items"]["part"]["_props"]["Recoil"] = -99
        target.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(DataError):
            load_snapshot(target)

    def test_conflict_alias_order_survives_snapshot_and_tampering_is_rejected(self):
        props = self.catalog.props("part")
        props["ConflictingItems"] = []
        props["Conflictingitems"] = ["weapon"]
        target = self.project / "ordered-conflicts.json"
        save_snapshot(self.catalog, target)
        loaded = load_snapshot(target)
        self.assertEqual(loaded.conflicts("part"), ("weapon",))
        document = json.loads(target.read_text(encoding="utf-8"))
        original = document["items"]["part"]["_props"]
        document["items"]["part"]["_props"] = {key: value for key, value in reversed(list(original.items()))}
        target.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(DataError):
            load_snapshot(target)

    def test_old_unordered_snapshot_schema_is_rejected(self):
        target = self.project / "old-schema.json"
        save_snapshot(self.catalog, target)
        document = json.loads(target.read_text(encoding="utf-8"))
        document["schema_version"] = 1
        target.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(DataError):
            load_snapshot(target)

    def test_localized_labels_survive_snapshot_and_context_tampering_is_rejected(self):
        self.catalog.locale_ch.update({"weapon Name": "测试武器", "weapon ShortName": "测试短名"})
        target = self.project / "localized.json"
        save_snapshot(self.catalog, target)
        loaded = load_snapshot(target)
        self.assertEqual(loaded.name("weapon", language="ch", short=True), "测试短名")
        self.assertEqual(loaded.name("weapon", language="ch"), "测试武器")
        original = json.loads(target.read_text(encoding="utf-8"))
        for section in ("locale_ch", "acquisition", "compatibility"):
            with self.subTest(section=section):
                tampered = dict(original)
                tampered[section] = {"changed": True}
                target.write_text(json.dumps(tampered), encoding="utf-8")
                with self.assertRaises(DataError):
                    load_snapshot(target)


class ConflictKeyResolutionTests(unittest.TestCase):
    def make_catalog(self, *, canonical_last=False, final_null=False):
        candidate = item("candidate", Recoil=-20)
        props = candidate["_props"]
        if canonical_last:
            del props["ConflictingItems"]
        props["Conflictingitems"] = ["other"]
        if canonical_last:
            props["ConflictingItems"] = None if final_null else []
        return catalog_with(candidate, item("safe", Recoil=-1), item("other"), weapon_slots=[
            slot("choice", "candidate", "safe", required=True), slot("other", "other", required=True),
        ])

    def test_later_noncanonical_conflicts_reject_build_and_change_optimum(self):
        catalog = self.make_catalog()
        invalid = node("weapon", None, node("candidate", "choice"), node("other", "other"))
        with self.assertRaises(ValueError):
            evaluate_build(catalog, invalid)
        result = optimize(catalog, "weapon", objective="recoil", limit=1, acquisition="templates")
        self.assertEqual(result["solutions"][0]["build"]["children"][0]["template_id"], "safe")
        self.assertEqual(result["solutions"][0]["stats"]["recoil_modifier_percent"], -1)

    def test_later_canonical_empty_or_null_clears_instead_of_unioning(self):
        for final_null in (False, True):
            with self.subTest(final_null=final_null):
                catalog = self.make_catalog(canonical_last=True, final_null=final_null)
                build = node("weapon", None, node("candidate", "choice"), node("other", "other"))
                self.assertEqual(evaluate_build(catalog, build)["recoil_modifier_percent"], -20)
                self.assertEqual(optimize(catalog, "weapon", objective="recoil", limit=1, acquisition="templates")["solutions"][0]["stats"]["recoil_modifier_percent"], -20)

    def test_noncanonical_self_conflict_still_limits_instances(self):
        part = item("part", Recoil=-2, Conflictingitems=["part"])
        catalog = catalog_with(part, weapon_slots=[slot("left", "part"), slot("right", "part")])
        result = optimize(catalog, "weapon", limit=1, acquisition="templates")
        self.assertEqual(result["solutions"][0]["stats"]["part_count"], 1)
        self.assertEqual(result["solutions"][0]["stats"]["recoil_modifier_percent"], -2)


class LiveSessionValidationTests(unittest.TestCase):
    def test_invalid_profile_ids_fail_before_any_http_request(self):
        invalid_ids = (None, "", "weaponoptimizerprofile", "a" * 23, "a" * 25, "g" * 24, "a" * 24 + ";x=y")
        with patch.object(data.urllib.request.OpenerDirector, "open", side_effect=AssertionError("network must not be reached")) as request:
            for session in invalid_ids:
                with self.subTest(session=session), self.assertRaises(ValueError):
                    data.load_live("https://127.0.0.1:6969", session, insecure_loopback=True)
            request.assert_not_called()

    def test_cli_returns_argument_error_without_contacting_server(self):
        output = io.StringIO()
        with patch.object(data.urllib.request.OpenerDirector, "open", side_effect=AssertionError("network must not be reached")) as request:
            with redirect_stdout(output):
                status = main(["weapons", "--server", "https://127.0.0.1:6969", "--session", "weaponoptimizerprofile", "--insecure-loopback"])
            request.assert_not_called()
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
