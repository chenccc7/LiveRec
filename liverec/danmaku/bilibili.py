"""B站（bilibili）弹幕客户端：公开 danmu API + WSS 长连。

协议：16 字节包头 (total_len:4, header_len:2, protover:2, op:4, seq:4)，
protover 2 = zlib，3 = brotli，0/1 = 明文 JSON，op 5 = 通知，op 7 = 认证，op 2 = 心跳。

实现要照 bilibili 网页端（参考 xfgryujk/blivedm 的 web 客户端）：
1. 先拿 buvid3（`https://www.bilibili.com/` 的 cookie），认证包里必须带上；
2. `getDanmuInfo` 现在要 wbi 签名（`nav` 取 img_key/sub_key 打乱成口令，拼 `wts` 后取 md5 作 `w_rid`），
   否则返回 -352 风控，只能退回老的 `getConf` 接口拿 token；
3. `DANMU_MSG` 的 `info` 在**消息顶层**（不在 `data` 里），聊天内容/昵称都从 `info` 取。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import struct
import time
import urllib.parse
from typing import Any, Callable

import httpx
import websockets

from ..logbook import log

HEAD_LEN = 16
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
API_INIT = "https://api.live.bilibili.com/room/v1/Room/room_init"
API_DANMU = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
API_CONF = "https://api.live.bilibili.com/room/v1/Danmu/getConf"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
BUVID_INIT = "https://www.bilibili.com/"
# wbi 口令的取值顺序（网页端写死的混淆表）
WBI_IDX = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
           27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13]
_wbi_cache: dict[str, Any] = {"key": "", "at": 0.0}


def _wbi_key_from_nav(nav: dict) -> str:
    wbi = ((nav.get("data") or {}).get("wbi_img") or {})
    img_url = str(wbi.get("img_url") or "")
    sub_url = str(wbi.get("sub_url") or "")
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        return ""
    raw = img_key + sub_key
    return "".join(raw[i] for i in WBI_IDX if i < len(raw))


def _wbi_sign(params: dict[str, Any], key: str) -> dict[str, Any]:
    if not key:
        return dict(params)
    signed = {**params, "wts": str(int(time.time()))}
    signed = {k: "".join(ch for ch in str(v) if ch not in "!'()*")
              for k, v in signed.items()}
    query = urllib.parse.urlencode(dict(sorted(signed.items())))
    signed["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return signed



def _pack(op: int, body: bytes) -> bytes:
    return struct.pack(">IHHII", HEAD_LEN + len(body), HEAD_LEN, 1, op, 1) + body


def _split(buf: bytes):
    """切分粘包，返回 (packets, rest)。"""
    packets = []
    while len(buf) >= HEAD_LEN:
        total, hlen, ver, op, _seq = struct.unpack(">IHHII", buf[:HEAD_LEN])
        if total < hlen or total > 8 * 1024 * 1024 or len(buf) < total:
            break
        packets.append((ver, op, buf[hlen:total]))
        buf = buf[total:]
    return packets, buf


def _decompress(ver: int, body: bytes) -> bytes:
    if ver == 2:
        import zlib

        return zlib.decompress(body)
    if ver == 3:
        import brotli

        return brotli.decompress(body)
    return body


class BilibiliDanmaku:
    supported = True

    def __init__(self, url: str, cookie: str = ""):
        self.url = url
        self.cookie = cookie
        self._buvid = ""
        m = re.search(r"live\.bilibili\.com/(?:blanc/)?(\d+)", url) or re.search(r"/(\d+)", url)
        self.short_id = m.group(1) if m else ""

    def _headers(self) -> dict[str, str]:
        h = {
            "User-Agent": UA,
            "Referer": "https://live.bilibili.com/",
            "Origin": "https://live.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    async def _signed(self, cli: httpx.AsyncClient, real_id: Any) -> dict[str, Any]:
        """给 getDanmuInfo 补上 wbi 签名（w_rid/wts），并保证拿到 buvid3。"""
        await self._ensure_buvid(cli)
        try:
            key = _wbi_cache.get("key") or ""
            if not key or time.time() - float(_wbi_cache.get("at") or 0) > 3600 * 6:
                nav = await cli.get(API_NAV, headers={"Referer": "https://www.bilibili.com/"})
                key = _wbi_key_from_nav(nav.json() or {})
                if key:
                    _wbi_cache["key"] = key
                    _wbi_cache["at"] = time.time()
        except Exception:
            key = ""
        return _wbi_sign({"id": real_id, "type": 0}, key)

    async def _ensure_buvid(self, cli: httpx.AsyncClient) -> None:
        if self._buvid:
            return
        try:
            resp = await cli.get(BUVID_INIT, headers={"Referer": "https://www.bilibili.com/"})
            self._buvid = resp.cookies.get("buvid3") or ""
            if self._buvid:
                # getDanmuInfo 没有 buvid3 会直接 -352 风控，所以把它并进 cookie 头
                if "buvid3=" not in (self.cookie or ""):
                    self.cookie = (f"{self.cookie}; buvid3={self._buvid}").strip("; ")
                cli.headers["Cookie"] = self.cookie
        except Exception:
            self._buvid = ""

    async def run(self, emit: Callable[[dict], None]) -> None:
        timeout = httpx.Timeout(15.0, connect=10.0)
        async with httpx.AsyncClient(headers=self._headers(), timeout=timeout,
                                     follow_redirects=True) as cli:
            await self._ensure_buvid(cli)
            init = await cli.get(API_INIT, params={"id": self.short_id})
            data = (init.json() or {}).get("data") or {}
            real_id = data.get("room_id")
            if not real_id:
                raise RuntimeError(f"B站房间 {self.short_id} 不存在")
            live_status = data.get("live_status")

            info = await cli.get(API_DANMU, params=await self._signed(cli, real_id))
            payload = (info.json() or {}).get("data") or {}
            token = payload.get("token") or ""
            hosts = payload.get("host_list") or []
            if not token or not hosts:
                conf = await cli.get(API_CONF, params={"room_id": real_id,
                                                       "platform": "pc", "player": "web"})
                payload = (conf.json() or {}).get("data") or {}
                token = payload.get("token") or ""
                hosts = payload.get("host_server_list") or []
                if token and hosts:
                    log("INFO", "[弹幕/B站] getDanmuInfo 被风控，改用 getConf 拿到的节点")
            if not token or not hosts:
                raise RuntimeError("B站弹幕服务器信息获取失败（接口已风控或无可用节点）")
            host = hosts[0]
            ws_url = f"wss://{host.get('host')}:{host.get('wss_port') or 443}/sub"
            uid = 0
            for part in (self.cookie or "").split(";"):
                if part.strip().startswith("DedeUserID="):
                    try:
                        uid = int(part.split("=", 1)[1])
                    except ValueError:
                        uid = 0

            log("INFO", f"[弹幕/B站] 连接 {ws_url}（房间 {real_id}，{'直播中' if live_status == 1 else '未开播'}）")
            async with websockets.connect(ws_url, max_size=8 * 1024 * 1024,
                                          open_timeout=15, close_timeout=5,
                                          ping_interval=None) as ws:
                auth = {
                    "uid": uid,
                    "roomid": int(real_id),
                    "protover": 3,
                    "platform": "web",
                    "type": 2,
                    "key": token,
                    "buvid": self._buvid or "",
                }
                await ws.send(_pack(7, json.dumps(auth).encode()))
                hb = asyncio.create_task(self._heartbeat(ws))
                buf = b""
                try:
                    async for raw in ws:
                        if isinstance(raw, str):
                            raw = raw.encode()
                        packets, buf = _split(buf + raw)
                        for ver, op, body in packets:
                            for msg in self._decode(ver, op, body, emit):
                                pass
                finally:
                    hb.cancel()

    async def _heartbeat(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(25)
                await ws.send(_pack(2, b""))
        except (asyncio.CancelledError, Exception):
            return

    def _decode(self, ver: int, op: int, body: bytes, emit: Callable[[dict], None]) -> list[Any]:
        if ver in (2, 3):
            try:
                inner = _decompress(ver, body)
            except Exception:
                return []
            packets, _rest = _split(inner)
            for iver, iop, ibody in packets:
                self._decode(iver, iop, ibody, emit)
            return []
        if op != 5:
            return []
        try:
            msg = json.loads(body.decode("utf-8", "ignore"))
        except json.JSONDecodeError:
            return []
        try:
            self.handle(msg, emit)
        except Exception:
            pass
        return []

    # ---------- 消息映射 ----------
    def handle(self, msg: dict, emit: Callable[[dict], None]) -> None:
        cmd = str(msg.get("cmd") or "").split(":")[0]
        data = msg.get("data") or {}
        if cmd == "DANMU_MSG":
            # 注意：DANMU_MSG 的 info 在消息顶层，不在 data 里（以前这里读错了，导致一条聊天弹幕都收不到）
            info = msg.get("info") or data.get("info") or []
            if len(info) < 3:
                return
            content = str(info[1])
            user = info[2] or []
            emit({
                "type": "chat",
                "user_id": str(user[0]) if user else "",
                "nickname": str(user[1]) if len(user) > 1 else "",
                "content": content,
            })
        elif cmd == "SEND_GIFT":
            emit({
                "type": "gift",
                "user_id": str(data.get("uid") or ""),
                "nickname": data.get("uname") or "",
                "gift_name": data.get("giftName") or "",
                "gift_count": int(data.get("num") or 0),
                "content": f"{data.get('giftName') or ''} x{data.get('num') or 0}",
            })
        elif cmd == "SUPER_CHAT_MESSAGE":
            emit({
                "type": "super_chat",
                "user_id": str(data.get("uid") or ""),
                "nickname": data.get("user_info", {}).get("uname") or "",
                "content": data.get("message") or "",
                "gift_count": int(data.get("price") or 0),
            })
        elif cmd == "INTERACT_WORD":
            emit({
                "type": "enter",
                "user_id": str(data.get("uid") or ""),
                "nickname": data.get("uname") or "",
                "content": "进入直播间",
            })
        elif cmd == "LIKE_INFO_V3_CLICK":
            emit({
                "type": "like",
                "user_id": str(data.get("uid") or ""),
                "nickname": data.get("uname") or "",
                "content": "点赞",
            })
        elif cmd == "ONLINE_RANK_COUNT":
            pass
