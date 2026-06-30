"""Tests for conda environment helpers in envs blueprint."""

from __future__ import annotations

from blueprints.envs import (
    _categorize_env,
    _group_envs,
    _parse_conda_history,
    _parse_conda_package_record,
    _resolve_env_directory,
)

SAMPLE_CONDA_HISTORY = """
==> 2024-01-01 <==
+defaults::python-3.10.12-h1234567_0
+defaults::numpy-1.24.0-py310h1234567_0
-defaults::openssl-1.1.1w-h1234567_0
"""


def test_parse_conda_package_record_splits_name_version_build() -> None:
    parsed = _parse_conda_package_record("defaults::python-3.10.12-h1234567_0")

    assert parsed == ("python", "python=3.10.12=h1234567_0")


def test_parse_conda_history_tracks_add_and_remove() -> None:
    dependencies = _parse_conda_history(SAMPLE_CONDA_HISTORY)

    assert "python=3.10.12=h1234567_0" in dependencies
    assert "numpy=1.24.0=py310h1234567_0" in dependencies
    assert not any(dep.startswith("openssl=") for dep in dependencies)


def test_categorize_env_prefers_scratch_over_tool_labels() -> None:
    assert _categorize_env("/home/user/.conda/envs/analysis") == "Conda"
    assert _categorize_env("/scratch/user/mamba/envs/work") == "Scratch"
    assert _categorize_env("/scratch/user/project/env") == "Scratch"


def test_group_envs_sorts_categories_and_names() -> None:
    envs = [
        {"name": "beta", "path": "/home/user/.conda/envs/beta"},
        {"name": "alpha", "path": "/home/user/.conda/envs/alpha"},
        {"name": "scratch-env", "path": "/scratch/user/scratch-env"},
    ]
    grouped, order = _group_envs(envs)

    assert order[:2] == ["Conda", "Scratch"]
    assert [env["name"] for env in grouped["Conda"]] == ["alpha", "beta"]


def test_resolve_env_directory_normalizes_trailing_slash(tmp_path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()

    assert _resolve_env_directory(f"{env_dir}/") == env_dir.resolve()
