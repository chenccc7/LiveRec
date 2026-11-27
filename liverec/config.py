"""配置读写：直接操作上游 data/config/*.ini，保证与命令行版互通。

注意：config.ini 的键是中文，且 RawConfigParser 默认会把键名小写化，
必须设置 optionxform = str，否则所有 get 都会落空。
"""
from __future__ import annotations

import configparser
import os
import threading
from typing import Any

from . import paths

SEC_REC = "录制设置"
SEC_COOKIE = "Cookie"

# 上游的「键名」里带可选取值，直接当键名用
K_QUALITY = "原画|超清|高清|标清|流畅"
K_FORMAT = "视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频"
# 弹幕明细（jsonl）落盘位置；留空 = 跟着录像目录走（<录像目录>/danmaku/）
K_DANMAKU_ROOT = "弹幕保存路径(不填则跟随视频目录)"
# 自定义 ffmpeg.exe 路径；留空 = 用内置（bin/ffmpeg.exe）或 PATH 里的那个
K_FFMPEG = "ffmpeg路径(不填则用内置)"
K_SAVE_ROOT = "直播保存路径(不填则默认)"
K_FOLDER_BY_AUTHOR = "保存文件夹是否以作者区分"
K_CONCURRENT = "同一时间访问网络的线程数"
K_SPLIT_ON = "分段录制是否开启"
K_SPLIT_SECONDS = "视频分段时间(秒)"
K_CHECK_INTERVAL = "开播检测间隔(秒)"

# 只有这些键是本程序真正读写的；其它（上游遗留的推送/账号/几十个平台 cookie 等）
# 一律在启动时清掉，配置文件保持干净，用户也不会被一堆没用的键看晕。
REC_KEYS: tuple[tuple[str, str], ...] = (
    (K_SAVE_ROOT, ""),
    (K_FOLDER_BY_AUTHOR, "是"),
    (K_FORMAT, "flv"),
    (K_QUALITY, "原画"),
    (K_CONCURRENT, "5"),
    (K_SPLIT_ON, "是"),
    (K_SPLIT_SECONDS, "1800"),
    (K_DANMAKU_ROOT, ""),
    (K_FFMPEG, ""),
    (K_CHECK_INTERVAL, "60"),
)

QUALITY_CODES = {
    "原画": "OD",
    "蓝光": "BD",
    "超清": "UHD",
    "高清": "HD",
    "标清": "SD",
    "流畅": "LD",
}

FORMATS = ["ts", "mkv", "flv", "mp4", "mp3", "m4a"]

COOKIE_KEYS = {
    "douyin": "抖音cookie",
    "bilibili": "b站cookie",
    "huya": "虎牙cookie",
    "douyu": "斗鱼cookie",
}


def _new_parser() -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser()
    cp.optionxform = str  # type: ignore[assignment]  # 保留大小写
    return cp


class Config:
    """线程安全的 ini 读写包装。"""

    def __init__(self, ini_path: str | None = None):
        self.path = ini_path or os.path.join(paths.config_dir(), "config.ini")
        self._lock = threading.RLock()
        self.cp = _new_parser()
        self.reload()
        try:
            # 清掉上游遗留的无关键、补齐缺失的必备键（幂等，无变更不写文件）
            self.normalize_keys()
            # 整个文件夹被搬走后，老的绝对路径会失效：启动时就地改写成相对路径
            self.relocate_paths()
        except Exception:  # noqa: BLE001 - 迁移失败不影响启动
            pass

    def reload(self) -> None:
        with self._lock:
            self.cp = _new_parser()
            if os.path.exists(self.path):
                self.cp.read(self.path, encoding="utf-8-sig")

    # ---------- 读取 ----------
    def raw(self, section: str, key: str, default: str = "") -> str:
        with self._lock:
            try:
                val = self.cp.get(section, key)
            except (configparser.NoSectionError, configparser.NoOptionError):
                return default
            return default if val is None else val.strip()

    def get_int(self, section: str, key: str, default: int) -> int:
        try:
            return int(self.raw(section, key, str(default)).strip())
        except (TypeError, ValueError):
            return default

    def get_bool(self, section: str, key: str, default: bool) -> bool:
        val = self.raw(section, key, "是" if default else "否").lower()
        return val in ("是", "true", "1", "yes", "on", "开启")

    # ---------- 写入 ----------
    def set(self, section: str, key: str, value: Any) -> None:
        with self._lock:
            if not self.cp.has_section(section):
                self.cp.add_section(section)
            self.cp.set(section, key, str(value))

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                self.cp.write(fh)

    # ---------- 语义化读取 ----------
    def quality(self) -> str:
        q = self.raw(SEC_REC, K_QUALITY, "原画") or "原画"
        return q if q in QUALITY_CODES else "原画"

    def quality_code(self, quality: str | None = None) -> str:
        return QUALITY_CODES.get(quality or self.quality(), "OD")

    def save_format(self) -> str:
        fmt = (self.raw(SEC_REC, K_FORMAT, "flv") or "flv").strip().lower()
        return fmt if fmt in FORMATS else "flv"

    def split_enabled(self) -> bool:
        return self.get_bool(SEC_REC, "分段录制是否开启", True)

    def split_seconds(self) -> int:
        return max(60, self.get_int(SEC_REC, "视频分段时间(秒)", 1800))

    def check_interval(self) -> int:
        """无人值守时的开播检测间隔（秒），默认 60。"""
        return max(20, min(3600, self.get_int(SEC_REC, "开播检测间隔(秒)", 60)))

    def concurrent(self) -> int:
        return max(1, self.get_int(SEC_REC, "同一时间访问网络的线程数", 5))

    def folder_by_author(self) -> bool:
        return self.get_bool(SEC_REC, "保存文件夹是否以作者区分", True)

    # ---------- 路径：相对 app_root 保存，整个文件夹搬走也不会失效 ----------
    @staticmethod
    def _relativize(value: str) -> str:
        """把 app_root 内的绝对路径存成相对路径（换机器/搬文件夹后依然有效）。"""
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            full = os.path.abspath(os.path.expanduser(text))
        except (OSError, ValueError):
            return text
        root = os.path.abspath(paths.app_root())
        if full == root:
            return "."
        if full.startswith(root + os.sep):
            return os.path.relpath(full, root)
        return full

    @staticmethod
    def _locate(value: str) -> str:
        """把配置里的路径还原成绝对路径；老的绝对路径按 data/ 之后的部分重新定位。"""
        text = str(value or "").strip().strip('"').strip("'")
        if not text:
            return ""
        try:
            expanded = os.path.expanduser(text)
        except (OSError, ValueError):
            return text
        if not os.path.isabs(expanded):
            return os.path.normpath(os.path.join(paths.app_root(), expanded))
        if os.path.exists(expanded):
            return expanded
        marker = os.sep + "data" + os.sep
        index = expanded.lower().rfind(marker.lower())
        if index >= 0:
            tail = expanded[index + len(marker):]
            # 只在「明显是本程序的 data 子目录」时才重定位，避免误改用户自己的 D:\data\xxx
            head = tail.split(os.sep, 1)[0].lower()
            if head in ("downloads", "config", "manifests", "danmaku", "logs", "merge-tmp"):
                return os.path.normpath(os.path.join(paths.app_root(), "data", tail))
        return expanded

    def normalize_keys(self) -> None:
        """只保留本程序真正使用的配置键，缺失的必备键写默认值（幂等）。"""
        changed = False
        with self._lock:
            keep_sections = {SEC_REC, SEC_COOKIE}
            for name in list(self.cp.sections()):
                if name not in keep_sections:
                    self.cp.remove_section(name)
                    changed = True
            if not self.cp.has_section(SEC_REC):
                self.cp.add_section(SEC_REC)
                changed = True
            wanted = {key: default for key, default in REC_KEYS}
            for key in list(self.cp.options(SEC_REC)):
                if key not in wanted:
                    self.cp.remove_option(SEC_REC, key)
                    changed = True
            for key, default in REC_KEYS:
                if not self.cp.has_option(SEC_REC, key):
                    self.cp.set(SEC_REC, key, default)
                    changed = True
            if not self.cp.has_section(SEC_COOKIE):
                self.cp.add_section(SEC_COOKIE)
                changed = True
            keep_cookies = set(COOKIE_KEYS.values())
            for key in list(self.cp.options(SEC_COOKIE)):
                if key not in keep_cookies:
                    self.cp.remove_option(SEC_COOKIE, key)
                    changed = True
            for key in COOKIE_KEYS.values():
                if not self.cp.has_option(SEC_COOKIE, key):
                    self.cp.set(SEC_COOKIE, key, "")
                    changed = True
        if changed:
            self.save()

    def relocate_paths(self) -> None:
        """启动时把「搬过的程序目录」留下的绝对路径改写成相对路径（幂等、无变更不写文件）。"""
        changed = False
        for key in ("直播保存路径(不填则默认)", K_DANMAKU_ROOT, K_FFMPEG):
            raw = self.raw(SEC_REC, key, "").strip()
            if not raw:
                continue
            # 先按新位置重定位（老机器/旧文件夹的绝对路径），再存成相对路径
            stored = self._relativize(self._locate(raw))
            if stored != raw:
                self.set(SEC_REC, key, stored)
                changed = True
        if changed:
            self.save()

    def save_root(self) -> str:
        custom = self._locate(self.raw(SEC_REC, "直播保存路径(不填则默认)", ""))
        if custom:
            try:
                os.makedirs(custom, exist_ok=True)
                return custom
            except OSError:
                pass
        return paths.default_download_dir()

    def danmaku_root(self) -> str:
        """弹幕明细的根目录；空串表示「跟着视频目录走」（放在录像旁边的 danmaku/ 里）。"""
        custom = self._locate(self.raw(SEC_REC, K_DANMAKU_ROOT, ""))
        if not custom:
            return ""
        try:
            os.makedirs(custom, exist_ok=True)
            return custom
        except OSError:
            return ""

    def ffmpeg_path(self) -> str:
        """设置页里自定义的 ffmpeg.exe；返回真正生效的路径（无效/为空则回落到内置）。"""
        raw = self._locate(self.raw(SEC_REC, K_FFMPEG, ""))
        return paths.set_ffmpeg_path(raw)

    def apply_ffmpeg_path(self) -> str:
        """把配置里的 ffmpeg 路径应用到 paths（启动时、保存设置后各调一次）。"""
        return self.ffmpeg_path()


    def cookie(self, platform: str) -> str:
        return self.raw(SEC_COOKIE, COOKIE_KEYS.get(platform, ""), "")

    # ---------- 面向界面的快照 ----------
    def to_dict(self) -> dict[str, Any]:
        return {
            "quality": self.quality(),
            "saveFormat": self.save_format(),
            "saveRoot": self.save_root(),
            "danmakuRoot": self.danmaku_root(),
            "splitEnabled": self.split_enabled(),
            "splitSeconds": self.split_seconds(),
                "checkInterval": self.check_interval(),
            "concurrent": self.concurrent(),
            "folderByAuthor": self.folder_by_author(),
            "cookies": {p: self.cookie(p) for p in COOKIE_KEYS},
            "ffmpeg": paths.ffmpeg_exe(),
            "ffmpegPath": self._locate(self.raw(SEC_REC, K_FFMPEG, "")),
        }

    def apply_dict(self, data: dict[str, Any]) -> None:
        if "quality" in data and data["quality"] in QUALITY_CODES:
            self.set(SEC_REC, K_QUALITY, data["quality"])
        if "saveFormat" in data and str(data["saveFormat"]).lower() in FORMATS:
            self.set(SEC_REC, K_FORMAT, str(data["saveFormat"]).lower())
        if "saveRoot" in data:
            self.set(SEC_REC, "直播保存路径(不填则默认)", self._relativize(str(data["saveRoot"] or "")))
        if "danmakuRoot" in data:
            self.set(SEC_REC, K_DANMAKU_ROOT, self._relativize(str(data["danmakuRoot"] or "")))
        if "ffmpegPath" in data:
            self.set(SEC_REC, K_FFMPEG, self._relativize(str(data["ffmpegPath"] or "")))
            self.apply_ffmpeg_path()
        if "splitEnabled" in data:
            self.set(SEC_REC, "分段录制是否开启", "是" if data["splitEnabled"] else "否")
        if "splitSeconds" in data:
            self.set(SEC_REC, "视频分段时间(秒)", int(data["splitSeconds"]))
        if "checkInterval" in data and data["checkInterval"]:
            self.set(SEC_REC, "开播检测间隔(秒)", int(data["checkInterval"]))
        if "concurrent" in data:
            self.set(SEC_REC, "同一时间访问网络的线程数", int(data["concurrent"]))
        if "folderByAuthor" in data:
            self.set(SEC_REC, "保存文件夹是否以作者区分", "是" if data["folderByAuthor"] else "否")
        cookies = data.get("cookies") or {}
        for platform, val in cookies.items():
            key = COOKIE_KEYS.get(platform)
            if key:
                self.set(SEC_COOKIE, key, str(val or ""))
        self.save()


# ---------- URL_config.ini：房间清单（与命令行版互通） ----------

def parse_url_config(path: str | None = None) -> list[dict[str, Any]]:
    """解析一行一个直播间的 URL_config.ini。

    行格式：画质,URL,主播: 名字   行首 # 表示停用但仍轮询。
    """
    target = path or os.path.join(paths.config_dir(), "URL_config.ini")
    out: list[dict[str, Any]] = []
    if not os.path.exists(target):
        return out
    with open(target, "r", encoding="utf-8-sig") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            enabled = True
            if line.startswith("#"):
                enabled = False
                line = line.lstrip("#").strip()
                if not line:
                    continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            quality, url = parts[0], parts[1]
            anchor = parts[2] if len(parts) > 2 else ""
            if anchor.startswith("主播:"):
                anchor = anchor[3:].strip()
            if not url.startswith("http"):
                continue
            out.append({"quality": quality, "url": url, "anchor": anchor, "enabled": enabled})
    return out


def write_url_config(rooms: list[dict[str, Any]], path: str | None = None) -> None:
    target = path or os.path.join(paths.config_dir(), "URL_config.ini")
    lines = ["# 由 LiveRec 控制台维护；格式：画质,URL,主播: 名字（行首 # 为停用）"]
    for room in rooms:
        anchor = (room.get("anchor") or "").strip()
        # 画质写「实际解析到的档位」：用户选原画但平台只给超清时，这里就是超清
        quality = (room.get("qualityActual") or room.get("quality") or "原画").strip()
        body = f"{quality},{room.get('url')},主播: {anchor}"
        lines.append(("" if room.get("enabled", True) else "#") + body)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
