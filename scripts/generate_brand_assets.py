"""从一张方形源图重新生成 Tidoc 的全部品牌素材。

用法：python scripts/generate_brand_assets.py <源PNG路径>

产物：
- icon/source/icon-master.png     512×512，完整方形母版
- icon/windows/icon-rounded.png   512×512，透明圆角 Windows 图标
- icon/windows/icon.ico           Windows 应用与 .tidoc 文件图标（16-256）
- icon/macos/icon-1024.png         1024×1024，完整方形 macOS 图标
- icon/macos/icon.icns             macOS 应用与 .tidoc 文件图标（16-1024）
- tidoc/web/assets/                Web 界面用 512、128、32 像素 PNG

源图要求：正方形、含背景（不透明）。Windows 由素材自己带透明圆角，
macOS 保留完整方形交给系统裁切；缩放统一用 LANCZOS。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT_DIR = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT_DIR / "icon"
WEB_ASSETS_DIR = ROOT_DIR / "tidoc" / "web" / "assets"
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]
MASTER_SIZE = 512
WINDOWS_CORNER_RADIUS = 68
MASK_SCALE = 8


def resize(img: Image.Image, size: int) -> Image.Image:
    return img.resize((size, size), Image.LANCZOS)


def rounded_windows_icon(img: Image.Image) -> Image.Image:
    """Apply a supersampled transparent mask suitable for Windows icons."""
    size = img.width
    scale = MASK_SCALE
    radius = round(size * WINDOWS_CORNER_RADIUS / MASTER_SIZE)
    mask_large = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask_large).rounded_rectangle(
        (0, 0, size * scale - 1, size * scale - 1),
        radius=radius * scale,
        fill=255,
    )
    mask = mask_large.resize((size, size), Image.LANCZOS)
    rounded = img.convert("RGBA")
    rounded.putalpha(mask)
    return rounded


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    source_path = Path(sys.argv[1])
    img = Image.open(source_path).convert("RGB")
    if img.width != img.height:
        raise SystemExit("源图必须是正方形。")

    source_dir = ICON_DIR / "source"
    windows_dir = ICON_DIR / "windows"
    macos_dir = ICON_DIR / "macos"
    for output_dir in (source_dir, windows_dir, macos_dir, WEB_ASSETS_DIR):
        output_dir.mkdir(parents=True, exist_ok=True)

    master = resize(img, MASTER_SIZE)
    master.save(source_dir / "icon-master.png")
    master.save(WEB_ASSETS_DIR / "tidoc-logo.png")
    resize(master, 128).save(WEB_ASSETS_DIR / "tidoc-logo-128.png")
    resize(master, 32).save(WEB_ASSETS_DIR / "favicon.png")

    windows_icon = rounded_windows_icon(master)
    windows_icon.save(windows_dir / "icon-rounded.png")
    # Pillow 会按 sizes 从最大可用帧自动缩放生成各尺寸条目。
    windows_icon.save(
        windows_dir / "icon.ico",
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
    )

    icns_size = max(ICNS_SIZES)
    icns_main = resize(master, icns_size)
    icns_main.save(macos_dir / "icon-1024.png")
    icns_main.save(
        macos_dir / "icon.icns",
        format="ICNS",
        append_images=[resize(master, size) for size in ICNS_SIZES[:-1]],
    )
    print(f"已写入 {ICON_DIR} 和 {WEB_ASSETS_DIR}")


if __name__ == "__main__":
    main()
