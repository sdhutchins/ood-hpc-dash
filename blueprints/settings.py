# Standard library imports
import logging
import os

# Third-party imports
from flask import Blueprint, flash, redirect, render_template, request, url_for

# Local imports
from utils import load_settings, save_settings

# Blueprint for the settings page
settings_bp = Blueprint('settings', __name__, url_prefix='/settings')

# Logger for the settings blueprint
logger = logging.getLogger(__name__)

# Whitelisted light navbar colors (value, label)
ALLOWED_NAV_COLORS = [
    ("#e8f5e9", "Mint"),
    ("#e3f2fd", "Light Blue"),
    ("#ffeef3", "Rose Tint"),
    ("#f1f3f5", "Light Gray"),
    ("#ede7f6", "Lavender"),
]


def _get_default_settings():
    """Get default settings dictionary.
    
    Returns:
        dict: Default settings
    """
    return load_settings()


@settings_bp.route('/')
def settings():
    """Render the settings page with current settings."""
    return render_template(
        'settings.html',
        settings=load_settings(),
        allowed_nav_colors=ALLOWED_NAV_COLORS,
        username=os.environ.get("USER")
    )


@settings_bp.route('/save', methods=['POST'])
def save_settings():
    """Save settings from form submission."""
    try:
        # Get form data
        navbar_color = request.form.get('navbar_color', '#ede7f6').strip()
        code_editor_path = request.form.get('code_editor_path', '').strip()
        
        # Get conda envs paths (can be multiple, separated by newlines)
        conda_envs_paths_text = request.form.get('conda_envs_paths', '').strip()
        conda_envs_paths = [
            path.strip() 
            for path in conda_envs_paths_text.split('\n') 
            if path.strip()
        ]
        
        # Get project directories (can be multiple, separated by newlines)
        project_directories_text = request.form.get('project_directories', '').strip()
        project_directories = [
            path.strip() 
            for path in project_directories_text.split('\n') 
            if path.strip()
        ]
        
        # Validate navbar color against allowed list
        allowed_values = {c[0] for c in ALLOWED_NAV_COLORS}
        if navbar_color not in allowed_values:
            navbar_color = ALLOWED_NAV_COLORS[0][0]
        
        # Get defaults for fallback
        defaults = _get_default_settings()
        
        # Build settings dictionary, using defaults if empty
        new_settings = {
            'navbar_color': navbar_color,
            'code_editor_path': code_editor_path,
            'conda_envs_paths': conda_envs_paths or defaults['conda_envs_paths'],
            'project_directories': project_directories or defaults['project_directories']
        }
        
        # Save settings
        if save_settings(new_settings):
            flash('Settings saved successfully! Note: Some changes may require app restart.', 'success')
        else:
            flash('Error saving settings. Please try again.', 'error')
        
    except Exception as e:
        logger.error(f"Error processing settings save: {e}", exc_info=True)
        flash(f'Error saving settings: {str(e)}', 'error')
    
    return redirect(url_for('settings.settings'))
