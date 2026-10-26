"""存储与路径解析的单元测试。"""

import json
import shutil
import unittest
import uuid
from datetime import date
from pathlib import Path

from core.storage import DiaryStore, build_paths, record_run

_RUNTIME_ROOT = Path(__file__).resolve().parent / "_runtime"


class StorageTest(unittest.TestCase):
    def setUp(self):
        _RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = _RUNTIME_ROOT / uuid.uuid4().hex
        self.data_dir = self.root / "data"
        self.plugin_dir = self.data_dir / "plugin_data" / "astrbot_plugin_twin_diary"
        self.paths = build_paths(
            data_dir=self.data_dir,
            plugin_data_dir=self.plugin_dir,
            public_setting="",
            private_setting="",
        )
        self.store = DiaryStore(self.paths)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_default_paths(self):
        self.assertEqual(self.paths.public_dir, (self.plugin_dir / "public").resolve())
        self.assertEqual(self.paths.private_dir, (self.plugin_dir / "private").resolve())
        self.assertTrue(self.paths.public_dir.is_dir())
        self.assertTrue(self.paths.private_dir.is_dir())

    def test_relative_and_absolute_paths(self):
        paths = build_paths(
            data_dir=self.data_dir,
            plugin_data_dir=self.plugin_dir,
            public_setting="日记/公开",
            private_setting=str(self.root / "secret"),
        )
        self.assertEqual(paths.public_dir, (self.data_dir / "日记" / "公开").resolve())
        self.assertEqual(paths.private_dir, (self.root / "secret").resolve())

    def test_save_and_read(self):
        day = date(2026, 10, 26)
        path = self.store.save_diary("public", day, "今天写了很多东西。\n\n第二段。")
        self.assertTrue(path.exists())
        self.assertEqual(
            self.store.read_diary("public", day),
            "今天写了很多东西。\n\n第二段。",
        )
        self.assertIsNone(self.store.read_diary("private", day))

    def test_recent_diaries_order_and_before(self):
        for day in (date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)):
            self.store.save_diary("public", day, f"{day.isoformat()} 的日记内容")
        recent = self.store.recent_diaries(
            "public",
            days=2,
            max_chars=100,
            before=date(2026, 10, 22),
        )
        self.assertEqual([item[0] for item in recent], ["2026-10-21", "2026-10-20"])

    def test_state_roundtrip(self):
        state = self.store.load_state()
        self.assertEqual(state["bound_umo"], "")
        state["bound_umo"] = "napcat:FriendMessage:123"
        state["last_public_date"] = "2026-10-26"
        self.store.save_state(state)
        loaded = self.store.load_state()
        self.assertEqual(loaded["bound_umo"], "napcat:FriendMessage:123")
        self.assertEqual(loaded["last_public_date"], "2026-10-26")

    def test_corrupt_state_returns_default(self):
        self.paths.state_path.write_text("not json", encoding="utf-8")
        state = self.store.load_state()
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["runs"], [])

    def test_record_run_limit(self):
        state = {"runs": []}
        for index in range(5):
            record_run(state, {"index": index}, limit=3)
        self.assertEqual(len(state["runs"]), 3)
        self.assertEqual(state["runs"][-1]["index"], 4)

    def test_state_file_is_readable_json(self):
        self.store.save_state(self.store.load_state())
        raw = self.paths.state_path.read_text(encoding="utf-8")
        self.assertIsInstance(json.loads(raw), dict)


if __name__ == "__main__":
    unittest.main()
