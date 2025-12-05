# Standard library imports
import logging
from pathlib import Path

# Third-party imports
from flask import Blueprint, render_template

# Blueprint for the modules page
modules_bp = Blueprint('modules', __name__, url_prefix='/modules')

# Logger for the modules blueprint
logger = logging.getLogger(__name__)

# Path to modules file
MODULES_FILE = Path('logs/modules.txt')

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page."""
    try:
        if not MODULES_FILE.exists():
            logger.warning("Modules file not found")
            return render_template('modules.html', modules=[])
        
        with MODULES_FILE.open('r', encoding='utf-8') as f:
            module_lines = [line.strip() for line in f if line.strip()]
        
        logger.info(f"Loaded {len(module_lines)} module lines from file")
        return render_template('modules.html', modules=module_lines)
    except Exception as e:
        logger.error(f"Error reading modules file: {e}", exc_info=True)
        return render_template('modules.html', modules=[])