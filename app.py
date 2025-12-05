from flask import Flask, render_template
from blueprints.modules import modules_bp
from blueprints.jobs import jobs_bp
from blueprints.envs import envs_bp
from blueprints.viewer import viewer_bp

app = Flask(__name__)

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
