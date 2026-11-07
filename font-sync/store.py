"""配置、令牌保险箱、同步状态。

三份文件都在插件目录里，都是「拷走插件目录就带走」的：
- `config.json`   —— 用户设置（owner / repo / 同步哪些字族等），明文
- `secret.bin`    —— GitHub 令牌，**Windows DPAPI 加密**，只有当前 Windows 用户能解
- `state.json`    —— 每族最后同步的 zip 哈希与 asset id、待删队列

DPAPI 走 ctypes 直接调 crypt32，不引入任何第三方库；拿不到就**不落盘**
（令牌只留在内存里，本会话可用），绝不退化成明文。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
import uuid

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
SECRET_PATH = os.path.join(PLUGIN_DIR, "secret.bin")
STATE_PATH = os.path.join(PLUGIN_DIR, "state.json")

DEFAULT_CONFIG = {
    "repoName": "fontmanager-library",
    "sync": {
        # 只同步这些版权分类；空列表 = 全部。unknown 默认**不同步**：
        # 判不清授权的东西不该往云端放（默认从严，用户可自己放开）。
        "excludeCopyright": ["unknown"],
        # 单族 zip 上限（字节）。超了就跳过并报出来，免得一个 300 MB 的族把一次同步卡死。
        "maxFamilyBytes": 400 * 1024 * 1024,
        # 旧 asset 延迟删除的天数（给「另一台机器用旧版覆盖新版」留反悔窗口）
        "deleteAfterDays": 7,
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
    data = _read_json(CONFIG_PATH, {})
    for k, v in data.items():
        if k == "sync" and isinstance(v, dict):
            cfg["sync"].update(v)
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


def save_token(token):
    """加密后落盘。返回 True 表示真的写下去了。"""
    if not token:
        try:
            os.remove(SECRET_PATH)
        except OSError:
            pass
        return True
    try:
        sealed = _dpapi("CryptProtectData", token.encode("utf-8"))
        with open(SECRET_PATH, "wb") as fh:
            fh.write(b"DPAPI1" + sealed)
        return True
    except Exception:
        return False


def load_token():
    try:
        with open(SECRET_PATH, "rb") as fh:
            raw = fh.read()
        if not raw.startswith(b"DPAPI1"):
            return ""
        return _dpapi("CryptUnprotectData", raw[6:]).decode("utf-8", "replace")
    except Exception:
        return ""


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
