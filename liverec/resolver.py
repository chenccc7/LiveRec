"""直播流地址解析：复用上游 core/src 的 spider + stream。

上游的解析函数全是 async，且内部会用 execjs 做签名（阻塞），
所以统一放进线程 + 独立事件循环里跑，避免卡住服务端主循环。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from typing import Any

from . import paths
from .logbook import log

_lock = threading.Lock()
_core_loaded = False
_spider: Any = None
_stream: Any = None
_utils: Any = None


class ResolveError(RuntimeError):
    """解析失败：消息本身面向用户展示，界面不再加异常类名前缀。"""

    user_facing = True


def _ensure_core() -> None:
    """把 core 目录加入 sys.path 并懒加载上游模块。"""
    global _core_loaded, _spider, _stream, _utils
    if _core_loaded:
        return
    with _lock:
        if _core_loaded:
            return
        core = paths.core_dir()
        if core not in sys.path:
            sys.path.insert(0, core)
        # 上游 initializer 发现 PATH 里没有 node 会去下载 37MB 的 Node.js，
        # 这里先把 core/node（已存在时）塞进 PATH，避免每次启动都下载。
        node = paths.node_exe()
        if node:
            node_dir = os.path.dirname(node)
            env_path = os.environ.get("PATH", "")
            if node_dir not in env_path:
                os.environ["PATH"] = node_dir + os.pathsep + env_path
        from core.src import spider, stream, utils  # noqa: PLC0415

        _spider, _stream, _utils = spider, stream, utils
        _core_loaded = True
        log("INFO", "上游解析模块已加载（core/src）")


PLATFORMS = {
    "douyin": {"label": "抖音", "color": "#ff4d6a"},
    "bilibili": {"label": "B站", "color": "#38bdf8"},
    "huya": {"label": "虎牙", "color": "#f5a524"},
    "douyu": {"label": "斗鱼", "color": "#ff7a45"},
    "custom": {"label": "自定义", "color": "#94a3b8"},
}


def detect_platform(url: str) -> str:
    u = (url or "").strip().lower()
    if "douyin.com" in u:
        return "douyin"
    if "live.bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "huya.com" in u:
        return "huya"
    if "douyu.com" in u:
        return "douyu"
    if u.endswith(".m3u8") or u.endswith(".flv"):
        return "custom"
    return "unknown"


def select_source_url(url: str, info: dict) -> str:
    """抖音/TikTok 优先 flv；h265 编码的 flv 退回 hls。"""
    if any(k in url for k in ("douyin", "tiktok")):
        flv = info.get("flv_url")
        if flv:
            try:
                from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

                codec = (parse_qs(urlparse(flv).query).get("codec") or [""])[0]
                if codec == "h265":
                    log("WARN", "FLV 为 h265 编码，改用 HLS 源")
                else:
                    return flv
            except Exception:
                return flv
    return info.get("record_url") or ""


_DOUYU_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
             "Gecko/20100101 Firefox/124.0")


def _douyu_room_id(url: str) -> str:
    """从斗鱼链接里取数字房间号（支持 /12345、?rid=12345）。"""
    m = re.search(r"[?&]rid=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"douyu\.com/(?:topic/)?([0-9A-Za-z_]+)", url)
    if not m:
        return ""
    token = m.group(1)
    if token.isdigit():
        return token
    # 自定义短名：跟着跳转到正式房间页再取一次
    try:
        import requests  # noqa: PLC0415

        resp = requests.get(f"https://www.douyu.com/{token}", headers={"User-Agent": _DOUYU_UA},
                            allow_redirects=True, timeout=15)
        m2 = re.search(r"douyu\.com/(\d+)", resp.url)
        if m2:
            return m2.group(1)
    except Exception:
        return ""
    return ""


def _douyu_info(url: str, cookie: str, proxy: str) -> dict[str, Any]:
    """上游的 get_douyu_info_data 依赖已被斗鱼改版干掉的移动页结构，
    这里直接用官方的 betard 接口取房间信息，稳定得多。"""
    import requests  # noqa: PLC0415

    rid = _douyu_room_id(url)
    if not rid:
        raise ResolveError("未能从链接中解析出斗鱼房间号")

    headers = {"User-Agent": _DOUYU_UA, "Referer": f"https://www.douyu.com/{rid}"}
    if cookie:
        headers["Cookie"] = cookie
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.get(f"https://www.douyu.com/betard/{rid}", headers=headers,
                        proxies=proxies, timeout=20)
    payload = resp.json()
    room = payload.get("room") or {}
    if not room:
        raise ResolveError("斗鱼未返回房间信息（房间号可能不存在）")

    room_id = str(room.get("room_id") or rid)
    is_live = room.get("videoLoop") == 0 and room.get("show_status") == 1
    data: dict[str, Any] = {
        "anchor_name": room.get("nickname") or "",
        "is_live": bool(is_live),
        "room_id": room_id,
    }
    if is_live:
        data["title"] = str(room.get("room_name") or "").replace("&nbsp;", "")
    return data


_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
# 斗鱼/虎牙的移动页只在「老安卓 UA」下才返回含签名与流地址的 JSON，iPhone UA 会拿到精简页。
_ANDROID_UA = ("Mozilla/5.0 (Linux; Android 5.0; SM-G900P Build/LRX21T) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/75.0.3770.100 Mobile Safari/537.36 ")
_DOUYU_DID = "10000000000000000000000000001501"
# getEncryption / getH5PlayV1 这条链只认 PC UA + www 域名。
_DOUYU_PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _proxies(proxy: str) -> dict | None:
    return {"http": proxy, "https": proxy} if proxy else None


def _douyu_mobile_page(rid: str, cookie: str, proxy: str) -> dict[str, Any]:
    """移动页兜底：拿真实房间号、主播昵称、标题。"""
    import requests  # noqa: PLC0415

    headers = {"User-Agent": _ANDROID_UA, "Referer": "https://m.douyu.com/"}
    if cookie:
        headers["Cookie"] = cookie
    html = requests.get(f"https://m.douyu.com/{rid}", headers=headers,
                        proxies=_proxies(proxy), timeout=20).text

    def pick(pattern: str) -> str:
        m = re.search(pattern, html)
        return m.group(1) if m else ""

    return {
        "room_id": pick(r'rid":(\d{1,8}),"vipId') or rid,
        "anchor_name": pick(r'"nickname":"([^"]{1,40})"'),
        "title": pick(r'"roomName":"([^"]{0,80})"'),
    }


def _douyu_hls_preview(rid: str, cookie: str, proxy: str) -> tuple[int, str, str, str]:
    """斗鱼官方 H5 预览接口（real-url 里 @wjxgzz 等人挖出来的做法）。

    返回 (error, rtmp_url, rtmp_live, msg)；error=0 时 rtmp_url + rtmp_live 就是可用流。
    它不需要 ub98484234 那套 JS 签名，因此也不再依赖 Node/execjs。
    注意：这条接口只下发房间最高码率的一条流，rate/画质参数无效。
    """
    import hashlib  # noqa: PLC0415
    import time  # noqa: PLC0415

    import requests  # noqa: PLC0415

    t13 = str(int(time.time() * 1000))
    auth = hashlib.md5((rid + t13).encode()).hexdigest()
    headers = {
        "User-Agent": _ANDROID_UA,
        "Referer": f"https://m.douyu.com/{rid}",
        "rid": rid,
        "time": t13,
        "auth": auth,
    }
    if cookie:
        headers["Cookie"] = cookie
    resp = requests.post(f"https://playweb.douyucdn.cn/lapi/live/hlsH5Preview/{rid}",
                        headers=headers, data={"rid": rid, "did": _DOUYU_DID},
                        proxies=_proxies(proxy), timeout=20)
    body = resp.json()
    info = body.get("data") or {}
    return (int(body.get("error") or 0), str(info.get("rtmp_url") or ""),
            str(info.get("rtmp_live") or ""), str(body.get("msg") or ""))


_DOUYU_GUEST_DID = "10000000000000000000000000003306"
_DOUYU_RATE = {"OD": "0", "BD": "3", "UHD": "2", "HD": "2", "SD": "1", "LD": "1"}


def _douyu_h5play(rid: str, rate: str, cookie: str, proxy: str) -> tuple[int, str, str]:
    """斗鱼 getEncryption + getH5PlayV1：能按 rate 拿到对应码率的流。

    这套签名链是从 utlived-app 的 douyu.go 移植的：
    `auth` = rand_str 迭代 md5(enc_time 次) 后再拼 key(+roomID+ts)。
    返回 (error, url, msg)。
    """
    import hashlib  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    import requests  # noqa: PLC0415

    did = _DOUYU_GUEST_DID
    headers = {
        "User-Agent": _DOUYU_PC_UA,
        "Referer": f"https://www.douyu.com/{rid}",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie

    enc = requests.get(
        "https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption",
        params={"did": did}, headers=headers,
        proxies=_proxies(proxy), timeout=20).json()
    data = enc.get("data") or {}
    rand_str = str(data.get("rand_str") or "")
    key = str(data.get("key") or "")
    if not rand_str or not key:
        return -1, "", "斗鱼签名接口没返回 rand_str/key"

    ts = int(_time.time())
    auth = rand_str
    for _ in range(int(data.get("enc_time") or 0)):
        auth = hashlib.md5((auth + key).encode()).hexdigest()
    tail = "" if int(data.get("is_special") or 0) == 1 else f"{rid}{ts}"
    auth = hashlib.md5((auth + key + tail).encode()).hexdigest()

    form = {
        "enc_data": str(data.get("enc_data") or ""),
        "tt": ts,
        "did": did,
        "auth": auth,
        "cdn": "",
        "rate": rate,
        "hevc": "0",
        "fa": "0",
        "ive": "0",
    }
    resp = requests.post(f"https://www.douyu.com/lapi/live/getH5PlayV1/{rid}",
                         data=form, headers=headers,
                         proxies=_proxies(proxy), timeout=20)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return -1, "", f"斗鱼取流接口返回非 JSON（HTTP {resp.status_code}）"
    err = int(body.get("error") or 0)
    info = body.get("data") or {}
    if err != 0 or not info:
        return err, "", str(body.get("msg") or body.get("message") or "")
    base = str(info.get("rtmp_url") or "")
    live = str(info.get("rtmp_live") or "")
    if not base or not live:
        return -1, "", "斗鱼取流接口没返回地址"
    url = f"{base}/{live}"
    return 0, url, ""


def _douyu_stream_url(rid: str, cookie: str, proxy: str, quality_code: str = "OD") -> str:
    """优先按档位取流（getH5PlayV1 支持 rate）；失败退回「最高码率」的预览接口。"""
    rate = _DOUYU_RATE.get(quality_code or "OD", "0")
    error, url, msg = _douyu_h5play(rid, rate, cookie, proxy)
    if error == 102:
        raise ResolveError("斗鱼：房间不存在")
    if error == 104:
        raise ResolveError("斗鱼：主播未开播")
    if error == 0 and url:
        # .m3u8 换成 .flv 更利于 -c copy 直录（同 CDN 同 token 通用）
        return re.sub(r"\.m3u8(\?|$)", r".flv\1", url)

    error, base, live, msg = _douyu_hls_preview(rid, cookie, proxy)
    if error == 102:
        raise ResolveError("斗鱼：房间不存在")
    if error == 104:
        raise ResolveError("斗鱼：主播未开播")
    if error != 0 or not base or not live:
        raise ResolveError(f"斗鱼取流失败：{msg or f'error={error}'}")
    # same CDN 上把 .m3u8 换成 .flv 更利于 -c copy 直录
    return f"{base}/{re.sub(r'\.m3u8(\?|$)', r'.flv\1', live)}"


def _resolve_douyu(url: str, quality_code: str, cookie: str, proxy: str) -> dict[str, Any]:
    try:
        data = _douyu_info(url, cookie, proxy)
    except Exception:  # noqa: BLE001 - betard 偶尔也会抽风，退回移动页
        data = _douyu_mobile_page(_douyu_room_id(url), cookie, proxy)
    if not data.get("is_live"):
        return data
    rid = str(data.get("room_id") or _douyu_room_id(url))
    stream_url = _douyu_stream_url(rid, cookie, proxy, quality_code)
    rate = _DOUYU_RATE.get(quality_code or "OD", "0")
    result = {
        "anchor_name": data.get("anchor_name", ""),
        "is_live": True,
        "title": data.get("title", ""),
        "quality": quality_code,
        "flv_url": stream_url,
        "record_url": stream_url,
    }
    if rate not in ("0", ""):
        requested = _QUALITY_LABEL.get(quality_code, quality_code)
        result["quality_actual"] = requested
        result["quality_note"] = f"斗鱼按所选档位取流（rate={rate}，{requested}）"
    return result


_BILI_QN = {"OD": "10000", "BD": "400", "UHD": "250", "HD": "150", "SD": "80", "LD": "80"}
_BILI_QN_DESC = {"30000": "杜比", "20000": "4K", "10000": "原画", "401": "蓝光杜比",
                 "400": "蓝光", "250": "超清", "150": "高清", "80": "流畅"}
_BILI_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0"


def _bili_headers(cookie: str) -> dict[str, str]:
    headers = {
        "User-Agent": _BILI_UA,
        "Accept-Language": "zh-CN,zh;q=0.8",
        "Origin": "https://live.bilibili.com",
        "Referer": "https://live.bilibili.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _bili_room_id(url: str) -> str:
    m = re.search(r"live\.bilibili\.com/(?:h5/)?(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"b23\.tv/(\w+)", url)
    if m:
        import requests  # noqa: PLC0415

        resp = requests.get(url, headers=_bili_headers(""), allow_redirects=True, timeout=20)
        m2 = re.search(r"live\.bilibili\.com/(?:h5/)?(\d+)", resp.url)
        if m2:
            return m2.group(1)
    return ""


def _bili_pick(payload: dict[str, Any], want_qn: str) -> tuple[str, str]:
    """从 getRoomPlayInfo 里挑一条最适合 `-c copy` 的流，返回 (url, 实际 qn)。

    优先级：flv(http_stream,avc) > flv(任意编码) > hls ts(avc) > hls fmp4(avc) > 其它。
    B站 未登录时 `current_qn` 会被压到 250（超清），必须按接口返回的实际值报告，
    否则界面会显示「原画」但录到的是 720p/1080p 超清。
    """
    playurl = payload.get("playurl_info") or {}
    streams = ((playurl.get("playurl") or {}).get("stream") or [])
    pref_pairs = [("http_stream", "flv", "avc"), ("http_stream", "flv", None),
                  ("http_hls", "ts", "avc"), ("http_hls", "fmp4", "avc"),
                  ("http_hls", "ts", None), (None, None, None)]
    for want_proto, want_fmt, want_codec in pref_pairs:
        for stream in streams:
            if want_proto and stream.get("protocol_name") != want_proto:
                continue
            for fmt in stream.get("format") or []:
                if want_fmt and fmt.get("format_name") != want_fmt:
                    continue
                for codec in fmt.get("codec") or []:
                    if want_codec and codec.get("codec_name") != want_codec:
                        continue
                    url_info = (codec.get("url_info") or [{}])[0]
                    if not codec.get("base_url") or not url_info.get("host"):
                        continue
                    url = f"{url_info['host']}{codec['base_url']}{url_info.get('extra', '')}"
                    return url, str(codec.get("current_qn") or want_qn)
    return "", want_qn


def _bili_stream_url(room_id: str, qn: str, cookie: str, proxy: str) -> tuple[str, str]:
    """返回 (流地址, 实际 qn)。实际 qn 由接口的 current_qn 给出，可能低于请求值。"""
    import urllib.parse  # noqa: PLC0415

    import requests  # noqa: PLC0415

    headers = _bili_headers(cookie)
    params = {
        "room_id": room_id, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
        "qn": qn, "platform": "web", "ptype": "8", "dolby": "5",
        "panorama": "1", "hdr_type": "0,1",
    }
    api = ("https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?"
           + urllib.parse.urlencode(params))
    try:
        body = requests.get(api, headers=headers, proxies=_proxies(proxy), timeout=20).json()
    except Exception:
        body = {}
    if body.get("code") == 0:
        payload = body.get("data") or {}
        if payload.get("live_status") != 1:
            return "", qn
        url, actual = _bili_pick(payload, qn)
        if url:
            return url, actual
    elif body.get("code") not in (None, 0):
        raise ResolveError(f"B站取流失败：{body.get('message') or body.get('code')}")

    # 兜底：老 playUrl 接口（只给一路 flv，画质同样受登录状态限制）
    params2 = {"cid": room_id, "qn": qn, "platform": "web"}
    api2 = "https://api.live.bilibili.com/room/v1/Room/playUrl?" + urllib.parse.urlencode(params2)
    body2 = requests.get(api2, headers=headers, proxies=_proxies(proxy), timeout=20).json()
    if body2.get("code") != 0:
        raise ResolveError(f"B站取流失败：{body2.get('message') or body2.get('code')}")
    data2 = body2.get("data") or {}
    urls = [d.get("url") for d in (data2.get("durl") or []) if d.get("url")]
    if not urls:
        raise ResolveError("B站码流地址为空")
    pick = next((u for u in urls if "d1--cn-gotcha" in u), urls[-1])
    return pick, str(data2.get("quality") or qn)


def _resolve_bilibili(url: str, quality_code: str, cookie: str, proxy: str) -> dict[str, Any]:
    import requests  # noqa: PLC0415

    headers = _bili_headers(cookie)
    room_id = _bili_room_id(url)
    if not room_id:
        raise ResolveError("未能从链接中解析出 B站房间号")

    init = requests.get(f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}",
                        headers=headers, proxies=_proxies(proxy), timeout=20).json()
    if init.get("code") != 0:
        raise ResolveError(f"B站房间信息获取失败：{init.get('message')}")
    info = init.get("data") or {}
    real_id = str(info.get("room_id") or room_id)
    uid = info.get("uid")
    is_live = info.get("live_status") == 1

    anchor_name, title = "", ""
    if uid:
        try:
            master = requests.get(
                f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={uid}",
                headers=headers, proxies=_proxies(proxy), timeout=20).json()
            anchor_name = ((master.get("data") or {}).get("info") or {}).get("uname") or ""
        except Exception:
            pass
    try:
        h5 = requests.get(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getH5InfoByRoom?room_id="
            f"{real_id}", headers=headers, proxies=_proxies(proxy), timeout=20).json()
        title = ((h5.get("data") or {}).get("room_info") or {}).get("title") or ""
    except Exception:
        pass

    result: dict[str, Any] = {"anchor_name": anchor_name, "is_live": bool(is_live), "title": title}
    if not is_live:
        return result
    stream, actual_qn = _bili_stream_url(real_id, _BILI_QN.get(quality_code, "10000"),
                                        cookie, proxy)
    if not stream:
        result["is_live"] = False
        return result
    want_qn = _BILI_QN.get(quality_code, "10000")
    note = ""
    if actual_qn and actual_qn != want_qn:
        got = _BILI_QN_DESC.get(actual_qn, f"qn{actual_qn}")
        want = _BILI_QN_DESC.get(want_qn, f"qn{want_qn}")
        note = f"B站实际画质为{got}（请求{want}）：未登录时最高只能到超清，在「设置 → B站 cookie」填入登录 Cookie 即可解锁{want}"
    result.update({"quality": quality_code, "quality_note": note, "record_url": stream,
                   "quality_actual": _BILI_QN_DESC.get(actual_qn, "")})
    return result


_DOUYIN_UA = ("Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/116.0.5845.97 Safari/537.36 Core/1.116.567.400 QQBrowser/19.7.6764.400")
_DOUYIN_TTWID = ("ttwid=1%7C2iDIYVmjzMcpZ20fcaFde0VghXAA3NaNXE_SLR68IyE%7C1761045455"
                 "%7Cab35197d5cfb21df6cbb2fa7ef1c9262206b062c315b9d04da746d0b37dfbc7d")
_DOUYIN_QUALITY = {"OD": "origin", "BD": "FULL_HD1", "UHD": "HD1",
                   "HD": "SD1", "SD": "SD2", "LD": "SD2"}


def _douyin_web_rid(url: str, proxy: str) -> str:
    m = re.search(r"live\.douyin\.com/(?:share/)?(\d+)", url)
    if m:
        return m.group(1)
    if "v.douyin.com" in url:
        import requests  # noqa: PLC0415

        resp = requests.get(url, headers={"user-agent": _DOUYIN_UA}, allow_redirects=True,
                            proxies=_proxies(proxy), timeout=20)
        m = re.search(r"live\.douyin\.com/(?:share/)?(\d+)", resp.url)
        if m:
            return m.group(1)
    if "/user/" in url:
        raise ResolveError("抖音主页链接暂不支持，请粘贴直播间地址（live.douyin.com/数字）")
    raise ResolveError("未能从链接中解析出抖音房间号")


def _douyin_enter(web_rid: str, cookie: str, proxy: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """调 webcast/room/web/enter 取房间信息，返回 (room, user)。"""
    import json  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415

    import requests  # noqa: PLC0415

    from liverec.douyin_sign import ab_sign  # noqa: PLC0415

    headers = {
        "user-agent": _DOUYIN_UA,
        "referer": f"https://live.douyin.com/{web_rid}",
        "accept": "application/json, text/plain, */*",
        "cookie": cookie or _DOUYIN_TTWID,
    }
    params = {
        "aid": "6383", "app_name": "douyin_web", "live_id": "1", "device_platform": "web",
        "language": "zh-CN", "browser_language": "zh-CN", "browser_platform": "Win32",
        "browser_name": "Chrome", "browser_version": "116.0.0.0", "web_rid": web_rid,
        "msToken": "",
    }
    query = urllib.parse.urlencode(params)
    api = (f"https://live.douyin.com/webcast/room/web/enter/?{query}"
           f"&a_bogus={ab_sign(query, _DOUYIN_UA)}")
    raw = requests.get(api, headers=headers, proxies=_proxies(proxy), timeout=25).text
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise ResolveError("抖音接口返回异常（可能触发了风控，请稍后重试或配置代理）") from None
    data = body.get("data") or {}
    rows = data.get("data") or []
    if not rows:
        raise ResolveError("抖音直播间信息为空（房间可能不存在或已下播）")
    return rows[0], data.get("user") or {}


def _douyin_stream(room: dict[str, Any], quality_code: str) -> str:
    import json  # noqa: PLC0415

    stream_url = room.get("stream_url") or {}
    pulls = stream_url.get("flv_pull_url") or {}
    origin = ""
    sdk = ((stream_url.get("live_core_sdk_data") or {}).get("pull_data") or {}).get("stream_data")
    if sdk:
        try:
            inner = json.loads(sdk)
            main = ((inner.get("data") or {}).get("origin") or {}).get("main") or {}
            origin = main.get("flv") or ""
        except Exception:
            origin = ""
    key = _DOUYIN_QUALITY.get(quality_code, "origin")
    if key == "origin":
        chosen = origin or pulls.get("FULL_HD1") or ""
    else:
        chosen = pulls.get(key) or origin or ""
    if not chosen and pulls:
        chosen = next(iter(pulls.values()))
    return chosen.replace("http://", "https://", 1) if chosen.startswith("http://") else chosen


def _resolve_douyin(url: str, quality_code: str, cookie: str, proxy: str) -> dict[str, Any]:
    web_rid = _douyin_web_rid(url, proxy)
    room, user = _douyin_enter(web_rid, cookie, proxy)
    result: dict[str, Any] = {
        "anchor_name": user.get("nickname") or "",
        "title": room.get("title") or "",
        "is_live": room.get("status") == 2,
    }
    if not result["is_live"]:
        return result
    stream = _douyin_stream(room, quality_code)
    if not stream:
        raise ResolveError("抖音未返回可用直播流（该直播间可能限制网页端观看）")
    result.update({"quality": quality_code, "flv_url": stream, "record_url": stream})
    return result


def _huya_room_id(url: str, proxy: str = "") -> str:
    """虎牙房间号：数字直接用；短名（www.huya.com/dank1ng）从移动页里取 lChannelId。

    cache.php 那套接口只认数字房间号，所以短名必须先换一次。
    """
    m = re.search(r"huya\.com/(?:share/)?([A-Za-z0-9_]+)", url)
    if not m:
        raise ResolveError("未能从链接中解析出虎牙房间号")
    token = m.group(1)
    if token.isdigit():
        return token
    import requests  # noqa: PLC0415

    html = ""
    try:
        resp = requests.get(
            f"https://m.huya.com/{token}",
            headers={"User-Agent": _ANDROID_UA, "Referer": "https://m.huya.com/"},
            proxies=_proxies(proxy), timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:  # noqa: BLE001
        raise ResolveError(f"虎牙短名解析失败（{token}）：{exc}") from exc
    # 页面里的 lChannelId 是主播 uid，不是房间号；房间号在 lProfileRoom（详见 tLiveProfile）。
    for pat in (r'"lProfileRoom"\s*:\s*(\d{2,12})',
                r'"lProfileRoom\\?"\s*:\s*(\d{2,12})',
                r'"lSid"\s*:\s*(\d{2,12})',
                r'"roomId"\s*:\s*"(\d{2,12})"',
                r"lChannelId\"?\s*[:=]\s*(\d{2,12})"):
        for mm in re.finditer(pat, html):
            if mm.group(1).strip("0"):
                return mm.group(1)
    raise ResolveError(f"虎牙短名解析失败（{token}）：页面里没有房间号，请改用 https://www.huya.com/<数字>")


def _huya_stream_lines(html: str) -> list[dict[str, str]]:
    """移动页里还有一块 tLiveStreamInfo，按 CDN 列出 sFlvUrl + sStreamName + sFlvAntiCode。

    赛事/官方频道（例如 660000）没有 liveLineUrl，流地址只在这块里。
    """
    import json  # noqa: PLC0415

    head = html.find('"tLiveStreamInfo"')
    if head < 0:
        return []
    start = html.find('{"vStreamInfo"', head)
    if start < 0:
        return []
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[start:])
    except Exception:  # noqa: BLE001
        return []
    value = ((obj.get("vStreamInfo") or {}).get("value")) or []
    lines: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        cdn = str(item.get("sFlvUrl") or "")
        name = str(item.get("sStreamName") or "")
        suffix = str(item.get("sFlvUrlSuffix") or "flv")
        anti = str(item.get("sFlvAntiCode") or "")
        if not (cdn and name):
            continue
        if cdn.startswith("http://"):
            cdn = "https://" + cdn[len("http://"):]
        elif cdn.startswith("//"):
            cdn = "https:" + cdn
        query = f"?{anti}" if anti else ""
        lines.append({
            "url": f"{cdn}/{name}.{suffix}{query}",
            "anti": anti,
            "name": name,
            "cdn": item.get("sCdnType") or "",
        })
    return lines


_huya_mp_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _huya_mp_data_cached(room: str, cookie: str, proxy: str, ttl: float = 8.0) -> dict[str, Any]:
    """短缓存：HLS 中转每几秒刷新一次播放列表，别每次都打虎牙接口（容易被限流）。"""
    key = f"{room}|{len(cookie)}|{proxy}"
    now = time.time()
    hit = _huya_mp_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    data = _huya_mp_data(room, cookie, proxy)
    _huya_mp_cache[key] = (now, data)
    return data


def _huya_mp_data(room: str, cookie: str, proxy: str) -> dict[str, Any]:
    """移动端 cache 接口：一次请求拿到 liveStatus + 各 CDN 的 flv 线路 + HLS 线路。

    PC 端 cache.php?do=profileRoom 已经被风控成验证页，移动端这个还没封。
    """
    import requests  # noqa: PLC0415

    headers = {"User-Agent": _ANDROID_UA, "Referer": "https://m.huya.com/"}
    if cookie:
        headers["Cookie"] = cookie
    url = f"https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={room}"
    resp = requests.get(url, headers=headers, proxies=_proxies(proxy), timeout=20)
    text = (resp.text or "").strip()
    if resp.status_code != 200 or not text.startswith("{"):
        raise ResolveError("虎牙接口返回风控页面（该平台已加强反爬，稍后重试或改用代理）")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ResolveError("虎牙接口返回内容无法解析，稍后重试") from exc
    return data.get("data") or {}


def _huya_target_bitrate(quality: str) -> int:
    """把用户选的档位换算成目标码率（kbps）。

    虎牙的档位名本身就带码率（蓝光20M / 蓝光6M / 蓝光4M），
    也兼容传入规范码（OD/BD/UHD/HD/LD）或纯数字。
    """
    text = (quality or "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*[Mm]\b", text)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.search(r"\b(\d{3,5})\b", text)
    if m:
        return int(m.group(1))
    for name, bitrate in (("原画", 0), ("蓝光", 6000), ("超清", 2000), ("高清", 1000), ("标清", 800), ("流畅", 500)):
        if name in text:
            return bitrate
    return _QUALITY_BITRATE.get(text, 0)


def _huya_fresh_line(room_url: str, target_url: str, cookie: str = "", proxy: str = "") -> str:
    """给 HLS 中转用：每次刷新播放列表都要一条**新签**的地址。

    虎牙的 wsTime/wsSecret 有有效期（几十秒后重签老地址也会 403），
    所以这里重新问一次接口，按同 host 取回当次的新地址。
    """
    import urllib.parse  # noqa: PLC0415

    try:
        room = _huya_room_id(room_url, proxy)
        # 2.5 秒的短缓存：HLS 中转刷新播放列表时会连续取几次，太频繁会被接口限流，
        # 但签名 20 秒左右就过期，所以缓存不能超过一小会儿。
        data = _huya_mp_data_cached(room, cookie, proxy, ttl=2.5)
    except Exception:  # noqa: BLE001
        return ""
    host = urllib.parse.urlsplit(target_url).netloc
    want_ratio = ""
    match_ratio = re.search(r"[?&]ratio=(\d+)", target_url)
    if match_ratio:
        want_ratio = match_ratio.group(1)
    lines = _huya_mp_lines(data, 0)
    # 先按「同 CDN + 同 ratio（同一档位）」找，找不到再退回同 CDN，最后退回第一条
    for line in lines:
        if urllib.parse.urlsplit(line).netloc != host:
            continue
        got = re.search(r"[?&]ratio=(\d+)", line)
        if want_ratio and got and got.group(1) == want_ratio:
            return line
    for line in lines:
        if urllib.parse.urlsplit(line).netloc == host:
            return line
    return lines[0] if lines else ""


def _huya_resign(url: str, to_flv: bool = False) -> str | None:
    """重签虎牙线路的 wsSecret。

    接口给的地址里 wsSecret 是绑定旧 seqid 的，直接拿来拉流会被 CDN 判 403；
    用 fm 解出的前缀 + 新的 seqid 现算一次就能正常出数据（实测 flv/hls 都 200）。
    """
    import base64  # noqa: PLC0415
    import hashlib  # noqa: PLC0415
    import time  # noqa: PLC0415
    from urllib.parse import unquote  # noqa: PLC0415

    if not url:
        return None
    m = re.match(r"^(https?:)?//([^/]+)(/[^?]+?)(\.m3u8|\.flv)(\?.*)?$", url)
    if not m:
        return None
    scheme = m.group(1) or "http:"
    host, path, ext, query = m.group(2), m.group(3), m.group(4), m.group(5) or ""
    params: dict[str, str] = {}
    for part in query.lstrip("?").split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            params[key] = value
    ws_time = params.get("wsTime", "")
    fm = unquote(params.get("fm", ""))
    ratio = params.get("ratio", "")
    if not (ws_time and fm):
        return None
    stream_name = path.rsplit("/", 1)[-1]
    try:
        prefix = base64.b64decode(fm + "=" * (-len(fm) % 4)).decode("utf-8", "replace").split("_")[0]
    except Exception:  # noqa: BLE001
        return None
    seq = str(int(time.time() * 1e7))
    secret = hashlib.md5("_".join([prefix, "0", stream_name, seq, ws_time]).encode()).hexdigest()
    query_new = f"wsSecret={secret}&wsTime={ws_time}&u=0&seqid={seq}&fm={fm}"
    if ratio:
        query_new = f"ratio={ratio}&" + query_new
    if to_flv:
        host = host.replace("hls", "flv")
        path = path + ".flv"
    else:
        path = path + ext
    return f"{scheme}//{host}{path}?{query_new}"


def _huya_mp_lines(data: dict[str, Any], target_bitrate: int = 0) -> list[str]:
    """把移动端接口里的线路重签后拼成可录地址列表。

    顺序：先按码率就近排的 HLS（能按 ratio 选档），再补重签过的 FLV 直链
    （HLS 某些 CDN 会 403，录制端会逐条轮换到 flv）。
    """
    stream = data.get("stream") or {}
    hls_urls: list[str] = []
    for item in ((stream.get("hls") or {}).get("multiLine") or []):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        signed = _huya_resign(str(item["url"]), to_flv=False)
        if signed and signed not in hls_urls:
            hls_urls.append(signed)
    target = target_bitrate or 0
    if hls_urls:
        def _ratio(u: str) -> int:
            m = re.search(r"[?&]ratio=(\d+)", u)
            return int(m.group(1)) if m else 0

        if target:
            hls_urls.sort(key=lambda u: abs((_ratio(u) or target) - target))
        else:
            hls_urls.sort(key=lambda u: -_ratio(u))
    flv_urls: list[str] = []
    for item in stream.get("baseSteamInfoList") or []:
        if not isinstance(item, dict):
            continue
        cdn = str(item.get("sFlvUrl") or "")
        name = str(item.get("sStreamName") or "")
        suffix = str(item.get("sFlvUrlSuffix") or "flv")
        anti = str(item.get("sFlvAntiCode") or "")
        if not (cdn and name):
            continue
        query = f"?{anti}" if anti else ""
        raw = f"{cdn}/{name}.{suffix}{query}"
        signed = _huya_resign(raw, to_flv=True) or raw
        if signed not in flv_urls:
            flv_urls.append(signed)
    # 实测同一时刻有的 CDN 403、有的 200，hs 系最稳、al 最差，先按 CDN 排一层再按码率
    def _cdn_rank(u: str) -> int:
        host = u.split("/")[2] if "//" in u else u
        if host.startswith("hs."):
            return 0
        if host.startswith("tx."):
            return 1
        return 2

    hls_urls.sort(key=_cdn_rank)
    flv_urls.sort(key=_cdn_rank)
    return hls_urls + flv_urls


def _huya_probe(url: str) -> bool:
    """用 ffmpeg 空跑 3 秒，确认这条线路真的能出数据（403/秒断的线路直接淘汰）。"""
    import subprocess  # noqa: PLC0415

    ffmpeg = paths.ffmpeg_exe()
    if not ffmpeg:
        return True
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-rw_timeout", "15000000",
           "-tls_verify", "0", "-i", url, "-t", "3", "-c", "copy", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="ignore", timeout=40)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0 and "Forbidden" not in (proc.stderr or "")


_HUYA_RATE_PAT = re.compile(
    r'"sDisplayName"\s*:\s*"([^"]*)"\s*,\s*"iBitRate"\s*:\s*(\d+)\s*,\s*"iCodecType"\s*:\s*(\d+)')


def _huya_page_rates(html: str) -> list[tuple[str, int, int]]:
    """虎牙页面里 `vBitRateInfo` 的完整档位表：[(档位名, 码率, 编码)]，编码 0=H.264、1=HEVC。

    注意：高档位（蓝光20M / 蓝光10M）在 JSON 里 `iBitRate` 往往是 0，
    只给了名字；这些档位在网页播放器里照样可选，所以不能因为码率是 0 就丢掉。
    """
    rows: list[tuple[str, int, int]] = []
    for name, bitrate, codec in _HUYA_RATE_PAT.findall(html or ""):
        clean = name.strip()
        if clean:
            rows.append((clean, int(bitrate or 0), int(codec or 0)))
    return rows


def _huya_mobile_profile(room: str, cookie: str, proxy: str) -> dict[str, Any]:
    """虎牙的 PC 接口（cache.php?do=profileRoom）已经上风控、返回验证页；
    移动页 m.huya.com 仍会把整块 roomProfile JSON 塞在 HTML 里，用老安卓 UA 才拿得到。"""
    import requests  # noqa: PLC0415

    headers = {"User-Agent": _ANDROID_UA, "Referer": f"https://m.huya.com/{room}"}
    if cookie:
        headers["Cookie"] = cookie
    html = requests.get(f"https://m.huya.com/{room}", headers=headers,
                        proxies=_proxies(proxy), timeout=20).text

    def pick(*keys: str) -> str:
        for key in keys:
            m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"]*)"', html)
            if m and m.group(1):
                return m.group(1)
        return ""

    live = bool(re.search(r'"eLiveStatus"\s*:\s*2', html))
    line_m = re.search(r'"liveLineUrl"\s*:\s*"([A-Za-z0-9+/=]+)"', html)
    return {
        "anchor_name": pick("sNick", "sNickName", "sActorNick"),
        "title": pick("sRoomName", "sTitle"),
        "introduction": pick("sIntroduction"),
        "is_live": live,
        "line_b64": line_m.group(1) if line_m else "",
        "lines": _huya_stream_lines(html),
        "rates": _huya_page_rates(html),
    }


def _huya_playlist_text(url: str, proxy: str) -> str:
    """取一下播放列表的正文（200 才返回，失败给空串），用来判断是 media 还是 master。"""
    import requests  # noqa: PLC0415

    try:
        resp = requests.get(url, headers={"User-Agent": _ANDROID_UA,
                                          "Referer": "https://m.huya.com/"},
                            proxies=_proxies(proxy), timeout=(5, 8), stream=True)
        if resp.status_code != 200:
            resp.close()
            return ""
        # 只读前 64KB：虎牙偶尔会回一条超长/不结束的响应，读到底会把解析卡死。
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(8192):
            chunks.append(chunk)
            total += len(chunk)
            if total >= 65536:
                break
        resp.close()
        return b"".join(chunks).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


def _huya_url_alive(url: str, proxy: str) -> bool:
    """拉一下流地址的前几百字节，确认不是 403/404 的死链。"""
    import requests  # noqa: PLC0415

    try:
        resp = requests.get(url, headers={"User-Agent": _ANDROID_UA}, proxies=_proxies(proxy),
                            timeout=10, stream=True)
        ok = resp.status_code == 200
        ctype = str(resp.headers.get("Content-Type") or "").lower()
        resp.close()
        return ok and ("video" in ctype or "octet-stream" in ctype or "application/" in ctype)
    except Exception:  # noqa: BLE001
        return False


def _huya_sign_url(line_b64: str) -> str | None:
    """把 liveLineUrl 解出来的地址重新签名成可长期拉取的 flv（real-url 的 live() 算法）。

    原始地址里的 wsSecret 与 wsTime 是绑定时间戳的，直接拿来录几分钟就 403；
    这里用 fm 解出的前缀 + 新的 seqid 现算 wsSecret。
    """
    import base64  # noqa: PLC0415
    import hashlib  # noqa: PLC0415
    import time  # noqa: PLC0415

    if not line_b64:
        return None
    b64 = line_b64 + "=" * (-len(line_b64) % 4)
    try:
        text = base64.b64decode(b64).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    text = text.strip().replace("\x00", "")
    if not text:
        return None
    if "replay" in text:
        return text
    cut = re.search(r"\.(m3u8|flv)", text)
    if not cut:
        return None
    base = text[: cut.end()]
    tail = text[cut.end():]
    ws_time = re.search(r"wsTime=([0-9a-fA-F]+)", tail)
    fm = re.search(r"fm=([A-Za-z0-9+/=]+)", tail)
    ratio = re.search(r"ratio=(\d+)", tail)
    if not (ws_time and fm):
        return None
    stream_name = re.sub(r"\.(flv|m3u8)$", "", base.rsplit("/", 1)[-1])
    fmv = fm.group(1)
    prefix = base64.b64decode(fmv + "=" * (-len(fmv) % 4)).decode("utf-8", "replace").split("_")[0]
    seq = str(int(time.time() * 1e7))
    ws_time_v = ws_time.group(1)
    secret = hashlib.md5("_".join([prefix, "0", stream_name, seq, ws_time_v]).encode()).hexdigest()
    query = f"wsSecret={secret}&wsTime={ws_time_v}&u=0&seqid={seq}&fm={fmv}"
    if ratio:
        query = f"ratio={ratio.group(1)}&" + query
    url = "https:" + base + "?" + query
    return url.replace("hls", "flv").replace(".m3u8", ".flv")


_huya_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_huya_preferred: dict[str, int] = {}
_HUYA_CACHE_TTL = 10.0


def _resolve_huya(url: str, quality_code: str, cookie: str, proxy: str) -> dict[str, Any]:
    import time  # noqa: PLC0415

    room = _huya_room_id(url, proxy)
    now = time.time()
    cached = _huya_cache.get(room)
    if cached and now - cached[0] < _HUYA_CACHE_TTL:
        return dict(cached[1])

    try:
        data = _huya_mp_data(room, cookie, proxy)
    except ResolveError:
        # 被限流时宁可沿用上一次结果，也不要让正在录的房间掉到错误态
        if cached:
            return dict(cached[1])
        raise

    live = str(data.get("liveStatus") or "").upper() == "ON"
    profile = data.get("profileInfo") or {}
    live_data = data.get("liveData") or {}
    result: dict[str, Any] = {
        "anchor_name": str(profile.get("nick") or "").strip(),
        "title": str(live_data.get("roomName") or "").strip(),
        "is_live": live,
    }
    if live:
        candidates = _huya_mp_lines(data, _huya_target_bitrate(quality_code))
        if not candidates:
            page = _huya_mobile_profile(room, cookie, proxy)
            signed = _huya_sign_url(page.get("line_b64", ""))
            if signed:
                candidates = [signed]
            candidates += [line.get("url") or "" for line in page.get("lines") or []]
        candidates = [c for c in candidates if c.startswith("http")]
        if not candidates:
            raise ResolveError("虎牙未返回可用直播流（可能是付费/加密频道，或该房间已切到未支持的线路）")
        # 各路 CDN 在同一时刻的可用性不一样（实测常是 hs 通、al/tx 403），
        # 而且 wsSecret 可以本地重算，所以这里可以放心逐条「重签 + 探一下」挑出能用的那条。
        # 优先挑「直接给分片的 media 播放列表」的线路：虎牙有些线路返回 master，
        # 跟一跳拿到的子列表常常是 3 个分片、不滚动的死窗口，录 3 秒就 EOF。
        media_index = -1
        for index, candidate in enumerate(candidates[:6]):
            signed = _huya_resign(candidate) or candidate
            if "#EXTINF" in _huya_playlist_text(signed, proxy):
                media_index = index
                break
        if media_index >= 0:
            candidates.insert(0, candidates.pop(media_index))
        else:
            for index, candidate in enumerate(candidates[:6]):
                signed = _huya_resign(candidate) or candidate
                if _huya_url_alive(signed, proxy):
                    candidates.insert(0, candidates.pop(index))
                    break
        pref = _huya_preferred.get(room, 0) % len(candidates)
        order = candidates[pref:] + candidates[:pref]
        result.update({"quality": quality_code, "flv_url": order[0], "record_url": order[0],
                       "lines": order})
        _huya_preferred[room] = (pref + 1) % len(candidates)

    _huya_cache[room] = (now, dict(result))
    return result


# ---------------- 每个直播间实际可选的画质 ----------------

_QUALITY_ORDER = ["原画", "蓝光", "超清", "高清", "标清", "流畅"]

_QUALITY_BITRATE = {"OD": 100000, "BD": 6000, "UHD": 2000, "HD": 1000, "SD": 800, "LD": 500}

_QUALITY_LABEL = {"OD": "原画", "BD": "蓝光", "UHD": "超清", "HD": "高清", "SD": "标清", "LD": "流畅"}

_BILI_QN_NAME = {"30000": "原画", "20000": "原画", "15000": "原画", "10000": "原画",
                 "401": "蓝光", "400": "蓝光", "250": "超清", "150": "高清", "80": "流畅"}


def _quality_item(value: str, label: str, native: str = "", note: str = "",
                  default: bool = False) -> dict[str, Any]:
    return {"value": value, "label": label, "native": native, "note": note, "default": default}


def _name_to_quality(name: str) -> str:
    """把平台自己的清晰度名（蓝光10M / 超清 / 高清 / 流畅 …）映射到我们的六档。"""
    text = name or ""
    for key in ("原画", "蓝光", "超清", "高清", "标清", "流畅"):
        if key in text:
            return key
    if "8M" in text or "10M" in text or "4K" in text:
        return "蓝光"
    return ""


def _bili_probe_qn(headers: dict[str, str], room_id: str, qn: int, proxy: str) -> int:
    """问一次「我要 qn」，返回**实际会录到**的 qn（拿不到返回 0）。

    用 `_bili_pick` 同一套选流逻辑（优先 avc），所以报告的就是录制拿到的那条流；
    不能取所有 codec 的 current_qn 最大值——av1 常有 400，而录的是 avc 的 250。
    """
    import requests  # noqa: PLC0415

    try:
        resp = requests.get(
            "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo",
            params={"room_id": room_id, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
                    "qn": str(qn), "platform": "web", "ptype": "8"},
            headers=headers, proxies=_proxies(proxy), timeout=12)
        payload = (resp.json().get("data") or {})
    except Exception:  # noqa: BLE001
        return 0
    url, actual = _bili_pick(payload, str(qn))
    if not url:
        return 0
    try:
        return int(actual)
    except (TypeError, ValueError):
        return 0


def _bili_qualities(room_id: str, cookie: str, proxy: str) -> list[dict[str, Any]]:
    import requests  # noqa: PLC0415

    headers = _bili_headers(cookie)
    try:
        resp = requests.get(
            "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo",
            params={"room_id": room_id, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
                    "qn": "10000", "platform": "web", "ptype": "8"},
            headers=headers, proxies=_proxies(proxy), timeout=15)
        payload = resp.json()
    except Exception:  # noqa: BLE001 - 拿不到就退回默认列表
        return []
    playurl = ((payload.get("data") or {}).get("playurl_info") or {}).get("playurl") or {}
    descs = {str(item.get("qn")): str(item.get("desc") or "")
             for item in playurl.get("g_qn_desc") or [] if isinstance(item, dict)}
    accept: list[int] = []
    current: list[int] = []
    for stream in playurl.get("stream") or []:
        for fmt in stream.get("format") or []:
            for codec in fmt.get("codec") or []:
                for qn in codec.get("accept_qn") or []:
                    if isinstance(qn, int) and qn not in accept:
                        accept.append(qn)
                cur = codec.get("current_qn")
                if isinstance(cur, int):
                    current.append(cur)
    accept.sort(reverse=True)
    limit = max(current) if current else 0

    # 候选：只考虑「不超过当前账号上限」的档位，然后逐档探测真实返回值。
    tier_qns = sorted({int(k) for k in descs if k.isdigit()} or set(accept), reverse=True)
    candidates = [qn for qn in tier_qns if not limit or qn <= limit][:5]

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, qn in enumerate(candidates):
        name = descs.get(str(qn)) or _BILI_QN_NAME.get(str(qn)) or _BILI_QN_DESC.get(str(qn)) or ""
        if not name:
            continue
        got = _bili_probe_qn(headers, room_id, qn, proxy) if len(candidates) > 1 else qn
        if got and got != qn:
            # 请求这一档却拿到别的档位：说明账号权限不够，只保留「最高那档实际值」，别再列假的
            if index == 0:
                real_name = (descs.get(str(got)) or _BILI_QN_NAME.get(str(got))
                             or _BILI_QN_DESC.get(str(got)) or name)
                value = _name_to_quality(real_name) or _BILI_QN_NAME.get(str(got), "原画")
                if value not in seen:
                    seen.add(value)
                    items.append(_quality_item(value, real_name, str(got)))
            break
        value = _name_to_quality(name) or _BILI_QN_NAME.get(str(qn), "原画")
        if value in seen:
            continue
        seen.add(value)
        items.append(_quality_item(value, name, str(qn)))
    if items:
        items[0]["default"] = True
    return items


def _huya_qualities(data: dict[str, Any],
                    page_rates: list[tuple[str, int, int]] | None = None) -> list[dict[str, Any]]:
    """虎牙的可选画质有两个来源，合并去重后每个码率各占一项。

    1) 页面里的 `vBitRateInfo`（最全：含「蓝光20M / 蓝光10M / 2K HDR」这类高档位，
       它们的 `iBitRate` 常是 0，只能从档位名里读出码率）；
    2) `mp.huya.com` 的 `stream.hls.rateArray`（`iBitRate` + `sDisplayName`，实际可拉的线路档）。
    不做「六档归一」，否则 蓝光20M 与 蓝光6M 会互相覆盖。
    """
    rows: list[tuple[int, str]] = []

    def add(bitrate: int, name: str) -> None:
        if not name:
            return
        for existing, _ in rows:
            if existing == bitrate:
                return
        rows.append((bitrate, name))

    for name, bitrate, _codec in page_rates or []:
        rate = bitrate or _huya_target_bitrate(name)
        if rate <= 0:
            rate = 1  # 名字里读不出码率的档位排到末尾，但仍然展示
        add(rate, name)

    rate_array = (((data.get("stream") or {}).get("hls") or {}).get("rateArray") or [])
    for rate in rate_array:
        if not isinstance(rate, dict):
            continue
        name = str(rate.get("sDisplayName") or "").strip()
        bitrate = int(rate.get("iBitRate") or 0)
        if bitrate <= 0:
            bitrate = _huya_target_bitrate(name) or 1
        add(bitrate, name)

    rows.sort(key=lambda x: -x[0])

    items: list[dict[str, Any]] = []
    for index, (bitrate, name) in enumerate(rows):
        # 最高档就是原画（虎牙常把它叫「蓝光20M」）
        if index == 0:
            label = f"原画（{name}）" if "原画" not in name else name
        else:
            label = name
        items.append(_quality_item(label, label, str(bitrate)))
    for item in items:
        item["note"] = "虎牙每档对应一条不同码率的线路，录制时优先走码率最接近的线路"
    if items:
        items[0]["default"] = True
    return items


def _douyu_qualities(rid: str, cookie: str, proxy: str) -> list[dict[str, Any]]:
    import requests  # noqa: PLC0415

    headers = {"User-Agent": _DOUYU_UA, "Referer": f"https://www.douyu.com/{rid}"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        room = (requests.get(f"https://www.douyu.com/betard/{rid}", headers=headers,
                             proxies=_proxies(proxy), timeout=15).json().get("room") or {})
    except Exception:  # noqa: BLE001
        return []

    # 斗鱼的 multirates 里 type=0 是原画（最高码率），其余按平台自己的名字映射；名字认不出再看 type。
    _BY_TYPE = {"0": "原画", "4": "蓝光", "3": "超清", "2": "高清", "1": "流畅"}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rate in room.get("multirates") or []:
        if not isinstance(rate, dict):
            continue
        name = str(rate.get("name") or "").strip()
        rtype = str(rate.get("type", ""))
        # rate=0 在斗鱼语义里就是「原画」（最高码率），即使它叫「蓝光10M」。
        value = "原画" if rtype == "0" else (_name_to_quality(name) or _BY_TYPE.get(rtype, ""))
        if not value or value in seen:
            continue
        seen.add(value)
        label = name or value
        if value == "原画" and name and "原画" not in name:
            label = f"原画（{name}）"
        items.append(_quality_item(value, label, name))
    for item in items:
        item["note"] = "斗鱼按所选档位切换码率（原画最高），录制时按档位取流"
    # 关键：斗鱼「能选哪些档位」不取决于房间的 multirates（很多房间只标注了「蓝光10M」一条），
    # 而是取决于 getH5PlayV1 的 rate（0=原画 / 3=蓝光 / 2=超清 / 1=流畅）。实测同一房间
    # rate0/3/1 各录 6 秒分别是 2.4MB / 1.0MB / 0.7MB，说明档位是真的生效的。
    # 所以这里始终补齐四档，已有同值的房间自定义名字保持不变。
    _FIXED = [("原画", "原画（最高码率）"), ("蓝光", "蓝光"), ("超清", "超清"), ("流畅", "流畅")]
    if not items:
        items = [_quality_item(value, label, value) for value, label in _FIXED]
    else:
        have = {item.get("value") for item in items}
        for value, label in _FIXED:
            if value not in have:
                items.append(_quality_item(value, label, value))
    for item in items:
        item["note"] = "斗鱼按所选档位切换码率（原画最高），录制时按档位取流"
    if items:
        items[0]["default"] = True
    return items


def _douyin_qualities(room: dict[str, Any]) -> list[dict[str, Any]]:
    stream = room.get("stream_url") or {}
    pull = stream.get("flv_pull_url") or {}
    has_origin = bool(((stream.get("live_core_sdk_data") or {}).get("pull_data") or {}).get("stream_data"))
    items: list[dict[str, Any]] = []
    if has_origin:
        items.append(_quality_item("原画", "原画（1080P）", "origin", default=True))
    pairs = [("FULL_HD1", "蓝光", "蓝光（高码率）"), ("HD1", "超清", "超清"),
             ("SD1", "高清", "高清"), ("SD2", "流畅", "流畅")]
    seen = {item["value"] for item in items}
    for key, value, label in pairs:
        if key in pull and value not in seen:
            seen.add(value)
            items.append(_quality_item(value, label, key))
    if items and not any(item["default"] for item in items):
        items[0]["default"] = True
    return items


def _blocking_qualities(url: str, platform: str, cookie: str, proxy: str) -> dict[str, Any]:
    info: dict[str, Any] = {"ok": True, "platform": platform, "anchor": "", "title": "",
                            "isLive": False, "qualities": [], "note": ""}
    if platform == "bilibili":
        room_id = _bili_room_id(url)
        info["qualities"] = _bili_qualities(room_id, cookie, proxy)
        has_origin = any(item.get("value") in ("原画", "OD") for item in info["qualities"])
        if has_origin:
            info["note"] = "已登录：可以选「原画」；档位越高占用带宽越大"
        elif len(info["qualities"]) > 1:
            info["note"] = "B站未登录时最高只到「超清」；填了登录 Cookie 后这里会出现「原画」"
        else:
            got = (info["qualities"][0].get("label") if info["qualities"] else "超清")
            info["note"] = (f"B站未登录时只会下发「{got}」这一档（选更高档也会被降级，所以这里不列）；"
                            "在「设置 → B站 cookie」填入登录 Cookie 后会出现原画/蓝光等档位")
    elif platform == "douyu":
        rid = _douyu_room_id(url)
        info["qualities"] = _douyu_qualities(rid, cookie, proxy)
        try:
            room_info = _douyu_info(url, cookie, proxy)
            info["anchor"] = room_info.get("anchor_name") or ""
            info["title"] = room_info.get("title") or ""
            info["isLive"] = bool(room_info.get("is_live"))
        except Exception:  # noqa: BLE001 - 房间信息拿不到不影响画质列表
            pass
        info["note"] = ("斗鱼按所选档位真正切换码率（原画 / 蓝光 / 超清 / 高清 / 流畅），"
                        "档位来自房间的 multirates；平台没给的档位不会出现在列表里")
    elif platform == "huya":
        room = _huya_room_id(url, proxy)
        data = _huya_mp_data(room, cookie, proxy)
        page_rates: list[tuple[str, int, int]] = []
        try:
            page_rates = _huya_mobile_profile(room, cookie, proxy).get("rates") or []
        except Exception:  # noqa: BLE001 - 页面拿不到就只用接口里的档位
            page_rates = []
        info["qualities"] = _huya_qualities(data, page_rates)
        info["anchor"] = str((data.get("profileInfo") or {}).get("nick") or "")
        info["title"] = str((data.get("liveData") or {}).get("roomName") or "")
        info["isLive"] = str(data.get("liveStatus") or "").upper() == "ON"
        info["note"] = ("虎牙按码率就近选线路，档位以房间实际提供的为准；"
                        "「蓝光20M / 2K HDR」这类高档位只有主播推了对应码率才会出现")
    elif platform == "douyin":
        web_rid = _douyin_web_rid(url, proxy)
        room, user = _douyin_enter(web_rid, cookie, proxy)
        info["qualities"] = _douyin_qualities(room)
        info["anchor"] = str(user.get("nickname") or "")
        info["title"] = str(room.get("title") or "")
        info["isLive"] = room.get("status") == 2
        info["note"] = "抖音按 flv 轨道分档：原画需要登录 Cookie"
    else:
        return {"ok": False, "platform": platform, "error": "暂不支持该平台的可选画质查询"}
    if not info["qualities"]:
        return {"ok": False, "platform": platform, "error": "该直播间没有返回可选画质（可能未开播或接口受限）",
                "anchor": info.get("anchor") or "", "title": info.get("title") or "",
                "isLive": info.get("isLive") or False}
    return info


async def fetch_qualities(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """给「添加/编辑直播间」用：返回该房间真实可选的画质列表。"""
    platform = detect_platform(url)
    if platform in ("unknown", "custom"):
        return {"ok": False, "platform": platform, "error": "无法识别的直播平台链接"}
    try:
        return await asyncio.to_thread(_blocking_qualities, url, platform, cookie, proxy)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) if getattr(exc, "user_facing", False) else f"{type(exc).__name__}: {exc}"
        return {"ok": False, "platform": platform, "error": msg}


def _wanted_bitrate(quality: str) -> int:
    """把「原画 / 蓝光20M / 原画（蓝光6M）」这类名字换算成一个大致码率（Kbps）。"""
    text = quality or ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[Mm]\b", text)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.search(r"\b(\d{3,5})\b", text)
    if m:
        return int(m.group(1))
    base = _name_to_quality(text)
    return {"原画": 100000, "蓝光": 6000, "超清": 2000, "高清": 1500, "标清": 800, "流畅": 500}.get(base, 0)


async def adjust_quality(
    url: str,
    wanted: str,
    cookie: str = "",
    proxy: str = "",
) -> dict[str, Any]:
    """把一个「配置里存着的画质」校正成该房间此刻真实能录到的档位。

    房间档位会随主播推流设置变化，自动/无人值守拉起时不会走「添加直播间」的
    画质探查，所以录制前要再核一次：档位不存在时按码率就近挑一档，并把原因带回去。
    """
    info = await fetch_qualities(url, cookie, proxy)
    items = [it for it in (info.get("qualities") or []) if isinstance(it, dict) and it.get("value")]
    if not info.get("ok") or not items:
        return {"ok": False, "effective": "", "note": ""}
    if wanted in {str(it["value"]) for it in items}:
        return {"ok": True, "effective": wanted, "note": ""}

    def _bitrate_of(item: dict[str, Any]) -> int:
        try:
            native = int(str(item.get("native") or "").strip() or 0)
        except ValueError:
            native = 0
        return native or _wanted_bitrate(str(item.get("value") or "")) or 1

    # 「原画」这类通用叫法对上平台自己的档位名（原画（蓝光30M））时直接认同一档，不啰嗦
    canon = _name_to_quality(wanted)
    if canon:
        same = [it for it in items if _name_to_quality(str(it["value"])) == canon]
        if same:
            best_same = max(same, key=_bitrate_of)
            effective_same = str(best_same["value"])
            note_same = "" if (effective_same == wanted or wanted in effective_same) else f"该房间的这一档叫「{effective_same}」，已按它录制"
            return {"ok": True, "effective": effective_same, "note": note_same}

    target = _wanted_bitrate(wanted)
    best = items[0]
    if target:
        best = min(items, key=lambda it: (abs(_bitrate_of(it) - target), -_bitrate_of(it)))
    effective = str(best["value"])
    note = f"该房间当前没有「{wanted}」这一档，已按「{effective}」录制"
    return {"ok": True, "effective": effective, "note": note}


def _blocking_resolve(
    url: str,
    platform: str,
    quality_code: str,
    cookie: str,
    proxy: str,
    semaphore: threading.Semaphore | None = None,
    quality_raw: str = "",
) -> dict[str, Any]:
    """在线程中执行；解析全部走内置实现，不再依赖上游 core.src。

    quality_raw 是用户在界面上选的那一档的原文（虎牙会是「蓝光20M」这种带码率的名字），
    不能归一成六档码，否则虎牙多档位会被合并。
    """
    own = semaphore or _GLOBAL_SEM
    own.acquire()
    try:
        if platform == "douyin":
            info = _resolve_douyin(url, quality_code, cookie, proxy)
        elif platform == "huya":
            info = _resolve_huya(url, quality_raw or quality_code, cookie, proxy)
        elif platform == "douyu":
            info = _resolve_douyu(url, _name_to_quality(quality_raw) or quality_code, cookie, proxy)
        elif platform == "bilibili":
            info = _resolve_bilibili(url, quality_code, cookie, proxy)
        else:
            raise ResolveError(f"暂不支持的平台链接：{url}")
    finally:
        own.release()

    if not isinstance(info, dict):
        raise ResolveError("解析结果为空")

    anchor = (info.get("anchor_name") or "").strip()
    title = (info.get("title") or "").strip()
    if not info.get("is_live"):
        return {"is_live": False, "anchor_name": anchor, "title": title, "record_url": ""}

    record_url = select_source_url(url, info)
    if not record_url:
        return {"is_live": False, "anchor_name": anchor, "title": title, "record_url": ""}
    return {
        "is_live": True,
        "anchor_name": anchor,
        "title": title,
        "record_url": record_url,
        "flv_url": info.get("flv_url"),
        "m3u8_url": info.get("m3u8_url"),
        "lines": info.get("lines") or [],
        "quality_note": info.get("quality_note") or "",
        "quality_actual": info.get("quality_actual") or "",
    }


_GLOBAL_SEM = threading.Semaphore(6)


def set_concurrency(n: int) -> None:
    global _GLOBAL_SEM
    _GLOBAL_SEM = threading.Semaphore(max(1, int(n)))


async def resolve(
    url: str,
    quality_zh: str = "原画",
    cookie: str = "",
    proxy: str = "",
    semaphore: threading.Semaphore | None = None,
) -> dict[str, Any]:
    """异步门面：返回 {is_live, anchor_name, title, record_url, platform, error}。"""
    from .config import QUALITY_CODES

    platform = detect_platform(url)
    if platform == "unknown":
        return {"is_live": False, "platform": platform, "anchor_name": "", "title": "",
                "record_url": "", "error": "无法识别的直播平台链接"}
    if platform == "custom":
        return {"is_live": True, "platform": platform, "anchor_name": "", "title": "",
                "record_url": url}

    code = QUALITY_CODES.get(quality_zh, "OD")
    try:
        info = await asyncio.to_thread(_blocking_resolve, url, platform, code, cookie, proxy,
                                       semaphore, quality_zh)
    except Exception as exc:  # 解析失败只影响该房间
        msg = str(exc) if getattr(exc, "user_facing", False) else f"{type(exc).__name__}: {exc}"
        return {"is_live": False, "platform": platform, "anchor_name": "", "title": "",
                "record_url": "", "error": msg}
    info["platform"] = platform
    return info
