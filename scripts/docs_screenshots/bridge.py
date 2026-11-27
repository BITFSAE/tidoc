"""用本机 HTTP 服务代替 PyWebView：真实前端页面，调用同一个进程里的真实 Api。

页面里注入一段脚本，把 window.pywebview.api 的每个方法转成 POST /rpc/<方法名>。
`?dialog=` 参数决定截图前打开哪个窗口或隔离哪块界面，场景都在下面的 SHIM 里写。

截图时机由一张 /ready 图片控制：它在页面脚本报告界面就绪之前不返回，Chrome 会等 load
事件，因此不会拍到加载中或窗口还没打开的画面。只监听 127.0.0.1，随进程结束关闭；
更新检查在这里换成固定的离线结果，截图过程不联网。
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
# 1×1 透明 GIF：/ready 返回它，图片加载完成后 Chrome 才会触发 load 事件。
READY_IMAGE = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

# 页面加载时执行：设主题、跳过首次使用指南、接上 Api，再按 ?dialog= 打开对应界面。
# 带 sprite 参数时，把指定的几张卡片搬到一个无背景的容器里，按指定高度紧挨着叠放。
SHIM = """<script>(function () {
  var query = new URLSearchParams(location.search);
  try {
    localStorage.setItem("tidoc.themeMode", query.get("theme") || "dark");
    localStorage.setItem("tidoc.usageGuide.seen.v2", "1");
    localStorage.removeItem("tidoc.settings.devOptionsOpen");
  } catch (e) {}
  // 先挂一张就绪图片：/ready 在界面准备好之前不返回，load 事件随之推迟到截图时机。
  // 用 DOM 插入而不是 document.write，避免打断 head 的解析（换图表会丢掉站内样式）。
  var readyImage = document.createElement("img");
  readyImage.src = "/ready";
  readyImage.alt = "";
  readyImage.style.cssText = "position:fixed;left:-4px;top:-4px;width:1px;height:1px;opacity:0";
  document.documentElement.appendChild(readyImage);
  var signaled = false;
  function signal() { if (!signaled) { signaled = true; fetch("/scene-ready", {cache: "no-store"}); } }
  setTimeout(signal, 20000);  // 兜底：场景长时间不就绪也先拍，交给外层重试判断

  var api = {};
  %(names)s.forEach(function (name) {
    api[name] = function () {
      return fetch("/rpc/" + name, {method: "POST", body: JSON.stringify([].slice.call(arguments))})
        .then(function (response) { return response.json(); });
    };
  });
  window.pywebview = {api: api};

  function until(test, done) {
    (function poll() {
      var value = null;
      try { value = test(); } catch (e) { value = null; }
      if (value) return done(value);
      setTimeout(poll, 80);
    })();
  }
  function cards() { return [].slice.call(document.querySelectorAll(".entry-card")); }
  // 关掉入场动画和过渡：截图不应停在弹窗淡入、卡片放大的中间状态。
  var noMotion = document.createElement("style");
  noMotion.textContent = "*,*::before,*::after{animation:none!important;transition:none!important}";
  document.addEventListener("DOMContentLoaded", function () { document.head.appendChild(noMotion); });
  // 等字体、布局和窗口进场动画结束后再报告就绪。
  function settle(delay) { setTimeout(signal, delay || 650); }
  function pickCard(title) {
    return cards().find(function (card) { return card.textContent.indexOf(title) >= 0; });
  }
  // 把一块界面放到单色底上，裁图时只留下它。width 为空时按内容宽度。
  function isolate(node, padding, width, background) {
    var box = document.createElement("div");
    box.style.cssText = "position:fixed;inset:0;z-index:99999;box-sizing:border-box;overflow:hidden;" +
      "display:flex;align-items:flex-start;justify-content:center;padding:" + padding + "px;" +
      "background:" + (background || "var(--paper)");
    var inner = document.createElement("div");
    inner.style.cssText = "flex:0 0 auto;" + (width ? "width:" + width + "px;" : "");
    inner.appendChild(node);
    box.appendChild(inner);
    document.body.appendChild(box);
  }
  function isolateCard(title) {
    // 列表在宽窗口里是两列；截图用单列宽度，卡片内容才不会挤在一起。
    isolate(pickCard(title).cloneNode(true), 20, 756);
  }

  var scenes = {
    bindle: function () {
      until(function () { return cards()[0]; }, function () {
        importBindleFlow(%(bindle_path)s);
        until(function () { return document.querySelector(".bindle-entry-row"); }, settle);
      });
    },
    print: function () { openPrint(false); },
    "print-options": function () { openPrint(true); },
    detail: function () {
      until(function () { return cards()[0]; }, function () {
        openEntryDetail(cards()[0].dataset.entryId);
        until(function () { return document.querySelector(".modal .modal-body input"); }, settle);
      });
    },
    settings: function () {
      // 等条目和报账人都加载完再打开，否则「报账人」会显示 0 个，收款信息一行也不显示。
      until(function () { return cards().length && State.profiles.length > 1; }, function () {
        document.getElementById("settingsBtn").click();
        until(function () { return document.querySelector(".settings-shell"); }, function () {
          var dev = document.getElementById("setDevOptions");
          if (dev) dev.open = false;
          document.querySelectorAll(".settings-shell *").forEach(function (el) {
            if (el.children.length === 0 && el.textContent && el.textContent.indexOf("tidoc-shots-") >= 0) {
              el.textContent = "~/Documents/Tidoc";
            }
          });
          settle();
        });
      });
    },
    scheme: function () {
      AdapterUI.openSettings();
      until(function () {
        var page = document.querySelector(".scheme-page");
        return page && page.querySelectorAll(".settings-block").length > 2;
      }, settle);
    },
    "card-actions": function () {
      until(function () { return pickCard("锂电池组"); }, function () {
        isolateCard("锂电池组");
        settle();
      });
    },
    "first-run": function () {
      until(function () { return document.querySelector(".adapter-choice"); }, settle);
    },
    components: function () {
      openUpdateDialog();
      until(function () {
        return document.querySelector(".update-components .update-component") &&
          document.querySelector(".update-summary");
      }, settle);
    },
    export: function () {
      until(function () { return cards().length && State.batches.length; }, function () {
        var batch = State.batches.find(function (item) { return item.name === "9 月第一批"; });
        var ids = State.entries.filter(function (entry) {
          return (entry.batches || []).some(function (item) { return item.id === (batch && batch.id); });
        }).map(function (entry) { return entry.id; });
        doExport(ids);
        until(function () { return document.querySelector(".export-options"); }, settle);
      });
    },
    "batch-edit": function () {
      until(function () { return State.batches.length; }, function () {
        var batch = State.batches.find(function (item) { return item.name === "9 月第一批"; });
        renameBatchFlow(batch);
        until(function () { return document.getElementById("bNote"); }, settle);
      });
    },
    "batch-toolbar": function () {
      until(function () { return cards().length >= 3; }, function () {
        cards().slice(0, 3).forEach(function (card) { selectEntryFromCard(card.dataset.entryId); });
        until(function () {
          var count = document.getElementById("selCount");
          return count && count.textContent.indexOf("已选") >= 0;
        }, function () {
          var bar = document.getElementById("selectionBar").cloneNode(true);
          bar.classList.remove("empty");
          isolate(bar, 18);
          settle();
        });
      });
    },
    "status-views": function () {
      until(function () { return cards().length && document.getElementById("stats").children.length; }, function () {
        var head = document.querySelector(".content-head").cloneNode(true);
        var wrap = document.createElement("div");
        wrap.style.cssText = "width:1200px;background:var(--paper)";
        wrap.appendChild(document.getElementById("toolbar").cloneNode(true));
        var headWrap = document.createElement("div");
        headWrap.style.cssText = "padding:12px 24px 0";
        headWrap.appendChild(head);
        wrap.appendChild(headWrap);
        var box = document.createElement("div");
        box.style.cssText = "position:fixed;inset:0;z-index:99999;box-sizing:border-box;" +
          "overflow:hidden;background:var(--paper)";
        box.appendChild(wrap);
        document.body.appendChild(box);
        settle();
      });
    },
    "import-preview": function () {
      until(function () { return cards().length; }, function () {
        Api.scanFiles(%(import_paths)s).then(function (scan) {
          openBatchImportPreview(scan, %(import_label)s);
          until(function () { return document.querySelector(".batch-import-summary"); }, settle);
        });
      });
    }
  };

  // 带识别提醒的详情：打开「锂电池组」的详情，只把「关键信息」一栏摆在深色底上，裁剪时只留下它。
  function warningDetail() {
    var opened = false;
    until(function () {
      if (!opened) {
        var card = pickCard("锂电池组");
        if (!card) return false;
        openEntryDetail(card.dataset.entryId);
        opened = true;
      }
      return document.querySelector("[data-check-block] .check-line");
    }, function (block) {
      var section = block.closest(".detail-section");
      var cover = document.createElement("div");
      cover.style.cssText = "position:fixed;inset:0;z-index:9998;background:#202020";
      var wrap = document.createElement("div");
      wrap.style.cssText = "position:fixed;top:24px;left:24px;width:620px;z-index:9999;padding:16px 20px;" +
        "border-radius:12px;background:var(--panel)";
      wrap.appendChild(section);
      document.body.append(cover, wrap);
      settle();
    });
  }
  // 付款截图 AI 设置：打开设置和开发者选项，把这一小节单独摆在深色底上，裁剪时只留下它。
  function paymentAi() {
    until(function () {
      var section = document.querySelector(".payment-ai-settings");
      if (section) return section;
      var button = document.getElementById("settingsBtn");
      if (button && !document.querySelector(".settings-shell")) button.click();
      return null;
    }, function (section) {
      var cover = document.createElement("div");
      cover.style.cssText = "position:fixed;inset:0;z-index:9998;background:#202020";
      var wrap = document.createElement("div");
      wrap.style.cssText = "position:fixed;top:24px;left:24px;width:800px;z-index:9999;padding:0 20px 8px;" +
        "border-radius:12px;background:var(--panel)";
      section.style.borderTop = "none";
      wrap.appendChild(section);
      document.body.append(cover, wrap);
      settle();
    });
  }
  function openPrint(options) {
    until(function () { return cards()[0]; }, function () {
      AdapterUI.print([cards()[0].dataset.entryId]);
      until(function () { return document.querySelector(".export-dialog"); }, function () {
        if (!options) return settle();
        until(function () {
          return Array.from(document.querySelectorAll(".export-dialog .export-more"))
            .find(function (node) { return node.querySelector("summary").textContent.trim() === "扩展选项"; });
        }, function (summary) { summary.open = true; settle(520); });
      });
    });
  }

  if (query.get("dialog") === "warning-detail") warningDetail();
  else if (query.get("dialog") === "payment-ai") paymentAi();
  else if (query.get("sprite")) sprite();
  else if (scenes[query.get("dialog")]) {
    document.addEventListener("DOMContentLoaded", scenes[query.get("dialog")]);
  }
  else if (!query.get("dialog")) {
    // 普通页面：等条目列表出现后再留一点时间给字体和布局。
    until(function () { return cards().length; }, function () { settle(700); });
  }

  function sprite() {
    var style = document.createElement("style");
    style.textContent = "html,body{background:transparent!important}body::before,body::after{display:none!important}";
    document.addEventListener("DOMContentLoaded", function () { document.head.appendChild(style); });
    var titles = %(titles)s, heights = %(heights)s, width = %(width)s;
    function build() {
      var picked = titles.map(function (title) {
        return cards().find(function (card) { return card.textContent.indexOf(title) >= 0; });
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
      settle(180);
    }
    document.addEventListener("DOMContentLoaded", function () { setTimeout(build, 600); });
  }
})();</script>"""


def _update_status(core_version: str, print_version: str, ocr_version: str) -> dict:
    """固定的更新检查结果：核心已是最新，两个可选组件可安装；内容里没有真实下载地址。"""
    def component(name: str, label: str, version: str, available: bool, size: int) -> dict:
        return {
            "component": name,
            "name": label,
            "installed_valid": True,
            "needs_repair": False,
            "install_issue": "",
            "current_version": "" if available else version,
            "latest_version": version,
            "available": available,
            "manifest_missing": False,
            "downloaded": False,
            "downloaded_path": "",
            "state": "available" if available else "current",
            "asset": {"version": version, "size": size, "notes": [], "name": label},
        }

    return {
        "channel": "stable",
        "manifest_url": "",
        "platform": "macos",
        "current_core_version": core_version,
        "checked_at": 1790000000,
        "updates": [
            component("core", "tidoc 核心", core_version, False, 13 * 1024 * 1024),
            component("print", "打印导出组件", print_version, True, 19 * 1024 * 1024),
            component("ocr", "OCR 识别组件", ocr_version, True, 20 * 1024 * 1024),
        ],
        "components": {},
    }


def make_server(api, sprite_titles, sprite_heights, sprite_width, *, bindle_path="", import_paths=()) -> ThreadingHTTPServer:
    from tidoc import __version__
    from tidoc_ocr import __version__ as ocr_version
    from tidoc_print import __version__ as print_version

    names = sorted(name for name in dir(api) if not name.startswith("_"))
    update_status = _update_status(__version__, print_version, ocr_version)
    scene_ready = threading.Event()
    shim = SHIM % {
        "names": json.dumps(names),
        "titles": json.dumps(sprite_titles, ensure_ascii=False),
        "heights": json.dumps(sprite_heights),
        "width": sprite_width,
        "bindle_path": json.dumps(str(bindle_path)),
        "import_paths": json.dumps([str(path) for path in import_paths], ensure_ascii=False),
        "import_label": json.dumps(f"{len(import_paths)} 个文件", ensure_ascii=False),
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
            if route == "/ready":
                if not scene_ready.wait(120):
                    return self._send(504, "text/plain", b"")
                return self._send(200, "image/gif", READY_IMAGE)
            if route == "/scene-ready":
                scene_ready.set()
                return self._send(200, "text/plain", b"ok")
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
                # 截图过程不联网：更新检查返回固定的离线结果，状态与当前版本一致。
                if name in ("check_updates", "cached_update_status"):
                    result = {"ok": True, "data": update_status}
                elif name == "core_update_status":
                    result = {"ok": True, "data": {"state": "idle"}}
                else:
                    result = getattr(api, name)(*args)
                # 截图环境没有云识别组件：隐藏云识别入口，与未安装组件的界面一致。
                if name == "ocr_component_status" and isinstance(result.get("data"), dict):
                    result["data"]["available"] = False
            except Exception as error:
                result = {"ok": False, "error": str(error)}
            self._send(200, "application/json", json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # 同一服务会连续为多张截图提供页面；每次截图前清掉上一次的就绪信号。
    server.scene_ready = scene_ready
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
