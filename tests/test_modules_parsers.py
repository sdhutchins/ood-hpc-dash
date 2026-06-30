"""Tests for Lmod spider parsing helpers in modules blueprint."""

from __future__ import annotations

from pathlib import Path

from blueprints.modules import (
    _categorize_module,
    _natural_sort_key,
    _parse_spider_cache,
)


def test_natural_sort_key_orders_numeric_versions() -> None:
    versions = sorted(
        ["item/10.0", "item/2.0", "item/1.0"],
        key=_natural_sort_key,
    )
    assert versions == ["item/1.0", "item/2.0", "item/10.0"]


def test_categorize_module_uses_exact_and_prefix_matches() -> None:
    categories = {
        "Armadillo": "Math/Libraries",
        "rc/": "Research Computing modules",
        "rc": "Research Computing modules",
    }

    assert _categorize_module("Armadillo", categories) == "Math/Libraries"
    assert (
        _categorize_module("rc/3DSlicer", categories)
        == "Research Computing modules"
    )
    assert _categorize_module("unknown-module", categories) == "Misc"


SAMPLE_SPIDER_LUA = """\
spiderT = {
  ["/apps/modules/all"] = {
    ["gcc"] = {
      fileT = {
        ["gcc/11.4.0"] = {
          Version = "11.4.0",
          whatis = {"GNU Compiler Collection includes C, C++, Fortran"},
        },
        ["gcc/12.2.0"] = {
          Version = "12.2.0",
          whatis = {"GNU Compiler Collection includes C, C++, Fortran"},
        },
      },
    },
    ["CUDA"] = {
      fileT = {
        ["CUDA/11.8.0"] = {
          Version = "11.8.0",
          whatis = {"CUDA toolkit"},
        },
      },
    },
  },
}
"""


def test_parse_spider_cache_extracts_modules(tmp_path: Path) -> None:
    lua_file = tmp_path / "spiderT.lua"
    lua_file.write_text(SAMPLE_SPIDER_LUA)

    modules = _parse_spider_cache(lua_file)

    assert modules is not None
    assert "gcc" in modules
    assert "CUDA" in modules
    assert modules["gcc"]["versions"] == ["gcc/11.4.0", "gcc/12.2.0"]
    assert modules["CUDA"]["versions"] == ["CUDA/11.8.0"]
    assert "GNU Compiler" in str(modules["gcc"]["description"])


def test_parse_spider_cache_returns_none_for_missing_file(
    tmp_path: Path,
) -> None:
    result = _parse_spider_cache(tmp_path / "nonexistent.lua")
    assert result is None
