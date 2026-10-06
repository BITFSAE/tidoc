from pathlib import Path

from tidoc.adapters.resources import verify_core_resources


def test_core_adapter_resources_are_loadable():
    result = verify_core_resources()
    assert result["schemas"] == 6
    assert set(result["packages"]) == {"org.tidoc.generic", "org.bitfsae.reimbursement"}
    assert all(value["files"] >= 7 for value in result["packages"].values())


def test_source_and_resource_paths_are_stable():
    import tidoc.adapters.loader as loader

    root = Path(loader.__file__).resolve().parents[2]
    assert loader.SCHEMA_DIR == root / "schemas" / "team-adapter" / "1"
    assert (root / "tidoc" / "builtin_adapters" / "org.tidoc.generic" / "manifest.json").is_file()
