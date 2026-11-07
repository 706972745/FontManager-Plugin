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
import time
import traceback

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import gh as ghmod  # noqa: E402
import panel  # noqa: E402
import store  # noqa: E402
import syncer  # noqa: E402

API_BASE = ""
API_TOKEN = ""
SYNC_LOCK = threading.Lock()

MENU_FAMILY = "同步这个字族到 GitHub"
MENU_ALL = "立即同步（全部）"
MENU_DRYRUN = "预演本次同步（不传）"
MENU_RESTORE = "从 GitHub 下载到这台电脑"
MENU_RESTORE_DRY = "预演下载（看会拉哪些）"
MENU_SETTINGS = "设置 GitHub 库…"
MENU_GUIDE = "使用教程与权限说明…"
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
        tab = "account"
    else:
        toast("先设置 GitHub 库：还差仓库名（这就打开设置）")
        log("未配置：账号有了，仓库名还空着")
        tab = "repo"
    if interactive:
        open_settings_panel(tab)
    return False


# ── 同步进度（给设置面板轮询） ──────────────────────────────────────────
# 面板和同步在同一个进程里，所以直接用内存字典即可。字段全是展示用的，
# 读的时候加锁拿一份副本，避免面板线程读到半更新的状态。
PROGRESS = {
    "phase": "idle",     # idle / scan / plan / connect / pack / upload / delete / done / error
    "scope": "", "dryRun": False,
    "done": 0, "total": 0, "index": 0, "family": "",
    "bytes": 0, "bytesTotal": 0,
    "message": "", "error": "",
    "startedAt": 0, "updatedAt": 0, "finishedAt": 0,
}
_PROGRESS_LOCK = threading.Lock()


def _reset_progress(dry_run, scope):
    now = int(time.time() * 1000)
    with _PROGRESS_LOCK:
        PROGRESS.update({"phase": "starting", "scope": scope, "dryRun": bool(dry_run),
                         "done": 0, "total": 0, "index": 0, "family": "",
                         "bytes": 0, "bytesTotal": 0, "message": "", "error": "",
                         "startedAt": now, "updatedAt": now, "finishedAt": 0})


def _on_progress(phase, **fields):
    now = int(time.time() * 1000)
    with _PROGRESS_LOCK:
        PROGRESS["phase"] = phase
        for k, v in fields.items():
            if k in PROGRESS and k not in ("startedAt", "finishedAt"):
                PROGRESS[k] = v
        PROGRESS["updatedAt"] = now
        if phase in ("done", "error"):
            PROGRESS["finishedAt"] = now


def progress_snapshot():
    with _PROGRESS_LOCK:
        snap = dict(PROGRESS)
    snap["running"] = snap["phase"] not in ("idle", "done", "error")
    return snap


# ── 同步任务 ────────────────────────────────────────────────────────────
def do_sync(only_family=None, dry_run=False):
    if not require_configured():
        return
    if not SYNC_LOCK.acquire(blocking=False):
        toast("已经在同步了，等这一轮结束")
        return
    scope = only_family or "全部字族"
    _reset_progress(dry_run, scope)
    try:
        cfg = store.load_config()
        state = store.load_state()
        api = local_api()
        client = gh_client()
        login = store.current_account()[0]
        target = f"{(cfg.get('owner') or login)}/{cfg.get('repoName')}"
        toast(("预演：" if dry_run else "开始同步：") + f"{scope} → {target}")
        log(f"=== 同步开始（{scope}{'，预演' if dry_run else ''}），账号 {login}，目标 {target} ===")
        summary = syncer.run_sync(client, api, cfg, state, only_family=only_family,
                                  dry_run=dry_run, log=log, progress=_on_progress)
        if dry_run:
            toast(f"预演完成：{summary['planned']} 个族待传，未上传任何内容")
        else:
            msg = f"同步完成：上传 {summary['uploaded']} 个族（{summary['bytes']/1048576:.1f} MB）"
            if summary.get("removed"):
                msg += f"，删除远端 {len(summary['removed'])} 个族"
            if summary["failed"]:
                msg += f"，{len(summary['failed'])} 个失败"
            toast(msg)
    except Exception as e:
        log("同步失败：" + traceback.format_exc(limit=4).replace("\n", " | "))
        toast(f"同步失败：{e}")
        _on_progress("error", message=str(e), error=str(e))
    finally:
        # 兜底：任何异常路径都要让面板看到「结束了」，否则进度条会一直转
        with _PROGRESS_LOCK:
            if PROGRESS["phase"] not in ("done", "error", "idle"):
                PROGRESS["phase"] = "error"
                PROGRESS["message"] = PROGRESS["message"] or "同步提前结束"
                PROGRESS["finishedAt"] = int(time.time() * 1000)
        SYNC_LOCK.release()


def run_async(func, *args, **kwargs):
    threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()


# ── 恢复任务（从 GitHub 下载回本机） ────────────────────────────────────
def do_restore(only_family=None, dry_run=False):
    if not require_configured():
        return
    if not SYNC_LOCK.acquire(blocking=False):
        toast("已经在同步 / 恢复了，等这一轮结束")
        return
    scope = only_family or "全部字族"
    _reset_progress(dry_run, "恢复 " + scope)
    try:
        cfg = store.load_config()
        state = store.load_state()
        api = local_api()
        client = gh_client()
        login = store.current_account()[0]
        target = f"{(cfg.get('owner') or login)}/{cfg.get('repoName')}"
        toast(("预演下载：" if dry_run else "开始恢复：") + f"{target} → 本机 {scope}")
        log(f"=== 恢复开始（{scope}{'，预演' if dry_run else ''}），账号 {login}，来源 {target} ===")
        summary = syncer.run_restore(
            client, api, cfg, state, dry_run=dry_run,
            overwrite=bool(cfg.get("overwriteExisting")),
            only=only_family, log=log, progress=_on_progress)
        if dry_run:
            toast(f"预演完成：远端有 {summary['planned']} 个族可以拉到本机")
        else:
            msg = f"恢复完成：下载 {summary['downloaded']} 个族（{summary['bytes']/1048576:.1f} MB）"
            if summary["failed"]:
                msg += f"，{len(summary['failed'])} 个失败"
            if summary["conflicts"]:
                msg += f"，{len(summary['conflicts'])} 个文件冲突未覆盖"
            toast(msg)
    except Exception as e:
        log("恢复失败：" + traceback.format_exc(limit=4).replace("\n", " | "))
        toast(f"恢复失败：{e}")
        _on_progress("error", message=str(e), error=str(e))
    finally:
        with _PROGRESS_LOCK:
            if PROGRESS["phase"] not in ("done", "error", "idle"):
                PROGRESS["phase"] = "error"
                PROGRESS["message"] = PROGRESS["message"] or "恢复提前结束"
                PROGRESS["finishedAt"] = int(time.time() * 1000)
        SYNC_LOCK.release()


# ── 设置面板 ────────────────────────────────────────────────────────────
def open_settings_panel(tab="account"):
    """在浏览器里打开设置面板（账号 / 仓库 / 权限说明 / 教程）。

    为什么不用 tkinter：插件自带的是 Python embeddable 发行版，**没有 tkinter**，
    装没装 Python 也不该影响设置。浏览器面板零依赖，还能放权限表和可点击的链接。
    """
    actions = {
        "dry": lambda: run_async(do_sync, None, True),
        "sync": lambda: run_async(do_sync),
        "restoreDry": lambda: run_async(do_restore, None, True),
        "restore": lambda: run_async(do_restore),
    }
    try:
        url = panel.open_panel(actions, tab=tab, progress=progress_snapshot)
        log(f"设置面板：{url}")
    except Exception as e:
        toast(f"打不开设置面板：{e}")
        log("设置面板启动失败：" + traceback.format_exc(limit=3).replace("\n", " | "))
        log(f"可以手动改 {store.CONFIG_PATH}")


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
        log("账号：还没设置 —— 右键本插件 →「设置 GitHub 库…」，或用「使用教程与权限说明…」先看步骤")

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
        # 首次使用：只在真的动手时才弹面板，启动时给一句提示就够了
        toast("还没设置 GitHub 库：右键本插件 →「设置 GitHub 库…」（有权限说明和教程）")
        log("提示：设置面板在浏览器里打开；两步就够 —— 粘令牌、选一个私有库")
    return {"configured": store.configured(), "login": login}


# ── 钩子分派 ────────────────────────────────────────────────────────────
def handle_invoke(hook, payload):
    payload = payload or {}
    if hook == "settings":
        open_settings_panel("account")
        return
    if hook != "context-menu":
        log(f"未处理的钩子：{hook}")
        return
    menu = payload.get("menu") or ""
    family = payload.get("family") or ""

    if menu.startswith(MENU_GUIDE):
        open_settings_panel("guide")
        return
    if menu.startswith(MENU_SETTINGS) or menu.startswith("同步设置"):
        open_settings_panel("account")
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
    if menu.startswith(MENU_RESTORE_DRY):
        run_async(do_restore, None, True)
        return
    if menu.startswith(MENU_RESTORE):
        run_async(do_restore)
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
