"""配置、令牌保险箱、同步状态。

**令牌绝对不放在插件目录里（0.8.11 起）**：插件 ZIP 会被分发、插件目录会被
复制 / 备份 / 提交，任何形式的令牌（哪怕加密过）都不该跟着插件走。用户明确
要求「一定不能储存在插件中」，所以保险箱搬到操作系统自己的保管位置：

1. **Windows 凭据管理器**（Credential Manager，`CredWriteW/CredReadW`）——
   由 Windows 用当前用户账户的密钥加密保管，插件目录里连一个令牌文件都没有，
   只有一个凭据名 `FontManager/font-sync/vault`；
2. **%APPDATA%\\FontManager\\font-sync\\vault.bin** —— 凭据管理器不可用时回退，
   DPAPI + 应用专用熵加密，仍然在插件目录之外；
3. 两者都不可用 → **只在内存里有效**，本会话用完即弃，绝不写明文。

`config.json` / `state.json` 仍在插件目录，但它们不含令牌（只有账号名、仓库名、
同步账本）。旧版本把 `secret.bin` 写在插件目录，首次读取时自动迁移到新存储，
成功后立即删除旧文件。

保险箱内部结构：
    {"version": 1, "current": "<登录名>", "accounts": {"<登录名>": "<token>", ...}}
老版本存的是「一个裸 token 字符串」，读到时自动迁移成 `_legacy` 账号。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
import uuid

# 插件的两个非敏感状态文件（和 main.py 同级）。
# 注意别写成 dirname(dirname(...))：那会跑到 plugins/ 上一层，等于把配置丢在
# 所有插件的公共目录里 —— 0.1.1 踩过。
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
STATE_PATH = os.path.join(PLUGIN_DIR, "state.json")
# 只为「读旧数据 → 迁移 → 删除」而保留；新版本绝不再往这里写令牌。
SECRET_PATH = os.path.join(PLUGIN_DIR, "secret.bin")
_DEFAULT_SECRET_PATH = SECRET_PATH

_APP_ROOT = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "FontManager", "font-sync"
)
VAULT_PATH = os.path.join(_APP_ROOT, "vault.bin")
CRED_TARGET = "FontManager/font-sync/vault"
# DPAPI 的额外熵：即使同一 Windows 用户下的别的进程拿到密文，也不知道这层盐。
# 凭据管理器那条路根本不落文件，用不上它。
DPAPI_ENTROPY = b"FontManager.font-sync.v2.vault\x00"

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
    "restoreDir": "",
    # 恢复时本机已有同名文件且内容不同，要不要覆盖。默认 False（覆盖不可逆）。
    "overwriteExisting": False,
    # GitHub 登录验证用的 OAuth App Client ID（公开值，不是密钥）。
    # Device Flow 不需要 client_secret，所以插件里绝不会存任何 OAuth 密钥。
    "githubClientId": "",
    "sync": {
        # 只同步这些版权分类之外的；空列表 = 全部。
        "excludeCopyright": ["unknown"],
        # 单族 zip 上限（字节）
        "maxFamilyBytes": 85 * 1024 * 1024,
        # 旧 asset 延迟删除的天数
        "deleteAfterDays": 7,
        # 本地删掉某个字族时，远端那份要不要跟着删
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


# ── Windows 凭据管理器 / DPAPI ───────────────────────────────────────────
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD),
        ("Type", wt.DWORD),
        ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR),
        ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", wt.DWORD),
        ("AttributeCount", wt.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wt.LPWSTR),
        ("UserName", wt.LPWSTR),
    ]


def _to_blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob):
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi(func_name, data, entropy=None):
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = getattr(crypt32, func_name)
    fn.argtypes = [ctypes.POINTER(_Blob), wt.LPCWSTR, ctypes.POINTER(_Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wt.BOOL
    src = _to_blob(data)
    ent = _to_blob(entropy) if entropy else None
    dst = _Blob()
    ok = fn(ctypes.byref(src), "FontManager font-sync",
            ctypes.byref(ent) if ent else None, None, None, 0x1, ctypes.byref(dst))
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{func_name} 失败")
    try:
        return _from_blob(dst)
    finally:
        kernel32.LocalFree(dst.pbData)


def _cred_write(name, data):
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    blob = ctypes.create_string_buffer(data, len(data))
    cred = _CREDENTIAL()
    cred.Type = 1                     # CRED_TYPE_GENERIC
    cred.TargetName = name
    cred.Comment = "FontManager font-sync 令牌保险箱（由 Windows 凭据管理器保管）"
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_char))
    cred.Persist = 2                  # CRED_PERSIST_LOCAL_MACHINE（绑定当前用户配置）
    cred.UserName = "FontManager"
    adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wt.DWORD]
    adv.CredWriteW.restype = wt.BOOL
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "CredWriteW 失败")


def _cred_read(name):
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    ptr = ctypes.POINTER(_CREDENTIAL)()
    adv.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                              ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    adv.CredReadW.restype = wt.BOOL
    if not adv.CredReadW(name, 1, 0, ctypes.byref(ptr)):
        raise OSError(ctypes.get_last_error(), "CredReadW 失败")
    try:
        c = ptr.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
    finally:
        adv.CredFree.argtypes = [ctypes.c_void_p]
        adv.CredFree(ctypes.cast(ptr, ctypes.c_void_p))


def _cred_delete(name):
    if not name:
        return False
    try:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
        adv.CredDeleteW.restype = wt.BOOL
        return bool(adv.CredDeleteW(name, 1, 0))
    except Exception:
        return False


def _paths():
    """测试会把 SECRET_PATH 重定向到临时目录：这时跟着它走，且**不碰真实凭据库**。

    返回 (vault 文件路径, 凭据名, 旧 secret.bin 路径)。凭据名为空 = 禁用凭据管理器。
    """
    legacy = SECRET_PATH
    if os.path.abspath(legacy) != os.path.abspath(_DEFAULT_SECRET_PATH):
        return os.path.join(os.path.dirname(legacy), "vault.bin"), "", legacy
    return VAULT_PATH, CRED_TARGET, legacy


def _write_atomic_bytes(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


# ── 令牌保险箱（多账号） ─────────────────────────────────────────────────
_VAULT_CACHE = {"vault": None, "persisted": True, "backend": "memory"}


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


def _parse_vault(plain):
    try:
        return _normalize(json.loads(plain))
    except Exception:
        # 老版本存的是一串裸 token
        token = plain.strip()
        if token:
            return {"version": 1, "current": LEGACY_ACCOUNT,
                    "accounts": {LEGACY_ACCOUNT: token}}
    return None


def _wipe_files(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def load_vault():
    """解开保险箱：凭据管理器 → 应用数据目录 → 旧插件目录（迁移后立刻删除）。"""
    if _VAULT_CACHE["vault"] is not None:
        return dict(_VAULT_CACHE["vault"])
    vault_path, cred_name, legacy_path = _paths()
    vault = None
    backend = "memory"
    from_legacy = False

    if cred_name:
        try:
            vault = _parse_vault(_cred_read(cred_name).decode("utf-8", "replace"))
            backend = "credential-manager"
        except Exception:
            vault = None
    if vault is None:
        try:
            with open(vault_path, "rb") as fh:
                raw = fh.read()
            if raw.startswith(b"DPAPI2"):
                plain = _dpapi("CryptUnprotectData", raw[6:], DPAPI_ENTROPY).decode(
                    "utf-8", "replace")
                vault = _parse_vault(plain)
                backend = "appdata"
        except Exception:
            vault = None
    if vault is None:
        try:
            with open(legacy_path, "rb") as fh:
                raw = fh.read()
            if raw.startswith(b"DPAPI1"):
                plain = _dpapi("CryptUnprotectData", raw[6:]).decode("utf-8", "replace")
                vault = _parse_vault(plain)
                from_legacy = bool(vault and vault["accounts"])
        except Exception:
            vault = None

    vault = vault or _empty_vault()
    _VAULT_CACHE.update(vault=vault, persisted=True, backend=backend)
    if from_legacy:
        # 旧密文在插件目录里 —— 立刻搬到系统保管位置并删掉旧文件。
        ok = _persist(vault)
        if ok:
            _wipe_files(legacy_path)
    return dict(vault)


def _persist(vault):
    """把保险箱写进系统保管位置。返回 False = 只在内存里有效。"""
    vault_path, cred_name, legacy_path = _paths()
    payload = json.dumps(vault, ensure_ascii=False).encode("utf-8")
    if cred_name:
        try:
            _cred_write(cred_name, payload)
            _wipe_files(vault_path, legacy_path)
            _VAULT_CACHE.update(persisted=True, backend="credential-manager")
            return True
        except Exception:
            pass
    try:
        sealed = _dpapi("CryptProtectData", payload, DPAPI_ENTROPY)
        _write_atomic_bytes(vault_path, b"DPAPI2" + sealed)
        _wipe_files(legacy_path)
        _VAULT_CACHE.update(persisted=True, backend="appdata")
        return True
    except Exception:
        _VAULT_CACHE.update(persisted=False, backend="memory")
        return False


def save_vault(vault):
    """整块加密存入系统保管位置；返回 False = 两者都不可用，只在内存有效。"""
    vault = _normalize(vault)
    _VAULT_CACHE["vault"] = vault
    if not vault["accounts"]:
        vault_path, cred_name, legacy_path = _paths()
        _cred_delete(cred_name)
        _wipe_files(vault_path, legacy_path)
        _VAULT_CACHE.update(persisted=True, backend="credential-manager")
        return True
    return _persist(vault)


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


def vault_backend():
    """令牌现在存在哪（给设置页显示，绝不显示令牌本身）。"""
    return str(_VAULT_CACHE.get("backend") or "memory")


def _reset_cache():
    """测试与「换插件目录」时用：把内存里的保险箱丢掉，强制下次重新解密。"""
    _VAULT_CACHE.update(vault=None, persisted=True, backend="memory")


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
    # 多库分账（0.8.3）：把旧的扁平账迁移到 libraries["legacy"] 名下。
    st.setdefault("libraries", {})
    if "legacy" not in st["libraries"] and (st.get("families") or st.get("pendingDeletes")):
        st["libraries"] = {"legacy": {
            "families": st.pop("families"),
            "pendingDeletes": st.pop("pendingDeletes", []),
        }}
        st["families"] = {}
        st["pendingDeletes"] = []
        save_state(st)
    return st


def save_state(st):
    _atomic_write_json(STATE_PATH, st)


def state_dir():
    return PLUGIN_DIR


def now_ms():
    return int(time.time() * 1000)


def b64(data):
    return base64.b64encode(data).decode("ascii")
