import os
from pathlib import Path
import sys
import shutil
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import TOOL_ROOT, ensure_disjoint, ensure_file_outside, load_settings, select_path


class ConfigTests(unittest.TestCase):
    def setUp(self):
        base = (TOOL_ROOT / ".local/test-work").resolve()
        self.root = base / ("config-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup_fixture, base)

    def cleanup_fixture(self, base):
        if self.root.resolve().parent != base:
            raise ValueError("Test cleanup path escaped fixture directory")
        shutil.rmtree(self.root)

    def test_environment_overrides_env_file_and_explicit_wins(self):
        env_file = self.root / ".env"
        env_file.write_text('NEXTGAME_UI_SOURCE_DIR="relative source"\nNEXTGAME_PROJECT_ROOT=project\nNEXTGAME_UI_OUTPUT_DIR=output\n', encoding="utf-8")
        with patch.dict(os.environ, {"NEXTGAME_UI_SOURCE_DIR": str(self.root / "environment")}, clear=True):
            settings = load_settings(env_file)
        self.assertEqual(settings.source_dir, self.root / "environment")
        self.assertEqual(settings.output_dir, self.root / "output")
        self.assertEqual(settings.table, self.root / "project/Content/Settings/resource/resource.txt")
        self.assertEqual(select_path(self.root / "explicit", settings.source_dir, "NEXTGAME_UI_SOURCE_DIR"), self.root / "explicit")

    def test_optional_paths_do_not_use_example_mappings(self):
        env_file = self.root / ".env"
        env_file.write_text("# empty\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(env_file)
        for field in ("source_dir", "output_dir", "project_root", "table", "system_map", "id_catalog", "id_assignments", "texturepacker", "editor_exe", "nxue_cli"):
            self.assertIsNone(getattr(settings, field))
        self.assertEqual(settings.work_dir, TOOL_ROOT / ".local/work")
        self.assertEqual(settings.tps_template, TOOL_ROOT / "templates/default.tps")

    def test_blank_environment_clears_dotenv_value(self):
        env_file = self.root / ".env"
        env_file.write_text("NEXTGAME_UI_ID_CATALOG=example.json\n", encoding="utf-8")
        with patch.dict(os.environ, {"NEXTGAME_UI_ID_CATALOG": ""}, clear=True):
            self.assertIsNone(load_settings(env_file).id_catalog)

    def test_windows_backslashes_are_literal(self):
        env_file = self.root / ".env"
        env_file.write_text('NEXTGAME_UI_SOURCE_DIR="folder\\new\\test"\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_settings(env_file).source_dir, (self.root / "folder\\new\\test").resolve())

    def test_missing_path_error_names_environment_variable(self):
        with self.assertRaisesRegex(ValueError, "NEXTGAME_UI_SOURCE_DIR"):
            select_path(None, None, "NEXTGAME_UI_SOURCE_DIR")

    def test_missing_explicit_env_file_and_malformed_assignment(self):
        with self.assertRaisesRegex(ValueError, "env-file"):
            load_settings(self.root / "missing")
        env_file = self.root / ".env"
        env_file.write_text("SECRET_VALUE without equals\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            load_settings(env_file)
        self.assertNotIn("SECRET_VALUE", str(caught.exception))

    def test_overlap_is_rejected_both_directions(self):
        for first, second in ((self.root, self.root / "child"), (self.root / "child", self.root), (self.root, self.root)):
            with self.assertRaises(ValueError):
                ensure_disjoint(first, second)
        ensure_disjoint(self.root / "source", self.root / "output")

    def test_manifest_must_not_write_inside_inputs_or_outputs(self):
        roots = [self.root / "source", self.root / "stage", self.root / "output"]
        for root in roots:
            with self.assertRaises(ValueError):
                ensure_file_outside(root / "manifest.json", roots)
        self.assertEqual(ensure_file_outside(self.root / "manifest.json", roots), self.root / "manifest.json")


if __name__ == "__main__":
    unittest.main()
