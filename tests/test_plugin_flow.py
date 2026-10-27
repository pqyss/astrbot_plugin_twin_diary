"""用轻量 AstrBot 桩跑通插件的核心流程。

不依赖真实 AstrBot，只验证插件自身的编排、提示词和落盘逻辑。
"""

import asyncio
import json
import sys
import types
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = PLUGIN_ROOT.parent
_RUNTIME_ROOT = PLUGIN_ROOT / "tests" / "_runtime"


# ---------------------------------------------------------------- AstrBot 桩


class DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakeMessageChain:
    def __init__(self):
        self.chain = []
        self.use_t2i_ = None

    def message(self, text):
        self.chain.append(text)
        return self


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeStarTools:
    data_dir = _RUNTIME_ROOT / "plugin_data"

    @classmethod
    def get_data_dir(cls, name):
        path = cls.data_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path


class FakeFilter:
    @staticmethod
    def command(*args, **kwargs):
        def decorator(func):
            return func

        return decorator


def install_astrbot_stub():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = DummyLogger()
    api.AstrBotConfig = dict

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = FakeStar
    star_mod.StarTools = FakeStarTools

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    event_mod.MessageChain = FakeMessageChain
    event_mod.filter = FakeFilter

    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.event"] = event_mod


install_astrbot_stub()
sys.path.insert(0, str(WORKSPACE_ROOT))
from astrbot_plugin_twin_diary.main import TwinDiaryPlugin  # noqa: E402


# ---------------------------------------------------------------- 假的对象


class FakeConversation:
    def __init__(self, history):
        self.history = json.dumps(history, ensure_ascii=False)
        self.persona_id = None


class FakeConversationManager:
    def __init__(self):
        self.history = [
            {"role": "system", "content": "旧系统提示"},
            {"role": "user", "content": "今天加班到很晚，好累。"},
            {"role": "assistant", "content": "那你别忘了吃饭。"},
            {"role": "user", "content": "/日记 状态"},
        ]

    async def get_curr_conversation_id(self, umo):
        return "conv-1"

    async def get_conversation(self, umo, conversation_id):
        return FakeConversation(self.history)


class FakePersonaManager:
    async def resolve_selected_persona(self, **kwargs):
        return None, {"prompt": "你叫阿岚，嘴硬心软。", "name": "阿岚"}, None, False

    async def get_default_persona_v3(self, umo):
        return {"prompt": "你叫阿岚，嘴硬心软。", "name": "阿岚"}


PUBLIC_TEXT = (
    "10月26日 星期日\n\n"
    + "今天他加班到很晚，我嘴上说随便，其实一直在等消息。" * 6
)
PRIVATE_TEXT = (
    "10月26日 星期日\n\n"
    + "我其实很怕他觉得我烦，所以每次都说你先忙，可我真的不想一个人待着。" * 6
)


class FakeMemoryBridge:
    def __init__(self):
        self.calls = []

    async def compose_context(self, **kwargs):
        self.calls.append(kwargs)
        return "长期记忆：主人怕黑，睡觉要留小灯。"


class FakeMemoryStar:
    def __init__(self, bridge):
        self.name = "astrbot_plugin_memory_companion"
        self.star_cls = SimpleNamespace(memory_companion=bridge)


class FakeContext:
    def __init__(self):
        self.conversation_manager = FakeConversationManager()
        self.persona_manager = FakePersonaManager()
        self.sent = []
        self.llm_calls = []
        self.stars = []

    def get_all_stars(self):
        return self.stars

    def get_config(self, umo=None):
        return {"provider_settings": {}, "admins_id": ["10001"]}

    async def get_current_chat_provider_id(self, umo=None):
        return "fake-provider"

    async def llm_generate(self, **kwargs):
        self.llm_calls.append(kwargs)
        system = kwargs.get("system_prompt", "")
        text = PRIVATE_TEXT if "私密日记" in system else PUBLIC_TEXT
        return SimpleNamespace(completion_text=text)

    async def send_message(self, umo, chain):
        self.sent.append((umo, list(chain.chain)))
        return True


class FakeEvent:
    def __init__(self, text, *, private=True, admin=True):
        self.message_str = text
        self.unified_msg_origin = "napcat:FriendMessage:10001"
        self._private = private
        self._admin = admin

    def get_message_type(self):
        return SimpleNamespace(value="FriendMessage" if self._private else "GroupMessage")

    def get_sender_id(self):
        return "10001"

    def get_sender_name(self):
        return "主人"

    def get_self_id(self):
        return "9999"

    def get_platform_name(self):
        return "aiocqhttp"

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        return ("plain", text)


class PluginFlowTest(unittest.TestCase):
    def setUp(self):
        _RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self.case_dir = _RUNTIME_ROOT / uuid.uuid4().hex
        FakeStarTools.data_dir = self.case_dir / "data" / "plugin_data"
        self.context = FakeContext()
        self.config = {
            "enabled": True,
            "diary_time": "22:30",
            "timezone": "Asia/Shanghai",
            "jitter_seconds": 0,
            "catch_up_hours": 3,
            "master_name": "",
            "bot_name": "",
            "persona_mode": "session",
            "chat_history_enable": True,
            "chat_history_limit": 20,
            "chat_history_max_chars": 1000,
            "memory_enable": False,
            "past_days": 3,
            "past_entry_max_chars": 100,
            "split_public": True,
            "split_max_chars": 120,
            "send_interval_min": 0.01,
            "send_interval_max": 0.02,
            "send_retries": 0,
            "public_min_chars": 200,
            "public_max_chars": 700,
            "private_min_chars": 200,
            "private_max_chars": 800,
        }
        self.plugin = TwinDiaryPlugin(self.context, self.config)
        self.plugin.state["bound_umo"] = "napcat:FriendMessage:10001"
        self.plugin.state["bound_master_id"] = "10001"
        self.plugin.state["bound_master_name"] = "主人"
        self.plugin.store.save_state(self.plugin.state)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.case_dir, ignore_errors=True)

    def test_daily_flow_writes_both_diaries(self):
        ran = asyncio.run(self.plugin._run_daily(trigger="test", force=True))
        self.assertTrue(ran)

        today = self.plugin._now().date()
        public = self.plugin.store.read_diary("public", today)
        private = self.plugin.store.read_diary("private", today)
        self.assertIn("今天他加班到很晚", public)
        self.assertIn("我其实很怕他觉得我烦", private)

        # 公开日记应该被分段发送，私密日记一个字都不应该出现在发送内容里
        sent_text = "\n".join(text for _, chain in self.context.sent for text in chain)
        self.assertIn("今天他加班到很晚", sent_text)
        self.assertNotIn("我其实很怕他觉得我烦", sent_text)
        self.assertGreaterEqual(len(self.context.sent), 2)

        # 两次模型调用：公开 + 私密
        self.assertEqual(len(self.context.llm_calls), 2)
        private_call = self.context.llm_calls[1]
        self.assertIn("没有读者", private_call["system_prompt"])
        self.assertIn("今晚你已经写给对方的那一篇", private_call["prompt"])
        self.assertIn("今天他加班到很晚", private_call["prompt"])

        self.assertEqual(self.plugin.state["last_public_date"], today.isoformat())
        self.assertEqual(self.plugin.state["last_private_date"], today.isoformat())

    def test_second_run_is_skipped(self):
        asyncio.run(self.plugin._run_daily(trigger="test", force=True))
        ran = asyncio.run(self.plugin._run_daily(trigger="test", force=False))
        self.assertFalse(ran)
        self.assertEqual(len(self.context.llm_calls), 2)

    def test_status_and_help(self):
        status = self.plugin._status_text()
        self.assertIn("双面日记", status)
        self.assertIn("napcat 私聊 10001", status)
        self.assertIn("22:30", status)
        help_text = self.plugin._help_text()
        self.assertIn("/日记 绑定", help_text)

    def test_failed_send_keeps_pending_and_private(self):
        async def failing_send(umo, chain):
            return False

        self.context.send_message = failing_send
        with self.assertRaises(RuntimeError):
            asyncio.run(self.plugin._run_daily(trigger="test", force=True))

        pending = self.plugin.state["pending_public"]
        self.assertIsInstance(pending, dict)
        self.assertTrue(pending["text"])
        self.assertTrue(self.plugin.store.read_diary("private", self.plugin._now().date()))
        self.assertNotEqual(self.plugin.state["last_public_date"], self.plugin._now().date().isoformat())

    def test_command_help_parses_tokens(self):
        async def collect(agen):
            return [item async for item in agen]

        event = FakeEvent("/日记 帮助")
        results = asyncio.run(collect(self.plugin.diary_command(event)))
        self.assertIn("双面日记", results[0][1])
        self.assertIn("/日记 绑定", results[0][1])

    def test_bind_command_saves_session(self):
        async def collect(agen):
            return [item async for item in agen]

        self.plugin.state["bound_umo"] = ""
        event = FakeEvent("/日记 绑定")
        results = asyncio.run(collect(self.plugin._cmd_bind(event)))
        self.assertEqual(results[0][0], "plain")
        self.assertIn("绑定好了", results[0][1])
        self.assertEqual(
            self.plugin.state["bound_umo"],
            "napcat:FriendMessage:10001",
        )
        self.assertEqual(self.plugin.state["bound_master_name"], "主人")

    def test_bind_rejects_group(self):
        async def collect(agen):
            return [item async for item in agen]

        event = FakeEvent("/日记 绑定", private=False)
        results = asyncio.run(collect(self.plugin._cmd_bind(event)))
        self.assertIn("私聊", results[0][1])

    def test_memory_bridge_is_used(self):
        bridge = FakeMemoryBridge()
        self.context.stars = [FakeMemoryStar(bridge)]
        self.config["memory_enable"] = True
        asyncio.run(self.plugin._run_daily(trigger="test", force=True))
        self.assertEqual(len(bridge.calls), 2)
        self.assertEqual(bridge.calls[0]["session_context"]["scope"], "private")
        for call in self.context.llm_calls:
            self.assertIn("主人怕黑", call["prompt"])

    def test_private_view_sends_directly_to_private(self):
        async def collect(agen):
            return [item async for item in agen]

        self.plugin.store.save_diary("private", self.plugin._now().date(), "秘密正文")
        self.config["allow_private_view"] = True
        event = FakeEvent("/日记 私密", private=True)
        results = asyncio.run(collect(self.plugin._cmd_view(event, "private", "")))
        sent_text = "\n".join(text for _, chain in self.context.sent for text in chain)
        reply_text = "\n".join(text for _, text in results)
        self.assertIn("秘密正文", sent_text)
        self.assertNotIn("秘密正文", reply_text)
        self.assertIn("单独", reply_text)

    def test_private_view_is_rejected_in_group(self):
        async def collect(agen):
            return [item async for item in agen]

        self.plugin.store.save_diary("private", self.plugin._now().date(), "秘密正文")
        self.config["allow_private_view"] = True
        event = FakeEvent("/日记 私密", private=False)
        results = asyncio.run(collect(self.plugin._cmd_view(event, "private", "")))
        joined = "\n".join(text for _, text in results)
        self.assertIn("私聊", joined)
        self.assertNotIn("秘密正文", joined)

    def test_tick_runs_without_waiting_when_catchup_zero(self):
        fixed = datetime(
            2026,
            10,
            26,
            22,
            30,
            10,
            tzinfo=timezone(timedelta(hours=8)),
        )
        self.plugin._now = lambda: fixed
        self.config["catch_up_hours"] = 0
        self.config["jitter_seconds"] = 45
        called = []

        async def fake_run(**kwargs):
            called.append(kwargs)

        self.plugin._run_daily = fake_run
        asyncio.run(self.plugin._tick())
        self.assertTrue(called)

    def test_delivery_resumes_without_resending_first_chunk(self):
        text = "A" * 90 + "。" + "B" * 90 + "。" + "C" * 90 + "。"
        self.config["split_max_chars"] = 100
        day = self.plugin._now().date()
        umo = "napcat:FriendMessage:10001"
        progress = {
            "date": day.isoformat(),
            "text": text,
            "sent": 0,
            "umo": umo,
            "split_max_chars": 100,
            "split_public": True,
        }
        calls = []

        async def failing_send(_umo, chunk, attempts=0):
            calls.append(chunk)
            return len(calls) != 2

        self.plugin._send = failing_send

        async def on_progress(sent):
            progress["sent"] = sent

        remaining = asyncio.run(
            self.plugin._deliver_public(text, day, umo, progress, on_progress=on_progress),
        )
        self.assertIsNotNone(remaining)
        self.assertEqual(progress["sent"], 1)
        first_chunk = calls[0]
        calls.clear()

        async def ok_send(_umo, chunk, attempts=0):
            calls.append(chunk)
            return True

        self.plugin._send = ok_send
        remaining = asyncio.run(
            self.plugin._deliver_public(text, day, umo, progress, on_progress=on_progress),
        )
        self.assertIsNone(remaining)
        self.assertNotIn(first_chunk, calls)
        self.assertEqual(len(calls), 2)

    def test_group_target_is_refused(self):
        self.plugin.state["bound_umo"] = "napcat:GroupMessage:123456"
        with self.assertRaises(RuntimeError):
            asyncio.run(self.plugin._run_daily(trigger="test", force=True))
        self.assertEqual(self.context.llm_calls, [])

    def test_private_failure_does_not_block_public(self):
        async def fake_llm(**kwargs):
            if "私密日记" in kwargs.get("system_prompt", ""):
                raise RuntimeError("boom")
            return SimpleNamespace(completion_text=PUBLIC_TEXT)

        self.context.llm_generate = fake_llm
        ran = asyncio.run(self.plugin._run_daily(trigger="test", force=True))
        self.assertTrue(ran)
        self.assertIsNotNone(self.plugin.store.read_diary("public", self.plugin._now().date()))
        self.assertIsNone(self.plugin.store.read_diary("private", self.plugin._now().date()))
        self.assertIn("私密日记生成失败", self.plugin.state["last_error"])

    def test_private_diary_can_be_disabled(self):
        self.config["private_diary_enabled"] = False
        asyncio.run(self.plugin._run_daily(trigger="test", force=True))
        self.assertIsNone(self.plugin.store.read_diary("private", self.plugin._now().date()))
        sent_text = "\n".join(text for _, chain in self.context.sent for text in chain)
        self.assertIn("今天他加班到很晚", sent_text)


if __name__ == "__main__":
    unittest.main()
