"""LiveRec 控制台 —— 入口。

用法：
    python run.py                 启动控制台（带窗口 + 系统托盘，关窗后录制继续）
    python run.py --no-tray       不启用托盘：关掉窗口即退出
    python run.py --headless      只跑服务（调试/截图用）
    python run.py --port 8899 --no-window
    python run.py --tray-only     只起托盘不自动开窗口（开机自启时更好用）
"""
from __future__ import annotations

import argparse
import asyncio
import faulthandler
import os
import shutil
import socket
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

from liverec import paths  # noqa: E402
from liverec.logbook import log  # noqa: E402


def _ensure_streams() -> None:
    """窗口模式（console=False）下 sys.stdout/stderr 为 None，print 会直接抛异常。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                import io

                setattr(sys, name, io.StringIO())


def pick_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return preferred


def wait_port(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


class Controller:
    """托盘的桥：把菜单动作丢回 uvicorn 的事件循环里执行。"""

    def __init__(self, srv, url: str):
        self.srv = srv
        self.url = url
        self.window = None  # type: ignore[assignment]

    # ---------- 状态 ----------
    def stats(self) -> dict:
        try:
            return self.srv.manager.stats()
        except Exception:
            return {}

    def unattended(self) -> bool:
        try:
            return bool(self.srv.manager.unattended)
        except Exception:
            return False

    # ---------- 异步调用 ----------
    def _call(self, factory):
        loop = getattr(self.srv.hub, "loop", None)
        if loop is None or loop.is_closed():
            return None
        try:
            fut = asyncio.run_coroutine_threadsafe(factory(), loop)
            return fut.result(timeout=15)
        except Exception as exc:
            log("WARN", f"托盘操作失败：{exc}")
            return None

    def start_all(self) -> None:
        self._call(lambda: self.srv.manager.start_all())

    def stop_all(self) -> None:
        self._call(lambda: self.srv.manager.stop_all())

    def toggle_unattended(self) -> None:
        m = self.srv.manager
        self._call(lambda: m.set_unattended(not m.unattended))

    def open_folder(self) -> None:
        from liverec.desktop import open_folder

        target = self.srv.cfg.save_root()
        os.makedirs(target, exist_ok=True)
        open_folder(target)

    # ---------- 窗口 ----------
    def show_window(self) -> None:
        if self.window is None:
            from liverec.desktop import AppWindow

            self.window = AppWindow(self.url)
        mode = self.window.show()
        log("INFO", f"托盘唤回控制台（{mode}）{self.url}")

    def open_window(self) -> None:
        self.show_window()

    def close_window(self) -> None:
        if self.window is not None:
            self.window.close()


def find_running_instance(start: int = 8765, end: int = 8795) -> str | None:
    """如果已经有一个 LiveRec 在跑，返回它的地址（用于「重复双击只唤回窗口」）。"""
    import json
    import urllib.request

    for port in range(start, end):
        url = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(f"{url}/api/state", timeout=0.4) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("version"):
            return url
    return None


#: 解压目录的说明：这时日志文件还没接上，先存下来，等 FileSink 就绪再写一行
_RUNTIME_NOTE: tuple[str, str] | None = None


def _prepare_frozen_runtime() -> None:
    """单文件 exe 的解压目录固定到「程序所在目录\\runtime」。

    PyInstaller 的 ``runtime_tmpdir``（见 LiveRec.spec）写的是相对路径 ``runtime``，
    它是按**进程启动时的当前目录**解析的，所以这里先把工作目录切到 exe 所在目录，
    顺带让 data/ downloads/ 这些相对路径也统一落在程序目录下。
    另外清掉上次异常退出（崩溃/强杀）残留的 ``_MEIxxxx`` 解压目录。
    """
    if not getattr(sys, "frozen", False):
        return
    global _RUNTIME_NOTE
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    try:
        os.chdir(exe_dir)
    except OSError:
        pass

    runtime = os.path.join(exe_dir, "runtime")
    mei = os.path.abspath(getattr(sys, "_MEIPASS", "") or "")
    if mei and not mei.lower().startswith(runtime.lower()):
        _RUNTIME_NOTE = ("WARN", f"本次解压到了 {mei}，不在程序目录下的 runtime\\"
                                 "（从别的目录启动就会这样，建议双击程序目录里的 LiveRec.exe）")
        log(*_RUNTIME_NOTE)
        return

    # 只保留当前这次用的解压目录，其余 _MEIxxxx 都是上次残留
    try:
        for name in os.listdir(runtime):
            path = os.path.join(runtime, name)
            if not os.path.isdir(path):
                continue
            if not name.lower().startswith("_mei"):
                continue
            if mei and os.path.abspath(path).lower() == mei.lower():
                continue
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
    _RUNTIME_NOTE = ("INFO", f"解压目录：{runtime}")
    log(*_RUNTIME_NOTE)


def _enable_faulthandler() -> None:
    try:
        import liverec.paths as _paths
        log_dir = _paths.log_dir()
    except Exception:
        log_dir = "."
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = open(os.path.join(log_dir, "crash.log"), "a", buffering=1)
        faulthandler.enable(file=fh, all_threads=True)
    except Exception:
        pass


def main() -> int:
    _ensure_streams()
    _prepare_frozen_runtime()
    _enable_faulthandler()
    parser = argparse.ArgumentParser(description="LiveRec 控制台")
    parser.add_argument("--port", type=int, default=0, help="监听端口（默认自动挑选）")
    parser.add_argument("--headless", action="store_true", help="只跑服务，不开窗口")
    parser.add_argument("--no-window", action="store_true", help="同 --headless")
    parser.add_argument("--no-tray", action="store_true", help="不启用系统托盘（关窗即退出）")
    parser.add_argument("--tray-only", action="store_true", help="只驻留托盘，不自动开窗口")
    args = parser.parse_args()

    paths.seed_config()

    # 已经有一个实例在录：把窗口唤到前台就好，不要再起第二个录制进程
    if not args.headless and not args.no_window:
        existing = find_running_instance()
        if existing:
            log("INFO", f"检测到 LiveRec 已在运行（{existing}），直接唤回窗口")
            from liverec.desktop import AppWindow

            AppWindow(existing, "LiveRec 控制台").show()
            return 0

    from liverec import server as srv  # 延迟导入，确保配置已就绪

    if _RUNTIME_NOTE:  # 此刻日志文件才接上，补记本次的解压目录
        log(*_RUNTIME_NOTE)

    # ---- 启动画面：先开一个带进度条的窗口，服务就绪后自动跳到控制台 ----
    splash = None
    window_opened = False
    early_window = None
    if not (args.headless or args.no_window):
        try:
            from liverec.splash import Splash

            splash = Splash()
            splash_url = splash.start()
            if splash_url:
                from liverec.desktop import AppWindow

                early_window = AppWindow(splash_url, "LiveRec 控制台")
                early_window.show()
                window_opened = True
            splash.set(30, "正在启动服务…")
        except Exception as exc:  # noqa: BLE001 - 启动画面失败不能影响主流程
            log("WARN", f"启动画面不可用：{exc}")
            splash = None

    port = args.port or pick_port()
    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False)
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True, name="uvicorn")
    thread.start()

    if splash is not None:
        splash.set(55, "正在加载配置与直播间…")

    if not wait_port(port):
        print("服务启动失败，请检查端口占用或日志。", file=sys.stderr)
        if splash is not None:
            splash.set(100, "服务启动失败，请查看日志")
        return 1

    url = f"http://127.0.0.1:{port}"
    print(f"LiveRec 控制台已启动：{url}")
    print(f"数据目录：{paths.data_dir()}")

    if args.headless or args.no_window:
        log("INFO", f"服务已就绪（无窗口模式）{url}")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        uvicorn_server.should_exit = True
        return 0

    if splash is not None:
        splash.set(90, "即将打开控制台…")
        splash.ready(url)
        log("INFO", f"启动画面已切换到控制台（{url}）")

    if early_window is not None:
        # 启动画面那个窗口已经跳到控制台了：把它的地址换成控制台地址，
        # 否则 tray「打开控制台」还会去开启动画面的临时端口（几秒后就被关掉 → 无法访问此页）。
        early_window.url = url

    controller = Controller(srv, url)
    if early_window is not None:
        controller.window = early_window

    # ---- 托盘模式（默认）：关窗不退出，录制继续 ----
    return _run_with_tray(args, controller, uvicorn_server, thread,
                          window_opened=window_opened)


def _run_with_tray(args, controller: Controller, uvicorn_server, thread,
                   window_opened: bool = False) -> int:
    from liverec import tray as tray_mod

    enable_tray = not args.no_tray and tray_mod.available()
    if not enable_tray and not args.no_tray:
        log("WARN", "托盘依赖缺失，改为普通窗口模式（关窗即退出）")

    quitting = threading.Event()

    def _on_quit() -> None:
        quitting.set()
        controller.close_window()

    # 没有托盘：退回旧行为（窗口阻塞，关窗即退出）
    if not enable_tray:
        from liverec.desktop import open_window

        try:
            open_window(controller.url)
        except KeyboardInterrupt:
            log("INFO", "收到 Ctrl+C，正在退出…")
        finally:
            log("INFO", "正在停止录制并退出…")
            uvicorn_server.should_exit = True
            thread.join(timeout=15)
        return 0

    tray = tray_mod.Tray(controller, _on_quit)

    # 托盘占用主线程；窗口在后台线程里另起（Edge --app 窗口，关掉不影响后台）
    if not args.tray_only and not window_opened:
        def _open() -> None:
            time.sleep(0.4)
            try:
                controller.open_window()
            except Exception as exc:
                log("WARN", f"打开窗口失败：{exc}")

        threading.Thread(target=_open, daemon=True, name="window-open").start()

    try:
        result = tray.run()  # 阻塞，直到托盘「退出」
    except KeyboardInterrupt:
        log("INFO", "收到 Ctrl+C，正在退出…")
        result = "quit"

    if result != "quit":
        # 托盘没跑起来：退回普通窗口模式，别把正在录制的任务一起带走
        log("WARN", "托盘不可用，改为普通窗口模式（关掉窗口即退出）")
        try:
            proc = getattr(controller.window, "proc", None) if controller.window else None
            if proc is not None:
                proc.wait()
            else:
                while not quitting.is_set():
                    time.sleep(0.5)
        except KeyboardInterrupt:
            log("INFO", "收到 Ctrl+C，正在退出…")

    tray.stop()
    controller.close_window()
    log("INFO", "正在停止录制并退出…")
    uvicorn_server.should_exit = True
    thread.join(timeout=15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
