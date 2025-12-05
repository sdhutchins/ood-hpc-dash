from flask import Flask, render_template
from blueprints.modules import modules_bp

app = Flask(__name__)

# Register the modules blueprint
app.register_blueprint(modules_bp)

@app.route("/")
def index():
	return render_template("index.html")

if __name__ == "__main__":
	app.run()
