from flask import Flask, render_template
from blueprints.modules import modules_bp
from blueprints.jobs import jobs_bp
from blueprints.envs import envs_bp
from blueprints.viewer import viewer_bp

import logging
import sys
from pathlib import Path

app = Flask(__name__)

# Create logs directory if it doesn't exist
log_dir = Path('logs')
log_dir.mkdir(exist_ok=True)

# Configure logging
file_handler = logging.FileHandler('logs/app.log')
file_handler.setLevel(logging.INFO)

stream_handler = logging.StreamHandler(sys.stderr)
stream_handler.setLevel(logging.INFO)

formatter = logging.Formatter('[%(asctime)s - %(name)s] - [%(levelname)s] %(message)s')
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

# Get root logger and configure
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(stream_handler)

# Log that application is starting
logger = logging.getLogger(__name__)
logger.info("Application initialized - logging configured")

# Register blueprints
app.register_blueprint(modules_bp)
app.register_blueprint(jobs_bp)
app.register_blueprint(envs_bp)
app.register_blueprint(viewer_bp)

@app.route("/")
def index():
	logger.info("Home page accessed")
	return render_template("index.html")

if __name__ == "__main__":
	app.run()
