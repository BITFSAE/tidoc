"""用本机 HTTP 服务代替 PyWebView：真实前端页面，调用同一个进程里的真实 Api。

页面里注入一段脚本，把 window.pywebview.api 的每个方法转成 POST /rpc/<方法名>。
只监听 127.0.0.1，随进程结束关闭。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[2] / "tidoc" / "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
    ".png": "image/png", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".ttf": "font/ttf",
}

# 页面加载时执行：设主题、跳过首次使用指南、接上 Api。
# 带 sprite 参数时，把指定的几张卡片搬到一个无背景的容器里，按指定高度紧挨着叠放。
SHIM = """<script>(function () {
  var query = new URLSearchParams(location.search);
  try {
    localStorage.setItem("tidoc.themeMode", query.get("theme") || "dark");
    localStorage.setItem("tidoc.usageGuide.seen.v2", "1");
  } catch (e) {}
  var api = {};
  %(names)s.forEach(function (name) {
    api[name] = function () {
      return fetch("/rpc/" + name, {method: "POST", body: JSON.stringify([].slice.call(arguments))})
        .then(function (response) { return response.json(); });
    };
  });
  window.pywebview = {api: api};
  if (!query.get("sprite")) return;
  var style = document.createElement("style");
  style.textContent = "html,body{background:transparent!important}body::before,body::after{display:none!important}";
  document.addEventListener("DOMContentLoaded", function () { document.head.appendChild(style); });
  var titles = %(titles)s, heights = %(heights)s, width = %(width)s;
  function build() {
    var cards = [].slice.call(document.querySelectorAll(".entry-card"));
    var picked = titles.map(function (title) {
      return cards.find(function (card) { return card.textContent.indexOf(title) >= 0; });
    });
    if (picked.some(function (card) { return !card; })) return setTimeout(build, 100);
    var list = document.querySelector(".entry-list");
    var wrap = document.createElement("div");
    wrap.className = "entry-list";
    if (list && list.dataset.density) wrap.dataset.density = list.dataset.density;
    wrap.style.cssText = "position:fixed;left:0;top:0;width:" + width + "px;display:flex;flex-direction:column;" +
      "gap:0;padding:0;overflow:visible;z-index:99999;background:transparent";
    picked.forEach(function (card, index) {
      var copy = card.cloneNode(true);
      copy.style.contentVisibility = "visible";
      copy.style.boxSizing = "border-box";
      copy.style.height = heights[index] + "px";
      wrap.appendChild(copy);
    });
    document.body.innerHTML = "";
    document.body.appendChild(wrap);
  }
  window.addEventListener("load", function () { setTimeout(build, 800); });
})();</script>"""


def make_server(api, sprite_titles, sprite_heights, sprite_width) -> ThreadingHTTPServer:
    names = sorted(name for name in dir(api) if not name.startswith("_"))
    shim = SHIM % {
        "names": json.dumps(names),
        "titles": json.dumps(sprite_titles, ensure_ascii=False),
        "heights": json.dumps(sprite_heights),
        "width": sprite_width,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = self.path.split("?")[0]
            path = (WEB_DIR / ("index.html" if route == "/" else route.lstrip("/"))).resolve()
            if not path.is_file() or WEB_DIR.resolve() not in path.parents:
                return self._send(404, "text/plain", b"")
            body = path.read_bytes()
            if path.name == "index.html":
                body = body.decode("utf-8").replace("<head>", "<head>" + shim, 1).encode("utf-8")
            self._send(200, CONTENT_TYPES.get(path.suffix, "application/octet-stream"), body)

        def do_POST(self):
            name = self.path.rsplit("/", 1)[-1]
            if name not in names:
                return self._send(404, "text/plain", b"")
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
            try:
                result = getattr(api, name)(*args)
                # 截图环境没有云识别组件：隐藏云识别入口，与未安装组件的界面一致。
                if name == "ocr_component_status" and isinstance(result.get("data"), dict):
                    result["data"]["available"] = False
            except Exception as error:
                result = {"ok": False, "error": str(error)}
            self._send(200, "application/json", json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
