"""tidoc 打印导出组件（可选安装）。

重依赖（python-docx / pypdf / Pillow / reportlab）都在这个包里，核心不 import 它。
核心通过 is_available() 探测组件是否已安装，未装则相关入口置灰。

设计文档第 9 节：跨人合并、按抬头强隔离、可勾选叠加信息；默认按条目
连续生成材料 PDF，也可按材料类型分别生成旧式拼接 PDF。
"""

from __future__ import annotations

# This version is intentionally independent from the core application.  It
# starts at the last coupled release so existing v0.1.19 installations are not
# treated as newer than future component releases.
__version__ = "0.1.19"

# 组件所需的重依赖模块名
_REQUIRED = ("docx", "pypdf", "PIL", "reportlab")


def missing_dependencies() -> list[str]:
    """返回缺失的依赖模块名列表；空列表表示组件可用。"""
    import importlib.util

    missing = []
    for mod in _REQUIRED:
        if importlib.util.find_spec(mod) is None:
            missing.append(mod)
    return missing


def is_available() -> bool:
    """组件依赖是否齐备。核心据此决定打印导出入口是否可用。"""
    return not missing_dependencies()


def __getattr__(name):
    """延迟导入重依赖模块的符号：只有真正用到时才 import，
    这样核心 import tidoc_print 探测可用性时不会因缺依赖报错。"""
    exported = {
        "build_print_package": "builder",
        "PrintOptions": "builder",
        "PrintResult": "builder",
        "ANNOTATION_FIELDS": "builder",
        "PrintEntry": "models",
        "PrintItem": "models",
        "PersonProfile": "models",
    }
    if name in exported:
        import importlib
        mod = importlib.import_module(f".{exported[name]}", __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
