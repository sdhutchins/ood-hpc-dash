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

def _group_modules_by_name(module_lines):
    """Group modules by base name.
    
    Args:
        module_lines: List of module strings like 'Armadillo/11.4.3-foss-2022b'
    
    Returns:
        List of dicts with 'name' (base name) and 'versions' (list of full names)
    """
    grouped = {}
    
    for module_line in module_lines:
        # Skip lines that end with just '/' (base name only)
        if module_line.endswith('/'):
            base_name = module_line.rstrip('/')
            if base_name not in grouped:
                grouped[base_name] = []
        elif '/' in module_line:
            # Extract base name (everything before first '/')
            base_name = module_line.split('/', 1)[0]
            if base_name not in grouped:
                grouped[base_name] = []
            grouped[base_name].append(module_line)
        else:
            # No version, just base name
            if module_line not in grouped:
                grouped[module_line] = []
    
    # Convert to list of dicts, sorted by name
    result = [
        {'name': name, 'versions': sorted(versions) if versions else []}
        for name, versions in sorted(grouped.items())
    ]
    
    return result

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page."""
    module_lines = _load_modules_from_file()
    grouped_modules = _group_modules_by_name(module_lines) if module_lines else []
    unique_count = len(grouped_modules)
    return render_template('modules.html', modules=grouped_modules, unique_count=unique_count)

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of modules."""
    module_lines = _load_modules_from_file()
    if module_lines:
        grouped_modules = _group_modules_by_name(module_lines)
        unique_count = len(grouped_modules)
        return jsonify({
            'modules': grouped_modules,
            'unique_count': unique_count
        })
    else:
        return jsonify({'modules': [], 'unique_count': 0, 'loading': True})