# Standard library imports
import logging
from pathlib import Path

# Third-party imports
from flask import Blueprint, jsonify, render_template

# Blueprint for the modules page
modules_bp = Blueprint('modules', __name__, url_prefix='/modules')

# Logger for the modules blueprint
logger = logging.getLogger(__name__)

# Path to modules file
MODULES_FILE = Path('logs/modules.txt')

def _load_modules_from_file():
    """Load modules from file if it exists and has content."""
    try:
        if not MODULES_FILE.exists():
            return []
        
        with MODULES_FILE.open('r', encoding='utf-8') as f:
            module_lines = [line.strip() for line in f if line.strip()]
        
        return module_lines
    except Exception as e:
        logger.error(f"Error reading modules file: {e}", exc_info=True)
        return []

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page."""
    modules_list = _load_modules_from_file()
    return render_template('modules.html', modules=modules_list)

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of modules."""
    modules_list = _load_modules_from_file()
    if modules_list:
        return jsonify({'modules': modules_list})
    else:
        return jsonify({'modules': [], 'loading': True})