import ast
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from utils import expand_path, load_settings

envs_bp = Blueprint('envs', __name__, url_prefix='/envs')


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
    if lower.startswith("/scratch") or "/scratch/" in lower:
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


def _parse_conda_package_record(record: str) -> tuple[str, str] | None:
    """Parse one conda history package record into a stable dependency spec."""
    package_spec = record.split("::", 1)[-1]
    parts = package_spec.rsplit("-", 2)
    if len(parts) != 3:
        return None

    package_name, version, build = parts
    return package_name, f"{package_name}={version}={build}"


def _parse_update_specs(line: str) -> list[str]:
    """Parse a conda history update specs comment when present."""
    _, specs_text = line.split(":", 1)
    try:
        specs = ast.literal_eval(specs_text.strip())
    except (SyntaxError, ValueError):
        return []

    if not isinstance(specs, list):
        return []
    return [spec for spec in specs if isinstance(spec, str)]


def _parse_conda_history(history_text: str) -> tuple[list[str], list[str]]:
    """Build current dependency specs from conda-meta/history transactions."""
    dependencies_by_name: dict[str, str] = {}
    requested_specs: list[str] = []

    for raw_line in history_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# update specs:"):
            requested_specs = _parse_update_specs(line)
            continue
        if line[0] not in {"+", "-"}:
            continue

        parsed_record = _parse_conda_package_record(line[1:])
        if parsed_record is None:
            continue

        package_name, dependency_spec = parsed_record
        if line.startswith("+"):
            dependencies_by_name[package_name] = dependency_spec
        else:
            dependencies_by_name.pop(package_name, None)

    return sorted(dependencies_by_name.values()), requested_specs


def _format_env_history(
    env_name: str,
    dependencies: list[str],
    requested_specs: list[str],
) -> str:
    """Format parsed conda history as an env-file-like YAML document."""
    lines = [
        f"name: {env_name}",
        "dependencies:",
    ]
    lines.extend(f"  - {dependency}" for dependency in dependencies)

    if requested_specs:
        lines.append("requested_specs:")
        lines.extend(f"  - {spec}" for spec in requested_specs)

    return "\n".join(lines).strip() + "\n"


def _read_env_history(env_path: str) -> tuple[str | None, str | None]:
    """Read conda-meta/history and return parsed dependency output."""
    history_path = Path(env_path) / "conda-meta" / "history"
    if not history_path.exists():
        return None, f"No conda history found at {history_path}."

    try:
        history_text = history_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"Unable to read conda history: {exc}"

    dependencies, requested_specs = _parse_conda_history(history_text)
    if not dependencies:
        return None, f"No dependency records found in {history_path}."

    env_name = Path(env_path).name
    return _format_env_history(env_name, dependencies, requested_specs), None

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
    envs_by_category, category_order = (
        _group_envs(envs_list) if envs_list else ({}, [])
    )
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
    """Return parsed conda history for a configured environment."""
    payload = request.get_json(silent=True) or {}
    env_path = payload.get("path")
    if not isinstance(env_path, str) or not env_path.strip():
        return jsonify({"error": "Environment path is required."}), 400

    env_path = env_path.strip()
    if env_path not in _known_env_paths():
        return jsonify({"error": "Environment path is not configured."}), 403

    output, error = _read_env_history(env_path)
    if error is not None:
        return jsonify({"error": error}), 500

    return jsonify({"path": env_path, "output": output})
