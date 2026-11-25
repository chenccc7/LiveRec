# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件 exe（内嵌 ffmpeg、前端静态资源、默配置模板）。"""
import os

import py_mini_racer

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("web", "web"),
    ("core/config", "core/config"),
    ("bin/ffmpeg.exe", "bin"),
    ("bin/liverec.ico", "bin"),
    ("liverec/danmaku/assets", "liverec/danmaku/assets"),
    ("liverec/danmaku/proto/douyin.proto", "liverec/danmaku/proto"),
]

# py-mini-racer 带一个原生 v8 库；打包态它只认 sys._MEIPASS 根目录下的 mini_racer.dll
_mr_dir = os.path.dirname(py_mini_racer.__file__)
datas += [
    (_mr_dir, "py_mini_racer"),
    (os.path.join(_mr_dir, "mini_racer.dll"), "."),
]
_binaries = [
    (os.path.join(_mr_dir, "mini_racer.dll"), "."),
]

hiddenimports = (
    collect_submodules("uvicorn")
    # yt-dlp 的虎牙解析器按需 import 大量 extractor 模块，必须整包收集
    + collect_submodules("yt_dlp")
    + [
        "websockets",
        "websockets.legacy",
        "websockets.legacy.client",
        "websockets.legacy.server",
        "fastapi",
        "starlette",
        "pydantic",
        "anyio",
        "loguru",
        "httpx",
        "httpcore",
        "brotli",
        "py_mini_racer",
        "google.protobuf",
        "liverec",
        "liverec.server",
        "liverec.manager",
        "liverec.recorder",
        "liverec.resolver",
    "liverec.cookie_check",
        "liverec.douyin_sign",
        "liverec.desktop",
        "liverec.tray",
        "liverec.config",
        "liverec.logbook",
        "liverec.paths",
        "liverec.danmaku.base",
        "liverec.danmaku.bilibili",
        "liverec.danmaku.douyu",
        "liverec.danmaku.douyin",
        "liverec.danmaku.huya",
        "liverec.danmaku.proto",
        "liverec.danmaku.proto.douyin_pb2",
        "liverec.huya_ytdlp",
    ]
)

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "pystray", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LiveRec",
    icon="bin/liverec.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
