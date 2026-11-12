#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字体套用插件：右键一个字族 / 在速览里双击 → 套用到 Photoshop / Illustrator / CorelDRAW。

它是**薄插件**，自己不做套用：套用由主程序执行（`POST /api/v1/fonts/apply`，1.9.11+），
本插件只负责挑一个可用的字面、认出目标软件、把结果翻成一句人话。

为什么不自己写 COM / JSX / PowerShell：主程序里那套引擎已经有启用检查、缺中文提醒、
CorelDRAW 常驻 COM 宿主、9 种 outcome 的处理。插件复制一份，等于主程序修好的问题
在插件里还在，两边的提示也会慢慢分叉。所以这个插件里**一行 COM / JSX / PowerShell 都没有**。

与主程序只有两条通道（与其他插件一致）：
1. stdin/stdout 上的 JSON 行：`{"type":"init",...}` 握手、`{"type":"invoke","hook":...}` 调用；
2. 本地 HTTP API（用 init 里给的受限令牌，权限就是 plugin.json 里申报的那三项）。

两种来源（1.1.0 起）：
- **右键菜单**：按菜单项语义走（「自动：…」= 最近在用的软件，「应用到 X」= X）。
- **主程序双击转发**：payload 里带 `faceId`，按 config.json 的 `doubleClickMode` 走 ——
  `auto` 直接套用最近在用的软件；`pick` 由本插件自己等用户点一下目标窗口（见 pick.py）。
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import config
import panel
import pick

API_BASE = ""
API_TOKEN = ""

# 目标软件的显示名。CorelDRAW 的官方写法是 CamelCase，提示文案里照抄，别写成 Coreldraw
APP_LABELS = {
    "photoshop": "Photoshop",
    "illustrator": "Illustrator",
    "coreldraw": "CorelDRAW",
}

# 菜单标题 → 目标。按**前缀**匹配：主程序回传的就是用户点的那一行原文。
# 「自动」放第一条，避免以后有人把标题改成「自动：应用到 Photoshop」时被后面那条抢走。
MENU_TARGETS = (
    ("自动", "auto"),
    ("应用到 Photoshop", "photoshop"),
    ("应用到 Illustrator", "illustrator"),
    ("应用到 CorelDRAW", "coreldraw"),
)

# 设置页那条 `on:"plugin"` 菜单（点它等同于点齿轮）
SETTINGS_MENU_PREFIX = "双击行为"

# pick 模式那条常驻提示（1.9.14 的 hint 通道），文案与时长都在这里定
PICK_HINT = "点一下要应用到的窗口 · 15 秒内"
PICK_HINT_SECONDS = 15

# 套用要起 PowerShell 并轮询目标软件：PS/AI 最多 30 秒、CorelDRAW 最多 60 秒。
# 客户端给 90 秒，比引擎的上限宽一点，免得「其实成功了但客户端先超时」。
APPLY_TIMEOUT = 90
DEFAULT_TIMEOUT = 15


class ApiError(Exception):
    """主程序返回了 4xx/5xx，或者网络层出错。message 已经是人话。"""


def _emit(kind, message):
    sys.stdout.write(
        json.dumps({"type": kind, "message": message}, ensure_ascii=False) + "\n"
    )
    sys.stdout.flush()


def log(message):
    _emit("log", message)


def toast(message):
    _emit("toast", message)


def emit_open_url(url, title):
    """请主程序开一个应用内窗口加载 url（1.4.3+）。"""
    sys.stdout.write(
        json.dumps({"type": "open-url", "url": url, "title": title}, ensure_ascii=False)
        + "\n"
    )
    sys.stdout.flush()


def emit_hint(text, seconds=15):
    """要一条**常驻**提示（主程序 1.9.14+，速览元信息行渲染）。

    `text` 为空串 = 立刻清除。老版本主程序不认这个消息，只会忽略 ——
    所以调用方该给的 toast 一个都不能省（提示是加分项，不是唯一通道）。
    """
    sys.stdout.write(
        json.dumps({"type": "hint", "text": text, "seconds": seconds}, ensure_ascii=False)
        + "\n"
    )
    sys.stdout.flush()


def _call(method, route, body=None, timeout=DEFAULT_TIMEOUT):
    """调主程序本地 API。

    错误响应的正文是 `{"error": "人话"}`，比 `HTTP Error 400` 有用得多 ——
    这里把它抠出来重新抛，上层直接 toast 给用户。
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + route,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + API_TOKEN,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (json.loads(exc.read().decode("utf-8")) or {}).get("error") or ""
        except Exception:  # noqa: BLE001 - 正文不是 JSON 就用状态码兜底
            detail = ""
        raise ApiError(detail or ("HTTP %s" % exc.code))
    except urllib.error.URLError as exc:
        raise ApiError("连不上 FontManager 的本地 API：%s" % exc.reason)


def api_get(route, timeout=DEFAULT_TIMEOUT):
    return _call("GET", route, None, timeout)


def api_post(route, body, timeout=DEFAULT_TIMEOUT):
    return _call("POST", route, body, timeout)


# ── 业务 ────────────────────────────────────────────────────────────────


def target_for_menu(title):
    """菜单标题 → 目标软件；认不出返回 None。

    先按前缀匹配（标题就是用户点的那行字）；再退一步按软件名兜底 ——
    以后标题被加上 emoji / 改成「套到 Photoshop 里」也还认得出来。
    """
    text = (title or "").strip()
    for prefix, target in MENU_TARGETS:
        if text.startswith(prefix):
            return target
    lowered = text.lower()
    for key in ("photoshop", "illustrator", "coreldraw"):
        if key in lowered:
            return key
    return None


def pick_face(faces, want_id=None):
    """挑一个能用来套用的字面。

    优先级：
    1. `want_id`（主程序双击转发时点名的那一行字重，1.0.1 起）；
    2. Regular；
    3. 第一个有 PostScript 名的。

    只认有 PostScript 名的：没有名字就没法在目标软件里定位，套用必然失败，
    不如在这里就换下一个。
    """
    usable = [f for f in faces if (f.get("postscriptName") or "").strip()]
    if want_id:
        for face in usable:
            if face.get("id") == want_id:
                return face
        # 点名了却找不到（清单不同步，或这个字面没有 PS 名）：记一行再回退，
        # 否则「双击 Bold 却套了 Regular」会变成一条无线索的怪现象
        log("主程序点名了字面 %s，但这个字族里没有可用的同 id 字面，回退到 Regular" % want_id)
    for face in usable:
        if (face.get("style") or "") == "Regular":
            return face
    return usable[0] if usable else None


def active_app():
    """`GET /apps/active`：最近在用的设计软件。拿不到就返回 None（不抛）。

    这个接口是主程序 1.9.11 才有的；插件被装到更老的主程序上时这里会 404，
    那种情况下「自动」本来就该说「没找到」，而不是抛一堆栈给用户看。
    """
    try:
        data = api_get("/apps/active")
    except ApiError as exc:
        log("查 /apps/active 失败：%s" % exc)
        return None
    app = data.get("app")
    return app if app in APP_LABELS else None


def describe(outcome, app, label, family, active):
    """outcome → 一句中文。口径与主程序一致，别在这里自创说法。"""
    name = APP_LABELS.get(app, app)
    if outcome.startswith("applied:"):
        count = outcome.split(":", 1)[1].strip() or "?"
        return "已应用「%s」到 %s（%s 个图层/文本框）" % (label, name, count)
    if outcome == "created":
        return "已在 %s 新建文字「%s」" % (name, label)
    if outcome == "no-text-selected":
        return "%s 里选中的不是文字" % name
    if outcome == "font-not-found":
        # 这条最容易踩：字体没启用，或者启用之后没重启目标软件（软件只在启动时读字体表）
        return "%s 里找不到「%s」——先在 FontManager 里启用它，再重启 %s" % (
            name,
            family,
            name,
        )
    if outcome == "font-rejected":
        # 字体在列表里、赋值却不生效。CorelDRAW 实测：字体和文字的语言不匹配时
        # 会静默拒绝（繁体字体套简体文字、或反过来），连逐字符赋值都不起作用。
        return (
            "%s 里有「%s」，但套不上 —— 多半是字体和文字的语言不匹配"
            "（例如繁体字体套在简体文字上）。换一段拉丁文字或同语言字体再试"
        ) % (name, label)
    if outcome == "no-document":
        return "%s 没打开文档" % name
    if outcome == "not-running":
        message = "%s 没在运行" % name
        if active == app:
            # 我们这边刚看到它有窗口，引擎却说没在运行：几乎都是权限问题
            # （提权运行的软件，普通权限的 COM 打不进去）
            message += "（可能以管理员身份运行，普通权限驱动不了它）"
        return message
    if outcome in ("app-no-response", "launch-timeout"):
        return "%s 没响应（可能弹着对话框 / 正在渲染）" % name
    if outcome.startswith("ps-error:"):
        detail = outcome.split(":", 1)[1].strip()
        return detail or outcome
    # 没见过的 outcome 原样透出：吞掉它，下次主程序加状态就变成「点了没反应」
    return outcome


def apply_family(family, face_id, target, active=None):
    """把 `family` 的某个字面套到 `target`。所有套用路径最终都走这里。"""
    data = api_get("/fonts?family=%s&limit=2000" % urllib.parse.quote(family))
    faces = data.get("faces") or []
    if not faces:
        toast("字体库里找不到「%s」" % family)
        return
    face = pick_face(faces, face_id)
    if face is None:
        toast("这个字族没有可用的字面（都没有 PostScript 名）")
        return

    style = (face.get("style") or "").strip()
    label = ("%s %s" % (family, style)).strip()
    # CorelDRAW 对中文字体一律按中文名登记，只给英文名会成片地找不到
    aliases = [
        n.get("name")
        for n in (face.get("localizedNames") or [])
        if (n.get("name") or "").strip()
    ]

    result = api_post(
        "/fonts/apply",
        {
            "app": target,
            "postscriptName": face.get("postscriptName"),
            "label": label,
            "family": family,
            "aliases": aliases,
        },
        timeout=APPLY_TIMEOUT,
    )
    outcome = result.get("outcome") or ""
    message = describe(outcome, target, label, family, active)
    toast(message)
    log("%s → %s：%s" % (family, APP_LABELS.get(target, target), message))


def apply_auto(family, face_id):
    """套用到「最近在用的设计软件」。双击(auto) 与菜单「自动：…」共用。"""
    active = active_app()
    if active is None:
        toast("没找到正在运行的设计软件（PS / AI / CDR）")
        return
    apply_family(family, face_id, active, active)


def apply_pick(family, face_id):
    """pick 模式：插件自己等用户点一下目标窗口（不经过主程序）。"""
    if not pick.IS_WINDOWS:
        toast("「先选窗口」只在 Windows 上可用")
        return
    # 常驻提示（1.9.14 的 hint 通道）：用户得一直看得见「现在该点一下窗口」——
    # toast 一两秒就没了，等他低头去找窗口时提示已经消失，只剩一个不知道该干什么的面板。
    # 所以这里**不再**发同文案的 toast，避免两条重复。
    emit_hint(PICK_HINT, PICK_HINT_SECONDS)
    try:
        result = pick.wait_for_target()
    finally:
        # 不管怎么结束都先清提示，再报结果：结果走 toast，提示位不留残影
        emit_hint("")
    if result is None:
        toast("「先选窗口」只在 Windows 上可用")
        return
    app, exe, reason = result
    if reason == "hit":
        log("pick 命中 %s（%s）" % (app, exe))
        apply_family(family, face_id, app)
        return
    if reason == "timeout":
        toast("已取消：15 秒内没有选择窗口")
        return
    if reason == "cancelled":
        toast("已取消")
        return
    toast("这不是 PS / AI / CDR 窗口，没有套用" + ("（%s）" % exe if exe else ""))


def open_settings():
    """打开设置面板：主程序能开应用内窗口就用它，否则退回系统浏览器。"""
    url = panel.open_panel()
    if _main_supports_panel_window():
        emit_open_url(url, "字体套用 · 设置")
    else:
        panel.open_in_browser(url)
    log("设置面板已打开：%s" % url)


def _main_supports_panel_window():
    """主程序 ≥1.4.3 才会处理 open-url；老版本问都不问，直接走浏览器。"""
    try:
        version = str((api_get("/health") or {}).get("version") or "")
        parts = tuple(int(x) for x in version.split(".")[:3])
        return parts >= (1, 4, 3)
    except Exception:  # noqa: BLE001 - 版本问不到就当不支持
        return False


def on_context_menu(payload):
    payload = payload or {}
    family = (payload.get("family") or "").strip()
    menu = (payload.get("menu") or "").strip()
    # 主程序双击转发时带上被双击的那一行字重（1.0.1）；从右键菜单点则没有这个字段
    face_id = payload.get("faceId") or ""

    # `on:"plugin"` 的设置入口：没有 family，只开面板
    if not family:
        if menu.startswith(SETTINGS_MENU_PREFIX):
            open_settings()
        else:
            log("没有拿到字族名，跳过")
        return

    if face_id:
        # ── 来自主程序的双击：按 doubleClickMode 走 ──
        mode = config.double_click_mode()
        log("双击转发：%s（mode=%s, faceId=%s）" % (family, mode, face_id))
        if mode == "pick":
            apply_pick(family, face_id)
        else:
            apply_auto(family, face_id)
        return

    # ── 来自右键菜单：一律按菜单项语义，不受 doubleClickMode 影响 ──
    target = target_for_menu(menu)
    if target is None:
        toast("这个菜单项认不出来：%s" % menu)
        return
    if target == "auto":
        apply_auto(family, "")
    else:
        apply_family(family, "", target)


def main():
    global API_BASE, API_TOKEN
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            log("收到非 JSON 行，忽略")
            continue
        kind = msg.get("type")
        if kind == "init":
            API_BASE = msg.get("apiBase", "")
            API_TOKEN = msg.get("apiToken", "")
            log("已就绪：apiBase=%s，双击行为=%s" % (API_BASE, config.double_click_mode()))
        elif kind == "invoke":
            hook = msg.get("hook")
            try:
                if hook == "context-menu":
                    on_context_menu(msg.get("payload") or {})
                elif hook == "settings":
                    open_settings()
                else:
                    log("不认识的钩子：%s" % hook)
            except Exception as exc:  # noqa: BLE001 - 插件崩了用户什么都看不到
                toast("失败：%s" % exc)
                log(repr(exc))


if __name__ == "__main__":
    main()
