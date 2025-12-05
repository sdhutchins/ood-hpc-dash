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
    logger.info("get_available_modules() called")
    
    if not LMODULE_AVAILABLE:
        logger.error("lmodule package not available")
        return []
    
    try:
        logger.info("Creating Spider instance in get_available_modules")
        spider = Spider()
        
        # Get unique module names (e.g., ['CUDA', 'lmod', 'GCC'])
        logger.info("Calling spider.get_names()")
        unique_names = spider.get_names()
        logger.info(f"Got {len(unique_names)} unique names")
        
        # Get all modules filtered by those names
        # This returns all versions of each module
        logger.info("Calling spider.get_modules() with unique names")
        modules_dict = spider.get_modules(unique_names)
        logger.info(f"Got {len(modules_dict)} modules from spider")

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
        logger.info(f"Processed {len(processed_modules)} modules, sorting")
        sorted_modules = sorted(processed_modules, key=lambda x: x['name'])
        logger.info("Successfully retrieved and processed modules")
        return sorted_modules
        
    except FileNotFoundError:
        logger.error("lmodule Spider not found", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Error getting modules: {e}", exc_info=True)
        return []

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page"""
    logger.info("Modules page route accessed")
    
    if not LMODULE_AVAILABLE:
        logger.error("lmodule package not available")
        return render_template('modules.html', modules=[], unique_count=0)
    
    try:
        logger.info("Creating Spider instance")
        spider = Spider()
        logger.info("Getting unique module names")
        unique_names = spider.get_names()
        unique_count = len(unique_names)
        logger.info(f"Found {unique_count} unique module names")
    except Exception as e:
        logger.error(f"Error getting unique names count: {e}", exc_info=True)
        unique_count = 0
    
    try:
        logger.info("Calling get_available_modules()")
        modules_list = get_available_modules()
        logger.info(f"Retrieved {len(modules_list)} modules")
    except Exception as e:
        logger.error(f"Error in get_available_modules: {e}", exc_info=True)
        modules_list = []
    
    logger.info("Rendering modules template")
    return render_template('modules.html', modules=modules_list, unique_count=unique_count)

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of available modules."""
    modules = get_available_modules()
    return jsonify({'modules': modules})