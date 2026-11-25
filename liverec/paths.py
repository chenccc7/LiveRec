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


def core_dir() -> str:
    return os.path.join(bundle_root(), "core")


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


def seed_config() -> None:
    """首次运行时把上游 core/config/*.ini 复制到 data/config。"""
    src = os.path.join(core_dir(), "config")
    dst = config_dir()
    if not os.path.isdir(src):
        return
    for name in os.listdir(src):
        if not name.lower().endswith(".ini"):
            continue
        target = os.path.join(dst, name)
        if not os.path.exists(target):
            shutil.copyfile(os.path.join(src, name), target)


def ffmpeg_exe() -> str:
    for cand in (
        os.path.join(bundle_root(), "bin", "ffmpeg.exe"),
        os.path.join(app_root(), "bin", "ffmpeg.exe"),
        os.path.join(core_dir(), "bin", "ffmpeg.exe"),
    ):
        if os.path.exists(cand):
            return cand
    return shutil.which("ffmpeg") or "ffmpeg"


def node_exe() -> str | None:
    for cand in (
        os.path.join(bundle_root(), "node", "node.exe"),
        os.path.join(core_dir(), "node", "node.exe"),
        os.path.join(app_root(), "node", "node.exe"),
    ):
        if os.path.exists(cand):
            return cand
    return shutil.which("node")
