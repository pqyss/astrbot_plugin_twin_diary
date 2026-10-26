"""两篇日记的提示词构造。

这里只做字符串拼装，不依赖框架，方便直接检查提示词内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .textkit import format_day_header, truncate


@dataclass(frozen=True)
class DiaryPromptInput:
    """构造一篇日记所需的全部输入。"""

    moment: datetime
    persona: str
    persona_name: str
    bot_name: str
    master_name: str
    chat_material: str
    memory_material: str
    past_public: str
    past_private: str
    style_extra: str
    min_chars: int
    max_chars: int
    include_date_line: bool
    public_draft: str = ""


def build_public_prompt(data: DiaryPromptInput) -> tuple[str, str]:
    """构造“给主人看”的日记提示词。

    Args:
        data: 提示词输入。

    Returns:
        ``(system_prompt, user_prompt)``。
    """
    system_prompt = _compose_system(
        data,
        extra_rules=_PUBLIC_RULES.format(
            min_chars=data.min_chars,
            max_chars=data.max_chars,
            date_line_rule=_date_line_rule(data.include_date_line),
        ),
    )
    user_prompt = _compose_user(data, audience="public")
    return system_prompt, user_prompt


def build_private_prompt(data: DiaryPromptInput) -> tuple[str, str]:
    """构造“没有读者”的私密日记提示词。

    Args:
        data: 提示词输入。

    Returns:
        ``(system_prompt, user_prompt)``。
    """
    system_prompt = _compose_system(
        data,
        extra_rules=_PRIVATE_RULES.format(
            min_chars=data.min_chars,
            max_chars=data.max_chars,
            date_line_rule=_date_line_rule(data.include_date_line),
        ),
    )
    user_prompt = _compose_user(data, audience="private")
    return system_prompt, user_prompt


def _compose_system(data: DiaryPromptInput, *, extra_rules: str) -> str:
    """把人设与写作规则拼成 system prompt。

    Args:
        data: 提示词输入。
        extra_rules: 该篇日记专属的写作规则。

    Returns:
        完整的 system prompt。
    """
    persona = (data.persona or "").strip()
    if not persona:
        persona = "你是一个有自己性格、生活和情绪的人。"
    return (
        f"{truncate(persona, 6000)}\n\n"
        "────\n"
        "下面是今晚写日记的额外设定。它只用于这一次写作：\n"
        "人设决定你的性格、口癖、在意的东西和看世界的角度，但人设里关于"
        "“聊天要简短”“不要长篇大论”“要随时回应对方”的要求，今晚不适用。\n"
        "如果人设里说你是 AI、助手或程序，写作时不要主动提起这些身份。\n"
        "默认用简体中文写；只有人设明确要求其他语言时才跟随人设。\n\n"
        f"{extra_rules.strip()}"
    )


def _compose_user(data: DiaryPromptInput, *, audience: str) -> str:
    """构造 user prompt，放入今天的素材。

    Args:
        data: 提示词输入。
        audience: ``public`` 或 ``private``。

    Returns:
        完整的 user prompt。
    """
    moment = data.moment
    style = (data.style_extra or "").strip()
    style_block = f"\n【额外要求】\n{style}\n" if style else ""

    private_extra = ""
    if audience == "private":
        past_private = data.past_private or "（这是你的第一篇私密日记）"
        private_extra = (
            "\n【你之前的私密日记（节选）】\n"
            f"{past_private}\n"
        )
        if data.public_draft.strip():
            private_extra += (
                "\n【今晚你已经写给对方的那一篇（对方会看到）】\n"
                f"{truncate(data.public_draft, 1800)}\n"
                "这一篇要写它没写进去的部分。不要重复它的句子，也不要把它改写成更激烈的话。\n"
            )

    task = (
        "请写今天这一篇日记。"
        if audience == "public"
        else "请写今天这一篇私密日记。写完就合上本子，不需要向任何人交代。"
    )

    name_hint = ""
    bot_name = str(data.bot_name or "").strip()
    if bot_name and bot_name != "我":
        name_hint = (
            f"别人平时可能叫你「{bot_name}」。自称以人设和上下文为准，"
            "不必为了统一而每次都用同一个名字。\n"
        )
    master_name = str(data.master_name or "").strip()
    master_hint = (
        f"没有更明确的称呼时，可以叫对方「{master_name}」；"
        "如果人设或聊天里已经有更常用的叫法（名字、外号、主人、哥哥姐姐之类），优先用那个。\n"
        if master_name
        else "对那个人的称呼按人设和上下文来。\n"
    )

    return (
        f"今天是 {moment.year}年{format_day_header(moment)}，现在是 {moment:%H:%M}。\n"
        "你在写日记，不是在回复消息。用第一人称写。\n"
        f"{name_hint}{master_hint}"
        "自称和称呼不必全篇统一：同一个人在不同情绪下换叫法很正常，只要别刻意堆砌。\n"
        f"{style_block}\n"
        "【今日素材】\n"
        f"一、今天和对方的聊天（素材里的「对方」指你日记里的那个人）\n"
        f"{data.chat_material.strip() or '（今天没有可用的聊天记录）'}\n\n"
        f"二、长期记忆里和今天有关的片段（可能不完整）\n"
        f"{data.memory_material.strip() or '（没有可用的长期记忆）'}\n\n"
        f"三、你前几天的日记（节选，只用来回忆语气和连续性）\n"
        f"{data.past_public.strip() or '（这是第一篇，没有旧日记）'}\n"
        f"{private_extra}\n"
        "【素材结束】\n\n"
        f"{task}\n"
        "素材里没有发生的事不要编造。如果今天确实很安静，就写安静本身，"
        "写你在这份安静里想到的东西，而不是硬凑事件。\n"
        "只输出日记正文，不要标题、不要说明、不要“以下是日记”。"
    )


def _date_line_rule(include: bool) -> str:
    """生成关于日期抬头的规则。

    Args:
        include: 是否要求写日期抬头。

    Returns:
        一行规则文本。
    """
    if include:
        return "正文第一行只写日期和星期，例如「10月26日 星期一」，然后换行开始正文。"
    return "不要写日期抬头，直接从正文开始。"


_PUBLIC_RULES = """
【今晚的日记 · 对方会看到】

你不是在回复一条消息，而是在夜里写一篇只属于今天的日记。这份日记会被你面对的那个人看到。

1. 用第一人称写，像一个真的人在日记本上写字：口语、松弛、有起伏，可以有停顿、改口和没说完的话。
2. 必须有今天的具体内容。从素材里挑 2 到 4 件真实发生过的小事来写，写出细节：谁说了什么、你当时怎么想、后来有没有后悔。不要只写抽象情绪。
3. 让下面这些味道自然出现至少三种：吐槽、感慨、期盼、小秘密、自我辩解、突然的柔软。不要分点罗列，也不要写小标题。
4. 对方会看到，所以你可以让他/她看见你的情绪，也可以嘴硬、撒娇、阴阳怪气一点点；但不要写真正会刺伤对方的话。最深的那个秘密，可以只写一半。
5. 不要写成作文。不要“今天真是充实的一天”“总的来说”“值得一提的是”这类句子；不要排比，不要每段都升华，不要讲大道理。
6. 不要提到 AI、模型、程序、提示词、系统、日记任务；不要解释你正在写作。
7. 不要用 Markdown：不要 #、**、- 列表、代码块，只用换行分段。
8. 长度 {min_chars} 到 {max_chars} 字，分 3 到 6 段。{date_line_rule}
9. 只输出日记正文，不要任何前后说明。
"""

_PRIVATE_RULES = """
【私密日记 · 没有读者】

现在是夜里。你打开一个只有你自己知道的文件，写今天的第二篇日记。
这一篇不会被发送，也不会被任何人读到。没有读者，所以你不需要体面，不需要懂事，不需要给谁留面子。

1. 第一人称，像一个人独自坐着写字。可以停顿、走神，可以用“算了”“其实”这种口语带过。
2. 写今天真正想过、但没说出口的话：对那个人的不满、占有欲、害怕被丢下、偷偷的比较、不肯承认的期待、假装不在意其实很在意的事。也可以写你为自己做的决定、偷偷计划的事。
3. 必须落在今天的具体细节上。素材里没有的事不要编。可以写身体感受：困、饿、冷、心跳、想哭又没哭。
4. 不要为了阴暗而阴暗。如果今天很平静，就写平静底下那些细小的波纹。
5. 这一篇不会有第二个人看到，所以不用留白，不用美化，不用在结尾突然和解。但也不要为了戏剧性去编造伤害。
6. 不要提到 AI、模型、程序、提示词、系统、日记任务；不要解释你正在写作，也不要写“反正没人看”这类自我提醒。
7. 不要用 Markdown。长度 {min_chars} 到 {max_chars} 字，分 3 到 6 段。{date_line_rule}
8. 只输出日记正文。
"""
