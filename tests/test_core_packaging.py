"""核心安装包的打包约定：二维码取票依赖、Pillow 精简和 CI 的 Python 版本。

这些检查读的是仓库里的 requirements 和 workflow 文本，目的是让下面几类改动在合并前就失败，
而不是等到发布构建才暴露：

- 核心依赖（Pillow、zxing-cpp）的版本被改成没有预编译包的范围；
- 两个平台的核心构建排除了不同的 Pillow 模块，或者排除了二维码识别要用的模块；
- 核心的 Pillow 精简参数被复制到打印、识别组件的构建里（组件是独立构建，要用完整的 Pillow）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

CORE_BUILD_STEPS = ("Build Windows core", "Build macOS core app")
COMPONENT_BUILD_STEPS = (
    "Build Windows print component",
    "Build Windows OCR component",
    "Build macOS print component",
    "Build macOS OCR component",
)

# 二维码识别（tidoc/services/qr_fetch.py）和材料校验用得到的 Pillow 模块，以及 zxing-cpp 本身。
# 排除其中任何一个都会让打包后的应用在运行时失败或丢失常见图片格式。
MODULES_THE_CORE_NEEDS = {
    "zxingcpp",
    "PIL",
    "PIL.Image",
    "PIL.ImageFile",
    "PIL.ImageOps",
    "PIL.ImagePalette",
    "PIL.ExifTags",
    "PIL._imaging",
    "PIL._webp",
    "PIL.PngImagePlugin",
    "PIL.JpegImagePlugin",
    "PIL.MpoImagePlugin",
    "PIL.WebPImagePlugin",
    "PIL.GifImagePlugin",
    "PIL.BmpImagePlugin",
    "PIL.TiffImagePlugin",
}


def _steps(text: str) -> dict[str, str]:
    """按 `- name:` 把 workflow 切成步骤（只用于这几个固定缩进的文件）。"""
    steps = {}
    for chunk in re.split(r"(?m)^      - ", text)[1:]:
        match = re.match(r"name:\s*(.+)", chunk)
        if match:
            steps[match.group(1).strip()] = chunk
    return steps


def _excluded_modules(step: str) -> set[str]:
    return set(re.findall(r"--exclude-module\s+(\S+)", step))


def _pin(requirements: str, package: str) -> str:
    match = re.search(rf"(?mi)^{re.escape(package)}==(\S+)", requirements)
    assert match, f"{package} must be pinned with == in the requirements file"
    return match.group(1)


def test_core_requirements_pin_the_qr_decoder_and_image_library():
    core = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    print_component = (ROOT / "requirements-print.txt").read_text(encoding="utf-8")

    assert _pin(core, "zxing-cpp")
    # 两份清单同时被 CI 安装，Pillow 版本必须一致，否则 pip 会报冲突。
    assert _pin(core, "Pillow") == _pin(print_component, "Pillow")


def test_dev_requirements_install_the_core_requirements():
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    assert re.search(r"(?m)^-r requirements\.txt\s*$", dev)


def test_workflows_use_a_python_that_has_prebuilt_wheels():
    # Pillow 12 需要 3.10+，zxing-cpp 在 3.10 / 3.11 有各自的预编译包，3.12+ 用 abi3 包。
    # 更老的版本会让 pip 回落到源码编译（需要 C++ 工具链），所以版本下限是 3.10。
    versions = re.findall(r'python-version:\s*[\[]?\s*"?(\d+)\.(\d+)', RELEASE + CI)

    assert versions, "no python-version found in the workflows"
    assert all((int(major), int(minor)) >= (3, 10) for major, minor in versions)


def test_workflows_refuse_a_source_build_of_the_core_binary_dependencies():
    # 没有预编译包时让 pip 直接报错，而不是在 runner 上静默编译一份和用户拿到的不同的 zxing-cpp。
    for name, text in (("ci.yml", CI), ("release.yml", RELEASE)):
        match = re.search(r"(?m)^  PIP_ONLY_BINARY:\s*(\S+)", text)
        assert match, f"{name} must set PIP_ONLY_BINARY at workflow level"
        packages = {item.lower() for item in match.group(1).split(",")}
        assert {"zxing-cpp", "pillow"} <= packages, f"{name}: {packages}"


def test_release_installs_pillow_only_through_the_pinned_requirements():
    for line in RELEASE.splitlines():
        if "pip install" in line:
            tokens = [token.lower() for token in line.split()]
            assert "pillow" not in tokens, f"unpinned Pillow in: {line.strip()}"


def test_core_builds_share_one_pillow_exclusion_list():
    steps = _steps(RELEASE)
    lists = {name: _excluded_modules(steps[name]) for name in CORE_BUILD_STEPS}

    windows, macos = (lists[name] for name in CORE_BUILD_STEPS)
    assert windows, "the core build should trim the Pillow modules it never reaches"
    assert windows == macos, "Windows and macOS core builds must exclude the same modules"


def test_core_builds_keep_every_module_the_qr_decoder_needs():
    steps = _steps(RELEASE)

    for name in CORE_BUILD_STEPS:
        excluded = _excluded_modules(steps[name])
        assert all(module.startswith("PIL.") for module in excluded), excluded
        assert not excluded & MODULES_THE_CORE_NEEDS, excluded & MODULES_THE_CORE_NEEDS


def test_component_builds_do_not_inherit_the_core_exclusions():
    steps = _steps(RELEASE)

    for name in COMPONENT_BUILD_STEPS:
        assert name in steps, f"{name} step is missing from release.yml"
        assert not _excluded_modules(steps[name]), f"{name} must keep the full Pillow"


def test_core_self_test_runs_on_both_packaged_cores():
    # 自检里的二维码往返是打包问题（缺原生库、缺 python3.dll、被误排除的模块）的唯一出口。
    assert "tidoc.exe\" -ArgumentList \"--self-test\"" in RELEASE
    assert "tidoc.app/Contents/MacOS/tidoc --self-test" in RELEASE
