# Standard library imports
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

# Third-party imports
from flask import Flask, render_template

# Local imports
from blueprints.envs import envs_bp
from blueprints.jobs import jobs_bp
from blueprints.modules import modules_bp
from blueprints.viewer import viewer_bp

app = Flask(__name__)

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
            result = subprocess.run(
                ['bash', str(update_script)],
                capture_output=True,
                text=True,
                timeout=120
            )
            if result.returncode == 0:
                logger.info("Modules list updated successfully")
            else:
                logger.warning(f"Modules update had issues: {result.stderr}")
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

@app.route("/")
def index():
    """Render the home page."""
    logger.info("Home page accessed")
    return render_template("index.html")

if __name__ == "__main__":
	app.run()
