import logging
import shutil
import subprocess
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from utils import expand_path, load_settings

envs_bp = Blueprint('envs', __name__, url_prefix='/envs')
logger = logging.getLogger(__name__)


def _find_environments_file(conda_paths: list[str]) -> tuple[Path | None, str | None]:
    """
    Given configured conda paths, try to locate environments.txt.
    - If an entry ends with 'environments.txt', use it directly.
    - Otherwise, look for environments.txt under the directory.
    Returns (path_or_none, warning_or_none).
    """
    for raw in conda_paths:
        candidate_base = expand_path(raw)
        if candidate_base.lower().endswith("environments.txt"):
            candidate = Path(candidate_base)
            if candidate.exists():
                return candidate, None
        else:
            candidate = Path(candidate_base) / "environments.txt"
            if candidate.exists():
                return candidate, None
    return None, "No environments.txt found in configured conda paths."


def _load_envs_from_conda_list() -> tuple[list[dict[str, str]], str | None]:
    settings = load_settings()
    conda_paths = settings.get("conda_envs_paths", ["$HOME/.conda"])
    env_file, warning = _find_environments_file(conda_paths)
    if not env_file:
        return [], warning

    envs: list[dict[str, str]] = []
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
    if "snakemake" in lower:
        return "Snakemake"
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
    order = ["Conda", "Mamba", "Snakemake", "Home", "Scratch", "Other"]
    ordered = (
        [c for c in order if c in grouped]
        + [c for c in grouped if c not in order]
    )
    return grouped, ordered


def _known_env_paths() -> set[str]:
    """Return configured env paths so export requests stay scoped."""
    envs, _ = _load_envs_from_conda_list()
    return {env["path"] for env in envs}


def _filter_prefix_line(export_output: str) -> str:
    """Remove local prefix paths from exported env YAML."""
    lines = export_output.splitlines()
    filtered_lines = [
        line for line in lines if not line.startswith("prefix:")
    ]
    return "\n".join(filtered_lines).strip() + "\n"


def _export_env_file(env_path: str) -> tuple[str | None, str | None]:
    """Export a conda env YAML by prefix and hide machine-local prefix data."""
    conda_path = shutil.which("conda")
    if conda_path is None:
        return None, "conda command not found on PATH."

    try:
        result = subprocess.run(
            [conda_path, "env", "export", "--prefix", env_path],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "conda env export timed out after 30 seconds."
    except OSError as exc:
        logger.warning("Error running conda env export: %s", exc)
        return None, f"Unable to run conda env export: {exc}"

    if result.returncode != 0:
        error = result.stderr.strip() or "conda env export failed."
        return None, error

    return _filter_prefix_line(result.stdout), None

# Category display metadata
CATEGORY_META = {
    "Snakemake": {"title": "Snakemake Conda Environments", "icon": "fa-folder"},
    "Home": {"title": "Home Conda Environments", "icon": "fa-folder"},
    "Conda": {"title": "Conda Environments", "icon": "fa-folder"},
    "Mamba": {"title": "Mamba Environments", "icon": "fa-folder"},
    "Scratch": {"title": "Scratch Environments", "icon": "fa-folder"},
    "Other": {"title": "Other Environments", "icon": "fa-folder"},
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


@envs_bp.route('/export', methods=['POST'])
def export_env() -> tuple[object, int] | object:
    """Return a prefix-based conda env export for a configured environment."""
    payload = request.get_json(silent=True) or {}
    env_path = payload.get("path")
    if not isinstance(env_path, str) or not env_path.strip():
        return jsonify({"error": "Environment path is required."}), 400

    env_path = env_path.strip()
    if env_path not in _known_env_paths():
        return jsonify({"error": "Environment path is not configured."}), 403

    output, error = _export_env_file(env_path)
    if error is not None:
        return jsonify({"error": error}), 500

    return jsonify({"path": env_path, "output": output})
