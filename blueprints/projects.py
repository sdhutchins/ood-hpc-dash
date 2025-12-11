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

# Local imports
from utils import expand_path, find_binary, load_settings

projects_bp = Blueprint('projects', __name__, url_prefix='/projects')
logger = logging.getLogger(__name__.capitalize())

PROJECT_DIRS_CONFIG_KEY = 'project_directories'
PROJECTS_CACHE_FILE = Path('logs/projects_cache.json')
CACHE_VALIDITY_SECONDS = 3600  # 1 hour


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
    logger.info(f"Starting git-status-checker scan for directories: {base_dirs}")
    
    git_checker = _find_git_status_checker()
    if not git_checker:
        logger.warning("git-status-checker not found in PATH")
        return None, "git-status-checker not found. Install with: pip install git+https://github.com/sdhutchins/git-status-checker.git"
    
    logger.info(f"Using git-status-checker at: {git_checker}")
    
    # Expand user paths and environment variables
    expanded_dirs = []
    for dir_path in base_dirs:
        expanded = expand_path(dir_path)
        expanded_path = Path(expanded)
        logger.info(f"Checking directory: {dir_path} -> {expanded_path} (exists: {expanded_path.exists()})")
        if expanded_path.exists():
            expanded_dirs.append(str(expanded_path))
        else:
            logger.warning(f"Directory does not exist: {expanded_path}")
    
    if not expanded_dirs:
        logger.error("No valid project directories found after expansion")
        return None, "No valid project directories found in configuration"
    
    logger.info(f"Scanning {len(expanded_dirs)} directories: {expanded_dirs}")
    
    # Build command with --json flag
    cmd = [git_checker, '--json', '--recursive', '--check-fetch']
    if ignore_untracked:
        cmd.append('--ignore-untracked')
    
    # Add base directories
    cmd.extend(expanded_dirs)
    
    logger.debug(f"Running command: {' '.join(cmd)}")
    
    try:
        logger.info(f"Running git-status-checker with timeout=600s for {len(expanded_dirs)} directories")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes timeout for scanning multiple large directories
            env=os.environ.copy(),
            cwd=Path.cwd(),
            check=False,  # Don't raise on non-zero exit
        )
        
        logger.info(f"git-status-checker exit code: {result.returncode}")
        if result.stdout:
            logger.debug(f"git-status-checker stdout length: {len(result.stdout)} chars")
            logger.debug(f"git-status-checker stdout preview: {result.stdout[:500]}")
        if result.stderr:
            logger.warning(f"git-status-checker stderr: {result.stderr[:1000]}")
        
        # git-status-checker returns:
        # - exit code 0: all repos up-to-date
        # - exit code 1: some repos outdated (but repos were found)
        # - exit code 127: command not found or no git repos found
        # We treat exit code 127 as "no repos found" but don't treat it as a fatal error
        # since we can fall back to manual scanning
        if result.returncode == 127:
            logger.warning("git-status-checker returned exit code 127 (no git repos found or command issue)")
            # Return empty result instead of error - let fallback handle it
            return {"repositories": [], "total": 0, "outdated": 0}, None
        
        # Parse JSON output
        output = result.stdout.strip()
        if not output:
            logger.info("git-status-checker returned empty output")
            # Empty output might mean no repos found or all up-to-date
            return {"repositories": [], "total": 0, "outdated": 0}, None
        
        try:
            data = json.loads(output)
            repo_count = len(data.get('repositories', []))
            logger.info(f"Successfully parsed JSON: found {repo_count} repositories")
            return data, None
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON output: {e}")
            logger.error(f"Output was: {output[:1000]}")
            error_msg = result.stderr if result.stderr else f"Failed to parse JSON: {e}"
            return None, f"git-status-checker JSON parse error: {error_msg}"
        
    except subprocess.TimeoutExpired:
        logger.error("git-status-checker timed out after 600 seconds")
        return None, "git-status-checker timed out after 10 minutes. Using fallback mode to scan directories individually."
    except Exception as e:
        logger.error(f"Exception calling git-status-checker: {e}", exc_info=True)
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
    logger.info(f"Scanning for git repositories in: {base_dirs}")
    
    for base_dir in base_dirs:
        try:
            expanded = expand_path(base_dir)
            base_path = Path(expanded)
            logger.info(f"Checking base directory: {base_dir} -> {base_path} (exists: {base_path.exists()})")
            
            if not base_path.exists():
                logger.warning(f"Base directory does not exist: {base_path}")
                continue
            
            # Check if the base directory itself is a git repo
            if (base_path / '.git').exists():
                logger.info(f"Found git repo at base directory: {base_path}")
                repos.append(base_path)
                continue
            
            # Walk directory tree looking for .git directories
            repo_count_before = len(repos)
            try:
                for root, dirs, files in os.walk(base_path):
                    # Skip hidden directories (except .git)
                    dirs[:] = [d for d in dirs if not d.startswith('.') or d == '.git']
                    
                    if '.git' in dirs:
                        repo_path = Path(root)
                        logger.debug(f"Found git repository: {repo_path}")
                        repos.append(repo_path)
                        # Don't recurse into subdirectories of a git repo
                        dirs.remove('.git')
                        dirs.clear()  # Stop recursion
                
                logger.info(f"Found {len(repos) - repo_count_before} repositories in {base_path}")
            except (PermissionError, OSError) as e:
                logger.warning(f"Error scanning directory {base_path}: {e}")
                continue
        except Exception as e:
            logger.error(f"Error processing directory {base_dir}: {e}", exc_info=True)
            continue
    
    logger.info(f"Total repositories found: {len(repos)}")
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

def _process_repo(repo_path: Path, repo_status: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Process a single repository and return project data.
    
    Args:
        repo_path: Path to git repository
        repo_status: Optional status dict from git-status-checker
    
    Returns:
        Project data dict or None if processing fails
    """
    if not repo_path.exists():
        logger.warning(f"Repository path does not exist: {repo_path}")
        return None
    
    git_info = (_get_git_info_from_status_checker(repo_status, repo_path) 
                if repo_status else _get_git_info_fallback(repo_path))
    
    if not git_info:
        return None
    
    return {
        'name': git_info['name'],
        'path': git_info['path'],
        'git': git_info,
        'reproducibility': _check_reproducibility_health(repo_path),
        'drift_footprint': _check_drift_and_footprint(repo_path),
    }


def _load_projects_cache() -> Optional[Dict[str, Any]]:
    """Load projects cache from file.
    
    Returns:
        Cache dict with 'timestamp', 'directories', and 'projects', or None if invalid
    """
    if not PROJECTS_CACHE_FILE.exists():
        logger.debug("Projects cache file does not exist")
        return None
    
    try:
        with PROJECTS_CACHE_FILE.open('r', encoding='utf-8') as f:
            cache = json.load(f)
        
        # Validate cache structure
        if not isinstance(cache, dict):
            logger.warning("Projects cache is not a dictionary")
            return None
        
        if 'timestamp' not in cache or 'projects' not in cache or 'directories' not in cache:
            logger.warning("Projects cache missing required fields")
            return None
        
        # Check if cache is still valid (less than 1 hour old)
        cache_age = time.time() - cache.get('timestamp', 0)
        if cache_age < 0:
            logger.warning(f"Projects cache has invalid timestamp (future time)")
            return None
        
        if cache_age > CACHE_VALIDITY_SECONDS:
            logger.info(f"Projects cache expired (age: {cache_age:.0f}s)")
            return None
        
        # Validate projects data
        projects = cache.get('projects', [])
        if not isinstance(projects, list):
            logger.warning("Projects cache 'projects' field is not a list")
            return None
        
        logger.info(f"Loaded projects cache (age: {cache_age:.0f}s, {len(projects)} projects)")
        return cache
    except json.JSONDecodeError as e:
        logger.warning(f"Error parsing projects cache JSON: {e}")
        return None
    except Exception as e:
        logger.warning(f"Error loading projects cache: {e}", exc_info=True)
        return None


def _save_projects_cache(projects_data: List[Dict[str, Any]], directories: List[str]) -> None:
    """Save projects cache to file.
    
    Args:
        projects_data: List of project data dictionaries
        directories: List of directories that were scanned
    """
    try:
        PROJECTS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cache = {
            'timestamp': time.time(),
            'directories': directories,
            'projects': projects_data,
        }
        with PROJECTS_CACHE_FILE.open('w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved projects cache: {len(projects_data)} projects from {len(directories)} directories")
    except Exception as e:
        logger.warning(f"Error saving projects cache: {e}")


def _collect_projects_data(project_dirs: List[str], use_cache: bool = True) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Collect project data from cache or by scanning directories.
    Uses cache if valid (< 1 hour old) and directories match.
    When new directories are added, only scans new ones and merges with cache.
    
    Args:
        project_dirs: List of project directories to scan
        use_cache: Whether to use cache (default True)
    
    Returns:
        Tuple of (projects_data_list, error_message)
    """
    # Normalize directory paths for comparison
    normalized_dirs = sorted([expand_path(d) for d in project_dirs])
    
    # Try to load from cache
    cached_data = None
    if use_cache:
        cached_data = _load_projects_cache()
        if cached_data:
            cached_dirs = sorted([expand_path(d) for d in cached_data.get('directories', [])])
            # Check if all cached directories are still in the current list
            if set(cached_dirs).issubset(set(normalized_dirs)):
                # Check if there are new directories to scan
                new_dirs = [d for d in normalized_dirs if d not in cached_dirs]
                if not new_dirs:
                    # All directories are cached and no new ones - return cache
                    cached_projects = cached_data.get('projects', [])
                    logger.info(f"Using cached projects data: {len(cached_projects)} projects")
                    # Ensure cached projects are valid (have required keys)
                    valid_projects = [p for p in cached_projects if isinstance(p, dict) and 'path' in p]
                    if len(valid_projects) != len(cached_projects):
                        logger.warning(f"Filtered {len(cached_projects) - len(valid_projects)} invalid projects from cache")
                    return valid_projects, None
                else:
                    # Some new directories - scan only those and merge
                    logger.info(f"Cache found for {len(cached_dirs)} directories, scanning {len(new_dirs)} new directories")
                    cached_projects = cached_data.get('projects', [])
                    # Ensure cached projects are valid
                    cached_projects = [p for p in cached_projects if isinstance(p, dict) and 'path' in p]
                    # Scan new directories
                    try:
                        new_projects_data, new_error = _scan_directories(new_dirs)
                        # Merge: combine cached and new, remove duplicates by path
                        all_projects = {p['path']: p for p in cached_projects}
                        all_projects.update({p['path']: p for p in new_projects_data})
                        merged_projects = list(all_projects.values())
                        merged_projects.sort(key=lambda x: x['name'].lower())
                        # Save updated cache
                        _save_projects_cache(merged_projects, normalized_dirs)
                        logger.info(f"Merged cache with new scan: {len(merged_projects)} total projects")
                        return merged_projects, new_error
                    except Exception as e:
                        logger.error(f"Error scanning new directories: {e}", exc_info=True)
                        # Return cached data if new scan fails
                        return cached_projects, f"Error scanning new directories: {str(e)}"
    
    # No valid cache or cache disabled - scan all directories
    logger.info(f"Scanning {len(normalized_dirs)} directories (cache miss or disabled)")
    try:
        projects_data, error = _scan_directories(normalized_dirs)
        if projects_data:
            try:
                _save_projects_cache(projects_data, normalized_dirs)
            except Exception as cache_err:
                logger.warning(f"Failed to save cache: {cache_err}")
        return projects_data, error
    except Exception as e:
        logger.error(f"Error in _collect_projects_data: {e}", exc_info=True)
        return [], f"Error collecting projects data: {str(e)}"


def _scan_directories(project_dirs: List[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Scan directories for git repositories and collect project data.
    
    Args:
        project_dirs: List of project directories to scan
    
    Returns:
        Tuple of (projects_data_list, error_message)
    """
    projects_data = []
    errors = []
    
    # Try git-status-checker first
    git_status_data, git_error = _call_git_status_checker(project_dirs)
    repositories = git_status_data.get('repositories', []) if git_status_data else []
    
    # Use git-status-checker results if available
    if git_status_data and repositories:
        logger.info(f"Using git-status-checker results: {len(repositories)} repositories")
        for repo_status in repositories:
            try:
                repo_path_str = repo_status.get('path', '')
                repo_path = Path(repo_path_str)
                if repo_path.exists():
                    project = _process_repo(repo_path, repo_status)
                    if project:
                        projects_data.append(project)
                else:
                    logger.warning(f"Repository path does not exist: {repo_path}")
            except Exception as e:
                logger.warning(f"Error processing repository {repo_status.get('path', 'unknown')}: {e}")
                errors.append(f"Error processing {repo_status.get('path', 'unknown')}: {str(e)}")
    else:
        # Fallback to manual scanning
        if git_error and git_error != "No git repositories found in configured directories":
            logger.warning(f"git-status-checker error: {git_error}, using fallback")
            errors.append(git_error)
        
        repos = _find_git_repos(project_dirs)
        if repos:
            logger.info(f"Found {len(repos)} repositories via manual scan")
            for repo_path in repos:
                try:
                    project = _process_repo(repo_path)
                    if project:
                        projects_data.append(project)
                except Exception as e:
                    logger.warning(f"Error processing repository {repo_path}: {e}")
                    errors.append(f"Error processing {repo_path}: {str(e)}")
        else:
            if not projects_data:
                return [], f"No git repositories found. Checked directories: {', '.join(project_dirs)}"
    
    projects_data.sort(key=lambda x: x['name'].lower())
    logger.info(f"Collected {len(projects_data)} projects from {len(project_dirs)} directories")
    
    # Return error only if no projects were found and there were errors
    error = None
    if not projects_data and errors:
        error = "; ".join(errors[:3])  # Limit error message length
    elif errors and len(errors) > len(projects_data):
        error = f"Some directories had errors: {len(errors)} errors, {len(projects_data)} projects found"
    
    return projects_data, error


@projects_bp.route('/')
def projects():
    """
    Render the projects page immediately.
    Data is loaded asynchronously via JavaScript to avoid timeouts.
    """
    logger.info("Projects page accessed")
    settings = load_settings()
    project_dirs = settings.get(PROJECT_DIRS_CONFIG_KEY, [])
    logger.info(f"Loaded project directories from settings: {project_dirs}")
    
    if not project_dirs:
        logger.warning("No project directories configured")
        return render_template(
            'projects.html',
            projects=[],
            project_dirs=[],
            total_repos=0,
            git_status_error="No project directories configured. Please configure them in Settings.",
            loading=False,
        )
    
    # Return page immediately - data will be loaded via JavaScript
    return render_template(
        'projects.html',
        projects=[],
        project_dirs=project_dirs,
        total_repos=0,
        git_status_error=None,
        loading=True,
    )


@projects_bp.route('/status')
def projects_status():
    """
    Return JSON with project status information.
    This endpoint is called asynchronously by the frontend.
    
    Returns:
        JSON response with project monitoring data
    """
    try:
        settings = load_settings()
        project_dirs = settings.get(PROJECT_DIRS_CONFIG_KEY, [])
        
        # Ensure project_dirs is a list
        if not isinstance(project_dirs, list):
            logger.warning(f"project_dirs is not a list: {type(project_dirs)}, converting")
            project_dirs = [project_dirs] if project_dirs else []
        
        if not project_dirs:
            return jsonify({
                'projects': [],
                'total': 0,
                'error': 'No project directories configured',
            }), 200
        
        logger.info(f"Processing {len(project_dirs)} project directories: {project_dirs}")
        # Check if refresh was requested (force cache refresh)
        force_refresh = request.args.get('refresh', '').lower() == 'true'
        
        try:
            projects_data, error = _collect_projects_data(project_dirs, use_cache=not force_refresh)
        except Exception as collect_err:
            logger.error(f"Error in _collect_projects_data: {collect_err}", exc_info=True)
            return jsonify({
                'projects': [],
                'total': 0,
                'error': f'Error collecting projects: {str(collect_err)}',
            }), 500
        
        # Ensure all data is JSON-serializable
        try:
            # Use CustomJsonEncoder for Path and datetime objects
            from utils import CustomJsonEncoder
            response_data = {
                'projects': projects_data,
                'total': len(projects_data),
                'error': error,
            }
            # Test serialization
            json.dumps(response_data, cls=CustomJsonEncoder)
        except (TypeError, ValueError) as json_err:
            logger.error(f"JSON serialization error: {json_err}", exc_info=True)
            return jsonify({
                'projects': [],
                'total': 0,
                'error': f'Data serialization error: {str(json_err)}',
            }), 500
        
        return jsonify(response_data), 200
    except Exception as e:
        logger.error(f"Error in projects_status endpoint: {e}", exc_info=True)
        return jsonify({
            'projects': [],
            'total': 0,
            'error': f'Error loading projects: {str(e)}',
        }), 500
