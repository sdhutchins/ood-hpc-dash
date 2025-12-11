"""Shared utility functions for the HPC Dashboard application."""
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path('config/settings.json')


class CustomJsonEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle Path and datetime objects."""
    def default(self, obj):
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return json.JSONEncoder.default(self, obj)


def load_settings() -> Dict[str, Any]:
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
    except Exception as e:
        logger.warning(f"Error loading settings: {e}")
    
    return defaults


def save_settings(settings: Dict[str, Any]) -> bool:
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
    except Exception as e:
        logger.error(f"Error saving settings: {e}")
        return False


def find_binary(paths: list[str]) -> Optional[str]:
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
