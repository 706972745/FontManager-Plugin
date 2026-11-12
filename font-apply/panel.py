# -*- coding: utf-8 -*-
"""设置面板：本地起一个只监听 127.0.0.1 的小服务，把页面交给主程序开成应用内窗口。

为什么不用 tkinter：插件用的是主程序内置/系统 Python，不保证带 tkinter；
用户机器上装没装什么都不该影响设置面板。走 HTTP 页面：零依赖，还能放单选与说明。

安全边界（与 font-sync 的面板同一套）：
- 只绑 127.0.0.1，不监听对外网卡；
- 每次打开生成一次性随机 key，页面与所有 API 都要带上（`?k=` / `X-Panel-Key`）；
- 不发 CORS 头，别的站点读不到响应；
- 45 分钟无请求自动关停。

面板只做一件事：选双击行为（auto / pick）并存进 config.json。
"""

import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

IDLE_SECONDS = 45 * 60

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>字体套用 · 设置</title>
<style>
  :root{
    --bg:#fafaf8; --fg:#141412; --fg2:#6b6b66; --hair:#e2e2dd; --card:#fff;
    --accent:#1b4fe0; --hover:#f1f1ed;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#111110; --fg:#e8e8e8; --fg2:#9a9a94; --hair:#2a2a28; --card:#171716;
           --accent:#5b8cff; --hover:#1f1f1e; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:13px/1.6 "Inter","Segoe UI","Microsoft YaHei",system-ui,sans-serif;
       padding:32px 28px}
  h1{font-size:15px;font-weight:600;margin:0 0 4px}
  p.lead{margin:0 0 20px;color:var(--fg2);font-size:12px}
  .group{border:1px solid var(--hair);border-radius:4px;background:var(--card);
         overflow:hidden;max-width:640px}
  label.opt{display:flex;gap:10px;padding:14px 16px;cursor:pointer;
            border-top:1px solid var(--hair)}
  label.opt:first-child{border-top:0}
  label.opt:hover{background:var(--hover)}
  input[type=radio]{margin:3px 0 0;accent-color:var(--accent);flex:none}
  .t{font-weight:500}
  .d{color:var(--fg2);font-size:12px;margin-top:2px}
  .foot{max-width:640px;display:flex;align-items:center;gap:12px;margin-top:18px}
  button{border:1px solid var(--hair);border-radius:4px;background:var(--card);
         color:var(--fg);height:32px;padding:0 16px;font:inherit;cursor:pointer}
  button:hover{background:var(--hover)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover{filter:brightness(1.08)}
  #msg{color:var(--fg2);font-size:12px}
  #msg.ok{color:var(--accent)}
  #msg.bad{color:#c0392b}
</style></head><body>
<h1>字体套用 · 设置</h1>
<p class="lead">在速览界面双击字体时，这个插件该怎么做。</p>
<form id="f">
  <div class="group">
    <label class="opt"><input type="radio" name="mode" value="auto">
      <span><span class="t">双击直接套用到最近在用的设计软件</span>
      <span class="d">主程序会告诉我们最近在用的是 Photoshop / Illustrator / CorelDRAW 中的哪一个，直接套过去。</span></span></label>
    <label class="opt"><input type="radio" name="mode" value="pick">
      <span><span class="t">双击后先选窗口：点一下要应用到的 PS / AI / CDR 窗口</span>
      <span class="d">双击后 15 秒内点一下目标窗口即可；点任务栏切过去也认（有 1.5 秒宽限）。Esc 或超时即取消。</span></span></label>
  </div>
  <div class="foot">
    <button type="submit" class="primary">保存</button>
    <span id="msg"></span>
  </div>
</form>
<script>
const key = new URLSearchParams(location.search).get("k") || "";
const msg = document.getElementById("msg");
function say(text, cls){ msg.textContent = text; msg.className = cls || ""; }
fetch("/state?k=" + encodeURIComponent(key))
  .then(r => r.json())
  .then(s => {
    const v = s.doubleClickMode === "pick" ? "pick" : "auto";
    document.querySelector('input[name=mode][value=' + v + ']').checked = true;
  })
  .catch(() => say("读不到当前设置", "bad"));
document.getElementById("f").addEventListener("submit", e => {
  e.preventDefault();
  const picked = document.querySelector('input[name=mode]:checked');
  if (!picked) { say("先选一项", "bad"); return; }
  say("保存中…");
  fetch("/save?k=" + encodeURIComponent(key), {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({doubleClickMode: picked.value})
  }).then(r => r.json()).then(r => {
    if (r && r.ok) say("已保存", "ok");
    else say("保存失败：" + ((r && r.error) || "未知原因"), "bad");
  }).catch(() => say("保存失败：连不上插件", "bad"));
});
</script>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "FontApplyPanel/1.1"

    def log_message(self, *args):  # 不往 stderr 刷访问日志（会进 activation.log）
        return

    def _authorized(self):
        key = self.headers.get("X-Panel-Key") or ""
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        key = key or (params.get("k") or [""])[0]
        return bool(key) and secrets.compare_digest(key, self.server.panel.key)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 不发 CORS 头：别的站点读不到响应
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.panel.hit()
        path = urllib.parse.urlparse(self.path).path
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if not self._authorized():
            self._json({"ok": False, "error": "key 不对"}, 403)
            return
        if path == "/":
            self._html(PAGE)
            return
        if path == "/state":
            self._json(config.load())
            return
        self._json({"ok": False, "error": "unknown endpoint"}, 404)

    def do_POST(self):
        self.server.panel.hit()
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            self._json({"ok": False, "error": "key 不对"}, 403)
            return
        if path != "/save":
            self._json({"ok": False, "error": "unknown endpoint"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            patch = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            self._json({"ok": False, "error": "请求体不是 JSON：%s" % exc}, 400)
            return
        try:
            saved = config.save(patch)
        except OSError as exc:
            # 写盘失败要如实说，不能装作保存成功
            self._json({"ok": False, "error": "写配置失败：%s" % exc}, 500)
            return
        self._json({"ok": True, "config": saved})


class Panel:
    def __init__(self):
        self.key = secrets.token_urlsafe(24)
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
        threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True
        ).start()
        threading.Thread(target=self._reaper, daemon=True).start()
        return self

    def _reaper(self):
        while not self._stop.wait(30):
            if time.time() - self.last_hit > IDLE_SECONDS:
                self.stop()
                return

    def hit(self):
        self.last_hit = time.time()

    def url(self):
        return "http://127.0.0.1:%d/?k=%s" % (self.port, self.key)

    def alive(self):
        return bool(self.httpd) and not self._stop.is_set()

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        if _CURRENT.get("panel") is self:
            _CURRENT["panel"] = None


_CURRENT = {"panel": None}


def open_panel():
    """起服务并返回 URL（已经开着就复用同一个 key）。开窗口由调用方决定。

    为什么不在这里自己开：主程序 1.4.3+ 能开**应用内窗口**（观感就是一个应用窗口），
    老版本只能退回系统默认浏览器。这个判断属于 main.py 的握手逻辑，放这里会把
    「插件怎么跟主程序说话」的知识散到两个文件里。
    """
    panel = _CURRENT.get("panel")
    if panel and panel.alive():
        return panel.url()
    panel = Panel().start()
    _CURRENT["panel"] = panel
    return panel.url()


def open_in_browser(url):
    """降级路径：主程序太老、处理不了 open-url 时，用系统默认浏览器打开。"""
    try:
        webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001
        try:
            import os

            os.startfile(url)  # noqa: S606 - 只可能是我们自己拼的 127.0.0.1 URL
        except Exception:  # noqa: BLE001
            pass
