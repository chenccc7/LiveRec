"""虎牙弹幕客户端：TARS over WebSocket。

协议移植自 biliup（Rust）的 `crates/danmaku/src/protocols/huya.rs`，
要点：
- 连 `wss://cdnws.api.huya.com/`；
- uid 从 PC 页面 `https://www.huya.com/<room>` 的正则 `uid['"]*:\\s*['"]*(\\d+)` 取；
- 注册包 = `WebSocketCommand{iCmdType=1, vData=WSUserInfo}`，
  `WSUserInfo{lUid, bAnonymous=false, sGuid="", sToken="", lTid=0, lSid=0,
  lGroupId=uid, lGroupType=3}`（tid/sid 传 0、groupType=3 是关键，
  用真实 tid/sid 反而不推送）；
- 心跳是写死的 112 字节预编码包，60 秒一次；
- 推送：`cmd_type==7` → tag1 的 bytes 里 `read_int64(1)==1400` 是弹幕 →
  tag2 的 bytes → `MessageNotice` 的 tag0 struct 内 tag2 是昵称、tag3 是内容。
"""
from __future__ import annotations

import asyncio
import re
import struct
import time
from typing import Callable

from ..logbook import log

WSS_URL = "wss://cdnws.api.huya.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

CMD_REGISTER_REQ = 1
CMD_REGISTER_RSP = 2
CMD_HEARTBEAT = 5
CMD_HEARTBEAT_ACK = 6
CMD_PUSH = 7
MSG_TYPE_DANMAKU = 1400
HEARTBEAT_INTERVAL = 60.0

# 与 biliup 的 HEARTBEAT 常量逐字节一致（含 OnUserHeartBeat / adr_wap / tReq）
HEARTBEAT = bytes([
    0x00, 0x03, 0x1d, 0x00, 0x00, 0x69, 0x00, 0x00, 0x00, 0x69, 0x10, 0x03, 0x2c, 0x3c, 0x4c, 0x56,
    0x08, 0x6f, 0x6e, 0x6c, 0x69, 0x6e, 0x65, 0x75, 0x69, 0x66, 0x0f, 0x4f, 0x6e, 0x55, 0x73, 0x65,
    0x72, 0x48, 0x65, 0x61, 0x72, 0x74, 0x42, 0x65, 0x61, 0x74, 0x7d, 0x00, 0x00, 0x3c, 0x08, 0x00,
    0x01, 0x06, 0x04, 0x74, 0x52, 0x65, 0x71, 0x1d, 0x00, 0x00, 0x2f, 0x0a, 0x0a, 0x0c, 0x16, 0x00,
    0x26, 0x00, 0x36, 0x07, 0x61, 0x64, 0x72, 0x5f, 0x77, 0x61, 0x70, 0x46, 0x00, 0x0b, 0x12, 0x03,
    0xae, 0xf0, 0x0f, 0x22, 0x03, 0xae, 0xf0, 0x0f, 0x3c, 0x42, 0x6d, 0x52, 0x02, 0x60, 0x5c, 0x60,
    0x01, 0x7c, 0x82, 0x00, 0x0b, 0xb0, 0x1f, 0x9c, 0xac, 0x0b, 0x8c, 0x98, 0x0c, 0xa8, 0x0c, 0x20,
])

# TARS 类型
_T_INT8, _T_INT16, _T_INT32, _T_INT64 = 0, 1, 2, 3
_T_STRING1, _T_STRING4 = 6, 7
_T_MAP, _T_LIST, _T_STRUCT_BEGIN, _T_STRUCT_END = 8, 9, 10, 11
_T_ZERO, _T_BYTES = 12, 13


class _Out:
    """TARS 输出流（只实现虎牙弹幕需要的那几个写方法）。"""

    def __init__(self) -> None:
        self.buf = bytearray()

    def _head(self, tag: int, kind: int) -> None:
        if tag < 15:
            self.buf.append((tag << 4) | kind)
        else:
            self.buf.append(0xF0 | kind)
            self.buf.append(tag)

    def int8(self, tag: int, value: int) -> None:
        if value == 0:
            self._head(tag, _T_ZERO)
            return
        self._head(tag, _T_INT8)
        self.buf.append(value & 0xFF)

    def int16(self, tag: int, value: int) -> None:
        if -128 <= value <= 127:
            self.int8(tag, value)
            return
        self._head(tag, _T_INT16)
        self.buf += struct.pack(">h", value)

    def int32(self, tag: int, value: int) -> None:
        if -32768 <= value <= 32767:
            self.int16(tag, value)
            return
        self._head(tag, _T_INT32)
        self.buf += struct.pack(">i", value)

    def int64(self, tag: int, value: int) -> None:
        if -2147483648 <= value <= 2147483647:
            self.int32(tag, value)
            return
        self._head(tag, _T_INT64)
        self.buf += struct.pack(">q", value)

    def string(self, tag: int, value: str) -> None:
        raw = value.encode("utf-8")
        if len(raw) <= 255:
            self._head(tag, _T_STRING1)
            self.buf.append(len(raw))
        else:
            self._head(tag, _T_STRING4)
            self.buf += struct.pack(">I", len(raw))
        self.buf += raw

    def bytes_(self, tag: int, value: bytes) -> None:
        self._head(tag, _T_BYTES)
        self._head(0, _T_INT8)          # SIMPLE_LIST 的元素类型
        self.int32(0, len(value))
        self.buf += value


class _In:
    """TARS 输入流（含按类型跳字段，遇到畸形数据直接放弃剩余的）。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _peek_head(self):
        if self.pos >= len(self.data):
            return None
        byte = self.data[self.pos]
        tag, kind = (byte >> 4) & 0x0F, byte & 0x0F
        if tag >= 15:
            if self.pos + 1 >= len(self.data):
                return None
            return self.data[self.pos + 1], kind
        return tag, kind

    def _read_head(self):
        if self.pos >= len(self.data):
            return None
        byte = self.data[self.pos]
        tag, kind = (byte >> 4) & 0x0F, byte & 0x0F
        self.pos += 1
        if tag >= 15:
            if self.pos >= len(self.data):
                return None
            tag = self.data[self.pos]
            self.pos += 1
        return tag, kind

    def _advance(self, n: int) -> None:
        self.pos = min(self.pos + max(n, 0), len(self.data))

    def _abandon(self) -> None:
        self.pos = len(self.data)

    def _skip(self, kind: int, depth: int = 0) -> None:
        if depth > 64:
            self._abandon()
            return
        if kind == _T_INT8:
            self._advance(1)
        elif kind == _T_INT16:
            self._advance(2)
        elif kind == _T_INT32:
            self._advance(4)
        elif kind == _T_INT64:
            self._advance(8)
        elif kind in (4, 5):                     # float / double
            self._advance(4 if kind == 4 else 8)
        elif kind == _T_STRING1:
            if self.pos < len(self.data):
                size = self.data[self.pos]
                self._advance(1)
                self._advance(size)
        elif kind == _T_STRING4:
            if self.pos + 4 <= len(self.data):
                size = struct.unpack(">I", self.data[self.pos:self.pos + 4])[0]
                self._advance(4)
                self._advance(size)
        elif kind in (_T_MAP, _T_LIST):
            size = self._read_int32_inner() or 0
            count = size * 2 if kind == _T_MAP else size
            for _ in range(max(count, 0)):
                head = self._read_head()
                if head is None:
                    break
                self._skip(head[1], depth + 1)
        elif kind == _T_BYTES:
            self._read_head()                    # 内层元素类型
            size = self._read_int32_inner() or 0
            self._advance(size)
        elif kind == _T_STRUCT_BEGIN:
            self._skip_struct_end(depth + 1)
        else:                                    # STRUCT_END / ZERO
            pass

    def _skip_struct_end(self, depth: int = 0) -> None:
        while True:
            head = self._read_head()
            if head is None:
                return
            if head[1] == _T_STRUCT_END:
                return
            self._skip(head[1], depth)

    def _skip_to_tag(self, target: int) -> bool:
        while self.pos < len(self.data):
            head = self._peek_head()
            if head is None:
                return False
            tag, kind = head
            if kind == _T_STRUCT_END:
                return False
            if tag == target:
                return True
            if tag > target:
                return False
            self._read_head()
            self._skip(kind)
        return False

    def _read_int32_inner(self):
        head = self._read_head()
        if head is None:
            return None
        kind = head[1]
        if kind == _T_ZERO:
            return 0
        if kind == _T_INT8 and self.pos < len(self.data):
            value = struct.unpack(">b", self.data[self.pos:self.pos + 1])[0]
            self.pos += 1
            return value
        if kind == _T_INT16 and self.pos + 2 <= len(self.data):
            value = struct.unpack(">h", self.data[self.pos:self.pos + 2])[0]
            self.pos += 2
            return value
        if kind == _T_INT32 and self.pos + 4 <= len(self.data):
            value = struct.unpack(">i", self.data[self.pos:self.pos + 4])[0]
            self.pos += 4
            return value
        return None

    def int64(self, tag: int):
        if not self._skip_to_tag(tag):
            return None
        head = self._read_head()
        if head is None:
            return None
        kind = head[1]
        if kind == _T_ZERO:
            return 0
        sizes = {_T_INT8: (1, ">b"), _T_INT16: (2, ">h"),
                 _T_INT32: (4, ">i"), _T_INT64: (8, ">q")}
        if kind in sizes:
            size, fmt = sizes[kind]
            if self.pos + size <= len(self.data):
                value = struct.unpack(fmt, self.data[self.pos:self.pos + size])[0]
                self.pos += size
                return value
            return None
        self._skip(kind)
        return None

    def int32(self, tag: int):
        value = self.int64(tag)
        return int(value) if value is not None else None

    def string(self, tag: int) -> str:
        if not self._skip_to_tag(tag):
            return ""
        head = self._read_head()
        if head is None:
            return ""
        kind = head[1]
        if kind == _T_STRING1 and self.pos < len(self.data):
            size = self.data[self.pos]
            self.pos += 1
            raw = self.data[self.pos:self.pos + size]
            self.pos = min(self.pos + size, len(self.data))
            return raw.decode("utf-8", "replace")
        if kind == _T_STRING4 and self.pos + 4 <= len(self.data):
            size = struct.unpack(">I", self.data[self.pos:self.pos + 4])[0]
            self.pos += 4
            raw = self.data[self.pos:self.pos + size]
            self.pos = min(self.pos + size, len(self.data))
            return raw.decode("utf-8", "replace")
        self._skip(kind)
        return ""

    def bytes_(self, tag: int):
        if not self._skip_to_tag(tag):
            return None
        head = self._read_head()
        if head is None or head[1] != _T_BYTES:
            if head is not None:
                self._skip(head[1])
            return None
        self._read_head()                        # 元素类型
        size = self._read_int32_inner()
        if not isinstance(size, int) or size < 0:
            return None
        raw = self.data[self.pos:self.pos + size]
        self.pos = min(self.pos + size, len(self.data))
        return raw

    def struct(self, tag: int, reader: Callable[["_In"], object]):
        if not self._skip_to_tag(tag):
            return None
        head = self._read_head()
        if head is None:
            return None
        if head[1] != _T_STRUCT_BEGIN:
            self._skip(head[1])
            return None
        value = reader(self)
        self._skip_struct_end()
        return value


def _build_user_info(uid: int) -> bytes:
    out = _Out()
    out.int64(0, uid)          # lUid
    out.int8(1, 0)             # bAnonymous = false
    out.string(2, "")          # sGuid
    out.string(3, "")          # sToken
    out.int64(4, 0)            # lTid
    out.int64(5, 0)            # lSid
    out.int64(6, uid)          # lGroupId
    out.int64(7, 3)            # lGroupType
    return bytes(out.buf)


def _build_command(cmd_type: int, data: bytes) -> bytes:
    out = _Out()
    out.int32(0, cmd_type)
    out.bytes_(1, data)
    return bytes(out.buf)


def parse_push(data: bytes) -> list[dict]:
    """把一帧 WebSocket 消息解成弹幕事件列表。"""
    events: list[dict] = []
    outer = _In(data)
    if outer.int32(0) != CMD_PUSH:
        return events
    inner_raw = outer.bytes_(1)
    if not inner_raw:
        return events
    inner = _In(inner_raw)
    if inner.int64(1) != MSG_TYPE_DANMAKU:
        return events
    body = inner.bytes_(2)
    if not body:
        return events

    notice = _In(body)

    def read_sender(sender: "_In"):
        return sender.string(2)

    name = notice.struct(0, read_sender) or ""
    content = notice.string(3)

    if content:
        events.append({
            "type": "chat",
            "user_id": "",
            "nickname": str(name),
            "content": content,
        })
    return events


def room_id_from_url(url: str) -> str:
    match = re.search(r"huya\.com/(?:topic/)?([A-Za-z0-9_]+)", url or "")
    return match.group(1) if match else ""


class HuyaDanmaku:
    supported = True
    reason = ""

    def __init__(self, url: str, cookie: str = ""):
        self.url = url
        self.cookie = cookie
        self.room = room_id_from_url(url)

    def _fetch_uid(self) -> int:
        import requests

        headers = {"User-Agent": UA}
        if self.cookie:
            headers["Cookie"] = self.cookie
        resp = requests.get(f"https://www.huya.com/{self.room}",
                            headers=headers, timeout=15)
        resp.encoding = "utf-8"
        match = re.search(r"uid['\"]*:\s*['\"]*(\d+)", resp.text or "")
        if not match:
            raise RuntimeError("虎牙房间页没有取到 uid（可能被风控）")
        return int(match.group(1))

    async def run(self, emit: Callable[[dict], None]) -> None:
        import websockets

        uid = await asyncio.to_thread(self._fetch_uid)
        headers = {"User-Agent": UA, "Origin": "https://www.huya.com",
                   "Referer": f"https://www.huya.com/{self.room}"}
        log("INFO", f"[弹幕/虎牙] 连接 {WSS_URL}（房间 {self.room}，uid {uid}）")
        async with websockets.connect(WSS_URL, additional_headers=headers,
                                      max_size=2 ** 23, ping_interval=None,
                                      open_timeout=15) as ws:
            await ws.send(_build_command(CMD_REGISTER_REQ, _build_user_info(uid)))
            next_beat = time.time() + HEARTBEAT_INTERVAL
            while True:
                timeout = max(next_beat - time.time(), 0.2)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    await ws.send(HEARTBEAT)
                    next_beat = time.time() + HEARTBEAT_INTERVAL
                    continue
                if isinstance(raw, str):
                    continue
                for event in parse_push(bytes(raw)):
                    emit(event)
                if time.time() >= next_beat:
                    await ws.send(HEARTBEAT)
                    next_beat = time.time() + HEARTBEAT_INTERVAL
