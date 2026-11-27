"""路径解析：开发态与 PyInstaller 打包态统一入口。"""
from __future__ import annotations

import os
import shutil
import sys

FROZEN = bool(getattr(sys, "frozen", False))


def app_root() -> str:
    """数据根目录：打包后是 exe 所在目录，开发态是项目根。"""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_root() -> str:
    """只读资源根：打包后是 _MEIPASS，开发态同 app_root。"""
    if FROZEN and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return app_root()


def web_dir() -> str:
    return os.path.join(bundle_root(), "web")


def _ensure(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def data_dir() -> str:
    return _ensure(os.path.join(app_root(), "data"))


def config_dir() -> str:
    return _ensure(os.path.join(data_dir(), "config"))


def log_dir() -> str:
    return _ensure(os.path.join(data_dir(), "logs"))


def danmaku_dir() -> str:
    return _ensure(os.path.join(data_dir(), "danmaku"))


def default_download_dir() -> str:
    return _ensure(os.path.join(data_dir(), "downloads"))


def ui_state_path() -> str:
    return os.path.join(data_dir(), "ui_state.json")


_FFMPEG_OVERRIDE: str = ""


def set_ffmpeg_path(path: str) -> str:
    """设置页里自定义的 ffmpeg 路径（空串 = 用内置/自动探测）。

    返回真正生效的路径；传进来的文件不存在时不采用（返回当前仍然生效的那个），
    免得用户手滑填错导致所有录制直接崩掉。
    """
    global _FFMPEG_OVERRIDE
    candidate = (path or "").strip().strip('"').strip("'")
    if not candidate:
        _FFMPEG_OVERRIDE = ""
        return ffmpeg_exe()
    if os.path.isfile(candidate):
        _FFMPEG_OVERRIDE = candidate
    return ffmpeg_exe()


def ffmpeg_path_override() -> str:
    return _FFMPEG_OVERRIDE


def ffmpeg_ok(path: str | None = None) -> bool:
    return bool(path) and os.path.isfile(path or "")


def ffmpeg_exe() -> str:
    if _FFMPEG_OVERRIDE and os.path.isfile(_FFMPEG_OVERRIDE):
        return _FFMPEG_OVERRIDE
    for cand in (
        os.path.join(bundle_root(), "bin", "ffmpeg.exe"),
        os.path.join(app_root(), "bin", "ffmpeg.exe"),
    ):
        if os.path.exists(cand):
            return cand
    return shutil.which("ffmpeg") or "ffmpeg"

