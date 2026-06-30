"""Shared pytest fixtures for the HPC dashboard."""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient


def _create_test_app() -> Flask:
    """Build the real Flask app with startup side effects disabled."""
    from app import create_app

    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "START_BACKGROUND_THREADS": False,
        }
    )


@pytest.fixture
def app() -> Flask:
    """Application instance for route and integration tests."""
    return _create_test_app()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    """Flask test client bound to the test application."""
    return app.test_client()
