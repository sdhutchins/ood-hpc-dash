# Standard library imports
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

# Third-party imports
from flask import Blueprint, jsonify, render_template

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
LOCK_FILE = Path('logs/spider_initializing.lock')  # Lock file to track initialization across restarts

def _load_cache_from_file() -> Optional[Dict]:
    """Load cache from file if it exists and is valid.
    
    Returns:
        Dictionary with 'modules', 'unique_count', and 'timestamp' if valid,
        None otherwise.
    """
    try:
        if not CACHE_FILE.exists():
            logger.info("No cache file found")
            return None
        
        with CACHE_FILE.open('r', encoding='utf-8') as f:
            cache_data = json.load(f)
        
        cache_time = cache_data.get('timestamp', 0)
        current_time = time.time()
        cache_age = current_time - cache_time
        
        if cache_age < CACHE_TTL:
            logger.info(f"Loaded cache from file (age: {cache_age:.1f}s)")
            return cache_data
        
        logger.info(f"File cache expired (age: {cache_age:.1f}s)")
        return None
        
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Error loading cache from file: {e}", exc_info=True)
        return None

def _save_cache_to_file(modules: List[Dict[str, str]], unique_count: int) -> None:
    """Save cache to file so it survives app restarts.
    
    Args:
        modules: List of module dictionaries.
        unique_count: Number of unique module packages.
    """
    try:
        CACHE_FILE.parent.mkdir(exist_ok=True)
        cache_data = {
            'modules': modules,
            'unique_count': unique_count,
            'timestamp': time.time()
        }
        with CACHE_FILE.open('w', encoding='utf-8') as f:
            json.dump(cache_data, f, indent=2)
        logger.info(f"Saved cache to file: {CACHE_FILE}")
        # Remove lock file when cache is saved
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
            logger.info("Removed initialization lock file")
    except OSError as e:
        logger.error(f"Error saving cache to file: {e}", exc_info=True)

def _is_initializing() -> bool:
    """Check if Spider is currently being initialized (via lock file)."""
    if LOCK_FILE.exists():
        # Check if lock file is stale (older than 5 minutes)
        lock_age = time.time() - LOCK_FILE.stat().st_mtime
        if lock_age > 300:  # 5 minutes
            logger.warning(f"Stale lock file found (age: {lock_age:.1f}s), removing it")
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
            return False
        return True
    return False

def _set_initializing() -> None:
    """Create lock file to indicate initialization is in progress."""
    try:
        LOCK_FILE.parent.mkdir(exist_ok=True)
        LOCK_FILE.touch()
        logger.info("Created initialization lock file")
    except OSError as e:
        logger.error(f"Error creating lock file: {e}", exc_info=True)

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

def get_available_modules(spider: Optional[Spider] = None) -> List[Dict[str, str]]:
    """Get list of available modules using lmodule Spider.
    
    First gets unique module names, then retrieves all modules for those names.
    Returns list of dictionaries with name, version, and location.
    
    Args:
        spider: Spider instance to use. Must be provided (not created here).
    
    Returns:
        List of dictionaries with 'name', 'version', and 'location' keys.
    """
    logger.info("get_available_modules() called")
    
    if not LMODULE_AVAILABLE:
        logger.error("lmodule package not available")
        return []
    
    if spider is None:
        logger.error("Spider instance must be provided to get_available_modules")
        return []
    
    try:
        
        # Get unique module names (e.g., ['CUDA', 'lmod', 'GCC'])
        logger.info("Calling spider.get_names()")
        unique_names = spider.get_names()
        logger.info(f"Got {len(unique_names)} unique names")
        
        # Get all modules filtered by those names
        # This returns all versions of each module
        logger.info("Calling spider.get_modules() with unique names")
        modules_dict = spider.get_modules(unique_names)
        logger.info(f"Got {len(modules_dict)} modules from spider")

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
        
    except (AttributeError, RuntimeError) as e:
        logger.error(f"Error getting modules from Spider: {e}", exc_info=True)
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
    
    # Check file cache first (simplest approach)
    file_cache = _load_cache_from_file()
    if file_cache:
        _cached_modules = file_cache.get('modules', [])
        _cached_unique_count = file_cache.get('unique_count', 0)
        _cache_timestamp = file_cache.get('timestamp', 0)
        logger.info("Using file cached modules data for initial render")
        return render_template('modules.html', modules=_cached_modules, unique_count=_cached_unique_count)
    
    # No cache - return empty page, JavaScript will fetch
    logger.info("No cache available, returning empty page (data will load via JavaScript)")
    return render_template('modules.html', modules=[], unique_count=0)

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of available modules."""
    global _cached_modules, _cached_unique_count, _cache_timestamp
    
    logger.info("Modules list endpoint accessed")
    
    if not LMODULE_AVAILABLE:
        return jsonify({'modules': [], 'unique_count': 0, 'error': 'lmodule not available'})
    
    # STEP 1: Check file cache FIRST (survives app restarts)
    file_cache = _load_cache_from_file()
    if file_cache:
        # Load from file and update in-memory cache
        _cached_modules = file_cache.get('modules', [])
        _cached_unique_count = file_cache.get('unique_count', 0)
        _cache_timestamp = file_cache.get('timestamp', 0)
        logger.info("Returning data from file cache")
        return jsonify({
            'modules': _cached_modules,
            'unique_count': _cached_unique_count
        })
    
    # STEP 2: Check if initialization is in progress (via lock file)
    if _is_initializing():
        logger.info("Initialization in progress (lock file exists), returning wait message")
        return jsonify({
            'modules': [],
            'unique_count': 0,
            'loading': True,
            'message': 'Module system is being initialized. Please wait and the page will automatically retry.'
        })
    
    # STEP 3: No cache and not initializing - start initialization
    logger.info("No cache found, starting Spider initialization")
    _set_initializing()  # Create lock file
    
    try:
        spider = _get_spider_instance()
        if spider is None:
            # Another request started initialization
            return jsonify({
                'modules': [],
                'unique_count': 0,
                'loading': True,
                'message': 'Module system is being initialized. Please wait and the page will automatically retry.'
            })
        
        logger.info("Getting unique module names")
        unique_names = spider.get_names()
        unique_count = len(unique_names)
        logger.info(f"Found {unique_count} unique module names")
        
        logger.info("Getting all modules")
        modules_list = get_available_modules(spider=spider)
        logger.info(f"Retrieved {len(modules_list)} modules")
        
        # Save cache (this also removes lock file)
        _cached_modules = modules_list
        _cached_unique_count = unique_count
        _cache_timestamp = time.time()
        _save_cache_to_file(modules_list, unique_count)
        logger.info(f"Cache saved with {len(modules_list)} modules")
            
    except Exception as e:
        logger.error(f"Error in modules_list endpoint: {e}", exc_info=True)
        # Remove lock file on error
        if LOCK_FILE.exists():
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
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