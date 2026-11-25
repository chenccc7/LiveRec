"""虎牙直播录制实现（移植自 stream-rec 的 Huya extractor，MIT）。

取流链路完全对照 stream-rec：`platforms/.../huya/download/HuyaExtractor*.kt`
1. 房间信息：`mp.huya.com/cache.php?do=profileRoom&m=Live&roomid=<rid>&showSecret=1`，
   iPhone 微信 UA；开播判定要 `realLiveStatus == "ON" and liveStatus == "ON"`，
   并且跳过标题以「【回放】」开头的房间。
2. 画质：`liveData.bitRateInfo`（没有就退 `stream.flv.rateArray`），
   **首档码率取 `liveData.bitRate`**（首项的 iBitRate 常是 0），
   再把 `sFlvAntiCode` 里 `exsphd` 携带的额外档位（HDR/杜比）合并进来；名字带 HDR 的档位跳过。
3. 线路：`stream.baseSteamInfoList` 里每个 CDN 都拼 flv + hls 两套地址，
   antiCode 自己算：
       u        = ((uid << 8) | (uid >> 24)) & 0xFFFFFFFF
       seqid    = int((int(wsTime, 16) + rand()) * 1000) + uid
       ss       = md5(f"{seqid}|{ctype}|{platformId}")
       wsSecret = md5(f"{fm}_{u}_{streamName}_{ss}_{wsTime}")
   参数还要带 ctype / fs / u / t / ver=1 / uuid / sdk_sid / **codec=264**（+ ratio），
   然后按 CDN 的 `iWebPriorityRate` 排序、按码率档位组合成候选。
4. 兜底：算出来的地址拉不动时，stream-rec 会走 WUP
   `POST https://wup.huya.com`（TARS `liveui.getCdnTokenInfo`，HYSDK UA）拿服务端 antiCode。
   这里按同样的思路实现了 `wup_token()`，失败就退回自己算的那份，不影响主流程。

与本项目其它模块的约定：
- `resolve()` / `qualities()` / `next_stream()` / `drop_cache()` / `resign()` / `fresh_line()`
  是 resolver 与 recorder 依赖的对外接口，签名保持不变。
- HLS 中转（`liverec/hlsproxy.py`）每次刷新都会调 `fresh_line()` 要一条**新签**的地址。
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import struct
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .resolver import (
    ResolveError,
    _ANDROID_UA,
    _QUALITY_BITRATE,
    _name_to_quality,
    _proxies,
    _quality_item,
)

# 虎牙的签名地址有效期只有二三十秒，录制端按这个粒度分段续录（每段重新取一次签名）。
CHUNK_SECONDS = 28.0

MP_BASE_URL = "https://mp.huya.com/cache.php"
WUP_URL = "https://wup.huya.com"
COOKIE_VERIFY_URL = "https://udblgn.huya.com/web/cookie/verify"
HY_BASE = "https://www.huya.com"

APP_ID = 5002
WEB_PLATFORM_ID = 100

# stream-rec 用的两个 UA：
# - 取房间信息走 iPhone 微信 UA（PC/安卓拿到的字段不全，且更便宜被风控）
# - 只有 WUP 请求用 HYSDK UA（对应的版本号区间被服务端接受）
UA_IPHONE_WX = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003137) "
    "NetType/WIFI Language/zh_CN WeChat/8.0.49.33 CFNetwork/1474 Darwin/23.0.0"
)
HYSDK_UA = "HYSDK(Windows,30000002)_APP(pc_exe&7030003&official)_SDK(trans&2.29.0.5493)"

# WUP 请求体的编码版本：stream-rec 用 TARS 的 UniAttribute，
# v1 = map<string, bytes>，v3 = map<string, map<string, bytes>>。两种都试过，见 _wup_token()。
_WUP_STYLE = "v1"

_MP_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MP_TTL = 2.5
_PREFERRED: dict[str, int] = {}
_ANTI_CACHE: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------- 工具


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _get(url: str, headers: dict[str, str], proxy: str, timeout: float = 15.0) -> str:
    import requests  # noqa: PLC0415

    resp = requests.get(url, headers=headers, proxies=_proxies(proxy), timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _b64(text: str) -> str:
    text = text or ""
    return base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", "replace")


# --------------------------------------------------------------------------- 房间


def room_id(url: str, proxy: str = "") -> str:
    """从链接里取数字房间号；短名（dank1ng）会去页面里找 `lProfileRoom`。"""
    match = re.search(r"huya\.com/([^/?#]+)", url or "")
    if not match:
        raise ResolveError("没能从链接里认出虎牙房间号，请贴 https://www.huya.com/<房间号>")
    raw = match.group(1)
    if raw.isdigit():
        return raw
    # 短名：lChannelId 是主播 uid（当房间号用会 422），真正的房间号在 lProfileRoom
    html = ""
    for page in (f"https://m.huya.com/{raw}", f"{HY_BASE}/{raw}"):
        try:
            html = _get(page, {"user-agent": _ANDROID_UA, "referer": HY_BASE}, proxy, 20)
        except Exception:  # noqa: BLE001
            continue
        for pattern in (r'"lProfileRoom"\s*:\s*(\d{2,12})', r'"lSid"\s*:\s*(\d{2,12})',
                        r'"roomId"\s*:\s*"?(\d{2,12})'):
            found = re.search(pattern, html)
            if found and int(found.group(1)) != 0:
                return found.group(1)
    raise ResolveError("虎牙这个主播名解析不到房间号，请改用 https://www.huya.com/<数字房间号>")


def mp_data(room: str, cookie: str, proxy: str, ttl: float | None = None) -> dict[str, Any]:
    """`mp.huya.com` 的房间信息（stream-rec 的 isLive + extract 都走这个接口）。"""
    ttl = _MP_TTL if ttl is None else ttl
    key = f"{room}|{len(cookie)}|{proxy}"
    cached = _MP_CACHE.get(key)
    if cached and ttl > 0 and time.time() - cached[0] < ttl:
        return cached[1]

    import requests  # noqa: PLC0415

    headers = {
        "user-agent": UA_IPHONE_WX,
        "referer": f"{HY_BASE}/{room}",
        "accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["cookie"] = cookie
    try:
        resp = requests.get(MP_BASE_URL, params={
            "do": "profileRoom", "m": "Live", "roomid": room, "showSecret": "1",
        }, headers=headers, proxies=_proxies(proxy), timeout=20)
    except Exception as exc:  # noqa: BLE001
        raise ResolveError(f"虎牙接口请求失败：{type(exc).__name__}: {exc}") from exc

    text = (resp.text or "").strip()
    if not text.startswith("{"):
        raise ResolveError("虎牙接口返回风控页面（该平台已加强反爬，稍后重试或改用代理）")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResolveError("虎牙接口返回的不是 JSON（可能被风控）") from exc

    if not isinstance(payload, dict) or payload.get("status") != 200:
        message = (payload or {}).get("message") or ""
        if "不存在" in message:
            raise ResolveError(f"虎牙没找到这个房间：{message}")
        raise ResolveError(f"虎牙接口返回异常：status={(payload or {}).get('status')} {message}")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ResolveError("虎牙接口没给出房间数据")
    _MP_CACHE[key] = (time.time(), data)
    return data


def is_live(data: dict[str, Any]) -> bool:
    live_data = data.get("liveData") or {}
    intro = str(live_data.get("introduction") or "")
    if intro.startswith("【回放】"):  # 回放不是直播
        return False
    return data.get("realLiveStatus") == "ON" and data.get("liveStatus") == "ON"


def profile(data: dict[str, Any]) -> dict[str, str]:
    info = data.get("profileInfo") or {}
    live_data = data.get("liveData") or {}
    return {
        "anchor": str(info.get("nick") or ""),
        "title": str(live_data.get("introduction") or ""),
        "avatar": str(info.get("avatar180") or ""),
        "uid": str(info.get("uid") or ""),
    }


def _presenter_uid(data: dict[str, Any]) -> int:
    try:
        return int(str((data.get("profileInfo") or {}).get("uid") or 0))
    except (TypeError, ValueError):
        return 0


def _viewer_uid(cookie: str, data: dict[str, Any]) -> int:
    """签名里的 uid：配置里的 yyuid/udb_uid > 主播 uid > 随机一个（stream-rec 的做法）。"""
    if cookie:
        for key in ("yyuid", "udb_uid"):
            found = re.search(rf"(?:^|;\s*){key}=(\d+)", cookie)
            if found:
                return int(found.group(1))
    uid = _presenter_uid(data)
    if uid:
        return uid
    return random.randint(12340000, 12349999)


# --------------------------------------------------------------------------- 画质


def _bitrate_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    live_data = data.get("liveData") or {}
    stream = data.get("stream") or {}

    rows: list[Any] = []
    raw = live_data.get("bitRateInfo")
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            rows = []
    if not rows:
        flv = stream.get("flv") or {}
        rows = flv.get("rateArray") or []

    try:
        max_bitrate = int(live_data.get("bitRate") or 0)
    except (TypeError, ValueError):
        max_bitrate = 0

    result: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            continue
        name = str(item.get("sDisplayName") or "").strip()
        try:
            bitrate = int(item.get("iBitRate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        if index == 0 and max_bitrate:
            # 首档的 iBitRate 经常是 0，真实码率在 liveData.bitRate 里
            bitrate = max_bitrate
        if "HDR" in name.upper():
            continue  # 虎牙的 HDR 档拿不到干净的主流，跳过
        result.append({"name": name, "bitrate": bitrate, "codec": item.get("iCodecType", 0)})
    return result


def _extra_bitrates(data: dict[str, Any]) -> list[dict[str, Any]]:
    """`sFlvAntiCode` 的 `exsphd` 里会额外列出更高档位（HDR/杜比），格式 `codec_bitrate,...`。"""
    stream = data.get("stream") or {}
    infos = stream.get("baseSteamInfoList") or []
    if not infos:
        return []
    anti = str((infos[0] or {}).get("sFlvAntiCode") or "")
    query = dict(parse_qsl(anti))
    raw = query.get("exsphd") or ""
    extra: list[dict[str, Any]] = []
    for part in raw.rstrip(",").split(","):
        if "_" not in part:
            continue
        codec, _, bitrate = part.partition("_")
        try:
            value = int(bitrate)
        except ValueError:
            continue
        if value <= 10000:  # 只要超额档（4K/HDR 之类）
            continue
        extra.append({"name": codec, "bitrate": value, "codec": 0})
    return extra


def _quality_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _bitrate_list(data) + _extra_bitrates(data)
    merged: dict[int, dict[str, Any]] = {}
    for row in rows:
        bitrate = int(row.get("bitrate") or 0)
        if bitrate in merged:
            continue
        merged[bitrate] = row
    ordered = sorted(merged.values(), key=lambda r: int(r.get("bitrate") or 0), reverse=True)
    for row in ordered:
        if not row.get("name"):
            row["name"] = "原画" if row is ordered[0] else f"{int(row.get('bitrate') or 0) // 1000}M"
    return ordered


def target_bitrate(quality: str) -> int:
    """把用户选的档位名换算成码率（蓝光20M → 20000）。"""
    text = (quality or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*[Mm]\b", text)
    if match:
        return int(float(match.group(1)) * 1000)
    match = re.search(r"\b(\d{3,5})\b", text)
    if match:
        return int(match.group(1))
    for name, bitrate in (("原画", 0), ("蓝光", 6000), ("超清", 2000), ("高清", 1000),
                          ("标清", 800), ("流畅", 500)):
        if name in text:
            return bitrate
    return _QUALITY_BITRATE.get(text, 0)


def _pick_bitrate(rows: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    if not rows:
        return None
    text = (wanted or "").strip()
    for row in rows:  # 名字完全一致优先
        if row.get("name") == text:
            return row
    target = target_bitrate(text)
    canonical = _name_to_quality(text)
    if canonical:
        same = [r for r in rows if _name_to_quality(str(r.get("name"))) == canonical]
        if same:
            return max(same, key=lambda r: int(r.get("bitrate") or 0))
    if target <= 0:
        return max(rows, key=lambda r: int(r.get("bitrate") or 0))
    return min(rows, key=lambda r: (abs(int(r.get("bitrate") or 0) - target), -int(r.get("bitrate") or 0)))


# --------------------------------------------------------------------------- 签名


def _build_query(anti: str, uid: int, stream_name: str, bitrate: int | None = None) -> str:
    """按 stream-rec 的 buildQuery 复刻 antiCode（含强制 codec=264）。"""
    return _build_query_from(dict(parse_qsl((anti or "").rstrip(","))), uid, stream_name, bitrate)


def _build_query_from(query: dict[str, str], uid: int, stream_name: str,
                      bitrate: int | None = None) -> str:
    ws_time = query.get("wsTime")
    fm_raw = query.get("fm")
    ctype = query.get("ctype")
    if not (ws_time and fm_raw and ctype):
        raise ResolveError("虎牙线路地址缺少签名参数（wsTime / fm / ctype）")
    try:
        ct = int((int(ws_time, 16) + random.random()) * 1000)
    except ValueError as exc:
        raise ResolveError("虎牙线路签名参数 wsTime 不合法") from exc
    seq_id = ct + uid
    fm = _b64(fm_raw).split("_")[0]
    try:
        platform_id = int(query.get("t") or WEB_PLATFORM_ID)
    except ValueError:
        platform_id = WEB_PLATFORM_ID
    ss = _md5(f"{seq_id}|{ctype}|{platform_id}")
    rotated = ((uid << 8) | (uid >> 24)) & 0xFFFFFFFF
    ws_secret = _md5(f"{fm}_{rotated}_{stream_name}_{ss}_{ws_time}")

    params: list[tuple[str, str]] = [
        ("wsSecret", ws_secret),
        ("wsTime", ws_time),
        ("seqid", str(seq_id)),
        ("ctype", ctype),
        ("fs", str(query.get("fs") or "")),
        ("u", str(rotated)),
        ("t", str(platform_id)),
        ("ver", "1"),
        ("uuid", str(int(((ct % 1e10) + random.random()) * 1e3 % 0xFFFFFFFF))),
        # huya 的 h265 是「假 265」，强制 264 兼容性最好
        ("sdk_sid", str(int(time.time()))),
        ("codec", "264"),
    ]
    if bitrate:
        params.append(("ratio", str(bitrate)))
    return urlencode(params)


def _url_for(info: dict[str, Any], uid: int, fmt: str, bitrate: int | None) -> str:
    if fmt == "flv":
        base = str(info.get("sFlvUrl") or "").replace("http://", "https://", 1)
        suffix = str(info.get("sFlvUrlSuffix") or "flv")
        anti = str(info.get("sFlvAntiCode") or "")
    else:
        base = str(info.get("sHlsUrl") or "").replace("http://", "https://", 1)
        suffix = str(info.get("sHlsUrlSuffix") or "m3u8")
        anti = str(info.get("sHlsAntiCode") or "")
    if not base or not anti:
        return ""
    name = str(info.get("sStreamName") or "")
    if not name:
        return ""
    try:
        query = _build_query(anti, uid, name, bitrate)
    except ResolveError:
        return ""
    return f"{base.rstrip('/')}/{name}.{suffix}?{query}"


def _candidates(data: dict[str, Any], wanted: str, cookie: str = "",
                force_origin: bool = False) -> list[dict[str, Any]]:
    """一条线路一条记录：CDN × {flv, hls} × 码率，按 iWebPriorityRate 排序。"""
    stream = data.get("stream") or {}
    infos = stream.get("baseSteamInfoList") or []
    rows = _quality_rows(data)
    picked = _pick_bitrate(rows, wanted)
    uid = _viewer_uid(cookie, data)

    try:
        max_bitrate = int((data.get("liveData") or {}).get("bitRate") or 0)
    except (TypeError, ValueError):
        max_bitrate = 0

    result: list[dict[str, Any]] = []
    for raw in infos:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("sStreamName") or "")
        if force_origin:
            name = name.replace("-imgplus", "")
            raw = dict(raw, sStreamName=name)
        cdn = str(raw.get("sCdnType") or "")
        try:
            priority = int(raw.get("iWebPriorityRate") or 0)
        except (TypeError, ValueError):
            priority = 0
        target_bitrate = int((picked or {}).get("bitrate") or 0)
        bitrate = target_bitrate if target_bitrate and target_bitrate != max_bitrate else None
        for fmt in ("hls", "flv"):
            url = _url_for(raw, uid, fmt, bitrate)
            if not url:
                continue
            result.append({
                "url": url,
                "format": fmt,
                "cdn": cdn,
                "priority": priority,
                "stream_name": name,
                "bitrate": target_bitrate,
                "quality": str((picked or {}).get("name") or wanted),
                "max_bitrate": max_bitrate,
            })
    result.sort(key=lambda c: (-int(c.get("priority") or 0), 0 if c.get("format") == "hls" else 1))
    return result


# --------------------------------------------------------------------------- WUP 兜底


def _tars_head(tag: int, type_id: int) -> bytes:
    if tag < 15:
        return bytes([(tag << 4) | type_id])
    return bytes([(15 << 4) | type_id, tag])


def _tars_int(tag: int, value: int) -> bytes:
    if value == 0:
        return _tars_head(tag, 12)
    if -128 <= value <= 127:
        return _tars_head(tag, 0) + struct.pack("b", value)
    if -32768 <= value <= 32767:
        return _tars_head(tag, 1) + struct.pack(">h", value)
    if -2147483648 <= value <= 2147483647:
        return _tars_head(tag, 2) + struct.pack(">i", value)
    return _tars_head(tag, 3) + struct.pack(">q", value)


def _tars_string(tag: int, value: str) -> bytes:
    data = (value or "").encode("utf-8")
    if len(data) > 255:
        return _tars_head(tag, 7) + struct.pack(">i", len(data)) + data
    return _tars_head(tag, 6) + bytes([len(data)]) + data


def _tars_bytes(tag: int, data: bytes) -> bytes:
    return _tars_head(tag, 13) + _tars_int(0, len(data)) + data


def _tars_empty_map(tag: int) -> bytes:
    return _tars_head(tag, 8) + _tars_int(0, 0) + _tars_int(0, 0)


def _tars_struct_end() -> bytes:
    return _tars_head(0, 11)


def _token_req(cdn: str, stream_name: str, presenter_uid: int) -> bytes:
    return b"".join([
        _tars_string(0, ""),
        _tars_string(1, cdn),
        _tars_string(2, stream_name),
        _tars_int(3, presenter_uid),
        _tars_struct_end(),
    ])


def _uni_attribute(key: str, payload: bytes) -> bytes:
    """UniAttribute：v1 = map<string, bytes>，v3 = map<string, map<string, bytes>>。"""
    if _WUP_STYLE == "v3":
        inner = _tars_head(0, 8) + _tars_int(0, 2) + _tars_string(0, "s") + _tars_int(1, 7) \
            + _tars_head(1, 13) + _tars_int(0, len(payload)) + payload + _tars_int(0, 0)
        outer = _tars_head(0, 8) + _tars_int(0, 2) + _tars_string(0, key) + _tars_int(1, 8) + inner
        return _tars_int(0, 3) + outer
    # v1：version(short) + map<string, vector<byte>>
    return (_tars_int(0, 1)
            + _tars_head(1, 8) + _tars_int(0, 2) + _tars_string(0, key) + _tars_int(1, 13)
            + _tars_int(0, len(payload)) + payload + _tars_int(0, 0))


def _wup_body(attribute: bytes) -> bytes:
    body = b"".join([
        _tars_int(1, 3),               # version = VERSION3
        _tars_int(2, 0),               # packetType
        _tars_int(3, 0),               # messageType
        _tars_int(4, random.randint(1, 2 ** 31 - 1)),  # requestId
        _tars_string(5, "liveui"),     # servantName
        _tars_string(6, "getCdnTokenInfo"),
        _tars_bytes(7, attribute),     # uniAttribute
        _tars_int(8, 3000),            # timeout
        _tars_empty_map(9),            # context
        _tars_empty_map(10),           # status
        _tars_struct_end(),
    ])
    return struct.pack(">i", len(body) + 4) + body


def _read_tars_strings(blob: bytes) -> list[str]:
    """从 TARS 响应里把可读字符串捞出来（不完整实现：够找 antiCode/streamName 就行）。"""
    out: list[str] = []
    index = 0
    while index < len(blob) - 1:
        head = blob[index]
        type_id = head & 0x0F
        index += 1
        if head >> 4 == 0x0F:
            index += 1
        if type_id == 6:  # String1
            if index >= len(blob):
                break
            length = blob[index]
            index += 1
            out.append(blob[index:index + length].decode("utf-8", "replace"))
            index += length
        elif type_id == 7:  # String4
            if index + 4 > len(blob):
                break
            length = struct.unpack(">i", blob[index:index + 4])[0]
            index += 4
            if 0 < length <= 65535:
                out.append(blob[index:index + length].decode("utf-8", "replace"))
            index += max(0, length)
    return out


def wup_token(cdn: str, stream_name: str, presenter_uid: int, proxy: str = "") -> dict[str, str] | None:
    """stream-rec 的 getTrueUrl：向 WUP 要一份服务端 antiCode。失败返回 None。"""
    import requests  # noqa: PLC0415

    payload = _wup_body(_uni_attribute("tReq", _token_req(cdn, stream_name, presenter_uid)))
    headers = {
        "user-agent": HYSDK_UA,
        "referer": HY_BASE,
        "origin": HY_BASE,
        "content-type": "application/octet-stream",
    }
    try:
        resp = requests.post(WUP_URL, data=payload, headers=headers,
                             proxies=_proxies(proxy), timeout=20)
        blob = resp.content or b""
    except Exception:  # noqa: BLE001
        return None
    if not blob or b"wsSecret" not in blob:
        return None
    strings = _read_tars_strings(blob)
    anti = next((s for s in strings if "wsSecret" in s), "")
    name = next((s for s in strings if s.lower().endswith((".flv", ".m3u8"))), "")
    if not anti:
        return None
    return {"anti": anti, "stream": name or stream_name}


def _alive(url: str, cookie: str = "", proxy: str = "", timeout: float = 6.0) -> bool:
    """轻量探活：能读到一点数据就算活（虎牙的签名地址大多一次有效，别读太多）。"""
    import requests  # noqa: PLC0415

    headers = {"user-agent": _ANDROID_UA, "referer": HY_BASE}
    if cookie:
        headers["cookie"] = cookie
    try:
        with requests.get(url, headers=headers, proxies=_proxies(proxy), timeout=timeout,
                          stream=True) as resp:
            if resp.status_code >= 400:
                return False
            for _ in resp.iter_content(1024):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


# --------------------------------------------------------------------------- 对外接口


def resolve(url: str, quality_code: str = "OD", cookie: str = "", proxy: str = "",
            probe: bool = True) -> dict[str, Any]:
    room = room_id(url, proxy)
    data = mp_data(room, cookie, proxy, ttl=0.0)
    info = profile(data)
    live = is_live(data)
    result: dict[str, Any] = {
        "platform": "huya",
        "anchor_name": info["anchor"] or f"虎牙 {room}",
        "title": info["title"],
        "is_live": live,
        "record_url": "",
        "flv_url": "",
        "m3u8_url": "",
        "lines": [],
    }
    if not live:
        return result

    rows = _quality_rows(data)
    picked = _pick_bitrate(rows, quality_code)
    candidates = _candidates(data, quality_code, cookie)
    if not candidates:
        raise ResolveError("虎牙没有返回可用的直播线路（接口可能被风控，稍后再试）")

    # 候选轮转：同一个房间连续取流时，每次换一条线路试，避免老撞在同一个 CDN 上
    index = _PREFERRED.get(room, 0) % len(candidates)
    ordered = candidates[index:] + candidates[:index]

    # 挑一条真的能拉起来：先试前 3 条，全死就走 WUP 要服务端 antiCode（stream-rec 的 getTrueUrl）
    chosen = ordered[0]
    found = not probe  # 中转刷新时不做探活（每次刷新都发 GET 太慢，也容易把签名用掉）
    if probe:
        for candidate in ordered[:3]:
            if _alive(candidate["url"], cookie, proxy):
                chosen = candidate
                found = True
                break
    if not found:
        token = wup_token(chosen["cdn"], chosen["stream_name"], _presenter_uid(data), proxy)
        if token:
            parts = urlsplit(chosen["url"])
            query = dict(parse_qsl(parts.query))
            query.update(dict(parse_qsl(token["anti"])))
            token_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
            if _alive(token_url, cookie, proxy):
                chosen = dict(chosen, url=token_url)
    _PREFERRED[room] = (index + 1) % len(candidates)

    # 记住 antiCode 相关材料，resign() 可以据此重新算一次签名
    _ANTI_CACHE[chosen["url"]] = {
        "cdn": chosen["cdn"],
        "stream_name": chosen["stream_name"],
        "bitrate": chosen["bitrate"],
        "max_bitrate": chosen["max_bitrate"],
        "presenter_uid": _presenter_uid(data),
        "cookie": cookie,
        "proxy": proxy,
    }

    quality_name = str((picked or {}).get("name") or "")
    canonical = _name_to_quality(quality_name) or "原画"
    # 「有没有降级」只看两件事：拿到的码率是不是这房间的最高档，或者档位名换算后
    # 与所选的一类相同（原画/蓝光/…）。最高档永远不会被判成降级——虎牙的「原画」
    # 在接口里通常叫「蓝光20M」这种名字，早先按字符串前缀比会误报。
    max_bits = int(rows[0]["bitrate"]) if rows else 0
    picked_bits = int((picked or {}).get("bitrate") or 0)
    want_canonical = _name_to_quality(str(quality_code)) or ""
    is_top = picked_bits > 0 and picked_bits >= max_bits
    if is_top or (want_canonical and want_canonical == canonical):
        note = ""
    elif want_canonical:
        note = f"这个直播间没有「{quality_code}」这类档位，已按「{quality_name}」录制（实际画质：{canonical}）"
    elif quality_name and quality_name != str(quality_code):
        note = f"没找到「{quality_code}」这一档，已按「{quality_name}」录"
    else:
        note = ""
    result.update({
        "record_url": chosen["url"],
        "flv_url": chosen["url"] if chosen["format"] == "flv" else "",
        "m3u8_url": chosen["url"] if chosen["format"] == "hls" else "",
        "lines": [c["url"] for c in ordered[:4]],
        "quality_actual": canonical,
        "quality_note": note,
        "cdn": chosen["cdn"],
        "format": chosen["format"],
    })
    return result


def qualities(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    room = room_id(url, proxy)
    data = mp_data(room, cookie, proxy, ttl=0.0)
    info = profile(data)
    rows = _quality_rows(data) if data.get("stream") else []
    items: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        name = str(row.get("name") or "")
        label = f"原画（{name}）" if index == 0 and "原画" not in name else name
        items.append(_quality_item(label, label, str(int(row.get("bitrate") or 0)),
                                   default=index == 0))
    note = "虎牙按所选档位就近挑 CDN 线路；「蓝光20M / 2K HDR」这类高档位只有主播推了才会出现"
    if not items:
        note = "该房间当前没有返回可选画质（可能未开播）"
    return {
        "ok": bool(items),
        "platform": "huya",
        "anchor": info["anchor"],
        "title": info["title"],
        "isLive": is_live(data),
        "qualities": items,
        "note": note,
        "error": "" if items else note,
    }


def qualities_from_data(data: dict[str, Any], page_rates: Any = None) -> dict[str, Any]:
    """兼容 resolver 的 `_huya_qualities(data, page_rates)` 旧签名。"""
    info = profile(data)
    rows = _quality_rows(data)
    items = []
    for index, row in enumerate(rows):
        name = str(row.get("name") or "")
        label = f"原画（{name}）" if index == 0 and "原画" not in name else name
        items.append(_quality_item(label, label, str(int(row.get("bitrate") or 0)),
                                   default=index == 0))
    return {"anchor": info["anchor"], "title": info["title"], "isLive": is_live(data),
            "qualities": items}


def next_stream(url: str, quality_code: str = "OD", cookie: str = "", proxy: str = "") -> str | None:
    """取一条新的（重新签名的）线路地址；失败返回 None。"""
    try:
        info = resolve(url, quality_code, cookie, proxy)
    except ResolveError:
        return None
    return info.get("record_url") or None


def drop_cache(url: str) -> None:
    match = re.search(r"huya\.com/([^/?#]+)", url or "")
    if not match:
        return
    room = match.group(1)
    for key in [k for k in list(_MP_CACHE) if k.split("|", 1)[0] in (room, room.lower())]:
        _MP_CACHE.pop(key, None)
    _PREFERRED.pop(room, None)


def resign(url: str, to_flv: bool = False) -> str | None:
    """用原地址里的签名材料重算一次 wsSecret（不重新请求房间接口）。"""
    if not url:
        return None
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    if not query.get("wsTime") or not query.get("fm"):
        return None
    meta = _ANTI_CACHE.get(url) or {}
    stream = str(meta.get("stream_name") or parts.path.rsplit("/", 1)[-1].split(".")[0])
    try:
        uid = int(query.get("u") or 0)
    except ValueError:
        uid = 0
    ratio = query.get("ratio")
    try:
        new_query = _build_query_from(query, uid, stream, int(ratio) if ratio else None)
    except ResolveError:
        return None
    path = parts.path
    if to_flv and path.endswith(".m3u8"):
        path = path[:-5] + ".flv"
    return urlunsplit((parts.scheme or "https", parts.netloc, path, new_query, ""))


def fresh_line(room_url: str, target_url: str, cookie: str = "", proxy: str = "") -> str:
    """HLS 中转每次刷新播放列表时用：重新问房间接口拿一条新地址（同 CDN 同档位优先）。

    这里刻意 probe=False：刷新播放列表很频繁，探活既慢又会把一次性签名用掉。
    """
    try:
        info = resolve(room_url, "原画", cookie, proxy, probe=False)
    except ResolveError:
        return ""
    fresh = info.get("record_url") or ""
    if not fresh:
        return ""
    target = urlsplit(target_url)
    wanted = dict(parse_qsl(target.query)).get("ratio", "")
    for candidate in [fresh] + list(info.get("lines") or []):
        parts = urlsplit(candidate)
        if parts.netloc != target.netloc:
            continue
        if wanted and dict(parse_qsl(parts.query)).get("ratio", "") not in ("", wanted):
            continue
        return candidate
    return fresh


# --------------------------------------------------------------------------- Cookie 校验


def cookie_state(cookie: str, proxy: str = "") -> tuple[bool | None, str]:
    """真实登录态校验：`POST https://udblgn.huya.com/web/cookie/verify`（appId=5002）。"""
    import requests  # noqa: PLC0415

    if not cookie:
        return None, "未设置"
    headers = {
        "user-agent": HYSDK_UA,
        "referer": HY_BASE,
        "content-type": "application/json",
        "cookie": cookie,
    }
    try:
        resp = requests.post(COOKIE_VERIFY_URL, json={"appId": APP_ID}, headers=headers,
                             proxies=_proxies(proxy), timeout=15)
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return None, f"校验接口请求失败：{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "校验接口返回异常"
    code = payload.get("returnCode")
    message = str(payload.get("message") or "")
    data = payload.get("data")
    if code == 0:
        # 实测：udblgn 对「没带有效登录态」的请求也回 returnCode=0、data=null，
        # 所以只有真的带出用户信息时才敢判「登录有效」，否则只能算「没给结论」。
        if isinstance(data, dict) and data:
            nickname = str(data.get("nick") or data.get("nickName") or data.get("nickname") or "")
            return True, f"登录有效{'：' + nickname if nickname else ''}"
        return None, "接口只回了 returnCode=0，没返回用户信息"
    if code in (101, 102, 103):
        return False, f"Cookie 已失效（{message or code}）"
    return None, f"校验接口没给出结论（returnCode={code} {message}）".strip()
