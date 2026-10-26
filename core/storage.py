"""日记文件与运行状态的持久化。

不依赖 AstrBot，目录由调用方传入，方便测试。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .textkit import truncate

_DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_STATE: dict = {
    "version": 1,
    "bound_umo": "",
    "bound_master_id": "",
    "bound_master_name": "",
    "bound_bot_id": "",
    "bound_platform": "",
    "bound_at": "",
    "last_public_date": "",
    "last_private_date": "",
    "last_error": "",
    "pending_public": None,
    "runs": [],
}


@dataclass(frozen=True)
class StorePaths:
    """插件用到的所有目录与状态文件路径。"""

    data_dir: Path
    public_dir: Path
    private_dir: Path
    state_path: Path


def resolve_path(base_dir: Path, raw: str, default: Path) -> Path:
    """解析配置里的目录：支持留空、相对路径、绝对路径和 ``~``。

    Args:
        base_dir: 相对路径的基准目录（AstrBot 的 data 目录）。
        raw: 配置值。
        default: 配置为空时使用的默认目录。

    Returns:
        绝对路径。
    """
    text = str(raw or "").strip()
    if not text:
        target = default
    else:
        expanded = os.path.expandvars(os.path.expanduser(text))
        target = Path(expanded)
        if not target.is_absolute():
            target = Path(base_dir) / target
    return target.expanduser().resolve()


def build_paths(
    *,
    data_dir: Path,
    plugin_data_dir: Path,
    public_setting: str,
    private_setting: str,
) -> StorePaths:
    """根据配置构造目录结构。

    Args:
        data_dir: AstrBot 的 data 目录。
        plugin_data_dir: 本插件的数据目录。
        public_setting: 公开日记目录配置。
        private_setting: 私密日记目录配置。

    Returns:
        解析后的路径集合。
    """
    plugin_data_dir = Path(plugin_data_dir).resolve()
    return StorePaths(
        data_dir=Path(data_dir).resolve(),
        public_dir=resolve_path(
            Path(data_dir),
            public_setting,
            plugin_data_dir / "public",
        ),
        private_dir=resolve_path(
            Path(data_dir),
            private_setting,
            plugin_data_dir / "private",
        ),
        state_path=plugin_data_dir / "state.json",
    )


class DiaryStore:
    """负责读写日记文件、状态文件和历史节选。"""

    def __init__(self, paths: StorePaths) -> None:
        """初始化存储对象并创建目录。

        Args:
            paths: 目录与状态文件路径。
        """
        self.paths = paths
        self.paths.public_dir.mkdir(parents=True, exist_ok=True)
        self.paths.private_dir.mkdir(parents=True, exist_ok=True)
        self.paths.state_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- state

    def load_state(self) -> dict:
        """读取状态文件；不存在或损坏时返回默认状态。

        Returns:
            状态字典。
        """
        state = dict(DEFAULT_STATE)
        state["runs"] = []
        path = self.paths.state_path
        if not path.exists():
            return state
        try:
            loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return state
        if not isinstance(loaded, dict):
            return state
        state.update(loaded)
        if not isinstance(state.get("runs"), list):
            state["runs"] = []
        return state

    def save_state(self, state: dict) -> None:
        """原子地写入状态文件。

        Args:
            state: 要保存的状态字典。
        """
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        _atomic_write(self.paths.state_path, payload, private=False)

    # ---------------------------------------------------------------- diary

    def _dir_for(self, kind: str) -> Path:
        """返回某一类日记的目录。

        Args:
            kind: ``public`` 或 ``private``。

        Returns:
            对应目录。

        Raises:
            ValueError: kind 不合法。
        """
        if kind == "public":
            return self.paths.public_dir
        if kind == "private":
            return self.paths.private_dir
        raise ValueError(f"未知的日记类型：{kind!r}")

    def path_for(self, kind: str, day: date) -> Path:
        """返回某天日记的文件路径。

        Args:
            kind: ``public`` 或 ``private``。
            day: 日期。

        Returns:
            文件路径。
        """
        return self._dir_for(kind) / f"{day.isoformat()}.md"

    def save_diary(self, kind: str, day: date, text: str) -> Path:
        """保存日记正文。

        Args:
            kind: ``public`` 或 ``private``。
            day: 日期。
            text: 日记正文。

        Returns:
            写入的文件路径。
        """
        path = self.path_for(kind, day)
        body = str(text or "").strip()
        _atomic_write(path, body + "\n", private=(kind == "private"))
        return path

    def read_diary(self, kind: str, day: date) -> str | None:
        """读取某天的日记。

        Args:
            kind: ``public`` 或 ``private``。
            day: 日期。

        Returns:
            日记正文；文件不存在时返回 None。
        """
        path = self.path_for(kind, day)
        if not path.exists():
            return None
        try:
            return (
                path.read_text(encoding="utf-8", errors="replace")
                .replace("\ufeff", "")
                .strip()
            )
        except OSError:
            return None

    def recent_diaries(
        self,
        kind: str,
        *,
        days: int,
        max_chars: int,
        before: date | None = None,
    ) -> list[tuple[str, str]]:
        """读取最近几天的日记节选，按日期从新到旧。

        Args:
            kind: ``public`` 或 ``private``。
            days: 最多读取几篇。
            max_chars: 每篇最多保留多少字。
            before: 可选的日期上限（不含当天），用于避免把刚写的算进去。

        Returns:
            ``(日期字符串, 节选)`` 列表。
        """
        if days <= 0:
            return []
        directory = self._dir_for(kind)
        items: list[tuple[str, str]] = []
        try:
            candidates = sorted(directory.glob("*.md"), reverse=True)
        except OSError:
            return []

        for path in candidates:
            stem = path.stem
            if not _DATE_FILE_RE.fullmatch(stem):
                continue
            if before is not None and stem >= before.isoformat():
                continue
            try:
                text = (
                    path.read_text(encoding="utf-8", errors="replace")
                    .replace("\ufeff", "")
                    .strip()
                )
            except OSError:
                continue
            if text:
                items.append((stem, truncate(text, max_chars)))
            if len(items) >= days:
                break
        return items

    def has_diary(self, kind: str, day: date) -> bool:
        """判断某天是否已经写过某类日记。

        Args:
            kind: ``public`` 或 ``private``。
            day: 日期。

        Returns:
            文件存在且非空时返回 True。
        """
        text = self.read_diary(kind, day)
        return bool(text)


def _atomic_write(path: Path, content: str, *, private: bool) -> None:
    """原子写文件，尽量避免断电或崩溃留下半个文件。

    Args:
        path: 目标路径。
        content: 文本内容。
        private: 是否按私密文件收紧权限（仅类 Unix 系统生效）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")

    # Windows 上目标文件可能被杀毒软件、网盘同步或另一个句柄短暂占用，
    # os.replace 会抛 PermissionError。重试几次，仍失败就退化为直接覆盖。
    last_error: OSError | None = None
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            break
        except OSError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.05)
    else:
        try:
            path.write_text(content, encoding="utf-8", newline="\n")
        except OSError:
            raise last_error  # type: ignore[misc]
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    if private and os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def record_run(state: dict, run: dict, limit: int = 40) -> None:
    """把一次运行记录追加到状态里，并裁掉过老的记录。

    Args:
        state: 状态字典，会被原地修改。
        run: 本次运行的摘要信息。
        limit: 最多保留多少条历史记录。
    """
    runs = state.get("runs")
    if not isinstance(runs, list):
        runs = []
    runs.append(run)
    state["runs"] = runs[-limit:]
