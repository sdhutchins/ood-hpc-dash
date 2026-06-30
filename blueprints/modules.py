import fcntl
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, render_template

from utils import find_binary

modules_bp = Blueprint('modules', __name__, url_prefix='/modules')
logger = logging.getLogger(__name__)

CATEGORIES_FILE = Path('config/module_categories.json')
MODULE_REFRESH_LOCK_FILE = Path('logs/modules_refresh.lock')
MODULE_SPIDER_COMMAND = 'module --redirect spider'

_modules_cache: list[dict[str, Any]] | None = None
_modules_cache_timestamp: float | None = None

_streaming_lock = threading.Lock()
_streaming_in_progress = False

BASH_PATHS = [
    '/bin/bash',
    '/usr/bin/bash',
]


@contextmanager
def _module_refresh_file_lock() -> Iterator[bool]:
    """Coordinate expensive Lmod spider work across Passenger workers."""
    MODULE_REFRESH_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with MODULE_REFRESH_LOCK_FILE.open('a', encoding='utf-8') as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return

        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _call_module_command(
    command: str,
    timeout: int = 30,
) -> tuple[str | None, str | None]:
    """
    Call a module command using bash -lc with explicit environment.

    Since module is a shell function, we must use a login shell and source lmod init.

    Args:
        command: Module command to run (e.g., 'module --redirect spider')
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
            # Some module commands output to stderr instead of stdout.
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
    except OSError as e:
        return None, f"Error calling module command: {str(e)}"


def _load_descriptions_cache() -> dict[str, str]:
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
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Error loading descriptions from module_categories.json: {e}")
        return {}


def _save_descriptions_cache(cache: dict[str, str]) -> None:
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
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Unable to read existing module categories: {e}")

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
    except (OSError, TypeError) as e:
        logger.warning(f"Error saving descriptions to module_categories.json: {e}")


def _new_module_entry() -> dict[str, object]:
    """Return the internal record used while parsing spider output."""
    return {'versions': [], 'description': ''}


def _add_versions(module_entry: dict[str, object], version_text: str) -> None:
    """Append module versions parsed from a comma-separated spider line."""
    versions = module_entry['versions']
    if not isinstance(versions, list):
        return

    for token in version_text.split(','):
        version = token.strip()
        if not version or version == '...' or version.endswith('...'):
            continue
        if '/' in version:
            versions.append(version)


def _set_description(
    module_entry: dict[str, object],
    description_lines: list[str],
) -> None:
    """Store normalized description text for one parsed module."""
    description = ' '.join(description_lines).strip()
    if description:
        module_entry['description'] = description


def _parse_module_spider_output(output: str) -> dict[str, dict[str, object]]:
    """
    Parse cache-backed `module --redirect spider` output.

    The normal spider output carries module families, versions, and short
    descriptions. Reading that single command lets Lmod use its spider cache
    instead of forcing this app to issue one detail command per module.
    """
    modules: dict[str, dict[str, object]] = {}
    current_family: str | None = None
    description_lines: list[str] = []
    parser_state: str | None = None

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith('---'):
            continue

        indent = len(line) - len(line.lstrip())
        entry_match = re.match(r'^\s{2}([^:\s][^:]*?):\s*(.*)$', line)
        if entry_match and 'The following' not in stripped:
            if current_family is not None:
                _set_description(modules[current_family], description_lines)

            current_family = entry_match.group(1).strip()
            modules.setdefault(current_family, _new_module_entry())
            _add_versions(modules[current_family], entry_match.group(2))
            description_lines = []
            parser_state = 'description'
            continue

        if current_family is None:
            continue

        if stripped.startswith('Description:'):
            parser_state = 'description'
            description = stripped.split('Description:', 1)[1].strip()
            if description:
                description_lines.append(description)
            continue

        if stripped == 'Versions:':
            parser_state = 'versions'
            continue

        if stripped.startswith((
            'Other possible modules',
            'You will need',
            'Help:',
            'Names marked',
        )):
            parser_state = None
            continue

        if parser_state == 'versions':
            _add_versions(modules[current_family], stripped)
            continue

        if parser_state == 'description' and indent >= 4:
            description_lines.append(stripped)

    if current_family is not None:
        _set_description(modules[current_family], description_lines)

    for family_name, module_entry in modules.items():
        versions = module_entry['versions']
        if isinstance(versions, list):
            module_entry['versions'] = sorted(
                set(versions),
                key=_natural_sort_key,
            )
        if not module_entry.get('description'):
            module_entry['description'] = ''

    return modules


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


def _module_record(
    family_name: str,
    module_entry: dict[str, object],
    categories_config: dict[str, str] | None,
    descriptions_cache: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build the module payload used by templates, JSON, and SSE events."""
    raw_versions = module_entry.get('versions', [])
    versions = raw_versions if isinstance(raw_versions, list) else []
    sorted_versions = sorted(versions, key=_natural_sort_key)
    base_name = _module_base_name(family_name, sorted_versions)
    description = module_entry.get('description')
    if not isinstance(description, str):
        description = ''
    if descriptions_cache and not description:
        description = descriptions_cache.get(family_name, '')

    return {
        'name': base_name,
        'versions': sorted_versions,
        'description': description,
        'category': _categorize_module(base_name, categories_config),
    }


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


def _module_records_from_spider_data(
    modules_dict: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """Build sorted frontend module records from parsed spider data."""
    categories_config = _load_categories()
    descriptions_cache = _load_descriptions_cache()
    grouped_modules = [
        _module_record(
            family_name,
            modules_dict[family_name],
            categories_config,
            descriptions_cache,
        )
        for family_name in sorted(modules_dict.keys())
    ]
    grouped_modules.sort(key=lambda module: str(module['name']).lower())
    return grouped_modules


def _cache_spider_descriptions(
    modules_dict: dict[str, dict[str, object]],
) -> None:
    """Persist descriptions found in spider output for existing update routes."""
    parsed_descriptions = {
        family_name: str(module_entry['description'])
        for family_name, module_entry in modules_dict.items()
        if module_entry.get('description')
    }
    if not parsed_descriptions:
        return

    descriptions_cache = _load_descriptions_cache()
    changed_descriptions = {
        family_name: description
        for family_name, description in parsed_descriptions.items()
        if descriptions_cache.get(family_name) != description
    }
    if not changed_descriptions:
        return

    descriptions_cache.update(changed_descriptions)
    _save_descriptions_cache(descriptions_cache)


def _get_all_modules_streaming() -> Iterator[dict[str, object]]:
    """
    Stream module records parsed from one cache-backed Lmod spider command.

    Yields:
        Dict with 'type' ('progress', 'module', 'complete', 'error').
    """
    try:
        yield {
            'type': 'progress',
            'message': 'Fetching module list from Lmod spider cache...',
            'total': 0,
            'current': 0,
        }

        output, error = _call_module_command(MODULE_SPIDER_COMMAND, timeout=60)
        if error:
            logger.error(f"Error calling {MODULE_SPIDER_COMMAND}: {error}")
            yield {'type': 'error', 'message': f'Failed to get module list: {error}'}
            return

        if not output or not output.strip():
            logger.error(f"{MODULE_SPIDER_COMMAND} returned empty output")
            yield {
                'type': 'error',
                'message': f'{MODULE_SPIDER_COMMAND} returned empty output',
            }
            return
    except (OSError, ValueError, TypeError) as e:
        logger.error(f"Exception getting module list: {e}", exc_info=True)
        yield {'type': 'error', 'message': f'Exception: {str(e)}'}
        return

    modules_dict = _parse_module_spider_output(output)
    grouped_modules = _module_records_from_spider_data(modules_dict)
    total_modules = len(grouped_modules)

    yield {
        'type': 'progress',
        'message': f'Found {total_modules} module families',
        'total': total_modules,
        'current': 0,
    }

    _cache_spider_descriptions(modules_dict)

    for current, module in enumerate(grouped_modules, 1):
        yield {
            'type': 'module',
            'module': module,
        }
        if current % 50 == 0 or current == total_modules:
            yield {
                'type': 'progress',
                'message': f'Displayed {current}/{total_modules} modules',
                'total': total_modules,
                'current': current,
            }

    total_versions = sum(
        len(module['versions'])
        for module in grouped_modules
        if isinstance(module.get('versions'), list)
    )
    yield {
        'type': 'complete',
        'message': (
            f'Retrieved {total_versions} module versions across '
            f'{total_modules} modules'
        ),
        'total_modules': total_modules,
    }


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
    except (OSError, json.JSONDecodeError) as e:
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

def _get_cached_modules() -> list[dict[str, Any]]:
    """Get modules from cache. Returns empty list if cache is not ready."""
    global _modules_cache

    if _modules_cache is not None:
        return _modules_cache

    # Cache not ready yet - return empty list
    # Preload will populate it on startup
    return []


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
def timestamp_to_datetime_filter(timestamp: float | None) -> str:
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


@modules_bp.route('/refresh-start', methods=['POST'])
def refresh_start():
    """Confirm a refresh can start before the client opens the SSE stream."""
    with _streaming_lock:
        if _streaming_in_progress:
            return jsonify({'error': 'Refresh already in progress'}), 409

    return jsonify({'status': 'started'})


@modules_bp.route('/refresh-stream')
def refresh_modules():
    """Stream fresh module data via SSE (GET endpoint for EventSource)."""
    global _streaming_in_progress

    def generate() -> Iterator[str]:
        global _streaming_in_progress
        error_message = None
        with _streaming_lock:
            if _streaming_in_progress:
                error_message = 'Refresh already in progress'
            else:
                _streaming_in_progress = True

        if error_message:
            yield _sse_event({'type': 'error', 'message': error_message})
            return

        try:
            with _module_refresh_file_lock() as lock_acquired:
                if not lock_acquired:
                    yield _sse_event({
                        'type': 'error',
                        'message': (
                            'Module refresh is already running in another '
                            'worker'
                        ),
                    })
                    return

                all_modules = []
                for event in _get_all_modules_streaming():
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


@modules_bp.route('/refresh-status')
def refresh_status():
    """Check if refresh is in progress."""
    with _streaming_lock:
        return jsonify({'in_progress': _streaming_in_progress})


def _preload_modules_cache():
    """
    Preload modules cache on app startup from cache-backed Lmod spider output.
    This ensures modules are ready immediately when user visits the modules page.
    """
    global _modules_cache, _modules_cache_timestamp

    logger.info("Preloading modules cache on startup...")
    try:
        with _module_refresh_file_lock() as lock_acquired:
            if not lock_acquired:
                logger.info(
                    "Skipping module preload; another worker is refreshing "
                    "module data"
                )
                return

            output, error = _call_module_command(
                MODULE_SPIDER_COMMAND,
                timeout=60,
            )
            if error:
                logger.warning(f"Failed to preload modules cache: {error}")
                return

            if not output or not output.strip():
                logger.warning(
                    f"{MODULE_SPIDER_COMMAND} returned empty output during "
                    "preload"
                )
                return

            modules_dict = _parse_module_spider_output(output)
            grouped_modules = _module_records_from_spider_data(modules_dict)
            _cache_spider_descriptions(modules_dict)

            logger.info(
                f"Preloaded {len(grouped_modules)} module families from "
                f"{MODULE_SPIDER_COMMAND}"
            )

            _modules_cache = grouped_modules
            _modules_cache_timestamp = time.time()

            total_versions = sum(len(m['versions']) for m in grouped_modules)
            logger.info(
                "Modules cache preloaded successfully: "
                f"{len(grouped_modules)} modules, {total_versions} total versions"
            )

    except (OSError, ValueError, TypeError) as e:
        logger.error(f"Error preloading modules cache: {e}", exc_info=True)
