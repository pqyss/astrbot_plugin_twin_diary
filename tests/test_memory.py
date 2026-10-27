"""聊天素材与记忆插件连接的单元测试。"""

import asyncio
import json
import unittest
from types import SimpleNamespace

from core.memory import MaterialCollector


class DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class FakeConversation:
    def __init__(self, history):
        self.history = json.dumps(history, ensure_ascii=False)
        self.persona_id = None


class FakeConversationManager:
    def __init__(self, history):
        self.history = history

    async def get_curr_conversation_id(self, umo):
        return "conv-1" if self.history is not None else None

    async def get_conversation(self, umo, conversation_id):
        return FakeConversation(self.history) if self.history is not None else None


class FakeBridge:
    def __init__(self, text="长期记忆：主人喜欢下雨天。", fail=False):
        self.text = text
        self.fail = fail
        self.calls = []

    async def compose_context(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("boom")
        return self.text


class FakeStar:
    def __init__(self, name, plugin=None):
        self.name = name
        self.star_cls = plugin


class FakeContext:
    def __init__(self, history=None, bridge=None, bridge_name="astrbot_plugin_memory_companion"):
        self.conversation_manager = FakeConversationManager(history)
        plugin = SimpleNamespace(memory_companion=bridge) if bridge is not None else None
        self.stars = [FakeStar(bridge_name, plugin)] if plugin is not None else []

    def get_all_stars(self):
        return self.stars


class MemoryTest(unittest.TestCase):
    def test_collect_chat_renders_roles_and_skips_meta(self):
        history = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "/日记 绑定"},
            {"role": "user", "content": "今天加班到很晚"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "那你别忘了吃饭。"},
                    {"type": "image_url", "image_url": {"url": "http://x"}},
                ],
            },
            {"role": "tool", "content": "工具结果"},
        ]
        collector = MaterialCollector(FakeContext(history=history), logger=DummyLogger())
        text = asyncio.run(
            collector.collect_chat(
                "napcat:FriendMessage:123",
                limit=10,
                max_chars=1000,
                user_label="主人",
            ),
        )
        self.assertIn("主人：今天加班到很晚", text)
        self.assertIn("我：那你别忘了吃饭。[图片]", text)
        self.assertNotIn("系统提示", text)
        self.assertNotIn("/日记 绑定", text)
        self.assertNotIn("工具结果", text)

    def test_collect_chat_respects_budget(self):
        history = [{"role": "user", "content": "很长的消息" * 100} for _ in range(20)]
        collector = MaterialCollector(FakeContext(history=history), logger=DummyLogger())
        text = asyncio.run(
            collector.collect_chat(
                "napcat:FriendMessage:123",
                limit=20,
                max_chars=300,
                user_label="主人",
            ),
        )
        self.assertLessEqual(len(text), 400)

    def test_collect_chat_without_conversation(self):
        collector = MaterialCollector(FakeContext(history=None), logger=DummyLogger())
        self.assertEqual(
            asyncio.run(
                collector.collect_chat(
                    "napcat:FriendMessage:123",
                    limit=10,
                    max_chars=1000,
                ),
            ),
            "",
        )

    def test_collect_long_term_uses_bridge(self):
        bridge = FakeBridge("记忆包内容")
        collector = MaterialCollector(FakeContext(bridge=bridge), logger=DummyLogger())
        text = asyncio.run(
            collector.collect_long_term(
                query="今天发生了什么",
                umo="napcat:FriendMessage:123",
                top_k=4,
                max_chars=500,
                timeout=5,
                user_id="123",
                user_name="主人",
            ),
        )
        self.assertEqual(text, "记忆包内容")
        self.assertTrue(bridge.calls)
        self.assertEqual(bridge.calls[0]["top_k"], 4)
        self.assertEqual(bridge.calls[0]["session_context"]["scope"], "private")

    def test_collect_long_term_without_plugin(self):
        collector = MaterialCollector(FakeContext(bridge=None), logger=DummyLogger())
        text = asyncio.run(
            collector.collect_long_term(
                query="今天发生了什么",
                umo="napcat:FriendMessage:123",
                top_k=4,
                max_chars=500,
                timeout=5,
            ),
        )
        self.assertEqual(text, "")
        self.assertFalse(collector.memory_plugin_available())

    def test_bridge_failure_does_not_raise(self):
        bridge = FakeBridge(fail=True)
        collector = MaterialCollector(FakeContext(bridge=bridge), logger=DummyLogger())
        text = asyncio.run(
            collector.collect_long_term(
                query="今天发生了什么",
                umo="napcat:FriendMessage:123",
                top_k=4,
                max_chars=500,
                timeout=5,
            ),
        )
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
