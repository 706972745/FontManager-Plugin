#!/usr/bin/env python3
"""示例插件：把字体渲染的示例文本导出成 PNG。

用途有两层：
1. 对用户：右键字族 →「导出示例图」，把这款字体的样子存成图片（贴给同事、发文章都方便）。
2. 对开发者：插件一启动就自己跑一遍 **本地 API + 受限令牌 + 权限过滤** 的自检，
   结果写进导出目录的 self-check.json 与软件日志 —— 不用手点界面也能确认链路是通的。

通信协议（与主程序约定）：
- 收 stdin 的 JSON 行：{"type":"init", apiBase, apiToken, ...} / {"type":"invoke", hook, payload}
- 发 stdout 的 JSON 行：{"type":"log", message} / {"type":"toast", message}

设置面板走 settings 钩子（有 tkinter 才有窗口，没有就退化成文件夹选择框）。
只依赖标准库 + Pillow（Pillow 缺失时降级成「只报错不出图」，不会崩）。
"""
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

API_BASE = ""
API_TOKEN = ""
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
DEFAULT_EXPORT_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "FontManager", "exports"
)
MENU_EXPORT = "导出示例图（PNG）"
MENU_FOLDER = "设置导出位置…"

LATIN_SAMPLE = "Hello, FontManager"
CJK_SAMPLE = "中文示例：永和九年，岁在癸丑"
GLYPH_SAMPLE = "AaBbCc 0123456789 &@%#"


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(patch):
    cfg = load_config()
    cfg.update(patch)
    try:
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log(f"保存配置失败：{e}")
        return False


def export_dir():
    """导出位置：环境变量 > 插件目录下的 config.json > 默认目录。"""
    env = os.environ.get("FONTMANAGER_EXPORT_DIR", "").strip()
    if env:
        return os.path.expandvars(os.path.expanduser(env))
    cfg = load_config()
    custom = str(cfg.get("exportDir") or "").strip()
    if custom:
        return os.path.expandvars(os.path.expanduser(custom))
    return DEFAULT_EXPORT_DIR


def choose_folder():
    """弹系统文件夹选择框。插件没有主界面，用 tkinter 的原生对话框最省事；
    万一这个 Python 不带 tkinter，就返回 None 让调用方提示手动改 config.json。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="选择示例图保存位置", mustexist=False)
        root.destroy()
        return path or None
    except Exception as e:
        log(f"打开文件夹选择框失败：{e}")
        return None



def sample_lines(face):
    """字体没有中文字形时换一行字符集示例 —— 硬塞中文只会得到一排豆腐块。"""
    scripts = [str(s).lower() for s in (face.get("scripts") or [])]
    has_cjk = any(s.startswith(("han", "cjk")) or s in ("hans", "hant", "jpan", "kore") for s in scripts)
    return [LATIN_SAMPLE, CJK_SAMPLE if has_cjk else GLYPH_SAMPLE]


def meta_font(size):
    """小字用系统里带汉字的字体 —— 拿被导出的字体本身画「字形」两个字，
    遇到纯拉丁字体就是一排豆腐块（Book Antiqua 那次就是这样）。"""
    win = os.environ.get("WINDIR") or r"C:\Windows"
    for name in ("msyh.ttc", "msyhl.ttc", "simhei.ttf", "simsun.ttc", "Deng.ttf"):
        path = os.path.join(win, "Fonts", name)
        if os.path.exists(path):
            try:
                from PIL import ImageFont

                return ImageFont.truetype(path, size)
            except Exception:
                continue
    from PIL import ImageFont

    return ImageFont.load_default()


def emit(kind, message):
    sys.stdout.write(json.dumps({"type": kind, "message": message}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(message):
    emit("log", message)


def http(method, path, body=None):
    """返回 (status, json 或 None)。不抛异常 —— 自检要的是「结果」，不是崩溃。"""
    url = API_BASE + path
    data = None
    headers = {"Authorization": "Bearer " + API_TOKEN}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except Exception:
            parsed = {"raw": raw[:400]}
        return e.code, parsed
    except Exception as e:  # 连不上（软件关了 / 端口变了）
        return 0, {"error": str(e)}


def safe_name(text):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_")[:80] or "font"


def pick_face(family=None):
    """找一条可用的字体文件。给了 family 就必须是它 —— 找不到宁可报错，
    也别悄悄换成别的字体（用户点了 A 却导出 B 的图，比失败更糟）。"""
    if family:
        q = urllib.parse.quote(family)
        # family= 是精确匹配；没命中再用 q= 模糊一次（用户写的常是简称）
        for url in (f"/fonts?family={q}&limit=5", f"/fonts?q={q}&limit=5"):
            status, data = http("GET", url)
            if status == 200:
                for f in data.get("faces") or []:
                    if f.get("path"):
                        return f
        return None
    status, data = http("GET", "/fonts?limit=40")
    if status == 200:
        for f in data.get("faces") or []:
            if f.get("path"):
                return f
    return None


def render(face, out_path):
    """用该字体画两张示例文本 + 字族名，返回 (宽, 高)。"""
    from PIL import Image, ImageDraw

    size_big, size_small = 76, 20
    pad, gap = 48, 26
    width = 1280
    from PIL import ImageFont

    try:
        big = ImageFont.truetype(face["path"], size_big)
    except Exception as e:
        raise RuntimeError(f"字体打不开（{face.get('path')}）：{e}")
    # 小字（左上角字族名、底部元信息）一律用系统汉字字体，
    # 免得纯拉丁字体把「字形」画成豆腐块、中文字族名整行丢失。
    small = meta_font(size_small)
    label = meta_font(17)

    # 先量一遍行高，再决定画布高度
    probe = Image.new("RGB", (10, 10))
    pd = ImageDraw.Draw(probe)
    lines = sample_lines(face)
    heights = []
    for line in lines:
        box = pd.textbbox((0, 0), line, font=big)
        heights.append(box[3] - box[1])
    height = pad * 2 + sum(heights) + gap * (len(lines) - 1) + 64

    # 深色底 + 浅色字，和软件界面一致；导出图贴到白底文档里也看得清
    img = Image.new("RGB", (width, height), (24, 24, 27))
    d = ImageDraw.Draw(img)
    d.text((pad, pad - 18), face.get("family", ""), font=label, fill=(150, 150, 158))
    y = pad + 10
    for line, h in zip(lines, heights):
        d.text((pad, y), line, font=big, fill=(240, 240, 245))
        y += h + gap
    scripts = "/".join(face.get("scripts") or []) or "?"
    meta = (
        f'{face.get("family", "")} · {face.get("style", "")} · '
        f'{os.path.basename(face.get("path", ""))} · {face.get("glyphCount") or "?"} 字形 · {scripts}'
    )
    d.text((pad, height - 44), meta, font=small, fill=(140, 140, 150))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "PNG")
    return img.size


def export_family(family):
    """导出一个字族的示例图；成功返回文件路径。"""
    face = pick_face(family)
    if not face:
        return None, "找不到这个字族的字体文件（精确匹配和模糊搜索都没命中）"
    out = os.path.join(export_dir(), safe_name(face.get("family") or family) + ".png")
    try:
        w, h = render(face, out)
    except Exception as e:
        return None, f"渲染失败：{e}"
    return out, f"{w}×{h}"


def self_check():
    """启动即跑：API 通不通 / 令牌管不管用 / 没权限的端点是不是真被挡住。"""
    results = {}
    status, health = http("GET", "/health")
    results["health"] = {"status": status, "ok": (health or {}).get("ok"), "faces": (health or {}).get("faces")}

    # 插件常常比「首次扫描」先起来：字体快照一开始是空的，等一会儿再问，
    # 别把「还没扫完」当成「API 坏了」。
    faces, status, waited = [], 0, 0.0
    for _ in range(8):
        status, data = http("GET", "/fonts?limit=3")
        faces = (data or {}).get("faces") or []
        if faces:
            break
        time.sleep(1.5)
        waited += 1.5
    results["list_fonts"] = {"status": status, "count": len(faces), "waitedSeconds": waited}

    face = faces[0] if faces else None
    if face:
        out = os.path.join(export_dir(), "_selfcheck.png")
        try:
            w, h = render(face, out)
            results["render"] = {"ok": True, "file": out, "size": f"{w}×{h}", "family": face.get("family")}
        except Exception as e:
            results["render"] = {"ok": False, "error": str(e)}

        fam = face.get("family")
        status, note = http("PUT", "/curation/note", {"family": fam, "note": "【示例插件】已导出示例图 _selfcheck.png"})
        results["write_note"] = {"status": status, "body": note}

    # 这个插件没申请 import 权限：应当 403，正好验证「权限过滤」不是摆设。
    # 但用全权令牌（单独调试脚本时）本来就有 import，422 只是参数校验，别误判成漏权。
    perms = (health or {}).get("permissions") or []
    full = "*" in perms or "import" in perms
    status, body = http("POST", "/import", {"src": "C:/definitely-not-here"})
    results["import_denied"] = {
        "status": status,
        "expected": 403,
        "skipped": full,
        "note": "全权令牌（主令牌）跳过：它本来就有 import 权限" if full else "",
        "body": body,
    }

    payload = {
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "apiBase": API_BASE,
        "exportDir": export_dir(),
        "results": results,
    }
    out_dir = export_dir()
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "self-check.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log(f"导出位置：{out_dir}")

    ok_health = results["health"].get("status") == 200
    denied = results["import_denied"]["status"] == 403
    skipped = results["import_denied"].get("skipped")
    log(
        "自检：health=%s 字体=%s 出图=%s 写备注=%s 越权拦截=%s"
        % (
            "OK" if ok_health else results["health"].get("status"),
            len(faces),
            results.get("render", {}).get("ok"),
            results.get("write_note", {}).get("status"),
            "跳过(主令牌)" if skipped else ("OK(403)" if denied else f"漏了({results['import_denied']['status']})"),
        )
    )
    return payload


def set_export_folder():
    """让用户挑一个保存位置，记进 config.json。"""
    current = export_dir()
    picked = choose_folder()
    if not picked:
        log("没选文件夹（取消或这个 Python 没有 tkinter）")
        emit("toast", f"仍然保存到：{current}")
        return
    if save_config({"exportDir": picked}):
        log(f"导出位置改为：{picked}")
        emit("toast", f"以后示例图保存到：{picked}")


def open_settings_panel():
    """settings 钩子：在插件进程里弹一块**自己的**设置面板（FontManager 1.3.7+）。

    主程序只负责把钩子发过来（设置 → 插件 → 该行右侧的齿轮），面板长什么样、
    怎么存，全由插件自己决定。两个注意点：

    - **必须另起线程**跑 tkinter 的主循环。面板开着的时候 stdin 里还会来别的调用
      （右键菜单、再点一次齿轮），在主线程里 mainloop 等于把插件堵死。
    - 没装 tkinter 的精简版 Python 上，退化成系统文件夹选择框，不报错、不崩。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        set_export_folder()
        return

    def run():
        root = tk.Tk()
        root.title("示例文本导出 · 设置")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        var = tk.StringVar(value=export_dir())
        frame = tk.Frame(root, padx=14, pady=12)
        frame.pack()
        tk.Label(frame, text="示例图的保存位置", anchor="w").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        tk.Entry(frame, textvariable=var, width=46).grid(
            row=1, column=0, columnspan=3, sticky="we", pady=(4, 10)
        )

        def browse():
            picked = filedialog.askdirectory(
                title="选择示例图的保存位置", initialdir=var.get() or export_dir()
            )
            if picked:
                var.set(picked)

        def save():
            target = (var.get() or "").strip()
            if not target:
                emit("toast", "导出位置不能为空")
                return
            if save_config({"exportDir": target}):
                emit("toast", f"以后示例图保存到：{target}")
                log(f"设置面板：导出位置改为 {target}")
                root.destroy()

        tk.Button(frame, text="选择…", width=8, command=browse).grid(row=2, column=0, sticky="w")
        tk.Button(frame, text="保存", width=8, command=save).grid(row=2, column=1, padx=6)
        tk.Button(frame, text="关闭", width=8, command=root.destroy).grid(row=2, column=2, sticky="e")
        tk.Label(
            frame,
            text="保存后立即生效；也可以直接改插件目录里的 config.json。",
            fg="#888",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()


def handle_invoke(hook, payload):
    payload = payload or {}
    # 齿轮（设置 → 插件 → 该行右侧）：插件自己弹设置面板，不依赖具体字族
    if hook == "settings":
        open_settings_panel()
        return
    if hook != "context-menu":
        log(f"收到未处理的钩子：{hook}")
        return
    family = payload.get("family") or ""
    menu = payload.get("menu") or ""

    # 「设置导出位置」不依赖具体的字族，先判掉
    if menu.startswith("设置导出位置") or menu == "folder":
        set_export_folder()
        return
    if menu.startswith("打开导出文件夹") or menu == "open-folder":
        target = export_dir()
        try:
            os.makedirs(target, exist_ok=True)
            os.startfile(target)  # 这里要的是「进入目录本身」，Windows 上 startfile 就是对的
            log(f"已打开导出文件夹：{target}")
        except Exception as e:
            log(f"打开导出文件夹失败：{e}")
            emit("toast", f"打不开：{e}")
        return

    if not family:
        log("context-menu 没带 family，忽略")
        return
    path, extra = export_family(family)
    if path:
        log(f"已导出 {family} → {path}（{extra}）")
        folder = os.path.basename(os.path.dirname(path)) or path
        emit("toast", f"已导出 {os.path.basename(path)} → {folder}")
        # 顺手把导出路径写进备注，方便在软件里找回
        http("PUT", "/curation/note", {"family": family, "note": f"【示例插件】示例图：{os.path.basename(path)}"})
    else:
        log(f"导出 {family} 失败：{extra}")
        emit("toast", f"导出失败：{extra}")


def main():
    global API_BASE, API_TOKEN
    log("示例文本导出插件已就绪（右键字族即可导出 PNG）")
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
