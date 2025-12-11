# Standard library imports
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Third-party imports
from flask import Blueprint, jsonify, render_template, request

projects_bp = Blueprint('projects', __name__, url_prefix='/projects')
logger = logging.getLogger(__name__.capitalize())

CONFIG_FILE = Path('config/settings.json')
PROJECT_DIRS_CONFIG_KEY = 'project_directories'


def _load_settings() -> Dict[str, Any]:
    """Load settings from JSON file with sensible defaults."""
    defaults = {
        PROJECT_DIRS_CONFIG_KEY: [
            "$HOME/Documents/Git-Repos",
            "$HOME/Dev/src-repos",
        ],
    }
    try:
        if CONFIG_FILE.exists():
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {**defaults, **data}
    except Exception:
        return defaults
    return defaults


def _find_git_status_checker() -> Optional[str]:
    """Find git-status-checker binary in PATH or common locations."""
    # Try common locations
    common_paths = [
        'git-status-checker',
        '/usr/local/bin/git-status-checker',
        '/usr/bin/git-status-checker',
        os.path.expanduser('~/.local/bin/git-status-checker'),
    ]
    
    # Check PATH first
    git_checker = shutil.which('git-status-checker')
    if git_checker:
        return git_checker
    
    # Check common paths
    for path in common_paths:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    
    return None


def _call_git_status_checker(
    base_dirs: List[str],
    ignore_untracked: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Call git-status-checker with --json flag and return parsed JSON output.
    
    Args:
        base_dirs: List of base directories to scan
        ignore_untracked: Whether to ignore untracked files
    
    Returns:
        Tuple of (parsed_json_data, error_message)
    """
    git_checker = _find_git_status_checker()
    if not git_checker:
        return None, "git-status-checker not found. Install with: pip install git+https://github.com/sdhutchins/git-status-checker.git"
    
    # Expand user paths and environment variables
    expanded_dirs = []
    for dir_path in base_dirs:
        expanded = os.path.expandvars(os.path.expanduser(dir_path))
        expanded_path = Path(expanded)
        if expanded_path.exists():
            expanded_dirs.append(str(expanded_path))
    
    if not expanded_dirs:
        return None, "No valid project directories found in configuration"
    
    # Build command with --json flag
    cmd = [git_checker, '--json', '--recursive', '--check-fetch']
    if ignore_untracked:
        cmd.append('--ignore-untracked')
    
    # Add base directories
    cmd.extend(expanded_dirs)
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,  # 2 minutes timeout for scanning
            env=os.environ.copy(),
            cwd=Path.cwd(),
            check=False,  # Don't raise on non-zero exit
        )
        
        # git-status-checker returns exit code 1 if repos are outdated, 0 if all up-to-date
        # Exit code 127 means no git repos found
        if result.returncode == 127:
            return None, "No git repositories found in configured directories"
        
        # Parse JSON output
        output = result.stdout.strip()
        if not output:
            # Empty output might mean no repos found or all up-to-date
            return {"repositories": [], "total": 0, "outdated": 0}, None
        
        try:
            data = json.loads(output)
            return data, None
        except json.JSONDecodeError as e:
            error_msg = result.stderr if result.stderr else f"Failed to parse JSON: {e}"
            return None, f"git-status-checker JSON parse error: {error_msg}"
        
    except subprocess.TimeoutExpired:
        return None, "git-status-checker timed out after 120 seconds"
    except Exception as e:
        return None, f"Error calling git-status-checker: {str(e)}"


def _get_git_info_from_status_checker(
    repo_status: Dict[str, Any],
    repo_path: Path,
) -> Dict[str, Any]:
    """
    Convert git-status-checker status to our git_info format.
    
    Args:
        repo_status: Repository status dict from git-status-checker JSON
        repo_path: Path to git repository
    
    Returns:
        Dictionary with git status information
    """
    git_info = {
        'path': repo_status.get('path', str(repo_path)),
        'name': repo_path.name,
        'dirty': len(repo_status.get('local_changes', [])) > 0,
        'ahead': repo_status.get('ahead', False),
        'behind': repo_status.get('behind', False),
        'last_commit': None,
        'last_commit_author': None,
        'last_commit_date': None,
        'branch': None,
        'remote': None,
        'up_to_date': repo_status.get('up_to_date', True),
        'has_remote_changes': repo_status.get('has_remote_changes', False),
        'local_changes': repo_status.get('local_changes', []),
    }
    
    # Get additional info using git commands (branch, last commit, remote)
    try:
        # Get current branch
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            git_info['branch'] = result.stdout.strip()
        
        # Get last commit info
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%H|%an|%ae|%ad|%s', '--date=iso'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split('|', 4)
            if len(parts) >= 4:
                git_info['last_commit'] = parts[0][:8]  # Short hash
                git_info['last_commit_author'] = parts[1]
                git_info['last_commit_date'] = parts[3]
        
        # Get remote URL
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            git_info['remote'] = result.stdout.strip()
        
    except Exception as e:
        logger.warning(f"Error getting additional git info for {repo_path}: {e}")
    
    return git_info


def _get_git_info_fallback(repo_path: Path) -> Optional[Dict[str, Any]]:
    """
    Fallback function to get git info using direct git commands.
    Used when git-status-checker is not available.
    
    Args:
        repo_path: Path to git repository
    
    Returns:
        Dictionary with git status information or None
    """
    git_info = {
        'path': str(repo_path),
        'name': repo_path.name,
        'dirty': False,
        'ahead': False,
        'behind': False,
        'last_commit': None,
        'last_commit_author': None,
        'last_commit_date': None,
        'branch': None,
        'remote': None,
        'up_to_date': True,
        'has_remote_changes': False,
        'local_changes': [],
    }
    
    try:
        # Check if it's a git repo
        git_dir = repo_path / '.git'
        if not git_dir.exists():
            return None
        
        # Get current branch
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            git_info['branch'] = result.stdout.strip()
        
        # Check if dirty (uncommitted changes)
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            changes = result.stdout.strip().split('\n')
            changes = [c for c in changes if c.strip()]
            git_info['dirty'] = len(changes) > 0
            git_info['local_changes'] = changes
        
        # Get last commit info
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%H|%an|%ae|%ad|%s', '--date=iso'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split('|', 4)
            if len(parts) >= 4:
                git_info['last_commit'] = parts[0][:8]  # Short hash
                git_info['last_commit_author'] = parts[1]
                git_info['last_commit_date'] = parts[3]
        
        # Get remote URL
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            git_info['remote'] = result.stdout.strip()
        
        # Check ahead/behind (only if remote exists)
        if git_info['branch'] and git_info.get('remote'):
            try:
                result = subprocess.run(
                    ['git', 'rev-list', '--left-right', '--count', f'origin/{git_info["branch"]}...HEAD'],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    parts = result.stdout.strip().split()
                    if len(parts) == 2:
                        git_info['behind'] = int(parts[0]) > 0
                        git_info['ahead'] = int(parts[1]) > 0
                        git_info['up_to_date'] = int(parts[0]) == 0 and int(parts[1]) == 0 and not git_info['dirty']
            except (ValueError, subprocess.TimeoutExpired):
                # If remote branch doesn't exist or other error, just continue
                pass
        
        return git_info
        
    except Exception as e:
        logger.warning(f"Error getting git info for {repo_path}: {e}")
        return None


def _find_git_repos(base_dirs: List[str]) -> List[Path]:
    """
    Find all git repositories in base directories.
    
    Args:
        base_dirs: List of base directory paths
    
    Returns:
        List of Path objects pointing to git repositories
    """
    repos = []
    for base_dir in base_dirs:
        expanded = os.path.expandvars(os.path.expanduser(base_dir))
        base_path = Path(expanded)
        if not base_path.exists():
            continue
        
        # Walk directory tree looking for .git directories
        for root, dirs, files in os.walk(base_path):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            
            if '.git' in dirs:
                repo_path = Path(root)
                repos.append(repo_path)
                # Don't recurse into subdirectories of a git repo
                dirs.remove('.git')
                dirs.clear()  # Stop recursion
    
    return repos


def _check_reproducibility_health(repo_path: Path) -> Dict[str, Any]:
    """
    Check reproducibility health indicators for a repository.
    
    Args:
        repo_path: Path to repository
    
    Returns:
        Dictionary with reproducibility health information
    """
    health = {
        'environment_files': [],
        'workflow_configs': [],
        'missing_common_files': [],
        'staleness': {},
    }
    
    # Common environment files
    env_files = [
        'requirements.txt',
        'environment.yml',
        'conda-environment.yml',
        'Pipfile',
        'pyproject.toml',
        'setup.py',
        'renv.lock',  # R
        'DESCRIPTION',  # R
        'Cargo.toml',  # Rust
        'package.json',  # Node.js
        'go.mod',  # Go
    ]
    
    # Check for environment files
    for env_file in env_files:
        env_path = repo_path / env_file
        if env_path.exists():
            health['environment_files'].append({
                'name': env_file,
                'path': str(env_path.relative_to(repo_path)),
                'modified': datetime.fromtimestamp(env_path.stat().st_mtime).isoformat(),
                'size': env_path.stat().st_size,
            })
    
    # Check for workflow configs
    workflow_dirs = [
        repo_path / '.github' / 'workflows',
        repo_path / '.gitlab-ci.yml',
        repo_path / '.circleci',
        repo_path / '.travis.yml',
    ]
    
    for workflow_path in workflow_dirs:
        if workflow_path.exists():
            if workflow_path.is_dir():
                # Count workflow files
                workflow_files = list(workflow_path.glob('*.yml')) + list(workflow_path.glob('*.yaml'))
                for wf_file in workflow_files:
                    health['workflow_configs'].append({
                        'name': wf_file.name,
                        'path': str(wf_file.relative_to(repo_path)),
                        'modified': datetime.fromtimestamp(wf_file.stat().st_mtime).isoformat(),
                    })
            else:
                health['workflow_configs'].append({
                    'name': workflow_path.name,
                    'path': str(workflow_path.relative_to(repo_path)),
                    'modified': datetime.fromtimestamp(workflow_path.stat().st_mtime).isoformat(),
                })
    
    # Check for common missing files (best practices)
    common_files = ['README.md', 'LICENSE', '.gitignore']
    for common_file in common_files:
        if not (repo_path / common_file).exists():
            health['missing_common_files'].append(common_file)
    
    # Check staleness: compare file modification times to last commit
    try:
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%ct', '--', '.'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            last_commit_time = int(result.stdout.strip())
            last_commit_dt = datetime.fromtimestamp(last_commit_time)
            
            # Check if any tracked files are newer than last commit
            result = subprocess.run(
                ['git', 'ls-files'],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                tracked_files = result.stdout.strip().split('\n')
                modified_after_commit = []
                for file_path in tracked_files:
                    if not file_path:
                        continue
                    full_path = repo_path / file_path
                    if full_path.exists():
                        file_mtime = full_path.stat().st_mtime
                        if file_mtime > last_commit_time:
                            modified_after_commit.append({
                                'path': file_path,
                                'modified': datetime.fromtimestamp(file_mtime).isoformat(),
                                'last_commit': last_commit_dt.isoformat(),
                            })
                
                health['staleness'] = {
                    'last_commit': last_commit_dt.isoformat(),
                    'files_modified_after_commit': modified_after_commit,
                    'count': len(modified_after_commit),
                }
    except Exception:
        pass
    
    return health


def _check_drift_and_footprint(repo_path: Path) -> Dict[str, Any]:
    """
    Check filesystem drift and footprint for a repository.
    
    Args:
        repo_path: Path to repository
    
    Returns:
        Dictionary with drift and footprint information
    """
    info = {
        'directory_size': 0,
        'git_size': 0,
        'last_modified': None,
        'last_commit': None,
        'drift_days': None,
        'large_untracked_files': [],
    }
    
    try:
        # Get directory size (excluding .git)
        total_size = 0
        for root, dirs, files in os.walk(repo_path):
            # Skip .git directory
            if '.git' in root:
                continue
            dirs[:] = [d for d in dirs if d != '.git']
            
            for file in files:
                file_path = Path(root) / file
                try:
                    total_size += file_path.stat().st_size
                except (OSError, PermissionError):
                    pass
        
        info['directory_size'] = total_size
        
        # Get .git directory size
        git_dir = repo_path / '.git'
        if git_dir.exists():
            git_size = 0
            for root, dirs, files in os.walk(git_dir):
                for file in files:
                    file_path = Path(root) / file
                    try:
                        git_size += file_path.stat().st_size
                    except (OSError, PermissionError):
                        pass
            info['git_size'] = git_size
        
        # Get last modified time of directory (most recent file)
        last_modified = 0
        for root, dirs, files in os.walk(repo_path):
            if '.git' in root:
                continue
            dirs[:] = [d for d in dirs if d != '.git']
            
            for file in files:
                file_path = Path(root) / file
                try:
                    mtime = file_path.stat().st_mtime
                    if mtime > last_modified:
                        last_modified = mtime
                except (OSError, PermissionError):
                    pass
        
        if last_modified > 0:
            info['last_modified'] = datetime.fromtimestamp(last_modified).isoformat()
        
        # Get last commit time
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%ct'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            last_commit_time = int(result.stdout.strip())
            info['last_commit'] = datetime.fromtimestamp(last_commit_time).isoformat()
            
            # Calculate drift
            if last_modified > 0:
                drift_seconds = last_modified - last_commit_time
                info['drift_days'] = round(drift_seconds / 86400, 1)
        
        # Find large untracked files (> 1MB)
        result = subprocess.run(
            ['git', 'status', '--porcelain', '--untracked-files=all'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            for line in lines:
                if line.startswith('??'):  # Untracked file
                    file_path = line[3:].strip()
                    full_path = repo_path / file_path
                    if full_path.exists() and full_path.is_file():
                        try:
                            size = full_path.stat().st_size
                            if size > 1024 * 1024:  # > 1MB
                                info['large_untracked_files'].append({
                                    'path': file_path,
                                    'size': size,
                                    'size_mb': round(size / (1024 * 1024), 2),
                                })
                        except (OSError, PermissionError):
                            pass
        
        # Sort by size descending
        info['large_untracked_files'].sort(key=lambda x: x['size'], reverse=True)
        # Limit to top 10
        info['large_untracked_files'] = info['large_untracked_files'][:10]
        
    except Exception as e:
        logger.warning(f"Error checking drift/footprint for {repo_path}: {e}")
    
    return info




@projects_bp.route('/')
def projects():
    """Render the projects page with monitoring information."""
    settings = _load_settings()
    project_dirs = settings.get(PROJECT_DIRS_CONFIG_KEY, [])
    
    # Get git status from git-status-checker
    git_status_data, error = _call_git_status_checker(project_dirs)
    
    if error:
        logger.warning(f"Error calling git-status-checker: {error}")
        # Fall back to manual scanning if git-status-checker fails
        repos = _find_git_repos(project_dirs)
        projects_data = []
        for repo_path in repos:
            # Use fallback _get_git_info if needed (keeping old implementation)
            git_info = _get_git_info_fallback(repo_path)
            if not git_info:
                continue
            
            repro_health = _check_reproducibility_health(repo_path)
            drift_footprint = _check_drift_and_footprint(repo_path)
            
            projects_data.append({
                'name': git_info['name'],
                'path': git_info['path'],
                'git': git_info,
                'reproducibility': repro_health,
                'drift_footprint': drift_footprint,
            })
    else:
        # Use git-status-checker JSON data
        repositories = git_status_data.get('repositories', [])
        projects_data = []
        
        for repo_status in repositories:
            repo_path = Path(repo_status.get('path', ''))
            if not repo_path.exists():
                continue
            
            git_info = _get_git_info_from_status_checker(repo_status, repo_path)
            repro_health = _check_reproducibility_health(repo_path)
            drift_footprint = _check_drift_and_footprint(repo_path)
            
            projects_data.append({
                'name': git_info['name'],
                'path': git_info['path'],
                'git': git_info,
                'reproducibility': repro_health,
                'drift_footprint': drift_footprint,
            })
    
    # Sort by name
    projects_data.sort(key=lambda x: x['name'].lower())
    
    return render_template(
        'projects.html',
        projects=projects_data,
        project_dirs=project_dirs,
        total_repos=len(projects_data),
        git_status_error=error if error else None,
    )


@projects_bp.route('/status')
def projects_status():
    """
    Return JSON with project status information.
    
    Returns:
        JSON response with project monitoring data
    """
    settings = _load_settings()
    project_dirs = settings.get(PROJECT_DIRS_CONFIG_KEY, [])
    
    # Get git status from git-status-checker
    git_status_data, error = _call_git_status_checker(project_dirs)
    
    if error:
        logger.warning(f"Error calling git-status-checker: {error}")
        # Fall back to manual scanning if git-status-checker fails
        repos = _find_git_repos(project_dirs)
        projects_data = []
        for repo_path in repos:
            git_info = _get_git_info_fallback(repo_path)
            if not git_info:
                continue
            
            repro_health = _check_reproducibility_health(repo_path)
            drift_footprint = _check_drift_and_footprint(repo_path)
            
            projects_data.append({
                'name': git_info['name'],
                'path': git_info['path'],
                'git': git_info,
                'reproducibility': repro_health,
                'drift_footprint': drift_footprint,
            })
    else:
        # Use git-status-checker JSON data
        repositories = git_status_data.get('repositories', [])
        projects_data = []
        
        for repo_status in repositories:
            repo_path = Path(repo_status.get('path', ''))
            if not repo_path.exists():
                continue
            
            git_info = _get_git_info_from_status_checker(repo_status, repo_path)
            repro_health = _check_reproducibility_health(repo_path)
            drift_footprint = _check_drift_and_footprint(repo_path)
            
            projects_data.append({
                'name': git_info['name'],
                'path': git_info['path'],
                'git': git_info,
                'reproducibility': repro_health,
                'drift_footprint': drift_footprint,
            })
    
    return jsonify({
        'projects': projects_data,
        'total': len(projects_data),
    })
