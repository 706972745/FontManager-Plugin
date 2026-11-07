"""配置、令牌保险箱、同步状态。

三份文件都在插件目录里，都是「拷走插件目录就带走」的：
- `config.json`   —— 用户设置（当前账号 / 仓库 / 同步规则），明文
- `secret.bin`    —— **多个账号的令牌**，整块 JSON 用 Windows DPAPI 加密，
                     只有当前 Windows 用户能解（换台电脑解不开，等于没带令牌走）
- `state.json`    —— 每族最后同步的 zip 哈希与 asset id、待删队列

DPAPI 走 ctypes 直接调 crypt32，不引入任何第三方库；拿不到就**不落盘**
（令牌只留在内存里，本会话可用），绝不退化成明文。

保险箱内部结构（DPAPI 解开之后）：
    {"version": 1, "current": "<登录名>", "accounts": {"<登录名>": "<token>", ...}}
老版本存的是「一个裸 token 字符串」，读到时自动迁移成 `_legacy` 账号，
等下次测试连接拿到登录名再改名（`promote_legacy`）。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
import uuid

# 插件的三个状态文件就在插件目录里（和 main.py 同级）。
# 注意别写成 dirname(dirname(...))：那会跑到 plugins/ 上一层，等于把配置丢在
# 所有插件的公共目录里 —— 0.1.1 踩过，config.json 直接写到了 plugins\ 下。
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
SECRET_PATH = os.path.join(PLUGIN_DIR, "secret.bin")
STATE_PATH = os.path.join(PLUGIN_DIR, "state.json")

LEGACY_ACCOUNT = "_legacy"

DEFAULT_CONFIG = {
    # 当前使用哪个账号（登录名）；空 = 还没配
    "account": "",
    # 仓库：owner 留空 = 用当前账号本人
    "owner": "",
    "repoName": "",
    # 最近用过的库，方便切换（[{owner, name}]）
    "recentRepos": [],
    # 从 GitHub 恢复到本机时，字体放到哪个目录。空 = 自动（主程序当前字体库）。
    # 新机器字体库是空的，没有这个字段就只能靠猜 —— 猜错等于往人家硬盘里乱放文件。
    "restoreDir": "",
    # 恢复时本机已有同名文件且内容不同，要不要覆盖。默认 False（覆盖不可逆）。
    "overwriteExisting": False,
    "sync": {
        # 只同步这些版权分类之外的；空列表 = 全部。
        # unknown 默认**不同步**：判不清授权的东西不该往云端放（默认从严，用户可自己放开）。
        "excludeCopyright": ["unknown"],
        # 单族 zip 上限（字节）。超了就跳过并报出来，免得一个巨型族把一次同步卡死。
        # 85 MB 是刻意压低的小值：个别中日韩全集族（含十几个字重的）动辄几百 MB，
        # 一次上传失败要重来整族；拆不动就先跳过，让其它族先落地。
        "maxFamilyBytes": 85 * 1024 * 1024,
        # 旧 asset 延迟删除的天数（给「另一台机器用旧版覆盖新版」留反悔窗口）
        "deleteAfterDays": 7,
        # 本地删掉某个字族时，远端那份要不要跟着删（默认删 —— 备份应该和库一致）。
        # 关掉它，远端就变成「只增不减」的归档。
        "deleteRemovedFamilies": True,
        "batchFamilies": 100,
    },
}


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


# ── 配置 ────────────────────────────────────────────────────────────────
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg["sync"] = dict(DEFAULT_CONFIG["sync"])
    cfg["recentRepos"] = []
    data = _read_json(CONFIG_PATH, {})
    for k, v in data.items():
        if k == "sync" and isinstance(v, dict):
            cfg["sync"].update(v)
        elif k == "recentRepos" and isinstance(v, list):
            cfg["recentRepos"] = [r for r in v if isinstance(r, dict) and r.get("name")]
        else:
            cfg[k] = v
    return cfg


def save_config(patch):
    cfg = load_config()
    for k, v in patch.items():
        if k == "sync" and isinstance(v, dict):
            cfg["sync"].update(v)
        else:
            cfg[k] = v
    _atomic_write_json(CONFIG_PATH, cfg)
    return cfg


def remember_repo(owner, name):
    """把用过的库记进 recentRepos（最多 8 个，最近的在前面）。"""
    if not name:
        return
    cfg = load_config()
    items = [r for r in cfg.get("recentRepos") or []
             if not (r.get("owner") == owner and r.get("name") == name)]
    items.insert(0, {"owner": owner or "", "name": name})
    save_config({"recentRepos": items[:8]})


def configured():
    """能不能直接开工：有账号 + 有令牌 + 填了仓库名。"""
    login, token = current_account()
    cfg = load_config()
    return bool(login and token and (cfg.get("repoName") or "").strip())


# ── DPAPI ───────────────────────────────────────────────────────────────
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob):
    size = blob.cbData
    return ctypes.string_at(blob.pbData, size)


def _dpapi(func_name, data):
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = getattr(crypt32, func_name)
    fn.argtypes = [ctypes.POINTER(_Blob), wt.LPCWSTR, ctypes.POINTER(_Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wt.BOOL
    src = _to_blob(data)
    dst = _Blob()
    ok = fn(ctypes.byref(src), "FontManager font-sync", None, None, None, 0x1,
            ctypes.byref(dst))
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{func_name} 失败")
    try:
        return _from_blob(dst)
    finally:
        kernel32.LocalFree(dst.pbData)


# ── 令牌保险箱（多账号） ─────────────────────────────────────────────────
_VAULT_CACHE = {"vault": None, "persisted": True}


def _empty_vault():
    return {"version": 1, "current": "", "accounts": {}}


def _normalize(vault):
    out = _empty_vault()
    if isinstance(vault, dict):
        accounts = vault.get("accounts")
        if isinstance(accounts, dict):
            out["accounts"] = {str(k): str(v) for k, v in accounts.items() if v}
        out["current"] = str(vault.get("current") or "")
    if out["current"] not in out["accounts"]:
        out["current"] = next(iter(out["accounts"]), "")
    return out


def load_vault():
    """解开保险箱。老格式（裸 token 字符串）自动迁移成 `_legacy` 账号。"""
    if _VAULT_CACHE["vault"] is not None:
        return dict(_VAULT_CACHE["vault"])
    vault = _empty_vault()
    try:
        with open(SECRET_PATH, "rb") as fh:
            raw = fh.read()
        if raw.startswith(b"DPAPI1"):
            plain = _dpapi("CryptUnprotectData", raw[6:]).decode("utf-8", "replace")
            try:
                vault = _normalize(json.loads(plain))
            except Exception:
                # 老版本存的就是一串 token
                token = plain.strip()
                if token:
                    vault = {"version": 1, "current": LEGACY_ACCOUNT,
                             "accounts": {LEGACY_ACCOUNT: token}}
    except Exception:
        vault = _empty_vault()
    _VAULT_CACHE.update(vault=vault, persisted=True)
    return dict(vault)


def save_vault(vault):
    """整块加密落盘。返回 False = DPAPI 不可用，只在内存里有效。"""
    vault = _normalize(vault)
    _VAULT_CACHE["vault"] = vault
    if not vault["accounts"]:
        _VAULT_CACHE["persisted"] = True
        try:
            os.remove(SECRET_PATH)
        except OSError:
            pass
        return True
    try:
        sealed = _dpapi("CryptProtectData",
                        json.dumps(vault, ensure_ascii=False).encode("utf-8"))
        with open(SECRET_PATH, "wb") as fh:
            fh.write(b"DPAPI1" + sealed)
        _VAULT_CACHE["persisted"] = True
        return True
    except Exception:
        _VAULT_CACHE["persisted"] = False
        return False


def accounts():
    """[(登录名, 是否当前)]，按登录名排序，_legacy 排最后。"""
    vault = load_vault()
    names = sorted((n for n in vault["accounts"] if n != LEGACY_ACCOUNT), key=str.lower)
    if LEGACY_ACCOUNT in vault["accounts"]:
        names.append(LEGACY_ACCOUNT)
    return [(n, n == vault["current"]) for n in names]


def current_account():
    """(登录名, 令牌)。没配返回 ("", "")。"""
    vault = load_vault()
    login = vault["current"]
    return login, vault["accounts"].get(login, "")


def token_for(login=None):
    vault = load_vault()
    login = login or vault["current"]
    return vault["accounts"].get(login, "")


def upsert_account(login, token, make_current=True):
    vault = load_vault()
    login = (login or "").strip() or LEGACY_ACCOUNT
    vault["accounts"][login] = token
    if LEGACY_ACCOUNT in vault["accounts"] and login != LEGACY_ACCOUNT:
        # 老的裸 token 拿到真名之后就不该再留一份
        vault["accounts"].pop(LEGACY_ACCOUNT, None)
    if make_current or vault["current"] not in vault["accounts"]:
        vault["current"] = login
    return save_vault(vault)


def set_current(login):
    vault = load_vault()
    if login not in vault["accounts"]:
        return False
    vault["current"] = login
    return save_vault(vault)


def remove_account(login):
    vault = load_vault()
    vault["accounts"].pop(login, None)
    if vault["current"] == login:
        vault["current"] = next(iter(vault["accounts"]), "")
    return save_vault(vault)


def vault_persisted():
    return bool(_VAULT_CACHE.get("persisted", True))


def _reset_cache():
    """测试与「换插件目录」时用：把内存里的保险箱丢掉，强制下次重新解密。"""
    _VAULT_CACHE.update(vault=None, persisted=True)


# 兼容老接口（只有一个账号时够用）
def save_token(token):
    login = load_vault()["current"] or LEGACY_ACCOUNT
    return upsert_account(login, token)


def load_token():
    return current_account()[1]


def mask(token):
    if not token:
        return "(未设置)"
    return token[:7] + "…" + token[-4:] if len(token) > 14 else "***"


# ── 状态 ────────────────────────────────────────────────────────────────
def load_state():
    st = _read_json(STATE_PATH, {})
    st.setdefault("deviceId", uuid.uuid4().hex[:12])
    st.setdefault("repo", "")
    st.setdefault("families", {})
    st.setdefault("pendingDeletes", [])
    st.setdefault("lastSyncAt", 0)
    return st


def save_state(st):
    _atomic_write_json(STATE_PATH, st)


def state_dir():
    return PLUGIN_DIR


def now_ms():
    return int(time.time() * 1000)


def b64(data):
    return base64.b64encode(data).decode("ascii")
