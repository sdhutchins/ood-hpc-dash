import json
import os
from pathlib import Path

from flask import Blueprint, render_template

envs_bp = Blueprint('envs', __name__, url_prefix='/envs')

SETTINGS_FILE = Path("config/settings.json")


def _load_settings() -> dict:
    defaults = {
        "conda_envs_paths": [
            "$HOME/.conda",
        ],
    }
    try:
        if SETTINGS_FILE.exists():
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {**defaults, **data}
    except Exception:
        return defaults
    return defaults


def _find_environments_file(conda_paths: list[str]) -> tuple[Path | None, str | None]:
    """
    Given configured conda paths, try to locate environments.txt.
    - If an entry ends with 'environments.txt', use it directly.
    - Otherwise, look for environments.txt under the directory.
    Returns (path_or_none, warning_or_none).
    """
    for raw in conda_paths:
        candidate_base = os.path.expandvars(os.path.expanduser(raw))
        if candidate_base.lower().endswith("environments.txt"):
            candidate = Path(candidate_base)
            if candidate.exists():
                return candidate, None
        else:
            candidate = Path(candidate_base) / "environments.txt"
            if candidate.exists():
                return candidate, None
    return None, "No environments.txt found in configured conda paths."


def _load_envs_from_conda_list():
    settings = _load_settings()
    conda_paths = settings.get("conda_envs_paths", ["$HOME/.conda"])
    env_file, warning = _find_environments_file(conda_paths)
    if not env_file:
        return [], warning

    envs = []
    with env_file.open() as f:
        for line in f:
            env_path = line.strip()
            if not env_path:
                continue
            name = Path(env_path).name
            envs.append({"name": name, "path": env_path})
    return envs, warning

@envs_bp.route('/')
def envs():
    envs_list, warning = _load_envs_from_conda_list()
    return render_template(
        'envs.html',
        envs=envs_list,
        warning=warning,
    )