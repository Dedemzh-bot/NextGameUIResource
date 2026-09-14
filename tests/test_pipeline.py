"""Offline integration: synthetic PNGs and simulated saved assets, no live Editor."""
import codecs
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare_batch import prepare_batch, sha, setting
from ui_resources import pack_batch, publish_batch, check_tps_before_pack
from engine_bridge import dispatch_import, wait_for_report, write_json
from register_icons import register_resources, parse_table
from verify_packed_batch import verify_packed_batch


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.base = (ROOT / ".local/tests").resolve()
        self.root = self.base / ("pipeline-" + uuid.uuid4().hex)
        self.source = self.root / "input"
        self.source.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.manifest_path = self.root / "batch/manifest.json"
        self.stage = self.root / "batch/stage"
        self.output = self.root / "output"
        self.project = self.root / "Project"
        self.project.mkdir()

    def cleanup(self):
        if self.root.resolve().parent != self.base or not self.root.name.startswith("pipeline-"):
            raise RuntimeError("Invalid cleanup path")
        shutil.rmtree(self.root)

    def batch(self, prefixes=("gui", "icon", "pic", "por")):
        for prefix in prefixes:
            Image.new("RGBA", (4, 6), (23, 56, 89, 120)).save(self.source / f"{prefix}_demo_001.png")
        data = prepare_batch(self.source, self.stage, self.output,
                             template=ROOT / "templates/default.tps", system_map={"demo": "Demo"},
                             manifest_path=self.manifest_path)
        data["project_dir"] = str(self.project)
        write_json(self.manifest_path, data)
        return data

    def fake_pack(self, data):
        for group in data["groups"]:
            if group["kind"] != "atlas":
                continue
            check_tps_before_pack(data, group)
            item = group["files"][0]
            shutil.copyfile(item["original"], self.stage / group["image_relative"])
            write_json(self.stage / group["descriptor_relative"], {
                "frames": {item["name"]: {"frame": {"x": 0, "y": 0, "w": 4, "h": 6},
                          "rotated": False, "trimmed": False}},
                "meta": {"image": "Demo.png", "size": {"w": 4, "h": 6}}})
        return verify_packed_batch(self.manifest_path)

    def fake_readback(self, data):
        classes = {}
        for group in data["groups"]:
            if group["kind"] == "atlas":
                classes[group["sheet_asset"]] = "/Script/Paper2D.PaperSpriteSheet"
                classes[group["texture_asset"]] = "/Script/Engine.Texture2D"
            for item in group["files"]:
                classes[item["asset"]] = "/Script/Paper2D.PaperSprite" if group["kind"] == "atlas" else "/Script/Engine.Texture2D"
        rows = []
        for asset, class_name in classes.items():
            path = self.project / "Content" / (asset.split(".")[0].removeprefix("/Game/") + ".uasset")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic saved asset: " + asset, encoding="utf-8")
            rows.append({"path": asset, "class": class_name,
                         "saved_file": {"path": str(path), "bytes": path.stat().st_size, "sha256": sha(path)}})
        result = self.manifest_path.parent / "readback.json"
        write_json(result, {"success": True, "status": "complete", "manifest_sha256": sha(self.manifest_path),
                   "project_dir": str(self.project), "project_content_dir": str(self.project / "Content"), "assets": rows})
        return result

    def test_classify_pack_publish_and_register_all_four_routes(self):
        data = self.batch()
        originals = {p: sha(p) for p in self.source.iterdir()}
        data = self.fake_pack(data)
        self.assertEqual(len(data["expected_assets"]), 8)
        self.assertEqual(publish_batch(self.manifest_path)["file_count"], 10)
        self.assertEqual(publish_batch(self.manifest_path)["file_count"], 10)
        readback = self.fake_readback(data)
        table = self.root / "resource.txt"
        old = codecs.BOM_UTF8 + b"Id\tDescription\tPath\r\nId\tDes\tPath\r\n42\tlegacy\t/Game/Old.Old\r\n"
        table.write_bytes(old)
        catalog, assignments = self.root / "catalog.json", self.root / "assignments.json"
        write_json(catalog, {"version": 1, "categories": [{"major": 73, "minor": 84, "name": "Project custom"}]})
        write_json(assignments, {"version": 1, "resources": [
            {"file": f"{prefix}_demo_001.png", "major": 73, "minor": 84, "description": prefix}
            for prefix in ("icon", "por")]})
        kwargs = dict(catalog_path=catalog, assignments_path=assignments)
        plan = register_resources(self.manifest_path, readback, table, self.root / "registry", **kwargs)
        self.assertFalse(plan["applied"])
        self.assertEqual(table.read_bytes(), old)
        result = register_resources(self.manifest_path, readback, table, self.root / "registry", apply=True, **kwargs)
        self.assertEqual(result["new_count"], 2)
        self.assertTrue(table.read_bytes().startswith(old))
        self.assertEqual(set(parse_table(table.read_bytes())[0]), {42, 73840001, 73840002})
        again = register_resources(self.manifest_path, readback, table, self.root / "registry", apply=True, **kwargs)
        self.assertEqual(again["new_count"], 0)
        self.assertEqual({p: sha(p) for p in self.source.iterdir()}, originals)

    def test_single_image_pack_without_texturepacker(self):
        self.batch(("pic",))
        self.assertTrue(pack_batch(self.manifest_path)["packed_verified"])

    def test_publish_rejects_modified_destination_overlap(self):
        data = self.fake_pack(self.batch(("pic",)))
        for destination in (self.source, self.stage / "nested"):
            data["destination"] = str(destination)
            write_json(self.manifest_path, data)
            with self.assertRaises(ValueError):
                publish_batch(self.manifest_path)
        self.assertFalse((self.stage / "nested").exists())

    def test_publish_rejects_path_escape_and_changed_output(self):
        data = self.fake_pack(self.batch(("pic",)))
        publish_batch(self.manifest_path)
        target = self.output / data["publish_files"][0]["relative"]
        target.write_bytes(b"intervening edit")
        with self.assertRaises(FileExistsError):
            publish_batch(self.manifest_path)
        data["publish_files"][0]["relative"] = "../escaped.png"
        write_json(self.manifest_path, data)
        with self.assertRaises(ValueError):
            publish_batch(self.manifest_path)

    def test_tps_external_output_rejected_before_subprocess(self):
        data = self.batch(("icon",))
        project = self.stage / data["groups"][0]["project_relative"]
        tree = ET.parse(project)
        setting(tree.getroot().find("struct"), "textureFileName").text = str(self.root / "escape.png")
        tree.write(project, encoding="utf-8")
        with patch("ui_resources.subprocess.run") as run:
            with self.assertRaises(ValueError):
                pack_batch(self.manifest_path, sys.executable)
            run.assert_not_called()

    def test_manual_dispatch_and_project_identity(self):
        data = self.fake_pack(self.batch(("pic",)))
        publish_batch(self.manifest_path)
        report = dispatch_import(self.manifest_path, self.project, backend="manual")
        launch = report.parent / "launch_import.py"
        compile(launch.read_text(encoding="utf-8"), str(launch), "exec")
        request = json.loads((report.parent / "import-request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["project_dir"], str(self.project.resolve()))
        with self.assertRaises(ValueError):
            dispatch_import(self.manifest_path, self.root, backend="manual")
        write_json(report, {"success": True, "status": "complete"})
        self.assertTrue(wait_for_report(report, 1)["success"])
        with self.assertRaises(FileExistsError):
            dispatch_import(self.manifest_path, self.project, backend="manual")


if __name__ == "__main__":
    unittest.main()
