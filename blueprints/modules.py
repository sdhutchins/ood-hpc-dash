# Standard library imports
import logging

# Third-party imports
from flask import Blueprint, render_template

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

# Simple in-memory cache (survives within same process)
_cached_modules = None

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page - waits for modules to load."""
    global _cached_modules
    
    if not LMODULE_AVAILABLE:
        return render_template('modules.html', modules=[])
    
    # Return cached modules if available
    if _cached_modules is not None:
        return render_template('modules.html', modules=_cached_modules)
    
    try:
        logger.info("Creating Spider instance (this may take 20+ seconds)...")
        spider = Spider()
        logger.info("Spider created, getting module names...")
        module_names = spider.get_names()
        logger.info(f"Found {len(module_names)} module names")
        
        # Cache the result
        _cached_modules = module_names
        
        return render_template('modules.html', modules=module_names)
    except Exception as e:
        logger.error(f"Error getting modules: {e}", exc_info=True)
        return render_template('modules.html', modules=[])