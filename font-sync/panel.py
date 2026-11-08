"""设置在浏览器里打开：本地起一个只监听 127.0.0.1 的小服务，把 panel.html 发给默认浏览器。

为什么不用 tkinter：插件自带的是 Python **embeddable** 发行版，不含 tkinter
（实测 `runtime/python.exe -c "import tkinter"` → ModuleNotFoundError）。
用户机器上装没装 Python 也不该影响设置面板，所以走浏览器：零依赖，还能放
可点击的 GitHub 链接、权限表和折叠教程。

安全边界：
- 只绑 127.0.0.1（不监听对外网卡）；
- 每次打开生成一次性随机 key，页面和所有 API 都要带上；
- 页面永远不会拿到令牌原文（只回打码后的），令牌只在 Python 进程里流转；
- 没有 CORS 头，别的站点读不到响应；
- 45 分钟无请求自动关停。
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gh as ghmod
import packer
import store
import syncer

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_HTML = os.path.join(HERE, "panel.html")
IDLE_SECONDS = 45 * 60

# 一条功能 → 一项权限，直接对应 GitHub 文档里的 scope。设置页「权限」页签用。
PERMISSION_TABLE = [
    {"feature": "读取你的账号名", "api": "GET /user", "scope": "任意令牌",
     "why": "用来显示「已连接 xxx」，也是一次最简单的连通性测试"},
    {"feature": "创建私有仓库", "api": "POST /user/repos", "scope": "repo",
     "why": "首次同步时自动把库建出来（建出来的默认就是 private）"},
    {"feature": "读仓库信息 / 分支 / 是否私有", "api": "GET /repos/*, /git/ref/*",
     "scope": "repo", "why": "私有仓的内容用只读令牌读不到；也用来确认没被改成公开"},
    {"feature": "上传字体 zip（release asset）", "api": "POST /releases/*/assets",
     "scope": "repo", "why": "字体本体走 release，不进 git 历史"},
    {"feature": "写同步记账（ops/*.json）", "api": "Git Data API + PATCH /git/refs",
     "scope": "repo", "why": "记录哪台机器在什么时候传了哪些字族"},
    {"feature": "删旧版本 asset", "api": "DELETE /releases/assets/*",
     "scope": "repo", "why": "只删本插件自己上传的旧包，保留天数可在高级里调"},
    {"feature": "下载字体包（新机器恢复）", "api": "GET /releases/assets/*（下载时跳到 objects）",
     "scope": "repo", "why": "「下载回本机」要读私有仓的 release，匿名拿不到"},
    {"feature": "让 FontManager 重新扫描字体库", "api": "POST /api/v1/fonts/scan（本地 API）",
     "scope": "write:fonts", "why": "下载回来的字体要让界面立刻看见，不用你手动点一次重新扫描"},
]

NOT_NEEDED = [
    "不读你的其它仓库（只碰你填的那一个）",
    "不碰 Actions / workflow（改不了你的 CI）",
    "不碰组织设置、成员、SSH key、GPG key",
    "不读邮箱、关注、星标、私有 gist",
    "不删仓库、不改仓库可见性",
]


# ── 数据快照（只给页面看，不含令牌原文） ────────────────────────────────
def _save_sync_rules(data, owner="", name=""):
    """把界面上的「同步规则」写进 config.json，返回生效的单族上限（MB）。

    上限的唯一默认值在 `store.DEFAULT_CONFIG`，这里不再写死数字 ——
    以前 400 散在三处（store / snapshot / save），改一处忘一处就会出现
    「输入框显示 400，实际按 85 跑」这种对不上的情况。
    """
    default_mb = store.DEFAULT_CONFIG["sync"]["maxFamilyBytes"] // 1048576
    try:
        max_mb = max(1, int(float(data.get("maxFamilyMB") or default_mb)))
    except (TypeError, ValueError):
        max_mb = default_mb
    try:
        days = max(0, int(float(data.get("deleteAfterDays") or 7)))
    except (TypeError, ValueError):
        days = 7
    exclude = ["unknown"] if data.get("excludeUnknown", True) else []
    patch = {
        "owner": owner, "repoName": name,
        "account": store.current_account()[0],
        "sync": {"excludeCopyright": exclude,
                 "maxFamilyBytes": max_mb * 1024 * 1024,
                 "deleteAfterDays": days,
                 "deleteRemovedFamilies": bool(data.get("deleteRemoved", True))},
    }
    if "restoreDir" in data:
        patch["restoreDir"] = str(data.get("restoreDir") or "").strip()
    if "overwriteExisting" in data:
        patch["overwriteExisting"] = bool(data.get("overwriteExisting"))
    store.save_config(patch)
    if name:
        store.remember_repo(owner or store.current_account()[0], name)
    return max_mb


def _all_family_names(st):
    """所有账本里出现过的族名并集（多库切换，任一库同步过的都算「本机有记录」）。

    0.8.6 修：snapshot() 只数旧扁平 state.families —— 多库分账（0.8.3）后
    面板永远显示「已同步 0 个族」。
    """
    names = set(st.get("families") or {})
    for led in (st.get("libraries") or {}).values():
        if isinstance(led, dict):
            names.update(led.get("families") or {})
    return names


def _all_pending_deletes(st):
    """所有账本待删远端备份的总数（含旧扁平账）。"""
    total = len(st.get("pendingDeletes") or [])
    for led in (st.get("libraries") or {}).values():
        if isinstance(led, dict):
            total += len(led.get("pendingDeletes") or [])
    return total


def snapshot():
    cfg = store.load_config()
    login, token = store.current_account()
    sync_cfg = cfg.get("sync") or {}
    st = store.load_state()
    try:
        with open(os.path.join(HERE, "plugin.json"), encoding="utf-8") as fh:
            version = json.load(fh).get("version") or ""
    except Exception:
        version = ""
    return {
        "version": version,
        "configured": store.configured(),
        "login": login,
        "tokenMask": store.mask(token) if token else "",
        "vaultPersisted": store.vault_persisted(),
        "accounts": [{"login": n, "current": cur} for n, cur in store.accounts()],
        "owner": cfg.get("owner") or "",
        "repoName": cfg.get("repoName") or "",
        "recentRepos": [{"owner": r.get("owner") or "", "name": r.get("name") or ""}
                        for r in (cfg.get("recentRepos") or [])],
        "sync": {
            "excludeUnknown": "unknown" in {str(x).lower()
                                            for x in (sync_cfg.get("excludeCopyright") or [])},
            "maxFamilyMB": max(1, int(int(sync_cfg.get("maxFamilyBytes")
                                       or store.DEFAULT_CONFIG["sync"]["maxFamilyBytes"]) / 1048576)),
            "deleteAfterDays": int(sync_cfg.get("deleteAfterDays") or 7),
            "deleteRemoved": bool(sync_cfg.get("deleteRemovedFamilies", True)),
        },
        "deviceId": st.get("deviceId") or "",
        "syncedFamilies": len(_all_family_names(st)),
        "pendingDeletes": _all_pending_deletes(st),
        "lastSyncAt": st.get("lastSyncAt") or 0,
        "lastRestoreAt": st.get("lastRestoreAt") or 0,
        "restore": {
            "dir": cfg.get("restoreDir") or "",
            # 自动推断出来的目标目录：只在 UI 上显示，让用户知道文件会落到哪
            "resolvedDir": syncer.read_app_library_dir(),
            "overwriteExisting": bool(cfg.get("overwriteExisting")),
        },
        "pluginDir": store.PLUGIN_DIR,
        "permissionTable": PERMISSION_TABLE,
        "notNeeded": NOT_NEEDED,
    }


def _scope_flags(scopes):
    """classic 令牌的 scopes；fine-grained 返回空列表（GitHub 不给这个头）。"""
    have = {s.strip() for s in (scopes or []) if s and s.strip()}
    if not have:
        return {"known": False, "repo": None, "scopes": []}
    repo = "repo" in have or any(h.startswith("repo:") for h in have)
    return {"known": True, "repo": bool(repo), "scopes": sorted(have)}


def capabilities(login, owner="", name=""):
    """逐项体检：页面「权限」页签底部那张表就是它填的。

    能实测的就实测（读账号、读仓库），不能实测的（建仓/上传）按 scope 推断，
    推断不出来就如实说「未知」，不假装通过。
    """
    token = store.token_for(login)
    if not token:
        return {"ok": False, "error": "这个账号没有令牌", "items": []}
    client = ghmod.Gh(token)
    flags = _scope_flags(client.token_scopes())
    items = []

    def add(feature, ok, detail, need="repo"):
        items.append({"feature": feature, "ok": ok, "detail": detail, "need": need})

    try:
        me = client.whoami()
        add("读取账号信息", True, f"已连接 {me.get('login')}，令牌有效", "任意令牌")
        login = me.get("login") or login
    except Exception as e:
        add("读取账号信息", False, f"调用失败：{e}", "任意令牌")
        return {"ok": False, "items": items, "flags": flags, "login": login}

    if flags["known"]:
        if flags["repo"]:
            add("创建 / 读写私有仓库", True, "令牌带 repo 权限，建仓、传包、记账都够用")
        else:
            add("创建 / 读写私有仓库", False,
                f"令牌只有 {'/'.join(flags['scopes']) or '无'}，缺 repo —— 同步会被 GitHub 拒（403）")
    else:
        add("创建 / 读写私有仓库", None,
            "fine-grained 令牌不返回 scope，无法预判；如果同步报 403，请换 classic 令牌（勾 repo）")

    target_owner = (owner or login).strip()
    if name:
        try:
            st = client.repo_state(target_owner, name)
            if not st["exists"]:
                add("目标仓库可用性", None, f"{target_owner}/{name} 还不存在，首次同步会自动建私有仓")
            elif not st["private"]:
                add("目标仓库可用性", False, f"{target_owner}/{name} 是公开仓库，同步已被禁止")
            elif st["anonymousReadable"] is True:
                add("目标仓库可用性", False, f"{target_owner}/{name} 能匿名读取，同步已被禁止")
            elif st["anonymousReadable"] is None:
                add("目标仓库可用性", None,
                    f"{target_owner}/{name} 私有（匿名复核被 GitHub 限流跳过，以登录态为准）")
            else:
                add("目标仓库可用性", True,
                    f"{target_owner}/{name} 私有、匿名读不到"
                    + ("，空仓（首次会补 README）" if st["empty"] else ""))
        except Exception as e:
            add("目标仓库可用性", False, f"查仓库失败：{e}")
    else:
        add("目标仓库可用性", None, "还没填仓库名")

    failed = [i for i in items if i["ok"] is False]
    return {"ok": not failed, "items": items, "flags": flags, "login": login}


REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _edge_exe():
    """找 Edge：--app 窗口模式用它。装了 WebView2 的机器必然有。"""
    for p in (
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ):
        if os.path.exists(p):
            return p
    return shutil.which("msedge")


def repo_name_problem(name):
    """GitHub 的仓库名规则，提前拦住比让它回 422 好懂。

    返回人类可读的问题；没问题返回空串。前端也照这套规则实时提示，
    但后端必须再验一遍 —— 手填/脚本调用绕得过界面。
    """
    n = (name or "").strip()
    if not n:
        return "仓库名不能空"
    if len(n) > 100:
        return "仓库名最长 100 个字符"
    if not REPO_NAME_RE.match(n):
        return "只能用英文字母、数字、点、下划线、连字符（中文和空格不行）"
    if n.startswith("."):
        return "不能以点开头"
    if n in (".", ".."):
        return "不能用 . 或 .. 当仓库名"
    return ""


def _ensure_target(login, owner, name):
    return (owner or "").strip() or login, (name or "").strip()


# ── HTTP ────────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    server_version = "FontSyncPanel/0.2"
    protocol_version = "HTTP/1.1"

    # 面板自己就是本机调试用的，别往 stderr 刷请求日志
    def log_message(self, *_args):
        pass

    # -- 小工具 --
    @property
    def panel(self):
        return self.server.panel

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query_key=""):
        key = self.headers.get("X-Panel-Key") or query_key
        return bool(key) and secrets.compare_digest(key, self.panel.key)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1 << 20:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # -- GET --
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        self.panel.hit()
        if parsed.path in ("/", "/index.html"):
            key = (q.get("k") or [""])[0]
            if not self._authorized(key):
                return self._html("<h3>链接已失效</h3>"
                                  "<p>请回 FontManager 里重新打开「设置 GitHub 库…」。</p>", 403)
            try:
                with open(PANEL_HTML, encoding="utf-8") as fh:
                    return self._html(fh.read())
            except Exception as e:
                return self._html(f"<h3>找不到面板文件</h3><pre>{e}</pre>", 500)
        if parsed.path.startswith("/api/"):
            if not self._authorized():
                return self._json({"ok": False, "error": "unauthorized"}, 403)
            if parsed.path == "/api/state":
                return self._json({"ok": True, "state": snapshot()})
            if parsed.path == "/api/progress":
                return self._json({"ok": True, "progress": self.panel.progress()})
            if parsed.path == "/api/restore/info":
                return self._json(self._restore_info())
            return self._json({"ok": False, "error": "unknown endpoint"}, 404)
        return self._json({"ok": False, "error": "not found"}, 404)

    # -- 恢复：远端有什么 --
    def _restore_info(self):
        """列出远端所有字体族。慢（要翻 release 的 asset），页面只在用户点按钮时调。"""
        login, token = store.current_account()
        if not login or not token:
            return {"ok": False, "error": "先到「账号与令牌」页保存一个账号"}
        cfg = store.load_config()
        name = (cfg.get("repoName") or "").strip()
        if not name:
            return {"ok": False, "error": "还没设置仓库：先到「仓库」页选一个库"}
        owner = (cfg.get("owner") or login).strip()
        client = ghmod.Gh(token)
        try:
            rows = syncer.list_remote(client, owner, name, log=lambda *_a: None)
        except Exception as e:
            msg = f"读不到 {owner}/{name}：{e}"
            if "404" in str(e):
                msg = (f"备份仓库 {owner}/{name} 不存在（可能已被删除）。"
                       f"到上面「选一个库」重新选或新建一个 —— 首次同步会自动重建私有仓。")
            return {"ok": False, "error": msg}
        total = sum(r["size"] for r in rows)
        st = store.load_state()
        # state.families 的键是**原始族名**，asset 名是 slug —— 两边都过归一化再比，
        # 顺带兼容老包被 GitHub 改写的名字（`!`→`.`）。
        # 多库分账（0.8.3）：所有库的账取并集 —— 任一库同步过的都算「本机有记录」。
        all_fams = _all_family_names(st)
        synced = {packer.slug_key(k) for k in all_fams}
        for row in rows:
            row["synced"] = packer.slug_key(row["slug"]) in synced
        return {"ok": True, "owner": owner, "name": name, "count": len(rows),
                "bytes": total, "families": rows[:2000],
                "target": cfg.get("restoreDir") or syncer.read_app_library_dir()}

    # -- 补缺：快照里有、这台机器上没有的字体 --
    def _local_api(self):
        """拿主程序本地 API 客户端。面板只能从 FontManager 的插件菜单打开（actions 注入）。"""
        fn = self.panel.actions.get("localApi")
        if not fn:
            return None, {"ok": False,
                          "error": "这个功能要从 FontManager 里打开面板才能用（右键插件 → 设置 GitHub 库…）"}
        try:
            return fn(), None
        except Exception as e:
            return None, {"ok": False, "error": f"本地 API 不可用：{e}"}

    @staticmethod
    def _token_error(e):
        """read:snapshot 是 0.8.0 新增权限；旧令牌（插件启用时签发的）没有它。"""
        msg = str(e)
        if "403" in msg or "read:snapshot" in msg:
            return ("插件令牌还没有读快照的权限（0.8.0 新增）："
                    "重启 FontManager（或关掉再打开本插件）就会拿到新令牌")
        return None

    def _missing_snapshots(self):
        api, err = self._local_api()
        if err:
            return err
        try:
            snaps = (api.get("/snapshots") or {}).get("snapshots") or []
        except Exception as e:
            friendly = self._token_error(e)
            return {"ok": False, "error": friendly or f"读快照失败：{e}"}
        out = [{"name": s.get("name") or "", "count": len(s.get("entries") or [])}
               for s in snaps]
        return {"ok": True, "snapshots": out}

    def _missing_analyze(self, name):
        """快照里有、本机没有的族；再对一遍远端，标出「能从 GitHub 补」的。"""
        api, err = self._local_api()
        if err:
            return err
        try:
            snaps = (api.get("/snapshots") or {}).get("snapshots") or []
        except Exception as e:
            friendly = self._token_error(e)
            return {"ok": False, "error": friendly or f"读快照失败：{e}"}
        snap = next((s for s in snaps if (s.get("name") or "") == name), None)
        if snap is None:
            return {"ok": False, "error": f"找不到快照：{name}"}
        want = {}
        for e in (snap.get("entries") or []):
            # 系统自带字体任何 Windows 机器都有，不算缺失
            if e.get("system"):
                continue
            fam = (e.get("family") or "").strip()
            if fam:
                want[fam] = want.get(fam, 0) + 1
        have = {(f.get("family") or "").strip() for f in api.faces()}
        missing = sorted(f for f in want if f not in have)

        login, token = store.current_account()
        cfg = store.load_config()
        repo = (cfg.get("repoName") or "").strip()
        owner = (cfg.get("owner") or login).strip()
        by_key = {}
        if repo and token:
            try:
                rows = syncer.list_remote(ghmod.Gh(token), owner, repo,
                                          log=lambda *_a: None)
            except Exception:
                rows = []
            by_key = {packer.slug_key(r["slug"]): r for r in rows}

        items = []
        for fam in missing:
            row = by_key.get(packer.slug_key(fam))
            items.append({"family": fam, "faces": want[fam], "onRemote": bool(row),
                          "asset": (row or {}).get("asset") or "",
                          "size": int((row or {}).get("size") or 0)})
        return {"ok": True, "snapshot": name, "entries": sum(want.values()),
                "families": len(want), "missing": items,
                "downloadable": sum(1 for i in items if i["onRemote"]),
                "notOnRemote": [i["family"] for i in items if not i["onRemote"]],
                "target": cfg.get("restoreDir") or syncer.read_app_library_dir()}

    # -- POST --
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        self.panel.hit()
        if not self._authorized():
            return self._json({"ok": False, "error": "unauthorized"}, 403)
        data = self._body()
        try:
            return self._dispatch(path, data)
        except Exception as e:  # 任何异常都回给页面，别静默
            return self._json({"ok": False, "error": str(e)}, 200)

    def _dispatch(self, path, data):
        # restore/info 是只读端点，历史上注册在 do_GET；前端 api() 助手把它
        # 发成 POST（read 白名单只有 state/progress），两边一旦错位就 404。
        # 这里 GET/POST 都收，路由永不因方法不对而失联。
        if path == "/api/restore/info":
            return self._json(self._restore_info())
        # ── 账号 ──────────────────────────────────────────────
        if path == "/api/account/test":
            token = str(data.get("token") or "").strip()
            if not token:
                return self._json({"ok": False, "error": "先粘贴令牌"})
            client = ghmod.Gh(token)
            me = client.whoami()
            login = me.get("login") or ""
            saved = store.upsert_account(login, token, make_current=True)
            cfg = store.load_config()
            owner, name = _ensure_target(login, cfg.get("owner"), cfg.get("repoName"))
            caps = capabilities(login, owner, name)
            return self._json({
                "ok": True,
                "login": login,
                "name": me.get("name") or "",
                "saved": bool(saved),
                "persisted": store.vault_persisted(),
                "caps": caps,
                "state": snapshot(),
            })

        if path == "/api/account/select":
            login = str(data.get("login") or "")
            ok = store.set_current(login)
            store.save_config({"account": login})
            return self._json({"ok": bool(ok), "state": snapshot()})

        if path == "/api/account/copy":
            # 用户要求：账号行一键复制完整令牌。面板服务只监听 127.0.0.1、
            # 且要一次性 key 才能进到这里；令牌本身也是本机 DPAPI 存的。
            login = str(data.get("login") or "").strip()
            tok = store.token_for(login or None)
            if not tok:
                return self._json({"ok": False, "error": f"没有 {login or '当前账号'} 的令牌"})
            return self._json({"ok": True, "token": tok})

        if path == "/api/account/remove":
            login = str(data.get("login") or "")
            store.remove_account(login)
            left = store.current_account()[0]
            store.save_config({"account": left})
            return self._json({"ok": True, "state": snapshot()})

        if path == "/api/account/probe":
            login = store.current_account()[0]
            if not login:
                return self._json({"ok": False, "error": "还没有账号"})
            cfg = store.load_config()
            owner, name = _ensure_target(login, cfg.get("owner"), cfg.get("repoName"))
            return self._json({"ok": True, "caps": capabilities(login, owner, name),
                               "state": snapshot()})

        # ── 仓库 ──────────────────────────────────────────────
        if path == "/api/repos":
            login = store.current_account()[0]
            if not login:
                return self._json({"ok": False, "error": "先到「账号与令牌」页保存一个账号"})
            rows = ghmod.Gh(store.token_for(login)).list_repos()
            repos = [{
                "fullName": r.get("full_name") or "",
                "name": r.get("name") or "",
                "owner": ((r.get("owner") or {}).get("login") or ""),
                "private": bool(r.get("private")),
                "updatedAt": r.get("pushed_at") or r.get("updated_at") or "",
                "empty": int(r.get("size") or 0) == 0,
            } for r in rows]
            repos.sort(key=lambda x: (not x["private"], x["fullName"].lower()))
            return self._json({"ok": True, "login": login, "repos": repos})

        if path == "/api/repo/use":
            login = store.current_account()[0]
            if not login:
                return self._json({"ok": False, "error": "先保存账号"})
            owner, name = _ensure_target(login, data.get("owner"), data.get("repoName"))
            if not name:
                return self._json({"ok": False, "error": "先选一个库"})
            res = syncer.check_repo(ghmod.Gh(store.token_for(login)), owner, name)
            if not res["ok"]:
                return self._json({"ok": False, "message": res["message"]})
            store.save_config({"owner": owner if owner != login else "",
                               "repoName": name, "account": login})
            store.remember_repo(owner, name)
            return self._json({"ok": True, "message": res["message"], "state": snapshot()})

        if path == "/api/repo/check":
            login = store.current_account()[0]
            if not login:
                return self._json({"ok": False, "error": "先到「账号与令牌」页保存一个账号"})
            owner, name = _ensure_target(login, data.get("owner"), data.get("repoName"))
            problem = repo_name_problem(name)
            if problem:
                return self._json({"ok": False, "error": problem})
            res = syncer.check_repo(ghmod.Gh(store.token_for(login)), owner, name)
            return self._json({"ok": res["ok"], "message": res["message"],
                               "willCreate": bool(res.get("willCreate")),
                               "willInit": bool(res.get("willInit"))})

        if path == "/api/repo/create":
            login = store.current_account()[0]
            if not login:
                return self._json({"ok": False, "error": "先保存账号"})
            owner, name = _ensure_target(login, data.get("owner"), data.get("repoName"))
            if not name:
                return self._json({"ok": False, "error": "先填仓库名"})
            problem = repo_name_problem(name)
            if problem:
                return self._json({"ok": False, "error": problem})
            if owner.lower() != login.lower():
                # /user/repos 只能建在自己名下，硬建会悄悄建到个人账号里去
                return self._json({"ok": False, "error":
                                   f"新建只能建在你自己的账号下；{owner} 是组织，"
                                   f"先让管理员把库建好，再展开「手动填写」填它"})
            client = ghmod.Gh(store.token_for(login))
            st = client.repo_state(owner, name)
            existed = bool(st.get("exists"))
            if existed:
                # 防呆：名字撞了不该是红字报错，直接切过去更符合用户意图
                res = syncer.check_repo(client, owner, name)
                message = "这个库已经存在，已直接设为当前库：" + res["message"]
            else:
                client.create_private_repo(name, "FontManager 字体库同步（私有，勿设为公开）")
                res = syncer.check_repo(client, owner, name)
                message = "已建好并使用：" + res["message"]
            if res["ok"]:
                store.save_config({"owner": owner if owner != login else "",
                                   "repoName": name, "account": login})
            store.remember_repo(owner, name)
            return self._json({"ok": res["ok"], "existed": existed, "message": message,
                               "state": snapshot()})

        if path == "/api/save":
            name = str(data.get("repoName") or "").strip()
            problem = repo_name_problem(name)
            if problem:
                return self._json({"ok": False, "error": problem})
            owner = str(data.get("owner") or "").strip()
            _save_sync_rules(data, owner, name)
            return self._json({"ok": True, "message": "已保存", "state": snapshot()})

        # ── 动作（交给主程序界面的通知去反馈进度） ─────────────
        if path == "/api/dryrun":
            fn = self.panel.actions.get("dry")
            if not fn:
                return self._json({"ok": False, "error": "这个动作需要从插件菜单触发"})
            fn()
            return self._json({"ok": True, "message": "预演已开始，进度看 FontManager 的通知"})

        if path == "/api/sync":
            fn = self.panel.actions.get("sync")
            if not fn:
                return self._json({"ok": False, "error": "这个动作需要从插件菜单触发"})
            fn()
            return self._json({"ok": True, "message": "同步已开始，进度看 FontManager 的通知"})

        if path == "/api/restore/config":
            patch = {}
            if "restoreDir" in data:
                patch["restoreDir"] = str(data.get("restoreDir") or "").strip()
            if "overwriteExisting" in data:
                patch["overwriteExisting"] = bool(data.get("overwriteExisting"))
            if not patch:
                return self._json({"ok": False, "error": "没有要保存的字段"})
            store.save_config(patch)
            return self._json({"ok": True, "state": snapshot()})

        if path == "/api/restore/dry":
            fn = self.panel.actions.get("restoreDry")
            if not fn:
                return self._json({"ok": False, "error": "这个动作需要从插件菜单触发"})
            fn()
            return self._json({"ok": True, "message": "预演下载已开始，进度见下方进度条"})

        if path == "/api/open-external":
            # 面板开在主程序应用内窗口后，页面点不动外链（webview 禁外部跳转）——
            # 请插件进程发 open-url 消息，主程序用系统默认浏览器打开（1.4.5+）
            fn = self.panel.actions.get("openExternal")
            if not fn:
                return self._json({"ok": False, "error": "no-handler"})
            fn(str(data.get("url") or ""))
            return self._json({"ok": True})
        if path == "/api/restore":
            fn = self.panel.actions.get("restore")
            if not fn:
                return self._json({"ok": False, "error": "这个动作需要从插件菜单触发"})
            fn()
            return self._json({"ok": True, "message": "恢复已开始，进度见下方进度条"})

        if path == "/api/missing/analyze":
            name = str(data.get("name") or "").strip()
            if not name:
                return self._json({"ok": False, "error": "先选一个快照"})
            return self._json(self._missing_analyze(name))

        if path == "/api/missing/snapshots":
            # 统一走 POST（0.4.1 的教训：别让「有没有 body」决定方法）
            return self._json(self._missing_snapshots())

        if path == "/api/missing/restore":
            # 业务在 main.dispatch_action（restoreMissing）里；面板只负责转交
            fn = self.panel.actions.get("dispatch")
            if not fn:
                return self._json({"ok": False,
                                   "error": "这个功能要从 FontManager 里打开面板才能用"})
            fams = data.get("families")
            if not isinstance(fams, list) or not fams:
                return self._json({"ok": False, "error": "先勾选要下载的字族"})
            return self._json(fn("restoreMissing", {"families": fams}))

        # ── 杂项 ──────────────────────────────────────────────
        if path == "/api/open_dir":
            target = store.PLUGIN_DIR
            if not os.path.isdir(target):
                return self._json({"ok": False, "error": f"目录不存在：{target}",
                                   "path": target})
            first = ""
            try:
                os.startfile(target)  # 交给 shell，等于在资源管理器里双击
                return self._json({"ok": True, "path": target})
            except Exception as e:
                first = str(e)
            # 兜底：有的环境里 ShellExecute 被策略挡住，直接拉资源管理器
            try:
                import subprocess
                subprocess.Popen(["explorer.exe", target], close_fds=True)
                return self._json({"ok": True, "path": target, "via": "explorer"})
            except Exception as e:
                return self._json({"ok": False, "path": target,
                                   "error": f"打开失败：{first}；改用资源管理器也失败：{e}"})

        if path == "/api/reset":
            store.save_vault({"version": 1, "current": "", "accounts": {}})
            for p in (store.CONFIG_PATH, store.STATE_PATH):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return self._json({"ok": True, "state": snapshot()})

        return self._json({"ok": False, "error": "unknown endpoint"}, 404)


class Panel:
    def __init__(self, actions=None, progress=None):
        self.key = secrets.token_urlsafe(24)
        self.actions = actions or {}
        # 插件主进程注入的「当前同步进度」读取函数；面板只读不写
        self.progress = progress or (lambda: {})
        self.last_hit = time.time()
        self.httpd = None
        self.port = 0
        self._stop = threading.Event()

    def start(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.daemon_threads = True
        httpd.panel = self
        self.httpd = httpd
        self.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.3},
                         daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()
        return self

    def _reaper(self):
        while not self._stop.wait(30):
            if time.time() - self.last_hit > IDLE_SECONDS:
                self.stop()
                return

    def hit(self):
        self.last_hit = time.time()

    def url(self, tab="account"):
        return f"http://127.0.0.1:{self.port}/?k={self.key}&tab={tab}"

    def open(self, tab="account"):
        url = self.url(tab)
        # 首选：Edge 的 --app 模式 —— 没有地址栏、没有标签页的独立窗口，
        # 观感就是一个应用窗口（Win10/11 自带 Edge；装了 WebView2 就一定有）。
        # 打不开再退回系统默认浏览器。
        edge = _edge_exe()
        if edge:
            try:
                subprocess.Popen([edge, f"--app={url}", "--window-size=1000,900"],
                                 close_fds=True)
                return url
            except Exception:
                pass
        try:
            webbrowser.open(url, new=2)
        except Exception:
            try:
                os.startfile(url)
            except Exception:
                pass
        return url

    def alive(self):
        return bool(self.httpd) and not self._stop.is_set()

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        if _CURRENT.get("panel") is self:
            _CURRENT["panel"] = None


_CURRENT = {"panel": None}


def open_panel(actions=None, tab="account", progress=None, auto_open=True):
    """已经开着就复用（同一个 key），否则新起一个。返回 URL。

    `auto_open=False`：只把服务起好，不自己开窗口 —— 交给调用方决定
    （main 会按主程序版本决定「应用内窗口」还是「Edge 独立窗口」）。
    """
    panel = _CURRENT.get("panel")
    if panel and panel.alive():
        panel.actions.update(actions or {})
        if progress:
            panel.progress = progress
        if auto_open:
            panel.open(tab)
        return panel.url(tab)
    panel = Panel(actions, progress=progress).start()
    _CURRENT["panel"] = panel
    if auto_open:
        panel.open(tab)
    return panel.url(tab)


def current_panel():
    """当前活着的面板（可能为 None）。main 用它自己控制开窗方式。"""
    p = _CURRENT.get("panel")
    return p if (p and p.alive()) else None
