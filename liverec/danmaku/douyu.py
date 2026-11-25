"""斗鱼（douyu）弹幕客户端：STT 协议 + 原生 TCP 长连。

包结构：4 字节长度 L + 4 字节重复 L + 2 字节类型 + 1 加密 + 1 保留 + 负载。
负载以 \\0 结尾，L = 负载长度 + 8，整包字节数 = L + 4。
字段编码为 `key@=value/`，其中 `/` 用 `@S`、`@` 用 `@A` 转义。

注意：斗鱼的弹幕服务器（danmuproxy.douyu.com）在 8501-8506 上**不是** WSS，
TLS 握手会被直接拒绝；真正的弹幕入口是 8601 / 8602 上的裸 TCP STT 连接。
"""
from __future__ import annotations

import asyncio
import re
import struct
import time
from typing import Callable

from ..logbook import log

HOST = "danmuproxy.douyu.com"
PORTS = (8601, 8602, 8506, 8504, 8502)
MSG_LOGIN = 689
HEARTBEAT = 30.0


def _pack(text: str, code: int = MSG_LOGIN) -> bytes:
    payload = text.encode("utf-8") + b"\0"
    length = 8 + len(payload)
    return (struct.pack("<ii", length, length)
            + struct.pack("<hbb", code, 0, 0)
            + payload)


def _split(buf: bytes):
    messages = []
    while len(buf) >= 12:
        length = struct.unpack("<i", buf[:4])[0]
        if length < 8 or length > 4 * 1024 * 1024:
            buf = b""
            break
        total = length + 4
        if len(buf) < total:
            break
        messages.append(buf[12:total])
        buf = buf[total:]
    return messages, buf


def _unescape(value: str) -> str:
    return value.replace("@S", "/").replace("@A", "@")


def _fields(payload: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in payload.split("/"):
        if "@=" in chunk:
            key, val = chunk.split("@=", 1)
            out[key] = _unescape(val)
    return out


class DouyuDanmaku:
    supported = True
    reason = ""

    def __init__(self, url: str, cookie: str = ""):
        self.url = url
        self.cookie = cookie
        m = re.search(r"rid=(\d+)", url)
        if not m:
            m = re.search(r"douyu\.com/(?:topic/)?(\d+)", url)
        self.rid = m.group(1) if m else ""
        self._nick = ""

    async def run(self, emit: Callable[[dict], None]) -> None:
        last_error: Exception | None = None
        for port in PORTS:
            try:
                await self._connect(port, emit)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 换下一个端口
                last_error = exc
                log("WARN", f"[弹幕/斗鱼] {HOST}:{port} 连接中断：{type(exc).__name__}: {exc}")
                await asyncio.sleep(1)
        raise RuntimeError(f"斗鱼弹幕连接失败：{last_error}")

    async def _connect(self, port: int, emit: Callable[[dict], None]) -> None:
        log("INFO", f"[弹幕/斗鱼] 连接 tcp://{HOST}:{port}（房间 {self.rid}）")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(HOST, port), timeout=15)
        try:
            writer.write(_pack(
                f"type@=loginreq/roomid@={self.rid}/ver@=20190610/aver@=218101901/"))
            writer.write(_pack(f"type@=joingroup/rid@={self.rid}/gid@=-9999/"))
            await writer.drain()

            buf = b""
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(8192), timeout=HEARTBEAT)
                except asyncio.TimeoutError:
                    writer.write(_pack(f"type@=keeplive/tick@={int(time.time())}/"))
                    await writer.drain()
                    continue
                if not chunk:
                    raise ConnectionError("斗鱼服务端关闭了连接")
                messages, buf = _split(buf + chunk)
                for payload in messages:
                    self._handle(payload, writer, emit)
                if messages:
                    await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    def _handle(self, payload: bytes, writer, emit: Callable[[dict], None]) -> None:
        text = payload.decode("utf-8", "ignore").strip("\0").strip()
        if not text:
            return
        f = _fields(text)
        kind = f.get("type", "")
        if kind == "loginres":
            log("INFO", f"[弹幕/斗鱼] 已登录弹幕服务器（房间 {self.rid}）")
        elif kind == "pingreq":
            # 服务端探活，官方网页端回一个 mrkl
            writer.write(_pack("type@=mrkl/"))
        elif kind == "chatmsg":
            emit({
                "type": "chat",
                "user_id": f.get("uid", ""),
                "nickname": f.get("nn", ""),
                "content": f.get("txt", ""),
            })
        elif kind == "dgb":
            emit({
                "type": "gift",
                "user_id": f.get("uid", ""),
                "nickname": f.get("nn", ""),
                "gift_name": f.get("gfn", "") or f.get("gfid", ""),
                "gift_count": _to_int(f.get("gfcnt") or f.get("hits")),
                "content": f"{f.get('gfn', '')} x{f.get('gfcnt', '1')}",
            })
        elif kind == "uenter":
            emit({
                "type": "enter",
                "user_id": f.get("uid", ""),
                "nickname": f.get("nn", ""),
                "content": "进入直播间",
            })
        elif kind == "ssd":
            emit({
                "type": "super_chat",
                "user_id": f.get("uid", ""),
                "nickname": f.get("nn", ""),
                "content": f.get("content", ""),
            })
        # rss / oni / newblackres 等刷新类消息忽略


def _to_int(value) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0
