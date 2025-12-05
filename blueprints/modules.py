# Standard library imports
import json
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
CATEGORIES_FILE = Path('config/module_categories.json')

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

def _load_categories():
    """Load module categories from JSON file."""
    try:
        if not CATEGORIES_FILE.exists():
            logger.warning(f"Categories file not found: {CATEGORIES_FILE}")
            return None
        
        with CATEGORIES_FILE.open('r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading categories: {e}", exc_info=True)
        return None

def _categorize_module(module_name, categories_config):
    """Assign a category to a module based on configuration.
    
    Args:
        module_name: Base module name (e.g., 'Armadillo', 'rc/3DSlicer')
        categories_config: Loaded categories JSON configuration (flat dict mapping names to categories)
    
    Returns:
        Category name string, or 'Misc' if no match
    """
    if not categories_config:
        return 'Misc'
    
    # New format: flat dictionary mapping module names to categories
    # Check exact match first
    if module_name in categories_config:
        return categories_config[module_name]
    
    # For hierarchical modules like 'rc/3DSlicer', check if base path exists
    # Try progressively shorter prefixes
    parts = module_name.split('/')
    for i in range(len(parts), 0, -1):
        prefix = '/'.join(parts[:i])
        if prefix in categories_config:
            return categories_config[prefix]
        # Also check with trailing slash
        prefix_with_slash = prefix + '/'
        if prefix_with_slash in categories_config:
            return categories_config[prefix_with_slash]
    
    # Special handling for rc/* modules - check if 'rc' is in config
    if module_name.startswith('rc/'):
        if 'rc' in categories_config:
            return categories_config['rc']
        return 'Restricted Modules'
    
    # Default to Misc for unmatched modules
    return 'Misc'

def _group_modules_by_name(module_lines):
    """Group modules by base name.
    
    Handles both simple modules (e.g., 'Armadillo/11.4.3-foss-2022b') 
    and hierarchical modules (e.g., 'rc/3DSlicer/5.2.2').
    
    Args:
        module_lines: List of module strings from module -t spider output
    
    Returns:
        List of dicts with 'name' (base name), 'versions' (list of full names),
        and 'category' (category name)
    """
    grouped = {}
    categories_config = _load_categories()
    
    for module_line in module_lines:
        # Skip empty lines
        if not module_line.strip():
            continue
            
        # Lines ending with '/' are base names (directories)
        if module_line.endswith('/'):
            base_name = module_line.rstrip('/')
            if base_name not in grouped:
                grouped[base_name] = []
        # Lines with '/' have a version component
        elif '/' in module_line:
            # For hierarchical modules like 'rc/3DSlicer/5.2.2',
            # base name is everything except the last segment
            # For simple modules like 'Armadillo/11.4.3', base is first segment
            parts = module_line.split('/')
            if len(parts) > 2:
                # Hierarchical: rc/3DSlicer/5.2.2 -> base = rc/3DSlicer
                base_name = '/'.join(parts[:-1])
            else:
                # Simple: Armadillo/11.4.3 -> base = Armadillo
                base_name = parts[0]
            
            if base_name not in grouped:
                grouped[base_name] = []
            grouped[base_name].append(module_line)
        else:
            # No version, just base name (e.g., 'rc-base', 'shared')
            if module_line not in grouped:
                grouped[module_line] = []
    
    # Convert to list of dicts with categories, sorted by category then name
    result = []
    for name, versions in sorted(grouped.items()):
        category = _categorize_module(name, categories_config)
        result.append({
            'name': name,
            'versions': sorted(versions) if versions else [],
            'category': category
        })
    
    # Get unique categories and sort them (Misc goes last)
    unique_categories = sorted(set(m['category'] for m in result))
    if 'Misc' in unique_categories:
        unique_categories.remove('Misc')
        unique_categories.append('Misc')
    
    category_index = {cat: idx for idx, cat in enumerate(unique_categories)}
    
    def sort_key(module):
        cat = module['category']
        cat_idx = category_index.get(cat, 999)  # Unknown categories go last
        return (cat_idx, module['name'])
    
    result.sort(key=sort_key)
    
    return result

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page."""
    module_lines = _load_modules_from_file()
    grouped_modules = _group_modules_by_name(module_lines) if module_lines else []
    unique_count = len(grouped_modules)
    
    # Group modules by category for display
    modules_by_category = {}
    for module in grouped_modules:
        cat = module['category']
        if cat not in modules_by_category:
            modules_by_category[cat] = []
        modules_by_category[cat].append(module)
    
    # Get category order (sorted, with Misc last)
    category_order = sorted(modules_by_category.keys())
    if 'Misc' in category_order:
        category_order.remove('Misc')
        category_order.append('Misc')
    
    return render_template(
        'modules.html',
        modules=grouped_modules,
        modules_by_category=modules_by_category,
        category_order=category_order,
        unique_count=unique_count
    )

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of modules."""
    module_lines = _load_modules_from_file()
    if module_lines:
        grouped_modules = _group_modules_by_name(module_lines)
        unique_count = len(grouped_modules)
        
        # Group by category for frontend
        modules_by_category = {}
        for module in grouped_modules:
            cat = module['category']
            if cat not in modules_by_category:
                modules_by_category[cat] = []
            modules_by_category[cat].append(module)
        
        # Get category order (sorted, with Misc last)
        category_order = sorted(modules_by_category.keys())
        if 'Misc' in category_order:
            category_order.remove('Misc')
            category_order.append('Misc')
        
        return jsonify({
            'modules': grouped_modules,
            'modules_by_category': modules_by_category,
            'category_order': category_order,
            'unique_count': unique_count
        })
    else:
        return jsonify({'modules': [], 'unique_count': 0, 'loading': True})