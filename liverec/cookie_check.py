"""Cookie 有效性检测。

各平台能拿到「真实登录态」的接口有限，这里按可信度分三档：

* ``login``   —— 平台有登录态接口，且接口确认已登录（附带昵称）
* ``likely``  —— 平台没有登录态接口，但 Cookie 里带着登录令牌（能解出 uid / 过期时间）
* ``expired`` —— 接口明确回答「未登录/已过期」，或令牌的过期时间已到
* ``format``  —— 只能做必要字段 + 连通性校验

返回结构与前端约定：

.. code-block:: python

    {
      "platform": "bilibili",
      "state": "login" | "likely" | "expired" | "format" | "empty" | "network",
      "ok": True | False | None,      # None = 无法确认（仅格式/令牌层面通过）
      "message": "给人看的一句话",
      "detail": "补充说明，可能为空",
      "nickname": "登录账号昵称，可能为空",
    }
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import time
from typing import Any

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LABELS = {"douyin": "抖音", "bilibili": "B站", "huya": "虎牙", "douyu": "斗鱼"}

# 进程内缓存「已确认登录」的结果：抖音会轮换 sessionid，同一份 Cookie 连点几次检测会被判未登录，
# 缓存下来避免绿→蓝来回跳。
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 1800.0


def _cache_key(platform: str, cookie_header: str) -> str:
    return platform + ":" + hashlib.sha1(cookie_header.encode("utf-8")).hexdigest()

# 每个平台必须出现的 cookie 字段（同一个子列表里是「任选其一」）
REQUIRED: dict[str, list[list[str]]] = {
    "douyin": [["sessionid", "sessionid_ss", "sid_tt"]],
    "bilibili": [["SESSDATA"]],
    # 斗鱼登录后是 acf_jwt_token（JWT，能解出 uid/exp）+ dy_auth/dy_did 这一套
    "douyu": [["acf_jwt_token", "acf_dmjwt_token", "acf_auth", "dy_auth"]],
    # 虎牙登录后的 cookie 是 udb_* 那一套：udb_uid + (udb_biztoken 或 udb_cred)
    "huya": [["udb_uid", "yyuid"], ["udb_biztoken", "udb_cred", "udb_auth", "yy_auth"]],
}


def parse_cookie(raw: str) -> dict[str, str]:
    """把浏览器里复制的 cookie 串解析成 dict（容忍 ``Cookie:`` 前缀和换行）。"""
    text = (raw or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    text = text.replace("\r", ";").replace("\n", ";")
    out: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key:
            out[key] = value
    return out


def missing_fields(platform: str, parsed: dict[str, str]) -> list[str]:
    """返回缺失的必备字段（每组缺一个就记一条）。"""
    gaps: list[str] = []
    for group in REQUIRED.get(platform, []):
        if not any(parsed.get(k) for k in group):
            gaps.append(" 或 ".join(group))
    return gaps


def _cookie_header(parsed: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in parsed.items())


def _jwt_payload(token: str) -> dict[str, Any]:
    """解 JWT 的 payload（斗鱼的 acf_jwt_token 是 md5 签名的 JWT，能读 uid / exp）。"""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    seg = parts[1].replace("-", "+").replace("_", "/")
    seg += "=" * (-len(seg) % 4)
    try:
        return json.loads(base64.b64decode(seg))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


async def _check_douyu(cli: httpx.AsyncClient, parsed: dict[str, str]) -> dict[str, Any]:
    """斗鱼：没有登录态接口，但登录 Cookie 里的 JWT 能解出 uid 和过期时间。"""
    token = (parsed.get("acf_jwt_token") or parsed.get("acf_dmjwt_token") or "").strip()
    info = _jwt_payload(token)
    uid = info.get("uid") or parsed.get("acf_uid") or ""
    exp = info.get("exp")

    try:
        r = await cli.get("https://www.douyu.com/", headers={"Referer": "https://www.douyu.com/"})
        reachable = r.status_code < 400
    except Exception as exc:  # noqa: BLE001
        return _result("douyu", "network", None, f"连不上斗鱼：{type(exc).__name__}",
                       "检查网络或代理设置后再试。")
    if not reachable:
        return _result("douyu", "network", None, "斗鱼首页返回异常状态码", "稍后再试。")

    if info and exp:
        when = _fmt_ts(exp)
        left = float(exp) - time.time()
        if left <= 0:
            return _result("douyu", "expired", False,
                           f"登录令牌已过期（有效至 {when}）",
                           "重新登录 douyu.com 后把 Cookie 整段复制过来。")
        days = max(1, int(left // 86400))
        return _result("douyu", "login", True,
                       f"登录有效：uid {uid}（令牌有效至 {when}，约 {days} 天）",
                       "斗鱼没有公开的登录态查询接口，这里校验的是登录令牌本身："
                       "能解出 uid 且未过期。若录制日志出现 403/鉴权失败，再重新登录一次。")

    if token and not info:
        return _result("douyu", "format", None,
                       "Cookie 里有令牌但解析不出来（可能被截断）",
                       "复制 Cookie 时漏了字符，重新整段复制一次。")

    looks_logged = bool(re.search(r"(退出登录|个人中心|消息中心)", r.text or ""))
    return _result("douyu", "format", None,
                   "Cookie 字段齐全，站点可访问",
                   "没找到可解析的登录令牌，无法确认是否过期；"
                   "录制时会用这份 Cookie 直接取流，日志出现 403/鉴权失败通常就是过期了。"
                   + ("（首页显示已登录）" if looks_logged else ""))


def _result(platform: str, state: str, ok: bool | None, message: str,
            detail: str = "", nickname: str = "") -> dict[str, Any]:
    return {
        "platform": platform,
        "label": LABELS.get(platform, platform),
        "state": state,
        "ok": ok,
        "message": message,
        "detail": detail,
        "nickname": nickname,
    }


async def _check_bilibili(cli: httpx.AsyncClient, parsed: dict[str, str]) -> dict[str, Any]:
    r = await cli.get("https://api.bilibili.com/x/web-interface/nav",
                      headers={"Referer": "https://www.bilibili.com/"})
    if r.status_code != 200:
        return _result("bilibili", "network", None, f"接口返回 HTTP {r.status_code}，稍后再试")
    try:
        data = r.json().get("data") or {}
    except ValueError:
        return _result("bilibili", "network", None, "接口返回的不是 JSON，稍后再试")
    if data.get("isLogin"):
        nick = str(data.get("uname") or "").strip()
        vip = "大会员" if (data.get("vipStatus") or data.get("vip_status")) else "普通账号"
        return _result("bilibili", "login", True,
                       f"登录有效：{nick or '（取不到昵称）'}（{vip}）",
                       "填了登录 Cookie 后，「原画」不再被降级到超清。", nick)
    return _result("bilibili", "expired", False,
                   "Cookie 已失效（未登录）",
                   "请在浏览器重新登录 bilibili.com 后复制 Cookie 再粘贴一次。")


async def _check_douyin(cli: httpx.AsyncClient, parsed: dict[str, str]) -> dict[str, Any]:
    """抖音：先看直播侧的 me 接口（要 ttwid，返回 uid/nickname），再兜底 web 的 profile/self。"""
    # 1) 直播站 me 接口：能直接给出登录的 uid 与昵称
    try:
        live = await cli.get("https://live.douyin.com/webcast/user/me/",
                             params={"aid": "6383", "device_platform": "web"},
                             headers={"Referer": "https://live.douyin.com/"})
        data = live.json().get("data") if live.status_code == 200 else None
        if isinstance(data, dict) and data.get("id"):
            nick = str(data.get("nickname") or "").strip()
            uid = str(data.get("id") or data.get("short_id") or "")
            return _result("douyin", "login", True,
                           f"登录有效：{nick or '（无昵称）'}（uid {uid}）",
                           "这份 Cookie 带着 ttwid 与 sessionid，直播取流会用它降低风控概率；"
                           "原画清晰度也需要登录态。", nick)
    except Exception:  # noqa: BLE001 - 换下一个接口
        pass

    # 2) web 侧 profile/self
    try:
        r = await cli.get("https://www.douyin.com/aweme/v1/web/user/profile/self/",
                          params={"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web"},
                          headers={"Referer": "https://www.douyin.com/"})
    except Exception as exc:  # noqa: BLE001
        return _result("douyin", "network", None, f"连不上抖音：{type(exc).__name__}",
                       "检查网络或代理设置后再试。")
    if r.status_code != 200:
        return _result("douyin", "network", None, f"接口返回 HTTP {r.status_code}，稍后再试")
    try:
        payload = r.json()
    except ValueError:
        return _result("douyin", "network", None, "接口返回的不是 JSON，稍后再试")

    user = payload.get("user") or {}
    if payload.get("status_code") == 0 and user:
        nick = str(user.get("nickname") or "").strip()
        return _result("douyin", "login", True,
                       f"登录有效：{nick or '（取不到昵称）'}",
                       "登录态 Cookie 能显著降低抖音取流被风控的概率。", nick)

    code = payload.get("status_code")
    msg = payload.get("status_msg") or payload.get("message") or ""
    has_session = any(parsed.get(k) for k in ("sessionid", "sessionid_ss", "sid_tt"))
    has_ttwid = bool(parsed.get("ttwid"))
    said_logout = code in (8, 20003) or "未登录" in msg or "login" in msg.lower()

    # 抖音会按设备指纹校验并轮换 sessionid：同一份 Cookie 连发几次请求，后几次常被判「未登录」。
    # 所以「接口说未登录」不等于 Cookie 废了——字段齐全时降级成「登录字段齐全」，避免误报红色。
    if has_session and has_ttwid:
        return _result("douyin", "likely", None,
                       "Cookie 字段齐全（sessionid + ttwid），接口这次没确认登录态",
                       "抖音按设备指纹校验并会轮换 sessionid，同一份 Cookie 连续请求多次后常被判未登录；"
                       "刚在浏览器里还能用时通常过一会儿就恢复。"
                       "录制日志出现 403/推荐位空再重新复制一次 Cookie。")
    if said_logout:
        return _result("douyin", "expired", False,
                       "Cookie 已失效（缺少登录字段）",
                       "请在浏览器重新登录 douyin.com 后复制 Cookie，注意 sessionid 与 ttwid 都要有。")
    return _result("douyin", "format", None,
                   f"Cookie 格式正确，但接口没给出明确结论（status_code={code}）",
                   "这种情况通常是接口改版，录制时仍会带着这份 Cookie 去取流。")


async def _check_reach(cli: httpx.AsyncClient, platform: str, parsed: dict[str, str],
                      proxy: str = "") -> dict[str, Any]:
    """斗鱼/虎牙没有公开的登录态接口：做必备字段 + 站点连通性校验。"""
    home = {"douyu": "https://www.douyu.com/", "huya": "https://www.huya.com/"}[platform]
    label = LABELS[platform]
    gaps = missing_fields(platform, parsed)
    if gaps:
        return _result(platform, "format", False,
                       f"缺少必要字段：{'、'.join(gaps)}",
                       f"在浏览器登录 {home} 后，把 Cookie 整段复制过来。")

    # 虎牙登录后带的是 udb_* 那一套：udb_uid + udb_biztoken + udb_cred，udb_status=10 表示已登录
    uid = (parsed.get("udb_uid") or parsed.get("yyuid") or "").strip()
    token = (parsed.get("udb_biztoken") or parsed.get("udb_cred") or "").strip()
    logged_shape = bool(uid.isdigit() and len(token) >= 20)

    try:
        r = await cli.get(home, headers={"Referer": home})
    except Exception as exc:  # noqa: BLE001 - 网络错误直接回给用户
        return _result(platform, "network", None, f"连不上{label}：{type(exc).__name__}",
                       "检查网络或代理设置后再试。")
    if r.status_code >= 400:
        return _result(platform, "network", None, f"{label}首页返回 HTTP {r.status_code}",
                       "可能是网络或风控拦截，稍后再试。")

    if platform == "huya":
        # 虎牙有真正的登录态接口：udblgn 的 cookie/verify（appId=5002）
        from liverec import huya as _huya  # noqa: PLC0415

        raw_cookie = "; ".join(f"{k}={v}" for k, v in parsed.items())
        ok, message = await asyncio.to_thread(_huya.cookie_state, raw_cookie, proxy)
        if ok is True:
            return _result(platform, "login", True, f"登录有效：{message}",
                           "登录态 Cookie 能让虎牙取流更稳（匿名时常被 CDN 限流）。")
        if ok is False:
            return _result(platform, "expired", False, f"Cookie 已失效：{message}",
                           "在浏览器重新登录 huya.com，再把 Cookie 整段复制过来。")
        status = (parsed.get("udb_status") or "").strip()
        hint = "（udb_status=10，登录态标记）" if status == "10" else ""
        return _result(platform, "likely", None,
                       f"检测到登录态字段：uid {uid}{hint}",
                       f"udblgn 没给出明确结论（{message or '无返回码'}）；"
                       "结构上像是已登录，但如果录制日志出现 403/鉴权失败，重新登录一次再复制 Cookie 即可。")

    looks_logged = bool(re.search(r"(用户中心|退出登录|个人中心)", r.text or ""))
    return _result(platform, "format", None,
                   "Cookie 字段齐全，站点可访问",
                   "该平台没有公开的登录态查询接口，无法在这里确认是否过期；"
                   "录制时会用这份 Cookie 直接取流，若日志出现 403/鉴权失败，通常就是 Cookie 过期了。"
                   + ("（首页显示已登录）" if looks_logged else ""))


async def check_cookie(platform: str, cookie: str, proxy: str = "") -> dict[str, Any]:
    """检测某个平台的 Cookie。``cookie`` 为空时调用方应先从配置里取已保存的值。"""
    platform = (platform or "").strip().lower()
    if platform not in LABELS:
        return _result(platform or "?", "format", False, f"不认识的平台：{platform}")

    raw = (cookie or "").strip()
    if not raw:
        return _result(platform, "empty", False, "未设置 Cookie",
                       "留空时按未登录状态运行：B站弹幕仍可用，但拿不到原画。")

    parsed = parse_cookie(raw)
    if not parsed:
        return _result(platform, "format", False, "Cookie 格式不对（没有解析出任何 key=value）",
                       "请把浏览器请求头里的 Cookie 整段复制过来，例如 SESSDATA=xxx; bili_jct=yyy;")

    timeout = httpx.Timeout(15.0, connect=10.0)
    headers = {"User-Agent": UA, "Cookie": _cookie_header(parsed),
               "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-CN,zh;q=0.9"}
    key = _cache_key(platform, _cookie_header(parsed))
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        hit = dict(cached[1])
        hit["detail"] = (hit.get("detail") or "") + "（本进程内先前已确认登录，这里不再重复请求接口）"
        return hit

    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout,
                                     follow_redirects=True, proxy=proxy or None) as cli:
            if platform == "bilibili":
                result = await _check_bilibili(cli, parsed)
            elif platform == "douyin":
                result = await _check_douyin(cli, parsed)
            elif platform == "douyu":
                result = await _check_douyu(cli, parsed)
            else:
                result = await _check_reach(cli, platform, parsed, proxy)
    except Exception as exc:  # noqa: BLE001 - 网络类错误统一兜底
        return _result(platform, "network", None, f"检测失败：{type(exc).__name__}: {exc}")

    if result.get("state") == "login":
        _CACHE[key] = (time.time(), result)
    return result
