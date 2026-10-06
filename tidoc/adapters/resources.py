"""Checks for adapter resources included with the core application."""
from __future__ import annotations

from pathlib import Path

from .loader import load_package
from .registry import SETTINGS


def verify_core_resources() -> dict:
    package_root = Path(__file__).resolve().parents[1] / "builtin_adapters"
    schema_root = Path(__file__).resolve().parents[2] / "schemas" / "team-adapter" / "1"
    required_schemas = ("manifest.schema.json", "scheme.schema.json", "fields.schema.json", "materials.schema.json", "rules.schema.json", "outputs.schema.json")
    missing = [name for name in required_schemas if not (schema_root / name).is_file()]
    if missing:
        raise RuntimeError("缺少团队适配 Schema 资源：" + ", ".join(missing))
    packages = {}
    for package_id in ("org.tidoc.generic", "org.bitfsae.reimbursement"):
        directory = package_root / package_id
        package = load_package(directory)
        if package.package_id != package_id:
            raise RuntimeError(f"内置适配包标识异常：{directory}")
        packages[package_id] = {"content_hash": package.content_hash, "files": len(package.files)}
    if not SETTINGS:
        raise RuntimeError("团队适配设置注册表为空")
    from tidoc_print.context_validation import context_validator
    context_validator()
    return {"schemas": len(required_schemas), "packages": packages}
