#!/usr/bin/env python3
"""字体库同步（GitHub）—— 把字体库按字族打包，增量同步到自己的私有仓库。

每个用户用**自己的** GitHub 账号和仓库：插件支持保存多个账号（各自的令牌），
在设置面板里切换「用哪个账号、往哪个库同步」。没配置好之前**不会**同步任何东西。

为什么是「按字族」而不是「整库一份大包」：
改一个 60 KB 的英文字体，不该重传 520 MB。所以每个字族一个 zip，
内容没变就整族跳过；变了才重新打包，打包后哈希一样还不上传。

安全边界（四条硬闸，任一不满足就中止）：
1. 仓库必须**私有**且在 GitHub 上真私有（`private` 字段 + 匿名探一次）；
2. 令牌存 **Windows 凭据管理器**（回退时存 %APPDATA% 下 DPAPI 加盐密文），
   **插件目录里没有任何令牌文件**，插件 ZIP 被拷走也带不走令牌；
3. 上传与复制令牌前，必须在浏览器登录「令牌所属」的 GitHub 账号完成验证
   （Device Flow，临时 access_token 用完即弃）；把字体下载回本机不需要验证；
4. 判不清授权的族（`copyrightClass = unknown`）**默认不同步**，用户可在设置里放开。

通信协议与其它插件一致：stdin 收 JSON 行，stdout 发 JSON 行。
"""

import json
import os
import shutil
import sys
import threading
import time
import traceback

# ── 字节码缓存自清（在导入任何本地模块之前执行）──
# 更新插件时 .py 的修改时间可能被保留成旧值，Python 会误用 __pycache__ 里
# 旧版本的字节码 —— 表现就是「文件是新的、跑的是旧的」（面板出现 unknown
# endpoint、进度不动，且重装插件也无法恢复）。每次启动先清掉，永绝后患。
try:
    _pc = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__pycache__")
    if os.path.isdir(_pc):
        shutil.rmtree(_pc, ignore_errors=True)
except Exception:
    pass

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import gh as ghmod  # noqa: E402
import panel  # noqa: E402
import store  # noqa: E402
import syncer  # noqa: E402
import verify  # noqa: E402

API_BASE = ""
API_TOKEN = ""
SYNC_LOCK = threading.Lock()

MENU_FAMILY = "重新同步这个字族（强制重传）"
# 老标题：主程序若缓存了上一版菜单（插件刚更新、窗口没重开），点下来还是这句。
# 认它，别让用户点了没反应。
MENU_FAMILY_OLD = "同步这个字族到 GitHub"
MENU_RESTORE_FAMILY = "从 GitHub 下载这个字族"
MENU_ALL = "立即同步（全部）"
MENU_DRYRUN = "预演本次同步（不传）"
MENU_RESTORE = "从 GitHub 下载到这台电脑"
MENU_RESTORE_DRY = "预演下载（看会拉哪些）"
MENU_SETTINGS = "设置 GitHub 库…"
MENU_GUIDE = "使用教程与权限说明…"
MENU_OPEN = "打开同步状态目录"


# ── 协议 ────────────────────────────────────────────────────────────────
def emit(kind, message):
    with _STDOUT_LOCK:
        sys.stdout.write(json.dumps({"type": kind, "message": message}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def log(message):
    emit("log", message)


def toast(message):
    emit("toast", message)


def open_external(url):
    """请主程序在系统默认浏览器打开网页（面板开在应用内窗口后，webview 点不动外链）。
    主程序 1.4.5+ 支持；更早版本只会记一条日志，页面自行 window.open 兜底。"""
    url = str(url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        toast("拒绝打开非 http(s) 链接：" + url[:80])
        return
    sys.stdout.write(json.dumps({"type": "open-url", "url": url}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


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
_STDOUT_LOCK = threading.Lock()


def _mb(n):
    return f"{(n or 0) / 1048576:.1f} MB"


def _progress_plain_text(snap):
    """主页面底部进度条的文字（纯文本，不要 HTML）。"""
    phase = snap.get("phase") or ""
    total = snap.get("total") or 0
    done = snap.get("done") or 0
    family = snap.get("family") or ""
    if phase == "starting":
        return "正在启动…"
    if phase == "scan":
        return "正在读取字体清单、逐个算指纹…"
    if phase == "plan":
        return f"计划 {total} 个族 / {_mb(snap.get('bytesTotal'))}，开始处理…"
    if phase == "connect":
        return "正在核对目标仓库（私有性、匿名可读）…"
    if phase == "pack":
        return f"打包 {done}/{total}：{family}"
    if phase == "upload":
        return f"上传 {done}/{total}（{_mb(snap.get('bytes'))} / {_mb(snap.get('bytesTotal'))}）"
    if phase == "delete":
        return f"删除远端已删字族 {done}/{total}：{family}"
    if phase == "restore-scan":
        return "正在读取本机字体清单…"
    if phase == "restore-plan":
        return snap.get("message") or "正在比对远端与本机…"
    if phase == "download":
        return f"下载 {done}/{total}（{_mb(snap.get('bytes'))} / {_mb(snap.get('bytesTotal'))}）：{family}"
    if phase == "unpack":
        return f"写入本机 {done}/{total}：{family}"
    if phase == "rescan":
        return "让 FontManager 重新扫描字体库…"
    if phase == "failed":
        return f"失败跳过 {family}：{snap.get('message') or ''}（{done}/{total}）"
    if phase == "done":
        return snap.get("message") or "完成"
    if phase == "error":
        return f"失败：{snap.get('error') or snap.get('message') or '未知错误'}"
    return ""


def _emit_progress_line():
    """把当前进度发一条给主程序（1.4.8+ 会转到主页面底部进度条；
    老主程序忽略未知消息类型，无副作用）。"""
    with _PROGRESS_LOCK:
        snap = dict(PROGRESS)
    running = snap["phase"] not in ("idle", "done", "error")
    if snap["phase"] == "idle":
        return
    total = snap.get("total") or 0
    percent = None
    if not running:
        percent = 100
    elif total:
        percent = max(0, min(100, round(done_of(snap) / total * 100)))
    line = {"type": "progress",
            "text": _progress_plain_text(snap),
            "percent": percent,
            "running": running}
    try:
        with _STDOUT_LOCK:
            sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    except Exception:
        pass  # 主程序不在了也别让进度上报弄死任务


def done_of(snap):
    return snap.get("done") or 0


def _reset_progress(dry_run, scope):
    now = int(time.time() * 1000)
    with _PROGRESS_LOCK:
        PROGRESS.update({"phase": "starting", "scope": scope, "dryRun": bool(dry_run),
                         "done": 0, "total": 0, "index": 0, "family": "",
                         "bytes": 0, "bytesTotal": 0, "message": "", "error": "",
                         "startedAt": now, "updatedAt": now, "finishedAt": 0})
    _emit_progress_line()


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
    _emit_progress_line()


def progress_snapshot():
    with _PROGRESS_LOCK:
        snap = dict(PROGRESS)
    snap["running"] = snap["phase"] not in ("idle", "done", "error")
    return snap


# ── 同步任务 ────────────────────────────────────────────────────────────
def do_sync(only_family=None, dry_run=False):
    if not require_configured():
        return
    # 上传是敏感操作：面板会先带用户完成 GitHub 登录验证（票据留在本进程内存），
    # 插件菜单直接触发的同步若没有票据就拒绝。预演不上传、下载 do_restore
    # 按用户要求一律不检查。验证与面板服务在同一进程，票据可共享。
    if not dry_run and not verify.valid(store.current_account()[0]):
        toast("上传前请先在设置面板完成 GitHub 账号登录验证（打开设置 → 同步）")
        log("同步被拒绝：没有有效的 GitHub 登录验证")
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
            # 带上分母与跳过原因：只报「263 个族」会被当成「插件只认了 263 个族」
            toast(summary.get("preview")
                  or f"预演完成：{summary['planned']} 个族待传，未上传任何内容")
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
    """后台跑任务。try 包住整个目标 —— try 之前的代码（比如 0.8.5 的 scope
    切片崩在 _reset_progress 之前）异常再也不能无声带走线程，必须落进
    PROGRESS 让面板看到明确的「失败」。"""
    def runner():
        try:
            func(*args, **kwargs)
        except Exception as e:
            with _PROGRESS_LOCK:
                if PROGRESS.get("phase") not in ("done", "error"):
                    now = int(time.time() * 1000)
                    PROGRESS.update({"phase": "error", "message": str(e),
                                     "error": str(e), "finishedAt": now,
                                     "updatedAt": now})
            log("任务异常：" + traceback.format_exc(limit=4).replace("\n", " | "))
            toast(f"任务失败：{e}")
    threading.Thread(target=runner, daemon=True).start()


# ── 恢复任务（从 GitHub 下载回本机） ────────────────────────────────────
def do_restore(only_family=None, dry_run=False):
    """恢复：单个族（右键菜单）、多个族（面板「补缺」）或全部（None）。"""
    if isinstance(only_family, str):        # 右键字族菜单传单个族名
        only = [only_family]
    elif isinstance(only_family, (list, tuple)):
        only = list(only_family)
    else:
        only = only_family
    if not require_configured():
        return
    if not SYNC_LOCK.acquire(blocking=False):
        toast("已经在同步 / 恢复了，等这一轮结束")
        return
    # 判空必须在切片之前 —— 0.8.5 真机翻过车：面板全量恢复传 None，
    # `only[:3]` 直接 TypeError，任务线程无声死掉，进度永远 idle。
    if not only:
        scope = "全部字族"
    else:
        scope = ("、".join(only[:3]) + (f" 等 {len(only)} 个族" if len(only) > 3 else ""))
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
            only=only, log=log, progress=_on_progress)
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
def dispatch_action(action, payload=None):
    """面板的通用动作入口：面板进程里没有 main 的函数，只认识动作名。

    新的面板动作在这里加分支，别往 panel.py 里塞业务逻辑 —— 那边只管 HTTP 与页面。
    """
    payload = payload or {}
    if action == "restoreMissing":
        families = [str(f).strip() for f in (payload.get("families") or []) if str(f).strip()]
        if not families:
            toast("没有勾选任何要下载的字族")
            return {"ok": False, "error": "没有勾选任何要下载的字族"}
        run_async(do_restore, families, False)
        return {"ok": True, "message": f"开始恢复 {len(families)} 个族，进度看下方进度条"}
    return {"ok": False, "error": f"未知动作：{action}"}


def emit_open_url(url, title):
    """请主程序开一个应用内窗口加载 url（1.4.3+ 的 open-url 消息）。"""
    sys.stdout.write(json.dumps({"type": "open-url", "url": url, "title": title},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _main_supports_panel_window(api):
    """主程序 ≥1.4.3 才会处理 open-url；老版本问都不问，直接走本地窗口。"""
    try:
        v = str((api.get("/health") or {}).get("version") or "")
        parts = tuple(int(x) for x in v.split(".")[:3])
        return parts >= (1, 4, 3)
    except Exception:
        return False


def open_settings_panel(tab="account"):
    """在窗口里打开设置面板（账号 / 仓库 / 权限说明 / 教程）。

    开窗方式按主程序能力自动降级，任何一级失败都有下一级兜底：
    1. 主程序 ≥1.4.3 → 发 open-url，由主程序开**应用内窗口**；
    2. 否则 → Edge `--app` 无边框独立窗口（装了 WebView2 的机器必有 Edge）；
    3. 再不行 → 系统默认浏览器。
    """
    actions = {
        "dry": lambda: run_async(do_sync, None, True),
        "sync": lambda: run_async(do_sync),
        "restoreDry": lambda: run_async(do_restore, None, True),
        "restore": lambda: run_async(do_restore),
        # 面板「补缺」要用的本地 API 客户端（读快照 + 字体清单）与通用动作分发
        "localApi": local_api,
        "openExternal": open_external,
        "dispatch": dispatch_action,
    }
    try:
        url = panel.open_panel(actions, tab=tab, progress=progress_snapshot,
                               auto_open=False)
        if _main_supports_panel_window(local_api()):
            emit_open_url(url, "字体库同步 · 设置")
            log(f"已请求主程序打开设置窗口：{url}")
            return
        p = panel.current_panel()
        if p:
            p.open(tab)
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
    all_fams = set(state.get("families") or {})
    for led in (state.get("libraries") or {}).values():
        if isinstance(led, dict):
            all_fams.update(led.get("families") or {})
    log(f"设备号：{state['deviceId']}；账本记录 {len(all_fams)} 个族；"
        f"待删旧 asset {len(state.get('pendingDeletes') or [])} 个")
    if not store.configured():
        # 首次使用：只在真的动手时才弹面板，启动时给一句提示就够了
        toast("还没设置 GitHub 库：右键本插件 →「设置 GitHub 库…」（有权限说明和教程）")
        log("提示：设置面板在浏览器里打开；两步就够 —— 粘令牌、选一个私有库")
    return {"configured": store.configured(), "login": login}


# ── 钩子分派 ────────────────────────────────────────────────────────────
def start_task_with_progress(fn, args=(), dry_run=False, scope=""):
    """右键菜单触发的同步/下载：任务照常在后台跑，进度通过 progress 消息
    显示在**主程序主页面底部**的进度条上（1.4.8+；老主程序忽略该消息，
    退回只有开始/结束弹窗提示的行为）。任务被 SYNC_LOCK 挡住（上一轮还在
    跑）时，3 秒后把停在 starting 的进度放回 idle，别在主页面挂一条假进度。"""
    _reset_progress(dry_run, scope or ("预演" if dry_run else "任务"))
    started_at = PROGRESS.get("startedAt")
    run_async(fn, *args)

    def guard():
        time.sleep(3)
        with _PROGRESS_LOCK:
            if PROGRESS.get("startedAt") == started_at and PROGRESS["phase"] == "starting":
                now = int(time.time() * 1000)
                PROGRESS.update({"phase": "idle", "message": "", "error": "",
                                 "finishedAt": now, "updatedAt": now})

    threading.Thread(target=guard, daemon=True).start()


def handle_invoke(hook, payload):
    payload = payload or {}
    if hook == "settings":
        open_settings_panel("account")
        return
    if hook == "snapshot-missing":
        # 主程序导入快照后发现「本机没有」的字体，把缺失字族广播过来（1.8.3 / 0.8.18）。
        # 这里**只开面板指路，不自动下载**：补哪些族、从哪个库补，得用户在面板里看一眼再点。
        # 静默拉几百个族既慢又可能拷来用户不想要的东西。
        fams = [f for f in (payload.get("families") or []) if isinstance(f, str) and f]
        missing = payload.get("missing")
        if not isinstance(missing, int) or missing <= 0:
            missing = len(fams)
        open_settings_panel("sync")
        if fams:
            toast(f"快照缺 {missing} 个字体（{len(fams)} 个族）：已打开「同步与恢复 → 补缺」")
            log(f"快照缺字体 hook：{missing} 条 / {len(fams)} 族，样例 {fams[:3]}")
        else:
            toast("已打开「同步与恢复 → 补缺」")
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
        start_task_with_progress(do_sync, (None, True), dry_run=True, scope="同步 预演")
        return
    if menu.startswith(MENU_RESTORE_DRY):
        start_task_with_progress(do_restore, (None, True), dry_run=True, scope="恢复 预演")
        return
    if menu.startswith(MENU_RESTORE):
        start_task_with_progress(do_restore, scope="恢复 全部字族")
        return
    if menu.startswith(MENU_ALL):
        start_task_with_progress(do_sync, scope="同步 全部字族")
        return
    if menu.startswith(MENU_FAMILY) or menu.startswith(MENU_FAMILY_OLD):
        if not family:
            log("这个菜单需要在某个字族上右键")
            return
        start_task_with_progress(do_sync, (family, False), scope="同步 " + family)
        return
    if menu.startswith(MENU_RESTORE_FAMILY):
        if not family:
            log("这个菜单需要在某个字族上右键")
            return
        start_task_with_progress(do_restore, (family, False), scope="恢复 " + family)
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
