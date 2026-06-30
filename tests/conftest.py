"""Shared pytest fixtures for the HPC dashboard."""

from __future__ import annotations

from pathlib import Path

import pytest
import flaskcode
from flask import Flask


def _create_test_app() -> Flask:
    """Build a Flask app with blueprints only (no import-time side effects)."""
    from blueprints.editor import editor_bp
    from blueprints.envs import envs_bp
    from blueprints.jobs import jobs_bp
    from blueprints.modules import modules_bp
    from blueprints.projects import projects_bp
    from blueprints.settings import settings_bp

    repo_root = Path(__file__).resolve().parents[1]
    application = Flask(
        __name__,
        root_path=str(repo_root),
        static_folder="static",
        template_folder="templates",
    )
    application.config.update(
        TESTING=True,
        SECRET_KEY="test-secret-key",
    )
    application.config.from_object(flaskcode.default_config)
    application.config["FLASKCODE_RESOURCE_BASEPATH"] = str(repo_root)

    application.register_blueprint(flaskcode.blueprint, url_prefix="/flaskcode")
    application.register_blueprint(modules_bp)
    application.register_blueprint(jobs_bp)
    application.register_blueprint(envs_bp)
    application.register_blueprint(projects_bp)
    application.register_blueprint(settings_bp)
    application.register_blueprint(editor_bp)

    @application.route("/")
    def index() -> str:
        return "HPC Dashboard"

    @application.context_processor
    def inject_navbar_color() -> dict[str, str]:
        return {
            "csrf_token": "test-csrf-token",
            "navbar_color": "#ede7f6",
        }

    return application


@pytest.fixture
def app() -> Flask:
    """Application instance for route and integration tests."""
    return _create_test_app()


@pytest.fixture
def client(app: Flask):
    """Flask test client bound to the test application."""
    return app.test_client()
