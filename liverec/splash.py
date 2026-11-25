"""启动进度窗口。

主程序起来之前先开一个极小的本地服务，页面是一个带进度条的启动画面；等后端
uvicorn 就绪后把页面跳到真正的控制台，然后关闭这个小服务。

窗口本身用的是 Edge/Chrome 的 `--app` 模式，图标取自本页的 favicon（和 exe 同一张
`bin/liverec.ico`），所以标题栏、任务栏、托盘看起来是一套图标。
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import paths

_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8" />
<link rel="icon" type="image/x-icon" href="/icon.ico" />
<title>LiveRec 正在启动…</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    display: flex; align-items: center; justify-content: center;
    background: radial-gradient(1200px 500px at 50% -10%, #1b2333 0%, #0a0d13 60%);
    color: #e8edf7; font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    -webkit-user-select: none; user-select: none;
  }
  .wrap { width: 420px; text-align: center; }
  .logo { width: 72px; height: 72px; border-radius: 20px; margin: 0 auto 18px;
          background: #ff4d5e; display: flex; align-items: center; justify-content: center;
          box-shadow: 0 12px 34px rgba(255,77,94,.36); }
  .logo i { width: 34px; height: 34px; border-radius: 50%; border: 5px solid #fff;
            display: block; }
  h1 { font-size: 20px; font-weight: 650; letter-spacing: .2px; margin: 0 0 6px; }
  .sub { font-size: 12.5px; color: #7d879b; letter-spacing: 2.4px; margin-bottom: 26px; }
  .bar { height: 6px; border-radius: 999px; background: #1a2030; overflow: hidden; }
  .bar > i { display: block; height: 100%; width: 8%; border-radius: 999px;
             background: linear-gradient(90deg, #ff4d5e, #ff8a5c);
             transition: width .45s cubic-bezier(.2,.8,.2,1); }
  .tip { margin-top: 12px; font-size: 12.5px; color: #8b95a8; height: 18px; }
  .ver { position: fixed; bottom: 18px; font-size: 11.5px; color: #4c5566; letter-spacing: .4px; }
</style></head>
<body>
  <div class="wrap">
    <div class="logo"><i></i></div>
    <h1>LiveRec 控制台</h1>
    <div class="sub">DOUYIN LIVE RECORDER</div>
    <div class="bar"><i id="p"></i></div>
    <div class="tip" id="t">正在启动程序…</div>
  </div>
  <div class="ver">v0.1.0 · core v4.0.7</div>
<script>
  const p = document.getElementById('p'), t = document.getElementById('t');
  let target = 8, shown = 8, jumping = false;
  setInterval(() => {
    if (shown < target) shown = Math.min(target, shown + Math.max(0.6, (target - shown) / 6));
    p.style.width = shown.toFixed(1) + '%';
  }, 60);
  async function poll() {
    try {
      const r = await fetch('/state', { cache: 'no-store' });
      const s = await r.json();
      if (typeof s.pct === 'number') target = s.pct;
      if (s.text) t.textContent = s.text;
      if (s.url && !jumping) { jumping = true; target = 100; t.textContent = '正在打开控制台…';
        setTimeout(() => location.replace(s.url), 380); return; }
    } catch (e) {}
    setTimeout(poll, 220);
  }
  poll();
</script>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D401
        return

    def do_GET(self) -> None:  # noqa: N802
        server = self.server
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = _HTML.encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif path == "/state":
            body = json.dumps(server.state, ensure_ascii=False).encode("utf-8")  # type: ignore[attr-defined]
            self._send(200, "application/json; charset=utf-8", body)
        elif path == "/icon.ico":
            icon = _icon_bytes()
            if icon:
                self._send(200, "image/x-icon", icon)
            else:
                self._send(404, "text/plain", b"icon missing")
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


_ICON_CACHE: bytes | None = None


def _icon_bytes() -> bytes:
    global _ICON_CACHE
    if _ICON_CACHE is None:
        for candidate in (os.path.join(paths.app_root(), "bin", "liverec.ico"),
                          os.path.join(paths.bundle_root(), "bin", "liverec.ico")):
            if os.path.exists(candidate):
                try:
                    with open(candidate, "rb") as fh:
                        _ICON_CACHE = fh.read()
                    break
                except OSError:
                    continue
        else:
            _ICON_CACHE = b""
    return _ICON_CACHE


class Splash:
    """启动画面：本地起一个只服务「进度页」的小服务。"""

    def __init__(self) -> None:
        self.state: dict[str, object] = {"pct": 10, "text": "正在启动程序…", "url": ""}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            return ""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="splash", daemon=True)
        self._thread.start()
        return self.url

    def set(self, pct: int, text: str) -> None:
        self.state["pct"] = max(0, min(100, int(pct)))
        if text:
            self.state["text"] = text

    def ready(self, url: str, linger: float = 8.0) -> None:
        self.state["pct"] = 100
        self.state["text"] = "正在打开控制台…"
        self.state["url"] = url

        def _later() -> None:
            time.sleep(linger)
            self.stop()

        threading.Thread(target=_later, daemon=True, name="splash-stop").start()

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
