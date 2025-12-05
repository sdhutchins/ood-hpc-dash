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
CACHE_TTL = 300  # Cache for 5 minutes (300 seconds)
CACHE_FILE = Path('logs/modules_cache.json')
LOCK_FILE = Path('logs/spider_initializing.lock')  # Source of truth for initialization status

def _load_cache_from_file() -> Optional[Dict]:
    """Load cache from file if it exists and is valid.
    
    Returns:
        Dictionary with 'modules', 'unique_count', and 'timestamp' if valid,
        None otherwise.
    """
    try:
        if not CACHE_FILE.exists():
            return None
        
        with CACHE_FILE.open('r', encoding='utf-8') as f:
            cache_data = json.load(f)
        
        cache_time = cache_data.get('timestamp', 0)
        current_time = time.time()
        cache_age = current_time - cache_time
        
        if cache_age < CACHE_TTL:
            return cache_data
        
        logger.info(f"Cache expired (age: {int(cache_age)}s), will refresh")
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
        # Remove lock file when cache is saved
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except OSError as e:
        logger.error(f"Error saving cache to file: {e}", exc_info=True)

def _is_initializing() -> bool:
    """Check if initialization is in progress (lock file is source of truth)."""
    if not LOCK_FILE.exists():
        return False
    
    lock_age = time.time() - LOCK_FILE.stat().st_mtime
    
    # Remove stale lock (>30s with no cache, or >5min regardless)
    if (lock_age > 30 and not CACHE_FILE.exists()) or lock_age > 300:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
        return False
    
    return True

def _set_initializing() -> None:
    """Create lock file to indicate initialization is in progress."""
    try:
        LOCK_FILE.parent.mkdir(exist_ok=True)
        LOCK_FILE.touch()
    except OSError as e:
        logger.error(f"Error creating lock file: {e}", exc_info=True)

def _get_spider_instance():
    """Get or create cached Spider instance.
    
    Returns:
        Spider instance if available, None if initialization is in progress.
    """
    global _cached_spider
    
    if _cached_spider is not None:
        return _cached_spider
    
    if _is_initializing():
        return None
    
    # Create Spider instance
    start_time = time.time()
    try:
        _cached_spider = Spider()
        elapsed = time.time() - start_time
        logger.info(f"Spider instance created in {elapsed:.1f}s")
        return _cached_spider
    except Exception as e:
        # Remove lock file on error
        if LOCK_FILE.exists():
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
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
    if not LMODULE_AVAILABLE:
        logger.error("lmodule package not available")
        return []
    
    if spider is None:
        logger.error("Spider instance must be provided")
        return []
    
    try:
        # Get unique module names (e.g., ['CUDA', 'lmod', 'GCC'])
        unique_names = spider.get_names()
        
        # Get all modules filtered by those names
        # This returns all versions of each module
        modules_dict = spider.get_modules(unique_names)

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
        sorted_modules = sorted(processed_modules, key=lambda x: x['name'])
        return sorted_modules
        
    except (AttributeError, RuntimeError) as e:
        logger.error(f"Error getting modules from Spider: {e}", exc_info=True)
        return []

# Route for the modules page
@modules_bp.route('/')
def modules():
    """Render the modules page."""
    global _cached_modules, _cached_unique_count, _cache_timestamp
    
    logger.info("Modules page route accessed")
    
    if not LMODULE_AVAILABLE:
        return render_template('modules.html', modules=[], unique_count=0)
    
    file_cache = _load_cache_from_file()
    if file_cache:
        _cached_modules = file_cache.get('modules', [])
        _cached_unique_count = file_cache.get('unique_count', 0)
        _cache_timestamp = file_cache.get('timestamp', 0)
        return render_template('modules.html', modules=_cached_modules, unique_count=_cached_unique_count)
    
    return render_template('modules.html', modules=[], unique_count=0)

@modules_bp.route('/list')
def modules_list():
    """Return JSON list of available modules."""
    global _cached_modules, _cached_unique_count, _cache_timestamp
    
    logger.info("Modules list endpoint called")
    
    if not LMODULE_AVAILABLE:
        return jsonify({'modules': [], 'unique_count': 0, 'error': 'lmodule not available'})
    
    # Check cache first
    file_cache = _load_cache_from_file()
    if file_cache:
        _cached_modules = file_cache.get('modules', [])
        _cached_unique_count = file_cache.get('unique_count', 0)
        _cache_timestamp = file_cache.get('timestamp', 0)
        logger.info(f"Returning {len(_cached_modules)} modules from cache")
        return jsonify({
            'modules': _cached_modules,
            'unique_count': _cached_unique_count
        })
    
    # Check if initialization in progress
    if _is_initializing():
        logger.info("Initialization in progress, returning wait message")
        return jsonify({
            'modules': [],
            'unique_count': 0,
            'loading': True,
            'message': 'Module system is being initialized. Please wait and the page will automatically retry.'
        })
    
    # Start initialization
    logger.info("Starting Spider initialization")
    _set_initializing()
    
    try:
        # Create Spider instance directly (we've already checked lock and created it)
        spider = Spider()
        _cached_spider = spider
        
        unique_names = spider.get_names()
        unique_count = len(unique_names)
        modules_list = get_available_modules(spider=spider)
        
        _cached_modules = modules_list
        _cached_unique_count = unique_count
        _cache_timestamp = time.time()
        _save_cache_to_file(modules_list, unique_count)
        
        logger.info(f"Initialization complete: {unique_count} unique, {len(modules_list)} total modules")
        
        return jsonify({
            'modules': modules_list,
            'unique_count': unique_count
        })
            
    except Exception as e:
        logger.error(f"Error initializing modules: {e}", exc_info=True)
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