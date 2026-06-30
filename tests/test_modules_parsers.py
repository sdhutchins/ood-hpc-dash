"""Tests for Lmod spider parsing helpers in modules blueprint."""

from __future__ import annotations

from blueprints.modules import (
    _categorize_module,
    _natural_sort_key,
    _parse_module_spider_output,
)

SAMPLE_SPIDER_OUTPUT = """
The following modules match your search criteria:
  gcc: gcc/11.4.0, gcc/12.2.0
    Description:
      GNU Compiler Collection
  python: python/3.10.12, python/3.11.4
    Description:
      Python language runtime
"""


def test_parse_module_spider_output_extracts_versions_and_descriptions() -> None:
    modules = _parse_module_spider_output(SAMPLE_SPIDER_OUTPUT)

    assert set(modules) == {"gcc", "python"}
    assert modules["gcc"]["versions"] == ["gcc/11.4.0", "gcc/12.2.0"]
    assert modules["python"]["versions"] == [
        "python/3.10.12",
        "python/3.11.4",
    ]
    assert "GNU Compiler Collection" in str(modules["gcc"]["description"])
    assert "Python language runtime" in str(modules["python"]["description"])


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
