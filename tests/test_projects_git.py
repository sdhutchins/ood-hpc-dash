"""Tests for project git metadata helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from blueprints.projects import _get_remote, _remote_web_url


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_get_remote_uses_origin_config_fallback(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")

    remote_name, remote_url = _get_remote(repo, None)

    assert remote_name == "origin"
    assert remote_url == "git@github.com:owner/repo.git"


def test_remote_web_url_converts_common_git_urls() -> None:
    assert (
        _remote_web_url("git@github.com:owner/repo.git")
        == "https://github.com/owner/repo"
    )
    assert (
        _remote_web_url("https://github.com/owner/repo.git")
        == "https://github.com/owner/repo"
    )
