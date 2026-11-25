"""分段录像的拼接与转封装。

虎牙这类平台每几十秒就要换一次签名地址，录出来的是一场直播里的多个分段
（每段各自从 0 开始计时）。直接 `-f concat -c copy` 拼 FLV 会因为各段时间轴
互不相干而只拼出第一段的长度，所以这里先逐段归一化成 TS（重置时间轴），
再用 concat 拼接，最后按用户选的容器转封装。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import tempfile

from . import paths
from .logbook import log

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")

_FORMAT_EXT = {
    "ts": "ts",
    "mkv": "mkv",
    "flv": "flv",
    "mp4": "mp4",
    "mp3音频": "mp3",
    "m4a音频": "m4a",
}

_TARGET_TEMPLATE = {
    "ts": ["-f", "mpegts"],
    "mkv": ["-f", "matroska"],
    "flv": ["-f", "flv"],
    "mp4": ["-movflags", "+faststart", "-f", "mp4"],
    "mp3": ["-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"],
    "m4a": ["-vn", "-c:a", "aac", "-b:a", "192k", "-f", "ipod"],
}


def _ffmpeg() -> str:
    return paths.ffmpeg_exe() or "ffmpeg"


def _run(cmd: list[str], timeout: float = 3600.0) -> tuple[int, str]:
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"
    return res.returncode, (res.stderr or "").strip()


def temp_root() -> str:
    """拼接用的临时目录：优先用设置里指定的，否则 data/merge-tmp。"""
    try:
        from .config import Config
        from . import paths as _paths
        cfg = Config(os.path.join(_paths.config_dir(), "config.ini"))
        root = cfg.temp_dir()
    except Exception:
        from . import paths as _paths
        root = os.path.join(_paths.data_dir(), "merge-tmp")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        root = os.path.join(_paths.data_dir(), "merge-tmp")
        os.makedirs(root, exist_ok=True)
    return root


def probe_duration(path: str) -> float:
    """用 ffmpeg -i 的输出抓时长（秒），拿不到返回 0。"""
    if not os.path.isfile(path):
        return 0.0
    code, err = _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=120)
    if code < 0:
        return 0.0
    match = _DUR_RE.search(err)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _normalize(part: str, out: str, container: str = "mpegts") -> tuple[bool, str]:
    """把一段录好的文件重新封装成时间轴从 0 开始的容器（默认 TS）。

    分段可能是被 CDN 掐断的残缺文件，所以先常规转一次，失败再放宽错误容忍
    重试一次（`-err_detect ignore_err` 能救回大部分「末尾不完整」的 FLV）。
    """
    attempts = [
        ["-fflags", "+genpts", "-i", part],
        ["-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt", "-i", part],
    ]
    last = ""
    for extra in attempts:
        code, err = _run([
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            *extra, "-c", "copy", "-avoid_negative_ts", "make_zero", "-f", container, out,
        ])
        ok = code == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
        if ok and probe_duration(out) > 0.2:
            return True, ""
        last = " | ".join(err.splitlines()[-3:]) or f"产出为空（rc={code}）"
        with contextlib.suppress(OSError):
            os.remove(out)
    return False, last


def _write_list(items: list[str], list_path: str) -> None:
    with open(list_path, "w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write("file '" + item.replace("'", "'\\''") + "'\n")


def _concat(items: list[str], out: str, container: str) -> tuple[bool, str]:
    """直接用 concat 分离器把若干文件拼到 out（流拷贝，不重编码）。"""
    tmp_root = os.path.join(paths.data_dir(), "merge-tmp")
    os.makedirs(tmp_root, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="c-", dir=tmp_root)
    try:
        list_path = os.path.join(tmp_dir, "list.txt")
        _write_list(items, list_path)
        attempts = [
            ["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy"],
            ["-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt",
             "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy"],
        ]
        last = ""
        for extra in attempts:
            code, err = _run([
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                *extra, "-f", container, out,
            ], timeout=3600)
            ok = code == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
            if ok and probe_duration(out) > 0.5:
                return True, f"{probe_duration(out):.1f} 秒"
            last = " | ".join(err.splitlines()[-3:]) or f"产出为空（rc={code}）"
            with contextlib.suppress(OSError):
                os.remove(out)
        return False, last
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def merge_parts(parts: list[str], out: str) -> tuple[bool, str]:
    """把一场直播的多个分段拼成一个文件，返回 (是否成功, 说明)。

    虎牙的分段都是同一个源（FLV）切出来的，先试「直接 concat 原始分段」——
    这条路对「末尾被掐断」的残缺分段最宽容，能救回的信息最多；
    不行再退回「逐段重封装成 TS 再拼」的老路。
    """
    parts = [p for p in parts if os.path.isfile(p) and os.path.getsize(p) > 0]
    if not parts:
        return False, "没有可用分段"
    if len(parts) == 1:
        ok, detail = _normalize(parts[0], out)
        if not ok:
            return False, f"单段重封装失败：{detail}"
        return True, f"{probe_duration(out):.1f} 秒"

    expected = sum(probe_duration(p) for p in parts)
    flv_parts = [p for p in parts if p.lower().endswith(".flv")]
    if len(flv_parts) == len(parts):
        ok, detail = _concat(parts, out, "flv")
        got = probe_duration(out) if ok else 0.0
        # 拼出来的时长至少要覆盖分段合计的六成，否则当作这条路没走通
        if ok and (not expected or got >= expected * 0.6):
            note = f"{got:.1f} 秒"
            if expected and got < expected * 0.9:
                note += f"（分段合计 {expected:.1f} 秒，残缺部分已跳过）"
            return True, note
        log("WARN", f"直接拼接失败（{detail}），改走逐段重封装")

    tmp_root = os.path.join(paths.data_dir(), "merge-tmp")
    os.makedirs(tmp_root, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="m-", dir=tmp_root)
    try:
        normalized: list[str] = []
        skipped: list[str] = []
        total = 0.0
        for index, part in enumerate(parts):
            # 优先 TS；TS 转不出来的残缺 FLV 再试一次 FLV 输出（同容器成功率高得多）
            ok, detail = _normalize(part, os.path.join(tmp_dir, f"p{index:03d}.ts"))
            target = os.path.join(tmp_dir, f"p{index:03d}.ts")
            if not ok and part.lower().endswith(".flv"):
                target = os.path.join(tmp_dir, f"p{index:03d}_f.flv")
                ok, detail = _normalize(part, target, container="flv")
            if not ok:
                log("WARN", f"分段 {os.path.basename(part)} 无法重封装，已跳过：{detail}")
                skipped.append(os.path.basename(part))
                continue
            total += probe_duration(target)
            normalized.append(target)
        if not normalized:
            return False, "所有分段都无法重封装"

        # 归一化后容器可能混着 ts/flv，统一用 concat 分离器 + 各自容器重封装到 ts 不行，
        # 所以只把 ts 的拿去拼；一个 ts 都没有时退回直接拼原始分段。
        ts_parts = [p for p in normalized if p.endswith(".ts")]
        payload = ts_parts or [p for p in parts if p not in skipped]
        ok, detail = _concat(payload, out, "mpegts")
        if not ok:
            return False, "拼接失败：" + detail
        got = probe_duration(out)
        note = f"{got:.1f} 秒"
        if skipped:
            note += f"（跳过 {len(skipped)} 个残缺分段）"
        elif total and got < total * 0.8:
            note += f"（分段合计 {total:.1f} 秒，部分分段可能已损坏）"
        return True, note
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def remux(src: str, fmt: str) -> tuple[bool, str]:
    """把录好的文件转成用户选的容器（流拷贝，不重新编码）。

    分段末尾可能是被 CDN 掐断的半个包，直接 `-c copy` 会报
    `Error muxing a packet / Invalid data`。这里先常规转一次，失败再放宽错误容忍
    重试；只要转出来的文件有可播放时长，就当成成功（末尾残缺极少数帧无妨）。
    """
    ext = _FORMAT_EXT.get(fmt, "")
    if not ext or src.lower().endswith("." + ext):
        return True, "无需转换"
    out = os.path.splitext(src)[0] + "." + ext
    extra = _TARGET_TEMPLATE.get(ext, [])
    attempts = [
        ["-i", src],
        ["-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt", "-i", src],
    ]
    last = ""
    for input_args in attempts:
        with contextlib.suppress(OSError):
            os.remove(out)
        code, err = _run([
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            *input_args, "-c", "copy", *extra, out,
        ])
        duration = probe_duration(out)
        if os.path.isfile(out) and duration > 1.0:
            with contextlib.suppress(OSError):
                os.remove(src)
            note = os.path.basename(out)
            if code != 0:
                note += f"（末尾 {duration:.0f} 秒后数据不完整，已保留可播部分）"
            return True, note
        last = " | ".join((err or "").splitlines()[-3:]) or f"产出不可播放（rc={code}）"
        with contextlib.suppress(OSError):
            os.remove(out)
    return False, "转封装失败：" + last


def convert_session(path: str, target_fmt: str = "") -> None:
    """只有一段时的收尾：直接按用户选的容器转封装（虎牙中间文件固定是 FLV）。"""
    label = os.path.basename(path)
    if not target_fmt:
        return
    ok, detail = remux(path, target_fmt)
    if not ok:
        log("WARN", f"{label} 转成「{target_fmt}」失败，保留原始文件：{detail}")
    elif detail != "无需转换":
        log("SUCCESS", f"{label} 已转成 {detail}")


def merge_session(parts: list[str], out: str, keep_parts: bool = False,
                  target_fmt: str = "") -> tuple[bool, str]:
    """拼接 + 记日志 + 视情况清理分段，可选再转成目标容器。

    返回 (是否成功, 说明)；既能给后台线程用（忽略返回值），
    也能在「停止录制」时被同步 await（要拿到结果做日志）。
    """
    label = os.path.basename(out)
    if len(parts) < 2:
        return True, "只有一个分段，无需拼接"
    log("INFO", f"{label} 开始拼接 {len(parts)} 个分段…")
    ok, detail = merge_parts(parts, out)
    if not ok:
        log("WARN", f"{label} 拼接失败（分段已保留）：{detail}")
        return False, detail
    log("SUCCESS", f"{label} 拼接完成，时长 {detail}")
    if target_fmt:
        ok2, detail2 = remux(out, target_fmt)
        if not ok2:
            log("WARN", f"{label} 转成「{target_fmt}」失败，保留拼接文件：{detail2}")
            return True, f"已拼接（{detail}），转封装失败：{detail2}"
        if detail2 != "无需转换":
            log("SUCCESS", f"{label} 已转成 {detail2}")
    if not keep_parts:
        for part in parts:
            try:
                if os.path.abspath(part) != os.path.abspath(out):
                    os.remove(part)
            except OSError:
                pass
    return True, detail


async def _amain() -> None:  # pragma: no cover - 手工调试用
    await asyncio.sleep(0)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_amain())
