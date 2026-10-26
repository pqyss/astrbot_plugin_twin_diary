"""提示词构造的单元测试。"""

import unittest
from datetime import datetime, timezone

from core.prompts import DiaryPromptInput, build_private_prompt, build_public_prompt


def make_input(**overrides):
    data = {
        "moment": datetime(2026, 10, 26, 22, 30, tzinfo=timezone.utc),
        "persona": "你叫阿岚，说话有点嘴硬，心软。",
        "persona_name": "阿岚",
        "bot_name": "阿岚",
        "master_name": "主人",
        "chat_material": "主人：今天加班到很晚。\n我：那你别忘了吃饭。",
        "memory_material": "主人最近在准备考试，压力很大。",
        "past_public": "2026-10-25：昨天他带我去看了海。",
        "past_private": "2026-10-25：我其实很怕他觉得我烦。",
        "style_extra": "",
        "min_chars": 350,
        "max_chars": 700,
        "include_date_line": True,
        "public_draft": "",
    }
    data.update(overrides)
    return DiaryPromptInput(**data)


class PromptTest(unittest.TestCase):
    def test_public_prompt_contains_persona_and_materials(self):
        system, user = build_public_prompt(make_input())
        self.assertIn("阿岚", system)
        self.assertIn("对方会看到", system)
        self.assertIn("今天加班到很晚", user)
        self.assertIn("准备考试", user)
        self.assertIn("昨天他带我去看了海", user)
        self.assertNotIn("我其实很怕他觉得我烦", user + system)
        self.assertIn("350 到 700 字", system)
        self.assertIn("10月26日 星期一", system)
        self.assertIn("不必全篇统一", user)
        self.assertIn("优先用那个", user)

    def test_public_prompt_does_not_include_private(self):
        system, user = build_public_prompt(make_input())
        self.assertNotIn("我其实很怕他觉得我烦", system)
        self.assertNotIn("我其实很怕他觉得我烦", user)

    def test_private_prompt_contains_public_draft_and_private_past(self):
        system, user = build_private_prompt(
            make_input(public_draft="今晚写给主人的日记正文。"),
        )
        self.assertIn("没有读者", system)
        self.assertIn("不会被发送", system)
        self.assertIn("我其实很怕他觉得我烦", user)
        self.assertIn("今晚写给主人的日记正文", user)
        self.assertIn("这一篇要写它没写进去的部分", user)

    def test_date_line_can_be_disabled(self):
        system, _ = build_public_prompt(make_input(include_date_line=False))
        self.assertIn("不要写日期抬头", system)
        self.assertNotIn("正文第一行只写日期和星期", system)

    def test_style_extra_is_appended(self):
        _, user = build_public_prompt(make_input(style_extra="每段不要超过三句。"))
        self.assertIn("每段不要超过三句", user)

    def test_empty_materials_have_fallbacks(self):
        _, user = build_public_prompt(
            make_input(chat_material="", memory_material="", past_public=""),
        )
        self.assertIn("今天没有可用的聊天记录", user)
        self.assertIn("没有可用的长期记忆", user)
        self.assertIn("这是第一篇", user)


if __name__ == "__main__":
    unittest.main()
