"""Shared utility functions for the HPC Dashboard application."""
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from flask.json.provider import DefaultJSONProvider

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path('config/settings.json')


def _json_default(obj: Any) -> str:
    """Serialize project/status objects that Flask JSON does not know."""
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


class CustomJsonEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle Path and datetime objects."""

    def default(self, obj: Any) -> Any:
        try:
            return _json_default(obj)
        except TypeError:
            return json.JSONEncoder.default(self, obj)


class CustomJsonProvider(DefaultJSONProvider):
    """Flask JSON provider for shared dashboard serialization rules."""

    def default(self, obj: Any) -> Any:
        return _json_default(obj)


def _allowed_roots_from_env(
    env_var: str,
    fallback_roots: list[str],
) -> list[Path]:
    """Resolve configured root allowlists while dropping missing paths."""
    configured_roots = os.environ.get(env_var)
    if configured_roots:
        raw_roots = [
            root for root in configured_roots.split(os.pathsep)
            if root.strip()
        ]
    else:
        raw_roots = fallback_roots

    allowed_roots: list[Path] = []
    for raw_root in raw_roots:
        if not raw_root:
            continue
        resolved_root = _resolved_existing_directory(expand_path(raw_root))
        if resolved_root is not None and resolved_root not in allowed_roots:
            allowed_roots.append(resolved_root)

    return allowed_roots


def load_settings() -> dict[str, Any]:
    """Load settings from JSON file with sensible defaults.
    
    Returns:
        Dictionary of settings merged with defaults
    """
    defaults = {
        'navbar_color': '#ede7f6',
        'code_editor_path': str(Path.cwd()),
        'conda_envs_paths': ['$HOME/.conda/envs'],
        'project_directories': [
            '$HOME/Documents/Git-Repos',
            '$HOME/Dev/src-repos',
        ],
    }
    
    try:
        if SETTINGS_FILE.exists():
            with SETTINGS_FILE.open('r', encoding='utf-8') as f:
                data = json.load(f)
            return {**defaults, **data}
    except (OSError, json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Error loading settings: {e}")
    
    return defaults


def save_settings(settings: dict[str, Any]) -> bool:
    """Save settings to JSON file.
    
    Args:
        settings: Dictionary of settings to save
        
    Returns:
        True if successful, False otherwise
    """
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SETTINGS_FILE.open('w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)
        return True
    except (OSError, TypeError) as e:
        logger.error(f"Error saving settings: {e}")
        return False


def find_binary(paths: list[str]) -> str | None:
    """Find first existing executable binary from list of paths.
    
    Args:
        paths: List of absolute or relative paths to check
        
    Returns:
        Path to first found executable, or None
    """
    for path in paths:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return None


def expand_path(path: str) -> str:
    """Expand environment variables and user home directory in path.
    
    Args:
        path: Path string that may contain $HOME or other env vars
        
    Returns:
        Expanded path string
    """
    return os.path.expanduser(os.path.expandvars(path))


def _resolved_existing_directory(path: str | Path) -> Path | None:
    """Resolve a path only when it points to an existing directory."""
    try:
        resolved_path = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    if not resolved_path.is_dir():
        return None
    return resolved_path


def get_editor_allowed_roots() -> list[Path]:
    """Return filesystem roots that the embedded editor may browse."""
    username = os.environ.get('USER', '')
    return _allowed_roots_from_env(
        'OOD_HPC_DASH_EDITOR_ROOTS',
        [
            '$HOME',
            f'/data/user/{username}' if username else '',
            f'/scratch/{username}' if username else '',
            '/data/project',
            str(Path.cwd()),
        ],
    )


def get_project_allowed_roots() -> list[Path]:
    """Return roots that project scans may recursively inspect."""
    username = os.environ.get('USER', '')
    return _allowed_roots_from_env(
        'OOD_HPC_DASH_PROJECT_ROOTS',
        [
            '$HOME/Documents/Git-Repos',
            '$HOME/Dev/src-repos',
            f'/data/user/{username}' if username else '',
            f'/scratch/{username}' if username else '',
            '/data/project',
            str(Path.cwd()),
        ],
    )


def validate_code_editor_path(raw_path: str) -> tuple[Path | None, str | None]:
    """Validate the configured Flaskcode root against OOD-safe roots."""
    return _validate_under_allowed_roots(
        raw_path=raw_path,
        allowed_roots=get_editor_allowed_roots(),
        label="Code editor path",
    )


def validate_project_directory(raw_path: str) -> tuple[Path | None, str | None]:
    """Validate project scan roots against the project allowlist."""
    return _validate_under_allowed_roots(
        raw_path=raw_path,
        allowed_roots=get_project_allowed_roots(),
        label="Project directory",
    )


def _validate_under_allowed_roots(
    raw_path: str,
    allowed_roots: list[Path],
    label: str,
) -> tuple[Path | None, str | None]:
    """Validate that a configured directory stays inside approved roots."""
    if not raw_path.strip():
        return None, f"{label} is required."

    expanded_path = expand_path(raw_path.strip())
    candidate = _resolved_existing_directory(expanded_path)
    if candidate is None:
        return None, f"{label} must be an existing directory."

    for allowed_root in allowed_roots:
        try:
            candidate.relative_to(allowed_root)
        except ValueError:
            continue
        return candidate, None

    allowed_text = ', '.join(str(path) for path in allowed_roots)
    return (
        None,
        f"{label} must be under an allowed root: "
        f"{allowed_text or '(none configured)'}.",
    )


def safe_code_editor_path(raw_path: str | None) -> str:
    """Return a validated editor path or a conservative app-directory fallback."""
    candidate, error = validate_code_editor_path(raw_path or str(Path.cwd()))
    if candidate is not None:
        return str(candidate)

    logger.warning(f"Invalid code_editor_path ignored: {error}")
    fallback = Path.cwd().resolve()
    return str(fallback)
