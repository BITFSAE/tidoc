import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _png_header(path: Path) -> tuple[int, int, int]:
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, color_type, _compression, _filter, _interlace = (
        struct.unpack(">IIBBBBB", raw[16:29])
    )
    return width, height, color_type


def test_platform_icon_sources_have_expected_shape_and_alpha_contract():
    assert _png_header(ROOT / "icon/source/icon-master.png") == (512, 512, 2)
    assert _png_header(ROOT / "icon/windows/icon-rounded.png") == (512, 512, 6)
    assert _png_header(ROOT / "icon/macos/icon-1024.png") == (1024, 1024, 2)

    ico = (ROOT / "icon/windows/icon.ico").read_bytes()
    assert struct.unpack("<HHH", ico[:6]) == (0, 1, 7)
    assert (ROOT / "icon/macos/icon.icns").read_bytes().startswith(b"icns")


def test_release_builds_use_platform_icons_and_register_tidoc_file_icons():
    workflow = (ROOT / ".github/workflows/release.yml").read_text("utf-8")
    installer = (ROOT / "packaging/windows/tidoc.iss").read_text("utf-8")

    assert '--icon "icon/windows/icon.ico"' in workflow
    assert '--icon "icon/macos/icon.icns"' in workflow
    assert "--argv-emulation" in workflow
    assert 'CFBundleTypeIconFile string tidoc-file.icns' in workflow
    assert 'UTTypeIdentifier string com.bitfsae.tidoc.bindle' in workflow
    assert 'public.filename-extension:0 string tidoc' in workflow
    assert "SetupIconFile=..\\..\\icon\\windows\\icon.ico" in installer
    assert 'DestName: "tidoc-file.ico"' in installer
    assert 'ValueData: "{app}\\tidoc-file.ico,0"' in installer


def test_brand_generator_preserves_platform_specific_corner_behavior():
    source = (ROOT / "scripts/generate_brand_assets.py").read_text("utf-8")

    assert "rounded_windows_icon(master)" in source
    assert 'windows_dir / "icon-rounded.png"' in source
    assert 'windows_dir / "icon.ico"' in source
    assert 'icns_main.save(macos_dir / "icon-1024.png")' in source
    assert 'macos_dir / "icon.icns"' in source
