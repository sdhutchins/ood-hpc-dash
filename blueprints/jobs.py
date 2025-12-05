from flask import Blueprint, render_template

jobs_bp = Blueprint('jobs', __name__, url_prefix='/jobs')

@jobs_bp.route('/')
def jobs():
    """Render the jobs page"""
    return render_template('jobs.html')
