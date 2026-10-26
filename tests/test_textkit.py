"""文本工具与时间工具的单元测试。"""

import unittest
from datetime import date, datetime, time, timedelta, timezone

from core.schedule import next_run_at, should_run_today, target_for_day
from core.textkit import (
    clean_llm_text,
    parse_date_arg,
    parse_hhmm,
    split_for_chat,
    truncate,
    weekday_cn,
)


class TextKitTest(unittest.TestCase):
    def test_parse_hhmm(self):
        self.assertEqual(parse_hhmm("22:30"), time(22, 30))
        self.assertEqual(parse_hhmm("7:05"), time(7, 5))
        with self.assertRaises(ValueError):
            parse_hhmm("25:00")
        with self.assertRaises(ValueError):
            parse_hhmm("晚上十点")

    def test_weekday(self):
        self.assertEqual(weekday_cn(date(2026, 10, 26)), "星期一")

    def test_parse_date_arg(self):
        base = date(2026, 10, 26)
        self.assertEqual(parse_date_arg("", base), base)
        self.assertEqual(parse_date_arg("昨天", base), date(2026, 10, 25))
        self.assertEqual(parse_date_arg("10-20", base), date(2026, 10, 20))
        self.assertEqual(parse_date_arg("2025-01-02", base), date(2025, 1, 2))
        with self.assertRaises(ValueError):
            parse_date_arg("昨天前天", base)

    def test_clean_llm_text(self):
        raw = """```markdown
私密日记：

# 10月26日 星期日
**今天**被主人气到了。
- 第一件事
`代码`
```
"""
        cleaned = clean_llm_text(raw)
        self.assertIn("10月26日 星期日", cleaned)
        self.assertIn("今天被主人气到了", cleaned)
        self.assertNotIn("```", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("私密日记：", cleaned)

    def test_clean_keeps_diary_body(self):
        raw = "10月26日 星期日\n\n今天什么也没做，但也不算太坏。"
        self.assertEqual(clean_llm_text(raw), raw)

    def test_split_respects_limit(self):
        text = "\n\n".join(["这是第一段。" * 10, "这是第二段。" * 10, "这是第三段。" * 10])
        chunks = split_for_chat(text, 80)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 80)
        joined = "".join(chunks)
        self.assertIn("这是第一段", joined)
        self.assertIn("这是第三段", joined)

    def test_split_long_sentence_without_punctuation(self):
        chunks = split_for_chat("啊" * 500, 80)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))
        self.assertEqual("".join(chunks), "啊" * 500)

    def test_split_short_returns_single(self):
        self.assertEqual(split_for_chat("短日记", 200), ["短日记"])
        self.assertEqual(split_for_chat("", 200), [])

    def test_truncate(self):
        self.assertEqual(truncate("abcdef", 10), "abcdef")
        self.assertEqual(truncate("abcdef", 4), "abc…")


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tz = timezone(timedelta(hours=8))
        self.at = time(22, 30)

    def test_target_for_day(self):
        now = datetime(2026, 10, 26, 12, 0, tzinfo=self.tz)
        target = target_for_day(now, self.at)
        self.assertEqual(target, datetime(2026, 10, 26, 22, 30, tzinfo=self.tz))

    def test_next_run_before_and_after(self):
        before = datetime(2026, 10, 26, 12, 0, tzinfo=self.tz)
        self.assertEqual(
            next_run_at(before, self.at).day,
            26,
        )
        after = datetime(2026, 10, 26, 23, 0, tzinfo=self.tz)
        self.assertEqual(next_run_at(after, self.at).day, 27)

    def test_should_run_within_catchup(self):
        now = datetime(2026, 10, 26, 23, 30, tzinfo=self.tz)
        self.assertTrue(
            should_run_today(now, self.at, catch_up_hours=3, last_done_date=""),
        )
        self.assertFalse(
            should_run_today(now, self.at, catch_up_hours=3, last_done_date="2026-10-26"),
        )

    def test_should_not_run_before_or_too_late(self):
        before = datetime(2026, 10, 26, 22, 0, tzinfo=self.tz)
        self.assertFalse(
            should_run_today(before, self.at, catch_up_hours=3, last_done_date=""),
        )
        late = datetime(2026, 10, 27, 3, 0, tzinfo=self.tz)
        self.assertFalse(
            should_run_today(late, self.at, catch_up_hours=3, last_done_date=""),
        )


if __name__ == "__main__":
    unittest.main()
