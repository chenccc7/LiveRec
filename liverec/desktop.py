"""桌面窗口：用 Edge/Chrome 的 --app 模式开一个无地址栏的原生窗口。

窗口默认非阻塞：关掉窗口不会结束程序（录制继续），需要时用 :meth:`AppWindow.show`
重新拉起或把已有窗口提到最前。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import urlsplit

from . import paths
from .logbook import log

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _first_existing(paths_list: list[str]) -> str | None:
    for cand in paths_list:
        if os.path.exists(cand):
            return cand
    return None


def _profile_dir() -> str:
    d = os.path.join(paths.data_dir(), "webview")
    os.makedirs(d, exist_ok=True)
    return d


def _windows_by_title(title_keyword: str) -> list[int]:
    """列出标题匹配、且属于浏览器进程的可见窗口句柄。"""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        found: list[int] = []

        def _exe_name(pid: int) -> str:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return ""
            try:
                size = wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value).lower()
                return ""
            finally:
                kernel32.CloseHandle(handle)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, lparam):  # noqa: ANN001
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if title == title_keyword or title.startswith(title_keyword):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if _exe_name(pid.value) in ("msedge.exe", "chrome.exe"):
                    found.append(hwnd)
                    return False
            return True

        user32.EnumWindows(_enum, 0)
        return found
    except Exception:
        return []


def _focus_window(title_keyword: str) -> bool:
    """把标题匹配的已有窗口提到前台（Windows）。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        found = _windows_by_title(title_keyword)
        if not found:
            return False
        user32 = ctypes.windll.user32
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _close_windows(title_keyword: str) -> int:
    """给匹配的窗口发 WM_CLOSE（Edge 会把启动参数转交给已有实例，
    自己 spawn 的进程可能早就退出了，所以关窗口不能只靠 terminate 进程）。"""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        WM_CLOSE = 0x0010
        found = _windows_by_title(title_keyword)
        user32 = ctypes.windll.user32
        for hwnd in found:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return len(found)
    except Exception:
        return 0


def close_stale_splash() -> int:
    """关掉上回异常退出留下的「LiveRec 正在启动…」窗口。

    启动画面是转瞬即逝的窗口，任何时刻都不该有另一个进程在用它；
    留着只会让下次启动看起来像开了两个窗口。
    """
    return _close_windows("LiveRec 正在启动")


def _screen_size() -> tuple[int, int]:
    if sys.platform != "win32":
        return (1600, 900)
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
    except Exception:
        return (1600, 900)


def _geometry(width: int, height: int) -> tuple[int, int, int, int]:
    """按屏幕大小收一下窗口，并放到屏幕中央偏上（避免超出屏幕）。"""
    sw, sh = _screen_size()
    w = min(width, int(sw * 0.94))
    h = min(height, int(sh * 0.90))
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 4)
    return w, h, x, y


class AppWindow:
    """可重复打开的控制台窗口。"""

    def __init__(self, url: str, title: str = "LiveRec 控制台",
                 width: int = 1520, height: int = 980):
        self.url = url
        self.title = title
        self.width = width
        self.height = height
        self.mode = ""
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ---------- 判断是否还开着 ----------
    def alive(self) -> bool:
        if self.mode == "browser-app":
            return self.proc is not None and self.proc.poll() is None
        return False

    # ---------- 打开 / 唤醒 ----------
    @property
    def live_title(self) -> str:
        """带端口的窗口标题，用来区分「指向当前服务的窗口」和以前的僵尸窗口。

        页面加载后会把自己标题设成 `LiveRec 控制台 · <port>`；程序换了端口重启
        （或者上次没关干净）时，旧窗口标题里的端口对不上，就不会被误认成当前窗口。
        """
        port = ""
        try:
            port = str(urlsplit(self.url).port or "")
        except ValueError:
            port = ""
        return f"{self.title} · {port}" if port else self.title

    def show(self) -> str:
        """把控制台窗口带到前台；没有窗口就开一个（任何时刻只留一个）。

        注意：Edge 的 `--app` 会把启动参数转交给已在运行的实例，自己 spawn 的进程
        可能马上就退出了。所以判断「窗口在不在」得看窗口，而不是看进程。
        """
        with self._lock:
            for attempt in range(4):
                if _focus_window(self.live_title):
                    return "focused"
                time.sleep(0.15 if attempt == 0 else 0.35)
            # 没有指向当前服务的窗口：把标题还停在旧端口上的僵尸窗口清掉再开一个，
            # 否则会同时留着几个「LiveRec 控制台」，用户点到的是已经关掉的那个页面。
            stale = _close_windows(self.title)
            if stale:
                log("INFO", f"清理了 {stale} 个指向旧地址的控制台窗口")
                time.sleep(0.2)
            return self._open()

    def _kill(self) -> None:
        _close_windows(self.title)
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def _open(self) -> str:
        # 窗口统一走 Edge / Chrome 的 --app 模式：独立进程，关掉不影响后台录制，
        # 也能随时重新拉起（pywebview 只能在主线程跑，会和托盘抢主线程，故不在此使用）。
        browser = _first_existing(EDGE_CANDIDATES) or _first_existing(CHROME_CANDIDATES)
        if browser:
            w, h, x, y = _geometry(self.width, self.height)
            args = [
                f"--app={self.url}",
                f"--window-size={w},{h}",
                f"--window-position={x},{y}",
                f"--user-data-dir={_profile_dir()}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            log("INFO", f"使用浏览器 app 窗口：{os.path.basename(browser)}")
            self.proc = subprocess.Popen([browser, *args])
            self.mode = "browser-app"
            return "browser-app"

        # 3) 兜底：系统默认浏览器（关掉浏览器不影响后台录制）
        log("WARN", "未找到 Edge/Chrome，改用默认浏览器打开")
        webbrowser.open(self.url)
        self.mode = "browser"
        return "browser"

    def close(self) -> None:
        """退出程序时收掉自己开的窗口（托盘退出用）。"""
        self._kill()


def open_window(url: str, title: str = "LiveRec 控制台",
                width: int = 1520, height: int = 980) -> str:
    """打开窗口并阻塞到窗口关闭（托盘不可用时的旧行为）。"""
    win = AppWindow(url, title, width, height)
    mode = win._open()
    if mode == "browser-app" and win.proc is not None:
        try:
            win.proc.wait()
        except KeyboardInterrupt:
            pass
    elif mode == "browser":
        print(f"\n已用默认浏览器打开 {url}\n关闭本窗口（Ctrl+C）即可退出程序。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return mode


def open_folder(path: str) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])


if sys.platform != "win32":
    EDGE_CANDIDATES = []
    CHROME_CANDIDATES = []
