"""Tests for shared utility helpers."""

from __future__ import annotations

import json
from pathlib import Path

from utils import (
    CustomJsonEncoder,
    expand_path,
    find_binary,
    load_settings,
    validate_code_editor_path,
)


def test_expand_path_expands_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert expand_path("$HOME/data") == str(tmp_path / "data")


def test_find_binary_returns_first_executable(tmp_path) -> None:
    missing = tmp_path / "missing"
    present = tmp_path / "present"
    present.write_text("#!/bin/sh\n", encoding="utf-8")
    present.chmod(0o755)

    assert find_binary([str(missing), str(present)]) == str(present)


def test_load_settings_merges_file_with_defaults(
    tmp_path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_file = config_dir / "settings.json"
    settings_file.write_text(
        json.dumps({"navbar_color": "#e3f2fd"}),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    settings = load_settings()

    assert settings["navbar_color"] == "#e3f2fd"
    assert "conda_envs_paths" in settings
    assert "project_directories" in settings


def test_validate_code_editor_path_requires_allowed_existing_directory(
    tmp_path,
    monkeypatch,
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    editor_root = allowed_root / "project"
    editor_root.mkdir()

    monkeypatch.setenv("OOD_HPC_DASH_EDITOR_ROOTS", str(allowed_root))

    valid_path, valid_error = validate_code_editor_path(str(editor_root))
    invalid_path, invalid_error = validate_code_editor_path(
        str(tmp_path / "missing")
    )

    assert valid_path == editor_root
    assert valid_error is None
    assert invalid_path is None
    assert invalid_error is not None


def test_custom_json_encoder_handles_path_and_datetime() -> None:
    from datetime import datetime

    payload = {
        "path": Path("/tmp/example"),
        "created": datetime(2024, 1, 1, 12, 0, 0),
    }
    encoded = json.dumps(payload, cls=CustomJsonEncoder)

    assert '"/tmp/example"' in encoded
    assert "2024-01-01T12:00:00" in encoded
