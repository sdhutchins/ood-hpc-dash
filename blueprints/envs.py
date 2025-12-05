from flask import Blueprint, render_template

envs_bp = Blueprint('envs', __name__, url_prefix='/envs')

@envs_bp.route('/')
def envs():
    """Render the environments page"""
    return render_template('envs.html')
