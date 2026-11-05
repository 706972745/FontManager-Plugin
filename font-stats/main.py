#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""示例插件：字体使用统计（进程外插件协议的最小实现）。

它是**独立进程**，与 FontManager 只有两条通道：
1. stdin/stdout 上的 JSON 行（握手 + 调用）——
   主程序先发 {"type":"init","apiBase":..,"apiToken":..}，
   用户点右键菜单项时再发 {"type":"invoke","hook":"context-menu","payload":{"family":".."}}；
2. 本地 HTTP API（用 init 里给的受限令牌）。

本示例做的事：收到 context-menu 调用 → 查这个字族有几个字面、是否可商用 →
把结果写进该字族的备注（界面里立刻能看到），并回一条 log。
"""

import json
import sys
import urllib.parse
import urllib.request

API_BASE = ""
API_TOKEN = ""


def api(method, route, body=None):
    """调 FontManager 本地 API（令牌是主程序按 manifest 权限签发的受限令牌）"""
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
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def log(message):
    print(json.dumps({"type": "log", "message": message}, ensure_ascii=False), flush=True)


def on_context_menu(payload):
    family = (payload or {}).get("family") or ""
    if not family:
        log("没有拿到字族名，跳过")
        return
    data = api("GET", "/fonts?family=%s&limit=200" % urllib.parse.quote(family))
    faces = data.get("faces") or []
    if not faces:
        log("字体库里找不到 %s" % family)
        return
    classes = {}
    for f in faces:
        key = f.get("copyrightClass") or "unknown"
        classes[key] = classes.get(key, 0) + 1
    parts = "、".join("%s %d 个" % (k, v) for k, v in sorted(classes.items()))
    note = "【统计】%d 个字面；版权分类：%s" % (len(faces), parts)
    api("PUT", "/curation/note", {"family": family, "note": note})
    log("%s → %s" % (family, note))


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
            log("已就绪：apiBase=%s" % API_BASE)
        elif kind == "invoke":
            hook = msg.get("hook")
            if hook == "context-menu":
                try:
                    on_context_menu(msg.get("payload") or {})
                except Exception as exc:  # noqa: BLE001
                    log("调用失败：%s" % exc)
            else:
                log("不认识的钩子：%s" % hook)


if __name__ == "__main__":
    main()
