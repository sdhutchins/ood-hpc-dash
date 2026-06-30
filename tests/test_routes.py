"""Smoke tests for blueprint routes using the Flask test client."""

from __future__ import annotations

import json
from unittest.mock import patch

import utils


def test_settings_page_renders(client) -> None:
    response = client.get("/settings/")
    assert response.status_code == 200
    assert b"Settings" in response.data


def test_create_app_factory_registers_core_routes(app) -> None:
    rules = {str(rule) for rule in app.url_map.iter_rules()}

    assert app.config["START_BACKGROUND_THREADS"] is False
    assert "/" in rules
    assert "/settings/save" in rules
    assert "/modules/refresh-start" in rules


def test_editor_page_renders(client) -> None:
    editor_response = client.get("/editor/")

    assert editor_response.status_code == 200


def test_modules_list_returns_json(client) -> None:
    response = client.get("/modules/list")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["loading"] is False
    assert "modules" in payload


def test_csrf_protects_module_refresh_start(client) -> None:
    rejected_response = client.post("/modules/refresh-start")
    assert rejected_response.status_code == 400

    with client.session_transaction() as session:
        session["csrf_token"] = "known-test-token"

    accepted_response = client.post(
        "/modules/refresh-start",
        headers={"X-CSRF-Token": "known-test-token"},
    )
    assert accepted_response.status_code == 200


def test_settings_save_with_csrf_updates_settings_file(
    client,
    monkeypatch,
    tmp_path,
) -> None:
    settings_file = tmp_path / "settings.json"
    editor_root = tmp_path / "editor-root"
    project_root = tmp_path / "project-root"
    editor_root.mkdir()
    project_root.mkdir()

    monkeypatch.setattr(utils, "SETTINGS_FILE", settings_file)
    monkeypatch.setenv("OOD_HPC_DASH_EDITOR_ROOTS", str(tmp_path))
    monkeypatch.setenv("OOD_HPC_DASH_PROJECT_ROOTS", str(tmp_path))

    with client.session_transaction() as session:
        session["csrf_token"] = "settings-token"

    response = client.post(
        "/settings/save",
        data={
            "csrf_token": "settings-token",
            "navbar_color": "#e3f2fd",
            "code_editor_path": str(editor_root),
            "conda_envs_paths": "",
            "project_directories": str(project_root),
        },
    )

    saved_settings = json.loads(settings_file.read_text(encoding="utf-8"))

    assert response.status_code == 302
    assert saved_settings["navbar_color"] == "#e3f2fd"
    assert saved_settings["code_editor_path"] == str(editor_root)
    assert saved_settings["conda_envs_paths"] == []
    assert saved_settings["project_directories"] == [str(project_root)]


def test_env_history_accepts_resolved_configured_path(
    client,
    monkeypatch,
    tmp_path,
) -> None:
    env_dir = tmp_path / "envs" / "analysis"
    history_dir = env_dir / "conda-meta"
    history_dir.mkdir(parents=True)
    (history_dir / "history").write_text(
        "+defaults::python-3.10.12-h1234567_0\n",
        encoding="utf-8",
    )

    environments_file = tmp_path / "environments.txt"
    environments_file.write_text(f"{env_dir}\n", encoding="utf-8")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"conda_envs_paths": [str(environments_file)]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(utils, "SETTINGS_FILE", settings_file)

    with client.session_transaction() as session:
        session["csrf_token"] = "env-token"

    response = client.post(
        "/envs/history",
        json={"path": f"{env_dir}/"},
        headers={"X-CSRF-Token": "env-token"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["path"] == str(env_dir.resolve())
    assert "python=3.10.12=h1234567_0" in payload["output"]


@patch("blueprints.jobs._get_partition_info", return_value=([], None))
@patch("blueprints.jobs._call_squeue", return_value=(None, None))
@patch("blueprints.jobs._call_sacct", return_value=(None, None))
def test_jobs_page_renders_without_slurm(
    _mock_sacct,
    _mock_squeue,
    _mock_partitions,
    client,
) -> None:
    response = client.get("/jobs/")
    assert response.status_code == 200
    assert b"Cluster Partitions" in response.data


@patch(
    "blueprints.projects._collect_projects_data",
    return_value=([], None),
)
def test_projects_status_returns_json(
    mock_collect,
    client,
    monkeypatch,
    tmp_path,
) -> None:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    monkeypatch.setenv("OOD_HPC_DASH_PROJECT_ROOTS", str(tmp_path))
    monkeypatch.setattr(
        "blueprints.projects.load_settings",
        lambda: {"project_directories": [str(project_root)]},
    )

    response = client.get("/projects/status?refresh=true")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["projects"] == []
    assert payload["total"] == 0
    mock_collect.assert_called_once_with([str(project_root)], use_cache=True)
