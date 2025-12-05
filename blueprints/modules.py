from flask import Blueprint, render_template, jsonify
from pathlib import Path

import logging
from typing import List, Dict

try:
    from lmod.spider import Spider
    LMODULE_AVAILABLE = True
except ImportError:
    LMODULE_AVAILABLE = False
    Spider = None

# Blueprint for the modules page
modules_bp = Blueprint('modules', __name__, url_prefix='/modules')

# Logger for the modules blueprint
logger = logging.getLogger(__name__)

def get_available_modules() -> List[Dict[str, str]]:
    """
    Get list of available modules using lmodule Spider.
    First gets unique module names, then retrieves all modules for those names.
    Returns list of dictionaries with name, version, and location.
    """
    if not LMODULE_AVAILABLE:
        logger.error("lmodule package not available")
        return []
    
    try:
        spider = Spider()
        
        # Get unique module names (e.g., ['CUDA', 'lmod', 'GCC'])
        unique_names = spider.get_names()
        
        # Get all modules filtered by those names
        # This returns all versions of each module
        modules_dict = spider.get_modules(unique_names)

        processed_modules = []

        for file_path, module_name in modules_dict.items():
            # Extract Location from file_path
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

        # Sort by module name
        return sorted(processed_modules, key=lambda x: x['name'])
        
    except FileNotFoundError:
        logger.error("lmodule Spider not found")
        return []
    except Exception as e:
        logger.error(f"Error getting modules: {e}")
        return []

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page"""
    if not LMODULE_AVAILABLE:
        logger.error("lmodule package not available")
        return render_template('modules.html', modules=[], unique_count=0)
    
    try:
        spider = Spider()
        unique_names = spider.get_names()
        unique_count = len(unique_names)
    except Exception as e:
        logger.error(f"Error getting unique names count: {e}")
        unique_count = 0
    
    try:
        modules_list = get_available_modules()
    except Exception as e:
        logger.error(f"Error in get_available_modules: {e}")
        modules_list = []
    
    return render_template('modules.html', modules=modules_list, unique_count=unique_count)

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of available modules."""
    modules = get_available_modules()
    return jsonify({'modules': modules})