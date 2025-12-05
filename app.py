# Standard library imports
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
import os


# Third-party imports
from flask import Flask, render_template
import flaskcode
import json

# Local imports
from blueprints.envs import envs_bp
from blueprints.jobs import jobs_bp
from blueprints.modules import modules_bp
from blueprints.viewer import viewer_bp
from blueprints.settings import settings_bp

SETTINGS_FILE = Path("config/settings.json")


def _load_settings() -> dict:
    """Load settings from JSON file with sensible defaults."""
    defaults = {
        "navbar_color": "#e3f2fd",
        "code_editor_path": str(Path.cwd()),
        "conda_envs_paths": ["$HOME/.conda/envs"],
    }
    try:
        if SETTINGS_FILE.exists():
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {**defaults, **data}
    except Exception:
        return defaults
    return defaults

# Configure the app
app = Flask(__name__)

# Load settings once on startup
settings_data = _load_settings()

# Configure FlaskCode using settings
app.config.from_object(flaskcode.default_config)
app.config['FLASKCODE_RESOURCE_BASEPATH'] = settings_data.get("code_editor_path", str(Path.cwd()))
app.register_blueprint(flaskcode.blueprint, url_prefix='/flaskcode')

# Create logs directory if it doesn't exist
log_dir = Path('logs')
log_dir.mkdir(exist_ok=True)

# Configure logging
file_handler = logging.FileHandler('logs/app.log')
file_handler.setLevel(logging.INFO)

stream_handler = logging.StreamHandler(sys.stderr)
stream_handler.setLevel(logging.INFO)

formatter = logging.Formatter('[%(name)s - %(asctime)s] - [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

# Get root logger and configure
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(stream_handler)

# Log that application is starting
logger = logging.getLogger(__name__.capitalize())
logger.info("Application initialized - logging configured")

# Update modules list in background thread (non-blocking)
def update_modules_background():
    """Update modules list in background thread."""
    scripts_dir = Path('scripts')
    update_script = scripts_dir / 'update_modules.sh'
    modules_file = Path('logs/modules.txt')
    
    # Only update if file doesn't exist or is older than 1 hour
    if modules_file.exists():
        file_age = time.time() - modules_file.stat().st_mtime
        if file_age < 3600:  # 1 hour
            logger.info("Modules file is recent, skipping update")
            return
    
    if update_script.exists():
        try:
            logger.info("Updating modules list in background...")
            # Use bash -l to run as login shell (sources .bashrc, etc.)
            result = subprocess.run(
                ['bash', '-l', str(update_script)],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=Path.cwd()  # Ensure we're in the right directory
            )
            if result.returncode == 0:
                # Check if file was actually created and has content
                if modules_file.exists():
                    file_size = modules_file.stat().st_size
                    logger.info(f"Modules list updated successfully (file size: {file_size} bytes)")
                    if file_size == 0:
                        logger.warning("Modules file is empty - checking script output")
                        if result.stdout:
                            logger.warning(f"Script stdout: {result.stdout}")
                        if result.stderr:
                            logger.warning(f"Script stderr: {result.stderr}")
                else:
                    logger.warning("Modules file was not created")
            else:
                logger.warning(f"Modules update failed (exit code {result.returncode})")
                if result.stderr:
                    logger.warning(f"Script stderr: {result.stderr}")
                if result.stdout:
                    logger.warning(f"Script stdout: {result.stdout}")
        except Exception as e:
            logger.warning(f"Could not update modules: {e}")

# Start background thread
thread = threading.Thread(target=update_modules_background, daemon=True)
thread.start()

# Register blueprints
app.register_blueprint(modules_bp)
app.register_blueprint(jobs_bp)
app.register_blueprint(envs_bp)
app.register_blueprint(viewer_bp)
app.register_blueprint(settings_bp)


@app.context_processor
def inject_navbar_color():
    """Make navbar_color available to all templates."""
    navbar_color = settings_data.get("navbar_color", "#e3f2fd")
    return {"navbar_color": navbar_color}

@app.route("/")
def index():
    """Render the home page."""
    logger.info("Home page accessed")
    return render_template("index.html")

if __name__ == "__main__":
	app.run()
