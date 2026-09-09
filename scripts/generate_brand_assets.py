"""从一张方形源图重新生成 tidoc 的全部品牌素材。

用法：python scripts/generate_brand_assets.py <源PNG路径>

产物（写入 tidoc/web/assets/）：
- tidoc-logo.png      512×512，Web 界面与设置页
- tidoc-logo-128.png  128×128，顶栏小图
- favicon.png         32×32，浏览器/WebView 标签图标
- tidoc-logo.ico      Windows 应用与 .tidoc 文件关联图标（16-256 多尺寸）
- tidoc-logo.icns     macOS 应用图标（16-1024 多尺寸）

源图要求：正方形、含背景（不透明）。缩放统一用 LANCZOS。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ASSETS_DIR = Path(__file__).resolve().parents[1] / "tidoc" / "web" / "assets"
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def resize(img: Image.Image, size: int) -> Image.Image:
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    source_path = Path(sys.argv[1])
    img = Image.open(source_path).convert("RGB")
    if img.width != img.height:
        raise SystemExit("源图必须是正方形。")

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    resize(img, 512).save(ASSETS_DIR / "tidoc-logo.png")
    resize(img, 128).save(ASSETS_DIR / "tidoc-logo-128.png")
    resize(img, 32).save(ASSETS_DIR / "favicon.png")

    # Pillow 会按 sizes 从最大可用帧自动缩放生成各尺寸条目
    resize(img, max(ICO_SIZES)).save(
        ASSETS_DIR / "tidoc-logo.ico",
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
    )

    icns_size = max(ICNS_SIZES)
    icns_main = resize(img, icns_size)
    icns_main.save(
        ASSETS_DIR / "tidoc-logo.icns",
        format="ICNS",
        append_images=[resize(img, size) for size in ICNS_SIZES[:-1]],
    )
    print(f"已写入 {ASSETS_DIR}")


if __name__ == "__main__":
    main()
