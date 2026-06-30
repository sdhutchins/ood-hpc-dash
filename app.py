import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import flaskcode
from flask import Flask, abort, render_template, request, session

from blueprints.editor import editor_bp
from blueprints.envs import envs_bp
from blueprints.jobs import jobs_bp
from blueprints.modules import _preload_modules_cache, modules_bp
from blueprints.projects import projects_bp
from blueprints.settings import settings_bp
from utils import CustomJsonProvider, load_settings, safe_code_editor_path

SECRET_KEY_FILE = Path('config/.secret_key')
LOG_DIR = Path('logs')
CSRF_PROTECTED_ENDPOINTS = {
    'settings.save_settings',
    'envs.env_history',
    'modules.refresh_start',
}
CSRF_PROTECTED_STREAM_ENDPOINTS = {
    'modules.refresh_modules',
}
logger = logging.getLogger(__name__)


def _load_secret_key() -> str:
    """Load SECRET_KEY from env or create a persistent per-app OOD secret."""
    secret_key = os.environ.get('SECRET_KEY')
    if secret_key:
        return secret_key

    try:
        if SECRET_KEY_FILE.exists():
            return SECRET_KEY_FILE.read_text(encoding='utf-8').strip()

        SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        secret_key = secrets.token_urlsafe(48)
        file_descriptor = os.open(
            SECRET_KEY_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, 'w', encoding='utf-8') as secret_file:
            secret_file.write(f"{secret_key}\n")
        return secret_key
    except OSError:
        logging.getLogger(__name__).warning(
            "Unable to persist Flask SECRET_KEY; using process-local secret."
        )
        return secrets.token_urlsafe(48)


def _csrf_token() -> str:
    """Return the current session CSRF token, creating one if needed."""
    token = session.get('csrf_token')
    if isinstance(token, str) and token:
        return token

    token = secrets.token_urlsafe(32)
    session['csrf_token'] = token
    return token


def _configure_logging() -> None:
    """Configure process logging once for Passenger and local Flask runs."""
    LOG_DIR.mkdir(exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '[%(name)s - %(asctime)s] - [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    handler_names = {
        handler.get_name()
        for handler in root_logger.handlers
    }
    if 'ood_hpc_dash_file' not in handler_names:
        file_handler = logging.FileHandler(LOG_DIR / 'app.log')
        file_handler.set_name('ood_hpc_dash_file')
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    if 'ood_hpc_dash_stream' not in handler_names:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.set_name('ood_hpc_dash_stream')
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    logger.info("Application initialized - logging configured")


def _run_background_script(
    script_name: str,
    output_file: Path,
    max_age: int,
    timeout: int = 30,
) -> None:
    """Run a background script and log results.

    Args:
        script_name: Name of script in scripts/ directory
        output_file: Path to expected output file
        max_age: Maximum age in seconds before updating
        timeout: Subprocess timeout in seconds
    """
    script_path = Path('scripts') / script_name
    if not script_path.exists():
        return

    if output_file.exists():
        file_age = time.time() - output_file.stat().st_mtime
        if file_age < max_age:
            logger.info(f"{script_name} output is recent, skipping update")
            return

    try:
        logger.info(f"Running {script_name} in background...")
        result = subprocess.run(
            ['bash', '-l', str(script_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=Path.cwd()
        )

        if result.returncode == 0:
            if output_file.exists():
                file_size = output_file.stat().st_size
                logger.info(
                    f"{script_name} completed (output: {file_size} bytes)"
                )
                if file_size == 0:
                    logger.warning(f"{script_name} output is empty")
                    if result.stdout:
                        logger.warning(f"stdout: {result.stdout[:200]}")
                    if result.stderr:
                        logger.warning(f"stderr: {result.stderr[:200]}")
            else:
                logger.warning(f"{script_name} output file not created")
        else:
            logger.warning(f"{script_name} failed (exit {result.returncode})")
            if result.stderr:
                logger.warning(f"stderr: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        logger.warning(f"{script_name} timed out after {timeout}s")
    except OSError as e:
        logger.warning(f"Error running {script_name}: {e}")


def update_disk_quota_background(force: bool = False) -> None:
    """Update disk quota in background thread."""
    quota_file = Path('logs/disk_quota.txt')
    if not force and quota_file.exists():
        file_age = time.time() - quota_file.stat().st_mtime
        if file_age < 300:
            logger.info("Disk quota file is recent, skipping update")
            return
    _run_background_script('get_disk_quota.sh', quota_file, max_age=300)


def _start_background_threads() -> None:
    """Start non-request startup work after an application is created."""
    quota_thread = threading.Thread(
        target=lambda: update_disk_quota_background(force=True),
        name='disk-quota-refresh',
        daemon=True,
    )
    quota_thread.start()

    modules_preload_thread = threading.Thread(
        target=_preload_modules_cache,
        name='module-cache-preload',
        daemon=True,
    )
    modules_preload_thread.start()


def validate_csrf_token() -> None:
    """Reject cross-site requests to app routes that mutate state or scan HPC."""
    endpoint = request.endpoint
    if endpoint not in CSRF_PROTECTED_ENDPOINTS | CSRF_PROTECTED_STREAM_ENDPOINTS:
        return

    expected_token = session.get('csrf_token')
    if not isinstance(expected_token, str) or not expected_token:
        abort(400)

    if endpoint in CSRF_PROTECTED_STREAM_ENDPOINTS:
        submitted_token = request.args.get('csrf_token', '')
    else:
        submitted_token = (
            request.form.get('csrf_token')
            or request.headers.get('X-CSRF-Token')
            or ''
        )

    if not secrets.compare_digest(expected_token, submitted_token):
        abort(400)


def inject_shared_template_values(
    settings_defaults: dict[str, Any],
) -> dict[str, str]:
    """Make shared template values available, reloading settings each request."""
    current = load_settings()
    navbar_color = current.get(
        "navbar_color",
        settings_defaults.get("navbar_color", "#ede7f6"),
    )
    return {
        "csrf_token": _csrf_token(),
        "navbar_color": navbar_color,
    }


def _strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape codes from text."""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def _parse_size_to_gb(size_str: str) -> float:
    """Convert size string (e.g., '131.95GB', '1.39GB') to GB as float."""
    size_str = size_str.strip().upper()
    if size_str.endswith('GB'):
        try:
            return float(size_str[:-2].strip())
        except ValueError:
            return 0.0
    if size_str.endswith('TB'):
        try:
            return float(size_str[:-2].strip()) * 1024
        except ValueError:
            return 0.0
    if size_str.endswith('MB'):
        try:
            return float(size_str[:-2].strip()) / 1024
        except ValueError:
            return 0.0
    return 0.0


def _parse_disk_quota() -> dict[str, object] | None:
    """Parse disk quota file and return structured data with percentages."""
    quota_file = Path('logs/disk_quota.txt')
    if not quota_file.exists():
        return None

    try:
        with quota_file.open('r', encoding='utf-8') as f:
            lines = f.readlines()

        quota_data: dict[str, object] = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith('---') or 'Disk Quota Report' in line:
                continue

            line = _strip_ansi_codes(line)

            # Preserve the two quota shapes emitted by Cheaha helper scripts.
            if '/gpfs/user' in line or '/home' in line:
                if ':' in line:
                    parts = line.split(':', 1)
                    path = parts[0].strip()
                    quota_info = parts[1].strip()
                    
                    # Parse "131.95GB of 5368.71GB" or similar
                    used_gb = 0.0
                    total_gb = 0.0
                    if ' of ' in quota_info:
                        quota_parts = quota_info.split(' of ')
                        used_str = quota_parts[0].strip()
                        total_str = quota_parts[1].split()[0].strip()
                        used_gb = _parse_size_to_gb(used_str)
                        total_gb = _parse_size_to_gb(total_str)

                    percentage = (
                        (used_gb / total_gb * 100) if total_gb > 0 else 0.0
                    )

                    quota_data['home'] = {
                        'path': path,
                        'quota': quota_info,
                        'used_gb': used_gb,
                        'total_gb': total_gb,
                        'percentage': round(percentage, 1)
                    }
            elif '/gpfs/scratch' in line:
                if ':' in line:
                    parts = line.split(':', 1)
                    path = parts[0].strip()
                    quota_info = parts[1].strip()

                    # Parse "1.39GB" or "1.39GB - Please keep scratch clean!"
                    total_gb = 0.0
                    quota_parts = quota_info.split('-')[0].strip()
                    used_gb = _parse_size_to_gb(quota_parts)

                    message = ''
                    if '-' in quota_info:
                        message = quota_info.split('-', 1)[1].strip()

                    quota_data['scratch'] = {
                        'path': path,
                        'quota': quota_info,
                        'used_gb': used_gb,
                        'total_gb': total_gb,
                        'percentage': None,
                        'message': message
                    }

        return quota_data if quota_data else None

    except (OSError, ValueError, IndexError) as e:
        logger.warning(f"Error parsing disk quota: {e}")
        return None


def index() -> str:
    """Render the home page with disk quota information."""
    logger.info("Home page accessed")

    disk_quota = _parse_disk_quota()
    username = os.environ.get("USER", "user")

    return render_template("index.html", disk_quota=disk_quota, username=username)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    """Create and configure the Flask application."""
    application = Flask(__name__)
    settings_data = load_settings()
    configured_secret = (
        test_config.get('SECRET_KEY')
        if test_config is not None
        else None
    )

    application.config['SECRET_KEY'] = configured_secret or _load_secret_key()
    application.config.from_object(flaskcode.default_config)
    application.json = CustomJsonProvider(application)
    application.config['FLASKCODE_RESOURCE_BASEPATH'] = settings_data.get(
        "code_editor_path",
        str(Path.cwd()),
    )
    application.config['FLASKCODE_RESOURCE_BASEPATH'] = safe_code_editor_path(
        application.config['FLASKCODE_RESOURCE_BASEPATH']
    )
    application.config.setdefault('START_BACKGROUND_THREADS', True)
    if test_config is not None:
        application.config.update(test_config)
    application.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=not application.config.get('TESTING', False),
    )

    _configure_logging()

    application.register_blueprint(flaskcode.blueprint, url_prefix='/flaskcode')
    application.register_blueprint(modules_bp)
    application.register_blueprint(jobs_bp)
    application.register_blueprint(envs_bp)
    application.register_blueprint(projects_bp)
    application.register_blueprint(settings_bp)
    application.register_blueprint(editor_bp)

    application.before_request(validate_csrf_token)
    application.context_processor(
        lambda: inject_shared_template_values(settings_data)
    )
    application.add_url_rule("/", "index", index)

    if (
        application.config.get('START_BACKGROUND_THREADS')
        and not application.config.get('TESTING')
    ):
        _start_background_threads()

    return application


if __name__ == "__main__":
    create_app().run()
