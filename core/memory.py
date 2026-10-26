"""从 AstrBot 会话与记忆插件收集今天的素材。

只通过 duck typing 调用 AstrBot 的对象，不直接导入框架，便于测试。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .textkit import truncate

_LOGGER = logging.getLogger("astrbot_plugin_twin_diary")


class MaterialCollector:
    """收集聊天记录、长期记忆和历史日记素材。"""

    def __init__(
        self,
        context: Any,
        *,
        memory_plugin_name: str = "astrbot_plugin_memory_companion",
        logger: logging.Logger | None = None,
    ) -> None:
        """初始化收集器。

        Args:
            context: AstrBot 的 ``Context`` 对象。
            memory_plugin_name: 长期记忆插件的目录名。
            logger: 可选的日志对象。
        """
        self.context = context
        self.memory_plugin_name = memory_plugin_name
        self.logger = logger or _LOGGER

    # ----------------------------------------------------------- chat history

    async def collect_chat(
        self,
        umo: str,
        *,
        limit: int,
        max_chars: int,
        user_label: str = "对方",
    ) -> str:
        """读取绑定会话最近的对话，整理成“主人/我”的素材。

        Args:
            umo: 会话的 unified_msg_origin。
            limit: 最多读取多少条消息。
            max_chars: 素材总字数上限。
            user_label: 素材里对用户的临时标签，只用于区分说话人。

        Returns:
            已格式化的聊天节选；没有记录时返回空字符串。
        """
        if limit <= 0 or max_chars <= 0 or not umo:
            return ""
        try:
            manager = self.context.conversation_manager
            conversation_id = await manager.get_curr_conversation_id(umo)
            if not conversation_id:
                return ""
            conversation = await manager.get_conversation(umo, conversation_id)
            if conversation is None:
                return ""
            raw_history = getattr(conversation, "history", "") or "[]"
            history = json.loads(raw_history) if isinstance(raw_history, str) else raw_history
        except Exception:
            self.logger.debug("读取会话历史失败", exc_info=True)
            return ""

        if not isinstance(history, list):
            return ""

        lines: list[str] = []
        for item in reversed(history):
            if len(lines) >= limit:
                break
            line = _render_history_item(item, user_label)
            if line:
                lines.append(line)

        lines.reverse()
        return _fit_budget(lines, max_chars)

    # -------------------------------------------------------------- long term

    async def collect_long_term(
        self,
        *,
        query: str,
        umo: str,
        top_k: int,
        max_chars: int,
        timeout: float,
        platform: str = "aiocqhttp",
        user_id: str = "",
        user_name: str = "",
        bot_id: str = "",
        persona_id: str = "",
    ) -> str:
        """从记忆插件读取与今天相关的长期记忆。

        Args:
            query: 检索用的问题，尽量写成自然语言。
            umo: 当前会话。
            top_k: 召回条数。
            max_chars: 返回文本的字数上限。
            timeout: 检索超时时间（秒）。
            platform: 平台名。
            user_id: 主人 QQ。
            user_name: 主人称呼。
            bot_id: 机器人自身 ID。
            persona_id: 当前人格名。

        Returns:
            记忆插件返回的记忆包；不可用时返回空字符串。
        """
        bridge = self._find_bridge()
        if bridge is None or not query.strip():
            return ""

        session_context = {
            "session_id": umo,
            "scope": "private",
            "platform": platform,
            "user_id": str(user_id or ""),
            "user_name": str(user_name or ""),
            "bot_id": str(bot_id or ""),
            "persona_id": str(persona_id or ""),
            "message_text": query,
        }

        text = await _call_with_timeout(
            bridge,
            "compose_context",
            timeout,
            {
                "query": query,
                "session_context": session_context,
                "top_k": top_k,
                "max_chars": max_chars,
            },
            self.logger,
        )
        if text:
            return truncate(str(text), max_chars)

        # 兼容没有 compose_context 或版本较旧的 bridge
        items = await _call_with_timeout(
            bridge,
            "search",
            timeout,
            {
                "query": query,
                "session_context": session_context,
                "top_k": top_k,
            },
            self.logger,
            default=None,
        )
        if not isinstance(items, list):
            return ""

        lines: list[str] = []
        for item in items[:top_k]:
            if not isinstance(item, dict):
                continue
            content = (
                item.get("canonical_summary")
                or item.get("content")
                or item.get("summary")
                or item.get("evidence_preview")
            )
            content = str(content or "").strip()
            if content:
                lines.append(f"- {truncate(content, 220)}")
        return truncate("\n".join(lines), max_chars)

    def memory_plugin_available(self) -> bool:
        """检查记忆插件 bridge 是否存在。

        Returns:
            能找到可用 bridge 时返回 True。
        """
        return self._find_bridge() is not None

    def _find_bridge(self) -> Any | None:
        """在当前加载的插件里寻找记忆插件的 bridge 对象。

        Returns:
            记忆插件的 ``memory_companion`` 对象；找不到时返回 None。
        """
        try:
            stars = self.context.get_all_stars()
        except Exception:
            return None
        for metadata in stars or []:
            try:
                if getattr(metadata, "name", None) != self.memory_plugin_name:
                    continue
                plugin = getattr(metadata, "star_cls", None)
                bridge = getattr(plugin, "memory_companion", None)
                if bridge is not None:
                    return bridge
            except Exception:
                continue
        return None


def _render_history_item(item: Any, user_label: str) -> str:
    """把一条历史消息渲染成素材行。

    Args:
        item: 会话历史中的一条记录。
        user_label: 对用户的临时标签。

    Returns:
        形如 ``主人：……`` 的文本；应跳过时返回空字符串。
    """
    if not isinstance(item, dict) or item.get("_no_save"):
        return ""
    role = str(item.get("role") or "").strip()
    if role not in {"user", "assistant"}:
        return ""
    text = _content_to_text(item.get("content"))
    if not text:
        return ""
    lines = [line for line in text.splitlines() if not line.strip().startswith("/日记")]
    text = "\n".join(lines).strip()
    if not text:
        return ""
    speaker = user_label if role == "user" else "我"
    return f"{speaker}：{truncate(text, 400)}"


def _content_to_text(content: Any) -> str:
    """从字符串或多模态消息段里提取可读文本。

    Args:
        content: 消息内容。

    Returns:
        提取出的文本。
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            kind = str(part.get("type") or "")
            if kind in {"text", "input_text"}:
                parts.append(str(part.get("text") or ""))
            elif kind in {"image_url", "image", "input_image"}:
                parts.append("[图片]")
            elif kind in {"input_audio", "audio"}:
                parts.append("[语音]")
            elif kind == "file":
                parts.append("[文件]")
    return "".join(parts).strip()


def _fit_budget(lines: list[str], max_chars: int) -> str:
    """从最近的消息往前保留，直到用完字数预算。

    Args:
        lines: 按时间顺序排列的素材行。
        max_chars: 总字数上限。

    Returns:
        拼接后的文本。
    """
    selected: list[str] = []
    total = 0
    for line in reversed(lines):
        if len(line) > max_chars:
            line = truncate(line, max_chars)
        if total + len(line) + 1 > max_chars and selected:
            break
        selected.append(line)
        total += len(line) + 1
    selected.reverse()
    return "\n".join(selected)


async def _call_with_timeout(
    target: Any,
    method_name: str,
    timeout: float,
    kwargs: dict,
    logger: logging.Logger,
    *,
    default: Any = "",
) -> Any:
    """调用记忆插件的方法，兼容不同版本的签名并处理超时。

    Args:
        target: bridge 对象。
        method_name: 方法名。
        timeout: 超时时间（秒）。
        kwargs: 首选参数。
        logger: 日志对象。
        default: 失败时的返回值。

    Returns:
        方法的返回值，失败时返回 ``default``。
    """
    method = getattr(target, method_name, None)
    if not callable(method):
        return default

    attempts = [kwargs]
    # 老版本可能没有 session_context/top_k/max_chars，逐级退让。
    attempts.append({"query": kwargs.get("query", "")})
    attempts.append({})

    for attempt in attempts:
        try:
            result = method(**attempt)
            if asyncio.iscoroutine(result):
                result = await asyncio.wait_for(result, timeout=timeout)
            return result
        except TypeError:
            continue
        except asyncio.TimeoutError:
            logger.warning("记忆插件检索超时（%.1fs），本次跳过长期记忆", timeout)
            return default
        except Exception:
            logger.debug("调用记忆插件 %s 失败", method_name, exc_info=True)
            return default
    return default
