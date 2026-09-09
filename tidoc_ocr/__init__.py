"""tidoc OCR 识别组件（可选安装）。

阿里云 OCR SDK（alibabacloud-ocr-api20210707 等）都在这个包里，核心不 import
它。核心通过 is_available() 探测组件是否已安装，未装则相关入口引导安装。

设计文档第 10 节：只在用户明确点击时调用阿里云按量计费接口；识别原文与解析
结果由核心落库保存，避免重复计费。
"""

from __future__ import annotations

# 组件版本独立于核心；0.2.0 起支持多页发票逐页识别并合并。
__version__ = "0.2.0"

# 组件所需的重依赖模块名（import 名与包名不同）
_REQUIRED = (
    "alibabacloud_ocr_api20210707",
    "alibabacloud_credentials",
    "alibabacloud_tea_openapi",
    "alibabacloud_tea_util",
    "alibabacloud_darabonba_stream",
    "pypdf",
)


def missing_dependencies() -> list[str]:
    """返回缺失的依赖模块名列表；空列表表示组件可用。"""
    import importlib.util

    missing = []
    for mod in _REQUIRED:
        if importlib.util.find_spec(mod) is None:
            missing.append(mod)
    return missing


def is_available() -> bool:
    """组件依赖是否齐备。核心据此决定 OCR 入口是否可用。"""
    return not missing_dependencies()


def __getattr__(name):
    """延迟导入重依赖模块的符号：核心 import tidoc_ocr 探测可用性时不会拉起 SDK。"""
    exported = {
        "recognize_invoice": "ocr",
        "normalize_invoice_data": "ocr",
        "clean_item_name": "ocr",
        "DEFAULT_ENDPOINT": "ocr",
        "OcrError": "ocr",
    }
    if name in exported:
        import importlib

        mod = importlib.import_module(f".{exported[name]}", __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
