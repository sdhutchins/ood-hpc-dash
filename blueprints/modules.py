from flask import Blueprint, render_template, jsonify
from pathlib import Path

import logging
import time
from typing import List, Dict

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

def _get_spider_instance():
    """Get or create cached Spider instance."""
    global _cached_spider
    
    if _cached_spider is None:
        logger.info("Creating new Spider instance (will be cached)")
        logger.warning("Spider initialization may take 30-60 seconds on large HPC systems...")
        start_time = time.time()
        try:
            _cached_spider = Spider()
            elapsed = time.time() - start_time
            logger.info(f"Spider instance created and cached in {elapsed:.2f} seconds")
        except Exception as e:
            logger.error(f"Failed to create Spider instance: {e}", exc_info=True)
            raise
    else:
        logger.info("Reusing cached Spider instance")
    
    return _cached_spider

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
    
    # If we have cached data, use it for initial render
    if _cached_modules is not None and cache_age < CACHE_TTL:
        logger.info(f"Using cached modules data for initial render (age: {cache_age:.1f}s)")
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
    
    # Check cache
    current_time = time.time()
    cache_age = current_time - _cache_timestamp
    
    if _cached_modules is not None and cache_age < CACHE_TTL:
        logger.info(f"Returning cached data from /list endpoint (age: {cache_age:.1f}s)")
        return jsonify({
            'modules': _cached_modules,
            'unique_count': _cached_unique_count
        })
    
    # If we have stale cache, return it immediately while refreshing in background
    if _cached_modules is not None:
        logger.info("Returning stale cache immediately, will refresh in background")
        # Return stale data immediately to prevent timeout
        return jsonify({
            'modules': _cached_modules,
            'unique_count': _cached_unique_count,
            'stale': True,
            'message': 'Refreshing data, please refresh page in a moment'
        })
    
    # No cache at all - try to create Spider
    # This may take 30-60 seconds, but we'll let it complete
    logger.info("No cache available, creating Spider (this may take 30-60 seconds)")
    unique_count = 0
    modules_list = []
    
    try:
        # Get cached or create new Spider instance (this may take 30-60 seconds)
        spider = _get_spider_instance()
        
        logger.info("Getting unique module names")
        unique_names = spider.get_names()
        logger.info(f"get_names() returned {len(unique_names)} names")
        unique_count = len(unique_names)
        
        # Get modules using cached spider
        logger.info("Calling get_available_modules() with cached spider")
        modules_list = get_available_modules(spider=spider)
        logger.info(f"Retrieved {len(modules_list)} modules")
        
        # Update cache
        _cached_modules = modules_list
        _cached_unique_count = unique_count
        _cache_timestamp = current_time
        logger.info(f"Cache updated with {len(modules_list)} modules")
            
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