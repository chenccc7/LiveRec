"""房间管理：持久化清单 + 录制任务调度 + 事件广播。"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, Callable

from . import paths, resolver
from .config import Config, parse_url_config, write_url_config
from .logbook import log
from .recorder import RecordTask


def _fallback_name(url: str, platform: str) -> str:
    """解析失败时给卡片一个可读标题：平台名 + 房间号。"""
    label = resolver.PLATFORMS.get(platform, {}).get("label", platform)
    tail = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return f"{label} {tail}" if tail else label


class Manager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.meta: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, RecordTask] = {}
        self.runtime: dict[str, dict[str, Any]] = {}
        self._subs: list[Callable[[dict], None]] = []
        self._order: list[str] = []
        self._room_locks: dict[str, asyncio.Lock] = {}
        # 无人值守默认关闭：新装的程序（以及新添加的房间）不会启动就自动开录，
        # 需要用户逐张卡片点开「自动」才会在下次启动时自动恢复监测。
        self.unattended: bool = False
        self.load()

    # ---------- 持久化 ----------
    @property
    def state_path(self) -> str:
        return paths.ui_state_path()

    def load(self) -> None:
        data: dict[str, Any] = {}
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                data = {}
        rooms = data.get("rooms") if isinstance(data, dict) else None
        if isinstance(data, dict) and "unattended" in data:
            self.unattended = bool(data.get("unattended"))
        if not rooms:
            # 首次运行：从命令行版的 URL_config.ini 导入
            rooms = []
            for item in parse_url_config():
                rooms.append({
                    "id": uuid.uuid4().hex[:10],
                    "url": item["url"],
                    "quality": item["quality"] or self.cfg.quality(),
                    "danmaku": True,
                    "enabled": item["enabled"],
                    "anchor": item["anchor"],
                    "title": "",
                    "createdAt": int(time.time() * 1000),
                })
            if rooms:
                log("INFO", f"已从 URL_config.ini 导入 {len(rooms)} 个直播间")
        for room in rooms:
            rid = str(room.get("id") or uuid.uuid4().hex[:10])
            room["id"] = rid
            room.setdefault("quality", self.cfg.quality())
            room.setdefault("danmaku", True)
            room.setdefault("enabled", True)
            # 无人值守是「每个直播间各自一份」：老版本没有这个字段时，沿用当时的全局开关
            room.setdefault("unattended", bool(self.unattended))
            room.setdefault("anchor", "")
            room.setdefault("title", "")
            if not str(room.get("anchor") or "").strip():
                room["anchor"] = _fallback_name(room.get("url", ""), resolver.detect_platform(room.get("url", "")))
            self.meta[rid] = room
            self._order.append(rid)
        self.save()

    def save(self) -> None:
        payload = {
            "version": 1,
            "unattended": bool(self.unattended),
            "rooms": [self.meta[rid] for rid in self._order if rid in self.meta],
        }
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log("WARN", f"保存房间清单失败：{exc}")
        try:
            write_url_config([
                {"quality": r.get("quality"), "url": r.get("url"),
                 "anchor": r.get("anchor"), "enabled": r.get("enabled", True)}
                for r in (self.meta[rid] for rid in self._order if rid in self.meta)
            ])
        except OSError:
            pass

    # ---------- 订阅 ----------
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subs.append(fn)

    def unsubscribe(self, fn: Callable[[dict], None]) -> None:
        try:
            self._subs.remove(fn)
        except ValueError:
            pass

    def broadcast(self, event: dict) -> None:
        for fn in list(self._subs):
            try:
                fn(event)
            except Exception:
                pass

    # ---------- 房间 CRUD ----------
    def add(self, url: str, quality: str | None = None, danmaku: bool = True,
            anchor: str = "") -> dict[str, Any]:
        url = (url or "").strip()
        if not url:
            raise ValueError("直播间地址不能为空")
        platform = resolver.detect_platform(url)
        if platform == "unknown":
            raise ValueError("暂不支持该链接，目前支持抖音 / B站 / 虎牙 / 斗鱼")
        for room in self.meta.values():
            if room.get("url") == url:
                raise ValueError("该直播间已存在")
        rid = uuid.uuid4().hex[:10]
        # 画质不再白名单校验：虎牙等平台用「蓝光20M」这种房间自带的档位名，
        # 归一成六档会把不同码率的档位合并掉。非空即保留，空才回落到默认。
        quality = (quality or "").strip() or self.cfg.quality()
        room = {
            "id": rid,
            "url": url,
            "quality": quality or self.cfg.quality(),
            "danmaku": bool(danmaku),
            "enabled": True,
            "unattended": bool(self.unattended),
            "anchor": (anchor or "").strip() or _fallback_name(url, platform),
            "title": "",
            "createdAt": int(time.time() * 1000),
        }
        self.meta[rid] = room
        self._order.append(rid)
        self.save()
        log("INFO", f"已添加直播间 {url}")
        self.broadcast({"type": "rooms"})
        return room

    def update(self, rid: str, patch: dict[str, Any]) -> dict[str, Any]:
        room = self.meta.get(rid)
        if not room:
            raise KeyError(rid)
        for key in ("quality", "danmaku", "enabled", "anchor", "url", "unattended"):
            if key in patch and patch[key] is not None:
                room[key] = bool(patch[key]) if key == "unattended" else patch[key]
        task = self.tasks.get(rid)
        if task:
            if "quality" in patch and patch["quality"]:
                task.quality = patch["quality"]
            if "danmaku" in patch:
                task.danmaku_enabled = bool(patch["danmaku"])
        self.save()
        self.broadcast({"type": "rooms"})
        return room

    async def remove(self, rid: str) -> None:
        """删卡片：必须先把任务停干净，否则 ffmpeg / 弹幕连接会留在后台继续写。"""
        async with self._lock(rid):
            task = self.tasks.pop(rid, None)
            self.meta.pop(rid, None)
            if rid in self._order:
                self._order.remove(rid)
            self.runtime.pop(rid, None)
            if task is not None:
                try:
                    await task.stop("removed")
                except Exception:  # noqa: BLE001 - 删卡片不该被收尾异常挡住
                    pass
            self._room_locks.pop(rid, None)
            self.save()
            self.broadcast({"type": "rooms"})

    # ---------- 任务控制 ----------
    async def _ensure_task(self, rid: str) -> RecordTask | None:
        room = self.meta.get(rid)
        if not room:
            return None
        task = self.tasks.get(rid)
        if task is not None:
            if task.is_running():
                return task
            # 停止过（或被看门狗/异常结束）的任务不能再复用：
            # 旧实现会把它原样返回，于是「开始」什么也没发生，看起来就是「停不回来」。
            # 这里还要真的把它停干净（杀掉可能残留的 ffmpeg / 弹幕会话），
            # 否则旧任务继续推快照，卡片上的数字会来回跳。
            self.tasks.pop(rid, None)
            try:
                await task.stop("stopped")
            except Exception:  # noqa: BLE001 - 收尾失败不影响重建
                pass
            self.runtime.pop(rid, None)
        task = RecordTask(
            rid,
            room["url"],
            room.get("quality") or self.cfg.quality(),
            self.cfg,
            on_update=lambda snap: self._on_update(rid, snap),
            danmaku=bool(room.get("danmaku", True)),
        )
        task.anchor = room.get("anchor") or ""
        task.danmaku_forward = self._on_danmaku
        self.tasks[rid] = task
        return task

    async def start_room(self, rid: str) -> dict[str, Any]:
        async with self._lock(rid):
            return await self._start_locked(rid)

    async def stop_room(self, rid: str) -> dict[str, Any]:
        async with self._lock(rid):
            return await self._stop_locked(rid)

    async def retry_room(self, rid: str) -> dict[str, Any]:
        async with self._lock(rid):
            await self._stop_locked(rid)
            return await self._start_locked(rid)

    async def restart_room(self, rid: str) -> dict[str, Any]:
        """地址/画质改动后重建任务，让新配置真正生效。"""
        async with self._lock(rid):
            await self._stop_locked(rid)
            task = self.tasks.pop(rid, None)
            if task is not None:
                try:
                    await task.stop("stopped")
                except Exception:  # noqa: BLE001
                    pass
            self.runtime.pop(rid, None)
            return await self._start_locked(rid)

    def _lock(self, rid: str) -> asyncio.Lock:
        """每个房间一把锁：连点「停止 → 开始」时不会让两个操作互相插队。"""
        lock = self._room_locks.get(rid)
        if lock is None:
            lock = asyncio.Lock()
            self._room_locks[rid] = lock
        return lock

    async def _start_locked(self, rid: str) -> dict[str, Any]:
        task = await self._ensure_task(rid)
        if task is None:
            raise KeyError(rid)
        self.meta[rid]["enabled"] = True
        task.start()
        self.save()
        return task.snapshot()

    async def _stop_locked(self, rid: str, status: str = "stopped") -> dict[str, Any]:
        task = self.tasks.get(rid)
        if task is not None:
            await task.stop(status)
        snap = self.runtime.get(rid) or {"id": rid, "status": status}
        return snap

    async def start_all(self) -> int:
        count = 0
        for rid in list(self._order):
            if self.meta.get(rid, {}).get("enabled", True):
                await self.start_room(rid)
                count += 1
        return count

    async def resume_unattended(self) -> int:
        """无人值守：启动时只恢复「自己开着无人值守」的房间。"""
        count = 0
        for rid in list(self._order):
            room = self.meta.get(rid) or {}
            if not room.get("enabled", True):
                continue
            if not room.get("unattended", bool(self.unattended)):
                continue
            await self.start_room(rid)
            count += 1
        if count:
            log("INFO", f"无人值守已启动，{count} 个直播间进入开播监测"
                        f"（每 {self.cfg.check_interval()}s 检测一次）")
        else:
            log("INFO", "没有任何直播间开启无人值守，启动后不会自动开始监测")
        return count

    async def set_room_unattended(self, rid: str, enabled: bool) -> dict[str, Any]:
        """单卡片无人值守开关：立即落盘，开启时顺便把该房间拉起来。"""
        room = self.meta.get(rid)
        if not room:
            raise KeyError(rid)
        room["unattended"] = bool(enabled)
        self.save()
        started = 0
        if enabled:
            await self.start_room(rid)
            started = 1
        label = room.get("anchor") or rid
        log("INFO", f"{label} 无人值守{'已开启' if enabled else '已关闭'}"
                    f"{'，已进入开播监测' if enabled else '（下次启动不再自动监测）'}")
        self.broadcast({"type": "rooms"})
        return {"ok": True, "unattended": bool(enabled), "started": started}

    async def set_unattended(self, enabled: bool) -> dict[str, Any]:
        """全局开关（托盘用）：一次性改所有房间。"""
        self.unattended = bool(enabled)
        for room in self.meta.values():
            room["unattended"] = bool(enabled)
        self.save()
        started = 0
        if self.unattended:
            started = await self.start_all()
            log("INFO", f"已为全部直播间开启无人值守，{started} 个进入开播监测")
        else:
            log("INFO", "已为全部直播间关闭无人值守：重启程序后不会自动开始监测")
        self.broadcast({"type": "rooms"})
        return {"ok": True, "unattended": self.unattended, "started": started}

    async def stop_all(self) -> int:
        count = 0
        for rid, task in list(self.tasks.items()):
            await task.stop("stopped")
            count += 1
        return count

    async def shutdown(self) -> None:
        for task in list(self.tasks.values()):
            await task.stop("stopped")
        self.save()

    # ---------- 回调 ----------
    def _on_update(self, rid: str, snap: dict[str, Any]) -> None:
        self.runtime[rid] = snap
        room = self.meta.get(rid)
        if room:
            if snap.get("anchor"):
                room["anchor"] = snap["anchor"]
            if snap.get("title"):
                room["title"] = snap["title"]
        self.broadcast({"type": "room", "room": snap})

    def _on_danmaku(self, evt: dict) -> None:
        self.broadcast({"type": "danmaku", "event": evt})

    # ---------- 查询 ----------
    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for rid in self._order:
            room = self.meta.get(rid)
            if not room:
                continue
            snap = self.runtime.get(rid)
            if not snap:
                snap = {
                    "id": rid,
                    "url": room.get("url"),
                    "platform": resolver.detect_platform(room.get("url", "")),
                    "platformLabel": {"douyin": "抖音", "bilibili": "B站",
                                      "huya": "虎牙", "douyu": "斗鱼"}.get(
                        resolver.detect_platform(room.get("url", "")), "其它"),
                    "quality": room.get("quality"),
                    "anchor": room.get("anchor") or "",
                    "title": room.get("title") or "",
                    "status": "idle",
                    "error": "",
                    "elapsed": 0,
                    "size": 0,
                    "sizeText": "0 B",
                    "bitrate": "",
                    "resolution": "",
                    "segments": 0,
                    "currentFile": "",
                    "outDir": "",
                    "startedAt": 0,
                    "danmakuCount": 0,
                    "danmakuStatus": "off",
                    "danmakuNote": "",
                    "sessionId": "",
                }
            snap = dict(snap)
            snap["enabled"] = bool(room.get("enabled", True))
            snap["danmaku"] = bool(room.get("danmaku", True))
            snap["danmakuEnabled"] = bool(room.get("danmaku", True))
            snap["unattended"] = bool(room.get("unattended", bool(self.unattended)))
            # 画质以「正在跑的任务」为准：录制端开录前会按房间当前真实档位校正 quality，
            # 如果这里再用 meta 里的旧值覆盖回去，卡片显示的档位就会和实际录制的不一致。
            live_quality = snap.get("quality")
            if rid in self.runtime and live_quality and live_quality != room.get("quality"):
                room["quality"] = live_quality      # 同步回元数据，下次启动也沿用真实档位
            snap["quality"] = live_quality or room.get("quality")
            out.append(snap)
        return out

    def stats(self) -> dict[str, Any]:
        rooms = self.snapshot()
        recording = sum(1 for r in rooms if r.get("status") == "recording")
        waiting = sum(1 for r in rooms if r.get("status") == "waiting")
        total_bytes = sum(int(r.get("size") or 0) for r in rooms)
        danmaku = sum(int(r.get("danmakuCount") or 0) for r in rooms)
        return {
            "total": len(rooms),
            "recording": recording,
            "waiting": waiting,
            "bytes": total_bytes,
            "danmaku": danmaku,
        }


async def sleep_forever() -> None:
    await asyncio.sleep(3600)
