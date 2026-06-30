import logging
import os

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from utils import (
    load_settings,
    save_settings as save_settings_to_file,
    validate_code_editor_path,
)

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')
logger = logging.getLogger(__name__)

ALLOWED_NAV_COLORS = [
    ("#e8f5e9", "Mint"),
    ("#e3f2fd", "Light Blue"),
    ("#ffeef3", "Rose Tint"),
    ("#f1f3f5", "Light Gray"),
    ("#ede7f6", "Lavender"),
]


def _parse_path_textarea(text: str) -> list[str]:
    """Parse newline-separated paths while preserving first occurrence order."""
    paths = [
        path.strip()
        for path in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        if path.strip()
    ]

    seen = set()
    return [
        path for path in paths
        if not (path in seen or seen.add(path))
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
        validated_editor_path, editor_path_error = validate_code_editor_path(
            code_editor_path
        )
        if editor_path_error is not None or validated_editor_path is None:
            flash(editor_path_error or 'Invalid code editor path.', 'error')
            return redirect(url_for('settings.settings'))
        
        # Get conda envs paths (can be multiple, separated by newlines)
        conda_envs_paths_text = request.form.get('conda_envs_paths')
        if conda_envs_paths_text is not None:
            conda_envs_paths = _parse_path_textarea(conda_envs_paths_text)
        else:
            conda_envs_paths = defaults.get('conda_envs_paths', [])
        
        # Get project directories (can be multiple, separated by newlines)
        project_directories_text = request.form.get('project_directories')
        if project_directories_text is not None:
            project_directories = _parse_path_textarea(project_directories_text)
            # Validate paths are not empty and are strings
            project_directories = [
                p for p in project_directories
                if p and isinstance(p, str)
            ]
            logger.info(
                f"Parsed {len(project_directories)} project directories: "
                f"{project_directories}"
            )
        else:
            project_directories = defaults.get('project_directories', [])
        
        # Ensure it's a list
        if not isinstance(project_directories, list):
            logger.warning(
                "project_directories is not a list: "
                f"{type(project_directories)}, converting"
            )
            project_directories = [project_directories] if project_directories else []
        
        # Validate navbar color against allowed list
        allowed_values = {c[0] for c in ALLOWED_NAV_COLORS}
        if navbar_color not in allowed_values:
            navbar_color = ALLOWED_NAV_COLORS[0][0]
        
        # Build settings dictionary
        new_settings = {
            'navbar_color': navbar_color,
            'code_editor_path': str(validated_editor_path),
            'conda_envs_paths': conda_envs_paths,
            'project_directories': project_directories
        }
        
        # Save settings
        if save_settings_to_file(new_settings):
            current_app.config['FLASKCODE_RESOURCE_BASEPATH'] = str(
                validated_editor_path
            )
            flash('Settings saved successfully!', 'success')
        else:
            flash('Error saving settings. Please try again.', 'error')
        
    except (AttributeError, TypeError, ValueError) as e:
        logger.error(f"Error processing settings save: {e}", exc_info=True)
        flash(f'Error saving settings: {str(e)}', 'error')
    
    return redirect(url_for('settings.settings'))
