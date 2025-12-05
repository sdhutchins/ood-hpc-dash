from flask import Blueprint, render_template, jsonify
from lmod.spider import Spider

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
    a = Spider()
    b = Spider()
    try:
        # Try modulecmd python avail first
        module_names = a.get_names()
        modules = b.get_modules()

        processed_modules = []

        for file_path, module_name in modules_dict.items():
            # Extract Location from file_path
            from pathlib import Path
            location = str(Path(file_path).parent)
            
            # Extract Version (optional - for separate column)
            # If module_name has '/', version is after the '/'
            if '/' in module_name:
                version = module_name.split('/', 1)[1]  # Everything after first '/'
            else:
                version = None  # or '-' or empty string
            
            processed_modules.append({
                'name': module_name,        # Full name: "CUDA/10.0.130" or "lmod"
                'version': version,          # Just version: "10.0.130" or None
                'location': location         # Directory path
            })

        
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