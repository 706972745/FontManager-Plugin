#!/usr/bin/env python3
"""字体库同步（GitHub）—— 把字体库按字族打包，增量同步到自己的私有仓库。

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
MENU_SETTINGS = "同步设置…"
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


def gh_client():
    token = store.load_token()
    if not token:
        raise RuntimeError("还没填 GitHub 令牌：设置 → 插件 → 本插件右侧齿轮")
    return ghmod.Gh(token)


# ── 同步任务 ────────────────────────────────────────────────────────────
def do_sync(only_family=None, dry_run=False):
    if not SYNC_LOCK.acquire(blocking=False):
        toast("已经在同步了，等这一轮结束")
        return
    try:
        cfg = store.load_config()
        state = store.load_state()
        api = local_api()
        client = gh_client()
        scope = only_family or "全部字族"
        toast(("预演：" if dry_run else "开始同步：") + scope)
        log(f"=== 同步开始（{scope}{'，预演' if dry_run else ''}）===")
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
    """有 tkinter 就画面板，没有就退化成「改 config.json」的提示。

    注意：**不能在主线程跑 mainloop**，否则面板开着时右键菜单进不来。
    """
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        toast(f"这个 Python 没有 tkinter，请直接改 {store.CONFIG_PATH}")
        log(f"没有 tkinter，配置文件在：{store.CONFIG_PATH}")
        return

    cfg = store.load_config()
    sync_cfg = cfg.get("sync") or {}
    token_now = store.load_token()

    def run():
        root = tk.Tk()
        root.title("字体库同步 · 设置")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        pad = {"padx": 12, "pady": 4}
        row = 0

        tk.Label(root, text="GitHub 令牌（classic，勾 repo）", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        var_token = tk.StringVar(value=token_now)
        entry_token = tk.Entry(root, textvariable=var_token, width=52, show="•")
        entry_token.grid(row=row, column=0, columnspan=2, sticky="we", **pad)
        row += 1
        tk.Label(root, text=f"当前：{store.mask(token_now)}（DPAPI 加密存 secret.bin）",
                 fg="#888").grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="所有者（留空 = 令牌本人）", anchor="w").grid(
            row=row, column=0, sticky="w", **pad)
        tk.Label(root, text="仓库名", anchor="w").grid(row=row, column=1, sticky="w", **pad)
        row += 1
        var_owner = tk.StringVar(value=str(cfg.get("owner") or ""))
        var_repo = tk.StringVar(value=str(cfg.get("repoName") or "fontmanager-library"))
        tk.Entry(root, textvariable=var_owner, width=24).grid(row=row, column=0, sticky="we", **pad)
        tk.Entry(root, textvariable=var_repo, width=26).grid(row=row, column=1, sticky="we", **pad)
        row += 1

        var_unknown = tk.BooleanVar(
            value="unknown" not in {str(x).lower() for x in (sync_cfg.get("excludeCopyright") or [])})
        tk.Checkbutton(root, text="也同步「版权未知」的字体（默认不传，建议保持关闭）",
                       variable=var_unknown).grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="单族上限（MB）", anchor="w").grid(row=row, column=0, sticky="w", **pad)
        tk.Label(root, text="旧版本保留（天）", anchor="w").grid(row=row, column=1, sticky="w", **pad)
        row += 1
        var_maxmb = tk.StringVar(value=str(int(int(sync_cfg.get("maxFamilyBytes") or 0) / 1048576) or 400))
        var_days = tk.StringVar(value=str(sync_cfg.get("deleteAfterDays") or 7))
        tk.Entry(root, textvariable=var_maxmb, width=10).grid(row=row, column=0, sticky="w", **pad)
        tk.Entry(root, textvariable=var_days, width=10).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        status = tk.Label(root, text="", fg="#666", anchor="w", width=60, justify="left")
        status.grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1

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
                "repoName": (var_repo.get() or "").strip() or "fontmanager-library",
                "sync": {"excludeCopyright": unknown, "maxFamilyBytes": max_bytes,
                         "deleteAfterDays": days},
            }

        def save(close=False):
            patch = collect()
            token = (var_token.get() or "").strip()
            if token and token != token_now:
                if not store.save_token(token):
                    messagebox.showwarning("令牌没存下来",
                                           "DPAPI 加密失败，令牌只在本次运行有效。\n"
                                           f"（文件：{store.SECRET_PATH}）")
                patch["tokenSavedAt"] = int(__import__("time").time())
            store.save_config(patch)
            log("设置已保存：" + json.dumps(patch, ensure_ascii=False))
            toast("同步设置已保存")
            if close:
                root.destroy()
            else:
                status.config(text="已保存。", fg="#2a7")

        def test():
            def work():
                try:
                    store.save_config(collect())
                    client = gh_client()
                    me = client.whoami()
                    status.config(text=f"令牌有效：{me.get('login')}", fg="#2a7")
                    log(f"令牌自检通过：{me.get('login')}（scopes={client.token_scopes()}）")
                except Exception as e:
                    status.config(text=f"失败：{e}", fg="#c33")
                    log(f"令牌自检失败：{e}")
            run_async(work)

        def dry():
            store.save_config(collect())
            run_async(do_sync, None, True)

        tk.Button(root, text="保存", width=10, command=lambda: save(True)).grid(
            row=row, column=0, sticky="w", **pad)
        box = tk.Frame(root)
        box.grid(row=row, column=1, sticky="e", **pad)
        tk.Button(box, text="测试连接", width=9, command=test).pack(side="left", padx=4)
        tk.Button(box, text="预演一次", width=9, command=dry).pack(side="left", padx=4)
        tk.Button(box, text="关闭", width=7, command=root.destroy).pack(side="left")

        tk.Label(root, text="预演只列计划、不传任何东西 —— 首次全量前建议先跑一次。",
                 fg="#888").grid(row=row + 1, column=0, columnspan=2, sticky="w", **pad)
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()


# ── 自检 ────────────────────────────────────────────────────────────────
def self_check():
    """启动时打一份「我现在到底能不能同步」的体检，比报错后猜快得多。"""
    try:
        api = local_api()
        health = api.get("/health")
        log(f"本地 API 正常：{health.get('tokenName')} 权限={health.get('permissions')}")
    except Exception as e:
        log(f"本地 API 自检失败：{e}")
    token = store.load_token()
    log(f"令牌：{store.mask(token)}" + ("" if token else "（去设置面板填一个 classic token）"))
    cfg = store.load_config()
    log(f"目标仓库：{(cfg.get('owner') or '') or '(本人)'}/{cfg.get('repoName')}")
    state = store.load_state()
    log(f"设备号：{state['deviceId']}；已同步 {len(state['families'])} 个族；"
        f"待删旧 asset {len(state['pendingDeletes'])} 个")
    return {"token": bool(token), "config": cfg, "state": state}


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

    if menu.startswith(MENU_SETTINGS):
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
    log("字体库同步插件已就绪（右键字族同步单个族，或在插件行右键同步全部）")
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
