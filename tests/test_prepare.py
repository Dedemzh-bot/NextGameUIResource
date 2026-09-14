import json
from pathlib import Path
import subprocess
import sys
import shutil
import unittest
import uuid
import xml.etree.ElementTree as ET
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from classify_resources import classify_directory
from prepare_batch import prepare_batch, setting
from verify_packed_batch import verify_packed_batch


class PrepareTests(unittest.TestCase):
    def setUp(self):
        base = (Path(__file__).resolve().parents[1] / ".local/test-work").resolve()
        self.root = base / ("prepare-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup_fixture, base)
        self.source = self.root / "source"
        self.source.mkdir()
        self.stage = self.root / "stage"
        self.destination = self.root / "published"
        self.manifest = self.root / "manifest.json"

    def cleanup_fixture(self, base):
        if self.root.resolve().parent != base:
            raise ValueError("Test cleanup path escaped fixture directory")
        shutil.rmtree(self.root)

    def image(self, name="pic_demo_001.png", color=(30, 60, 120, 180)):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (2, 3), color).save(path)
        return path

    def prepare(self, **changes):
        kwargs = dict(source=self.source, stage=self.stage, destination=self.destination,
                      system_map={"demo": "Demo"}, manifest_path=self.manifest)
        kwargs.update(changes)
        return prepare_batch(**kwargs)

    def template(self):
        root = ET.Element("data")
        settings = ET.SubElement(root, "struct", type="Settings")

        def entry(parent, key, tag, text=None):
            ET.SubElement(parent, "key").text = key
            node = ET.SubElement(parent, tag)
            node.text = text
            return node

        for key, value in (("textureFileName", "unsafe.png"), ("dataFormat", "wrong"), ("textureFormat", "wrong"),
                           ("outputFormat", "wrong"), ("alphaHandling", "wrong"), ("multiPackMode", "wrong")):
            entry(settings, key, "filename" if key == "textureFileName" else "string", value)
        for key in ("allowRotation", "trimSpriteNames", "prependSmartFolderName"):
            entry(settings, key, "true")
        files = entry(settings, "dataFileNames", "map")
        entry(entry(files, "data", "struct"), "name", "filename", "unsafe.paper2dsprites")
        sprites = entry(settings, "globalSpriteSettings", "struct")
        entry(sprites, "trimMode", "string", "Trim")
        entry(sprites, "scale", "double", "0.5")
        entry(settings, "individualSpriteSettings", "map")
        entry(entry(entry(settings, "fileLists", "map"), "default", "struct"), "files", "array")
        path = self.root / "template.tps"
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
        return path

    def fake_pack(self, data):
        group = data["groups"][0]
        with Image.open(group["files"][0]["original"]) as original:
            original.save(self.stage / group["image_relative"])
        descriptor = {"frames": {group["files"][0]["name"]: {
            "frame": {"x": 0, "y": 0, "w": 2, "h": 3}, "rotated": False, "trimmed": False}},
            "meta": {"image": "Demo.png", "size": {"w": 2, "h": 3}}}
        (self.stage / group["descriptor_relative"]).write_text(json.dumps(descriptor), encoding="utf-8")
        return descriptor

    def test_single_texture_without_template_and_verified_contract(self):
        original = self.image()
        original_bytes = original.read_bytes()
        data = self.prepare(template=self.root / "missing.tps")
        self.assertIsNone(data["template"])
        sealed = verify_packed_batch(self.manifest)
        self.assertEqual(sealed["expected_assets"], ["/Game/UI/Textures/Demo/pic_demo_001.pic_demo_001"])
        self.assertTrue(sealed["packed_verified"])
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertFalse(self.destination.exists())

    def test_all_prefix_routes(self):
        for prefix in ("gui", "icon", "pic", "por"):
            self.image(prefix + "_demo_001.png")
        data = self.prepare(template=self.template())
        self.assertEqual({g["destination"] for g in data["groups"]},
                         {"/Game/UI/UI/Demo", "/Game/UI/ICON/Demo", "/Game/UI/Textures/Demo", "/Game/UI/Portrait/Demo"})

    def test_atlas_template_normalized_and_pixels_verified(self):
        self.image("icon_demo_001.png")
        data = self.prepare(template=self.template())
        group = data["groups"][0]
        settings = ET.parse(self.stage / group["project_relative"]).getroot().find("struct")
        self.assertEqual(setting(settings, "allowRotation").tag, "false")
        self.assertEqual(setting(setting(settings, "globalSpriteSettings"), "trimMode").text, "None")
        self.assertEqual(setting(settings, "dataFormat").text, "unreal-paper2d")
        self.fake_pack(data)
        sealed = verify_packed_batch(self.manifest)
        self.assertEqual(len(sealed["expected_assets"]), 3)
        self.assertEqual(sealed["groups"][0]["files"][0]["asset"], "/Game/UI/ICON/Demo/Frames/icon_demo_001_png.icon_demo_001_png")

    def test_invalid_sources_fail_without_output(self):
        with self.assertRaisesRegex(ValueError, "no PNG"):
            self.prepare()
        self.assertFalse(self.stage.exists())
        (self.source / "other.txt").write_text("test")
        with self.assertRaisesRegex(ValueError, "non-PNG"):
            self.prepare()
        self.assertFalse(self.stage.exists())

    def test_nonexistent_source_does_not_look_empty(self):
        with self.assertRaisesRegex(ValueError, "Source"):
            classify_directory(self.root / "missing")

    def test_overlap_and_manifest_isolation_before_writes(self):
        self.image()
        for changes in ({"stage": self.source / "stage"}, {"destination": self.root},
                        {"destination": self.source / "out"}, {"source": self.stage / "source", "stage": self.stage},
                        {"stage": self.destination / "stage"}, {"manifest_path": self.source / "manifest.json"},
                        {"manifest_path": self.stage / "manifest.json"}, {"manifest_path": self.destination / "manifest.json"}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileExistsError)):
                self.prepare(**changes)
        self.assertFalse(self.stage.exists())
        self.assertFalse(self.destination.exists())

    def test_bad_maps_names_and_collisions_fail_before_writes(self):
        self.image()
        for mapping in ({}, [], {"demo": "../Escape"}, {"demo": "CON"}, {"demo": 42}, {"bad_token": "Demo"},
                        {"demo": "Demo", "other": "DEMO"}, {"other": "Other"}):
            with self.subTest(mapping=mapping), self.assertRaises((ValueError, TypeError)):
                self.prepare(system_map=mapping)
        self.image("nested/pic_demo_001.png")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.prepare()
        self.assertFalse(self.stage.exists())

    def test_empty_filename_token_is_rejected(self):
        self.image("gui__001.png")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.prepare(template=self.template())
        self.assertFalse(self.stage.exists())

    def test_tps_external_path_is_rejected_before_writes(self):
        self.image("icon_demo_001.png")
        template = self.template()
        tree = ET.parse(template)
        settings = tree.getroot().find("struct")
        ET.SubElement(settings, "key").text = "additionalExternalInput"
        ET.SubElement(settings, "filename").text = "../../../../../../external.png"
        tree.write(template, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.prepare(template=template)
        self.assertFalse(self.stage.exists())

    def test_manifest_cannot_omit_sources_or_add_publish_directories(self):
        self.image()
        data = self.prepare()
        self.image("pic_demo_002.png")
        with self.assertRaisesRegex(ValueError, "added or omitted"):
            verify_packed_batch(self.manifest)
        (self.source / "pic_demo_002.png").unlink()
        data["directories"].append("../outside")
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "directory list"):
            verify_packed_batch(self.manifest)

    def test_existing_destination_and_manifest_are_preserved(self):
        self.image()
        target = self.destination / "图片/单图/Demo"
        target.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.manifest.write_text("preserve", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(self.manifest.read_text(encoding="utf-8"), "preserve")
        self.assertFalse(self.stage.exists())

    def test_source_tampering_rejected_under_python_optimization(self):
        self.image()
        self.prepare()
        self.image(color=(1, 2, 3, 4))
        module = Path(__file__).resolve().parents[1] / "verify_packed_batch.py"
        result = subprocess.run([sys.executable, "-O", str(module), str(self.manifest)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bytes changed", result.stderr)
        self.assertNotIn("packed_verified", json.loads(self.manifest.read_text(encoding="utf-8")))

    def test_extra_stage_files_are_not_published(self):
        self.image()
        self.prepare()
        (self.stage / "secret.env").write_text("private", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            verify_packed_batch(self.manifest)

    def test_trimmed_frames_and_pixel_changes_rejected(self):
        self.image("icon_demo_001.png")
        data = self.prepare(template=self.template())
        descriptor = self.fake_pack(data)
        descriptor["frames"]["icon_demo_001.png"]["trimmed"] = True
        descriptor_path = self.stage / data["groups"][0]["descriptor_relative"]
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "trimming"):
            verify_packed_batch(self.manifest)
        self.fake_pack(data)
        Image.new("RGBA", (2, 3), (255, 255, 255, 255)).save(self.stage / data["groups"][0]["image_relative"])
        with self.assertRaisesRegex(ValueError, "pixels differ"):
            verify_packed_batch(self.manifest)


if __name__ == "__main__":
    unittest.main()
