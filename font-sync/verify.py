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

_lock = threading.Lock()
_state = {
    "device_code": "",
    "user_code": "",
    "verification_uri": "https://github.com/login/device",
    "interval": 5,
    "expires_at": 0.0,
    "verified_login": "",
    "verified_at": 0.0,
}


def client_id():
    return str(store.load_config().get("githubClientId") or "").strip()


def set_client_id(value):
    store.save_config({"githubClientId": str(value or "").strip()})
    invalidate()
    return client_id()


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


def start():
    """开始一次登录验证：返回一次性验证码与 GitHub 的验证页地址。"""
    cid = client_id()
    if not cid:
        return {
            "ok": False,
            "needClientId": True,
            "error": "还没配置 GitHub OAuth App 的 Client ID：到「账号与令牌」页填一个即可（公开值，不是密钥）。",
        }
    try:
        r = _post_form(DEVICE_CODE_URL, {"client_id": cid, "scope": "read:user"})
    except Exception as e:
        return {"ok": False, "error": f"连不上 GitHub 的登录验证服务：{e}"}
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
        )
        return {
            "ok": True,
            "userCode": _state["user_code"],
            "verificationUri": _state["verification_uri"],
            "expiresIn": int(r.get("expires_in") or 900),
            "interval": _state["interval"],
        }


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
        _state.update(verified_login=expected, verified_at=time.time(), device_code="")
    return {"ok": True, "login": login, "secondsLeft": VERIFY_TTL}


def status():
    with _lock:
        left = int(max(0, VERIFY_TTL - (time.time() - _state["verified_at"])))
        verified = bool(_state["verified_at"] and left > 0)
        login = _state["verified_login"] if verified else ""
    return {"ok": True, "verified": verified, "login": login,
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
        _state.update(device_code="", verified_login="", verified_at=0.0)
