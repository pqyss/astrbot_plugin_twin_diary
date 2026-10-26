"""与时间有关的纯函数，方便测试“今天该不该写”。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta


def target_for_day(now: datetime, at: time) -> datetime:
    """返回某一天的设定时间点。

    Args:
        now: 当前时间（需带时区）。
        at: 每天的设定时间。

    Returns:
        与 ``now`` 同一时区的目标时间。
    """
    return datetime.combine(now.date(), at, tzinfo=now.tzinfo)


def next_run_at(now: datetime, at: time) -> datetime:
    """计算下一次自动写日记的时间。

    Args:
        now: 当前时间（需带时区）。
        at: 每天的设定时间。

    Returns:
        今天的时间点如果还没到就返回今天，否则返回明天。
    """
    today_target = target_for_day(now, at)
    if now < today_target:
        return today_target
    return datetime.combine(
        now.date() + timedelta(days=1),
        at,
        tzinfo=now.tzinfo,
    )


def should_run_today(
    now: datetime,
    at: time,
    *,
    catch_up_hours: float,
    last_done_date: str,
) -> bool:
    """判断此刻是否应该触发今天的自动日记。

    规则：已经过了设定时间、今天还没写过、且没有超过补写时限。

    Args:
        now: 当前时间（需带时区）。
        at: 每天的设定时间。
        catch_up_hours: 允许补写的小时数。
        last_done_date: 最近一次成功写公开日记的日期（``YYYY-MM-DD``）。

    Returns:
        应该触发时返回 True。
    """
    today = now.date().isoformat()
    if last_done_date == today:
        return False
    target = target_for_day(now, at)
    if now < target:
        return False
    elapsed = (now - target).total_seconds()
    if catch_up_hours <= 0:
        # 不补写时给 60 秒的容差，避免调度循环刚好晚几秒就永远错过。
        return 0 <= elapsed <= 60
    return elapsed <= catch_up_hours * 3600


def today_str(now: datetime) -> str:
    """返回当前的日期字符串。

    Args:
        now: 当前时间。

    Returns:
        ``YYYY-MM-DD``。
    """
    return now.date().isoformat()


def date_str(value: date) -> str:
    """把日期格式化为文件名使用的形式。

    Args:
        value: 日期。

    Returns:
        ``YYYY-MM-DD``。
    """
    return value.isoformat()
