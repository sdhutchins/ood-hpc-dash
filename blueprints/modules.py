# Standard library imports
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Third-party imports
from flask import Blueprint, jsonify, render_template, Response
import threading

# Blueprint for the modules page
modules_bp = Blueprint('modules', __name__, url_prefix='/modules')

# Logger for the modules blueprint
logger = logging.getLogger(__name__)

# Path to modules file
MODULES_FILE = Path('logs/modules.txt')
CATEGORIES_FILE = Path('config/module_categories.json')

# Module cache (stores grouped modules data)
_modules_cache: Optional[List[Dict[str, Any]]] = None

# Streaming state
_streaming_lock = threading.Lock()
_streaming_in_progress = False

# Common absolute paths for bash
BASH_PATHS = [
    '/bin/bash',
    '/usr/bin/bash',
]


def _find_binary(paths: List[str]) -> Optional[str]:
    """Find first existing binary from list of absolute paths."""
    for path in paths:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return None


def _call_module_command(command: str, timeout: int = 30) -> Tuple[Optional[str], Optional[str]]:
    """
    Call a module command using bash -lc with explicit environment.
    
    Since module is a shell function, we must use a login shell and source lmod init.
    
    Args:
        command: Module command to run (e.g., 'module -t spider' or 'module --redirect spider zlib')
        timeout: Timeout in seconds
    
    Returns:
        Tuple of (output, error_message)
    """
    bash_path = _find_binary(BASH_PATHS)
    if not bash_path:
        return None, "bash binary not found in standard locations"
    
    # Build command that sources lmod init first
    lmod_init_paths = [
        '/usr/share/lmod/lmod/init/bash',
        '/etc/profile.d/modules.sh',
    ]
    
    # Find lmod init script
    lmod_init = None
    for path in lmod_init_paths:
        if os.path.exists(path):
            lmod_init = path
            break
    
    if lmod_init:
        # Source lmod init, then run the command
        full_command = f'source {lmod_init} && {command}'
    else:
        # Fallback: try without explicit sourcing (might work if in .bashrc)
        full_command = command
    
    # Preserve some environment variables that might be needed
    env = os.environ.copy()
    # Ensure basic PATH is set
    env['PATH'] = '/usr/bin:/bin:/usr/local/bin'
    # Preserve HOME and USER if they exist
    if 'HOME' not in env:
        env['HOME'] = os.path.expanduser('~')
    
    try:
        result = subprocess.run(
            [bash_path, '-lc', full_command],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=Path.cwd(),
        )
        if result.returncode == 0:
            # Some module commands output to stderr instead of stdout (e.g., module -t spider)
            # Check both streams
            output = result.stdout.strip()
            if not output and result.stderr:
                # If stdout is empty but stderr has content, use stderr
                output = result.stderr.strip()
                logger.debug(f"module command output was in stderr, using it")
            
            if not output:
                return None, "module command returned empty output"
            return output, None
        error_msg = result.stderr if result.stderr else f"Exit code: {result.returncode}"
        return None, f"module command failed: {error_msg}"
    except subprocess.TimeoutExpired:
        return None, f"module command timed out after {timeout}s"
    except Exception as e:
        return None, f"Error calling module command: {str(e)}"


def _get_module_families() -> Tuple[Optional[List[str]], Optional[str]]:
    """
    Step 1: Get all module family names using 'module -t spider'.
    
    Returns:
        Tuple of (list of module family names, error_message)
    """
    # Try with retry
    max_retries = 2
    for attempt in range(max_retries):
        output, error = _call_module_command('module -t spider', timeout=60)
        if error:
            if attempt < max_retries - 1:
                logger.warning(f"Attempt {attempt + 1} failed: {error}, retrying...")
                time.sleep(1)  # Brief delay before retry
                continue
            logger.error(f"Error calling module -t spider after {max_retries} attempts: {error}")
            return None, error
        
        if not output or not output.strip():
            if attempt < max_retries - 1:
                logger.warning(f"Attempt {attempt + 1} returned empty output, retrying...")
                time.sleep(1)
                continue
            logger.error("module -t spider returned empty output after retries")
            # Try alternative: check if module function exists
            test_output, test_error = _call_module_command('type module', timeout=5)
            if test_error:
                logger.error(f"module function check failed: {test_error}")
            else:
                logger.info(f"module function check output: {test_output[:200]}")
            return None, "module -t spider returned empty output"
        
        # Success - break out of retry loop
        break
    
    # Parse output: extract unique family names
    # module -t spider returns both family names and full module/version names
    # We need to extract just the family names (base names without versions)
    families_set = set()
    for line in output.split('\n'):
        line = line.strip()
        # Skip empty lines and lines ending with '/' (these are directories in hierarchical modules)
        if not line or line.endswith('/'):
            continue
        
        # If line contains '/', it's a full module/version - extract family name
        if '/' in line:
            # Extract base name (everything before the last '/')
            parts = line.split('/')
            if len(parts) >= 2:
                # For hierarchical modules like "rc/3DSlicer/5.2.2", family is "rc/3DSlicer"
                # For simple modules like "ABRA2/2.23-GCC-8.3.0", family is "ABRA2"
                if len(parts) > 2:
                    family_name = '/'.join(parts[:-1])
                else:
                    family_name = parts[0]
                families_set.add(family_name)
        else:
            # No '/', it's already a family name
            families_set.add(line)
    
    families = sorted(list(families_set))
    
    if not families:
        logger.warning(f"No module families found in output. Output preview: {output[:500]}")
        return None, "No module families found in output"
    
    logger.info(f"Found {len(families)} module families")
    return families, None


def _get_module_details(module_name: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Step 2: Get full details for a specific module using 'module --redirect spider <name>'.
    
    Args:
        module_name: Module family name (e.g., 'zlib', 'python')
    
    Returns:
        Tuple of (dict with 'versions' list and 'description' string, error_message)
        Returns (None, None) if module has no output (skip it, not an error)
    """
    output, error = _call_module_command(f'module --redirect spider {module_name}', timeout=5)
    if error:
        # For individual modules, empty output is not fatal - just skip it
        if "empty output" in error.lower():
            logger.debug(f"Module {module_name} returned empty output - skipping")
            return None, None
        # Other errors (timeout, etc.) are logged but we continue
        logger.debug(f"Module {module_name} error: {error}")
        return None, None
    
    if not output or not output.strip():
        logger.debug(f"Module {module_name} has no output - skipping")
        return None, None
    
    # Parse the detailed output
    versions = []
    description_lines = []
    in_versions_section = False
    in_description_section = False
    
    lines = output.split('\n')
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        
        # Skip separator lines
        if line_stripped.startswith('---'):
            continue
        
        # Skip empty lines (but allow them within description)
        if not line_stripped and not in_description_section:
            continue
        
        # Check for module name line (e.g., "zlib:")
        if line_stripped.endswith(':') and '/' not in line_stripped and 'Versions' not in line_stripped and 'Description' not in line_stripped and 'Dependencies' not in line_stripped:
            in_versions_section = False
            in_description_section = False
            continue
        
        # Check for Versions section
        if 'Versions:' in line_stripped:
            in_versions_section = True
            in_description_section = False
            continue
        
        # Check for Description section
        if 'Description:' in line_stripped:
            in_versions_section = False
            in_description_section = True
            # Description text may be on the same line after "Description:"
            desc_part = line_stripped.split('Description:', 1)
            if len(desc_part) > 1 and desc_part[1].strip():
                description_lines.append(desc_part[1].strip())
            continue
        
        # Check for Dependencies section (end of description)
        if 'Dependencies:' in line_stripped:
            in_description_section = False
            in_versions_section = False
            continue
        
        # Collect versions (they appear indented after "Versions:")
        if in_versions_section and line_stripped:
            # Versions are listed as "module/version" - check if it looks like a version line
            if '/' in line_stripped and not line_stripped.startswith('(') and ':' not in line_stripped:
                # This looks like a version entry
                versions.append(line_stripped)
        
        # Collect description (all lines between "Description:" and "Dependencies:")
        if in_description_section and line_stripped:
            # Include all text until we hit Dependencies
            description_lines.append(line_stripped)
    
    description = ' '.join(description_lines).strip() if description_lines else ''
    
    # Debug logging for first few modules to troubleshoot
    if module_name in ['zlib', 'python', 'gcc'] and not description:
        logger.warning(f"Module {module_name}: No description found. Versions: {len(versions)}. Output preview:\n{output[:1000]}")
    
    return {
        'versions': versions,
        'description': description
    }, None


def _get_all_modules_two_stage_streaming():
    """
    Generator that yields modules as they're discovered (for streaming).
    
    Yields:
        Dict with 'type' ('progress', 'module', 'complete', 'error') and relevant data
    """
    # Step 1: Get all module family names
    try:
        families, error = _get_module_families()
        if error:
            logger.error(f"Error getting module families during refresh: {error}")
            yield {'type': 'error', 'message': f'Failed to get module list: {error}'}
            return
        
        if not families:
            logger.error("No module families found during refresh")
            yield {'type': 'error', 'message': 'No module families found'}
            return
    except Exception as e:
        logger.error(f"Exception getting module families: {e}", exc_info=True)
        yield {'type': 'error', 'message': f'Exception: {str(e)}'}
        return
    
    total_families = len(families)
    yield {'type': 'progress', 'message': f'Found {total_families} module families', 'total': total_families, 'current': 0}
    
    # Step 2: Get details for each module family
    modules_data = {}
    failed_count = 0
    categories_config = _load_categories()
    
    for i, family_name in enumerate(families):
        # Log progress every 50 modules
        if i % 50 == 0:
            logger.info(f"Processing module {i+1}/{total_families}: {family_name}")
        
        yield {'type': 'progress', 'message': f'Processing {family_name}', 'total': total_families, 'current': i + 1}
        
        # Small delay to avoid overwhelming the module system
        if i > 0 and i % 10 == 0:
            time.sleep(0.05)
        
        try:
            details, error = _get_module_details(family_name)
        except Exception as e:
            logger.error(f"Exception getting details for {family_name}: {e}", exc_info=True)
            continue
        # If error is None, it means we should skip this module (not a fatal error)
        if error is not None:
            logger.warning(f"Failed to get details for {family_name}: {error}")
            failed_count += 1
            continue
        
        # If details is None, module was skipped (empty output, etc.)
        if details is None:
            continue
        
        if details and details.get('versions'):
            modules_data[family_name] = {
                'versions': details['versions'],
                'description': details.get('description', '')
            }
            
            # Group and yield this module immediately
            versions = details['versions']
            description = details.get('description', '')
            
            # Determine base name from first version
            first_version = versions[0]
            if '/' in first_version:
                parts = first_version.split('/')
                if len(parts) > 2:
                    base_name = '/'.join(parts[:-1])
                else:
                    base_name = parts[0]
            else:
                base_name = family_name
            
            category = _categorize_module(base_name, categories_config)
            sorted_versions = sorted(versions, key=_natural_sort_key) if versions else []
            
            grouped_module = {
                'name': base_name,
                'versions': sorted_versions,
                'description': description,
                'category': category
            }
            
            yield {'type': 'module', 'module': grouped_module}
    
    if failed_count > 0:
        yield {'type': 'progress', 'message': f'Failed to get details for {failed_count} modules', 'total': total_families, 'current': total_families}
    
    total_versions = sum(len(data['versions']) for data in modules_data.values())
    yield {'type': 'complete', 'message': f'Retrieved {total_versions} module versions across {len(modules_data)} modules', 'total_modules': len(modules_data)}


def _get_all_modules_two_stage() -> Tuple[Optional[Dict[str, Dict[str, Any]]], Optional[str]]:
    """
    Two-stage Lmod spider crawl to get all modules with all versions and descriptions.
    
    Step 1: Get all module family names
    Step 2: For each family, get all versions and description
    
    Returns:
        Tuple of (dict mapping module names to {'versions': [...], 'description': '...'}, error_message)
    """
    # Step 1: Get all module family names
    families, error = _get_module_families()
    if error:
        return None, error
    
    if not families:
        return None, "No module families found"
    
    logger.info(f"Found {len(families)} module families, fetching details...")
    
    # Step 2: Get details for each module family
    modules_data = {}
    failed_count = 0
    
    for i, family_name in enumerate(families):
        if i % 50 == 0:
            logger.info(f"Processing module {i+1}/{len(families)}: {family_name}")
        
        details, error = _get_module_details(family_name)
        # If error is None, it means we should skip this module (not a fatal error)
        if error is not None:
            logger.warning(f"Failed to get details for {family_name}: {error}")
            failed_count += 1
            continue
        
        # If details is None, module was skipped (empty output, etc.)
        if details is None:
            continue
        
        if details and details.get('versions'):
            modules_data[family_name] = {
                'versions': details['versions'],
                'description': details.get('description', '')
            }
    
    if failed_count > 0:
        logger.warning(f"Failed to get details for {failed_count} out of {len(families)} modules")
    
    total_versions = sum(len(data['versions']) for data in modules_data.values())
    logger.info(f"Retrieved {total_versions} module versions across {len(modules_data)} modules")
    return modules_data, None


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

def _natural_sort_key(text):
    """Generate a sort key for natural (numeric-aware) sorting.
    
    Splits text into alternating text and number parts for proper version sorting.
    Example: "Armadillo/11.4.3" -> ('Armadillo/', 11, '.', 4, '.', 3)
    """
    def convert(text_part):
        return int(text_part) if text_part.isdigit() else text_part.lower()
    
    return [convert(part) for part in re.split(r'(\d+)', text)]

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
        return 'Research Computing modules'
    
    # Special handling for cuda* modules (case-insensitive, but not all-caps CUDA)
    # Group all cuda10.0/*, cuda11.2/*, Cuda*, etc. under "Compilers/Toolchains"
    # Note: All-caps CUDA is handled by exact match above
    module_lower = module_name.lower()
    if module_lower.startswith('cuda') and module_name != 'CUDA':
        if 'cuda' in categories_config:
            return categories_config['cuda']
        return 'Compilers/Toolchains'
    
    # Default to Misc for unmatched modules
    return 'Misc'



def _group_modules_by_name(modules_data: Dict[str, Dict[str, Any]]):
    """Group modules by base name with descriptions.
    
    Args:
        modules_data: Dict mapping module family names to {'versions': [...], 'description': '...'}
    
    Returns:
        List of dicts with 'name' (base name), 'versions' (list of full names),
        'description' (description text), and 'category' (category name)
    """
    categories_config = _load_categories()
    result = []
    
    for family_name, data in modules_data.items():
        versions = data.get('versions', [])
        description = data.get('description', '')
        
        if not versions:
            continue
        
        # Determine base name from first version
        # For hierarchical modules like 'rc/3DSlicer/5.2.2',
        # base name is everything except the last segment
        # For simple modules like 'Armadillo/11.4.3', base is first segment
        first_version = versions[0]
        if '/' in first_version:
            parts = first_version.split('/')
            if len(parts) > 2:
                # Hierarchical: rc/3DSlicer/5.2.2 -> base = rc/3DSlicer
                base_name = '/'.join(parts[:-1])
            else:
                # Simple: Armadillo/11.4.3 -> base = Armadillo
                base_name = parts[0]
        else:
            base_name = family_name
        
        category = _categorize_module(base_name, categories_config)
        
        # Sort versions using natural (numeric-aware) sorting
        sorted_versions = sorted(versions, key=_natural_sort_key) if versions else []
        
        result.append({
            'name': base_name,
            'versions': sorted_versions,
            'description': description,
            'category': category
        })
    
    # Sort all modules alphabetically by name (case-insensitive)
    result.sort(key=lambda m: m['name'].lower())
    
    return result

def _get_cached_modules() -> List[Dict[str, Any]]:
    """Get modules from cache or fetch if cache is empty."""
    global _modules_cache
    
    if _modules_cache is not None:
        return _modules_cache
    
    # Fetch modules
    modules_data, error = _get_all_modules_two_stage()
    if error:
        logger.warning(f"Error getting modules: {error}, falling back to file")
        module_lines = _load_modules_from_file()
        # Convert old format to new format for compatibility
        if module_lines:
            modules_data = {}
            for line in module_lines:
                if '/' in line:
                    parts = line.split('/')
                    base_name = parts[0] if len(parts) == 2 else '/'.join(parts[:-1])
                    if base_name not in modules_data:
                        modules_data[base_name] = {'versions': [], 'description': ''}
                    modules_data[base_name]['versions'].append(line)
        else:
            modules_data = {}
    
    grouped_modules = _group_modules_by_name(modules_data) if modules_data else []
    _modules_cache = grouped_modules
    return grouped_modules


def _clear_modules_cache():
    """Clear the modules cache."""
    global _modules_cache
    _modules_cache = None


# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page."""
    grouped_modules = _get_cached_modules()
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
    """Return JSON list of modules from cache."""
    grouped_modules = _get_cached_modules()
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
        'unique_count': unique_count,
        'loading': False
    })


@modules_bp.route('/refresh-start', methods=['POST'])
def refresh_start():
    """Start refresh process by clearing cache."""
    global _streaming_in_progress
    
    with _streaming_lock:
        if _streaming_in_progress:
            return jsonify({'error': 'Refresh already in progress'}), 409
        
        _streaming_in_progress = True
        _clear_modules_cache()
    
    return jsonify({'status': 'started'})


@modules_bp.route('/refresh-stream')
def refresh_modules():
    """Stream fresh module data via SSE (GET endpoint for EventSource)."""
    global _streaming_in_progress
    
    def generate():
        global _streaming_in_progress
        try:
            all_modules = []
            for event in _get_all_modules_two_stage_streaming():
                if event['type'] == 'module':
                    all_modules.append(event['module'])
                    # Send module to client
                    yield f"data: {json.dumps(event)}\n\n"
                elif event['type'] == 'progress':
                    # Send progress update
                    yield f"data: {json.dumps(event)}\n\n"
                elif event['type'] == 'complete':
                    # Update cache with all collected modules
                    global _modules_cache
                    _modules_cache = sorted(all_modules, key=lambda m: m['name'].lower())
                    # Send completion
                    yield f"data: {json.dumps(event)}\n\n"
                elif event['type'] == 'error':
                    yield f"data: {json.dumps(event)}\n\n"
        finally:
            with _streaming_lock:
                _streaming_in_progress = False
    
    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no'
    })


@modules_bp.route('/refresh-status')
def refresh_status():
    """Check if refresh is in progress."""
    with _streaming_lock:
        return jsonify({'in_progress': _streaming_in_progress})