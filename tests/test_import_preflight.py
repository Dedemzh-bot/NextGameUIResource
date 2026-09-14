"""Offline validation of the exact files and object paths handed to Unreal."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import struct
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch
import uuid
import zlib

TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT))
from import_contract import validate_import_contract


def png_bytes():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = (b"\x00" + bytes((30, 60, 120, 180)) * 2) * 3
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 3, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class ImportPreflightTests(unittest.TestCase):
    def setUp(self):
        self.base = (TOOL_ROOT / ".local/test-work").resolve()
        self.root = self.base / ("import-preflight-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup_fixture)
        self.source = self.root / "source"
        self.stage = self.root / "stage"
        self.output = self.root / "output"
        for path in (self.source, self.stage, self.output):
            path.mkdir()

    def cleanup_fixture(self):
        if self.root.resolve().parent != self.base:
            raise ValueError("Fixture cleanup escaped its root")
        shutil.rmtree(self.root)

    def batch(self, atlas=False, prefix=None):
        prefix = prefix or ("icon" if atlas else "pic")
        routes = {"gui": ("图片", "/Game/UI/UI", False), "icon": ("图标", "/Game/UI/ICON", True),
                  "pic": ("图片", "/Game/UI/Textures", False), "por": ("图标", "/Game/UI/Portrait", True)}
        branch, engine_root, register_icon = routes[prefix]
        name = prefix + "_demo_001.png"
        original = self.source / name
        original.write_bytes(png_bytes())
        relative = f"{branch}/" + ("图集/资源" if atlas else "单图") + "/Demo/" + name
        published_source = self.output / relative
        published_source.parent.mkdir(parents=True, exist_ok=True)
        published_source.write_bytes(original.read_bytes())
        destination = engine_root + "/Demo"
        asset_name = name.replace(".", "_") if atlas else Path(name).stem
        asset = destination + ("/Frames/" if atlas else "/") + asset_name + "." + asset_name
        item = {"original": str(original), "source_relative": name, "source": str(published_source),
                "relative": relative, "name": name, "sha256": hashlib.sha256(png_bytes()).hexdigest(),
                "dimensions": [2, 3], "asset": asset}
        group = {"kind": "atlas" if atlas else "standalone_texture", "branch": branch,
                 "system": "Demo", "destination": destination, "register_icon": register_icon, "files": [item]}
        expected_assets = [asset]
        directories = [Path(relative).parent.as_posix()]
        if atlas:
            output = f"{branch}/图集/图集/Demo"
            directories.append(output)
            for key, suffix in (("project_relative", ".tps"), ("descriptor_relative", ".paper2dsprites"), ("image_relative", ".png")):
                group[key] = output + "/Demo" + suffix
                (self.output / group[key]).parent.mkdir(parents=True, exist_ok=True)
            group["dimensions"] = [2, 3]
            group["sheet_asset"] = destination + "/Demo.Demo"
            group["texture_asset"] = destination + "/Textures/Demo.Demo"
            expected_assets += [group["sheet_asset"], group["texture_asset"]]
            item["frame"] = {"frame": {"x": 0, "y": 0, "w": 2, "h": 3}, "rotated": False, "trimmed": False}
            (self.output / group["image_relative"]).write_bytes(png_bytes())
            (self.output / group["project_relative"]).write_text("<data />", encoding="utf-8")
            descriptor = {"frames": {name: item["frame"]}, "meta": {"image": "Demo.png", "size": {"w": 2, "h": 3}}}
            (self.output / group["descriptor_relative"]).write_text(json.dumps(descriptor), encoding="utf-8")
        publish_files = [{"relative": path.relative_to(self.output).as_posix(),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                         for path in self.output.rglob("*") if path.is_file()]
        return {"source": str(self.source), "stage": str(self.stage), "destination": str(self.output),
                "groups": [group], "expected_assets": expected_assets, "directories": directories,
                "publish_files": publish_files, "packed_verified": True, "all_source_pixels_preserved": True}

    def test_valid_standalone_and_legacy_manifest_without_source_relative(self):
        manifest = self.batch()
        self.assertEqual(validate_import_contract(manifest), set(manifest["expected_assets"]))
        del manifest["groups"][0]["files"][0]["source_relative"]
        self.assertEqual(validate_import_contract(manifest), set(manifest["expected_assets"]))

    def test_valid_atlas_manifest(self):
        manifest = self.batch(atlas=True)
        self.assertEqual(validate_import_contract(manifest), set(manifest["expected_assets"]))

    def test_valid_gui_atlas_route(self):
        manifest = self.batch(atlas=True, prefix="gui")
        self.assertEqual(validate_import_contract(manifest), set(manifest["expected_assets"]))

    def test_valid_portrait_route(self):
        manifest = self.batch(prefix="por")
        self.assertEqual(validate_import_contract(manifest), set(manifest["expected_assets"]))

    def test_real_prepare_verify_output_matches_import_contract(self):
        from prepare_batch import prepare_batch
        from verify_packed_batch import verify_packed_batch
        (self.source / "pic_demo_001.png").write_bytes(png_bytes())
        manifest_path = self.root / "prepared-manifest.json"
        stage = self.root / "prepared-stage"
        prepare_batch(self.source, stage, self.output, system_map={"demo": "Demo"}, manifest_path=manifest_path)
        manifest = verify_packed_batch(manifest_path)
        for row in manifest["publish_files"]:
            destination = self.output / row["relative"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(stage / row["relative"], destination)
        self.assertEqual(validate_import_contract(manifest), set(manifest["expected_assets"]))

    def test_standalone_source_cannot_point_to_another_same_size_png(self):
        manifest = self.batch()
        other = self.root / "other.png"
        other.write_bytes(png_bytes())
        manifest["groups"][0]["files"][0]["source"] = str(other)
        with self.assertRaisesRegex(ValueError, "Actual import source differs"):
            validate_import_contract(manifest)

    def test_atlas_descriptor_cannot_point_to_another_file(self):
        manifest = self.batch(atlas=True)
        manifest["groups"][0]["descriptor_relative"] = "wrong.paper2dsprites"
        with self.assertRaisesRegex(ValueError, "descriptor or output path"):
            validate_import_contract(manifest)

    def test_group_destination_cannot_escape_its_route(self):
        manifest = self.batch()
        for target in ("/Game/Other/Demo", "/Game/UI/ICON/Demo", "/Game/UI/Textures/Other"):
            changed = copy.deepcopy(manifest)
            changed["groups"][0]["destination"] = target
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "Engine group destination"):
                validate_import_contract(changed)

    def test_expected_assets_must_match_exactly(self):
        manifest = self.batch()
        for assets in ([], manifest["expected_assets"] * 2, ["/Game/UI/Textures/Demo/wrong.wrong"],
                       manifest["expected_assets"] + ["/Game/UI/ICON/Demo/Extra.Extra"]):
            changed = copy.deepcopy(manifest)
            changed["expected_assets"] = assets
            with self.subTest(assets=assets), self.assertRaisesRegex(ValueError, "Expected assets"):
                validate_import_contract(changed)

    def test_actual_asset_target_cannot_be_changed(self):
        manifest = self.batch()
        manifest["groups"][0]["files"][0]["asset"] = "/Game/UI/Textures/Demo/wrong.wrong"
        with self.assertRaisesRegex(ValueError, "asset path differs"):
            validate_import_contract(manifest)

    def test_actual_source_must_be_present_in_publication_list(self):
        manifest = self.batch()
        manifest["publish_files"][0]["relative"] = "unrelated.png"
        with self.assertRaisesRegex(ValueError, "missing from the verified"):
            validate_import_contract(manifest)

    def test_publication_checksum_is_rechecked(self):
        manifest = self.batch()
        path = Path(manifest["groups"][0]["files"][0]["source"])
        path.write_bytes(b"changed published source")
        with self.assertRaisesRegex(ValueError, "Published source changed"):
            validate_import_contract(manifest)

    def test_atlas_descriptor_frame_cannot_be_changed(self):
        manifest = self.batch(atlas=True)
        manifest["groups"][0]["files"][0]["frame"]["frame"]["x"] = 1
        with self.assertRaisesRegex(ValueError, "Published frame differs"):
            validate_import_contract(manifest)

    def test_contract_runs_with_only_standard_library(self):
        result = subprocess.run([sys.executable, "-B", "-S", "-c",
                                 "import runpy,sys; runpy.run_path(sys.argv[1]); print('stdlib contract loaded')",
                                 str(TOOL_ROOT / "import_contract.py")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stdlib contract loaded", result.stdout)

    def test_unreal_script_rejects_tampering_before_scheduling_import(self):
        manifest = self.batch()
        manifest["groups"][0]["files"][0]["source"] = str(self.root / "other.png")
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        report = self.root / "readback.json"
        project = self.root / "project"
        (project / "Content").mkdir(parents=True)
        request_path = self.root / "request.json"
        request_path.write_text(json.dumps({"manifest": str(manifest_path), "report": str(report), "project_dir": str(project)}), encoding="utf-8")
        fake_unreal = types.ModuleType("unreal")
        fake_unreal.AssetRegistryHelpers = types.SimpleNamespace(get_asset_registry=Mock(return_value=Mock()))
        fake_unreal.Paths = types.SimpleNamespace(project_content_dir=lambda: str(project / "Content"))
        fake_unreal.register_slate_post_tick_callback = Mock()
        fake_unreal.AssetToolsHelpers = Mock()
        with patch.dict(sys.modules, {"unreal": fake_unreal}):
            with self.assertRaisesRegex(ValueError, "Actual import source differs"):
                runpy.run_path(str(TOOL_ROOT / "import_batch.py"), init_globals={"UI_RESOURCE_ARGS_FILE": str(request_path)})
        fake_unreal.register_slate_post_tick_callback.assert_not_called()
        fake_unreal.AssetToolsHelpers.get_asset_tools.assert_not_called()
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["status"], "preflight_failed")


if __name__ == "__main__":
    unittest.main()
