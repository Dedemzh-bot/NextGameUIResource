"""Registry regression tests use isolated fake assets and never an Unreal project."""
import codecs
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT))

from register_icons import _table_lock, digest, parse_table, register_resources
from resource_ids import split_resource_id


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.test_root = (TOOL_ROOT / ".local/tests").resolve()
        self.root = self.test_root / ("registry-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup_test_directory)
        self.source = self.root / "input"
        self.content = self.root / "Project" / "Content"
        self.source.mkdir()
        self.content.mkdir(parents=True)
        self.table = self.root / "resource.txt"
        self.old = codecs.BOM_UTF8 + "ID\t描述\t路径\r\nId\tDes\tPath\r\n41\t旧描述\t/Game/Legacy.Legacy\r\n".encode("utf-8")
        self.table.write_bytes(self.old)
        self.output = self.root / "report"
        self.manifest_path = self.root / "manifest.json"
        self.readback_path = self.root / "readback.json"
        self.catalog_path = self.root / "catalog.json"
        self.assignments_path = self.root / "assignments.json"
        self.catalog = {"version": 1, "categories": [{"major": 20, "minor": 1, "name": "项目武器图标", "enabled": True}]}
        self.assignments = {"version": 1, "resources": []}
        self.manifest = {"source": str(self.source), "project_dir": str(self.content.parent),
                         "expected_assets": [], "groups": [], "packed_verified": True}
        self.readback = {"success": True, "assets": [], "project_content_dir": str(self.content)}
        self.add_resource("icon_weapon_a.png")
        self.flush()

    def cleanup_test_directory(self):
        resolved = self.root.resolve()
        if resolved.parent != self.test_root or not resolved.name.startswith("registry-"):
            raise RuntimeError("Refusing cleanup outside this test's directory.")
        shutil.rmtree(resolved)

    def write_json(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def flush(self):
        self.write_json(self.manifest_path, self.manifest)
        self.readback["manifest_sha256"] = digest(self.manifest_path.read_bytes())
        self.write_json(self.readback_path, self.readback)
        self.write_json(self.catalog_path, self.catalog)
        self.write_json(self.assignments_path, self.assignments)

    def add_saved_asset(self, asset, class_name):
        file = self.content / (asset.split(".")[0].removeprefix("/Game/") + ".uasset")
        file.parent.mkdir(parents=True, exist_ok=True)
        data = ("fake saved asset: " + asset).encode("utf-8")
        file.write_bytes(data)
        self.manifest["expected_assets"].append(asset)
        self.readback["assets"].append({"path": asset, "class": class_name,
                                        "saved_file": {"path": str(file), "bytes": len(data), "sha256": digest(data)}})

    def add_resource(self, source_relative, *, system="Test", registered=True, kind="atlas", assign=True):
        name = Path(source_relative).name
        stem = Path(name).stem
        folder = f"/Game/UI/ICON/{system}" if kind == "atlas" else f"/Game/UI/Portrait/{system}"
        asset = folder + ("/Frames/" if kind == "atlas" else "/") + stem + "." + stem
        group = next((row for row in self.manifest["groups"] if row["system"] == system and row["kind"] == kind), None)
        if group is None:
            group = {"system": system, "kind": kind, "register_icon": registered, "files": []}
            self.manifest["groups"].append(group)
            if kind == "atlas":
                group["texture_asset"] = folder + "/Texture.Texture"
                group["sheet_asset"] = folder + "/Sheet.Sheet"
                self.add_saved_asset(group["texture_asset"], "/Script/Engine.Texture2D")
                self.add_saved_asset(group["sheet_asset"], "/Script/Paper2D.PaperSpriteSheet")
        group["files"].append({"name": name, "original": str(self.source / source_relative),
                               "source_relative": source_relative, "asset": asset})
        self.add_saved_asset(asset, "/Script/Paper2D.PaperSprite" if kind == "atlas" else "/Script/Engine.Texture2D")
        if assign:
            self.assignments["resources"].append({"file": source_relative, "major": 20, "minor": 1, "description": "武器图标"})
        return asset

    def register(self, **kwargs):
        options = {"catalog_path": self.catalog_path, "assignments_path": self.assignments_path}
        options.update(kwargs)
        return register_resources(self.manifest_path, self.readback_path, self.table, self.output, **options)

    def assert_unmodified(self):
        self.assertEqual(self.table.read_bytes(), self.old)
        self.assertFalse(self.table.with_name("resource.txt.lock").exists())

    def test_classified_plan_then_apply_preserves_legacy_bytes(self):
        plan = self.register()
        self.assertEqual(plan["additions"][0]["Id"], 20010001)
        self.assertFalse(plan["applied"])
        self.assert_unmodified()
        result = self.register(apply=True)
        new = self.table.read_bytes()
        self.assertTrue(new.startswith(self.old))
        self.assertEqual(new.count(codecs.BOM_UTF8), 1)
        self.assertEqual(new.count(b"\n"), new.count(b"\r\n"))
        self.assertIn("20010001\t武器图标\t".encode("utf-8"), new)
        self.assertEqual(Path(result["backup"]).read_bytes(), self.old)
        self.assertTrue(result["applied"])
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(parse_table(new)[0][41], "/Game/Legacy.Legacy")

    def test_custom_project_category_needs_no_code_change(self):
        self.catalog["categories"] = [{"major": 73, "minor": 84, "name": "项目独有类型"}]
        self.assignments["resources"][0].update(major=73, minor=84)
        self.flush()
        self.assertEqual(self.register()["additions"][0]["Id"], 73840001)

    def test_existing_path_reuses_id_and_description_without_assignment(self):
        asset = self.manifest["groups"][0]["files"][0]["asset"]
        self.old += f"58\t保留备注\t{asset}\r\n".encode("utf-8")
        self.table.write_bytes(self.old)
        report = self.register(apply=True, catalog_path=None, assignments_path=None)
        self.assertEqual(report["new_count"], 0)
        self.assertEqual(report["mappings"][0]["Id"], 58)
        self.assertTrue(report["mappings"][0]["reused"])
        self.assert_unmodified()

    def test_missing_catalog_never_falls_back_to_sequential(self):
        with self.assertRaisesRegex(ValueError, "require --catalog"):
            self.register(apply=True, catalog_path=None)
        self.assert_unmodified()

    def test_unreal_path_case_variation_reuses_existing_id(self):
        asset = self.manifest["groups"][0]["files"][0]["asset"]
        historical = "/Game/" + asset.removeprefix("/Game/").lower()
        self.old += f"59\t历史备注\t{historical}\r\n".encode("utf-8")
        self.table.write_bytes(self.old)
        report = self.register(apply=True, catalog_path=None, assignments_path=None)
        self.assertEqual(report["new_count"], 0)
        self.assertEqual(report["mappings"][0]["Id"], 59)
        self.assert_unmodified()

    def test_duplicate_unreal_path_casing_rejected(self):
        table = self.old + b"42\t\t/Game/legacy.legacy\r\n"
        with self.assertRaisesRegex(ValueError, "Duplicate ID/path"):
            parse_table(table)

    def test_missing_assignment_is_actionable(self):
        self.assignments["resources"] = []
        self.flush()
        with self.assertRaisesRegex(ValueError, "Missing classification assignment.*icon_weapon_a"):
            self.register(apply=True)
        self.assert_unmodified()

    def test_duplicate_category_rejected(self):
        self.catalog["categories"].append(dict(self.catalog["categories"][0]))
        self.flush()
        with self.assertRaisesRegex(ValueError, "Duplicate category"):
            self.register(apply=True)
        self.assert_unmodified()

    def test_duplicate_assignment_rejected(self):
        self.assignments["resources"].append(dict(self.assignments["resources"][0]))
        self.flush()
        with self.assertRaisesRegex(ValueError, "Duplicate assignment"):
            self.register(apply=True)
        self.assert_unmodified()

    def test_disabled_or_unknown_categories_rejected(self):
        for categories in ([], [{"major": 20, "minor": 1, "name": "停用", "enabled": False}]):
            with self.subTest(categories=categories):
                self.catalog["categories"] = categories
                self.flush()
                with self.assertRaisesRegex(ValueError, "missing or disabled"):
                    self.register(apply=True)
                self.assert_unmodified()

    def test_retired_ids_and_existing_max_are_never_reused(self):
        self.catalog["reserved_ids"] = [20010009, 73019999]
        self.old += b"20010005\t\t/Game/Previous.Previous\r\n"
        self.table.write_bytes(self.old)
        self.flush()
        self.assertEqual(self.register()["additions"][0]["Id"], 20010010)

    def test_full_subgroup_does_not_silently_change_category(self):
        self.catalog["reserved_ids"] = [20019999]
        self.flush()
        with self.assertRaisesRegex(ValueError, "exhausted serial 9999"):
            self.register(apply=True)
        self.assert_unmodified()

    def test_multiple_new_resources_receive_unique_monotonic_ids(self):
        self.add_resource("icon_weapon_b.png")
        self.flush()
        self.assertEqual([row["Id"] for row in self.register()["additions"]], [20010001, 20010002])

    def test_source_relative_assignments_distinguish_same_basename(self):
        first = self.manifest["groups"][0]["files"][0]
        first["source_relative"] = "folder_a/icon_weapon_a.png"
        first["original"] = str(self.source / first["source_relative"])
        self.assignments["resources"][0]["file"] = first["source_relative"]
        self.add_resource("folder_b/icon_weapon_a.png", system="Other")
        self.flush()
        self.assertEqual(self.register()["new_count"], 2)
        self.assignments["resources"] = [{"file": "icon_weapon_a.png", "major": 20, "minor": 1}]
        self.flush()
        with self.assertRaisesRegex(ValueError, "Ambiguous assignment"):
            self.register()

    def test_unique_basename_fallback_and_legacy_original_path(self):
        file = self.manifest["groups"][0]["files"][0]
        del file["source_relative"]
        file["original"] = str(self.source / "nested" / file["name"])
        self.flush()
        self.assertEqual(self.register()["new_count"], 1)

    def test_unsafe_description_fails_before_table_write(self):
        for description in ("bad\tcolumn", "bad\nrow", "bad\rrow"):
            with self.subTest(description=description):
                self.assignments["resources"][0]["description"] = description
                self.flush()
                with self.assertRaisesRegex(ValueError, "without tabs, newlines"):
                    self.register(apply=True)
                self.assert_unmodified()

    def test_gui_pic_requires_all_resources_opt_in(self):
        self.add_resource("pic_ui_background.png", kind="standalone_texture", system="Background", registered=False)
        self.flush()
        self.assertEqual(self.register()["new_count"], 1)
        self.assertEqual(self.register(register_all=True)["new_count"], 2)

    def test_sequential_mode_is_explicit(self):
        report = self.register(id_mode="sequential", catalog_path=None, assignments_path=None)
        self.assertEqual(report["additions"][0]["Id"], 42)

    def test_no_table_is_created_automatically(self):
        self.table.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "no table is created"):
            self.register(apply=True)
        self.assertFalse(self.table.exists())

    def test_existing_lock_is_not_deleted(self):
        lock = self.table.with_name("resource.txt.lock")
        lock.write_bytes(b"another owner")
        with self.assertRaisesRegex(RuntimeError, "Resource table is locked"):
            self.register(apply=True)
        self.assertEqual(lock.read_bytes(), b"another owner")
        self.assertEqual(self.table.read_bytes(), self.old)

    def test_lock_excludes_a_second_process(self):
        code = "from pathlib import Path\nfrom register_icons import _table_lock\nimport sys\nwith _table_lock(Path(sys.argv[1])):\n print('unexpected lock acquisition')\n"
        environment = dict(os.environ, PYTHONPATH=str(TOOL_ROOT))
        with _table_lock(self.table):
            child = subprocess.run([sys.executable, "-c", code, str(self.table)], env=environment,
                                   capture_output=True, text=True, timeout=15)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn("Resource table is locked", child.stderr)
        self.assert_unmodified()

    def test_corrupted_saved_asset_rejects_entire_readback(self):
        # Corrupt the atlas texture, even though only its sprite gets an ID.
        Path(self.readback["assets"][0]["saved_file"]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Saved asset changed"):
            self.register(apply=True)
        self.assert_unmodified()

    def test_changed_manifest_cannot_reuse_readback(self):
        self.manifest_path.write_bytes(self.manifest_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "this manifest"):
            self.register(apply=True)
        self.assert_unmodified()

    def test_duplicate_or_missing_readback_assets_rejected(self):
        saved = list(self.readback["assets"])
        for assets in (saved + [saved[0]], saved[1:]):
            with self.subTest(count=len(assets)):
                self.readback["assets"] = assets
                self.flush()
                with self.assertRaisesRegex(ValueError, "Duplicate readback|coverage"):
                    self.register(apply=True)
                self.assert_unmodified()

    def test_wrong_class_rejected(self):
        self.readback["assets"][-1]["class"] = "/Script/Engine.Texture2D"
        self.flush()
        with self.assertRaisesRegex(ValueError, "Wrong resource asset class"):
            self.register(apply=True)
        self.assert_unmodified()

    def test_saved_asset_must_belong_to_reported_project(self):
        row = self.readback["assets"][0]
        copy = self.root / "outside.uasset"
        copy.write_bytes(Path(row["saved_file"]["path"]).read_bytes())
        row["saved_file"]["path"] = str(copy)
        self.flush()
        with self.assertRaisesRegex(ValueError, "project identity"):
            self.register(apply=True)
        self.assert_unmodified()

    def test_manifest_and_readback_project_mismatch(self):
        self.readback["project_content_dir"] = str(self.root / "AnotherProject/Content")
        self.flush()
        with self.assertRaisesRegex(ValueError, "project identities differ"):
            self.register(apply=True)
        self.assert_unmodified()

    def test_legacy_readback_without_project_identity_still_works(self):
        del self.manifest["project_dir"]
        del self.readback["project_content_dir"]
        self.flush()
        self.assertEqual(self.register()["new_count"], 1)

    def test_replacement_failure_leaves_original_and_releases_lock(self):
        with patch("register_icons.os.replace", side_effect=PermissionError("locked by an editor")):
            with self.assertRaises(PermissionError):
                self.register(apply=True)
        self.assert_unmodified()
        self.assertEqual(list(self.root.glob(".resource.txt.*.tmp")), [])

    def test_outside_writer_detected_before_replace(self):
        import register_icons
        write_report = register_icons._write_report
        concurrent = self.old + b"88\t\t/Game/Concurrent.Concurrent\r\n"

        def change_after_plan(path, report):
            write_report(path, report)
            self.table.write_bytes(concurrent)

        with patch("register_icons._write_report", side_effect=change_after_plan):
            with self.assertRaisesRegex(RuntimeError, "changed concurrently"):
                self.register(apply=True)
        self.assertEqual(self.table.read_bytes(), concurrent)
        self.assertFalse(self.table.with_name("resource.txt.lock").exists())

    def test_lf_no_bom_and_no_terminal_newline_preserved(self):
        self.old = b"ID\tDescription\tPath\nId\tDes\tPath\n41\t\t/Game/Legacy.Legacy"
        self.table.write_bytes(self.old)
        report = self.register(apply=True)
        self.assertTrue(self.table.read_bytes().startswith(self.old + b"\n"))
        self.assertNotIn(b"\r", self.table.read_bytes())
        self.assertFalse(self.table.read_bytes().startswith(codecs.BOM_UTF8))
        self.assertEqual(report["newline"], "LF")

    def test_report_cannot_overwrite_table(self):
        self.table = self.root / "resource.prepared.txt"
        self.table.write_bytes(self.old)
        self.output = self.root
        with self.assertRaisesRegex(ValueError, "overwrite an input"):
            self.register(apply=True)
        self.assertEqual(self.table.read_bytes(), self.old)

    def test_id_field_boundaries(self):
        self.assertEqual(split_resource_id(10110001), (10, 11, 1))
        self.assertEqual(split_resource_id(99999999), (99, 99, 9999))
        for resource_id in (10010000, 10000001, 9999999, 100000000, True, 20010001.0):
            with self.subTest(value=resource_id), self.assertRaises(ValueError):
                split_resource_id(resource_id)


if __name__ == "__main__":
    unittest.main()
