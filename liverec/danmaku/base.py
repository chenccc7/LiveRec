"""弹幕采集：会话管理、落盘（JSONL + SQLite 索引）与事件分发。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from .. import paths
from ..logbook import LOG, log

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    platform     TEXT,
    room_key     TEXT,
    anchor       TEXT,
    started_at   REAL,
    ended_at     REAL,
    count        INTEGER DEFAULT 0,
    jsonl_path   TEXT,
    video_dir    TEXT
);
CREATE TABLE IF NOT EXISTS danmaku (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,
    platform     TEXT,
    room_key     TEXT,
    ts_ms        INTEGER,
    offset_ms    INTEGER,
    type         TEXT,
    user_id      TEXT,
    nickname     TEXT,
    content      TEXT,
    gift_name    TEXT,
    gift_count   INTEGER,
    repeat       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_danmaku_session ON danmaku(session_id);
CREATE INDEX IF NOT EXISTS idx_danmaku_offset ON danmaku(session_id, offset_ms);
"""


class DanmakuStore:
    """SQLite 索引 + JSONL 明细。线程安全。"""

    def __init__(self, db_path: str | None = None):
        self.path = db_path or os.path.join(paths.danmaku_dir(), "danmaku.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def start_session(self, session_id: str, platform: str, room_key: str,
                      anchor: str, jsonl_path: str, video_dir: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions"
                "(session_id, platform, room_key, anchor, started_at, jsonl_path, video_dir)"
                " VALUES (?,?,?,?,?,?,?)",
                (session_id, platform, room_key, anchor, time.time(), jsonl_path, video_dir),
            )
            self._conn.commit()

    def end_session(self, session_id: str, count: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_at=?, count=? WHERE session_id=?",
                (time.time(), count, session_id),
            )
            self._conn.commit()

    def insert(self, session_id: str, platform: str, room_key: str, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO danmaku(session_id, platform, room_key, ts_ms, offset_ms, type,"
                " user_id, nickname, content, gift_name, gift_count, repeat)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id, platform, room_key, row.get("ts_ms", 0), row.get("offset_ms", 0),
                    row.get("type", "chat"), row.get("user_id", ""), row.get("nickname", ""),
                    row.get("content", ""), row.get("gift_name", ""),
                    int(row.get("gift_count") or 0), int(row.get("repeat") or 0),
                ),
            )

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM danmaku")
            return int(cur.fetchone()[0])

    def sessions(self, limit: int = 100) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT session_id, platform, room_key, anchor, started_at, ended_at, count, jsonl_path"
                " FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


STORE = DanmakuStore()


def _safe_name(text: str) -> str:
    bad = '\\/:*?"<>|\r\n\t'
    out = "".join("_" if ch in bad else ch for ch in (text or "")).strip(" .")
    return out or "未知主播"


class DanmakuSession:
    """一个直播场次的弹幕采集会话。"""

    def __init__(self, client, platform: str, room_key: str, anchor: str,
                 video_dir: str, room_id: str, on_event: Callable[[dict], None]):
        self.client = client
        self.platform = platform
        self.room_key = room_key
        self.anchor = anchor
        self.video_dir = video_dir
        self.room_id = room_id
        self.on_event = on_event
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        # 同一秒内「停止 → 再开始」会撞到相同的 session_id：
        # 两份会话共用一个 JSONL 与一行 DB 记录，条数就会一会儿大一会儿小。
        # 追加 4 位随机串保证每次录制都有独立会话。
        self.session_id = f"{platform}_{_safe_name(anchor)}_{stamp}_{uuid4().hex[:4]}"
        folder = os.path.join(paths.danmaku_dir(), platform, _safe_name(anchor))
        self.jsonl_path = os.path.join(folder, f"{self.session_id}.jsonl")
        self.count = 0
        self.status = "connecting"
        self._t0 = time.time()
        self._task = None

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        import asyncio

        os.makedirs(os.path.dirname(self.jsonl_path), exist_ok=True)
        STORE.start_session(self.session_id, self.platform, self.room_key,
                            self.anchor, self.jsonl_path, self.video_dir)
        self.status = "live"
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        import asyncio

        backoff = 3
        while self.status != "stopped":
            try:
                self.status = "live"
                await self.client.run(self.emit)
                self.status = "closed"
            except asyncio.CancelledError:
                self.status = "stopped"
                raise
            except Exception as exc:
                self.status = "retry"
                log("WARN", f"[弹幕] {self.anchor} 连接中断：{type(exc).__name__}: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if self.status == "stopped":
                break
            await asyncio.sleep(backoff)

    async def stop(self) -> None:
        self.status = "stopped"
        if self._task:
            self._task.cancel()
            try:
                import asyncio

                await self._task
            except BaseException:
                pass
            self._task = None
        STORE.end_session(self.session_id, self.count)
        log("INFO", f"[弹幕] {self.anchor} 已停止，本次共 {self.count} 条 → {self.jsonl_path}")

    # ---------- 事件 ----------
    def emit(self, evt: dict[str, Any]) -> None:
        now = time.time()
        ts_ms = int(now * 1000)
        row = {
            "session_id": self.session_id,
            "platform": self.platform,
            "room_id": self.room_key,
            "ts_ms": ts_ms,
            "offset_ms": int((now - self._t0) * 1000),
            "type": evt.get("type", "chat"),
            "user_id": str(evt.get("user_id") or ""),
            "nickname": evt.get("nickname") or "",
            "content": evt.get("content") or "",
            "gift_name": evt.get("gift_name") or "",
            "gift_count": int(evt.get("gift_count") or 0),
            "repeat": int(evt.get("repeat") or 0),
        }
        self.count += 1
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass
        try:
            STORE.insert(self.session_id, self.platform, self.room_key, row)
            if self.count % 40 == 0:
                STORE.commit()
        except sqlite3.Error:
            pass
        if self.on_event:
            try:
                self.on_event({"roomId": self.room_id, "count": self.count, **row})
            except Exception:
                pass


# ---------- 平台客户端注册 ----------

def create_client(platform: str, url: str, cookie: str = ""):
    from .bilibili import BilibiliDanmaku
    from .douyu import DouyuDanmaku
    from .douyin import DouyinDanmaku
    from .huya import HuyaDanmaku

    makers = {
        "bilibili": BilibiliDanmaku,
        "douyu": DouyuDanmaku,
        "douyin": DouyinDanmaku,
        "huya": HuyaDanmaku,
    }
    maker = makers.get(platform)
    if maker is None:
        return None
    return maker(url, cookie)


def journal_path(platform: str, anchor: str, stamp: str) -> str:
    folder = os.path.join(paths.danmaku_dir(), platform, _safe_name(anchor))
    return os.path.join(folder, f"{platform}_{_safe_name(anchor)}_{stamp}.jsonl")


__all__ = ["DanmakuSession", "DanmakuStore", "STORE", "create_client", "journal_path", "LOG"]
