"""重新生成文档站首页和概念页用的界面截图。

用法（在仓库根目录）：
    python -m scripts.docs_screenshots [--out docs/public/images] [--chrome <浏览器路径>] [--only 名称 ...]

做法：在临时目录里建一份全新的数据，写入虚构条目（见 data.py），用本机 HTTP 服务把真实的
tidoc/web 前端接到真实的 Api 上，再用无界面的 Chrome 或 Edge 按固定窗口大小截图。
全程不读写真实的数据目录，也不联网（自动检查更新已关闭）。

产物（--out 目录下）：
- main-overview.png / main-overview-dark.png   主界面，浅色与深色，1800×1293
- hero-cards.png / hero-cards-dark.png         首页右上角三张卡片，849×627，四角透明

依赖：本机装有 Chrome 或 Edge，Python 里装有 Pillow（requirements-dev.txt）。浏览器路径也可以用
环境变量 TIDOC_CHROME 指定。界面改动后这些图要重新生成，并检查改动是否符合预期再提交。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT_DIR / "docs" / "public" / "images"

SCALE = 1.5
WINDOW = (1200, 862)  # 主界面窗口，1.5 倍后正好 1800×1293
# 首页卡片图的三张卡片：宽 849/1.5，高度取自原图 203、222、202 像素，卡片内容在其中垂直居中。
SPRITE_TITLES = ["螺丝刀套装", "锂电池组", "数显游标卡尺"]
SPRITE_HEIGHTS = [203 / SCALE, 222 / SCALE, 202 / SCALE]
SPRITE_WIDTH = 849 / SCALE
SPRITE_WINDOW = (round(SPRITE_WIDTH), round(sum(SPRITE_HEIGHTS)))

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


@dataclass(frozen=True)
class Shot:
    name: str
    theme: str
    sprite: bool = False

    @property
    def window(self) -> tuple[int, int]:
        return SPRITE_WINDOW if self.sprite else WINDOW

    @property
    def query(self) -> str:
        return f"?theme={self.theme}" + ("&sprite=1" if self.sprite else "")


SHOTS = [
    Shot("main-overview", "light"),
    Shot("main-overview-dark", "dark"),
    Shot("hero-cards", "light", sprite=True),
    Shot("hero-cards-dark", "dark", sprite=True),
]


def find_chrome(explicit: str | None) -> str:
    for candidate in (explicit, os.environ.get("TIDOC_CHROME"), *CHROME_CANDIDATES,
                      shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("msedge")):
        if candidate and Path(candidate).is_file():
            return candidate
    sys.exit("找不到 Chrome 或 Edge：用 --chrome 或环境变量 TIDOC_CHROME 指定可执行文件。")


def capture(chrome: str, url: str, shot: Shot, target: Path, env: dict[str, str]) -> None:
    width, height = shot.window
    command = [
        chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        "--use-mock-keychain", f"--force-device-scale-factor={SCALE}",
        f"--window-size={width},{height}", "--virtual-time-budget=8000",
        f"--screenshot={target}", url + shot.query,
    ]
    if shot.sprite:
        command.insert(1, "--default-background-color=00000000")  # 透明背景，四角保持透明
    subprocess.run(command, check=True, env=env, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def save(raw: Path, final: Path, sprite: bool) -> None:
    """卡片图保持完整色彩与透明；主界面图压成 256 色调色板，体积小得多。"""
    from PIL import Image

    image = Image.open(raw)
    if sprite:
        image.save(final, optimize=True)
        return
    image.convert("RGB").quantize(
        colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG
    ).save(final, optimize=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="重新生成文档站界面截图")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录，默认 docs/public/images")
    parser.add_argument("--chrome", help="Chrome 或 Edge 可执行文件路径")
    parser.add_argument("--only", nargs="+", choices=[shot.name for shot in SHOTS], help="只生成指定的图")
    args = parser.parse_args(argv)
    chrome = find_chrome(args.chrome)
    shots = [shot for shot in SHOTS if not args.only or shot.name in args.only]

    with tempfile.TemporaryDirectory(prefix="tidoc-shots-") as temp:
        temp = Path(temp)
        browser_env = os.environ.copy()  # 浏览器沿用真实的用户目录，否则 macOS 上会卡在钥匙串
        # 数据根显式指定为临时目录；再把用户目录也指过去，保证不碰真实数据。
        home = temp / "home"
        home.mkdir()
        for key in ("HOME", "USERPROFILE", "APPDATA", "XDG_DATA_HOME"):
            os.environ[key] = str(home)
        sys.path.insert(0, str(ROOT_DIR))
        from tidoc.api import Api

        from . import data
        from .bridge import make_server

        api = Api(str(temp / "data"))
        # 关闭自动检查更新，截图过程不联网。
        api.set_app_preference("tidoc.update.autoCheck", "0")
        print("写入虚构数据…")
        data.seed(api, temp / "files")
        server = make_server(api, SPRITE_TITLES, SPRITE_HEIGHTS, SPRITE_WIDTH)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        args.out.mkdir(parents=True, exist_ok=True)
        try:
            for shot in shots:
                raw = temp / f"{shot.name}.png"
                capture(chrome, url, shot, raw, browser_env)
                save(raw, args.out / f"{shot.name}.png", shot.sprite)
                print(f"已生成 {args.out / (shot.name + '.png')}")
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
