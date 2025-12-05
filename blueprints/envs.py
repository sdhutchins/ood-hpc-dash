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


def _categorize_env(path: str) -> str:
    """Derive a friendly category from the env path."""
    lower = path.lower()
    if "mamba" in lower:
        return "Mamba"
    if ".conda" in lower:
        return "Conda"
    if lower.startswith("/scratch"):
        return "Scratch"
    if lower.startswith("/data/project"):
        return "Project"
    if lower.startswith("/home"):
        return "Home"
    return "Other"


def _group_envs(envs: list[dict]) -> tuple[dict, list[str]]:
    """Group envs by derived category and sort."""
    grouped = {}
    for env in envs:
        cat = _categorize_env(env["path"])
        grouped.setdefault(cat, []).append(env)
    # Sort envs in each category by name (alpha)
    for cat_envs in grouped.values():
        cat_envs.sort(key=lambda e: e["name"].lower())
    # Order categories
    order = ["Project", "Home", "Conda", "Mamba", "Scratch", "Other"]
    ordered = [c for c in order if c in grouped] + [c for c in grouped if c not in order]
    return grouped, ordered

# Category display metadata
CATEGORY_META = {
    "Project": {"title": "Project Conda Environments", "icon": "fa-folder-tree"},
    "Home": {"title": "Home Conda Environments", "icon": "fa-house"},
    "Conda": {"title": "Conda Environments", "icon": "fa-flask"},
    "Mamba": {"title": "Mamba Environments", "icon": "fa-snake"},
    "Scratch": {"title": "Scratch Environments", "icon": "fa-database"},
    "Other": {"title": "Other Environments", "icon": "fa-box"},
}

@envs_bp.route('/')
def envs():
    envs_list, warning = _load_envs_from_conda_list()
    envs_by_category, category_order = _group_envs(envs_list) if envs_list else ({}, [])
    return render_template(
        'envs.html',
        envs=envs_list,
        warning=warning,
        envs_by_category=envs_by_category,
        category_order=category_order,
        category_meta=CATEGORY_META,
    )