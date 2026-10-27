"""双面日记：每天用当前人设写两篇日记。

一篇发给主人看，另一篇只写到本机的私密目录。
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.memory import MaterialCollector
from .core.prompts import DiaryPromptInput, build_private_prompt, build_public_prompt
from .core.schedule import next_run_at, should_run_today
from .core.storage import DiaryStore, build_paths, record_run
from .core.textkit import (
    clean_llm_text,
    format_day_header,
    looks_like_refusal,
    parse_date_arg,
    parse_hhmm,
    split_for_chat,
    truncate,
)

PLUGIN_NAME = "astrbot_plugin_twin_diary"
PLUGIN_VERSION = "1.0.0"
DEFAULT_DIARY_TIME = "22:30"
_FALLBACK_PERSONA_CN = (
    "你是一个有自己性格、生活和情绪的人。你有自己的喜恶、小脾气和在意的事，"
    "说话自然，不端着，也不像客服。"
)


class TwinDiaryPlugin(Star):
    """每天在指定时间写两篇日记：一篇给主人看，一篇只留在本机。

    公开日记会发送到绑定的私聊会话；私密日记只写入配置的文件夹，不会主动发送。
    两篇日记都会使用当前会话的人设，并参考聊天记录、长期记忆和旧日记。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """初始化插件并读取本地状态。

        Args:
            context: AstrBot 插件上下文。
            config: WebUI 中的插件配置。
        """
        super().__init__(context)
        self.context = context
        self.config: dict = config or {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()
        self._jitter_until = 0.0
        self._cooldown_until = 0.0
        self._persona_name_cache = ""
        self._last_error_notice = ""
        self._last_private_failed = False

        self.plugin_data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir = self.plugin_data_dir.parent.parent
        self.store = self._create_store()

        self.state = self.store.load_state()
        logger.info(
            "双面日记 v%s 已加载：每天 %s（%s），公开日记 -> %s，私密日记 -> %s",
            PLUGIN_VERSION,
            self._cfg_str("diary_time", DEFAULT_DIARY_TIME),
            self._cfg_str("timezone", "Asia/Shanghai"),
            self.store.paths.public_dir,
            self.store.paths.private_dir,
        )

    # ------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        """启动后台调度任务。"""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._scheduler_loop(), name="twin-diary")

    async def terminate(self) -> None:
        """停止后台任务并保存状态。"""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        try:
            self.store.save_state(self.state)
        except OSError:
            logger.warning("双面日记：退出时保存状态失败", exc_info=True)

    # --------------------------------------------------------------- schedule

    async def _scheduler_loop(self) -> None:
        """每 20 秒检查一次是否到点、是否有待重发的日记。"""
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("双面日记：调度循环出现异常")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        """处理一次调度检查。"""
        if not self._cfg_bool("enabled", True):
            return
        now = self._now()
        await self._retry_pending(now)

        # 失败后冷却 15 分钟，避免每个调度周期都重复调用模型。
        if self._cooldown_until and now.timestamp() < self._cooldown_until:
            return
        if self._run_lock.locked():
            return
        umo = self._target_umo()
        if not self._valid_umo(umo) or not self._is_private_umo(umo):
            return

        at = self._diary_time()
        last_done = str(self.state.get("last_public_date") or "")
        catch_up = self._cfg_float("catch_up_hours", 3.0)
        if not should_run_today(
            now,
            at,
            catch_up_hours=catch_up,
            last_done_date=last_done,
        ):
            self._jitter_until = 0.0
            return

        jitter = max(0, self._cfg_int("jitter_seconds", 45))
        # 不补写时没有宽限窗口，jitter 会吃掉唯一的触发机会，直接执行。
        if catch_up <= 0:
            jitter = 0
        if jitter > 0:
            if self._jitter_until <= 0:
                self._jitter_until = now.timestamp() + random.uniform(0, jitter)
                return
            if now.timestamp() < self._jitter_until:
                return
        self._jitter_until = 0.0
        try:
            await self._run_daily(trigger="schedule")
        except Exception:
            # 具体错误已由 _run_daily 记录，这里只避免循环退出
            logger.debug("双面日记：本次自动任务未完成", exc_info=True)

    async def _retry_pending(self, now: datetime) -> None:
        """重发上次没有发送成功的公开日记。

        Args:
            now: 当前时间。
        """
        if self._run_lock.locked():
            return
        async with self._run_lock:
            await self._retry_pending_locked(now)

    async def _retry_pending_locked(self, now: datetime) -> None:
        """在持有运行锁的情况下补发公开日记。

        Args:
            now: 当前时间。
        """
        pending = self.state.get("pending_public")
        if not isinstance(pending, dict):
            return
        try:
            since = datetime.fromisoformat(str(pending.get("since") or ""))
            if since.tzinfo is None:
                since = since.replace(tzinfo=now.tzinfo)
            if (now - since).total_seconds() < 60:
                return
        except ValueError:
            pass

        text = str(pending.get("text") or "")
        day_text = str(pending.get("date") or "")
        umo = self._target_umo() or str(pending.get("umo") or "")
        if (
            not text
            or not day_text
            or not self._valid_umo(umo)
            or not self._is_private_umo(umo)
        ):
            return
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            self.state["pending_public"] = None
            self.store.save_state(self.state)
            return

        pending.setdefault("split_max_chars", self._cfg_int("split_max_chars", 260))
        pending.setdefault("split_public", self._cfg_bool("split_public", True))

        async def on_progress(sent: int) -> None:
            pending["sent"] = sent
            pending["since"] = self._now().isoformat()
            self.state["pending_public"] = pending
            self.store.save_state(self.state)

        remaining = await self._deliver_public(
            text,
            day,
            umo,
            pending,
            on_progress=on_progress,
        )
        if remaining is None:
            self.state["pending_public"] = None
            self.state["last_public_date"] = day.isoformat()
            logger.info("双面日记：补发 %s 的公开日记成功", day_text)
        else:
            remaining["since"] = now.isoformat()
            self.state["pending_public"] = remaining
            logger.warning("双面日记：补发 %s 的公开日记仍然失败", day_text)
        self.store.save_state(self.state)

    # ----------------------------------------------------------------- daily

    async def _run_daily(
        self,
        *,
        trigger: str,
        force: bool = False,
        umo_override: str = "",
    ) -> bool:
        """生成并保存/发送两篇日记。

        Args:
            trigger: 触发来源，``schedule`` 或 ``manual``。
            force: 忽略“今天已写过”的标记，重新生成。
            umo_override: 手动触发时使用的会话。

        Returns:
            实际执行了写作时返回 True，因今天已完成而跳过时返回 False。

        Raises:
            RuntimeError: 未绑定会话或日记生成失败。
        """
        try:
            result = await self._run_daily_inner(
                trigger=trigger,
                force=force,
                umo_override=umo_override,
            )
            self._cooldown_until = 0.0
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._cooldown_until = self._now().timestamp() + 900
            await self._record_error(f"{type(exc).__name__}: {exc}")
            raise

    async def _run_daily_inner(
        self,
        *,
        trigger: str,
        force: bool,
        umo_override: str,
    ) -> bool:
        """实际执行写作流程，异常由外层统一记录。"""
        async with self._run_lock:
            now = self._now()
            day = now.date()
            today = day.isoformat()

            if not force and str(self.state.get("last_public_date") or "") == today:
                return False

            umo = (umo_override or self._target_umo()).strip()
            if not self._valid_umo(umo) or not self._is_private_umo(umo):
                raise RuntimeError(
                    "接收公开日记的会话必须是私聊；请先在私聊里发送「/日记 绑定」",
                )

            persona, persona_name = await self._load_persona(umo)
            self._persona_name_cache = persona_name
            provider_id = await self._provider_id(umo)
            collector = MaterialCollector(
                self.context,
                memory_plugin_name=self._cfg_str(
                    "memory_plugin_name",
                    "astrbot_plugin_memory_companion",
                ),
                logger=logger,
            )

            chat_material = ""
            if self._cfg_bool("chat_history_enable", True):
                chat_material = await collector.collect_chat(
                    umo,
                    limit=self._cfg_int("chat_history_limit", 40),
                    max_chars=self._cfg_int("chat_history_max_chars", 2500),
                    user_label="对方",
                )

            past_public = self._format_past(
                self.store.recent_diaries(
                    "public",
                    days=self._cfg_int("past_days", 3),
                    max_chars=self._cfg_int("past_entry_max_chars", 220),
                    before=day,
                ),
            )
            past_private = self._format_past(
                self.store.recent_diaries(
                    "private",
                    days=self._cfg_int("past_days", 3),
                    max_chars=self._cfg_int("past_entry_max_chars", 220),
                    before=day,
                ),
            )

            logger.info("双面日记：开始写 %s 的公开日记", today)
            public_text = await self._write_public(
                day=day,
                now=now,
                umo=umo,
                provider_id=provider_id,
                collector=collector,
                chat_material=chat_material,
                past_public=past_public,
                persona=persona,
                persona_name=persona_name,
                force=force,
            )

            split_max_chars = self._cfg_int("split_max_chars", 260)
            split_public = self._cfg_bool("split_public", True)
            pending = None if force else self.state.get("pending_public")
            try:
                same_pending = (
                    isinstance(pending, dict)
                    and str(pending.get("date") or "") == today
                    and str(pending.get("text") or "") == public_text
                    and int(pending.get("split_max_chars") or 0) == split_max_chars
                    and bool(pending.get("split_public")) == split_public
                )
            except (TypeError, ValueError):
                same_pending = False
            if not same_pending:
                pending = {
                    "date": today,
                    "text": public_text,
                    "sent": 0,
                    "umo": umo,
                    "split_max_chars": split_max_chars,
                    "split_public": split_public,
                    "since": now.isoformat(),
                }
            self.state["pending_public"] = pending
            self.store.save_state(self.state)

            async def on_progress(sent: int) -> None:
                pending["sent"] = sent
                pending["since"] = self._now().isoformat()
                self.state["pending_public"] = pending
                self.store.save_state(self.state)

            remaining = await self._deliver_public(
                public_text,
                day,
                umo,
                pending,
                on_progress=on_progress,
            )
            delivery_failed = remaining is not None
            if delivery_failed:
                remaining["since"] = now.isoformat()
                self.state["pending_public"] = remaining
                self.store.save_state(self.state)
                logger.warning("双面日记：公开日记发送失败，正文已存档，等待补发")
            else:
                self.state["pending_public"] = None
                self.state["last_public_date"] = today
                self.store.save_state(self.state)

            private_chars = 0
            private_failed = False
            if self._cfg_bool("private_diary_enabled", True):
                logger.info("双面日记：开始写 %s 的私密日记", today)
                private_error: Exception | None = None
                for attempt in range(2):
                    try:
                        private_text = None if force else self.store.read_diary("private", day)
                        if not private_text:
                            private_text = await self._write_private(
                                day=day,
                                now=now,
                                umo=umo,
                                provider_id=provider_id,
                                collector=collector,
                                chat_material=chat_material,
                                past_public=past_public,
                                past_private=past_private,
                                public_draft=public_text,
                                persona=persona,
                                persona_name=persona_name,
                            )
                            path = self.store.save_diary("private", day, private_text)
                            logger.info("双面日记：私密日记已保存到 %s", path)
                        else:
                            logger.info("双面日记：%s 的私密日记已存在，跳过重写", today)
                        private_chars = len(private_text)
                        self.state["last_private_date"] = today
                        self.store.save_state(self.state)
                        private_error = None
                        break
                    except Exception as exc:
                        private_error = exc
                        if attempt == 0:
                            logger.warning("双面日记：私密日记失败，5 秒后重试一次")
                            await asyncio.sleep(5)
                if private_error is not None:
                    # 公开日记已经发出，不能因为私密那份失败就让整次任务报死。
                    private_failed = True
                    self.state["last_error"] = (
                        f"{now:%Y-%m-%d %H:%M} 私密日记生成失败："
                        f"{type(private_error).__name__}: {private_error}"
                    )
                    self.store.save_state(self.state)
                    logger.error("双面日记：私密日记生成失败：%s", private_error)
            self._last_private_failed = private_failed

            if delivery_failed:
                raise RuntimeError("公开日记发送失败，正文已存档，稍后会自动重试")

            record_run(
                self.state,
                {
                    "date": today,
                    "trigger": trigger,
                    "at": now.isoformat(),
                    "provider": provider_id,
                    "persona": persona_name,
                    "public_chars": len(public_text),
                    "private_chars": private_chars,
                    "status": "partial" if private_failed else "ok",
                },
            )
            if not private_failed:
                self.state["last_error"] = ""
            self.store.save_state(self.state)
            return True

    async def _write_public(
        self,
        *,
        day: date,
        now: datetime,
        umo: str,
        provider_id: str,
        collector: MaterialCollector,
        chat_material: str,
        past_public: str,
        persona: str,
        persona_name: str,
        force: bool,
    ) -> str:
        """取得（或生成）今天的公开日记。

        Args:
            day: 日期。
            now: 当前时间。
            umo: 绑定的会话。
            provider_id: 模型 ID。
            collector: 素材收集器。
            chat_material: 聊天素材。
            past_public: 旧公开日记节选。
            persona: 人设文本。
            persona_name: 人设名。
            force: 是否强制重新生成。

        Returns:
            公开日记正文。
        """
        if not force:
            existing = self.store.read_diary("public", day)
            if existing and str(self.state.get("last_public_date") or "") != day.isoformat():
                logger.info("双面日记：发现 %s 尚未发送的公开日记，直接补发", day)
                return existing

        memory_material = ""
        if self._cfg_bool("memory_enable", True):
            memory_material = await collector.collect_long_term(
                query="今天发生的事，我和主人的相处，主人的近况、情绪和我们在意的事",
                umo=umo,
                top_k=self._cfg_int("memory_top_k", 6),
                max_chars=self._cfg_int("memory_max_chars", 1500),
                timeout=self._cfg_float("memory_timeout", 20.0),
                platform=str(
                    self.state.get("bound_platform") or umo.split(":", 1)[0],
                ),
                user_id=self._master_id(umo),
                user_name=self._master_name(),
                bot_id=str(self.state.get("bound_bot_id") or ""),
                persona_id=persona_name,
            )

        min_chars, max_chars = self._lengths(public=True)
        prompt_input = self._prompt_input(
            now=now,
            persona=persona,
            persona_name=persona_name,
            chat_material=chat_material,
            memory_material=memory_material,
            past_public=past_public,
            past_private="",
            min_chars=min_chars,
            max_chars=max_chars,
        )
        system_prompt, user_prompt = build_public_prompt(prompt_input)
        text = await self._generate_text(
            provider_id=provider_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            min_chars=min_chars,
            label="公开日记",
        )
        text = self._ensure_date_header(text, now)
        self.store.save_diary("public", day, text)
        return text

    async def _write_private(
        self,
        *,
        day: date,
        now: datetime,
        umo: str,
        provider_id: str,
        collector: MaterialCollector,
        chat_material: str,
        past_public: str,
        past_private: str,
        public_draft: str,
        persona: str,
        persona_name: str,
    ) -> str:
        """生成今天的私密日记。

        Args:
            day: 日期。
            now: 当前时间。
            umo: 绑定的会话。
            provider_id: 模型 ID。
            collector: 素材收集器。
            chat_material: 聊天素材。
            past_public: 旧公开日记节选。
            past_private: 旧私密日记节选。
            public_draft: 今晚已写的公开日记。
            persona: 人设文本。
            persona_name: 人设名。

        Returns:
            私密日记正文。
        """
        memory_material = ""
        if self._cfg_bool("memory_enable", True):
            memory_material = await collector.collect_long_term(
                query="我对主人的真实想法，没说出口的话，最近的心里事和害怕的事",
                umo=umo,
                top_k=self._cfg_int("memory_top_k", 6),
                max_chars=self._cfg_int("memory_max_chars", 1500),
                timeout=self._cfg_float("memory_timeout", 20.0),
                platform=str(
                    self.state.get("bound_platform") or umo.split(":", 1)[0],
                ),
                user_id=self._master_id(umo),
                user_name=self._master_name(),
                bot_id=str(self.state.get("bound_bot_id") or ""),
                persona_id=persona_name,
            )

        min_chars, max_chars = self._lengths(public=False)
        prompt_input = self._prompt_input(
            now=now,
            persona=persona,
            persona_name=persona_name,
            chat_material=chat_material,
            memory_material=memory_material,
            past_public=past_public,
            past_private=past_private,
            min_chars=min_chars,
            max_chars=max_chars,
            public_draft=public_draft,
        )
        system_prompt, user_prompt = build_private_prompt(prompt_input)
        text = await self._generate_text(
            provider_id=provider_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            min_chars=min_chars,
            label="私密日记",
        )
        return self._ensure_date_header(text, now)

    def _prompt_input(
        self,
        *,
        now: datetime,
        persona: str,
        persona_name: str,
        chat_material: str,
        memory_material: str,
        past_public: str,
        past_private: str,
        min_chars: int,
        max_chars: int,
        public_draft: str = "",
    ) -> DiaryPromptInput:
        """组装提示词输入对象。

        Args:
            now: 当前时间。
            persona: 人设文本。
            persona_name: 人设名。
            chat_material: 聊天素材。
            memory_material: 长期记忆。
            past_public: 旧公开日记。
            past_private: 旧私密日记。
            min_chars: 最少字数。
            max_chars: 最多字数。
            public_draft: 公开日记草稿（仅私密日记使用）。

        Returns:
            提示词输入。
        """
        bot_name = self._cfg_str("bot_name", "").strip() or persona_name or "我"
        if bot_name.strip().lower() in {"default", "system_default"}:
            bot_name = "我"
        persona_text = persona
        if not _contains_cjk(persona_text) and (
            not persona_name or persona_name.strip().lower() in {"default", "system_default"}
        ):
            persona_text = _FALLBACK_PERSONA_CN
        return DiaryPromptInput(
            moment=now,
            persona=persona_text,
            persona_name=persona_name,
            bot_name=bot_name,
            master_name=self._master_name(),
            chat_material=chat_material,
            memory_material=memory_material,
            past_public=past_public,
            past_private=past_private,
            style_extra=self._cfg_str("style_extra", ""),
            min_chars=min_chars,
            max_chars=max_chars,
            include_date_line=self._cfg_bool("include_date_line", True),
            public_draft=public_draft,
        )

    # ------------------------------------------------------------------- LLM

    async def _generate_text(
        self,
        *,
        provider_id: str,
        system_prompt: str,
        user_prompt: str,
        min_chars: int,
        label: str,
    ) -> str:
        """调用模型生成一篇日记，短得离谱时重试一次。

        Args:
            provider_id: 模型 ID。
            system_prompt: 系统提示词。
            user_prompt: 用户提示词。
            min_chars: 期望的最少字数。
            label: 日志里使用的名称。

        Returns:
            清理后的日记正文。

        Raises:
            RuntimeError: 两次都没有得到可用正文。
        """
        last_text = ""
        for attempt in range(2):
            prompt = user_prompt
            if attempt > 0:
                prompt += (
                    f"\n\n（上一版太短或太空了。请重写，至少写 {min_chars} 字，"
                    "并且写出今天具体发生过的细节。）"
                )
            raw = await self._call_llm(provider_id, system_prompt, prompt)
            text = clean_llm_text(raw)
            threshold = max(80, int(min_chars * 0.7))
            if text and len(text) >= threshold and not looks_like_refusal(text):
                return text
            last_text = text
            logger.warning(
                "双面日记：%s 第 %s 次生成不理想（%s 字），准备重试",
                label,
                attempt + 1,
                len(text),
            )
        fallback_min = max(60, int(min_chars * 0.5))
        if last_text and len(last_text) >= fallback_min and not looks_like_refusal(last_text):
            logger.warning(
                "双面日记：%s 低于配置下限但已接受（%s 字，期望至少 %s 字）",
                label,
                len(last_text),
                min_chars,
            )
            return last_text
        raise RuntimeError(f"{label}生成失败：模型没有返回可用的正文")

    async def _call_llm(self, provider_id: str, system_prompt: str, prompt: str) -> str:
        """调用 ``context.llm_generate``，兼容不接受写作参数的 Provider。

        Args:
            provider_id: 模型 ID。
            system_prompt: 系统提示词。
            prompt: 用户提示词。

        Returns:
            模型返回的纯文本。
        """
        kwargs: dict[str, Any] = {
            "chat_provider_id": provider_id,
            "prompt": prompt,
            "system_prompt": system_prompt,
            "temperature": self._cfg_float("temperature", 0.9),
            "max_tokens": self._cfg_int("max_tokens", 1600),
        }
        try:
            response = await self.context.llm_generate(**kwargs)
        except TypeError:
            kwargs.pop("temperature", None)
            kwargs.pop("max_tokens", None)
            response = await self.context.llm_generate(**kwargs)
        except Exception as exc:
            message = str(exc).lower()
            if any(token in message for token in ("max_tokens", "temperature", "unsupported")):
                kwargs.pop("temperature", None)
                kwargs.pop("max_tokens", None)
                response = await self.context.llm_generate(**kwargs)
            else:
                raise
        return str(getattr(response, "completion_text", "") or "")

    # -------------------------------------------------------------- delivery

    async def _deliver_public(
        self,
        text: str,
        day: date,
        umo: str,
        pending: dict | None,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
    ) -> dict | None:
        """分段发送公开日记，并在每段成功后回调保存进度。

        Args:
            text: 日记正文。
            day: 日期。
            umo: 目标会话。
            pending: 上次未完成的发送进度。
            on_progress: 每成功发送一段后调用的异步回调，参数为已发送段数。

        Returns:
            全部发送成功返回 None；否则返回新的进度字典。
        """
        max_chars = self._cfg_int("split_max_chars", 260)
        split_enabled = self._cfg_bool("split_public", True)
        chunks = split_for_chat(text, max_chars) if split_enabled else [text]
        if not chunks:
            return None

        start = 0
        if isinstance(pending, dict) and str(pending.get("date") or "") == day.isoformat():
            try:
                same_split = (
                    int(pending.get("split_max_chars") or 0) == max_chars
                    and bool(pending.get("split_public")) == split_enabled
                )
            except (TypeError, ValueError):
                same_split = False
            if same_split:
                try:
                    start = int(pending.get("sent") or 0)
                except (TypeError, ValueError):
                    start = 0
            else:
                logger.warning("双面日记：分段参数已变化，本次从第一段重新发送")
        start = max(0, min(start, len(chunks)))

        interval_min = self._cfg_float("send_interval_min", 1.5)
        interval_max = self._cfg_float("send_interval_max", 3.5)
        if interval_max < interval_min:
            interval_min, interval_max = interval_max, interval_min

        retries = max(0, self._cfg_int("send_retries", 2))
        for index in range(start, len(chunks)):
            ok = await self._send(umo, chunks[index], attempts=retries)
            if not ok:
                progress = dict(pending) if isinstance(pending, dict) else {}
                progress.update(
                    {
                        "date": day.isoformat(),
                        "text": text,
                        "sent": index,
                        "umo": umo,
                        "split_max_chars": max_chars,
                        "split_public": split_enabled,
                    },
                )
                return progress
            if on_progress is not None:
                await on_progress(index + 1)
            if index < len(chunks) - 1:
                await asyncio.sleep(random.uniform(interval_min, interval_max))
        return None

    async def _send(self, umo: str, text: str, *, attempts: int = 0) -> bool:
        """发送一条纯文本消息。

        Args:
            umo: 目标会话。
            text: 文本内容。
            attempts: 额外重试次数。

        Returns:
            发送成功时返回 True。
        """
        if not text.strip():
            return True
        for attempt in range(attempts + 1):
            try:
                chain = MessageChain().message(text)
                if await self.context.send_message(umo, chain):
                    return True
                logger.warning("双面日记：发送消息失败（第 %s 次）", attempt + 1)
            except Exception:
                # 异常时无法确认平台是否已经送达，继续重试可能造成重复消息。
                logger.warning(
                    "双面日记：发送消息异常，停止本次发送",
                    exc_info=True,
                )
                return False
            if attempt < attempts:
                await asyncio.sleep(2)
        return False

    # ------------------------------------------------------------------ state

    async def _record_error(self, message: str) -> None:
        """记录最近一次错误，并按配置通知主人。

        Args:
            message: 错误摘要（不会包含日记正文）。
        """
        now = self._now()
        self.state["last_error"] = f"{now:%Y-%m-%d %H:%M} {message}"
        try:
            self.store.save_state(self.state)
        except OSError:
            logger.warning("双面日记：保存错误状态失败", exc_info=True)

        if not self._cfg_bool("notify_errors", False):
            return
        umo = self._target_umo()
        if not self._valid_umo(umo) or not self._is_private_umo(umo):
            return
        notice = f"[双面日记] 今晚的日记没有写成：{truncate(message, 160)}"
        if notice == self._last_error_notice:
            return
        self._last_error_notice = notice
        await self._send(umo, notice, attempts=0)

    # --------------------------------------------------------------- commands

    @filter.command("日记", alias={"diary"})
    async def diary_command(self, event: AstrMessageEvent):
        """双面日记指令：绑定、状态、立即写、查看公开/私密日记。"""
        try:
            raw = (event.message_str or "").strip()
            tokens = raw.split()
            if tokens and tokens[0].lstrip("/!！／").lower() in {"日记", "diary"}:
                tokens = tokens[1:]
            action = tokens[0].lower() if tokens else ""
            arg = tokens[1] if len(tokens) > 1 else ""

            if action in {"绑定", "bind"}:
                async for result in self._cmd_bind(event):
                    yield result
                return
            if action in {"状态", "status", "info"}:
                if not self._require_admin(event):
                    yield event.plain_result("这个指令只对管理员开放。")
                    return
                if not self._is_private_event(event):
                    yield event.plain_result("请在私聊里查看状态；状态会显示绑定账号和本机目录。")
                    return
                yield event.plain_result(self._status_text())
                return
            if action in {"写", "生成", "write", "now"}:
                if not self._require_admin(event):
                    yield event.plain_result("这个操作只对管理员开放。")
                    return
                if not self._is_private_event(event):
                    yield event.plain_result("请在私聊里使用「/日记 写」，避免日记或目录发到群里。")
                    return
                yield event.plain_result("好，我去写今天的日记，写完告诉你。")
                umo_override = "" if self._target_umo() else event.unified_msg_origin
                try:
                    await self._run_daily(
                        trigger="manual",
                        force=True,
                        umo_override=umo_override,
                    )
                except Exception as exc:
                    yield event.plain_result(f"没写成：{exc}")
                    return
                if self._last_private_failed:
                    yield event.plain_result(
                        "公开日记已经发出去了，但私密日记没写成；"
                        "具体原因看「/日记 状态」里的上次错误。",
                    )
                elif self._cfg_bool("private_diary_enabled", True):
                    yield event.plain_result(
                        "写完了。公开日记已经发过去了；"
                        f"私密日记保存在：{self.store.paths.private_dir}",
                    )
                else:
                    yield event.plain_result("写完了，公开日记已经发过去了。")
                return
            if action in {"私密", "private"}:
                async for result in self._cmd_view(event, "private", arg):
                    yield result
                return
            if action in {"公开", "public"}:
                async for result in self._cmd_view(event, "public", arg):
                    yield result
                return
            yield event.plain_result(self._help_text())
        except Exception as exc:
            logger.exception("双面日记：指令处理失败")
            yield event.plain_result(f"出错了：{exc}")

    async def _cmd_bind(self, event: AstrMessageEvent):
        """处理绑定指令。"""
        message_type = str(getattr(event.get_message_type(), "value", ""))
        if message_type != "FriendMessage":
            yield event.plain_result("请在私聊里发送「/日记 绑定」，我不想把日记发到群里。")
            return
        if not self._require_admin(event) and not self._cfg_bool(
            "allow_any_private_bind",
            False,
        ):
            yield event.plain_result("只有 AstrBot 管理员可以绑定日记接收会话。")
            return

        umo = event.unified_msg_origin
        if not self._valid_umo(umo):
            yield event.plain_result("这个会话的标识有点问题，绑定失败。")
            return

        self.state["bound_umo"] = umo
        self.state["bound_master_id"] = str(event.get_sender_id() or "")
        self.state["bound_master_name"] = str(event.get_sender_name() or "")
        self.state["bound_bot_id"] = str(event.get_self_id() or "")
        self.state["bound_platform"] = str(event.get_platform_name() or "")
        self.state["bound_at"] = self._now().isoformat()
        self.store.save_state(self.state)

        configured = self._cfg_str("target_session", "").strip()
        extra = (
            "\n注意：插件配置里手动填了 target_session，它会覆盖这里的绑定。"
            if configured
            else ""
        )
        yield event.plain_result(
            "绑定好了。以后每天 "
            f"{self._cfg_str('diary_time', DEFAULT_DIARY_TIME)} 我会把公开日记发到这里；"
            f"私密日记只写到：{self.store.paths.private_dir}{extra}",
        )

    async def _cmd_view(self, event: AstrMessageEvent, kind: str, arg: str):
        """处理查看公开/私密日记的指令。"""
        if not self._require_admin(event):
            yield event.plain_result("这个操作只对管理员开放。")
            return
        if not self._is_private_event(event):
            yield event.plain_result("请在私聊里使用查看指令。")
            return
        if kind == "private" and not self._cfg_bool("allow_private_view", False):
            yield event.plain_result("私密日记的查看指令已关闭，请直接去文件夹里看。")
            return
        try:
            day = parse_date_arg(arg, today=self._now().date())
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        text = self.store.read_diary(kind, day)
        if not text:
            yield event.plain_result(f"{day.isoformat()} 没有找到{'私密' if kind == 'private' else '公开'}日记。")
            return

        if kind == "private":
            target = self._target_umo() or event.unified_msg_origin
            if not self._is_private_umo(target):
                yield event.plain_result("绑定会话不是私聊，已拒绝发送私密日记。")
                return
            header = f"{format_day_header(day)} · 私密日记（本不该出现在这里的）\n\n"
            if await self._send(target, header + text, attempts=1):
                yield event.plain_result("私密日记已经单独发给你了。")
            else:
                yield event.plain_result("发送失败了，请直接去文件里看。")
            return

        header = f"{format_day_header(day)} · 公开日记\n\n"
        yield event.plain_result(header + text)

    # ----------------------------------------------------------------- helpers

    def _create_store(self) -> DiaryStore:
        """根据配置创建存储对象，配置路径不可用时回退到插件数据目录。

        Returns:
            存储对象。
        """
        allow_external = self._cfg_bool("allow_external_dirs", False)
        try:
            paths = build_paths(
                data_dir=self.data_dir,
                plugin_data_dir=self.plugin_data_dir,
                public_setting=self._cfg_str("public_dir", ""),
                private_setting=self._cfg_str("private_dir", ""),
                allow_external=allow_external,
            )
            if paths.blocked:
                logger.warning(
                    "双面日记：%s 指向插件数据目录之外，且未开启 allow_external_dirs，"
                    "已回退到插件数据目录",
                    "、".join(paths.blocked),
                )
            if paths.public_dir == paths.private_dir:
                logger.error("双面日记：公开与私密目录相同，私密日记改用独立目录")
                paths = build_paths(
                    data_dir=self.data_dir,
                    plugin_data_dir=self.plugin_data_dir,
                    public_setting=self._cfg_str("public_dir", ""),
                    private_setting=str(self.plugin_data_dir / "private"),
                    allow_external=allow_external,
                )
            return DiaryStore(paths)
        except (OSError, ValueError):
            logger.error("双面日记：配置的目录不可用，回退到插件数据目录", exc_info=True)
            paths = build_paths(
                data_dir=self.data_dir,
                plugin_data_dir=self.plugin_data_dir,
                public_setting="",
                private_setting="",
            )
            return DiaryStore(paths)

    async def _load_persona(self, umo: str) -> tuple[str, str]:
        """读取当前会话使用的人格。

        Args:
            umo: 会话标识。

        Returns:
            ``(人格提示词, 人格名)``；读取失败时返回空字符串。
        """
        mode = self._cfg_str("persona_mode", "session").strip().lower()
        if mode == "custom":
            return self._cfg_str("custom_persona", ""), "自定义人设"

        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return "", ""

        if mode == "fixed":
            persona_id = self._cfg_str("persona_id", "").strip()
            if persona_id:
                try:
                    persona = manager.get_persona_v3_by_id(persona_id)
                    text, name = _persona_text(persona)
                    if text:
                        return text, name or persona_id
                except Exception:
                    logger.warning("双面日记：读取固定人格失败", exc_info=True)

        platform_name = umo.split(":", 1)[0]
        try:
            conversation_persona_id = await self._conversation_persona_id(umo)
            _, persona, _, _ = await manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conversation_persona_id,
                platform_name=platform_name,
                provider_settings=self.context.get_config(umo).get(
                    "provider_settings",
                )
                or {},
            )
            text, name = _persona_text(persona)
            if text:
                return text, name
        except Exception:
            logger.debug("双面日记：按会话解析人格失败，尝试默认人格", exc_info=True)

        try:
            persona = await manager.get_default_persona_v3(umo)
            return _persona_text(persona)
        except Exception:
            logger.warning("双面日记：读取默认人格失败", exc_info=True)
            return "", ""

    async def _conversation_persona_id(self, umo: str) -> str | None:
        """读取会话绑定的独立人格 ID。

        Args:
            umo: 会话标识。

        Returns:
            人格 ID 或 None。
        """
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return None
        conversation_id = await manager.get_curr_conversation_id(umo)
        if not conversation_id:
            return None
        conversation = await manager.get_conversation(umo, conversation_id)
        return getattr(conversation, "persona_id", None) if conversation else None

    async def _provider_id(self, umo: str) -> str:
        """获取写日记使用的模型 ID。

        Args:
            umo: 会话标识。

        Returns:
            模型 ID。

        Raises:
            RuntimeError: 没有可用的聊天模型。
        """
        configured = self._cfg_str("provider_id", "").strip()
        if configured:
            return configured
        try:
            return await self.context.get_current_chat_provider_id(umo=umo)
        except Exception as exc:
            raise RuntimeError(
                "没有找到可用的聊天模型，请先在 WebUI 配置模型，"
                "或在插件配置里单独指定写日记的模型",
            ) from exc

    def _ensure_date_header(self, text: str, now: datetime) -> str:
        """确保日记第一行是日期抬头；模型漏写时补上。

        Args:
            text: 模型生成的日记正文。
            now: 当前时间。

        Returns:
            带日期抬头的正文。
        """
        body = str(text or "").strip()
        if not body or not self._cfg_bool("include_date_line", True):
            return body
        first_line = body.splitlines()[0].strip()
        if re.match(r"^\d{1,2}月\d{1,2}日", first_line) or re.match(
            r"^\d{4}\s*年",
            first_line,
        ):
            return body
        return f"{format_day_header(now)}\n\n{body}"

    def _format_past(self, items: list[tuple[str, str]]) -> str:
        """把旧日记节选拼成提示词里的一段。

        Args:
            items: ``(日期, 节选)`` 列表。

        Returns:
            拼接后的文本。
        """
        if not items:
            return ""
        lines: list[str] = []
        for day_text, text in items:
            compact = re.sub(r"\s*\n\s*", " ", text).strip()
            lines.append(f"{day_text}：{truncate(compact, self._cfg_int('past_entry_max_chars', 220))}")
        return "\n".join(lines)

    def _lengths(self, *, public: bool) -> tuple[int, int]:
        """读取并校正字数配置。

        Args:
            public: True 表示公开日记。

        Returns:
            ``(最少字数, 最多字数)``。
        """
        if public:
            low = self._cfg_int("public_min_chars", 350)
            high = self._cfg_int("public_max_chars", 700)
        else:
            low = self._cfg_int("private_min_chars", 300)
            high = self._cfg_int("private_max_chars", 800)
        low = max(80, low)
        high = max(low + 50, high)
        return low, high

    def _diary_time(self):
        """读取每天的写日记时间，配置错误时回退到 22:30。"""
        raw = self._cfg_str("diary_time", DEFAULT_DIARY_TIME)
        try:
            return parse_hhmm(raw)
        except ValueError:
            logger.warning("双面日记：diary_time 配置无效（%r），改用 22:30", raw)
            return parse_hhmm(DEFAULT_DIARY_TIME)

    def _tzinfo(self):
        """读取时区，配置错误时回退到系统本地时区。"""
        name = self._cfg_str("timezone", "Asia/Shanghai").strip() or "Asia/Shanghai"
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("双面日记：时区 %r 无效，改用系统本地时区", name)
            return datetime.now().astimezone().tzinfo

    def _now(self) -> datetime:
        """返回当前时区的时间。"""
        return datetime.now(self._tzinfo())

    def _target_umo(self) -> str:
        """返回公开日记的目标会话。"""
        configured = self._cfg_str("target_session", "").strip()
        if configured:
            return configured
        return str(self.state.get("bound_umo") or "").strip()

    def _master_name(self) -> str:
        """返回对主人的称呼。"""
        configured = self._cfg_str("master_name", "").strip()
        if configured:
            return configured
        return str(self.state.get("bound_master_name") or "").strip() or "主人"

    def _master_id(self, umo: str) -> str:
        """返回主人 QQ，优先使用绑定信息，其次从会话标识里取。"""
        bound = str(self.state.get("bound_master_id") or "").strip()
        if bound:
            return bound
        parts = umo.split(":", 2)
        return parts[2] if len(parts) == 3 else ""

    def _valid_umo(self, umo: str) -> bool:
        """检查会话标识是否形如 ``platform:type:id``。"""
        parts = str(umo or "").split(":", 2)
        return len(parts) == 3 and all(part.strip() for part in parts)

    def _is_private_umo(self, umo: str) -> bool:
        """检查会话标识是否是 QQ 私聊。

        Args:
            umo: 会话标识。

        Returns:
            形如 ``platform:FriendMessage:id`` 时返回 True。
        """
        parts = str(umo or "").split(":", 2)
        return len(parts) == 3 and parts[1] == "FriendMessage" and all(
            part.strip() for part in parts
        )

    def _is_private_event(self, event: AstrMessageEvent) -> bool:
        """检查当前事件是否来自私聊。

        Args:
            event: 消息事件。

        Returns:
            私聊事件返回 True。
        """
        return str(getattr(event.get_message_type(), "value", "")) == "FriendMessage"

    def _require_admin(self, event: AstrMessageEvent) -> bool:
        """检查当前用户是否有管理权限。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            admins = self.context.get_config().get("admins_id", []) or []
            return str(event.get_sender_id()) in {str(item) for item in admins}
        except Exception:
            return False

    def _status_text(self) -> str:
        """生成 ``/日记 状态`` 的回复。"""
        now = self._now()
        at = self._diary_time()
        next_run = next_run_at(now, at)
        today = now.date().isoformat()
        if next_run.date() == now.date() and str(self.state.get("last_public_date")) == today:
            next_run = next_run_at(next_run, at)

        bound = self._target_umo()
        bound_text = bound or "未绑定"
        if bound:
            parts = bound.split(":", 2)
            if len(parts) == 3:
                bound_text = f"{parts[0]} 私聊 {parts[2]}"
            if not self._is_private_umo(bound):
                bound_text += "（不是私聊，自动任务已拒绝发送）"
        public_done = "✅" if str(self.state.get("last_public_date")) == today else "—"
        private_done = "✅" if str(self.state.get("last_private_date")) == today else "—"
        memory = (
            "已检测到 bridge（返回空时自动跳过）"
            if self._memory_available()
            else "未检测到"
        )
        last_error = str(self.state.get("last_error") or "").strip() or "无"
        return (
            "【双面日记】\n"
            f"状态：{'开启' if self._cfg_bool('enabled', True) else '关闭'}\n"
            f"绑定会话：{bound_text}\n"
            f"主人称呼：{self._master_name()}\n"
            f"每天时间：{at:%H:%M}（{self._cfg_str('timezone', 'Asia/Shanghai')}）\n"
            f"下次自动写：{next_run:%Y-%m-%d %H:%M}\n"
            f"今天：公开 {public_done} / 私密 {private_done}\n"
            f"人格：{self._persona_name_cache or '运行时读取'}\n"
            f"记忆插件：{memory}\n"
            f"公开存档：{self.store.paths.public_dir}\n"
            f"私密目录：{self.store.paths.private_dir}\n"
            f"外部目录：{'允许' if self._cfg_bool('allow_external_dirs', False) else '关闭（仅插件数据目录）'}\n"
            f"上次错误：{truncate(last_error, 200)}"
        )

    def _memory_available(self) -> bool:
        """检查记忆插件是否可用。"""
        try:
            collector = MaterialCollector(
                self.context,
                memory_plugin_name=self._cfg_str(
                    "memory_plugin_name",
                    "astrbot_plugin_memory_companion",
                ),
                logger=logger,
            )
            return collector.memory_plugin_available()
        except Exception:
            return False

    def _help_text(self) -> str:
        """生成帮助文本。"""
        return (
            "【双面日记】\n"
            "/日记 绑定 —— 在私聊里绑定公开日记的接收会话\n"
            "/日记 状态 —— 查看绑定、下次时间、目录和最近错误（管理员）\n"
            "/日记 写 —— 立刻写今天的两篇（管理员）\n"
            "/日记 公开 [日期] —— 查看某天的公开日记（管理员）\n"
            "/日记 私密 [日期] —— 查看某天的私密日记（管理员）\n"
            "日期格式：2026-10-26、10-26，或留空表示今天。"
        )

    # ------------------------------------------------------------------ config

    def _cfg(self, key: str, default: Any) -> Any:
        """读取配置项，任何异常都回退到默认值。"""
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_str(self, key: str, default: str) -> str:
        """读取字符串配置。"""
        return str(self._cfg(key, default) or "")

    def _cfg_bool(self, key: str, default: bool) -> bool:
        """读取布尔配置，兼容字符串写法。"""
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
        return bool(value)

    def _cfg_int(self, key: str, default: int) -> int:
        """读取整数配置。"""
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        """读取浮点配置。"""
        try:
            return float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default


def _contains_cjk(value: str) -> bool:
    """判断文本里是否包含中日韩统一表意文字。

    Args:
        value: 待检查文本。

    Returns:
        至少包含一个汉字时返回 True。
    """
    return bool(re.search(r"[\u4e00-\u9fff]", str(value or "")))


def _persona_text(persona: Any) -> tuple[str, str]:
    """从 Persona/Personality 对象里取出提示词和名字。

    Args:
        persona: 人格对象，可能是字典或带属性的对象。

    Returns:
        ``(提示词, 名字)``。
    """
    if persona is None:
        return "", ""
    if isinstance(persona, dict):
        return str(persona.get("prompt") or ""), str(persona.get("name") or "")
    return (
        str(getattr(persona, "prompt", "") or ""),
        str(getattr(persona, "name", "") or ""),
    )
