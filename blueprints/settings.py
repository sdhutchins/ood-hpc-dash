# Standard library imports
import logging
import os

# Third-party imports
from flask import Blueprint, flash, redirect, render_template, request, url_for

# Local imports
from utils import load_settings, save_settings as save_settings_to_file

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
        # Get defaults for fallback
        defaults = _get_default_settings()
        
        # Get form data
        navbar_color = request.form.get('navbar_color', '#ede7f6').strip()
        code_editor_path = request.form.get('code_editor_path', '').strip()
        
        # Get conda envs paths (can be multiple, separated by newlines)
        conda_envs_paths_text = request.form.get('conda_envs_paths', '').strip()
        if conda_envs_paths_text:
            # Split by newlines and also handle carriage returns (Windows line endings)
            conda_envs_paths = [
                path.strip() 
                for path in conda_envs_paths_text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
                if path.strip()
            ]
            # Remove duplicates while preserving order
            seen = set()
            conda_envs_paths = [p for p in conda_envs_paths if not (p in seen or seen.add(p))]
        else:
            conda_envs_paths = defaults.get('conda_envs_paths', [])
        
        # Get project directories (can be multiple, separated by newlines)
        project_directories_text = request.form.get('project_directories', '').strip()
        if project_directories_text:
            # Split by newlines and also handle carriage returns (Windows line endings)
            project_directories = [
                path.strip() 
                for path in project_directories_text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
                if path.strip()
            ]
            # Remove duplicates while preserving order
            seen = set()
            project_directories = [p for p in project_directories if not (p in seen or seen.add(p))]
            logger.info(f"Parsed {len(project_directories)} project directories: {project_directories}")
        else:
            project_directories = defaults.get('project_directories', [])
        
        # Validate navbar color against allowed list
        allowed_values = {c[0] for c in ALLOWED_NAV_COLORS}
        if navbar_color not in allowed_values:
            navbar_color = ALLOWED_NAV_COLORS[0][0]
        
        # Build settings dictionary
        new_settings = {
            'navbar_color': navbar_color,
            'code_editor_path': code_editor_path,
            'conda_envs_paths': conda_envs_paths,
            'project_directories': project_directories
        }
        
        # Save settings
        if save_settings_to_file(new_settings):
            flash('Settings saved successfully! Note: Some changes may require app restart.', 'success')
        else:
            flash('Error saving settings. Please try again.', 'error')
        
    except Exception as e:
        logger.error(f"Error processing settings save: {e}", exc_info=True)
        flash(f'Error saving settings: {str(e)}', 'error')
    
    return redirect(url_for('settings.settings'))
