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
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s - %(name)s] - [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr),  # Log to stderr (Passenger captures this)
        logging.FileHandler('logs/app.log')  # Log to file
    ]
)

# Register blueprints
app.register_blueprint(modules_bp)
app.register_blueprint(jobs_bp)
app.register_blueprint(envs_bp)
app.register_blueprint(viewer_bp)

@app.route("/")
def index():
	return render_template("index.html")

if __name__ == "__main__":
	app.run()
