"""单个直播间的录制任务：解析流地址 → 拉起 ffmpeg → 解析进度 → 断流重连。"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import threading
import time
from datetime import datetime
from typing import Any, Callable

from . import concat, paths, resolver
from .hlsproxy import HlsProxy
from .logbook import log

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Windows 下 GUI 程序拉起 ffmpeg 会弹一个黑窗口（ffmpeg 是控制台程序），
# CREATE_NO_WINDOW 让它完全在后台跑，不占桌面也不抢焦点。
_NO_WINDOW_FLAGS = 0x08000000 if os.name == "nt" else 0
PLATFORM_LABEL = {"douyin": "抖音", "bilibili": "哔哩哔哩", "huya": "虎牙", "douyu": "斗鱼", "custom": "自定义"}
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_RES_RE = re.compile(r"Stream #\d+:\d+.*?(\d{2,5})x(\d{2,5})")
_PROGRESS_KEYS = ("out_time_us", "total_size", "bitrate", "speed", "progress")

# 录制过程中不会因为「多久没有新数据」而停止录制：
# 只有 ffmpeg 自己退出才会收尾（虎牙还会按签名过期分片续录）。


def safe_name(text: str, fallback: str = "未知主播") -> str:
    out = _BAD_CHARS.sub("_", (text or "").strip()).strip(" ._")
    return (out[:60] or fallback)


def _fmt_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.2f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.2f} TB"


def _start_hls_proxy(url: str, platform: str, room_url: str = "",
                     cookie: str = "", proxy_addr: str = "") -> HlsProxy | None:
    """m3u8 地址才需要本地中转（把分片的相对 URI 改写成带签名的绝对地址）。"""
    if ".m3u8" not in url.lower():
        return None
    headers = {"User-Agent": UA}
    if platform == "bilibili":
        headers["Referer"] = "https://live.bilibili.com/"
        headers["Origin"] = "https://live.bilibili.com"
    elif platform == "huya":
        headers["Referer"] = "https://m.huya.com/"
        headers["Origin"] = "https://m.huya.com"
    elif platform == "douyin":
        headers["Referer"] = "https://live.douyin.com/"
    try:
        # 虎牙的 wsSecret 是「一次一签」的：播放列表每刷新一次都要重算，否则第二次就 403
        signer = None
        if platform == "huya":
            from . import huya as huya_mod  # noqa: PLC0415 - 虎牙实现在独立模块

            def signer(candidate: str) -> str:  # noqa: F811
                # 纯本地重算 wsSecret（微秒级）：stream-rec 也是这么做的。
                # 刻意不在刷新里请求接口——一次几百毫秒会把 ffmpeg 的播放列表请求拖到超时；
                # wsTime 真过期时由录制端换段重新 resolve 拿新的。
                return huya_mod.resign(candidate) or candidate

        proxy = HlsProxy(url, headers, signer=signer)
        proxy.start()
        log("INFO", f"HLS 中转已启动 → {proxy.local_url}")
        return proxy
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"HLS 中转启动失败（改用原始地址）：{exc}")
        return None


class RecordTask:
    """一个直播间的完整状态与录制循环。"""

    def __init__(self, room_id: str, url: str, quality: str, cfg,
                 on_update: Callable[[dict], None], danmaku: bool = True):
        self.room_id = room_id
        self.url = url
        self.quality = quality
        self.quality_note = ""
        self.quality_actual = ""
        self.cfg = cfg
        self.on_update = on_update
        self.danmaku_enabled = danmaku

        self.status = "idle"          # idle|waiting|recording|stopped|error
        self.anchor = ""
        self.title = ""
        self.platform = resolver.detect_platform(url)
        # 虎牙会在 ffmpeg 自己退出（签名过期/CDN 掐断）时按分片续录。
        # 虎牙分片续录的连续失败计数：连续几段都拿不到数据时要退避，别把 CDN 当刷量
        self._huya_streak = 0
        self._no_data = False
        self._no_data_streak = 0
        self._chunk_index = 0
        self._parts: list[str] = []
        self.error = ""
        self.started_at = 0.0
        self.elapsed = 0.0
        self.size = 0
        self.size_text = "0 B"
        self.bitrate = ""
        self.resolution = ""
        self.segments = 0
        self.current_file = ""
        self.out_dir = ""
        self.danmaku_count = 0
        self.danmaku_status = "off"
        self.danmaku_note = ""
        self.session_id = ""
        self.next_check_at = 0.0      # 下一次开播检测的时间戳（秒）
        self.note = ""                # 给卡片看的补充说明（如「60 秒无数据已停止」）

        self._stop = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._loop_task: asyncio.Task | None = None
        self._dm_session = None
        self._last_push = 0.0
        self._files_prefix = ""
        self._live_since = 0.0
        self._last_error_lines: list[str] = []
        self.danmaku_forward: Callable[[dict], None] | None = None
        # 最近一次文件体积增长的时刻。只用来判断「虎牙这一段是不是卡死了要换新地址」，
        # 不会因为超时就停止录制（停止是用户明确要求去掉的）。
        self._last_growth = 0.0
        self._quality_checked = False  # 本场是否已经按房间真实档位校正过画质

    # ---------- 对外快照 ----------
    def snapshot(self) -> dict[str, Any]:
        if self.status == "recording" and self._live_since:
            self.elapsed = max(self.elapsed, time.time() - self._live_since)
        return {
            "id": self.room_id,
            "url": self.url,
            "platform": self.platform,
            "platformLabel": PLATFORM_LABEL.get(self.platform, self.platform),
            "quality": self.quality,
            "qualityNote": self.quality_note,
            "qualityActual": self.quality_actual,
            "anchor": self.anchor,
            "title": self.title,
            "status": self.status,
            "error": self.error,
            "elapsed": int(self.elapsed),
            "size": int(self.size),
            "sizeText": self.size_text,
            "bitrate": self.bitrate,
            "resolution": self.resolution,
            "segments": self.segments,
            "currentFile": self.current_file,
            "outDir": self.out_dir,
            "startedAt": int(self.started_at * 1000) if self.started_at else 0,
            "danmakuCount": self.danmaku_count,
            "danmakuStatus": self.danmaku_status,
            "danmakuNote": self.danmaku_note,
            "sessionId": self.session_id,
            "nextCheck": int(self.next_check_at * 1000) if self.next_check_at else 0,
            "checkInterval": self.cfg.check_interval(),
            "note": self.note,
        }

    def push(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_push < 0.8:
            return
        self._last_push = now
        try:
            self.on_update(self.snapshot())
        except Exception:
            pass

    # ---------- 生命周期 ----------
    def is_running(self) -> bool:
        """录制循环还在跑吗（任务被取消/已结束就算没在跑）。"""
        return self._loop_task is not None and not self._loop_task.done()

    def start(self) -> None:
        if self.is_running():
            return
        # 上一次停止会留下 size / 分段 / 文件名等运行时状态，
        # 不重置的话「停止 → 再开始」的卡片会显示上一场的数字，看起来像没重新录。
        self._stop.clear()
        self.error = ""
        self.note = ""
        self.quality_note = ""
        self.quality_actual = ""
        self._quality_checked = False
        self._base_bytes = 0
        self._no_data = False
        self._no_data_streak = 0
        self._huya_streak = 0
        self._chunk_index = 0
        self._parts: list[str] = []
        self.size = 0
        self.size_text = "0 B"
        self.bitrate = ""
        self.resolution = ""
        self.segments = 0
        self.current_file = ""
        self.record_url = ""
        self.started_at = 0.0
        self.elapsed = 0.0
        self.next_check_at = 0.0
        # 弹幕状态也得跟着清零：否则新一场的计数会从上一场的数字开始跳，
        # 卡片上的「弹幕 N 条」就会一会儿大一会儿小。
        self.danmaku_count = 0
        self.danmaku_status = "off"
        self.danmaku_note = ""
        self.session_id = ""
        self.status = "waiting"
        self.push(force=True)
        self._loop_task = asyncio.create_task(self._guarded_run())

    async def stop(self, status: str = "stopped") -> None:
        was_recording = self.status == "recording"
        self._stop.set()
        await self._kill_proc()
        await self._stop_danmaku()
        task = self._loop_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self._loop_task = None
        self.status = status
        self.next_check_at = 0.0
        # 停止按钮/删卡片时也要把这次直播的分段拼起来，否则用户只拿到一堆零散文件。
        # 这里是同步等待合并完成：用户点完「停止」就应该在目录里看到完整文件，
        # 而不是过一会儿才出现（删卡片时可能连进程都已经被关掉了）。
        self._refresh_disk()
        self._remember_part()
        try:
            await self._finish_session(blocking=True)
        except Exception as exc:  # noqa: BLE001
            # 拼接/转封装是收尾动作，失败了也不能让「停止录制」整体失败：
            # 分段文件都还在磁盘上，用户仍能拿到内容，日志里写清原因即可。
            log("WARN", f"{self.anchor or self.url} 收尾合并失败（分段已保留）：{type(exc).__name__}: {exc}")
        self.push(force=True)
        if was_recording:
            # 停录默认是「静默」的，出问题时看不出录音是谁停的；记一条便于排查。
            log("INFO", f"{self.anchor or self.url} 录制任务已停止（{status}），本段 {self.size_text}")

    async def _kill_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=6)
            except asyncio.TimeoutError:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            pass

    # ---------- 主循环 ----------
    async def _guarded_run(self) -> None:
        """保护壳：录制循环里出任何意外异常都不要让房间永久卡在「录制中」。

        `_run()` 正常返回只发生在停止/取消时；异常时记录日志、把状态摆回
        「等待开播」并隔一会儿重来，避免卡片显示录制中但实际早已没有输出。
        """
        while not self._stop.is_set():
            try:
                await self._run()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log(
                    "ERROR",
                    f"{self.anchor or self.url} 录制循环异常：{type(exc).__name__}: {exc}",
                )
                self.status = "waiting"
                self.error = ""
                self.note = ""
                self.next_check_at = time.time() + 15
                self.push(force=True)
                await self._wait(15)

    async def _run(self) -> None:
        cfg = self.cfg
        while not self._stop.is_set():
            try:
                # 解析卡死会让整张卡永远停在「等待开播」且毫无日志，这里硬性限时：
                # 虎牙的 CDN 偶尔回一条不结束的响应，线程里的 requests 会一直挂着。
                if self.platform == "huya":
                    # 虎牙的 wsSecret 只有 20–30 秒有效期，分片续录必须重新拿地址，
                    # 否则会一直复用缓存里那条已经过期的 URL（表现就是「录着录着停住」）。
                    from . import huya as huya_mod  # noqa: PLC0415

                    huya_mod.drop_cache(self.url)
                    try:
                        from . import huya_ytdlp  # noqa: PLC0415

                        huya_ytdlp.clear(self.url)
                    except Exception:  # noqa: BLE001
                        pass
                info = await asyncio.wait_for(
                    resolver.resolve(
                        self.url,
                        self.quality,
                        cookie=cfg.cookie(self.platform),
                        proxy=cfg.proxy_for(self.platform),
                        probe=self.platform != "huya",
                    ),
                    timeout=75,
                )
            except asyncio.TimeoutError:
                self._to_error("解析超时（75 秒没有结果），稍后重试")
                await self._wait(self.cfg.check_interval())
                continue
            except Exception as exc:
                if getattr(exc, "user_facing", False):
                    self._to_error(str(exc))
                else:
                    self._to_error(f"解析异常：{type(exc).__name__}: {exc}")
                await self._wait(self.cfg.check_interval())
                continue

            if info.get("error"):
                self._to_error(str(info["error"]))
                await self._wait(self.cfg.check_interval())
                continue

            if info.get("anchor_name"):
                self.anchor = info["anchor_name"]
            if info.get("title"):
                self.title = info["title"]
            note = str(info.get("quality_note") or "")
            if note and note != self.quality_note:
                log("WARN", f"{self.anchor or self.url} {note}")
            self.quality_note = note
            self.quality_actual = str(info.get("quality_actual") or "")

            if not info.get("is_live") or not info.get("record_url"):
                self.status = "waiting"
                self.error = ""
                interval = cfg.check_interval()
                self.next_check_at = time.time() + interval
                self.push(force=True)
                log("INFO", f"{self.anchor or self.url} 未开播，{interval}s 后再次检测")
                await self._wait(interval)
                continue

            # 房间的档位会随主播推流变化；自动/无人值守拉起时不会走「添加直播间」的画质探查，
            # 所以开录前用房间当前真实档位校正一次，避免卡片写着旧档位、实际录的是另一档。
            if not self._quality_checked:
                self._quality_checked = True
                try:
                    adj = await resolver.adjust_quality(
                        self.url, self.quality,
                        cookie=cfg.cookie(self.platform),
                        proxy=cfg.proxy_for(self.platform),
                    )
                except Exception as exc:  # noqa: BLE001
                    log("WARN", f"{self.anchor or self.url} 画质校正失败（按原档位继续）：{exc}")
                    adj = {}
                effective = str(adj.get("effective") or "")
                if effective and effective != self.quality:
                    message = str(adj.get("note") or "")
                    # 不管平台叫法是否不同，都把「实际要用的档位」写回 self.quality：
                    # snapshot() 里的 quality 就是卡片上显示的档位，必须与真正解析用的档位一致。
                    self.quality = effective
                    self.quality_actual = effective
                    if message:
                        # 真的换了档（房间没这个档位才降级）才在卡片上标「降级」原因；
                        # 只是平台叫法不同（原画 vs 原画（蓝光30M））就静默用真实档位去解析。
                        self.quality_note = message
                        log("INFO", f"{self.anchor or self.url} {message}")
                    self.push(force=True)
                    try:
                        changed = await resolver.resolve(
                            self.url, effective,
                            cookie=cfg.cookie(self.platform),
                            proxy=cfg.proxy_for(self.platform),
                        )
                    except Exception as exc:  # noqa: BLE001
                        log("WARN", f"{self.anchor or self.url} 按「{effective}」重新解析失败：{exc}")
                        changed = {}
                    if changed.get("is_live") and changed.get("record_url"):
                        info = changed

            await self._record_once(info["record_url"], info.get("lines") or [])
            if self._stop.is_set():
                return
            if self._no_data and self.size <= 0 and self._no_data_streak >= 2:
                # 线路一个字节都没出：不报错、不换线，直接回等待开播等下一轮检测。
                self._no_data = False
                self._no_data_streak = 0
                interval = cfg.check_interval()
                self.status = "waiting"
                self.error = ""
                self.next_check_at = time.time() + interval
                self.push(force=True)
                log("INFO", f"{self.anchor or self.url} 回到等待开播，{interval}s 后重新检测")
                await self._wait(interval)
                continue
            if self.platform == "huya" and self.size > self._base_bytes:
                # 虎牙签名几十秒就过期，ffmpeg 每次都会在过期点正常退出（rc 0）。
                # 只要这一段真的录到了东西，就按分片边界处理：记下已录字节，重新解析
                # 拿新签名接着录，卡片保持「录制中」；低于阈值则交给下面的失败分支退避。
                chunk = self.size - self._base_bytes
                if chunk >= 300 * 1024:
                    self._huya_streak = 0
                    self._chunk_index += 1
                    self._remember_part()
                    self._base_bytes = self.size
                    self.status = "recording"
                    self.error = ""
                    self.note = "虎牙签名到期自动续录（每段单独成文件）"
                    self.push(force=True)
                    log(
                        "INFO",
                        f"{self.anchor or self.url} 第 {self._chunk_index} 段录到 "
                        f"{chunk / 1048576:.2f} MB（累计 {self.size_text}），换新签名续录",
                    )
                    await self._wait(1.5)
                    continue
                # 这一段什么都没录到（CDN 拒绝/中转失败）：把 0 字节的空壳清掉，
                # 免得目录里堆一堆 0KB 的 .ts，看着像「录了但没数据」。
                self._sweep_empty_files()
                # 已经录到东西、只是这一小段空转：立刻换新签名再试几次，
                # 别一断就回到 90 秒的等待（用户会觉得「录一半停了」）。
                self._no_data = False
                self._no_data_streak = 0
                self._huya_streak += 1
                if self._huya_streak <= 3:
                    log("INFO", f"{self.anchor or self.url} 这一段没读到数据，换新签名重试"
                                f"（{self._huya_streak}/3）")
                    await self._wait(2.0)
                    continue
            # 录制结束（主播下播/断流）：稍等再回到轮询
            await self._stop_danmaku()
            self._remember_part()
            await self._finish_session()
            log(
                "INFO",
                f"{self.anchor or self.url} 本段录制结束（{self.size_text}），"
                "15 秒后重新检测开播状态",
            )
            self.status = "waiting"
            self.note = ""
            self.next_check_at = time.time() + 15
            self.push(force=True)
            await self._wait(15)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _to_error(self, message: str) -> None:
        self.status = "error"
        self.error = message[:300]
        self.push(force=True)
        log("ERROR", f"{self.anchor or self.url} 解析失败：{self.error}")

    # ---------- 单次录制 ----------
    async def _record_once(self, stream_url: str, lines: list[str] | None = None) -> None:
        cfg = self.cfg
        self._no_data = False
        self._no_data_streak = 0
        fmt = cfg.save_format()
        split = cfg.split_enabled()
        split_time = cfg.split_seconds()
        anchor = safe_name(self.anchor, self.platform)
        root = cfg.save_root()
        platform_label = PLATFORM_LABEL.get(self.platform, "其它")
        sub = [root, safe_name(platform_label)]
        if cfg.folder_by_author():
            sub.append(anchor)
        out_dir = os.path.join(*sub)
        os.makedirs(out_dir, exist_ok=True)
        self.out_dir = out_dir

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        title_part = ""
        if self.title:
            title_part = safe_name(self.title, "")[:30]
            title_part = f"_{title_part}" if title_part else ""
        prefix = f"{anchor}{title_part}_{stamp}"
        self._files_prefix = prefix
        ext = {"ts": "ts", "mkv": "mkv", "flv": "flv", "mp4": "mp4",
               "mp3": "mp3", "m4a": "m4a"}.get(fmt, "ts")
        chunk_fmt = fmt
        if self.platform == "huya" and ext in ("ts", "mkv", "mp4"):
            # 虎牙原生就是 FLV，分段直接用 FLV 落盘最稳（-c copy 不必重打 TS），
            # 等这场直播结束再拼接、按用户选的格式转一次。
            ext = "flv"
            chunk_fmt = "flv"
        # 只保留一条线路：虎牙的多线路里大部分是 403 的死线路，其它平台给的第一条
        # 就是正片线路。线路没数据就直接回等待开播，不再「换下一条试试」——
        # 那样只会让卡片看起来一直在录、实际什么都没录进去。
        candidates: list[str] = [stream_url]

        def make_pattern(index: int) -> str:
            # 每条候选线路用独立文件名：否则后一条线路重开会把前一条已录的数据覆盖掉，
            # 表现就是「录了一会儿文件变小/前面内容没了」。
            stem = prefix if index == 0 else f"{prefix}_{index + 1}"
            name = f"{stem}_%03d.{ext}" if split else f"{stem}.{ext}"
            return os.path.join(out_dir, name)

        out_pattern = make_pattern(0)

        self.started_at = time.time()
        self._live_since = time.time()
        self.elapsed = 0.0
        self.size = self._base_bytes
        self.size_text = _fmt_size(self.size)
        self.segments = 0
        self.current_file = os.path.basename(out_pattern.replace("%03d", "000"))
        self.status = "recording"
        self.error = ""
        self.next_check_at = 0.0
        self._last_error_lines = []
        self.note = ""
        self.push(force=True)
        log("SUCCESS", f"{anchor} 开始录制（{platform_label} · {self.quality} · {fmt.upper()}）→ {out_dir}")

        await self._start_danmaku(out_dir)

        healthy = False
        for index, candidate in enumerate(candidates):
            if self._stop.is_set():
                break
            # HLS 播放列表里常是相对 URI（B站 fMP4 尤其如此），ffmpeg 解析时会丢掉
            # playlist 上的签名 query 导致分片 403，这里过一层本地中转把它改写绝对。
            proxy = _start_hls_proxy(candidate, self.platform, room_url=self.url,
                                     cookie=self.cfg.cookie(self.platform),
                                     proxy_addr=self.cfg.proxy_for(self.platform))
            target = proxy.local_url if proxy else candidate
            out_pattern = make_pattern(index)
            self.current_file = os.path.basename(out_pattern.replace("%03d", "000"))
            cmd = self._build_cmd(target, out_pattern, chunk_fmt, split, split_time,
                                  allow_any_ext=bool(proxy))
            try:
                spawn_kwargs: dict[str, Any] = {}
                if _NO_WINDOW_FLAGS:
                    spawn_kwargs["creationflags"] = _NO_WINDOW_FLAGS
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    **spawn_kwargs,
                )
            except FileNotFoundError:
                self._to_error(f"找不到 ffmpeg：{paths.ffmpeg_exe()}")
                await self._stop_danmaku()
                return
            except Exception as exc:
                self._to_error(f"启动 ffmpeg 失败：{exc}")
                await self._stop_danmaku()
                return

            self._last_error_lines = []
            attempt_started = time.time()
            self._last_growth = time.time()
            last_beat = time.time()
            pump = asyncio.create_task(self._pump())
            while True:
                done, _ = await asyncio.wait({pump}, timeout=5)
                if done:
                    break
                # 每分钟留一条「还活着」的日志：出问题时能一眼看出是录着不涨还是彻底没动。
                if time.time() - last_beat >= 60 and self.size > 0:
                    last_beat = time.time()
                    log(
                        "INFO",
                        f"{self.anchor or self.url} 录制中 {self.size_text}"
                        f"（{self.resolution or '-'} {self.bitrate or '-'}，"
                        f"已录 {int(self.elapsed)} 秒）",
                    )
                # 一个字节都没出：这条线路就是死的（403/秒断/挂住），
                # 直接结束这一段，回到「等待开播」，不换线路、也不报错。
                first_wait = 45.0 if self.platform == "huya" else 20.0
                if self.size <= 0 and time.time() - attempt_started > first_wait:
                    self._no_data = True
                    # 连续两次都没出数据才回等待；只失败一次就先重试，别把整场打断。
                    self._no_data_streak += 1
                    log("WARN", f"{anchor} {int(first_wait)} 秒没有收到任何数据"
                                  f"（第 {self._no_data_streak} 次，连续 2 次才回到等待开播）")
                    if self._proc:
                        with contextlib.suppress(ProcessLookupError):
                            self._proc.terminate()
                    break
                # 虎牙专用：已经录到过数据、但这一段 20 秒没有任何增长。
                # 这不是「停录」，而是换一条新签名的地址接着录（每段单独落文件），
                # 否则 ffmpeg 会挂在那里不动、卡片一直显示「录制中」但体积不变。
                if (self.platform == "huya" and self.size > self._base_bytes
                        and time.time() - self._last_growth > 20.0):
                    log("WARN", f"{anchor} 这 20 秒没有再写入数据（虎牙签名到期），换新地址接着录")
                    if self._proc:
                        with contextlib.suppress(ProcessLookupError):
                            self._proc.terminate()
                    break
            if not pump.done():
                try:
                    await asyncio.wait_for(asyncio.shield(pump), timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pump.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pump
            code = self._proc.returncode if self._proc else None
            self._proc = None
            if proxy is not None:
                proxy.stop()
                proxy = None

            # 这一段新录到的字节（要减掉之前的累计，否则第二段一开始就「健康」）
            chunk_bytes = self.size - self._base_bytes
            # 这一段已经落盘（哪怕是被 CDN 掐断的），记下来供收尾时拼接
            if chunk_bytes > 0:
                self._remember_part()
            # 只有一条线路，录到了就继续（虎牙走分片续录），没录到就回等待开播。
            healthy = chunk_bytes > 512 * 1024 or (code in (0, None) and self.elapsed >= 20)
            if chunk_bytes <= 0 and not self._stop.is_set():
                self._no_data = True
                self._no_data_streak += 1
                if self._last_error_lines:
                    log("WARN", f"{anchor} 这一段没出数据，ffmpeg 最后一行："
                                  f"{self._last_error_lines[-1]}")
                self._discard_empty_files(out_dir, prefix)
            break

        if self.platform != "huya":
            # 虎牙是「分片续录」：一段签到期就重开一段，弹幕要跨段连着，
            # 否则卡片上的条数会随每段归零、看起来「一会大一会小」。
            await self._stop_danmaku()
        if self._stop.is_set():
            return
        if self._no_data and self.platform == "huya" and self.size > 0:
            # 虎牙：本段没有读到数据，但这一场已经录到东西了 —— 不算「线路不通」，
            # 交给 _run() 的续段逻辑立刻换新签名重试，卡片保持「录制中」。
            self._no_data = True
            self.status = "recording"
            self.note = "这一段没读到数据，正在换新签名重试"
        elif self._no_data and self._no_data_streak >= 2:
            # 连续两次都没出数据：不报错、不换线，回等待开播等下一轮检测。
            self.status = "waiting"
            self.error = ""
            self.note = "这条线路没有出数据，已回到等待开播（下播/风控/地址失效都会这样）"
            self.next_check_at = time.time() + self.cfg.check_interval()
            log("INFO", f"{anchor} 已回到等待开播，{self.cfg.check_interval()} 秒后重新检测")
        elif not healthy:
            tail = " | ".join(self._last_error_lines[-3:]) or "所有线路均无法拉流"
            self.status = "error"
            self.error = tail[:300]
            log("ERROR", f"{anchor} 录制中断：{self.error}")
        else:
            log("INFO", f"{anchor} 本次录制结束（{_fmt_size(self.size)}，{self.segments or 1} 个文件）")
        self.push(force=True)

    @staticmethod
    def _discard_empty_files(out_dir: str, prefix: str) -> None:
        """换线路重试时清掉上一次留下的 0 字节文件。"""
        try:
            for name in os.listdir(out_dir):
                if not name.startswith(prefix):
                    continue
                path = os.path.join(out_dir, name)
                if os.path.isfile(path) and os.path.getsize(path) == 0:
                    os.remove(path)
        except OSError:
            pass

    def _remember_part(self) -> None:
        """把刚录完的那一段记下来，等这场直播结束统一拼接成一个文件。"""
        name = self.current_file
        if not name:
            return
        path = os.path.join(self.out_dir, name)
        if not os.path.isfile(path):
            log("WARN", f"{self.anchor or self.url} 分段文件不存在，跳过拼接记录：{name}")
            return
        size = os.path.getsize(path)
        if size <= 0:
            log("WARN", f"{self.anchor or self.url} 分段是空文件，跳过拼接记录：{name}")
            return
        if path not in self._parts:
            self._parts.append(path)
            log("INFO", f"{self.anchor or self.url} 记下第 {len(self._parts)} 个分段 "
                        f"（{_fmt_size(size)}）：{name}")

    async def _finish_session(self, blocking: bool = False) -> None:
        """这场直播结束时，把虎牙的分段拼成一个完整文件。

        只有虎牙会「分段续录」，其它平台保持老行为：一个房间一个文件，
        中途换线留下的文件原样留着，不做任何拼接。

        blocking=True 时当场（在 executor 里）拼完再返回——用户点「停止录制」
        就是这条路，停止后马上能在目录里看到合好的完整文件。
        """
        # 只有这一场真的结束才合并。录制中途（虎牙签名到期续段）若合并，
        # 会删掉 ffmpeg 还在引用的分段，下一条续段就只剩
        # 「分段文件不存在，跳过拼接记录」，录制循环卡在收尾而不重开。
        if (not self._stop.is_set() and self._loop_task is not None
                and not self._loop_task.done() and self.status == "recording"):
            log("INFO", f"{self.anchor or self.url} 录制中，分段先留着，等这一场结束再合并")
            return
        parts = [p for p in self._parts if os.path.isfile(p)]
        self._parts = []
        if self.platform != "huya":
            return
        target_fmt = ""
        try:
            target_fmt = self.cfg.save_format()
        except Exception:  # noqa: BLE001
            target_fmt = ""
        if len(parts) < 2:
            if not parts:
                return
            # 只有一段也要收尾：虎牙中间文件是 FLV，按用户选的格式转一次，
            # 否则设置里选了 ts/mp4 却只拿到一个 .flv。
            if blocking:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, concat.convert_session, parts[0], target_fmt)
                log("INFO", f"{self.anchor or self.url} 单段录制已收尾（按 {target_fmt or '原格式'} 输出）")
            else:
                threading.Thread(
                    target=concat.convert_session,
                    args=(parts[0], target_fmt),
                    daemon=True,
                    name="convert",
                ).start()
            return
        first = os.path.basename(parts[0])
        stem = re.sub(r"(_\d+)+\.(ts|mkv|flv|mp4)$", "", first) or (self.anchor or "session")
        ext = "flv" if self.platform == "huya" else "ts"
        out = os.path.join(self.out_dir, f"{stem}_merged.{ext}")
        if blocking:
            loop = asyncio.get_running_loop()
            ok, message = await loop.run_in_executor(
                None, concat.merge_session, parts, out, False, target_fmt)
            log("INFO" if ok else "WARN",
                f"{self.anchor or self.url} 停止后已合并 {len(parts)} 个分段 → "
                f"{os.path.basename(out)}（{message or 'ok'}）")
        else:
            threading.Thread(
                target=concat.merge_session,
                args=(parts, out, False, target_fmt),
                daemon=True,
                name="concat",
            ).start()

    def _sweep_empty_files(self) -> None:
        """扫一遍本房间的输出目录，删掉 0 字节的空壳文件。

        虎牙「分片续录」每段都会新建一个文件，CDN 偶尔一两段直接拒绝，
        留着 0KB 的 .ts 会让人以为「录了却没数据」，也干扰体积统计。
        """
        out_dir = self.out_dir
        if not out_dir:
            return
        try:
            for name in os.listdir(out_dir):
                path = os.path.join(out_dir, name)
                if os.path.isfile(path) and name.endswith(".ts") and os.path.getsize(path) == 0:
                    os.remove(path)
        except OSError:
            pass

    def _build_cmd(self, stream_url: str, out_pattern: str, fmt: str,
                   split: bool, split_time: int, allow_any_ext: bool = False) -> list[str]:
        cmd = [
            paths.ffmpeg_exe(), "-hide_banner", "-loglevel", "info", "-nostats",
            "-progress", "pipe:2", "-y",
            "-rw_timeout", "15000000", "-user_agent", UA,
        ]
        # 直播 CDN 常见证书链异常（部分节点缺少中间证书 / 代理环境 MITM），
        # 对公开直播流的拉流不做证书校验，避免整场录不到。
        # 注意：-tls_verify 是 tls 协议私有选项，纯 http:// 的地址不能带，
        # 否则 ffmpeg 会报 "Option not found" 直接打不开输入（虎牙线路就是 http）。
        if stream_url.lower().startswith("https"):
            cmd += ["-tls_verify", "0"]
        is_hls = ".m3u8" in stream_url.lower()
        cmd += [
            "-protocol_whitelist", "rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy",
            "-thread_queue_size", "1024", "-analyzeduration", "20000000",
            "-probesize", "10000000", "-fflags", "+discardcorrupt",
        ]
        if is_hls:
            # HLS 播放列表本身会被反复重取，reconnect_* 会让 http 协议层把「列表读完」
            # 当成 EOF 反复重连（日志里刷 "Will reconnect at N ... End of file"），
            # 所以 HLS 输入不加这几个参数，重连交给 hls demuxer。
            cmd += ["-http_persistent", "0"]
        else:
            cmd += [
                "-reconnect_delay_max", "60", "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_at_eof", "1",
            ]
        if allow_any_ext:
            # 走本地 HLS 中转时，分片地址会带上原始扩展名，但 ffmpeg 的 hls 白名单
            # 只认内置列表，这里放开，否则会报 "is not in allowed_segment_extensions"。
            cmd += ["-allowed_extensions", "ALL"]
        # 部分平台 CDN 校验 Referer，缺失会直接 I/O error。
        if self.platform == "bilibili":
            cmd += ["-headers",
                    "Referer: https://live.bilibili.com/\r\nOrigin: https://live.bilibili.com\r\n"]
        elif self.platform == "douyin":
            cmd += ["-headers", "Referer: https://live.douyin.com/\r\n"]
        elif self.platform == "huya":
            cmd += ["-headers", "Referer: https://m.huya.com/\r\nOrigin: https://m.huya.com\r\n"]
            # 而不是挂着等超时；skip_frame/drop_changed 只在解码层生效，不影响 -c copy。
        cmd += [
            "-i", stream_url,
            "-bufsize", "8000k", "-max_muxing_queue_size", "1024",
        ]
        seg = ["-f", "segment", "-segment_time", str(split_time), "-reset_timestamps", "1"]
        if fmt == "mp3":
            cmd += ["-map", "0:a", "-c:a", "libmp3lame", "-b:a", "192k"]
            cmd += seg + ["-segment_format", "mp3"] if split else ["-f", "mp3"]
        elif fmt == "m4a":
            cmd += ["-map", "0:a", "-c:a", "aac", "-b:a", "192k"]
            cmd += seg + ["-segment_format", "ipod"] if split else ["-f", "ipod"]
        elif fmt == "mkv":
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-flags", "global_header"]
            cmd += seg + ["-segment_format", "matroska"] if split else ["-f", "matroska"]
        elif fmt == "mp4":
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-movflags", "+frag_keyframe+empty_moov"]
            cmd += seg + ["-segment_format", "mp4"] if split else ["-f", "mp4"]
        elif fmt == "flv":
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"]
            cmd += seg + ["-segment_format", "flv"] if split else ["-f", "flv"]
        else:  # ts
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"]
            cmd += seg + ["-segment_format", "mpegts"] if split else ["-f", "mpegts"]
        cmd.append(out_pattern)
        return cmd

    async def _pump(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        while True:
            try:
                line = await stderr.readline()
            except (asyncio.CancelledError, Exception):
                break
            if not line:
                break
            text = line.decode("utf-8", "ignore").strip()
            if not text:
                continue
            if "=" in text and text.split("=", 1)[0] in _PROGRESS_KEYS:
                key, val = text.split("=", 1)
                self._on_progress(key, val)
                continue
            if "x" in text and "Stream #" in text:
                m = _RES_RE.search(text)
                if m:
                    self.resolution = f"{m.group(1)}x{m.group(2)}"
            if any(k in text for k in ("error", "Error", "ERROR", "failed", "Invalid", "Connection")):
                self._last_error_lines.append(text[:200])
                if len(self._last_error_lines) > 8:
                    self._last_error_lines.pop(0)
        try:
            await self._proc.wait()
        except Exception:
            pass

    def _on_progress(self, key: str, val: str) -> None:
        if key == "out_time_us":
            try:
                self.elapsed = max(self.elapsed, int(val) / 1_000_000)
            except ValueError:
                pass
        elif key == "total_size":
            try:
                new_size = int(val)
            except ValueError:
                return
            if self._base_bytes + new_size != self.size:
                self._last_growth = time.time()
            self.size = self._base_bytes + new_size
            self.size_text = _fmt_size(self.size)
        elif key == "bitrate":
            if val and val.upper() != "N/A":
                self.bitrate = val.replace("bits/s", "bps")
        elif key == "progress":
            self._refresh_disk()
            if not self.bitrate and self.elapsed > 1 and self.size:
                kbps = self.size * 8 / self.elapsed / 1000
                self.bitrate = f"{kbps / 1000:.1f} Mbps" if kbps >= 1000 else f"{kbps:.0f} Kbps"
            self.push()

    def _refresh_disk(self) -> None:
        """以磁盘实际文件为准修正大小/分区数（比 ffmpeg 自报更可靠）。"""
        if not self.out_dir or not self._files_prefix:
            return
        total = 0
        count = 0
        latest = ""
        try:
            for name in os.listdir(self.out_dir):
                if not name.startswith(self._files_prefix):
                    continue
                full = os.path.join(self.out_dir, name)
                if not os.path.isfile(full):
                    continue
                try:
                    total += os.path.getsize(full)
                except OSError:
                    continue
                count += 1
                if name > latest:
                    latest = name
        except OSError:
            return
        if total:
            if self._base_bytes + total != self.size:
                self._last_growth = time.time()
            self.size = self._base_bytes + total
            self.size_text = _fmt_size(self.size)
        self.segments = count
        if latest:
            self.current_file = latest

    # ---------- 弹幕 ----------
    async def _start_danmaku(self, video_dir: str) -> None:
        if not self.danmaku_enabled:
            self.danmaku_status = "off"
            return
        if self._dm_session is not None:
            # 虎牙分片续录会反复进出这里，已经连着就别重开，否则计数会一会大一会小
            return
        from .danmaku import DanmakuSession, create_client

        client = create_client(self.platform, self.url, self.cfg.cookie(self.platform))
        if client is None:
            self.danmaku_status = "unsupported"
            self.danmaku_note = "该平台暂不支持弹幕采集"
            return
        if not getattr(client, "supported", True):
            self.danmaku_status = "unsupported"
            self.danmaku_note = getattr(client, "reason", "协议适配中")
            self.push(force=True)
            return
        try:
            self._dm_session = DanmakuSession(
                client, self.platform, self.url, self.anchor or self.url,
                video_dir, self.room_id, self._on_danmaku,
                danmaku_dir=self.cfg.danmaku_root(),
            )
            self.session_id = self._dm_session.session_id
            await self._dm_session.start()
            self.danmaku_status = "live"
            self.danmaku_note = ""
            log("INFO", f"{self.anchor} 弹幕采集已启动 → {self._dm_session.jsonl_path}")
        except Exception as exc:
            self.danmaku_status = "error"
            self.danmaku_note = f"{type(exc).__name__}: {exc}"
            log("WARN", f"{self.anchor} 弹幕采集启动失败：{self.danmaku_note}")
        self.push(force=True)

    async def _stop_danmaku(self) -> None:
        if self._dm_session is not None:
            try:
                await self._dm_session.stop()
            except Exception:
                pass
            self._dm_session = None
        if self.danmaku_status == "live":
            self.danmaku_status = "closed"
        self.push(force=True)

    def _on_danmaku(self, evt: dict) -> None:
        # 上一场会话收尾时可能还有几条事件在路上，丢掉它们：
        # 否则旧会话的大计数会把新会话的值顶回去。
        sid = evt.get("session_id")
        if sid and self.session_id and sid != self.session_id:
            return
        self.danmaku_count = int(evt.get("count") or self.danmaku_count)
        if self.danmaku_forward:
            try:
                self.danmaku_forward(evt)
            except Exception:
                pass
        self.push()
