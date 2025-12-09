# Third-party imports
from flask import Blueprint, render_template

editor_bp = Blueprint('editor', __name__, url_prefix='/editor')


@editor_bp.route('/')
def editor():
    """Render the code editor page with embedded flaskcode."""
    return render_template('editor.html')
