# Standard library imports
import json
import logging
import os
from pathlib import Path

# Third-party imports
from flask import Blueprint, flash, redirect, render_template, request, url_for

# Blueprint for the settings page
settings_bp = Blueprint('settings', __name__, url_prefix='/settings')

# Logger for the settings blueprint
logger = logging.getLogger(__name__)

# Path to settings file
SETTINGS_FILE = Path('config/settings.json')

# Whitelisted light navbar colors (value, label)
ALLOWED_NAV_COLORS = [
    ("#e3f2fd", "Light Blue"),
    ("#f8f9fa", "Off White"),
    ("#f1f3f5", "Light Gray"),
    ("#e8f5e9", "Mint"),
    ("#fff3e0", "Soft Orange"),
    ("#fdf0f5", "Blush"),
    ("#e0f7fa", "Cyan Tint"),
    ("#fef8e7", "Cream"),
]


def _load_settings():
    """Load settings from JSON file.
    
    Returns:
        dict: Settings dictionary with default values if file doesn't exist
    """
    try:
        if not SETTINGS_FILE.exists():
            # Return defaults if file doesn't exist
            return {
                'navbar_color': '#e3f2fd',
                'code_editor_path': str(Path.cwd()),
                'conda_envs_paths': [
                    '$HOME/.conda/envs'
                ]
            }
        
        with SETTINGS_FILE.open('r', encoding='utf-8') as f:
            settings = json.load(f)
        
        return settings
    except Exception as e:
        logger.error(f"Error loading settings: {e}", exc_info=True)
        # Return defaults on error
        return {
            'navbar_color': '#e3f2fd',
            'code_editor_path': str(Path.cwd()),
            'conda_envs_paths': [
                '$HOME/.conda/envs',
                '$HOME/miniconda3/envs'
            ]
        }


def _save_settings(settings):
    """Save settings to JSON file.
    
    Args:
        settings: Dictionary of settings to save
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Ensure config directory exists
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        with SETTINGS_FILE.open('w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)
        
        logger.info("Settings saved successfully")
        return True
    except Exception as e:
        logger.error(f"Error saving settings: {e}", exc_info=True)
        return False


def _expand_path(path):
    """Expand environment variables in path (e.g., $HOME).
    
    Args:
        path: Path string that may contain environment variables
        
    Returns:
        str: Expanded path
    """
    return os.path.expanduser(os.path.expandvars(path))


@settings_bp.route('/')
def settings():
    """Render the settings page with current settings."""
    current_settings = _load_settings()
    return render_template(
        'settings.html',
        settings=current_settings,
        allowed_nav_colors=ALLOWED_NAV_COLORS,
        username=os.environ.get("USER")
    )


@settings_bp.route('/save', methods=['POST'])
def save_settings():
    """Save settings from form submission."""
    try:
        # Get form data
        navbar_color = request.form.get('navbar_color', '#e3f2fd').strip()
        code_editor_path = request.form.get('code_editor_path', '').strip()
        
        # Get conda envs paths (can be multiple, separated by newlines)
        conda_envs_paths_text = request.form.get('conda_envs_paths', '').strip()
        conda_envs_paths = [
            path.strip() 
            for path in conda_envs_paths_text.split('\n') 
            if path.strip()
        ]
        
        # Validate navbar color against allowed list
        allowed_values = {c[0] for c in ALLOWED_NAV_COLORS}
        if navbar_color not in allowed_values:
            navbar_color = ALLOWED_NAV_COLORS[0][0]
        
        # Build settings dictionary
        new_settings = {
            'navbar_color': navbar_color,
            'code_editor_path': code_editor_path,
            'conda_envs_paths': conda_envs_paths if conda_envs_paths else [
                '$HOME/.conda/'
            ]
        }
        
        # Save settings
        if _save_settings(new_settings):
            flash('Settings saved successfully! Note: Some changes may require app restart.', 'success')
        else:
            flash('Error saving settings. Please try again.', 'error')
        
    except Exception as e:
        logger.error(f"Error processing settings save: {e}", exc_info=True)
        flash(f'Error saving settings: {str(e)}', 'error')
    
    return redirect(url_for('settings.settings'))
