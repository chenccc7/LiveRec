"""系统托盘常驻（原生 Win32 实现，不依赖 pystray）。

关掉控制台窗口后录制继续；托盘菜单可以唤回控制台、全部开始/停止、
打开录制目录、切换无人值守、退出。

之所以不用 pystray：打包成 windowed exe 后它内部的
``ChangeWindowMessageFilterEx`` 会返回 WinError 5，托盘直接起不来。
这里只用 ctypes + Shell_NotifyIconW，行为可控。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Callable

from . import paths
from .logbook import log

WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_NULL = 0x0000

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_SEPARATOR = 0x00000800

TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020
TPM_RETURNCMD = 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

CMD_SHOW = 1
CMD_START_ALL = 2
CMD_STOP_ALL = 3
CMD_OPEN_FOLDER = 4
CMD_TOGGLE_UA = 5
CMD_QUIT = 6


def available() -> bool:
    return sys.platform == "win32"


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _Guid),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

LRESULT = ctypes.c_ssize_t


def _setup_prototypes(user32, shell32, kernel32) -> None:
    """显式声明签名：不声明的话 LPARAM 会被当成 int 转换，指针值直接溢出。"""
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.WORD
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t,
                                   wintypes.LPCWSTR]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      ctypes.c_void_p]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    kernel32.GetLastError.restype = wintypes.DWORD


def _icon_path() -> str:
    candidates = [
        os.path.join(paths.app_root(), "bin", "liverec.ico"),
        os.path.join(paths.bundle_root(), "bin", "liverec.ico"),
    ]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return ""


class Tray:
    """托盘控制器：菜单动作转给 ``controller``，它负责和 asyncio 服务通信。"""

    _ID = 1
    _CLASS = "LiveRecTrayWnd"

    def __init__(self, controller: Any, on_quit: Callable[[], None]):
        self.controller = controller
        self.on_quit = on_quit
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _setup_prototypes(self.user32, self.shell32, self.kernel32)
        self.hwnd: int = 0
        self.hicon: int = 0
        self.nid: NOTIFYICONDATAW | None = None
        self._wndproc = WNDPROC(self._on_message)  # 必须常驻，否则回调被回收
        self._halt = threading.Event()
        self._quit_requested = False

    # ---------- 状态文案 ----------
    def _stats(self) -> dict[str, int]:
        try:
            return dict(self.controller.stats() or {})
        except Exception:
            return {}

    def _label(self, *_args) -> str:
        st = self._stats()
        rec = int(st.get("recording") or 0)
        wait = int(st.get("waiting") or 0)
        total = int(st.get("total") or 0)
        if not total:
            return "还没有直播间"
        return f"录制中 {rec} 路 · 等待 {wait} 路 · 共 {total} 路"

    def _unattended_label(self, *_args) -> str:
        try:
            on = bool(self.controller.unattended())
        except Exception:
            on = False
        return f"无人值守：{'开启' if on else '关闭'}"

    def _tooltip(self) -> str:
        label = self._label()
        return f"LiveRec 控制台 · {label}" if label != "还没有直播间" else "LiveRec 控制台"

    # ---------- 窗口回调 ----------
    def _on_message(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        try:
            if msg == WM_TRAYICON:
                if lparam == WM_LBUTTONUP:
                    self._show()
                elif lparam in (WM_RBUTTONUP, 0x0204):  # 右键 / 右键按下
                    self._popup_menu()
                elif lparam == WM_LBUTTONDBLCLK:
                    self._show()
                return 0
            if msg == WM_CLOSE:
                self.user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # 回调里绝不能抛
            log("WARN", f"托盘回调异常：{exc}")
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------- 菜单 ----------
    def _popup_menu(self) -> None:
        u = self.user32
        menu = u.CreatePopupMenu()
        try:
            u.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, self._label())
            u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            u.AppendMenuW(menu, MF_STRING, CMD_SHOW, "打开控制台")
            u.AppendMenuW(menu, MF_STRING, CMD_START_ALL, "全部开始监测")
            u.AppendMenuW(menu, MF_STRING, CMD_STOP_ALL, "全部停止")
            u.AppendMenuW(menu, MF_STRING, CMD_OPEN_FOLDER, "打开录制目录")
            u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            u.AppendMenuW(menu, MF_STRING, CMD_TOGGLE_UA, self._unattended_label())
            u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            u.AppendMenuW(menu, MF_STRING, CMD_QUIT, "退出 LiveRec（停止录制）")

            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            u.SetForegroundWindow(self.hwnd)
            cmd = u.TrackPopupMenu(
                menu, TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD,
                pt.x, pt.y, 0, self.hwnd, None,
            )
            u.PostMessageW(self.hwnd, WM_NULL, 0, 0)
            if cmd:
                self._dispatch(cmd)
        finally:
            u.DestroyMenu(menu)

    def _dispatch(self, cmd: int) -> None:
        if cmd == CMD_SHOW:
            self._show()
        elif cmd == CMD_START_ALL:
            log("INFO", "托盘：全部开始监测")
            self.controller.start_all()
        elif cmd == CMD_STOP_ALL:
            log("INFO", "托盘：全部停止")
            self.controller.stop_all()
        elif cmd == CMD_OPEN_FOLDER:
            try:
                self.controller.open_folder()
            except Exception as exc:
                log("WARN", f"打开目录失败：{exc}")
        elif cmd == CMD_TOGGLE_UA:
            try:
                self.controller.toggle_unattended()
            except Exception as exc:
                log("WARN", f"切换无人值守失败：{exc}")
        elif cmd == CMD_QUIT:
            self._quit()

    def _show(self) -> None:
        try:
            self.controller.show_window()
        except Exception as exc:
            log("WARN", f"打开窗口失败：{exc}")

    def _quit(self) -> None:
        if self._quit_requested:
            return
        self._quit_requested = True
        log("INFO", "托盘：退出 LiveRec，正在停止录制…")
        self._halt.set()
        try:
            self.on_quit()
        finally:
            self._remove_icon()
            self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # ---------- 托盘图标 ----------
    def _remove_icon(self) -> None:
        if self.nid is not None:
            try:
                self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            except Exception:
                pass
            self.nid = None

    def _refresh_tip(self) -> None:
        if self.nid is None:
            return
        tip = self._tooltip()[:127]
        self.nid.szTip = tip
        self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def _create_window(self) -> bool:
        u = self.user32
        hinst = self.kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        cls.hInstance = hinst
        cls.lpszClassName = self._CLASS
        atom = u.RegisterClassExW(ctypes.byref(cls))
        if not atom:
            err = self.kernel32.GetLastError()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                log("WARN", f"托盘：RegisterClassExW 失败（{err}）")
                return False

        self.hwnd = u.CreateWindowExW(0, self._CLASS, self._CLASS, 0,
                                      0, 0, 0, 0, 0, 0, hinst, None)
        if not self.hwnd:
            log("WARN", f"托盘：CreateWindowExW 失败（{self.kernel32.GetLastError()}）")
            return False
        return True

    def _add_icon(self) -> bool:
        ico = _icon_path()
        self.hicon = 0
        if ico:
            self.hicon = self.user32.LoadImageW(None, ico, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = self._ID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self.hicon or 0
        nid.szTip = self._tooltip()[:127]
        if not self.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            log("WARN", f"托盘：Shell_NotifyIcon 添加失败（{self.kernel32.GetLastError()}）")
            return False
        self.nid = nid
        return True

    # ---------- 生命周期 ----------
    def _tick(self) -> None:
        while not self._halt.wait(5):
            try:
                self._refresh_tip()
            except Exception:
                pass

    def run(self) -> str:
        if not available():
            log("WARN", "当前系统不是 Windows，无法启用系统托盘")
            return "unavailable"
        if not self._create_window() or not self._add_icon():
            self._remove_icon()
            return "error"

        log("INFO", "系统托盘已启用：关闭窗口后录制继续，从托盘图标可唤回控制台")
        threading.Thread(target=self._tick, daemon=True, name="tray-tick").start()

        msg = wintypes.MSG()
        try:
            while self.user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            log("WARN", f"托盘消息循环异常：{exc}")
            self._remove_icon()
            return "error"
        self._remove_icon()
        return "quit" if self._quit_requested else "error"

    def stop(self) -> None:
        self._halt.set()
        self._remove_icon()
        if self.hwnd:
            try:
                self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
