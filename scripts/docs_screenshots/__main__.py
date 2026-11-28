"""重新生成文档站的界面截图和分享图。

用法（在仓库根目录）：
    python -m scripts.docs_screenshots [--out docs/public/images] [--chrome <浏览器路径>] [--only 名称 ...] [--no-og]

做法：在临时目录里建一份全新的数据，写入虚构条目（见 data.py），用本机 HTTP 服务把真实的
tidoc/web 前端接到真实的 Api 上，再用无界面的 Chrome 或 Edge 按固定窗口大小截图。需要交互的
窗口由 bridge.py 里的场景脚本打开并隔离。全程不读写真实的数据目录，也不联网（更新检查返回
固定的离线结果，自动检查已关闭）。

产物（--out 目录下）：
- main-overview.png / main-overview-dark.png   主界面，浅色与深色，2400×1724（2x 无损）
- hero-cards.png / hero-cards-dark.png         首页右上角三张卡片，1132×836（2x 无损），四角透明
- detail.png                                  条目详情（含查验单的次数与时间）
- warning-detail.png                          带识别提醒的条目详情，只留关键信息一栏
- check-menu.png                              卡片上的识别提醒弹层
- print-dialog.png                            打印导出窗口，默认选项
- print-options.png                           打印导出窗口，展开扩展选项
- payment-ai-settings.png                     付款截图 AI 识别设置，单独摆在一块面板上
- bindle-preview.png                          绑定包导入预览，含新增和已有条目
- settings.png                                设置窗口，开发者选项收起
- scheme-settings.png                         报账方案设置窗口
- card-actions.png                            锂电池组卡片，含提醒、已修改、实付差异和操作按钮
- first-run-scheme.png                        未配置数据时的选择报账方案窗口
- components.png                              软件与组件更新窗口，固定的离线状态
- export-dialog.png                           导出窗口，已选 9 月第一批条目
- batch-edit.png                              编辑批次窗口，含示例批次备注
- batch-toolbar.png                           选中 3 条后的批量操作条
- status-views.png                            工具条、批次、状态视图和统计条
- grouped-entries.png                         按报账人分组的通栏标题与批次统计
- import-preview.png                          批量导入预览，含 XML+PDF、只有 PDF、付款截图和查验单

另生成 docs/public/og-image.png（1200×630，见 og.py），内容取自本次的深色主界面图。

依赖：本机装有 Chrome 或 Edge，Python 里装有 Pillow 和 pypdf（requirements-dev.txt）。浏览器路径
也可以用环境变量 TIDOC_CHROME 指定。界面改动后这些图要重新生成，并检查改动是否符合预期再提交。
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
OG_IMAGE = ROOT_DIR / "docs" / "public" / "og-image.png"

SCALE = 1.5
HOME_SCALE = 2.0  # 首页主界面与卡片图使用 2x 高分无损 PNG，保证高分屏下文字与细线清晰
WINDOW = (1200, 862)  # 主界面窗口，2x 后为 2400×1724
# 首页卡片图的三张卡片：CSS 宽 566、高 135.33 / 148 / 134.67，2x 后为 1132×836，纵横比与 home.css 一致。
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
    theme: str = "light"
    scene: str = ""
    kind: str = "page"  # page | sprite | dialog | isolate
    size: tuple[int, int] | None = None
    expect_width: int = 0  # 弹窗的 CSS 宽度；用于确认弹窗真的打开且内容已替换
    full_width: bool = False  # 有意占满窗口宽度的隔离图，不做宽度上限检查
    scale: float = SCALE
    lossless: bool = False  # True 时保持全彩无损 PNG，不做 256 色抖动量化

    @property
    def window(self) -> tuple[int, int]:
        if self.size:
            return self.size
        if self.kind == "sprite":
            return SPRITE_WINDOW
        if self.scene in ("detail", "warning-detail"):
            return (1200, 1700)
        if self.kind in ("dialog", "isolate"):
            return (1200, 1400)
        return WINDOW

    @property
    def query(self) -> str:
        params = f"?theme={self.theme}"
        if self.kind == "sprite":
            params += "&sprite=1"
        elif self.scene:
            params += f"&dialog={self.scene}"
        return params


SHOTS = [
    Shot("main-overview", "light", scale=HOME_SCALE, lossless=True),
    Shot("main-overview-dark", "dark", scale=HOME_SCALE, lossless=True),
    Shot("hero-cards", "light", kind="sprite", scale=HOME_SCALE, lossless=True),
    Shot("hero-cards-dark", "dark", kind="sprite", scale=HOME_SCALE, lossless=True),
    Shot("detail", scene="detail", kind="dialog", expect_width=880, size=(1200, 1700)),
    Shot("warning-detail", scene="warning-detail", kind="dialog", size=(1200, 1700)),
    Shot("check-menu", scene="check-menu", kind="isolate", lossless=True),
    Shot("print-dialog", scene="print", kind="dialog", expect_width=880),
    Shot("print-options", scene="print-options", kind="dialog", expect_width=880),
    Shot("payment-ai-settings", scene="payment-ai", kind="dialog"),
    Shot("bindle-batch", scene="bindle-batch", kind="dialog", expect_width=880),
    Shot("bindle-preview", scene="bindle", kind="dialog", expect_width=880),
    Shot("settings", scene="settings", kind="dialog", expect_width=880, size=(1200, 1950)),
    Shot("scheme-settings", scene="scheme", kind="dialog", expect_width=880, size=(1200, 1400)),
    Shot("card-actions", scene="card-actions", kind="isolate"),
    Shot("first-run-scheme", scene="first-run", kind="dialog", expect_width=680, size=(1200, 1000)),
    Shot("components", scene="components", kind="dialog", expect_width=880, size=(1200, 1100)),
    Shot("export-dialog", scene="export", kind="dialog", expect_width=880, size=(1200, 900)),
    Shot("batch-edit", scene="batch-edit", kind="dialog", expect_width=680, size=(1200, 780)),
    Shot("batch-toolbar", scene="batch-toolbar", kind="isolate"),
    Shot("status-views", scene="status-views", kind="isolate", full_width=True),
    Shot("grouped-entries", scene="grouped-entries", lossless=True),
    Shot("import-preview", scene="import-preview", kind="dialog", expect_width=880, size=(1200, 1600)),
]


class CaptureRace(RuntimeError):
    """截图时界面还没准备好：裁剪结果与预期的窗口尺寸不符，需要重新截一次。"""


def find_chrome(explicit: str | None) -> str:
    for candidate in (explicit, os.environ.get("TIDOC_CHROME"), *CHROME_CANDIDATES,
                      shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("msedge")):
        if candidate and Path(candidate).is_file():
            return candidate
    sys.exit("找不到 Chrome 或 Edge：用 --chrome 或环境变量 TIDOC_CHROME 指定可执行文件。")


def capture(chrome: str, url: str, shot: Shot, target: Path, env: dict[str, str], profile: Path) -> None:
    width, height = shot.window
    # 不用 virtual-time-budget：页面用 /ready 图片推迟 load 事件，Chrome 会等到界面真正就绪再截。
    command = [
        chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        "--use-mock-keychain", "--run-all-compositor-stages-before-draw", "--force-prefers-reduced-motion",
        f"--user-data-dir={profile}", f"--force-device-scale-factor={shot.scale}",
        f"--window-size={width},{height}",
        f"--screenshot={target}", url + shot.query,
    ]
    if shot.kind == "sprite":
        command.insert(1, "--default-background-color=00000000")  # 透明背景，四角保持透明
    subprocess.run(command, check=True, env=env, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def _bright_bounds(image):
    """浅色弹窗比遮罩后的背景亮，取亮部的包围盒。"""
    return image.convert("L").point(lambda value: 255 if value > 225 else 0).getbbox()


def _content_bounds(image, tolerance: int = 10):
    """隔离图铺在同色底上：以左上角为底色，取与底色不同的部分的包围盒。"""
    from PIL import Image, ImageChops

    rgb = image.convert("RGB")
    background = Image.new("RGB", rgb.size, rgb.getpixel((2, 2)))
    diff = ImageChops.difference(rgb, background).convert("L")
    return diff.point(lambda value: 255 if value > tolerance else 0).getbbox()


def save(raw: Path, final: Path, shot: Shot) -> tuple[int, int]:
    """裁掉弹窗或隔离块周围的界面，并按类型保存。返回最终尺寸。

    裁剪结果与预期不符时抛出 CaptureRace，由调用方重新截图；这样弹窗没来得及打开时
    不会悄悄生成一张整页或半张加载中的图。
    """
    from PIL import Image

    image = Image.open(raw)
    if shot.kind in ("dialog", "isolate"):
        bounds = _content_bounds(image) if shot.kind == "isolate" else _bright_bounds(image)
        if not bounds:
            raise CaptureRace("没有找到要保留的界面")
        image = image.crop(bounds)
        width = image.width
        if shot.expect_width:
            expected = round(shot.expect_width * shot.scale)
            if abs(width - expected) > 10:
                raise CaptureRace(f"裁出的宽度 {width}px，与弹窗预期 {expected}px 不符")
        if not shot.full_width and width >= shot.window[0] * shot.scale * 0.92:
            raise CaptureRace(f"裁出的宽度 {width}px 接近整窗，弹窗或隔离块可能还没出现")
        if width < 120 or image.height < 50:
            raise CaptureRace(f"裁剪结果过小：{width}×{image.height}")
    elif shot.kind == "page":
        expected = (round(shot.window[0] * shot.scale), round(shot.window[1] * shot.scale))
        if abs(image.width - expected[0]) > 2 or abs(image.height - expected[1]) > 2:
            raise CaptureRace(f"整页尺寸 {image.size} 与预期 {expected} 不符")

    if shot.kind == "sprite" or shot.lossless:
        image.save(final, optimize=True)
    else:
        image.convert("RGB").quantize(
            colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG
        ).save(final, optimize=True)
    return image.size


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="重新生成文档站界面截图和分享图")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录，默认 docs/public/images")
    parser.add_argument("--chrome", help="Chrome 或 Edge 可执行文件路径")
    parser.add_argument("--only", nargs="+", choices=[shot.name for shot in SHOTS], help="只生成指定的图")
    parser.add_argument("--no-og", action="store_true", help="不重新生成 docs/public/og-image.png")
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
        bindle_path = data.seed_bindle_preview(api, temp / "files") if any(shot.scene == "bindle" for shot in shots) else ""
        bindle_batch_paths = data.seed_bindle_batch(api, temp / "batch-bindles") if any(shot.scene == "bindle-batch" for shot in shots) else []
        import_paths = data.seed_import_preview(temp / "import") if any(shot.scene == "import-preview" for shot in shots) else []
        server = make_server(api, SPRITE_TITLES, SPRITE_HEIGHTS, SPRITE_WIDTH,
                             bindle_path=bindle_path, bindle_batch_paths=bindle_batch_paths, import_paths=import_paths)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        # 首次使用：另一个空数据目录，前端会自己打开「选择报账方案」。
        first_run_url = ""
        first_run_server = None
        if any(shot.scene == "first-run" for shot in shots):
            first_run_server = make_server(
                Api(str(temp / "first-run-data")), SPRITE_TITLES, SPRITE_HEIGHTS, SPRITE_WIDTH
            )
            first_run_url = f"http://127.0.0.1:{first_run_server.server_address[1]}/"
        args.out.mkdir(parents=True, exist_ok=True)
        try:
            for shot in shots:
                target_url = first_run_url if shot.scene == "first-run" else url
                target_server = first_run_server if shot.scene == "first-run" else server
                final = args.out / f"{shot.name}.png"
                for attempt in range(3):
                    raw = temp / f"{shot.name}-{attempt}.png"
                    target_server.scene_ready.clear()
                    capture(chrome, target_url, shot, raw, browser_env, temp / f"chrome-{shot.name}-{attempt}")
                    try:
                        width, height = save(raw, final, shot)
                    except CaptureRace as error:
                        if attempt == 2:
                            raise SystemExit(f"{shot.name}: {error}")
                        print(f"  {shot.name} 未就绪，重新截图（{error}）")
                        continue
                    break
                print(f"已生成 {final} {width}×{height}")
        finally:
            server.shutdown()
            if first_run_server is not None:
                first_run_server.shutdown()

    if not args.no_og:
        dark = args.out / "main-overview-dark.png"
        # 分享图要用本次的深色主界面图；只生成别的截图时沿用现有文件，避免拿旧图重做。
        if args.only and "main-overview-dark" not in args.only:
            print("跳过分享图：本次没有重新生成深色主界面图")
        elif not dark.is_file():
            print("跳过分享图：缺少 main-overview-dark.png")
        else:
            from . import og

            og.generate(chrome, OG_IMAGE, dark, browser_env)
            print(f"已生成 {OG_IMAGE}")


if __name__ == "__main__":
    main()
