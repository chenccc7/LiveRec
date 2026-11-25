"""本地 HLS 中转。

背景：B站部分房间（以及个别 CDN）只提供 HLS-fMP4，且播放列表里的分片是**相对 URI**
（如 `628318389.m4s`、`#EXT-X-MAP:URI="h1795540126.m4s"`）。ffmpeg 解析相对 URI 时会把
playlist 上的签名 query 丢掉，导致分片请求 403 / 0 字节。

做法：在本机起一个极小的 HTTP 服务，把上游 playlist 的每条 URI 改写成「绝对地址 + 原
query」并挂到 /seg;请求分片时再透传上游字节。录制端只需把 m3u8 地址换成这个本地地址。
"""
from __future__ import annotations

import base64
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict

import requests
import urllib3

# 直播 CDN 的证书链经常不完整（虎牙的 tx.hls 就是这样），本机中转统一不校验，
# 否则 requests 会抛 SSLError。这个中转只连「直播流地址」，不承载其它请求。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_URI_RE = re.compile(r'URI="([^"]+)"')
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class _State:
    """播放列表与分片的共享状态（一个代理实例一份）。"""

    def __init__(self, url: str, headers: dict[str, str], timeout: float = 15.0,
                 signer: Callable[[str], str] | None = None) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        # 有些平台（虎牙）每次请求都要现算签名，播放列表每刷新一次就得重签一次
        self.signer = signer
        self.segments: Dict[str, str] = {}
        self.last_text = ""
        self.lock = threading.Lock()

    def register(self, absolute: str) -> str:
        key = base64.urlsafe_b64encode(absolute.encode("utf-8")).decode("ascii")
        path = urllib.parse.urlsplit(absolute).path
        # ffmpeg 会按扩展名白名单校验分片地址，这里保留原扩展名（.m4s/.ts/...）
        ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else "bin"
        if len(ext) > 5 or not ext.isalnum():
            ext = "bin"
        with self.lock:
            if len(self.segments) > 8000:
                # 长时间录制时只保留最近的一批，避免无上限增长
                for old in list(self.segments)[:4000]:
                    self.segments.pop(old, None)
            self.segments[key] = absolute
        return f"/seg/{key}.{ext}"


def _rewrite(state: _State, text: str) -> str:
    base = state.url.split("?")[0]
    query = "?" + state.url.split("?", 1)[1] if "?" in state.url else ""
    folder = base.rsplit("/", 1)[0]

    def absolute(uri: str) -> str:
        if uri.startswith("http://") or uri.startswith("https://"):
            return uri
        if uri.startswith("//"):
            return "https:" + uri
        # 分片自己带查询串（虎牙每个分片都有自己的 wsSecret）时不能再拼播放列表的签名，
        # 否则会变成 `?wsSecret=A&...?wsSecret=B&...`，CDN 直接 403。
        tail = "" if "?" in uri else query
        if uri.startswith("/"):
            root = urllib.parse.urlsplit(base)
            return f"{root.scheme}://{root.netloc}{uri}" + tail
        return f"{folder}/{uri}" + tail

    out: list[str] = []

    def safe_register(uri: str) -> str:
        """注册分片；解析不了的行原样保留（虎牙偶尔会给出带方括号的坏 URI）。"""
        try:
            return state.register(uri)
        except Exception:  # noqa: BLE001 - 保住整篇播放列表比纠一条坏行重要
            return uri

    for line in text.splitlines():
        if line.startswith("#"):
            match = _URI_RE.search(line)
            if match:
                target = safe_register(absolute(match.group(1)))
                line = line.replace(match.group(1), target)
            out.append(line)
        elif line.strip():
            raw = line.strip()
            if not raw.startswith(("http://", "https://", "//", "/")) and "://" not in raw:
                # 纯文件名/相对路径才需要拼前缀；其它形态（含非法字符）原样放行
                pass
            out.append(safe_register(absolute(raw)))
        else:
            out.append(line)
    return "\n".join(out) + "\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D401 静默
        return

    # 让 ffmpeg 的 keep-alive 请求能复用
    def do_GET(self) -> None:  # noqa: N802
        # ffmpeg 经常在分片写到一半就断开（关窗口 / 换线路 / 分段边界），
        # 这里统一吞掉断连异常，否则 socketserver 会往 stderr 刷一大段 traceback。
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _handle_get(self) -> None:
        state: _State = self.server.state  # type: ignore[attr-defined]
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in ("/live.m3u8", "/index.m3u8"):
            self._playlist(state)
        elif parsed.path.startswith("/seg/"):
            key = parsed.path[len("/seg/"):].split(".", 1)[0]
            target = state.segments.get(key, "")
            if not target:
                self._fail(404, b"segment unknown")
                return
            self._relay(state, target)
        else:
            self._fail(404, b"not found")

    def _playlist(self, state: _State) -> None:
        # 虎牙这类 CDN 的签名基本是「一次一签」，同一地址重放就会 403，
        # 所以每次刷新都重新签名，并且失败时再试两次（每次都是新的签名）。
        resp = None
        target = state.url
        for _attempt in range(3):
            candidate = state.url
            if state.signer is not None:
                try:
                    signed = state.signer(candidate)
                    if signed:
                        candidate = signed
                except Exception:  # noqa: BLE001 签名失败就沿用原地址
                    pass
            try:
                resp = requests.get(candidate, headers=state.headers, timeout=state.timeout,
                                    verify=False)
            except Exception:  # noqa: BLE001
                resp = None
                continue
            target = candidate
            if resp.status_code == 200:
                break
        if resp is None:
            self._fail(502, b"upstream error")
            return
        if resp.status_code != 200:
            # 拿不到新列表时，把上一次成功的内容再发一遍，让 ffmpeg 继续等新分片，
            # 比直接 502 让整条记录断掉要好。
            last = getattr(state, "last_text", "")
            if last:
                self._send_playlist(state, last)
                return
            self._fail(502, f"upstream {resp.status_code}".encode())
            return
        text = resp.text or ""
        # 主播放列表（master）指向的子列表常是「一次性」地址，透传必 403；
        # 这里自己跟一跳，把真正带分片的 media 列表取回来。
        for _ in range(2):
            if "#EXT-X-STREAM-INF" not in text:
                break
            child = next((ln.strip() for ln in text.splitlines()
                          if ln.strip() and not ln.startswith("#")), "")
            if not child:
                break
            child = child if child.startswith("http") else urllib.parse.urljoin(target, child)
            if state.signer is not None:
                try:
                    child = state.signer(child) or child
                except Exception:  # noqa: BLE001
                    pass
            try:
                sub = requests.get(child, headers=state.headers, timeout=state.timeout, verify=False)
            except Exception as exc:  # noqa: BLE001
                self._fail(502, f"child error: {exc}".encode())
                return
            if sub.status_code != 200:
                self._fail(502, f"child {sub.status_code}".encode())
                return
            target, text = child, (sub.text or "")
        # 用当次真正请求的地址来拼绝对地址（签名参数跟着变）
        view = _State(target, state.headers, state.timeout)
        view.segments = state.segments
        view.lock = state.lock
        state.last_text = text
        self._send_playlist(state, text, target)

    def _send_playlist(self, state: _State, text: str, base_url: str = "") -> None:
        view = _State(base_url or state.url, state.headers, state.timeout)
        view.segments = state.segments
        view.lock = state.lock
        body = _rewrite(view, text).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _relay(self, state: _State, target: str) -> None:
        # 分片地址里又出现 m3u8（主播放列表 → 子列表）时，仍然走「重新取 + 改写」，
        # 不然子列表会带着上一跳的签名去请求，几秒后必 403。
        if urllib.parse.urlsplit(target).path.lower().endswith(".m3u8"):
            view = _State(target, state.headers, state.timeout, signer=state.signer)
            view.segments = state.segments
            view.lock = state.lock
            self._playlist(view)
            return
        try:
            resp = requests.get(target, headers=state.headers, timeout=state.timeout, stream=True, verify=False)
        except Exception as exc:  # noqa: BLE001
            self._fail(502, f"segment error: {exc}".encode())
            return
        if resp.status_code != 200:
            self._fail(resp.status_code, b"upstream segment failed")
            return
        self.send_response(200)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "application/octet-stream"))
        length = resp.headers.get("Content-Length")
        if length:
            self.send_header("Content-Length", length)
        self.end_headers()
        try:
            for chunk in resp.iter_content(65536):
                if chunk:
                    self.wfile.write(chunk)
        except Exception:  # noqa: BLE001 客户端断开时静默
            pass
        finally:
            resp.close()

    def _fail(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HlsProxy:
    """把一个 m3u8 地址包装成本地可直连的地址。"""

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 signer: Callable[[str], str] | None = None) -> None:
        self.state = _State(url, dict(headers or {}), signer=signer)
        self.state.headers.setdefault("User-Agent", _DEFAULT_UA)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def local_url(self) -> str:
        if self._server is None:
            raise RuntimeError("HlsProxy 尚未启动")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/live.m3u8"

    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, name="hlsproxy", daemon=True)
        self._thread.start()
        return self.local_url

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
