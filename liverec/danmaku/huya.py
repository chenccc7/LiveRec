"""虎牙弹幕：协议未接入（需要开放平台资质）。

实测记录（2026-11）：
- 老方案 `wss://cdnws.api.huya.com/`：TCP/TLS 握手成功，但用 Tars 编码的
  `WebSocketCommand{iCmdType=1, vData=WSUserInfo}` 注册后，服务端一帧都不回
  （试过匿名 uid、真实 uid、带 Cookie、带 baseinfo/t 查询串、先发心跳、
  指定子协议等全部变体，40 秒内 raw=0）。
- 虎牙现在把弹幕能力放在开放平台（open.huya.com）：先用 getMessageNotice
  订阅 WSLink，需要 openId / secretId 之类的开发者资质才能鉴权。

所以这里只保留接口占位，界面标记「适配中」；等有开放平台凭据再接。
"""
from __future__ import annotations

from typing import Callable

UNSUPPORTED_REASON = "虎牙弹幕需要开放平台资质（匿名 WSS 通道已不再推送）"


class HuyaDanmaku:
    supported = False
    reason = UNSUPPORTED_REASON

    def __init__(self, url: str, cookie: str = ""):
        self.url = url
        self.cookie = cookie

    async def run(self, emit: Callable[[dict], None]) -> None:
        raise RuntimeError(UNSUPPORTED_REASON)
