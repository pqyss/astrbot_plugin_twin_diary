"""与框架无关的文本处理工具。

这里不导入任何 AstrBot 模块，方便离线单元测试。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, time

_WEEKDAYS_CN = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)

# 模型偶尔会把“日记：”之类的标签一起吐出来，统一去掉。
_LABEL_PATTERN = re.compile(
    r"^(?:公开日记|私密日记|私密|日记正文|今日日记|今天的日记|今晚的日记|日记)\s*[:：]\s*",
)
_SENTENCE_END_PATTERN = re.compile(r"(?<=[。！？!?…~～])\s*")


def weekday_cn(value: date | datetime) -> str:
    """返回中文星期，例如“星期日”。

    Args:
        value: 日期或时间对象。

    Returns:
        星期的中文写法。
    """
    return _WEEKDAYS_CN[value.weekday()]


def format_day_header(value: date | datetime) -> str:
    """返回日记抬头，例如“10月26日 星期日”。

    Args:
        value: 日期或时间对象。

    Returns:
        不带年份的中文日期抬头。
    """
    return f"{value.month}月{value.day}日 {weekday_cn(value)}"


def parse_hhmm(raw: str) -> time:
    """解析 HH:MM 格式的时间。

    Args:
        raw: 形如 ``"22:30"`` 或 ``"7:05"`` 的字符串。

    Returns:
        解析后的 ``datetime.time``。

    Raises:
        ValueError: 格式不是 HH:MM，或时分超出范围。
    """
    text = str(raw or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError(f"时间格式应为 HH:MM，收到：{raw!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围：{raw!r}")
    return time(hour=hour, minute=minute)


def parse_date_arg(raw: str, today: date | None = None) -> date:
    """解析指令里的日期参数。

    支持 ``YYYY-MM-DD``、``MM-DD``、``今天``、``昨天``、``前天``。

    Args:
        raw: 用户输入的日期文本；空字符串表示今天。
        today: 用于相对日期的基准日，默认取系统本地日期。

    Returns:
        解析后的日期。

    Raises:
        ValueError: 无法识别的日期格式。
    """
    base = today or date.today()
    text = str(raw or "").strip()
    if not text or text in {"今天", "today"}:
        return base
    if text in {"昨天", "yesterday"}:
        return base - timedelta(days=1)
    if text in {"前天"}:
        return base - timedelta(days=2)

    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", text)
    if match:
        return date(base.year, int(match.group(1)), int(match.group(2)))

    raise ValueError(f"无法识别的日期：{raw!r}，请使用 2026-10-26 这种格式")


def clean_llm_text(raw: str) -> str:
    """清理模型输出里的代码块、标签和 Markdown 噪声。

    Args:
        raw: 模型返回的原始文本。

    Returns:
        只保留正文的干净文本。
    """
    text = str(raw or "").replace("\ufeff", "").replace("\u200b", "").strip()
    if not text:
        return ""

    # 有些模型会把思考过程直接内联在正文里
    text = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", text).strip()

    # 去掉整段被包在代码块里的情况
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*[ \t]*\n?", "", text)
        text = re.sub(r"\n?```[ \t]*$", "", text).strip()

    # 去掉“日记：”“私密日记：”等前缀，最多剥两层
    for _ in range(2):
        new_text = _LABEL_PATTERN.sub("", text)
        if new_text == text:
            break
        text = new_text

    # 整段被引号包住时去掉最外层引号
    if len(text) >= 2 and text[0] in {"“", '"'} and text[-1] in {"”", '"'}:
        text = text[1:-1].strip()

    # Markdown 标题、引用、列表符号
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    text = re.sub(r"(?m)^\s{0,3}[-*+]\s+", "", text)
    text = re.sub(r"(?m)^\s{0,3}\d+[.)、]\s+", "", text)

    # 粗体、斜体、行内代码
    text = text.replace("**", "").replace("__", "")
    text = text.replace("`", "")

    # 行尾空格与多余空行
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def looks_like_refusal(text: str) -> bool:
    """粗略判断模型是不是在拒绝或解释自己，而不是写日记。

    Args:
        text: 清理后的模型输出。

    Returns:
        如果看起来像拒绝/说明文，返回 True。
    """
    sample = text.strip()[:120]
    if not sample:
        return True
    head = sample.lstrip()
    refusal_starts = (
        "作为一个 AI",
        "作为一个AI",
        "作为 AI",
        "作为AI",
        "我是一个语言模型",
        "我无法",
        "I cannot",
        "As an AI",
    )
    if any(head.startswith(prefix) for prefix in refusal_starts):
        return True
    lowered = head[:40].lower()
    return "语言模型" in head[:30] or "language model" in lowered


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """按字符数截断文本，尽量不截断在半个词上。

    Args:
        text: 原始文本。
        limit: 最大字符数。
        suffix: 截断后追加的省略标记。

    Returns:
        截断后的文本；未超长时原样返回。
    """
    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def split_for_chat(text: str, max_chars: int = 260) -> list[str]:
    """把长日记拆成适合聊天窗口逐条发送的片段。

    优先按空行分段；单段过长时按句号、问号、感叹号等断句；再把小段
    合并到不超过 ``max_chars`` 的消息里。

    Args:
        text: 日记正文。
        max_chars: 单条消息的软上限，最小按 80 处理。

    Returns:
        待发送的文本片段列表；空文本返回空列表。
    """
    value = str(text or "").strip()
    if not value:
        return []

    limit = max(80, int(max_chars))
    if len(value) <= limit:
        return [value]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", value) if p.strip()]
    if not paragraphs:
        paragraphs = [value]

    # 每一段先压到 limit 以内，作为不可再分的“块”
    blocks: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= limit:
            blocks.append(paragraph)
            continue
        sentences = [s.strip() for s in _SENTENCE_END_PATTERN.split(paragraph) if s.strip()]
        if not sentences:
            sentences = [paragraph]
        blocks.extend(_pack_units(sentences, limit, joiner=""))

    return _pack_units(blocks, limit, joiner="\n\n")


def _pack_units(units: list[str], limit: int, joiner: str) -> list[str]:
    """把若干文本单元按长度合并成消息块。

    Args:
        units: 文本单元列表。
        limit: 单块最大字符数。
        joiner: 单元之间的连接符。

    Returns:
        合并后的文本块列表。
    """
    packed: list[str] = []
    buffer = ""
    for unit in units:
        # 单个句子仍可能超长（例如没有标点的长段落），按硬上限切开。
        while len(unit) > limit:
            if buffer:
                packed.append(buffer)
                buffer = ""
            packed.append(unit[:limit])
            unit = unit[limit:]
        if not unit:
            continue
        candidate = unit if not buffer else f"{buffer}{joiner}{unit}"
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            packed.append(buffer)
        buffer = unit
    if buffer:
        packed.append(buffer)
    return packed
