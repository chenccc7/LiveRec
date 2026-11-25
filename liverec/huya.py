"""虎牙直播：解析 + 录制策略（独立模块，与其它三个平台彻底分离）。

虎牙和其它平台不一样的地方：
* 地址是**一次性签名**，几十秒后同一地址重签也会 403；
* 同一房间有十几条 CDN 线路，多数 403，稳定的是 hs.hls / tx.flv；
* 很多房间下发的 HLS 只有 3 个分片、不滚动，读完就 EOF。

所以这里不追求「一条长连接录到底」，而是：
  1. `resolve()` 用 yt-dlp 的 HuyaLive 解析器拿最新签名地址（它自己算 anti-code）；
  2. 录制端每 `CHUNK_SECONDS` 秒换一条新地址续录（`next_stream()`），每段单独成文件；
  3. 停止录制时由 `liverec/concat.py` 把这些分段拼成一个文件。

对外的入口：`resolve()` / `qualities()` / `next_stream()` / `drop_cache()`。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from . import paths
from .huya_ytdlp import urls as _ytdlp_urls
from .resolver import (  # noqa: E402 - 复用通用小工具，resolver 不会在导入期反向导入本模块
    _ANDROID_UA,
    _QUALITY_BITRATE,
    ResolveError,
    _name_to_quality,
    _proxies,
    _quality_item,
)

# 每段录制时长：虎牙签名大约 20~30 秒就过期，取 28 秒能拿到最多数据又不至于中途 403。
CHUNK_SECONDS = 28.0


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


def drop_cache(url: str) -> None:
    """清掉某个直播间的解析缓存（虎牙的地址几十秒就过期，续录前必须拿新的）。"""
    global _huya_cache  # noqa: PLW0603
    room = ""
    try:
        match = re.search(r"huya\.com/([^/?#]+)", url or "")
        if match:
            room = match.group(1)
    except Exception:  # noqa: BLE001
        room = ""
    if not room:
        return
    _huya_cache.pop(room, None)
    # 线路表也要一起丢：里面存的是带 wsSecret 的旧地址，重签也只能续一小会儿。
    for key in [k for k in _huya_mp_cache if k.split("|", 1)[0] == room]:
        _huya_mp_cache.pop(key, None)
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
        # yt-dlp 的 HuyaLive 解析器自己算 anti-code，给出的地址通常比我们拼的更「新鲜」，
        # 而且带了多档清晰度。把它排到最前面，拉不动时录制端会自动回落到下面的自有线路。
        try:
            from . import huya_ytdlp  # noqa: PLC0415

            yt_lines = huya_ytdlp.urls(url, limit=6)
        except Exception:  # noqa: BLE001
            yt_lines = []
        if yt_lines:
            candidates = yt_lines + candidates
        if not candidates:
            raise ResolveError("虎牙未返回可用直播流（可能是付费/加密频道，或该房间已切到未支持的线路）")
        # 注意：虎牙的签名地址基本是「一次性」的——用 GET 探一次，ffmpeg 再拉就是 403。
        # 所以这里绝不去预读播放列表/探活，只按 CDN 稳定性排序，交给录制端逐个试，
        # 由 HLS 中转在每次刷新时重新签名（见 liverec/hlsproxy.py）。
        pref = _huya_preferred.get(room, 0) % len(candidates)
        order = candidates[pref:] + candidates[:pref]
        result.update({"quality": quality_code, "flv_url": order[0], "record_url": order[0],
                       "lines": order})
        _huya_preferred[room] = (pref + 1) % len(candidates)

    _huya_cache[room] = (now, dict(result))
    return result


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


# ---------------- 对外入口 ----------------


def resolve(url: str, quality_code: str = "OD", cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """返回 {is_live, anchor_name, title, record_url, flv_url, quality}；不直播时只有前三个。"""
    return _resolve_huya(url, quality_code, cookie, proxy)


def qualities(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """房间信息 + 真实可选档位（页面 vBitRateInfo 合并接口 rateArray）。"""
    room = _huya_room_id(url, proxy)
    data = _huya_mp_data(room, cookie, proxy)
    page_rates: list[tuple[str, int, int]] = []
    try:
        page_rates = _huya_mobile_profile(room, cookie, proxy).get("rates") or []
    except Exception:  # noqa: BLE001 - 页面拿不到就只用接口里的档位
        page_rates = []
    return {
        "anchor": str((data.get("profileInfo") or {}).get("nick") or ""),
        "title": str((data.get("liveData") or {}).get("roomName") or ""),
        "isLive": str(data.get("liveStatus") or "").upper() == "ON",
        "qualities": _huya_qualities(data, page_rates),
    }


def qualities_from_data(data: dict[str, Any],
                        page_rates: list[tuple[str, int, int]] | None = None) -> list[dict[str, Any]]:
    return _huya_qualities(data, page_rates)


def next_stream(url: str, quality_code: str = "OD", cookie: str = "", proxy: str = "") -> str:
    """给分片续录用：丢掉旧缓存，重新解析出一条**全新签名**的地址。"""
    drop_cache(url)
    info = resolve(url, quality_code, cookie, proxy)
    return str(info.get("record_url") or "")
