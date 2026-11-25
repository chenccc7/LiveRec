"""内存日志环 + loguru 汇流，供 WebSocket 实时推送。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable

from . import paths

_LEVEL_MAP = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "SUCCESS": "SUCCESS",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "CRITICAL": "ERROR",
}


class LogBook:
    """保留最近 N 条日志，并广播给订阅者（WebSocket）。"""

    def __init__(self, capacity: int = 600):
        self._buf: deque[dict] = deque(maxlen=capacity)
        self._subs: list[Callable[[dict], None]] = []
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, level: str, message: str) -> dict:
        level = _LEVEL_MAP.get(level.upper(), level.upper())
        now = datetime.now()
        with self._lock:
            self._seq += 1
            entry = {
                "id": self._seq,
                "ts": now.timestamp(),
                "time": now.strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            }
            self._buf.append(entry)
        for sub in list(self._subs):
            try:
                sub(entry)
            except Exception:  # 订阅者异常不能影响主流程
                pass
        return entry

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subs.append(fn)

    def unsubscribe(self, fn: Callable[[dict], None]) -> None:
        try:
            self._subs.remove(fn)
        except ValueError:
            pass

    def tail(self, limit: int = 200, after_id: int = 0) -> list[dict]:
        with self._lock:
            items = [e for e in self._buf if e["id"] > after_id]
        return items[-limit:]

    # ---------- loguru 汇流 ----------
    def attach_loguru(self) -> None:
        from loguru import logger

        book = self

        def _sink(message) -> None:
            rec = message.record
            name = rec.get("name") or ""
            # 上游模块自己的日志带 emitter，界面里只保留消息体
            book.add(rec["level"].name, str(rec["message"]))

        logger.add(
            _sink,
            level="INFO",
            format="{message}",
            colorize=False,
            backtrace=False,
            diagnose=False,
            enqueue=False,
        )
        self.add("INFO", "日志系统就绪（loguru 已接入）")


LOG = LogBook()
_last_log = time.time()


def log(level: str, message: str) -> None:
    LOG.add(level, message)


# ---------- 按天落盘（简单实现，避免依赖 loguru 的 rotator） ----------

class FileSink:
    def __init__(self, directory: str | None = None):
        self.dir = directory or paths.log_dir()
        os.makedirs(self.dir, exist_ok=True)
        self._day = datetime.now().strftime("%Y-%m-%d")

    def write(self, entry: dict) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
        path = os.path.join(self.dir, f"app_{day}.log")
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{entry['time']} {entry['level']:<7} {entry['message']}\n")
        except OSError:
            pass

    def current_path(self) -> str:
        return os.path.join(self.dir, f"app_{self._day}.log")


def journal_append(path: str, row: dict) -> None:
    """弹幕 JSONL 追加写。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


async def delay(seconds: float) -> None:
    await asyncio.sleep(seconds)
