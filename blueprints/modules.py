import fcntl
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, render_template

modules_bp = Blueprint('modules', __name__, url_prefix='/modules')
logger = logging.getLogger(__name__)

CATEGORIES_FILE = Path('config/module_categories.json')
MODULE_REFRESH_LOCK_FILE = Path('logs/modules_refresh.lock')

SPIDER_CACHE_DEFAULT = Path('/share/apps/sysCacheDir/spiderT.lua')
SPIDER_CACHE_ENV_VAR = 'OOD_HPC_DASH_SPIDER_CACHE'

_modules_cache: list[dict[str, Any]] | None = None
_modules_cache_timestamp: float | None = None

_streaming_lock = threading.Lock()
_streaming_in_progress = False


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


def _get_spider_cache_path() -> Path | None:
    """Return the Lmod spider cache file path if it exists."""
    env_path = os.environ.get(SPIDER_CACHE_ENV_VAR)
    if env_path:
        candidate = Path(env_path)
        if candidate.is_file():
            return candidate
        logger.warning(
            "%s=%s does not exist; trying default",
            SPIDER_CACHE_ENV_VAR,
            env_path,
        )

    if SPIDER_CACHE_DEFAULT.is_file():
        return SPIDER_CACHE_DEFAULT

    return None


def _parse_lua_string(text: str, pos: int) -> tuple[str, int]:
    """Parse a quoted Lua string starting at pos, return (value, new_pos)."""
    quote = text[pos]
    end = pos + 1
    while end < len(text):
        ch = text[end]
        if ch == '\\':
            end += 2
            continue
        if ch == quote:
            return text[pos + 1:end], end + 1
        end += 1
    return text[pos + 1:end], end


def _parse_lua_table(text: str, pos: int) -> tuple[dict | list, int]:
    """Parse a Lua table literal into a Python dict or list.

    Handles the subset of Lua syntax found in Lmod spiderT cache files:
    string keys (["key"] or bare identifiers), string/number/bool values,
    and nested tables. Array-style tables (sequential integer keys) are
    returned as lists.
    """
    pos += 1  # skip opening {
    result: dict[str, object] = {}
    array_items: list[object] = []
    has_string_keys = False
    idx = 0

    while pos < len(text):
        # skip whitespace and commas
        while pos < len(text) and text[pos] in ' \t\n\r,':
            pos += 1

        if pos >= len(text) or text[pos] == '}':
            pos += 1
            break

        # skip single-line comments
        if text[pos:pos + 2] == '--':
            nl = text.find('\n', pos)
            pos = nl + 1 if nl != -1 else len(text)
            continue

        key: str | None = None

        # ["string-key"] = value
        if text[pos] == '[' and pos + 1 < len(text) and text[pos + 1] in '"\'':
            key, pos = _parse_lua_string(text, pos + 1)
            # skip ] =
            while pos < len(text) and text[pos] in '] \t=':
                pos += 1
            has_string_keys = True

        # bare_key = value
        elif text[pos].isalpha() or text[pos] == '_':
            end = pos
            while end < len(text) and (text[end].isalnum() or text[end] == '_'):
                end += 1
            key = text[pos:end]
            pos = end
            while pos < len(text) and text[pos] in ' \t=':
                pos += 1
            has_string_keys = True

        # parse value
        if pos >= len(text):
            break

        value: object
        if text[pos] == '{':
            value, pos = _parse_lua_table(text, pos)
        elif text[pos] in '"\'':
            value, pos = _parse_lua_string(text, pos)
        elif text[pos:pos + 4] == 'true':
            value, pos = True, pos + 4
        elif text[pos:pos + 5] == 'false':
            value, pos = False, pos + 5
        elif text[pos] == '-' or text[pos].isdigit():
            end = pos + 1
            while end < len(text) and (text[end].isdigit() or text[end] in '.eE+-'):
                end += 1
            try:
                num_str = text[pos:end]
                value = int(num_str) if '.' not in num_str else float(num_str)
            except ValueError:
                value = text[pos:end]
            pos = end
        else:
            pos += 1
            continue

        if key is not None:
            result[key] = value
        else:
            array_items.append(value)
            idx += 1

    if not has_string_keys and array_items:
        return array_items, pos
    if array_items and not result:
        return array_items, pos
    return result, pos


def _parse_spider_cache(
    cache_path: Path,
) -> dict[str, dict[str, object]] | None:
    """Read spiderT.lua and convert it to the modules dict format.

    Returns the same shape as _parse_module_spider_output:
    {family_name: {'versions': [str, ...], 'description': str}}
    """
    try:
        raw = cache_path.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        logger.warning("Unable to read spider cache %s: %s", cache_path, exc)
        return None

    # Locate the spiderT table
    marker = 'spiderT = {'
    start = raw.find(marker)
    if start == -1:
        logger.warning("No spiderT table found in %s", cache_path)
        return None

    table_start = raw.index('{', start)
    try:
        spider_table, _ = _parse_lua_table(raw, table_start)
    except (IndexError, ValueError, RecursionError) as exc:
        logger.warning("Failed to parse Lua table in %s: %s", cache_path, exc)
        return None

    if not isinstance(spider_table, dict):
        logger.warning("spiderT is not a dict in %s", cache_path)
        return None

    # Flatten: spiderT[modulepath_dir][family_name].fileT[full_name]
    modules: dict[str, dict[str, object]] = {}

    for mpath_data in spider_table.values():
        if not isinstance(mpath_data, dict):
            continue

        for family_name, family_data in mpath_data.items():
            if not isinstance(family_data, dict):
                continue

            file_table = family_data.get('fileT', {})
            if not isinstance(file_table, dict):
                continue

            if family_name not in modules:
                modules[family_name] = {'versions': [], 'description': ''}

            entry = modules[family_name]
            versions = entry['versions']
            if not isinstance(versions, list):
                continue

            for full_name, version_data in file_table.items():
                if not isinstance(version_data, dict):
                    continue

                if '/' in full_name:
                    versions.append(full_name)

                # Extract description from whatis array
                if not entry.get('description'):
                    whatis = version_data.get('whatis')
                    if isinstance(whatis, list) and whatis:
                        desc = str(whatis[0]).strip()
                        if desc:
                            entry['description'] = desc
                    elif isinstance(whatis, str) and whatis.strip():
                        entry['description'] = whatis.strip()

            # Also check dirT for sub-hierarchy modules
            dir_table = family_data.get('dirT', {})
            if isinstance(dir_table, dict):
                for dir_data in dir_table.values():
                    if not isinstance(dir_data, dict):
                        continue
                    sub_file_table = dir_data.get('fileT', {})
                    if not isinstance(sub_file_table, dict):
                        continue
                    for full_name, version_data in sub_file_table.items():
                        if '/' in full_name:
                            versions.append(full_name)
                        if not entry.get('description') and isinstance(
                            version_data, dict
                        ):
                            whatis = version_data.get('whatis')
                            if isinstance(whatis, list) and whatis:
                                entry['description'] = str(whatis[0]).strip()

    # Deduplicate and sort versions
    for entry in modules.values():
        versions = entry.get('versions')
        if isinstance(versions, list):
            entry['versions'] = sorted(
                set(versions), key=_natural_sort_key
            )
        if not entry.get('description'):
            entry['description'] = ''

    logger.info(
        "Parsed %d module families from spider cache %s",
        len(modules),
        cache_path,
    )
    return modules


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
    Stream module records, preferring the spider cache file over subprocess.

    Yields:
        Dict with 'type' ('progress', 'module', 'complete', 'error').
    """
    yield {
        'type': 'progress',
        'message': 'Reading Lmod spider cache...',
        'total': 0,
        'current': 0,
    }

    cache_path = _get_spider_cache_path()
    if not cache_path:
        yield {
            'type': 'error',
            'message': (
                f'Lmod spider cache not found at {SPIDER_CACHE_DEFAULT} '
                f'(override with {SPIDER_CACHE_ENV_VAR})'
            ),
        }
        return

    try:
        modules_dict = _parse_spider_cache(cache_path)
    except (OSError, ValueError, TypeError) as e:
        logger.error(f"Exception reading spider cache: {e}", exc_info=True)
        yield {'type': 'error', 'message': f'Exception: {str(e)}'}
        return

    if not modules_dict:
        yield {'type': 'error', 'message': f'Failed to parse {cache_path}'}
        return

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


def _preload_modules_cache() -> None:
    """Preload modules cache on startup by reading the Lmod spider cache."""
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

            cache_path = _get_spider_cache_path()
            if not cache_path:
                logger.warning(
                    "Lmod spider cache not found at %s (override with %s)",
                    SPIDER_CACHE_DEFAULT,
                    SPIDER_CACHE_ENV_VAR,
                )
                return

            modules_dict = _parse_spider_cache(cache_path)
            if not modules_dict:
                logger.warning("Failed to parse spider cache at %s", cache_path)
                return

            grouped_modules = _module_records_from_spider_data(modules_dict)
            _cache_spider_descriptions(modules_dict)

            _modules_cache = grouped_modules
            _modules_cache_timestamp = time.time()

            total_versions = sum(len(m['versions']) for m in grouped_modules)
            logger.info(
                "Modules cache preloaded: %d families, %d total versions",
                len(grouped_modules),
                total_versions,
            )

    except (OSError, ValueError, TypeError) as e:
        logger.error(f"Error preloading modules cache: {e}", exc_info=True)
