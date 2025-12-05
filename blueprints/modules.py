# Standard library imports
import logging
import subprocess

# Third-party imports
from flask import Blueprint, render_template

# Blueprint for the modules page
modules_bp = Blueprint('modules', __name__, url_prefix='/modules')

# Logger for the modules blueprint
logger = logging.getLogger(__name__)

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page."""
    try:
        logger.info("Running 'module -t spider' command...")
        # Source lmod init script and run module command
        cmd = 'source /usr/share/lmod/lmod/init/bash && module -t spider'
        result = subprocess.run(
            cmd,
            shell=True,
            executable='/bin/bash',
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0:
            module_lines = result.stdout.strip().split('\n')
            logger.info(f"Found {len(module_lines)} module lines")
            return render_template('modules.html', modules=module_lines)
        else:
            logger.error(f"Error running module command: {result.stderr}")
            return render_template('modules.html', modules=[])
    except subprocess.TimeoutExpired:
        logger.error("Module command timed out")
        return render_template('modules.html', modules=[])
    except Exception as e:
        logger.error(f"Error getting modules: {e}", exc_info=True)
        return render_template('modules.html', modules=[])