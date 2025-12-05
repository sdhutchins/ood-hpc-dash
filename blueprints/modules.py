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

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page."""
    if not LMODULE_AVAILABLE:
        return render_template('modules.html', modules=[])
    
    try:
        spider = Spider()
        module_names = spider.get_names()
        return render_template('modules.html', modules=module_names)
    except Exception as e:
        logger.error(f"Error getting modules: {e}", exc_info=True)
        return render_template('modules.html', modules=[])