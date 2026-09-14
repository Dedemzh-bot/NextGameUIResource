"""Regression checks for routing and resource registry data protection."""
from pathlib import Path
from contextlib import contextmanager
import shutil
import uuid
import unittest
from classify_resources import classify_directory
from register_icons import parse_table


@contextmanager
def test_directory():
    base = (Path(__file__).resolve().parent / ".local/tests").resolve()
    root = base / ("rules-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        if root.resolve().parent != base or not root.name.startswith("rules-"):
            raise RuntimeError("Cleanup escaped test directory")
        shutil.rmtree(root)


class Rules(unittest.TestCase):
    def test_first_word_and_portrait_route(self):
        with test_directory() as temp:
            root = Path(temp)
            for name in ("gui_parkour_icon.png", "icon_skill_pic.png", "por_puzzle_001.png",
                         "pic_icon_001.png", "portrait_test.png", "POR_puzzle.png", "gui.png", "por_puzzle.txt"):
                (root / name).write_bytes(b"test")
            report = classify_directory(root)
            self.assertEqual(report["counts"], {"atlas": 2, "standalone_texture": 2, "unmatched": 3, "other_files": 1})
            singles = {x["prefix"]: x for x in report["groups"]["standalone_texture"]}
            self.assertEqual(singles["por"]["engine_root"], "/Game/UI/Portrait")
            self.assertTrue(singles["por"]["register_icon"])
            self.assertFalse(singles["pic"]["register_icon"])

    def table(self, rows):
        return ("\ufeffId\t//描述\t资源完整路径\r\nId\tDes\tPath\r\n" + rows).encode("utf-8")

    def test_blank_description(self):
        ids, paths = parse_table(self.table("10000081\t\t/Game/UI/ICON/Skill/F.F\r\n"))
        self.assertEqual(ids[10000081], "/Game/UI/ICON/Skill/F.F")
        self.assertEqual(paths["/Game/UI/ICON/Skill/F.F"], 10000081)

    def test_reject_duplicate_id(self):
        with self.assertRaises(ValueError):
            parse_table(self.table("1\t\t/Game/A.A\r\n1\t\t/Game/B.B\r\n"))

    def test_reject_duplicate_path(self):
        with self.assertRaises(ValueError):
            parse_table(self.table("1\t\t/Game/A.A\r\n2\t\t/Game/A.A\r\n"))

    def test_reject_missing_description_column(self):
        with self.assertRaises(ValueError):
            parse_table(self.table("1\t/Game/A.A\r\n"))


if __name__ == "__main__":
    unittest.main()
