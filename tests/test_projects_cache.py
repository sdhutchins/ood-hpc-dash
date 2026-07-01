"""Tests for project monitor cache behavior."""

from __future__ import annotations

import time

from blueprints.projects import _collect_projects_data


def test_collect_projects_uses_fresh_matching_cache(
    monkeypatch,
    tmp_path,
) -> None:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    cached_project = {"name": "cached", "path": str(project_root / "repo")}

    monkeypatch.setattr(
        "blueprints.projects._load_projects_cache",
        lambda: {
            "schema_version": 2,
            "timestamp": time.time(),
            "directories": [str(project_root)],
            "projects": [cached_project],
        },
    )

    def fail_scan(_project_dirs):
        raise AssertionError("fresh matching cache should avoid scanning")

    monkeypatch.setattr("blueprints.projects._scan_directories", fail_scan)

    projects, error = _collect_projects_data([str(project_root)])

    assert projects == [cached_project]
    assert error is None


def test_collect_projects_force_refresh_scans_and_updates_cache(
    monkeypatch,
    tmp_path,
) -> None:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    scanned_project = {"name": "scanned", "path": str(project_root / "repo")}
    saved = {}

    monkeypatch.setattr(
        "blueprints.projects._load_projects_cache",
        lambda: {
            "schema_version": 2,
            "timestamp": time.time(),
            "directories": [str(project_root)],
            "projects": [{"name": "cached", "path": str(project_root / "old")}],
        },
    )
    monkeypatch.setattr(
        "blueprints.projects._scan_directories",
        lambda _project_dirs: ([scanned_project], None),
    )
    monkeypatch.setattr(
        "blueprints.projects._save_projects_cache",
        lambda projects, dirs: saved.update({"projects": projects, "dirs": dirs}),
    )

    projects, error = _collect_projects_data(
        [str(project_root)],
        force_refresh=True,
    )

    assert projects == [scanned_project]
    assert error is None
    assert saved["projects"] == [scanned_project]
