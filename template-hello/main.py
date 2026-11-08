# -*- coding: utf-8 -*-
"""你好，插件 —— 最小可用模板。

改三个地方就是一个你自己的插件：
  1. plugin.json 里的 id / name / menus / permissions
  2. 本文件里的 do_family() / do_plugin()（真正干活的地方）
  3. 装进 FontManager：整个文件夹拷到 设置 → 插件 →「打开插件目录」打开的那个目录里，
     然后「重新扫描」+ 打开开关

协议只有两条 stdin / stdout 约定，下面全用到了。完整文档：
https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md
"""
import json
import sys
import urllib.parse
import urllib.request

API_BASE = ""
API_TOKEN = ""


# ── stdout 三个出口 ──────────────────────────────────────────────────────
def emit(kind, **fields):
    """往 stdout 写一行 JSON。主程序按 type 分派：log 进日志、toast 弹提示、
    progress 上主窗口底部进度条（1.4.8+；老版本忽略，所以长任务要配合 toast）。"""
    sys.stdout.write(json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(message):
    emit("log", message=message)


def toast(message):
    emit("toast", message=message)


# ── 本地 API（受限令牌） ─────────────────────────────────────────────────
def api(path):
    """GET /api/v1/... 。令牌只有 plugin.json 里 permissions 声明的权限，
    没申请的端点会拿到 403 —— 需要什么就先去 plugin.json 里申报。"""
    req = urllib.request.Request(
        API_BASE + path, headers={"Authorization": "Bearer " + API_TOKEN})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


# ── 真正干活的地方：把这两个函数换成你的功能 ────────────────────────────
def do_family(family):
    """字族右键：报这个字族有多少字面。"""
    rows = api("/fonts?family=" + urllib.parse.quote(family)).get("faces") or []
    toast(f"「{family}」共 {len(rows)} 个字面（模板演示）")


def do_plugin():
    """插件行右键：报字库总量。长任务的话在这里发 progress —— 见下方注释。"""
    families = api("/fonts/families").get("families") or []
    toast(f"字库共 {len(families)} 个族（模板演示）")

    # 你的任务如果超过两三秒，就把进度一条条发出来，用户在主窗口底部能看到：
    #   emit("progress", text=f"处理中 3/{len(families)}", percent=30, running=True)
    # 全部做完后发收尾（percent 不给就画流动条）：
    #   emit("progress", text="完成：处理了 N 个族", running=False)


# ── 主循环：stdin 上的 JSON 行就是主程序发来的调用 ──────────────────────
def handle(hook, payload):
    payload = payload or {}
    if hook == "context-menu":
        menu = payload.get("menu") or ""
        family = payload.get("family") or ""
        if menu.startswith("这个字族有多少字面") and family:
            do_family(family)
        elif menu.startswith("字库统计"):
            do_plugin()
    # hook == "init" 时主程序已把 init 写进 stdin，握手在下面统一处理


def main():
    global API_BASE, API_TOKEN
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "init":
            API_BASE = msg["apiBase"]
            API_TOKEN = msg["apiToken"]
            health = api("/health")
            log(f"你好，插件已就绪；我是「{health.get('tokenName')}」，"
                f"权限：{health.get('permissions')}")
        elif msg.get("type") == "invoke":
            try:
                handle(msg.get("hook"), msg.get("payload"))
            except Exception as e:            # 任务崩了要说话，别静默
                toast(f"失败：{e}")
                log("异常：" + repr(e))


if __name__ == "__main__":
    main()
