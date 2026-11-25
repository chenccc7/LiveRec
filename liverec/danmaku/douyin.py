"""抖音（douyin）弹幕客户端：WSS 长连 + webmssdk 签名 + protobuf(gzip) 解析。

实现要点（移植自 saermart/DouyinLiveWebFetcher，2025-09 仍可用）：
1. 用直播间网页拿到 room_id 与 ttwid；
2. 按固定 13 个参数拼串做 md5，再用 assets/sign.js 的 get_sign 算出 signature；
3. 连 wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/...；
4. 每 5s 发 PING 帧（PushFrame(payloadType='hb')），收包是 PushFrame(gzip(Response))；
5. needAck 时回 ack 帧，否则长连会被服务端断开。
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import os
import re
import time
from typing import Any, Callable

import httpx
import websockets

from .. import paths
from ..logbook import log
from .proto import douyin_pb2 as pb

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
USER_UNIQUE_ID = "7319483754668557238"
SIGN_KEYS = ("live_id,aid,version_code,webcast_sdk_version,"
             "room_id,sub_room_id,sub_channel_id,did_rule,"
             "user_unique_id,device_platform,device_type,ac,"
             "identity").split(",")

WSS_TPL = (
    "wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
    "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
    "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
    "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
    "&browser_name=Mozilla"
    "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
    "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
    "&browser_online=true&tz_name=Asia/Shanghai"
    "&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
    "&internal_ext=internal_src:dim|wss_push_room_id:{room_id}|wss_push_did:7319483754668557238"
    "|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|wss_info:0-1721106114633-0-0|"
    "wrds_v:7392094459690748497"
    "&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
    "&user_unique_id={user_unique_id}&im_path=/webcast/im/fetch/&identity=audience"
    "&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={room_id}&heartbeatDuration=0"
)

_GIFT_TEXT = {
    "1": "送出 {name} ×{count}",
}


def _sign_js_path() -> str:
    """sign.js 在开发态与打包态的位置。"""
    for base in (paths.bundle_root(), paths.app_root(), os.path.dirname(__file__)):
        candidate = os.path.join(base, "liverec", "danmaku", "assets", "sign.js")
        if os.path.exists(candidate):
            return candidate
        candidate = os.path.join(base, "assets", "sign.js")
        if os.path.exists(candidate):
            return candidate
    return os.path.join(os.path.dirname(__file__), "assets", "sign.js")


_sign_ctx: Any = None


def _get_sign_ctx() -> Any:
    """MiniRacer 上下文只创建一次（eval 485KB 的 webmssdk 约 0.3~0.8s）。"""
    global _sign_ctx
    if _sign_ctx is None:
        from py_mini_racer import MiniRacer

        path = _sign_js_path()
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            script = fh.read()
        ctx = MiniRacer()
        ctx.eval(script)
        _sign_ctx = ctx
    return _sign_ctx


def _signature(wss_url: str) -> str:
    query = wss_url.split("?", 1)[1]
    values = {p.split("=", 1)[0]: p.split("=")[-1] for p in query.split("&")}
    param = ",".join(f"{k}={values.get(k, '')}" for k in SIGN_KEYS)
    md5_param = hashlib.md5(param.encode()).hexdigest()
    return str(_get_sign_ctx().call("get_sign", md5_param))


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


class DouyinDanmaku:
    supported = True
    reason = ""

    def __init__(self, url: str, cookie: str = ""):
        self.url = url
        self.cookie = cookie
        m = re.search(r"live\.douyin\.com/(?:share/)?(\d+)", url)
        self.web_rid = m.group(1) if m else ""
        self.room_id = ""
        self.ttwid = ""

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": UA,
            "Referer": f"https://live.douyin.com/{self.web_rid or ''}",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _prepare(self) -> None:
        """从直播间网页拿 room_id 与 ttwid。"""
        if not self.web_rid:
            raise RuntimeError("抖音直播间地址里没有房间号")
        timeout = httpx.Timeout(20.0, connect=10.0)
        async with httpx.AsyncClient(headers=self._headers(), timeout=timeout,
                                     follow_redirects=True) as cli:
            page = await cli.get(f"https://live.douyin.com/{self.web_rid}")
            text = page.text or ""
            self.ttwid = cli.cookies.get("ttwid") or page.cookies.get("ttwid") or ""
            for pattern in (r'roomId\\":\\"(\d+)\\"', r'"roomId":"(\d+)"', r"roomId\\\":\\\"(\d+)"):
                m = re.search(pattern, text)
                if m:
                    self.room_id = m.group(1)
                    break
        if not self.room_id:
            raise RuntimeError("抖音弹幕：未能从直播间网页解析出 room_id")

    def _wss_url(self) -> str:
        wss = WSS_TPL.format(room_id=self.room_id, user_unique_id=USER_UNIQUE_ID)
        return f"{wss}&signature={_signature(wss)}"

    # ---------- 消息分派 ----------

    def _parse(self, method: str, payload: bytes) -> dict[str, Any] | None:
        if method == "WebcastChatMessage":
            msg = pb.ChatMessage()
            msg.ParseFromString(payload)
            content = _text(msg.content).strip()
            if not content:
                return None
            return {"type": "chat", "user_id": _text(msg.user.id),
                    "nickname": _text(msg.user.nickName), "content": content}
        if method == "WebcastEmojiChatMessage":
            msg = pb.EmojiChatMessage()
            msg.ParseFromString(payload)
            content = _text(getattr(msg, "emojiContent", "")) or "[表情]"
            return {"type": "chat", "user_id": _text(msg.user.id),
                    "nickname": _text(msg.user.nickName), "content": content}
        if method == "WebcastGiftMessage":
            msg = pb.GiftMessage()
            msg.ParseFromString(payload)
            name = _text(msg.gift.name) or f"礼物#{msg.giftId}"
            count = int(msg.repeatCount or msg.comboCount or 1)
            return {"type": "gift", "user_id": _text(msg.user.id),
                    "nickname": _text(msg.user.nickName), "gift_name": name,
                    "gift_count": count, "repeat": int(msg.repeatCount or 0),
                    "content": f"送出 {name} ×{count}"}
        if method == "WebcastMemberMessage":
            msg = pb.MemberMessage()
            msg.ParseFromString(payload)
            return {"type": "enter", "user_id": _text(msg.user.id),
                    "nickname": _text(msg.user.nickName), "content": "进入直播间"}
        if method == "WebcastLikeMessage":
            msg = pb.LikeMessage()
            msg.ParseFromString(payload)
            return {"type": "like", "user_id": _text(msg.user.id),
                    "nickname": _text(msg.user.nickName),
                    "content": f"点赞 ×{int(msg.count or 1)}"}
        if method == "WebcastSocialMessage":
            msg = pb.SocialMessage()
            msg.ParseFromString(payload)
            return {"type": "follow", "user_id": _text(msg.user.id),
                    "nickname": _text(msg.user.nickName), "content": "关注了主播"}
        # 其余（在线人数、榜单、进房提示等）不入库，避免把弹幕流刷满
        return None

    def _extract(self, frame: pb.PushFrame, emit: Callable[[dict], None]) -> int:
        body = frame.payload
        if frame.payloadType == "hb":
            return 0
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
        response = pb.Response()
        try:
            response.ParseFromString(body)
        except Exception:
            return 0
        if response.needAck:
            ack = pb.PushFrame(logId=frame.logId, payloadType="ack",
                               payload=response.internalExt.encode("utf-8"))
            self._pending_ack = ack.SerializeToString()
        count = 0
        for message in response.messagesList:
            try:
                event = self._parse(message.method, message.payload)
            except Exception:
                continue
            if event:
                event["raw"] = message.method
                try:
                    emit(event)
                except Exception:
                    pass
                count += 1
        return count

    async def _consume(self, emit: Callable[[dict], None]) -> None:
        url = self._wss_url()
        headers = {"User-Agent": UA, "Cookie": f"ttwid={self.ttwid}"} if self.ttwid else {"User-Agent": UA}
        self._pending_ack = b""
        async with websockets.connect(url, additional_headers=headers, max_size=2 ** 23,
                                      ping_interval=None, open_timeout=15) as ws:
            log("INFO", f"[弹幕/抖音] 连接 {url.split('?')[0]}（房间 {self.room_id}）")
            last_ping = 0.0
            while True:
                if time.time() - last_ping >= 5:
                    last_ping = time.time()
                    ping = pb.PushFrame(payloadType="hb")
                    await ws.ping(ping.SerializeToString())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    if self._pending_ack:
                        ack, self._pending_ack = self._pending_ack, b""
                        await ws.send(ack)
                    continue
                if isinstance(raw, str):
                    continue
                frame = pb.PushFrame()
                try:
                    frame.ParseFromString(raw)
                except Exception:
                    continue
                self._extract(frame, emit)
                if self._pending_ack:
                    ack, self._pending_ack = self._pending_ack, b""
                    await ws.send(ack)

    async def run(self, emit: Callable[[dict], None]) -> None:
        await self._prepare()
        if not self.ttwid:
            log("WARN", "[弹幕/抖音] 未拿到 ttwid，可能会被服务端拒绝")
        delay = 3.0
        while True:
            try:
                await self._consume(emit)
                delay = 3.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 长连任何异常都重试
                log("WARN", f"[弹幕/抖音] 连接中断：{type(exc).__name__}: {exc}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
