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
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gh as ghmod
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
]

NOT_NEEDED = [
    "不读你的其它仓库（只碰你填的那一个）",
    "不碰 Actions / workflow（改不了你的 CI）",
    "不碰组织设置、成员、SSH key、GPG key",
    "不读邮箱、关注、星标、私有 gist",
    "不删仓库、不改仓库可见性",
]


# ── 数据快照（只给页面看，不含令牌原文） ────────────────────────────────
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
            "maxFamilyMB": max(1, int(int(sync_cfg.get("maxFamilyBytes") or 0) / 1048576) or 400),
            "deleteAfterDays": int(sync_cfg.get("deleteAfterDays") or 7),
            "deleteRemoved": bool(sync_cfg.get("deleteRemovedFamilies", True)),
        },
        "deviceId": st.get("deviceId") or "",
        "syncedFamilies": len(st.get("families") or {}),
        "pendingDeletes": len(st.get("pendingDeletes") or []),
        "lastSyncAt": st.get("lastSyncAt") or 0,
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
            elif st["anonymousReadable"]:
                add("目标仓库可用性", False, f"{target_owner}/{name} 能匿名读取，同步已被禁止")
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
            return self._json({"ok": False, "error": "unknown endpoint"}, 404)
        return self._json({"ok": False, "error": "not found"}, 404)

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
        if path == "/api/repo/check":
            login = store.current_account()[0]
            if not login:
                return self._json({"ok": False, "error": "先到「账号与令牌」页保存一个账号"})
            owner, name = _ensure_target(login, data.get("owner"), data.get("repoName"))
            if not name:
                return self._json({"ok": False, "error": "先填仓库名"})
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
            client = ghmod.Gh(store.token_for(login))
            client.create_private_repo(name, "FontManager 字体库同步（私有，勿设为公开）")
            res = syncer.check_repo(client, owner, name)
            store.remember_repo(owner, name)
            return self._json({"ok": res["ok"], "message": "已建私有仓：" + res["message"],
                               "state": snapshot()})

        if path == "/api/save":
            name = str(data.get("repoName") or "").strip()
            if not name:
                return self._json({"ok": False, "error": "仓库名不能空"})
            owner = str(data.get("owner") or "").strip()
            try:
                max_mb = max(1, int(float(data.get("maxFamilyMB") or 400)))
            except Exception:
                max_mb = 400
            try:
                days = max(0, int(float(data.get("deleteAfterDays") or 7)))
            except Exception:
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
            store.save_config(patch)
            store.remember_repo(owner or store.current_account()[0], name)
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

        # ── 杂项 ──────────────────────────────────────────────
        if path == "/api/open_dir":
            try:
                os.startfile(store.PLUGIN_DIR)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if path == "/api/reset":
            store.save_vault({"version": 1, "current": "", "accounts": {}})
            for p in (store.CONFIG_PATH, store.STATE_PATH):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return self._json({"ok": True, "state": snapshot()})

        if path == "/api/close":
            threading.Thread(target=self.panel.stop, daemon=True).start()
            return self._json({"ok": True})

        return self._json({"ok": False, "error": "unknown endpoint"}, 404)


class Panel:
    def __init__(self, actions=None):
        self.key = secrets.token_urlsafe(24)
        self.actions = actions or {}
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


def open_panel(actions=None, tab="account"):
    """已经开着就复用（同一个 key），否则新起一个。返回 URL。"""
    panel = _CURRENT.get("panel")
    if panel and panel.alive():
        panel.actions.update(actions or {})
        return panel.open(tab)
    panel = Panel(actions).start()
    _CURRENT["panel"] = panel
    return panel.open(tab)
