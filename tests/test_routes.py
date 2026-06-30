"""Smoke tests for blueprint routes using the Flask test client."""

from __future__ import annotations

from unittest.mock import patch


def test_settings_page_renders(client) -> None:
    response = client.get("/settings/")
    assert response.status_code == 200
    assert b"Settings" in response.data


def test_editor_page_renders(client) -> None:
    editor_response = client.get("/editor/")

    assert editor_response.status_code == 200


def test_modules_list_returns_json(client) -> None:
    response = client.get("/modules/list")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["loading"] is False
    assert "modules" in payload


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
def test_projects_status_returns_json(mock_collect, client) -> None:
    response = client.get("/projects/status")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["projects"] == []
    assert payload["total"] == 0
    mock_collect.assert_called_once()
