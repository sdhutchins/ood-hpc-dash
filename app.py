# Standard library imports
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


# Third-party imports
from flask import Flask, render_template
import flaskcode
import json

# Local imports
from blueprints.editor import editor_bp
from blueprints.envs import envs_bp
from blueprints.jobs import jobs_bp
from blueprints.modules import modules_bp, _preload_modules_cache
from blueprints.projects import projects_bp
from blueprints.viewer import viewer_bp
from blueprints.settings import settings_bp, _load_settings as load_app_settings

# Configure the app
app = Flask(__name__)
# Secret key for session/flash; override with env SECRET_KEY in production
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

# Load settings on startup (used for one-time config like flaskcode path)
settings_data = load_app_settings()

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

# Update partitions list in background thread (non-blocking)
def update_partitions_background():
    """Update partitions list by running update_partitions.sh in background thread."""
    scripts_dir = Path('scripts')
    update_script = scripts_dir / 'update_partitions.sh'
    partitions_file = Path('logs/partitions.txt')
    
    # Only update if file doesn't exist or is older than 5 minutes
    if partitions_file.exists():
        file_age = time.time() - partitions_file.stat().st_mtime
        if file_age < 300:  # 5 minutes
            logger.info("Partitions file is recent, skipping update")
            return
    
    if update_script.exists():
        try:
            logger.info("Updating partitions list in background...")
            # Use bash -l to run as login shell (sources .bashrc, etc.)
            result = subprocess.run(
                ['bash', '-l', str(update_script)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=Path.cwd()
            )
            if result.returncode == 0:
                # Check if file was actually created and has content
                if partitions_file.exists():
                    file_size = partitions_file.stat().st_size
                    logger.info(f"Partitions list updated successfully (file size: {file_size} bytes)")
                    if file_size == 0:
                        logger.warning("Partitions file is empty - checking script output")
                        if result.stdout:
                            logger.warning(f"Script stdout: {result.stdout}")
                        if result.stderr:
                            logger.warning(f"Script stderr: {result.stderr}")
                else:
                    logger.warning("Partitions file was not created")
            else:
                logger.warning(f"Partitions update failed (exit code {result.returncode})")
                if result.stderr:
                    logger.warning(f"Script stderr: {result.stderr}")
                if result.stdout:
                    logger.warning(f"Script stdout: {result.stdout}")
        except Exception as e:
            logger.warning(f"Could not update partitions: {e}")

# Start background threads
modules_thread = threading.Thread(target=update_modules_background, daemon=True)
modules_thread.start()

partitions_thread = threading.Thread(target=update_partitions_background, daemon=True)
partitions_thread.start()

# Update disk quota in background thread (non-blocking)
def update_disk_quota_background(force=False):
    """Update disk quota by running get_disk_quota.sh in background thread."""
    scripts_dir = Path('scripts')
    update_script = scripts_dir / 'get_disk_quota.sh'
    quota_file = Path('logs/disk_quota.txt')
    
    # Always update on startup if forced, otherwise check age
    if not force and quota_file.exists():
        file_age = time.time() - quota_file.stat().st_mtime
        if file_age < 300:  # 5 minutes (reduced from 1 hour for faster updates)
            logger.info("Disk quota file is recent, skipping update")
            return
    
    if update_script.exists():
        try:
            logger.info("Updating disk quota in background...")
            result = subprocess.run(
                ['bash', '-l', str(update_script)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=Path.cwd()
            )
            if result.returncode == 0:
                if quota_file.exists():
                    file_size = quota_file.stat().st_size
                    logger.info(f"Disk quota updated successfully (file size: {file_size} bytes)")
                else:
                    logger.warning("Disk quota file was not created")
            else:
                logger.warning(f"Disk quota update failed (exit code {result.returncode})")
                if result.stderr:
                    logger.warning(f"Script stderr: {result.stderr}")
        except Exception as e:
            logger.warning(f"Could not update disk quota: {e}")

# Run disk quota update immediately on startup (non-blocking)
quota_thread = threading.Thread(target=lambda: update_disk_quota_background(force=True), daemon=True)
quota_thread.start()

# Preload modules cache immediately on startup (non-blocking)
# This runs module -t spider and populates the cache so modules page loads instantly
modules_preload_thread = threading.Thread(target=_preload_modules_cache, daemon=True)
modules_preload_thread.start()

# Register blueprints
app.register_blueprint(modules_bp)
app.register_blueprint(jobs_bp)
app.register_blueprint(envs_bp)
app.register_blueprint(projects_bp)
app.register_blueprint(viewer_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(editor_bp)


@app.context_processor
def inject_navbar_color():
    """Make navbar_color available to all templates (reload each request)."""
    current = load_app_settings()
    navbar_color = current.get("navbar_color", settings_data.get("navbar_color", "#ede7f6"))
    return {"navbar_color": navbar_color}

def _strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape codes from text."""
    import re
    # Remove ANSI escape sequences
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
    elif size_str.endswith('TB'):
        try:
            return float(size_str[:-2].strip()) * 1024
        except ValueError:
            return 0.0
    elif size_str.endswith('MB'):
        try:
            return float(size_str[:-2].strip()) / 1024
        except ValueError:
            return 0.0
    return 0.0


def _parse_disk_quota() -> Optional[Dict[str, Any]]:
    """Parse disk quota file and return structured data with percentages."""
    quota_file = Path('logs/disk_quota.txt')
    if not quota_file.exists():
        return None
    
    try:
        with quota_file.open('r', encoding='utf-8') as f:
            lines = f.readlines()
        
        quota_data = {}
        for line in lines:
            line = line.strip()
            # Skip header line and empty lines
            if not line or line.startswith('---') or 'Disk Quota Report' in line:
                continue
            
            # Strip ANSI codes
            line = _strip_ansi_codes(line)
            
            # Parse lines like: /gpfs/user/shutchin + /home/shutchin : 131.95GB of 5368.71GB
            # or: /gpfs/scratch/shutchin               :   1.39GB - Please keep scratch clean!
            if '/gpfs/user' in line or '/home' in line:
                # Home directory line
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
                        total_str = quota_parts[1].split()[0].strip()  # Get first part before any extra text
                        used_gb = _parse_size_to_gb(used_str)
                        total_gb = _parse_size_to_gb(total_str)
                    
                    percentage = (used_gb / total_gb * 100) if total_gb > 0 else 0.0
                    
                    quota_data['home'] = {
                        'path': path,
                        'quota': quota_info,
                        'used_gb': used_gb,
                        'total_gb': total_gb,
                        'percentage': round(percentage, 1)
                    }
            elif '/gpfs/scratch' in line:
                # Scratch directory line
                if ':' in line:
                    parts = line.split(':', 1)
                    path = parts[0].strip()
                    quota_info = parts[1].strip()
                    
                    # Parse "1.39GB" or "1.39GB - Please keep scratch clean!"
                    used_gb = 0.0
                    total_gb = 0.0
                    # Scratch might not have "of X" format, just show used
                    quota_parts = quota_info.split('-')[0].strip()  # Get part before "-"
                    used_gb = _parse_size_to_gb(quota_parts)
                    # For scratch, we don't have total quota info, so percentage is N/A
                    percentage = None
                    
                    # Extract message if present
                    message = ''
                    if '-' in quota_info:
                        message = quota_info.split('-', 1)[1].strip()
                    
                    quota_data['scratch'] = {
                        'path': path,
                        'quota': quota_info,
                        'used_gb': used_gb,
                        'total_gb': total_gb,
                        'percentage': percentage,
                        'message': message
                    }
        
        return quota_data if quota_data else None
        
    except Exception as e:
        logger.warning(f"Error parsing disk quota: {e}")
        return None


@app.route("/")
def index():
    """Render the home page with disk quota information."""
    logger.info("Home page accessed")
    
    # Parse disk quota
    disk_quota = _parse_disk_quota()
    username = os.environ.get("USER", "user")
    
    return render_template("index.html", disk_quota=disk_quota, username=username)

if __name__ == "__main__":
	app.run()
