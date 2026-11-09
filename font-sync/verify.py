"""GitHub 账号登录验证（OAuth Device Flow，0.8.11 起）。

用户口径：**上传字体到 GitHub、或复制令牌之前，必须在浏览器里登录「令牌所属」
的那个 GitHub 账号**；把字体下载回本机不需要验证。

为什么用 Device Flow：
- GitHub 官方给桌面 / CLI 应用的方式，**不需要 client_secret**，插件里
  永远不会放 OAuth 密钥（需要一个公开的 Client ID，由用户自己建一个 OAuth App）；
- 用户是在 github.com 的页面上登录并输入一次性验证码，插件碰不到密码；
- 拿到的临时 access_token 只用来 `GET /user` 确认登录名，随即丢弃 ——
  不落盘、不写日志、不进任何状态快照。

验证结果只在**本进程内存里**保留 5 分钟并绑定登录名：
面板点上传 → 先验证 → 触发同步时主进程再检查一次票据，插件菜单直接触发的
同步（没验证过）会被拒绝，不会被绕过。

0.8.12 起（用户报「连不上 GitHub 的登录验证服务：HTTP Error 400」）：
- **不再把 GitHub 的错误体吞掉**。`POST /login/device/code` 返回 4xx 时，
  原来只把 `HTTPError` 的 `str()` 抛给用户看，于是最常见的两种错都被显示成
  「连不上…HTTP Error 400」——其实网络是通的，是 OAuth App 没开 Device Flow。
  现在按错误码/错误体翻成人话，并给出下一步该点哪里。
- 新增 `probe()`：保存 Client ID 前先真打一次 GitHub，直接告出「能用 / 没开
  Device Flow / Client ID 不存在」。
- 新增 `verify_by_token()`：没有 OAuth App（或 GitHub 死活不放行 Device Flow）
  时的降级路径 —— 用保险箱里的令牌打 `GET /user`，登录名与当前账号一致才算过。
  仍然是 GitHub 侧的身份校验，只是少了「一次性验证码」这一层。
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import store

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
VERIFY_TTL = 300  # 秒

# 给用户点的两个官方页面（面板里直接做成链接）
OAUTH_APPS_PAGE = "https://github.com/settings/developers"
DEVICE_FLOW_HELP = ("https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/"
                    "authorizing-oauth-apps")

_lock = threading.Lock()
_state = {
    "device_code": "",
    "user_code": "",
    "verification_uri": "https://github.com/login/device",
    "interval": 5,
    "expires_at": 0.0,
    "verified_login": "",
    "verified_at": 0.0,
    "method": "",          # "" / "device"（浏览器验证码）/ "token"（令牌自检）
}


def client_id():
    return str(store.load_config().get("githubClientId") or "").strip()


def set_client_id(value):
    store.save_config({"githubClientId": str(value or "").strip()})
    invalidate()
    return client_id()


# ── GitHub 请求 ─────────────────────────────────────────────────────────
def _explain_failure(code, body, cid=""):
    """把 GitHub 的 4xx 翻成「下一步该做什么」。

    实测（2026-11-09）：
    - 不存在的 Client ID → 404 `{"error":"Not Found"}`
    - OAuth App 没勾 Enable Device Flow → 400，`error: device_flow_disabled`
    旧代码把这两种都显示成「连不上…HTTP Error 400: Bad Request」，用户完全
    没法判断自己是网络问题还是应用没配好。
    """
    try:
        data = json.loads(body or "{}")
    except Exception:
        data = {}
    err = str(data.get("error") or "")
    desc = str(data.get("error_description") or "")
    if code == 404 or err in ("Not Found", "incorrect_client_credentials"):
        return {
            "kind": "bad_client_id",
            "error": f"这个 Client ID 在 GitHub 上不存在（{cid or '没填'}）。"
                     f"请回 OAuth App 页面重新复制一遍 Client ID —— 注意别把 Client secret 粘进来。",
        }
    if err == "device_flow_disabled":
        return {
            "kind": "device_flow_disabled",
            "error": "这个 OAuth App 没打开 Device Flow，GitHub 直接拒绝了（HTTP 400）。"
                     "打开 GitHub → Developer settings → OAuth Apps → 你的应用 → "
                     "勾选 Enable Device Flow → Update application，然后回来重试。",
        }
    if err == "unsupported_grant_type":
        return {"kind": "unsupported_grant_type",
                "error": f"GitHub 不支持这种授权方式：{desc or err}"}
    return {
        "kind": "http",
        "error": f"GitHub 拒绝了这次请求（HTTP {code}）：{desc or err or (body or '')[:200]}",
    }


def _request_device_code(cid):
    """请求一次设备码。不抛异常：成功给 data，失败给人话。"""
    req = urllib.request.Request(
        DEVICE_CODE_URL,
        data=urllib.parse.urlencode({"client_id": cid, "scope": "read:user"}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "FontManager-font-sync",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        out = _explain_failure(e.code, body, cid)
        out["raw"] = (body or "")[:300]
        return out
    except Exception as e:
        return {"kind": "network",
                "error": f"连不上 GitHub（网络或代理问题）：{e}。"
                         f"如果挂着代理/加速器，确认它能访问 github.com 与 api.github.com。"}
    return {"ok": True, "data": data}


def _post_form(url, fields):
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "FontManager-font-sync",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ── 开始 / 轮询 浏览器验证 ───────────────────────────────────────────────
def start():
    """开始一次登录验证：返回一次性验证码与 GitHub 的验证页地址。"""
    cid = client_id()
    if not cid:
        return {
            "ok": False,
            "needClientId": True,
            "error": "还没配置 GitHub OAuth App 的 Client ID：到「账号与令牌」页填一个即可（公开值，不是密钥）。",
        }
    res = _request_device_code(cid)
    if not res.get("ok"):
        return {"ok": False, "kind": res.get("kind") or "http",
                "error": res.get("error") or "GitHub 没给出原因"}
    r = res["data"]
    code = str(r.get("device_code") or "")
    if not code:
        return {"ok": False,
                "error": str(r.get("error_description") or r.get("error") or "GitHub 没返回设备码")}
    with _lock:
        _state.update(
            device_code=code,
            user_code=str(r.get("user_code") or ""),
            verification_uri=str(r.get("verification_uri") or "https://github.com/login/device"),
            interval=max(5, int(r.get("interval") or 5)),
            expires_at=time.time() + int(r.get("expires_in") or 900),
            verified_login="",
            verified_at=0.0,
            method="",
        )
        return {
            "ok": True,
            "userCode": _state["user_code"],
            "verificationUri": _state["verification_uri"],
            "expiresIn": int(r.get("expires_in") or 900),
            "interval": _state["interval"],
        }


def probe(cid=None):
    """自检一个 Client ID：真打一次 GitHub，告诉用户「能用 / 为什么不能」。

    只请求设备码，不进入验证流程、不改 _state，随便点多少次都不影响正在进行的验证。
    """
    cid = (cid or client_id() or "").strip()
    if not cid:
        return {"ok": False, "needClientId": True,
                "error": "先填 Client ID 再点检测。"}
    res = _request_device_code(cid)
    if not res.get("ok"):
        return {"ok": False, "kind": res.get("kind") or "http",
                "error": res.get("error") or "GitHub 没给出原因",
                "raw": res.get("raw") or ""}
    data = res.get("data") or {}
    return {"ok": True, "message": "Client ID 可用，Device Flow 已开启。",
            "expiresIn": int(data.get("expires_in") or 900)}


def poll():
    """轮询一次：用户还没输码就 pending；输完且账号对得上才算通过。"""
    with _lock:
        code = _state["device_code"]
        expires = _state["expires_at"]
        interval = _state["interval"]
    if not code:
        return {"ok": False, "error": "请先开始验证"}
    if time.time() > expires:
        return {"ok": False, "expired": True, "error": "验证码已过期，请重新开始验证"}
    cid = client_id()
    try:
        r = _post_form(TOKEN_URL, {
            "client_id": cid,
            "device_code": code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
    except urllib.error.HTTPError as e:
        # GitHub 偶尔用 4xx 返回 JSON 错误体，尽量读出来
        try:
            r = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"ok": False, "error": f"GitHub 验证请求失败（HTTP {e.code}）"}
    except Exception as e:
        return {"ok": False, "error": f"GitHub 验证请求失败：{e}"}

    err = str(r.get("error") or "")
    if err in ("authorization_pending", "slow_down"):
        return {"ok": False, "pending": True,
                "interval": interval + (5 if err == "slow_down" else 0)}
    if err:
        if err == "device_flow_disabled":
            return {"ok": False, "kind": "device_flow_disabled",
                    "error": _explain_failure(400, json.dumps(r), cid)["error"]}
        return {"ok": False, "error": str(r.get("error_description") or err)}

    access_token = str(r.get("access_token") or "")
    if not access_token:
        return {"ok": False, "error": "GitHub 没返回登录结果，请重试"}
    del r
    login = ""
    try:
        req = urllib.request.Request(
            USER_URL,
            headers={
                "Authorization": "Bearer " + access_token,
                "Accept": "application/json",
                "User-Agent": "FontManager-font-sync",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            me = json.loads(resp.read().decode("utf-8", "replace"))
        login = str(me.get("login") or "")
    except Exception as e:
        return {"ok": False, "error": f"确认 GitHub 账号失败：{e}"}
    finally:
        # 临时访问令牌用完即弃：绝不落盘、绝不写日志、绝不进状态快照
        access_token = ""

    expected = store.current_account()[0]
    if not login or not expected or login.lower() != expected.lower():
        return {
            "ok": False,
            "error": f"登录的是 {login or '未知账号'}，当前令牌属于 {expected or '未知账号'}；"
                     f"请退出后用 {expected or '对应账号'} 重新登录再试",
        }
    with _lock:
        _state.update(verified_login=expected, verified_at=time.time(),
                      device_code="", method="device")
    return {"ok": True, "login": login, "secondsLeft": VERIFY_TTL, "method": "device"}


# ── 降级路径：用保险箱里的令牌自检 ───────────────────────────────────────
def verify_by_token():
    """没有 OAuth App 时用：拿保险箱里的令牌打 `GET /user`，登录名对得上才放行。

    这仍然是 GitHub 侧的身份校验（令牌属于谁一查就知道），只是少了「刚在浏览器
    输入一次性验证码」这一层现场证明。只在用户明确点「改用令牌校验」时才走这里。
    """
    expected, token = store.current_account()
    if not token:
        return {"ok": False, "error": "还没有保存令牌，先到「账号与令牌」粘贴一个。"}
    req = urllib.request.Request(
        USER_URL,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "FontManager-font-sync"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            me = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "error": "令牌无效或已过期（GitHub 返回 401），重新生成一个再试。"}
        return {"ok": False, "error": f"用令牌确认账号失败（HTTP {e.code}）。"}
    except Exception as e:
        return {"ok": False, "error": f"连不上 GitHub：{e}"}
    login = str((me or {}).get("login") or "")
    if not login:
        return {"ok": False, "error": "GitHub 没返回登录名，令牌可能没有 read:user 权限。"}
    if expected and login.lower() != expected.lower():
        return {"ok": False,
                "error": f"这个令牌属于 {login}，不是当前账号 {expected}；换回对应账号的令牌再试。"}
    with _lock:
        _state.update(verified_login=login, verified_at=time.time(),
                      device_code="", method="token")
    return {"ok": True, "login": login, "method": "token", "secondsLeft": VERIFY_TTL}


def status():
    with _lock:
        left = int(max(0, VERIFY_TTL - (time.time() - _state["verified_at"])))
        verified = bool(_state["verified_at"] and left > 0)
        login = _state["verified_login"] if verified else ""
        method = _state["method"] if verified else ""
    return {"ok": True, "verified": verified, "login": login, "method": method,
            "secondsLeft": left, "hasClientId": bool(client_id())}


def valid(expected_login=None):
    """这次操作有没有经过「对应账号的 GitHub 登录」验证。"""
    with _lock:
        if not _state["verified_at"] or time.time() - _state["verified_at"] > VERIFY_TTL:
            return False
        login = _state["verified_login"]
    if not login:
        return False
    if expected_login and login.lower() != expected_login.lower():
        return False
    return True


def invalidate():
    """换账号、改 Client ID、移除令牌时立即作废验证。"""
    with _lock:
        _state.update(device_code="", verified_login="", verified_at=0.0, method="")
