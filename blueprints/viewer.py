from flask import Blueprint, render_template

viewer_bp = Blueprint('viewer', __name__, url_prefix='/viewer')

@viewer_bp.route('/')
def viewer():
    """Render the HTML viewer page"""
    return render_template('viewer.html')
