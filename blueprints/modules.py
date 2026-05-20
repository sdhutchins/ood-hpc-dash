import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, Response, jsonify, render_template

from utils import find_binary

modules_bp = Blueprint('modules', __name__, url_prefix='/modules')
logger = logging.getLogger(__name__)

MODULES_FILE = Path('logs/modules.txt')
CATEGORIES_FILE = Path('config/module_categories.json')

_modules_cache: Optional[List[Dict[str, Any]]] = None
_modules_cache_timestamp: Optional[float] = None

_streaming_lock = threading.Lock()
_streaming_in_progress = False

BASH_PATHS = [
    '/bin/bash',
    '/usr/bin/bash',
]


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
    bash_path = find_binary(BASH_PATHS)
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
        # subprocess.run with timeout should already kill the process
        # Don't log timeouts as warnings - they're expected for some slow modules
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


def _load_descriptions_cache() -> Dict[str, str]:
    """Load module descriptions from module_categories.json.
    
    Returns:
        Dictionary mapping module family names to descriptions
    """
    if not CATEGORIES_FILE.exists():
        return {}
    
    try:
        with CATEGORIES_FILE.open('r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Check if descriptions key exists
            descriptions = data.get('descriptions', {})
            if isinstance(descriptions, dict):
                logger.debug(f"Loaded {len(descriptions)} cached descriptions from module_categories.json")
                return descriptions
        return {}
    except Exception as e:
        logger.warning(f"Error loading descriptions from module_categories.json: {e}")
        return {}


def _save_descriptions_cache(cache: Dict[str, str]) -> None:
    """Save module descriptions to module_categories.json, preserving existing categories.
    
    Args:
        cache: Dictionary mapping module family names to descriptions
    """
    try:
        CATEGORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing categories file
        existing_data = {}
        if CATEGORIES_FILE.exists():
            try:
                with CATEGORIES_FILE.open('r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            except Exception:
                pass
        
        # Ensure existing_data is a dict
        if not isinstance(existing_data, dict):
            existing_data = {}
        
        # Preserve categories (everything except 'descriptions')
        categories = {k: v for k, v in existing_data.items() if k != 'descriptions'}
        
        # Merge descriptions
        existing_descriptions = existing_data.get('descriptions', {})
        if isinstance(existing_descriptions, dict):
            existing_descriptions.update(cache)
            descriptions = existing_descriptions
        else:
            descriptions = cache
        
        # Combine categories and descriptions
        combined_data = {**categories, 'descriptions': descriptions}
        
        # Save to file
        with CATEGORIES_FILE.open('w', encoding='utf-8') as f:
            json.dump(combined_data, f, indent=4, ensure_ascii=False)
        logger.debug(f"Saved {len(descriptions)} descriptions to module_categories.json")
    except Exception as e:
        logger.warning(f"Error saving descriptions to module_categories.json: {e}")


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


def _parse_module_spider_output(output: str) -> Dict[str, List[str]]:
    """
    Parse 'module -t spider' output to extract all modules with versions.
    
    Returns:
        Dict mapping module family names to lists of full module/version strings
    """
    modules_dict = {}
    for line in output.split('\n'):
        line = line.strip()
        # Skip empty lines and lines ending with '/' (directories)
        if not line or line.endswith('/'):
            continue
        
        # If line contains '/', it's a full module/version
        if '/' in line:
            parts = line.split('/')
            if len(parts) >= 2:
                # Determine family name
                if len(parts) > 2:
                    family_name = '/'.join(parts[:-1])
                else:
                    family_name = parts[0]
                
                if family_name not in modules_dict:
                    modules_dict[family_name] = []
                modules_dict[family_name].append(line)
        else:
            # No '/', treat as family name with no versions yet
            if line not in modules_dict:
                modules_dict[line] = []
    
    return modules_dict


def _module_base_name(family_name: str, versions: list[str]) -> str:
    """Return the display name shared by all versions in a module family."""
    if not versions:
        return family_name

    parts = versions[0].split('/')
    if len(parts) > 2:
        return '/'.join(parts[:-1])
    if len(parts) == 2:
        return parts[0]
    return family_name


def _module_name_map(modules_dict: dict[str, list[str]]) -> dict[str, str]:
    """Map Lmod family names to the display names used by the frontend."""
    return {
        family_name: _module_base_name(
            family_name,
            sorted(versions, key=_natural_sort_key),
        )
        for family_name, versions in modules_dict.items()
    }


def _module_record(
    family_name: str,
    versions: list[str],
    categories_config: dict[str, str] | None,
    description: str = '',
) -> dict[str, object]:
    """Build the module payload used by templates, JSON, and SSE events."""
    sorted_versions = sorted(versions, key=_natural_sort_key)
    base_name = _module_base_name(family_name, sorted_versions)

    return {
        'name': base_name,
        'versions': sorted_versions,
        'description': description,
        'category': _categorize_module(base_name, categories_config),
    }


def _fetch_module_description(
    family_name: str,
    descriptions_cache: dict[str, str] | None = None,
) -> tuple[str | None, str | None, str]:
    """Fetch one module description while treating slow Lmod modules as skips."""
    if descriptions_cache and family_name in descriptions_cache:
        logger.debug(f"Using cached description for {family_name}")
        return descriptions_cache[family_name], None, family_name

    try:
        details, error = _get_module_details(family_name)
        if error is not None:
            if "timed out" not in error.lower():
                logger.debug(f"Module {family_name} error: {error}")
            return None, None, family_name
        if details is None:
            return None, None, family_name
        return details.get('description', ''), None, family_name
    except Exception as e:
        logger.debug(f"Exception getting description for {family_name}: {e}")
        return None, None, family_name


def _description_results(
    family_names: list[str],
    descriptions_cache: dict[str, str] | None = None,
    max_workers: int = 20,
) -> Iterator[tuple[int, str, str | None, str | None]]:
    """Yield module-description results as worker threads complete."""
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_family = {
            executor.submit(
                _fetch_module_description,
                family_name,
                descriptions_cache,
            ): family_name
            for family_name in family_names
        }

        for completed, future in enumerate(as_completed(future_to_family), 1):
            family_name = future_to_family[future]
            try:
                description, error, _ = future.result()
                yield completed, family_name, description, error
            except Exception as e:
                logger.debug(f"Error processing description for {family_name}: {e}")
                yield completed, family_name, None, str(e)


def _description_events(
    family_names: list[str],
    module_name_map: dict[str, str],
    descriptions_cache: dict[str, str],
) -> Iterator[dict[str, object]]:
    """Yield shared progress and update events for description streams."""
    total_families = len(family_names)
    failed_count = 0
    new_descriptions: dict[str, str] = {}

    for completed, family_name, description, error in _description_results(
        family_names,
        descriptions_cache,
    ):
        if completed % 10 == 0 or completed == total_families:
            yield {
                'type': 'progress',
                'message': f'Loading descriptions: {completed}/{total_families}',
                'total': total_families,
                'current': completed,
            }

        if error is not None:
            failed_count += 1
            continue

        if description is None:
            description = ''

        if description and family_name not in descriptions_cache:
            new_descriptions[family_name] = description

        yield {
            'type': 'module_update',
            'module_name': module_name_map.get(family_name, family_name),
            'description': description,
        }

    if failed_count > 0:
        yield {
            'type': 'progress',
            'message': f'Failed to get descriptions for {failed_count} modules',
            'total': total_families,
            'current': total_families,
        }

    if new_descriptions:
        descriptions_cache.update(new_descriptions)
        _save_descriptions_cache(descriptions_cache)
        logger.info(f"Added {len(new_descriptions)} new descriptions to cache")


def _sse_event(event: dict[str, object]) -> str:
    """Serialize one server-sent event payload."""
    return f"data: {json.dumps(event)}\n\n"


def _sse_response(events: Iterator[str]) -> Response:
    """Return a consistent SSE response for module streaming endpoints."""
    return Response(
        events,
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


def _get_all_modules_two_stage_streaming():
    """
    Generator that yields modules as they're discovered (for streaming).
    
    Strategy:
    1. Get all modules and versions from 'module -t spider' immediately
    2. Display them in table right away (without descriptions)
    3. Then fetch descriptions in background and update as they arrive
    
    Yields:
        Dict with 'type' ('progress', 'module', 'module_update', 'complete', 'error') and relevant data
    """
    # Step 1: Get all modules and versions from 'module -t spider'
    try:
        yield {'type': 'progress', 'message': 'Fetching module list...', 'total': 0, 'current': 0}
        
        output, error = _call_module_command('module -t spider', timeout=60)
        if error:
            logger.error(f"Error calling module -t spider: {error}")
            yield {'type': 'error', 'message': f'Failed to get module list: {error}'}
            return
        
        if not output or not output.strip():
            logger.error("module -t spider returned empty output")
            yield {'type': 'error', 'message': 'module -t spider returned empty output'}
            return
    except Exception as e:
        logger.error(f"Exception getting module list: {e}", exc_info=True)
        yield {'type': 'error', 'message': f'Exception: {str(e)}'}
        return
    
    # Parse all modules and versions immediately
    modules_dict = _parse_module_spider_output(output)
    families = sorted(modules_dict.keys())
    total_families = len(families)
    
    yield {
        'type': 'progress',
        'message': f'Found {total_families} module families with versions',
        'total': total_families,
        'current': 0,
    }
    
    categories_config = _load_categories()
    module_name_map = _module_name_map(modules_dict)
    
    for family_name in families:
        yield {
            'type': 'module',
            'module': _module_record(
                family_name,
                modules_dict[family_name],
                categories_config,
            ),
        }
    
    yield {'type': 'descriptions_start', 'message': 'Loading descriptions...'}
    yield {
        'type': 'progress',
        'message': f'Displayed {total_families} modules, loading descriptions...',
        'total': total_families,
        'current': 0,
    }
    
    descriptions_cache = _load_descriptions_cache()
    yield from _description_events(families, module_name_map, descriptions_cache)
    
    yield {'type': 'descriptions_complete', 'message': 'All descriptions loaded'}
    total_versions = sum(len(versions) for versions in modules_dict.values())
    yield {
        'type': 'complete',
        'message': (
            f'Retrieved {total_versions} module versions across '
            f'{len(modules_dict)} modules'
        ),
        'total_modules': len(modules_dict),
    }


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
    """Load module categories from JSON file (excluding descriptions key).
    
    Returns:
        Dictionary mapping module names to categories, or None on error
    """
    try:
        if not CATEGORIES_FILE.exists():
            logger.warning(f"Categories file not found: {CATEGORIES_FILE}")
            return None
        
        with CATEGORIES_FILE.open('r', encoding='utf-8') as f:
            data = json.load(f)
        
        if isinstance(data, dict):
            # Return only categories, exclude 'descriptions' key
            categories = {k: v for k, v in data.items() if k != 'descriptions'}
            return categories
        return None
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

        result.append(
            _module_record(
                family_name,
                versions,
                categories_config,
                description,
            )
        )
    
    # Sort all modules alphabetically by name (case-insensitive)
    result.sort(key=lambda m: m['name'].lower())
    
    return result

def _get_cached_modules() -> List[Dict[str, Any]]:
    """Get modules from cache. Returns empty list if cache is not ready."""
    global _modules_cache
    
    if _modules_cache is not None:
        return _modules_cache
    
    # Cache not ready yet - return empty list
    # Preload will populate it on startup
    return []


def _clear_modules_cache():
    """Clear the modules cache."""
    global _modules_cache, _modules_cache_timestamp
    _modules_cache = None
    _modules_cache_timestamp = None


def _modules_by_category(
    grouped_modules: list[dict[str, object]],
) -> tuple[dict[str, list[dict[str, object]]], list[str]]:
    """Group cached modules for shared template and JSON responses."""
    modules_by_category: dict[str, list[dict[str, object]]] = {}
    for module in grouped_modules:
        category = str(module['category'])
        modules_by_category.setdefault(category, []).append(module)

    category_order = sorted(modules_by_category.keys())
    if 'Misc' in category_order:
        category_order.remove('Misc')
        category_order.append('Misc')

    return modules_by_category, category_order


@modules_bp.app_template_filter('timestamp_to_datetime')
def timestamp_to_datetime_filter(timestamp: Optional[float]) -> str:
    """Convert Unix timestamp to formatted datetime string."""
    if not timestamp:
        return 'Unknown'
    try:
        dt = datetime.fromtimestamp(float(timestamp))
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        return 'Unknown'


@modules_bp.route('/')
def modules():
    """Render the modules page."""
    grouped_modules = _get_cached_modules()
    unique_count = len(grouped_modules)
    cache_exists = _modules_cache is not None and len(grouped_modules) > 0
    modules_by_category, category_order = _modules_by_category(grouped_modules)
    cache_timestamp = _modules_cache_timestamp if _modules_cache_timestamp else None
    
    return render_template(
        'modules.html',
        modules=grouped_modules,
        modules_by_category=modules_by_category,
        category_order=category_order,
        unique_count=unique_count,
        cache_empty=not cache_exists,
        cache_timestamp=cache_timestamp
    )

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of modules from cache."""
    grouped_modules = _get_cached_modules()
    unique_count = len(grouped_modules)
    modules_by_category, category_order = _modules_by_category(grouped_modules)
    
    return jsonify({
        'modules': grouped_modules,
        'modules_by_category': modules_by_category,
        'category_order': category_order,
        'unique_count': unique_count,
        'loading': False
    })


@modules_bp.route('/descriptions-status')
def descriptions_status():
    """Return status of module descriptions loading and file modification time."""
    try:
        file_mtime = None
        if CATEGORIES_FILE.exists():
            file_mtime = CATEGORIES_FILE.stat().st_mtime
        
        return jsonify({
            'file_exists': CATEGORIES_FILE.exists(),
            'file_mtime': file_mtime,
            'loading': _streaming_in_progress,
        })
    except Exception as e:
        logger.error(f"Error getting descriptions status: {e}")
        return jsonify({
            'file_exists': False,
            'file_mtime': None,
            'loading': False,
            'error': str(e),
        })


@modules_bp.route('/descriptions-update')
def descriptions_update():
    """Return updated descriptions from cache for modules on the page."""
    try:
        descriptions_cache = _load_descriptions_cache()
        
        # Get all modules from cache to map family names to base names
        grouped_modules = _get_cached_modules()
        
        # Build mapping of base_name -> description
        # We need to map from module family names to base names
        base_name_to_description = {}
        
        # For each module, try to find its description
        # Module names in the cache are base names (e.g., "zlib"), but descriptions are keyed by family names
        # We need to reverse-engineer the family name from the base name
        for module in grouped_modules:
            base_name = module['name']
            # Try to find description by checking if base_name matches a family name
            # or if any family name starts with base_name
            description = ''
            for family_name, desc in descriptions_cache.items():
                # Check if family name matches base name or starts with it
                if family_name == base_name or family_name.startswith(base_name + '/'):
                    description = desc
                    break
            
            if description:
                base_name_to_description[base_name] = description
        
        return jsonify({
            'descriptions': base_name_to_description,
            'count': len(base_name_to_description),
        })
    except Exception as e:
        logger.error(f"Error getting descriptions update: {e}")
        return jsonify({
            'descriptions': {},
            'count': 0,
            'error': str(e),
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


@modules_bp.route('/load-descriptions', methods=['POST'])
def load_descriptions():
    """Load descriptions for cached modules without clearing cache."""
    global _streaming_in_progress
    
    with _streaming_lock:
        if _streaming_in_progress:
            return jsonify({'error': 'Description loading already in progress'}), 409
        
        if _modules_cache is None or len(_modules_cache) == 0:
            return jsonify({'error': 'No cached modules to load descriptions for'}), 400
        
        _streaming_in_progress = True
    
    return jsonify({'status': 'started'})


@modules_bp.route('/refresh-stream')
def refresh_modules():
    """Stream fresh module data via SSE (GET endpoint for EventSource)."""
    global _streaming_in_progress
    
    def generate() -> Iterator[str]:
        global _streaming_in_progress
        try:
            all_modules = []
            for event in _get_all_modules_two_stage_streaming():
                if event['type'] == 'module':
                    all_modules.append(event['module'])
                    yield _sse_event(event)
                elif event['type'] == 'complete':
                    global _modules_cache, _modules_cache_timestamp
                    _modules_cache = sorted(
                        all_modules,
                        key=lambda m: m['name'].lower(),
                    )
                    _modules_cache_timestamp = time.time()
                    yield _sse_event(event)
                elif event['type'] in {'progress', 'error'}:
                    yield _sse_event(event)
        finally:
            with _streaming_lock:
                _streaming_in_progress = False
    
    return _sse_response(generate())


@modules_bp.route('/descriptions-stream')
def stream_descriptions():
    """Stream descriptions for all module families, using cache when available.
    Fetches descriptions for ALL families from module -t spider, not just cached ones.
    Only fetches new descriptions and appends them to the static cache."""
    global _streaming_in_progress
    
    def generate() -> Iterator[str]:
        global _streaming_in_progress
        try:
            yield _sse_event({
                'type': 'progress',
                'message': 'Fetching all module families...',
                'total': 0,
                'current': 0,
            })
            
            output, error = _call_module_command('module -t spider', timeout=60)
            if error:
                yield _sse_event({
                    'type': 'error',
                    'message': f'Failed to get module list: {error}',
                })
                return
            
            if not output or not output.strip():
                yield _sse_event({
                    'type': 'error',
                    'message': 'module -t spider returned empty output',
                })
                return
            
            modules_dict = _parse_module_spider_output(output)
            all_families = sorted(modules_dict.keys())
            total_families = len(all_families)
            yield _sse_event({
                'type': 'descriptions_start',
                'message': (
                    f'Loading descriptions for {total_families} module families...'
                ),
            })
            
            module_name_map = _module_name_map(modules_dict)
            descriptions_cache = _load_descriptions_cache()

            for event in _description_events(
                all_families,
                module_name_map,
                descriptions_cache,
            ):
                yield _sse_event(event)
            
            yield _sse_event({
                'type': 'descriptions_complete',
                'message': 'All descriptions loaded',
            })
        finally:
            with _streaming_lock:
                _streaming_in_progress = False
    
    return _sse_response(generate())


@modules_bp.route('/refresh-status')
def refresh_status():
    """Check if refresh is in progress."""
    with _streaming_lock:
        return jsonify({'in_progress': _streaming_in_progress})


def _preload_modules_cache():
    """
    Preload modules cache on app startup by running module -t spider.
    This ensures modules are ready immediately when user visits the modules page.
    """
    global _modules_cache, _modules_cache_timestamp
    
    logger.info("Preloading modules cache on startup...")
    try:
        # Get all modules and versions from module -t spider
        output, error = _call_module_command('module -t spider', timeout=60)
        if error:
            logger.warning(f"Failed to preload modules cache: {error}")
            return
        
        if not output or not output.strip():
            logger.warning("module -t spider returned empty output during preload")
            return
        
        # Parse modules immediately
        modules_dict = _parse_module_spider_output(output)
        families = sorted(modules_dict.keys())
        total_families = len(families)
        
        logger.info(f"Preloaded {total_families} module families from module -t spider")
        
        categories_config = _load_categories()
        grouped_modules = [
            _module_record(
                family_name,
                modules_dict[family_name],
                categories_config,
            )
            for family_name in families
        ]
        grouped_modules.sort(key=lambda m: m['name'].lower())

        _modules_cache = grouped_modules
        _modules_cache_timestamp = time.time()
        
        total_versions = sum(len(m['versions']) for m in grouped_modules)
        logger.info(
            "Modules cache preloaded successfully: "
            f"{len(grouped_modules)} modules, {total_versions} total versions"
        )
        
    except Exception as e:
        logger.error(f"Error preloading modules cache: {e}", exc_info=True)


def _preload_module_descriptions():
    """
    Preload module descriptions on app startup.
    Fetches descriptions for all module families and saves to module_categories.json.
    Only fetches descriptions for families not already in the cache.
    """
    logger.info("Preloading module descriptions on startup...")
    try:
        # Get all module families from module -t spider
        output, error = _call_module_command('module -t spider', timeout=60)
        if error:
            logger.warning(f"Failed to get module list for descriptions preload: {error}")
            return
        
        if not output or not output.strip():
            logger.warning("module -t spider returned empty output during descriptions preload")
            return
        
        # Parse all modules to get ALL families
        modules_dict = _parse_module_spider_output(output)
        all_families = sorted(modules_dict.keys())
        total_families = len(all_families)
        
        logger.info(
            f"Found {total_families} module families, "
            "checking for missing descriptions..."
        )
        
        # Load existing descriptions cache
        descriptions_cache = _load_descriptions_cache()
        
        # Find families missing descriptions
        missing_families = [f for f in all_families if f not in descriptions_cache]
        
        if not missing_families:
            logger.info("All module descriptions already cached")
            return
        
        logger.info(
            f"Fetching descriptions for {len(missing_families)} "
            "new module families..."
        )
        
        new_descriptions = {}
        failed_count = 0

        for completed, family_name, description, error in _description_results(
            missing_families,
        ):
            if error is not None:
                failed_count += 1
                new_descriptions[family_name] = ''
            elif description is not None:
                new_descriptions[family_name] = description
            else:
                new_descriptions[family_name] = ''
                failed_count += 1

            if completed % 50 == 0 or completed == len(missing_families):
                if new_descriptions:
                    _save_descriptions_cache(new_descriptions)
                    logger.info(
                        "Preloaded descriptions: "
                        f"{completed}/{len(missing_families)} "
                        f"({len(new_descriptions)} saved so far, "
                        f"{failed_count} failed)"
                    )
                else:
                    logger.info(
                        "Preloaded descriptions: "
                        f"{completed}/{len(missing_families)} "
                        f"({failed_count} failed)"
                    )
        
        # Final save (in case there were fewer than 50 items)
        if new_descriptions:
            _save_descriptions_cache(new_descriptions)
            logger.info(
                f"Preloaded {len(new_descriptions)} new descriptions to "
                f"module_categories.json ({failed_count} failed)"
            )
        else:
            logger.info(f"No new descriptions to save ({failed_count} failed)")
        
    except Exception as e:
        logger.error(f"Error preloading module descriptions: {e}", exc_info=True)
