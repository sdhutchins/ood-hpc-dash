from flask import Blueprint, render_template, jsonify
from subprocess import run, PIPE, CalledProcessError

import logging
from typing import List

# Blueprint for the modules page
modules_bp = Blueprint('modules', __name__, url_prefix='/modules')

# Logger for the modules blueprint
logger = logging.getLogger(__name__)

def get_available_modules() -> List[str]:
    """
    Run modulecmd python avail to get list of available modules.
    Returns sorted list of module names.
    """
    try:
        # Try modulecmd python avail first
        result = run(
            ['module', 'spider'],
            capture_output=True,
            text=True,
            check=False  # Don't raise on non-zero exit
        )
        
        # Parse the output
        # modulecmd output format varies, you'll need to parse it
        # Look for module names in the output
        
        modules = []
        # Parse logic here - extract module names from stdout
        # Filter out empty strings, sort, return
        
        return sorted(modules)
        
    except FileNotFoundError:
        logger.error("module command not found")
        return []
    except Exception as e:
        logger.error(f"Error getting modules: {e}")
        return []

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page"""
    modules_list = get_available_modules()
    return render_template('modules.html', modules=modules_list)

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of available modules."""
    modules = get_available_modules()
    return jsonify({'modules': modules})