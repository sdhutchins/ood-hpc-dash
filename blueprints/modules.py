from flask import Blueprint, render_template, jsonify
from pathlib import Path
import json

import logging
import time
from typing import List, Dict, Optional

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

# Cache for Spider instance and results
_cached_spider = None
_cached_modules = None
_cached_unique_count = 0
_cache_timestamp = 0
_spider_initializing = False  # Flag to prevent multiple simultaneous initializations
CACHE_TTL = 300  # Cache for 5 minutes (300 seconds)
CACHE_FILE = Path('logs/modules_cache.json')  # File-based cache that survives app restarts

def _load_cache_from_file() -> Optional[dict]:
    """Load cache from file if it exists and is valid."""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, 'r') as f:
                cache_data = json.load(f)
            cache_time = cache_data.get('timestamp', 0)
            current_time = time.time()
            cache_age = current_time - cache_time
            
            if cache_age < CACHE_TTL:
                logger.info(f"Loaded cache from file (age: {cache_age:.1f}s)")
                return cache_data
            else:
                logger.info(f"File cache expired (age: {cache_age:.1f}s)")
        else:
            logger.info("No cache file found")
    except Exception as e:
        logger.error(f"Error loading cache from file: {e}", exc_info=True)
    return None

def _save_cache_to_file(modules: List[Dict], unique_count: int):
    """Save cache to file so it survives app restarts."""
    try:
        CACHE_FILE.parent.mkdir(exist_ok=True)
        cache_data = {
            'modules': modules,
            'unique_count': unique_count,
            'timestamp': time.time()
        }
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache_data, f)
        logger.info(f"Saved cache to file: {CACHE_FILE}")
    except Exception as e:
        logger.error(f"Error saving cache to file: {e}", exc_info=True)

def _get_spider_instance():
    """Get or create cached Spider instance."""
    global _cached_spider, _spider_initializing
    
    if _cached_spider is not None:
        logger.info("Reusing cached Spider instance")
        return _cached_spider
    
    # Check if another request is already initializing
    if _spider_initializing:
        logger.info("Spider is already being initialized by another request, returning None")
        return None
    
    # Start initialization
    _spider_initializing = True
    logger.info("Creating new Spider instance (will be cached)")
    logger.warning("Spider initialization may take 30-60 seconds on large HPC systems...")
    start_time = time.time()
    try:
        _cached_spider = Spider()
        elapsed = time.time() - start_time
        logger.info(f"Spider instance created and cached in {elapsed:.2f} seconds")
        _spider_initializing = False
        return _cached_spider
    except Exception as e:
        _spider_initializing = False
        logger.error(f"Failed to create Spider instance: {e}", exc_info=True)
        raise

def get_available_modules(spider=None) -> List[Dict[str, str]]:
    """
    Get list of available modules using lmodule Spider.
    First gets unique module names, then retrieves all modules for those names.
    Returns list of dictionaries with name, version, and location.
    
    Args:
        spider: Optional Spider instance to reuse. If None, creates a new one.
    """
    logger.info("get_available_modules() called")
    
    if not LMODULE_AVAILABLE:
        logger.error("lmodule package not available")
        return []
    
    try:
        # Create Spider if not provided
        if spider is None:
            logger.info("Creating Spider instance in get_available_modules")
            try:
                spider = Spider()
                logger.info("Spider instance created in get_available_modules")
            except Exception as spider_error:
                logger.error(f"Failed to create Spider in get_available_modules: {spider_error}", exc_info=True)
                raise
        
        # Get unique module names (e.g., ['CUDA', 'lmod', 'GCC'])
        logger.info("Calling spider.get_names()")
        try:
            unique_names = spider.get_names()
            logger.info(f"Got {len(unique_names)} unique names")
        except Exception as names_error:
            logger.error(f"Error in spider.get_names(): {names_error}", exc_info=True)
            raise
        
        # Get all modules filtered by those names
        # This returns all versions of each module
        logger.info("Calling spider.get_modules() with unique names")
        try:
            modules_dict = spider.get_modules(unique_names)
            logger.info(f"Got {len(modules_dict)} modules from spider")
        except Exception as modules_error:
            logger.error(f"Error in spider.get_modules(): {modules_error}", exc_info=True)
            raise

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
    global _cached_modules, _cached_unique_count, _cache_timestamp
    
    logger.info("Modules page route accessed")
    
    if not LMODULE_AVAILABLE:
        logger.error("lmodule package not available")
        return render_template('modules.html', modules=[], unique_count=0)
    
    # Always return page immediately, let JavaScript fetch data asynchronously
    # This prevents blocking on Spider initialization
    current_time = time.time()
    cache_age = current_time - _cache_timestamp
    
    # Check in-memory cache first
    if _cached_modules is not None and cache_age < CACHE_TTL:
        logger.info(f"Using in-memory cached modules data for initial render (age: {cache_age:.1f}s)")
        return render_template('modules.html', modules=_cached_modules, unique_count=_cached_unique_count)
    
    # Check file cache
    file_cache = _load_cache_from_file()
    if file_cache:
        _cached_modules = file_cache.get('modules', [])
        _cached_unique_count = file_cache.get('unique_count', 0)
        _cache_timestamp = file_cache.get('timestamp', 0)
        logger.info(f"Using file cached modules data for initial render")
        return render_template('modules.html', modules=_cached_modules, unique_count=_cached_unique_count)
    
    # No cache or expired - return empty page, JavaScript will fetch
    logger.info("No cache available, returning empty page (data will load via JavaScript)")
    return render_template('modules.html', modules=[], unique_count=0)

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of available modules."""
    global _cached_modules, _cached_unique_count, _cache_timestamp
    
    logger.info("Modules list endpoint accessed")
    
    if not LMODULE_AVAILABLE:
        return jsonify({'modules': [], 'unique_count': 0, 'error': 'lmodule not available'})
    
    # Check in-memory cache first
    current_time = time.time()
    cache_age = current_time - _cache_timestamp
    
    if _cached_modules is not None and cache_age < CACHE_TTL:
        logger.info(f"Returning in-memory cached data (age: {cache_age:.1f}s)")
        return jsonify({
            'modules': _cached_modules,
            'unique_count': _cached_unique_count
        })
    
    # If in-memory cache is stale, check file cache
    file_cache = _load_cache_from_file()
    if file_cache:
        # Load from file and update in-memory cache
        _cached_modules = file_cache.get('modules', [])
        _cached_unique_count = file_cache.get('unique_count', 0)
        _cache_timestamp = file_cache.get('timestamp', 0)
        logger.info(f"Loaded cache from file, returning data")
        return jsonify({
            'modules': _cached_modules,
            'unique_count': _cached_unique_count
        })
    
    # If we have stale in-memory cache, return it
    if _cached_modules is not None:
        logger.info("Returning stale in-memory cache")
        return jsonify({
            'modules': _cached_modules,
            'unique_count': _cached_unique_count,
            'stale': True,
            'message': 'Refreshing data, please refresh page in a moment'
        })
    
    # No cache at all - check if Spider is already initializing
    global _spider_initializing
    if _spider_initializing:
        logger.info("Spider is already being initialized, returning wait message")
        return jsonify({
            'modules': [],
            'unique_count': 0,
            'loading': True,
            'message': 'Module system is being initialized. Please wait and the page will automatically retry.'
        })
    
    # No cache and not initializing - start initialization
    logger.info("No cache available, creating Spider (this may take 30-60 seconds)")
    unique_count = 0
    modules_list = []
    
    try:
        # Get cached or create new Spider instance (this may take 30-60 seconds)
        spider = _get_spider_instance()
        
        # If Spider is being initialized by another request, return "still initializing" message
        if spider is None:
            logger.info("Spider initialization started by another request, returning wait message")
            return jsonify({
                'modules': [],
                'unique_count': 0,
                'loading': True,
                'message': 'Module system is being initialized. Please wait and the page will automatically retry.'
            })
        
        logger.info("Getting unique module names")
        unique_names = spider.get_names()
        logger.info(f"get_names() returned {len(unique_names)} names")
        unique_count = len(unique_names)
        
        # Get modules using cached spider
        logger.info("Calling get_available_modules() with cached spider")
        modules_list = get_available_modules(spider=spider)
        logger.info(f"Retrieved {len(modules_list)} modules")
        
        # Update in-memory and file cache
        _cached_modules = modules_list
        _cached_unique_count = unique_count
        _cache_timestamp = current_time
        _save_cache_to_file(modules_list, unique_count)
        logger.info(f"Cache updated in memory and file with {len(modules_list)} modules")
            
    except Exception as e:
        logger.error(f"Error in modules_list endpoint: {e}", exc_info=True)
        # Always return JSON, never let it hang
        return jsonify({
            'modules': [],
            'unique_count': 0,
            'error': str(e),
            'message': 'Failed to load modules. Please try again.'
        })
    
    return jsonify({
        'modules': modules_list,
        'unique_count': unique_count
    })