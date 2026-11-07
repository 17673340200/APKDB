"""定时播放计划：时段判断与配置读写（纯 Python，桌面与安卓共用）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

_ITEMS_KEY = "items"


def parse_hhmm(value: str) -> int | None:
    """把 "08:30" 转成当天分钟数，格式不对返回 None。"""
    text = (value or "").strip()
    if ":" not in text:
        return None
    hour, _, minute = text.partition(":")
    try:
        hours, minutes = int(hour), int(minute)
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


def in_window(minutes: int, start: int, end: int | None) -> bool:
    """当前分钟数是否落在 [start, end) 时段内；end 为空表示播到当天结束。"""
    if end is None:
        return minutes >= start
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # 跨零点的时段，如 22:00–06:00


def is_valid_item(item: object) -> bool:
    """时段与文件都齐了才算一条有效计划。"""
    if not isinstance(item, dict):
        return False
    has_time = bool(item.get("start") or item.get("time"))
    has_file = bool(item.get("file") or item.get("name"))
    return has_time and has_file


def load_schedule(path: str | Path) -> list[dict]:
    """读取计划文件（坏文件按空计划处理，不让播放器起不来）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get(_ITEMS_KEY, []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [item for item in items if is_valid_item(item)]


def save_schedule(path: str | Path, items: list[dict]) -> None:
    Path(path).write_text(
        json.dumps({_ITEMS_KEY: list(items)}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def now_minutes() -> int:
    """当前本地时间对应的当天分钟数。"""
    now = time.localtime()
    return now.tm_hour * 60 + now.tm_min


def today() -> str:
    return time.strftime("%Y-%m-%d")


def window_key(day: str, start: int, end: int | None, target: str) -> str:
    """某条计划在某天的唯一标识（用来避免同一时段重复打开）。"""
    return f"{day}|{start}-{end}|{target}"
