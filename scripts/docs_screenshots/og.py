"""生成文档站的分享图 og-image.png（1200×630）。

用无界面的 Chrome 渲染一小段 HTML：深色渐变底、官方 TiDoc 字标、标题与说明，右侧放本次
重新生成的深色主界面截图。截图以 data URI 内嵌，页面从临时文件打开，不联网。
"""

from __future__ import annotations

import base64
import re
import subprocess
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
WORDMARK = ROOT_DIR / "docs" / "public" / "tidoc-wordmark.svg"
FALLBACK_WORDMARK = ROOT_DIR / "tidoc" / "web" / "assets" / "tidoc-wordmark.svg"

WIDTH, HEIGHT = 1200, 630

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; width: %(width)spx; height: %(height)spx; overflow: hidden; }
  body {
    font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    background:
      radial-gradient(760px 520px at 6%% -8%%, rgba(22, 87, 252, 0.30), transparent 62%%),
      radial-gradient(620px 420px at 74%% 118%%, rgba(30, 111, 255, 0.16), transparent 60%%),
      linear-gradient(132deg, #111c33 0%%, #0a1220 58%%, #0b1526 100%%);
    color: #f5f7fb;
    position: relative;
  }
  .glow {
    position: absolute; right: 120px; top: 70px; width: 520px; height: 480px;
    background: radial-gradient(closest-side, rgba(22, 87, 252, 0.20), transparent);
    filter: blur(6px);
  }
  .copy { position: absolute; left: 76px; top: 92px; width: 480px; }
  .wordmark { color: #fff; margin-bottom: 44px; }
  .wordmark svg { display: block; height: 76px; width: auto; }
  h1 {
    margin: 0 0 22px; font-size: 48px; line-height: 1.16; font-weight: 700;
    letter-spacing: 0.5px; color: #ffffff;
  }
  p.lead {
    margin: 0; font-size: 22px; line-height: 1.6; color: #bdcce3; font-weight: 400;
    letter-spacing: 0.2px;
  }
  .foot {
    position: absolute; left: 78px; bottom: 52px;
    font-size: 18px; line-height: 1.9; color: #92a5c2; letter-spacing: 0.3px;
  }
  .foot b { color: #c9d7ec; font-weight: 500; }
  .shot {
    position: absolute; left: 566px; top: 116px; width: 620px; height: 445px;
    border-radius: 14px; overflow: hidden;
    border: 1px solid rgba(160, 190, 240, 0.22);
    box-shadow: 0 30px 70px rgba(2, 8, 20, 0.62), 0 2px 0 rgba(255, 255, 255, 0.05) inset;
  }
  .shot img { display: block; width: 100%%; height: 100%%; object-fit: cover; object-position: left top; }
</style>
</head>
<body>
  <div class="glow"></div>
  <div class="copy">
    <div class="wordmark">%(wordmark)s</div>
    <h1>报账发票与材料整理</h1>
    <p class="lead">发票、付款截图与查验单归入同一条目，<br />缺项直接标出。</p>
  </div>
  <div class="shot"><img src="%(screenshot)s" alt="" /></div>
  <div class="foot"><b>Windows · macOS</b>　识别在本机完成<br />tidoc.bitfsae.com</div>
</body>
</html>
"""


def _wordmark_svg() -> str:
    path = WORDMARK if WORDMARK.is_file() else FALLBACK_WORDMARK
    svg = path.read_text(encoding="utf-8")
    # 内联时去掉 XML 声明和注释，保留 <svg>…</svg>。
    svg = re.sub(r"<\?xml[^>]*\?>\s*", "", svg, count=1)
    svg = re.sub(r"<!--.*?-->\s*", "", svg, flags=re.S)
    svg = svg.strip()
    if not svg.startswith("<svg"):
        raise SystemExit(f"{path} 不是可内联的 SVG。")
    return svg


def generate(chrome: str, target: Path, screenshot: Path, env: dict[str, str]) -> tuple[int, int]:
    """渲染分享图并检查尺寸；返回最终像素尺寸。"""
    from PIL import Image

    if not screenshot.is_file():
        raise SystemExit(f"缺少深色主界面截图：{screenshot}")
    data = base64.b64encode(screenshot.read_bytes()).decode("ascii")
    html = HTML % {
        "width": WIDTH, "height": HEIGHT,
        "wordmark": _wordmark_svg(),
        "screenshot": f"data:image/png;base64,{data}",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tidoc-og-") as temp:
        page = Path(temp) / "og.html"
        page.write_text(html, encoding="utf-8")
        raw = Path(temp) / "og.png"
        subprocess.run(
            [
                chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                "--use-mock-keychain", "--force-device-scale-factor=1",
                f"--window-size={WIDTH},{HEIGHT}", "--virtual-time-budget=4000",
                f"--screenshot={raw}", page.as_uri(),
            ],
            check=True, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
        image = Image.open(raw)
        if image.size != (WIDTH, HEIGHT):
            raise SystemExit(f"分享图尺寸是 {image.size}，应为 {(WIDTH, HEIGHT)}")
        image.convert("RGB").save(target, optimize=True)
        return image.size
