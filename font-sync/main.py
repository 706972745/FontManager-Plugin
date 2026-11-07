#!/usr/bin/env python3
"""字体库同步（GitHub）—— 把字体库按字族打包，增量同步到自己的私有仓库。

每个用户用**自己的** GitHub 账号和仓库：插件支持保存多个账号（各自的令牌），
在设置面板里切换「用哪个账号、往哪个库同步」。没配置好之前**不会**同步任何东西。

为什么是「按字族」而不是「整库一份大包」：
改一个 60 KB 的英文字体，不该重传 520 MB。所以每个字族一个 zip，
内容没变就整族跳过；变了才重新打包，打包后哈希一样还不上传。

安全边界（三条硬闸，任一不满足就中止）：
1. 仓库必须**私有**且在 GitHub 上真私有（`private` 字段 + 匿名探一次）；
2. 令牌用 Windows DPAPI 加密落盘，只有当前 Windows 用户能解；
3. 判不清授权的族（`copyrightClass = unknown`）**默认不同步**，用户可在设置里放开。

通信协议与其它插件一致：stdin 收 JSON 行，stdout 发 JSON 行。
"""

import json
import os
import sys
import threading
import traceback

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import gh as ghmod  # noqa: E402
import store  # noqa: E402
import syncer  # noqa: E402

API_BASE = ""
API_TOKEN = ""
SYNC_LOCK = threading.Lock()

MENU_FAMILY = "同步这个字族到 GitHub"
MENU_ALL = "立即同步（全部）"
MENU_DRYRUN = "预演本次同步（不传）"
MENU_SETTINGS = "设置 GitHub 库…"
MENU_OPEN = "打开同步状态目录"


# ── 协议 ────────────────────────────────────────────────────────────────
def emit(kind, message):
    sys.stdout.write(json.dumps({"type": kind, "message": message}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(message):
    emit("log", message)


def toast(message):
    emit("toast", message)


# ── 客户端 ──────────────────────────────────────────────────────────────
def local_api():
    if not API_BASE or not API_TOKEN:
        raise RuntimeError("还没和主程序握手（apiBase / apiToken 为空）")
    return syncer.LocalApi(API_BASE, API_TOKEN)


def gh_client(login=None):
    token = store.token_for(login)
    if not token:
        raise RuntimeError("这个账号还没存令牌：设置 → 插件 → 齿轮 → 填令牌 → 测试并保存")
    return ghmod.Gh(token)


def require_configured(interactive=True):
    """没配置好就别同步 —— 顺手把设置面板弹出来，省得用户去找齿轮。"""
    if store.configured():
        return True
    login, token = store.current_account()
    if not login or not token:
        toast("先设置 GitHub 库：填令牌 + 选一个仓库（这就打开设置）")
        log("未配置：没有可用的账号令牌")
    else:
        toast("先设置 GitHub 库：还差仓库名（这就打开设置）")
        log("未配置：账号有了，仓库名还空着")
    if interactive:
        open_settings_panel()
    return False


# ── 同步任务 ────────────────────────────────────────────────────────────
def do_sync(only_family=None, dry_run=False):
    if not require_configured():
        return
    if not SYNC_LOCK.acquire(blocking=False):
        toast("已经在同步了，等这一轮结束")
        return
    try:
        cfg = store.load_config()
        state = store.load_state()
        api = local_api()
        client = gh_client()
        login = store.current_account()[0]
        target = f"{(cfg.get('owner') or login)}/{cfg.get('repoName')}"
        scope = only_family or "全部字族"
        toast(("预演：" if dry_run else "开始同步：") + f"{scope} → {target}")
        log(f"=== 同步开始（{scope}{'，预演' if dry_run else ''}），账号 {login}，目标 {target} ===")
        summary = syncer.run_sync(client, api, cfg, state, only_family=only_family,
                                  dry_run=dry_run, log=log)
        if dry_run:
            toast(f"预演完成：{summary['planned']} 个族待传，未上传任何内容")
        else:
            msg = f"同步完成：上传 {summary['uploaded']} 个族（{summary['bytes']/1048576:.1f} MB）"
            if summary["failed"]:
                msg += f"，{len(summary['failed'])} 个失败"
            toast(msg)
    except Exception as e:
        log("同步失败：" + traceback.format_exc(limit=4).replace("\n", " | "))
        toast(f"同步失败：{e}")
    finally:
        SYNC_LOCK.release()


def run_async(func, *args, **kwargs):
    threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()


# ── 设置面板 ────────────────────────────────────────────────────────────
def open_settings_panel():
    """账号 + 仓库的设置面板。没有 tkinter 就退化成「改 config.json」的提示。

    注意：**不能在主线程跑 mainloop**，否则面板开着时右键菜单进不来。
    """
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception:
        toast(f"这个 Python 没有 tkinter，请直接改 {store.CONFIG_PATH}")
        log(f"没有 tkinter，配置文件在：{store.CONFIG_PATH}")
        return

    def run():
        cfg = store.load_config()
        sync_cfg = cfg.get("sync") or {}
        vault = store.load_vault()
        cur_login = vault.get("current") or ""

        root = tk.Tk()
        root.title("字体库同步 · 设置 GitHub 库")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        pad = {"padx": 12, "pady": 4}

        def status(label, text, ok=True):
            label.config(text=text, fg="#1a7f37" if ok else "#c0392b")

        # ── 账号区 ───────────────────────────────────────────────
        tk.Label(root, text="① 用哪个 GitHub 账号", anchor="w",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3,
                                                     sticky="w", **pad)
        names = [n for n, _ in store.accounts()]
        var_account = tk.StringVar(value=cur_login or (names[0] if names else ""))
        cb_account = ttk.Combobox(root, textvariable=var_account, values=names,
                                  width=24, state="readonly")
        cb_account.grid(row=1, column=0, sticky="we", **pad)

        def new_account():
            var_account.set("")
            cb_account.config(state="normal")
            entry_token.delete(0, "end")
            entry_token.focus_set()
            status(lbl_conn, "粘贴新账号的令牌 → 点「测试并保存账号」", ok=True)

        def drop_account():
            who = var_account.get().strip()
            if not who:
                return
            if not messagebox.askyesno("移除账号", f"从本机移除「{who}」的令牌？（不影响 GitHub 上的仓库）"):
                return
            store.remove_account(who)
            left = [n for n, _ in store.accounts()]
            var_account.set(left[0] if left else "")
            cb_account.config(values=left, state="readonly" if left else "normal")
            status(lbl_conn, f"已移除 {who}", ok=True)

        box_acc = tk.Frame(root)
        box_acc.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        tk.Button(box_acc, text="新增账号", width=9, command=new_account).pack(side="left", padx=3)
        tk.Button(box_acc, text="移除", width=6, command=drop_account).pack(side="left", padx=3)

        tk.Label(root, text="令牌（classic，勾 repo）", anchor="w").grid(
            row=2, column=0, sticky="w", **pad)
        var_token = tk.StringVar(value=store.token_for(cur_login))
        entry_token = tk.Entry(root, textvariable=var_token, width=52, show="•")
        entry_token.grid(row=2, column=1, columnspan=2, sticky="we", **pad)

        lbl_conn = tk.Label(root, text="", anchor="w", fg="#666", width=64, justify="left")
        lbl_conn.grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        ttk.Separator(root, orient="horizontal").grid(row=4, column=0, columnspan=3,
                                                      sticky="we", padx=12, pady=6)

        # ── 仓库区 ───────────────────────────────────────────────
        tk.Label(root, text="② 往哪个仓库同步（必须是私有库）", anchor="w",
                 font=("Segoe UI", 10, "bold")).grid(row=5, column=0, columnspan=3,
                                                     sticky="w", **pad)
        tk.Label(root, text="所有者（留空 = 当前账号）", anchor="w").grid(
            row=6, column=0, sticky="w", **pad)
        tk.Label(root, text="仓库名", anchor="w").grid(row=6, column=1, sticky="w", **pad)

        var_owner = tk.StringVar(value=str(cfg.get("owner") or ""))
        var_repo = tk.StringVar(value=str(cfg.get("repoName") or ""))
        tk.Entry(root, textvariable=var_owner, width=24).grid(row=7, column=0, sticky="we", **pad)
        tk.Entry(root, textvariable=var_repo, width=26).grid(row=7, column=1, sticky="we", **pad)

        recent = [f"{(r.get('owner') or '自己')}/{r.get('name')}" for r in cfg.get("recentRepos") or []]
        tk.Label(root, text="最近的库", anchor="w").grid(row=8, column=0, sticky="w", **pad)
        var_recent = tk.StringVar(value="")
        cb_recent = ttk.Combobox(root, textvariable=var_recent, values=recent,
                                 width=24, state="readonly")
        cb_recent.grid(row=8, column=1, sticky="we", **pad)

        def pick_recent(_evt=None):
            v = var_recent.get()
            if "/" not in v:
                return
            o, n = v.split("/", 1)
            var_owner.set("" if o == "自己" else o)
            var_repo.set(n)
            status(lbl_repo, f"已选 {v}，点「检查」确认能用", ok=True)

        cb_recent.bind("<<ComboboxSelected>>", pick_recent)

        lbl_repo = tk.Label(root, text="", anchor="w", fg="#666", width=64, justify="left")
        lbl_repo.grid(row=10, column=0, columnspan=3, sticky="w", **pad)

        def collect():
            unknown = [] if var_unknown.get() else ["unknown"]
            try:
                max_bytes = max(8, int(float(var_maxmb.get()))) * 1024 * 1024
            except Exception:
                max_bytes = 400 * 1024 * 1024
            try:
                days = max(0, int(float(var_days.get())))
            except Exception:
                days = 7
            return {
                "owner": (var_owner.get() or "").strip(),
                "repoName": (var_repo.get() or "").strip(),
                "sync": {"excludeCopyright": unknown, "maxFamilyBytes": max_bytes,
                         "deleteAfterDays": days},
            }

        def current_login():
            return var_account.get().strip() or store.current_account()[0]

        def test_and_save_account():
            token = (var_token.get() or "").strip()
            if not token:
                status(lbl_conn, "先把令牌粘贴进来（GitHub → Settings → Developer settings → Tokens (classic)）", ok=False)
                return

            def work():
                try:
                    client = ghmod.Gh(token)
                    me = client.whoami()
                    login = me.get("login") or ""
                    ok_saved = store.upsert_account(login, token, make_current=True)
                    scopes = client.token_scopes()
                    msg = f"已连接 {login}" + (f"（scopes: {','.join(scopes)}）" if scopes else "（fine-grained 或未返回 scopes）")
                    if not ok_saved:
                        msg += " · ⚠️ DPAPI 保存失败，令牌只在本次运行有效"
                    root.after(0, lambda: cb_account.config(
                        values=[n for n, _ in store.accounts()], state="readonly"))
                    root.after(0, lambda: var_account.set(login))
                    root.after(0, lambda: status(lbl_conn, msg, ok=ok_saved))
                    log(f"账号已保存：{login}；{msg}")
                except Exception as e:
                    root.after(0, lambda: status(lbl_conn, f"连接失败：{e}", ok=False))
                    log(f"令牌测试失败：{e}")
            run_async(work)

        def check():
            owner = (var_owner.get() or "").strip()
            name = (var_repo.get() or "").strip()

            def work():
                try:
                    client = gh_client(current_login())
                    res = syncer.check_repo(client, owner or current_login(), name)
                    root.after(0, lambda: status(lbl_repo, res["message"], ok=res["ok"]))
                    log("仓库检查：" + res["message"].replace("**", ""))
                except Exception as e:
                    root.after(0, lambda: status(lbl_repo, f"检查失败：{e}", ok=False))
            run_async(work)

        def make_repo():
            name = (var_repo.get() or "").strip()
            if not name:
                status(lbl_repo, "先填一个仓库名", ok=False)
                return

            def work():
                try:
                    client = gh_client(current_login())
                    owner = (var_owner.get() or "").strip() or current_login()
                    client.create_private_repo(name, "FontManager 字体库同步（私有，勿设为公开）")
                    res = syncer.check_repo(client, owner, name)
                    root.after(0, lambda: status(lbl_repo, "已建私有仓：" + res["message"].replace("**", ""), ok=True))
                    store.remember_repo(owner, name)
                    log(f"已创建私有仓 {owner}/{name}")
                except Exception as e:
                    root.after(0, lambda: status(lbl_repo, f"建仓失败：{e}", ok=False))
            run_async(work)

        box_repo = tk.Frame(root)
        box_repo.grid(row=9, column=0, columnspan=2, sticky="w", **pad)
        tk.Button(box_repo, text="检查这个库", width=11, command=check).pack(side="left", padx=3)
        tk.Button(box_repo, text="新建私有仓", width=11, command=make_repo).pack(side="left", padx=3)

        ttk.Separator(root, orient="horizontal").grid(row=11, column=0, columnspan=3,
                                                      sticky="we", padx=12, pady=6)

        # ── 同步规则 ─────────────────────────────────────────────
        var_unknown = tk.BooleanVar(
            value="unknown" not in {str(x).lower() for x in (sync_cfg.get("excludeCopyright") or [])})
        tk.Checkbutton(root, text="也同步「版权未知」的字体（默认不传，建议保持关闭）",
                       variable=var_unknown).grid(row=12, column=0, columnspan=3,
                                                  sticky="w", **pad)
        tk.Label(root, text="单族上限（MB）", anchor="w").grid(row=13, column=0, sticky="w", **pad)
        tk.Label(root, text="旧版本保留（天）", anchor="w").grid(row=13, column=1, sticky="w", **pad)
        var_maxmb = tk.StringVar(value=str(int(int(sync_cfg.get("maxFamilyBytes") or 0) / 1048576) or 400))
        var_days = tk.StringVar(value=str(sync_cfg.get("deleteAfterDays") or 7))
        tk.Entry(root, textvariable=var_maxmb, width=10).grid(row=14, column=0, sticky="w", **pad)
        tk.Entry(root, textvariable=var_days, width=10).grid(row=14, column=1, sticky="w", **pad)

        # ── 底部按钮 ─────────────────────────────────────────────
        def save(close=False):
            patch = collect()
            if not patch["repoName"]:
                status(lbl_repo, "仓库名不能空 —— 填一个（例如 fontmanager-library）", ok=False)
                return
            store.save_config(patch)
            store.set_current(current_login())
            store.remember_repo(patch["owner"] or current_login(), patch["repoName"])
            log("设置已保存：" + json.dumps(patch, ensure_ascii=False)
                + f"；当前账号 {current_login()}")
            toast("同步设置已保存")
            if close:
                root.destroy()
            else:
                status(lbl_repo, "已保存。", ok=True)

        def dry():
            store.save_config(collect())
            store.set_current(current_login())
            run_async(do_sync, None, True)

        row = 15
        tk.Button(root, text="保存并关闭", width=12, command=lambda: save(True)).grid(
            row=row, column=0, sticky="w", **pad)
        box_btn = tk.Frame(root)
        box_btn.grid(row=row, column=1, columnspan=2, sticky="e", **pad)
        tk.Button(box_btn, text="保存", width=7, command=save).pack(side="left", padx=4)
        tk.Button(box_btn, text="预演一次", width=9, command=dry).pack(side="left", padx=4)
        tk.Button(box_btn, text="关闭", width=7, command=root.destroy).pack(side="left")

        hint = ("首次使用：① 粘贴令牌 →「测试并保存账号」；② 填仓库名 →「检查这个库」"
                "（不存在会提示将自动新建）；③「保存并关闭」，然后右键字族同步。")
        if not store.configured():
            tk.Label(root, text=hint, fg="#8a6d3b", anchor="w", wraplength=560,
                     justify="left").grid(row=row + 1, column=0, columnspan=3, sticky="w", **pad)
        else:
            tk.Label(root, text=f"当前：{store.current_account()[0]} → "
                                f"{(cfg.get('owner') or store.current_account()[0])}/{cfg.get('repoName')}",
                     fg="#666", anchor="w").grid(row=row + 1, column=0, columnspan=3,
                                                 sticky="w", **pad)

        tk.Label(root, text="令牌用 Windows DPAPI 加密后存在插件目录的 secret.bin；"
                            "面板里显示的是已保存的那一份，可以直接覆盖。",
                 fg="#888", wraplength=560, justify="left").grid(
            row=row + 2, column=0, columnspan=3, sticky="w", **pad)

        root.mainloop()

    threading.Thread(target=run, daemon=True).start()


# ── 自检 ────────────────────────────────────────────────────────────────
def self_check():
    """启动时打一份「我现在到底能不能同步」的体检，比报错后猜快得多。"""
    try:
        health = local_api().get("/health")
        log(f"本地 API 正常：{health.get('tokenName')} 权限={health.get('permissions')}")
    except Exception as e:
        log(f"本地 API 自检失败：{e}")

    login, token = store.current_account()
    if login:
        log(f"账号：{login}（令牌 {store.mask(token)}）"
            + ("" if store.vault_persisted() else "，⚠️ 未落盘（DPAPI 不可用）"))
    else:
        log("账号：还没设置（设置 → 插件 → 齿轮 → 粘贴 classic token）")

    cfg = store.load_config()
    owner = cfg.get("owner") or login or "(当前账号)"
    repo = cfg.get("repoName") or ""
    if repo:
        log(f"目标仓库：{owner}/{repo}")
    else:
        log("目标仓库：还没填 —— 插件在设置好之前不会同步任何内容")

    state = store.load_state()
    log(f"设备号：{state['deviceId']}；已同步 {len(state['families'])} 个族；"
        f"待删旧 asset {len(state['pendingDeletes'])} 个")
    if not store.configured():
        log("提示：首次使用请在 设置 → 插件 → 本插件齿轮 里完成「账号 + 仓库」两步")
    return {"configured": store.configured(), "login": login}


# ── 钩子分派 ────────────────────────────────────────────────────────────
def handle_invoke(hook, payload):
    payload = payload or {}
    if hook == "settings":
        open_settings_panel()
        return
    if hook != "context-menu":
        log(f"未处理的钩子：{hook}")
        return
    menu = payload.get("menu") or ""
    family = payload.get("family") or ""

    if menu.startswith(MENU_SETTINGS) or menu.startswith("同步设置"):
        open_settings_panel()
        return
    if menu.startswith(MENU_OPEN):
        try:
            os.startfile(store.state_dir())
            log(f"已打开同步状态目录：{store.state_dir()}")
        except Exception as e:
            toast(f"打不开：{e}")
        return
    if menu.startswith(MENU_DRYRUN):
        run_async(do_sync, None, True)
        return
    if menu.startswith(MENU_ALL):
        run_async(do_sync)
        return
    if menu.startswith(MENU_FAMILY):
        if not family:
            log("这个菜单需要在某个字族上右键")
            return
        run_async(do_sync, family, False)
        return
    log(f"未知菜单项：{menu}")


def main():
    global API_BASE, API_TOKEN
    log("字体库同步插件已就绪（首次使用先在设置里配好 GitHub 账号与库）")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            log(f"看不懂的消息：{line[:120]}")
            continue
        kind = msg.get("type")
        if kind == "init":
            API_BASE = msg.get("apiBase") or ""
            API_TOKEN = msg.get("apiToken") or ""
            log(f"已握手：apiLevel={msg.get('apiLevel')} apiBase={API_BASE}")
            try:
                self_check()
            except Exception:
                log("自检异常：" + traceback.format_exc(limit=3).replace("\n", " | "))
        elif kind == "invoke":
            try:
                handle_invoke(msg.get("hook"), msg.get("payload"))
            except Exception:
                log("处理调用异常：" + traceback.format_exc(limit=3).replace("\n", " | "))
        else:
            log(f"未知消息类型：{kind}")


if __name__ == "__main__":
    main()
