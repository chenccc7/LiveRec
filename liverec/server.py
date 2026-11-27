"""FastAPI 服务：REST + WebSocket 实时推送，静态页兜底。"""
from __future__ import annotations

import asyncio
import os
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cookie_check, paths, resolver
from .config import Config
from .logbook import LOG, FileSink, log
from .manager import Manager, sleep_forever

APP_VERSION = "0.2.0"
FILE_SINK = FileSink()
LOG.subscribe(FILE_SINK.write)


class Hub:
    """WebSocket 广播中心。"""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    async def broadcast(self, event: dict[str, Any]) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def push(self, event: dict[str, Any]) -> None:
        """从任意同步上下文安全投递。"""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast(event)))
        except RuntimeError:
            pass


class StartBody(BaseModel):
    url: str = ""
    quality: str | None = None
    danmaku: bool = True
    anchor: str = ""
    start: bool = True
    splitMode: str | None = None
    splitSeconds: int | None = None


class PatchBody(BaseModel):
    quality: str | None = None
    splitMode: str | None = None
    splitSeconds: int | None = None
    danmaku: bool | None = None
    enabled: bool | None = None
    anchor: str | None = None
    url: str | None = None
    unattended: bool | None = None


class SettingsBody(BaseModel):
    quality: str | None = None
    saveFormat: str | None = None
    saveRoot: str | None = None
    danmakuRoot: str | None = None
    ffmpegPath: str | None = None
    splitEnabled: bool | None = None
    splitSeconds: int | None = None
    checkInterval: int | None = None
    concurrent: int | None = None
    folderByAuthor: bool | None = None
    cookies: dict[str, str] | None = None


class PathBody(BaseModel):
    path: str


class PreviewBody(BaseModel):
    url: str


class UnattendedBody(BaseModel):
    enabled: bool = True


cfg = Config()
manager = Manager(cfg)
hub = Hub()
RECENT: dict[str, deque] = {}


def _cookies_masked() -> dict[str, Any]:
    out = {}
    for key, value in {p: cfg.cookie(p) for p in ("douyin", "bilibili", "huya", "douyu")}.items():
        masked = ""
        if value:
            masked = f"{'*' * 8}{value[-4:]}" if len(value) > 8 else "*" * len(value)
        out[key] = {"masked": masked, "set": bool(value)}
    return out


async def _cookie_audit() -> None:
    """启动时把四份 Cookie 全查一遍，结果写进运行日志（各平台只记一条）。"""
    await asyncio.sleep(1.2)
    labels = {"douyin": "抖音", "bilibili": "B站", "huya": "虎牙", "douyu": "斗鱼"}
    for platform, label in labels.items():
        raw = cfg.cookie(platform)
        if not raw:
            log("INFO", f"Cookie 启动校验 · {label}：未设置（游客模式）")
            continue
        try:
            res = await cookie_check.check_cookie(platform, raw, "")
        except Exception as exc:  # noqa: BLE001 - 校验失败不影响启动
            log("WARN", f"Cookie 启动校验 · {label}：检测失败 {type(exc).__name__}: {exc}")
            continue
        message = res.get("message") or ""
        if res.get("state") == "login" or (res.get("state") == "likely" and platform in ("huya", "douyu")):
            log("SUCCESS", f"Cookie 启动校验 · {label}：{message}")
        elif res.get("ok") is False:
            log("WARN", f"Cookie 启动校验 · {label}：{message}")
        else:
            log("INFO", f"Cookie 启动校验 · {label}：{message}")


async def _apply_startup() -> None:
    hub.loop = asyncio.get_running_loop()
    manager.subscribe(_on_manager_event)
    # 设置里自定义的 ffmpeg 路径要先应用，后面的启动自检/录制才会用它
    try:
        cfg.apply_ffmpeg_path()
    except Exception as exc:  # 配置里那条路径写坏了也不能拖垮启动
        log("WARN", f"自定义 ffmpeg 路径不可用，改用内置：{exc}")
    # 解析并发上限（设置里的「同时解析线程数」）：启动时就要生效，
    # 不能等用户点一次「保存设置」才应用（否则一直用 resolver 里的默认 6）。
    try:
        resolver.set_concurrency(cfg.concurrent())
    except Exception as exc:  # 配置写坏了也不能拖垮启动
        log("WARN", f"解析并发上限设置无效，按默认值运行：{exc}")
    log("INFO", f"LiveRec 控制台 v{APP_VERSION} 已启动 · ffmpeg={paths.ffmpeg_exe()}")
    try:
        await manager.resume_unattended()
    except Exception as exc:  # 自动恢复失败不能拖垮启动
        log("WARN", f"无人值守自动恢复失败：{exc}")
    # 启动自检放在后台，别拖慢窗口打开
    asyncio.create_task(_cookie_audit())


def _on_manager_event(event: dict[str, Any]) -> None:
    """manager 的同步事件 → 记录最近弹幕 → 推给 WebSocket。"""
    if event.get("type") == "danmaku":
        evt = event.get("event") or {}
        rid = str(evt.get("roomId") or "")
        # 「进入直播间」量太大（斗鱼一场能上千条），只计数、不进实时抽屉；
        # JSONL 里仍然是全量，方便后面做数据分析。
        if rid and evt.get("type") != "enter":
            RECENT.setdefault(rid, deque(maxlen=80)).append({
                "type": evt.get("type"),
                "nickname": evt.get("nickname"),
                "content": evt.get("content"),
                "giftName": evt.get("gift_name"),
                "giftCount": evt.get("gift_count"),
                "offsetMs": evt.get("offset_ms"),
            })
        if evt.get("type") == "enter":
            return  # 进房流水不推前端，条数由每秒 tick 更新
    hub.push(event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _apply_startup()
    ticker = asyncio.create_task(_ticker())
    try:
        yield
    finally:
        ticker.cancel()
        await manager.shutdown()


async def _ticker() -> None:
    """每秒推送一次运行态（时长/体积在涨，但状态事件不一定触发）。"""
    while True:
        try:
            await asyncio.sleep(1)
            if not hub.clients:
                continue
            rooms = []
            for snap in manager.snapshot():
                rooms.append({
                    "id": snap.get("id"),
                    "status": snap.get("status"),
                    "elapsed": snap.get("elapsed"),
                    "size": snap.get("size"),
                    "sizeText": snap.get("sizeText"),
                    "bitrate": snap.get("bitrate"),
                    "resolution": snap.get("resolution"),
                    "segments": snap.get("segments"),
                    "currentFile": snap.get("currentFile"),
                    "danmakuCount": snap.get("danmakuCount"),
                    "danmakuStatus": snap.get("danmakuStatus"),
                    "qualityNote": snap.get("qualityNote"),
                    "qualityActual": snap.get("qualityActual"),
                    "note": snap.get("note"),
                    "error": snap.get("error"),
                    "anchor": snap.get("anchor"),
                    "title": snap.get("title"),
                    "nextCheck": snap.get("nextCheck"),
                    "checkInterval": snap.get("checkInterval"),
                    "unattended": snap.get("unattended"),
                })
            await hub.broadcast({"type": "tick", "stats": manager.stats(), "rooms": rooms})
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)


app = FastAPI(title="LiveRec 控制台", version=APP_VERSION, lifespan=lifespan)

WEB_DIR = paths.web_dir()
if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
async def index():
    page = os.path.join(WEB_DIR, "index.html")
    if not os.path.exists(page):
        return JSONResponse({"error": "web/index.html 缺失"}, status_code=500)
    return FileResponse(page)


def _icon_path() -> str:
    """和 exe 同一个图标（窗口标题栏 / 任务栏 / 浏览器标签都用它）。"""
    for candidate in (os.path.join(paths.app_root(), "bin", "liverec.ico"),
                      os.path.join(paths.bundle_root(), "bin", "liverec.ico")):
        if os.path.exists(candidate):
            return candidate
    return ""


@app.get("/favicon.ico")
async def favicon():
    icon = _icon_path()
    if not icon:
        raise HTTPException(status_code=404, detail="icon missing")
    return FileResponse(icon, media_type="image/x-icon")


@app.get("/static/liverec.ico")
async def favicon_static():
    return await favicon()


@app.get("/api/state")
async def api_state():
    return {
        "version": APP_VERSION,
        "appRoot": paths.app_root(),
        "rooms": manager.snapshot(),
        "stats": manager.stats(),
        "settings": cfg.to_dict(),
        "unattended": manager.unattended,
        "checkInterval": cfg.check_interval(),
        "cookies": _cookies_masked(),
        "logs": LOG.tail(120),
        "logPath": FILE_SINK.current_path(),
        "ffmpeg": paths.ffmpeg_exe(),
    }


@app.post("/api/preview")
async def api_preview(body: PreviewBody):
    url = (body.url or "").strip()
    platform = resolver.detect_platform(url)
    if platform == "unknown":
        return {"ok": False, "platform": platform, "error": "无法识别的平台链接"}
    info = await resolver.resolve(url, cfg.quality(), cfg.cookie(platform), "")
    return {
        "ok": not info.get("error"),
        "platform": platform,
        "label": resolver.PLATFORMS.get(platform, {}).get("label", platform),
        "anchor": info.get("anchor_name") or "",
        "title": info.get("title") or "",
        "isLive": bool(info.get("is_live")),
        "error": info.get("error") or "",
    }


@app.get("/api/qualities")
async def api_qualities(url: str = ""):
    """按链接查这个直播间实际能选的画质（添加/编辑直播间的下拉用）。"""
    link = (url or "").strip()
    if not link:
        return {"ok": False, "error": "缺少直播间链接"}
    platform = resolver.detect_platform(link)
    result = await resolver.fetch_qualities(link, cfg.cookie(platform), "")
    if result.get("ok"):
        result["label"] = resolver.PLATFORMS.get(platform, {}).get("label", platform)
        log("INFO", f"{result['label']} 可选画质：" + "、".join(q["label"] for q in result["qualities"]))
    return result


@app.post("/api/rooms")
async def api_add_room(body: StartBody):
    try:
        room = manager.add(body.url, body.quality, body.danmaku, body.anchor,
                           split_mode=body.splitMode or "default",
                           split_seconds=body.splitSeconds or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.start:
        await manager.start_room(room["id"])
    return {"ok": True, "room": room, "rooms": manager.snapshot()}


@app.patch("/api/rooms/{rid}")
async def api_patch_room(rid: str, body: PatchBody):
    before_meta = manager.meta.get(rid) or {}
    before_url = str(before_meta.get("url") or "")
    before_quality = str(before_meta.get("quality") or "")
    before_danmaku = bool(before_meta.get("danmaku"))
    before_split = (str(before_meta.get("splitMode") or "default"),
                    int(before_meta.get("splitSeconds") or 0))
    payload = body.model_dump(exclude_none=True)
    unattended = payload.pop("unattended", None)
    try:
        room = manager.update(rid, payload) if payload else (manager.meta.get(rid) or {})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="房间不存在") from exc
    if unattended is not None:
        # 单卡片无人值守：落盘 + 开启时立刻拉起（不走 update，避免只改字段不启任务）
        result = await manager.set_room_unattended(rid, bool(unattended))
        snap = next((r for r in manager.snapshot() if r.get("id") == rid), None)
        room = snap or {**(manager.meta.get(rid) or {}), "unattended": result["unattended"]}

    new_url = str(body.url or "").strip()
    url_changed = bool(new_url and before_url and new_url != before_url)
    # 画质/弹幕开关也必须重建任务，否则正在录制的任务还在用旧的画质与弹幕设置，
    # 表现就是「改了画质但录出来还是原来那档」。
    quality_changed = body.quality is not None and str(body.quality) != before_quality
    danmaku_changed = body.danmaku is not None and bool(body.danmaku) != before_danmaku
    # 分段策略改了也要重建任务，否则正在录的还是按旧的分段时长切
    split_changed = (
        (body.splitMode is not None and str(body.splitMode) != before_split[0])
        or (body.splitSeconds is not None and int(body.splitSeconds) != before_split[1])
    )
    if url_changed:
        # 地址变了：清掉旧主播名，重建任务，让新地址真正生效
        manager.meta[rid]["anchor"] = ""
        manager.save()

    task = manager.tasks.get(rid)
    was_running = bool(task is not None and getattr(task, "is_running", lambda: False)())
    if (url_changed or quality_changed or danmaku_changed or split_changed) and was_running:
        room = await manager.restart_room(rid)
    return {"ok": True, "room": room}


@app.delete("/api/rooms/{rid}")
async def api_delete_room(rid: str):
    if rid not in manager.meta:
        raise HTTPException(status_code=404, detail="房间不存在")
    await manager.remove(rid)
    return {"ok": True, "rooms": manager.snapshot()}


@app.post("/api/rooms/{rid}/start")
async def api_start(rid: str):
    if rid not in manager.meta:
        raise HTTPException(status_code=404, detail="房间不存在")
    return {"ok": True, "room": await manager.start_room(rid)}


@app.post("/api/rooms/{rid}/stop")
async def api_stop(rid: str):
    return {"ok": True, "room": await manager.stop_room(rid)}


@app.post("/api/rooms/{rid}/retry")
async def api_retry(rid: str):
    return {"ok": True, "room": await manager.retry_room(rid)}


@app.post("/api/all/start")
async def api_all_start():
    count = await manager.start_all()
    return {"ok": True, "count": count}


@app.post("/api/all/stop")
async def api_all_stop():
    count = await manager.stop_all()
    return {"ok": True, "count": count}


@app.post("/api/unattended")
async def api_unattended(body: UnattendedBody):
    result = await manager.set_unattended(bool(body.enabled))
    await hub.broadcast({"type": "unattended", "unattended": result["unattended"],
                         "checkInterval": cfg.check_interval()})
    await hub.broadcast({"type": "rooms", "rooms": manager.snapshot()})
    return result


@app.get("/api/settings")
async def api_get_settings():
    return {"settings": cfg.to_dict(), "cookies": _cookies_masked()}


@app.post("/api/settings")
async def api_set_settings(body: SettingsBody):
    data = body.model_dump(exclude_none=True)
    cookies = data.pop("cookies", None)
    # ffmpeg 路径要先校验：填了但文件不存在时直接 400，别让用户以为换成功了
    wanted_ffmpeg = (data.get("ffmpegPath") or "").strip().strip('"').strip("'")
    if wanted_ffmpeg and not os.path.isfile(wanted_ffmpeg):
        raise HTTPException(status_code=400,
                            detail=f"找不到这个 ffmpeg：{wanted_ffmpeg}（请填 ffmpeg.exe 的完整路径）")
    # 打码值原样回传时不覆盖
    cfg.apply_dict(data)
    if cookies:
        clean = {k: v for k, v in cookies.items() if v and "*" * 8 not in v}
        if clean:
            cfg.apply_dict({"cookies": clean})
    resolver.set_concurrency(cfg.concurrent())
    if wanted_ffmpeg:
        log("INFO", f"ffmpeg 路径已切换：{paths.ffmpeg_exe()}")
    elif "ffmpegPath" in data:
        log("INFO", f"ffmpeg 已改回内置：{paths.ffmpeg_exe()}")
    log("INFO", "全局设置已保存")
    return {"ok": True, "settings": cfg.to_dict()}


class CookieCheckBody(BaseModel):
    platform: str
    cookie: Optional[str] = None


@app.post("/api/cookie-check")
async def api_cookie_check(body: CookieCheckBody):
    """检测某个平台的 Cookie 是否仍然有效。

    未传 ``cookie`` 时用配置里已保存的那份；传了则只测传进来的这份（不写盘）。
    """
    platform = (body.platform or "").strip().lower()
    raw = (body.cookie or "").strip()
    if not raw:
        raw = cfg.cookie(platform)
    if "*" * 8 in raw:  # 前端把打码串回传了，说明用户没改过
        raw = cfg.cookie(platform)
    result = await cookie_check.check_cookie(platform, raw, "")
    label = result.get("label") or platform
    if result.get("state") == "login" or (result.get("state") == "likely" and platform in ("huya", "douyu")):
        log("SUCCESS", f"{label} Cookie 检测：{result.get('message')}")
    elif result.get("ok") is False:
        log("WARN", f"{label} Cookie 检测：{result.get('message')}")
    else:
        log("INFO", f"{label} Cookie 检测：{result.get('message')}")
    return result



@app.get("/api/logs")
async def api_logs(after: int = 0, limit: int = 200):
    return {"logs": LOG.tail(limit, after), "path": FILE_SINK.current_path()}


@app.get("/api/danmaku/{rid}")
async def api_danmaku(rid: str, limit: int = 40):
    items = list(RECENT.get(rid) or [])
    return {"items": items[-limit:]}


@app.get("/api/danmaku/sessions")
async def api_danmaku_sessions(limit: int = 60):
    from .danmaku import STORE

    return {"sessions": STORE.sessions(limit)}


@app.post("/api/open-folder")
async def api_open_folder(body: PathBody):
    target = body.path or paths.default_download_dir()
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="目录不存在")
    try:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            import subprocess

            subprocess.Popen(["xdg-open", target])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"无法打开目录：{exc}") from exc
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    try:
        await ws.send_json({
            "type": "hello",
            "version": APP_VERSION,
            "rooms": manager.snapshot(),
            "stats": manager.stats(),
            "settings": cfg.to_dict(),
            "unattended": manager.unattended,
            "checkInterval": cfg.check_interval(),
            "cookies": _cookies_masked(),
            "logs": LOG.tail(120),
            "logPath": FILE_SINK.current_path(),
        })
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.clients.discard(ws)
