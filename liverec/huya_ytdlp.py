"""虎牙取流兜底：用 yt-dlp 的 HuyaLive 解析器拿地址。

为什么需要它：虎牙的 wsSecret 是一次性签名，我们自己拼接口/重签在部分网络下会
被 CDN 直接 403；yt-dlp 的虎牙解析器维护得很勤（它自己算 anti-code、按 CDN 排序），
拿到的 flv 地址每次都是新签的，单条只能拉几百 KB 到一两 MB，正好配上「分片续录」：
每段换一条新地址，画面就接上了。

yt-dlp 是可选依赖（打包时会内嵌），没装就返回空列表，调用方自动退回原逻辑。
"""
from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_TTL = 2.0


def available() -> bool:
    try:
        import yt_dlp  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001
        return False
    return True


def _extract_blocking(url: str) -> list[dict[str, Any]]:
    import time  # noqa: PLC0415

    import yt_dlp  # noqa: PLC0415

    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "noplaylist": True,
        # 虎牙有 HLS 也会给 flv，这里不强绑；哪个能拉由录制端逐条试
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    if not formats:
        return []

    # 按「清晰度优先、码率其次」排；同一清晰度下按 CDN 顺序保留（hs/al/tx 都试）
    formats.sort(key=lambda f: (-(f.get("height") or 0), -int(f.get("tbr") or 0)))

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in formats:
        stream_url = str(item.get("url") or "")
        if not stream_url or stream_url in seen:
            continue
        seen.add(stream_url)
        rows.append({
            "url": stream_url,
            "height": int(item.get("height") or 0),
            "tbr": int(item.get("tbr") or 0),
            "protocol": str(item.get("protocol") or ""),
            "format_id": str(item.get("format_id") or ""),
        })
    _CACHE[url] = (time.time(), rows)
    return rows


def formats(url: str, ttl: float = _TTL) -> list[dict[str, Any]]:
    """返回 yt-dlp 解析出的地址列表（带极短缓存，避免一次续录里重复解析）。"""
    import time  # noqa: PLC0415

    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return list(hit[1])
    with _LOCK:
        hit = _CACHE.get(url)
        if hit and time.time() - hit[0] < ttl:
            return list(hit[1])
        try:
            return _extract_blocking(url)
        except Exception:  # noqa: BLE001
            return []


def urls(url: str, limit: int = 6) -> list[str]:
    return [row["url"] for row in formats(url) if row.get("url")][:limit]


def clear(url: str = "") -> None:
    if url:
        _CACHE.pop(url, None)
    else:
        _CACHE.clear()
