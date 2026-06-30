import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from utils import expand_path, load_settings, validate_project_directory

projects_bp = Blueprint('projects', __name__, url_prefix='/projects')
logger = logging.getLogger(__name__)

PROJECT_DIRS_CONFIG_KEY = 'project_directories'
PROJECTS_CACHE_FILE = Path('logs/projects_cache.json')
CACHE_VALIDITY_SECONDS = 3600  # 1 hour
PROJECTS_CACHE_SCHEMA_VERSION = 2
PROJECT_REPO_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    subprocess.SubprocessError,
    ValueError,
    KeyError,
    TypeError,
)
SSH_REMOTE_PATTERN = re.compile(r'^git@([^:]+):(.+)$')

# git-status-checker timeout scales with number of directories
_CHECKER_TIMEOUT_BASE = 120
_CHECKER_TIMEOUT_PER_DIR = 60


def _find_checker_binary() -> str | None:
    """Locate git-status-checker on PATH or in common pip locations."""
    found = shutil.which('git-status-checker')
    if found:
        return found

    pip_locations = [
        os.path.expanduser('~/.local/bin/git-status-checker'),
        '/usr/local/bin/git-status-checker',
    ]
    for path in pip_locations:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return None


def _call_git_status_checker(
    base_dirs: list[str],
) -> tuple[list[dict[str, Any]], str | None]:
    """Run git-status-checker once for all base directories.

    Returns (repos_list, error). repos_list is the 'repositories' array from
    the JSON output. On failure returns ([], error_message) so the caller can
    fall back to manual scanning.
    """
    checker = _find_checker_binary()
    if not checker:
        return [], "git-status-checker not found on PATH"

    expanded_dirs = []
    for dir_path in base_dirs:
        expanded = expand_path(dir_path)
        if Path(expanded).is_dir():
            expanded_dirs.append(expanded)

    if not expanded_dirs:
        return [], "No valid directories after expansion"

    cmd = [
        checker,
        '--json',
        '--recursive',
        '--check-fetch',
        '--ignore-untracked',
        *expanded_dirs,
    ]
    timeout = _CHECKER_TIMEOUT_BASE + _CHECKER_TIMEOUT_PER_DIR * len(
        expanded_dirs
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
            cwd=Path.cwd(),
            check=False,
        )

        if result.stderr:
            logger.debug(
                "git-status-checker stderr: %s", result.stderr[:500]
            )

        # exit 0 = all clean, exit 1 = some repos outdated (still valid JSON)
        if result.returncode not in (0, 1):
            return [], (
                f"git-status-checker exited {result.returncode}"
            )

        output = result.stdout.strip()
        if not output:
            return [], None

        data = json.loads(output)
        repos = data.get('repositories', [])
        logger.info(
            "git-status-checker found %d repositories in %d directories",
            len(repos),
            len(expanded_dirs),
        )
        return repos, None

    except json.JSONDecodeError as exc:
        return [], f"git-status-checker JSON parse error: {exc}"
    except subprocess.TimeoutExpired:
        return [], f"git-status-checker timed out after {timeout}s"
    except OSError as exc:
        return [], f"Error calling git-status-checker: {exc}"


def _git_info_from_checker(
    repo_status: dict[str, Any],
    repo_path: Path,
) -> dict[str, Any]:
    """Build a git_info dict from git-status-checker output.

    git-status-checker provides dirty/ahead/behind/up_to_date already.
    Branch, last commit, and remote URL still need lightweight git calls.
    """
    git_info: dict[str, Any] = {
        'path': str(repo_path),
        'name': repo_path.name,
        'dirty': bool(repo_status.get('local_changes')),
        'ahead': repo_status.get('ahead', False),
        'behind': repo_status.get('behind', False),
        'last_commit': None,
        'last_commit_author': None,
        'last_commit_date': None,
        'branch': None,
        'remote': None,
        'remote_name': None,
        'remote_web_url': None,
        'up_to_date': repo_status.get('up_to_date', True),
        'has_remote_changes': repo_status.get('has_remote_changes', False),
        'local_changes': repo_status.get('local_changes', []),
    }

    try:
        git_info['branch'] = _repo_stdout(
            repo_path, ['rev-parse', '--abbrev-ref', 'HEAD']
        )

        log_line = _repo_stdout(
            repo_path,
            ['log', '-1', '--format=%H|%an|%ae|%ad|%s', '--date=iso'],
        )
        if log_line:
            parts = log_line.split('|', 4)
            if len(parts) >= 4:
                git_info['last_commit'] = parts[0][:8]
                git_info['last_commit_author'] = parts[1]
                git_info['last_commit_date'] = parts[3]

        remote_name, remote_url = _get_remote(
            repo_path,
            git_info['branch'],
        )
        git_info['remote_name'] = remote_name
        git_info['remote'] = remote_url
        git_info['remote_web_url'] = _remote_web_url(remote_url)

    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.debug(
            "Supplemental git info failed for %s: %s", repo_path, exc
        )

    return git_info


def _repo_stdout(repo_path: Path, args: list[str]) -> str | None:
    """Run a read-only git command and return trimmed stdout when it succeeds."""
    result = subprocess.run(
        ['git', *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None

    output = result.stdout.strip()
    return output or None


def _remote_web_url(remote_url: str | None) -> str | None:
    """Convert common Git remote URL forms to a browser URL when possible."""
    if not remote_url:
        return None

    cleaned_url = remote_url.removesuffix('.git')
    if cleaned_url.startswith(('http://', 'https://')):
        return cleaned_url

    ssh_match = SSH_REMOTE_PATTERN.match(cleaned_url)
    if ssh_match:
        host, path = ssh_match.groups()
        return f"https://{host}/{path}"

    ssh_prefix = 'ssh://git@'
    if cleaned_url.startswith(ssh_prefix):
        return f"https://{cleaned_url[len(ssh_prefix):]}"

    return None


def _remote_url_for_name(repo_path: Path, remote_name: str) -> str | None:
    """Return a remote URL using both modern and older Git commands."""
    remote_url = _repo_stdout(repo_path, ['remote', 'get-url', remote_name])
    if remote_url:
        return remote_url

    remote_url = _repo_stdout(
        repo_path,
        ['config', '--get', f'remote.{remote_name}.url'],
    )
    if remote_url:
        return remote_url

    remote_lines = _repo_stdout(repo_path, ['remote', '-v'])
    if not remote_lines:
        return None

    prefix = f"{remote_name}\t"
    for line in remote_lines.splitlines():
        if not line.startswith(prefix):
            continue
        url = line[len(prefix):].split(maxsplit=1)[0]
        if url:
            return url
    return None


def _get_remote(
    repo_path: Path,
    branch: str | None,
) -> tuple[str | None, str | None]:
    """Return the best available remote name and URL for a repository."""
    if branch and branch != 'HEAD':
        upstream_remote = _repo_stdout(
            repo_path,
            ['config', '--get', f'branch.{branch}.remote'],
        )
        if upstream_remote and upstream_remote != '.':
            upstream_url = _remote_url_for_name(repo_path, upstream_remote)
            if upstream_url:
                return upstream_remote, upstream_url

    origin_url = _remote_url_for_name(repo_path, 'origin')
    if origin_url:
        return 'origin', origin_url

    first_remote = _repo_stdout(repo_path, ['remote'])
    if not first_remote:
        return None, None

    remote_name = first_remote.splitlines()[0].strip()
    if not remote_name:
        return None, None
    return remote_name, _remote_url_for_name(repo_path, remote_name)


def _get_remote_url(
    repo_path: Path,
    branch: str | None,
) -> str | None:
    """Return the best available remote URL for compatibility with tests."""
    _, remote_url = _get_remote(repo_path, branch)
    return remote_url


def _get_git_info(repo_path: Path) -> dict[str, Any] | None:
    """
    Get git info for a repository using direct git commands.
    
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
        'remote_name': None,
        'remote_web_url': None,
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
        
        remote_name, remote_url = _get_remote(repo_path, git_info['branch'])
        git_info['remote_name'] = remote_name
        git_info['remote'] = remote_url
        git_info['remote_web_url'] = _remote_web_url(remote_url)
        
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
        
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.warning(f"Error getting git info for {repo_path}: {e}")
        return None


def _find_git_repos(base_dirs: list[str]) -> list[Path]:
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
        except OSError as e:
            logger.error(f"Error processing directory {base_dir}: {e}", exc_info=True)
            continue
    
    logger.info(f"Total repositories found: {len(repos)}")
    return repos


def _check_reproducibility_health(repo_path: Path) -> dict[str, Any]:
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
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.debug(f"Unable to check staleness for {repo_path}: {e}")
    
    return health


def _check_drift_and_footprint(repo_path: Path) -> dict[str, Any]:
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
        total_size = 0
        git_size = 0
        last_modified = 0.0
        git_dir = repo_path / '.git'
        git_dir_str = str(git_dir)

        for root, dirs, files in os.walk(repo_path):
            inside_git = root == git_dir_str or root.startswith(
                git_dir_str + os.sep
            )

            for file in files:
                try:
                    stat = (Path(root) / file).stat()
                except (OSError, PermissionError):
                    continue

                if inside_git:
                    git_size += stat.st_size
                else:
                    total_size += stat.st_size
                    if stat.st_mtime > last_modified:
                        last_modified = stat.st_mtime

            if not inside_git:
                dirs[:] = [
                    d for d in dirs
                    if d == '.git' or not d.startswith('.')
                ]

        info['directory_size'] = total_size
        if git_dir.exists():
            info['git_size'] = git_size
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
            
            # Calculate drift - only show for dirty repos (uncommitted changes)
            drift_days = None
            
            # Check git status for uncommitted changes
            status_result = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            if status_result.returncode == 0 and status_result.stdout.strip():
                # Has uncommitted changes - calculate drift
                if last_modified > 0 and last_modified > last_commit_time:
                    drift_seconds = last_modified - last_commit_time
                    drift_days = round(drift_seconds / 86400, 1)
            # Clean repos: drift_days stays None
            
            info['drift_days'] = drift_days
        
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
        
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.warning(f"Error checking drift/footprint for {repo_path}: {e}")
    
    return info

def _process_repo(repo_path: Path) -> dict[str, Any] | None:
    """
    Process a single repository and return project data.
    
    Args:
        repo_path: Path to git repository
    
    Returns:
        Project data dict or None if processing fails
    """
    try:
        if not repo_path.exists():
            logger.warning(f"Repository path does not exist: {repo_path}")
            return None
        
        git_info = _get_git_info(repo_path)
        
        if not git_info:
            logger.warning(f"Could not get git info for {repo_path}")
            return None
        
        # Ensure path is a string, not a Path object
        if isinstance(git_info.get('path'), Path):
            git_info['path'] = str(git_info['path'])
        
        # Get reproducibility and drift data with error handling
        try:
            reproducibility = _check_reproducibility_health(repo_path)
        except PROJECT_REPO_ERRORS as e:
            logger.warning(f"Error checking reproducibility for {repo_path}: {e}")
            reproducibility = {
                'environment_files': [],
                'workflow_configs': [],
                'missing_common_files': [],
                'staleness': {},
            }
        
        try:
            drift_footprint = _check_drift_and_footprint(repo_path)
        except PROJECT_REPO_ERRORS as e:
            logger.warning(f"Error checking drift/footprint for {repo_path}: {e}")
            drift_footprint = {
                'directory_size': 0,
                'git_size': 0,
                'last_modified': None,
                'last_commit': None,
                'drift_days': None,
                'large_untracked_files': [],
            }
        
        return {
            'name': git_info['name'],
            'path': str(repo_path),  # Ensure path is always a string
            'git': git_info,
            'reproducibility': reproducibility,
            'drift_footprint': drift_footprint,
        }
    except (PermissionError, OSError) as e:
        # Permission/authentication errors - skip this repo
        logger.warning(f"Skipping repository {repo_path} due to permission/authentication error: {e}")
        return None
    except PROJECT_REPO_ERRORS as e:
        logger.error(f"Error processing repository {repo_path}: {e}", exc_info=True)
        return None


def _load_projects_cache() -> dict[str, Any] | None:
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

        if cache.get('schema_version') != PROJECTS_CACHE_SCHEMA_VERSION:
            logger.info("Projects cache schema changed; refreshing project data")
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
    except (OSError, TypeError) as e:
        logger.warning(f"Error loading projects cache: {e}", exc_info=True)
        return None


def _save_projects_cache(projects_data: list[dict[str, Any]], directories: list[str]) -> None:
    """Save projects cache to file.
    
    Args:
        projects_data: List of project data dictionaries
        directories: List of directories that were scanned
    """
    try:
        PROJECTS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cache = {
            'schema_version': PROJECTS_CACHE_SCHEMA_VERSION,
            'timestamp': time.time(),
            'directories': directories,
            'projects': projects_data,
        }
        with PROJECTS_CACHE_FILE.open('w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved projects cache: {len(projects_data)} projects from {len(directories)} directories")
    except (OSError, TypeError) as e:
        logger.warning(f"Error saving projects cache: {e}")


def _collect_projects_data(
    project_dirs: list[str],
    use_cache: bool = True,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Collect project data from cache or by scanning directories.
    Scans configured directories so git state reflects the current filesystem.
    Uses a valid cache only as a fallback when scanning fails.
    
    Args:
        project_dirs: List of project directories to scan
        use_cache: Whether to use cache (default True)
    
    Returns:
        Tuple of (projects_data_list, error_message)
    """
    # Normalize directory paths for comparison
    normalized_dirs = sorted([expand_path(d) for d in project_dirs])
    
    # Load cache only as an error fallback. Returning it before a scan would
    # hide new repos, deleted repos, branch changes, and dirty/clean state.
    cached_data = None
    if use_cache:
        cached_data = _load_projects_cache()
    
    logger.info(f"Scanning {len(normalized_dirs)} project directories")
    try:
        projects_data, error = _scan_directories(normalized_dirs)
        if use_cache:
            try:
                _save_projects_cache(projects_data, normalized_dirs)
            except (OSError, TypeError) as cache_err:
                logger.warning(f"Failed to save cache: {cache_err}")
        return projects_data, error
    except PROJECT_REPO_ERRORS as e:
        logger.error(f"Error in _collect_projects_data: {e}", exc_info=True)
        if cached_data:
            cached_projects = cached_data.get('projects', [])
            valid_projects = [
                p for p in cached_projects
                if isinstance(p, dict) and 'path' in p
            ]
            if valid_projects:
                logger.warning(
                    f"Returning {len(valid_projects)} cached projects after "
                    f"scan failure: {e}"
                )
                return (
                    valid_projects,
                    f"Error collecting projects data: {str(e)}",
                )

        return [], f"Error collecting projects data: {str(e)}"


def _process_checker_repos(
    checker_repos: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build project records from git-status-checker repository entries."""
    projects_data: list[dict[str, Any]] = []
    skipped: list[str] = []

    for repo_status in checker_repos:
        raw_path = repo_status.get('path', '')
        if not raw_path:
            continue

        repo_path = Path(raw_path)
        if not repo_path.is_dir():
            skipped.append(raw_path)
            continue

        try:
            git_info = _git_info_from_checker(repo_status, repo_path)

            try:
                reproducibility = _check_reproducibility_health(repo_path)
            except PROJECT_REPO_ERRORS:
                reproducibility = {
                    'environment_files': [],
                    'workflow_configs': [],
                    'missing_common_files': [],
                    'staleness': {},
                }

            try:
                drift_footprint = _check_drift_and_footprint(repo_path)
            except PROJECT_REPO_ERRORS:
                drift_footprint = {
                    'directory_size': 0,
                    'git_size': 0,
                    'last_modified': None,
                    'last_commit': None,
                    'drift_days': None,
                    'large_untracked_files': [],
                }

            projects_data.append({
                'name': git_info['name'],
                'path': str(repo_path),
                'git': git_info,
                'reproducibility': reproducibility,
                'drift_footprint': drift_footprint,
            })
        except PROJECT_REPO_ERRORS as exc:
            logger.warning("Skipping %s: %s", repo_path, exc)
            skipped.append(str(repo_path))

    return projects_data, skipped


def _scan_directories_manual(
    project_dirs: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fallback scanner: walk directories and run git commands per repo."""
    projects_data: list[dict[str, Any]] = []
    skipped: list[str] = []

    repos = _find_git_repos(project_dirs)
    logger.info("Manual scan found %d repositories", len(repos))

    for repo_path in repos:
        try:
            project = _process_repo(repo_path)
            if project:
                projects_data.append(project)
            else:
                skipped.append(str(repo_path))
        except PROJECT_REPO_ERRORS as exc:
            logger.warning("Error processing %s: %s", repo_path, exc)
            skipped.append(str(repo_path))

    return projects_data, skipped


def _scan_directories(
    project_dirs: list[str],
) -> tuple[list[dict[str, Any]], str | None]:
    """Scan directories for git repositories and collect project data.

    Tries git-status-checker first (single process, bulk scan). Falls back
    to manual per-repo git commands when the binary is unavailable.
    """
    checker_repos, checker_error = _call_git_status_checker(project_dirs)

    if checker_repos:
        projects_data, skipped = _process_checker_repos(checker_repos)
    else:
        if checker_error:
            logger.info(
                "git-status-checker unavailable (%s); using manual scan",
                checker_error,
            )
        projects_data, skipped = _scan_directories_manual(project_dirs)

    if not projects_data and not skipped:
        return [], (
            "No git repositories found. Checked directories: "
            + ", ".join(project_dirs)
        )

    projects_data.sort(key=lambda x: x['name'].lower())
    logger.info(
        "Collected %d projects from %d directories",
        len(projects_data),
        len(project_dirs),
    )

    error = None
    if not projects_data and skipped:
        error = (
            f"Skipped {len(skipped)} repository/repositories "
            f"(permission errors or processing issues)"
        )

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
        
        validated_project_dirs = []
        for project_dir in project_dirs:
            if not isinstance(project_dir, str):
                return jsonify({
                    'projects': [],
                    'total': 0,
                    'error': 'Project directories must be strings.',
                }), 200

            validated_directory, directory_error = validate_project_directory(
                project_dir
            )
            if directory_error is not None or validated_directory is None:
                return jsonify({
                    'projects': [],
                    'total': 0,
                    'error': directory_error or 'Invalid project directory.',
                }), 200
            validated_project_dirs.append(str(validated_directory))

        logger.info(
            f"Processing {len(validated_project_dirs)} project directories: "
            f"{validated_project_dirs}"
        )
        # Projects always scans live. Refresh keeps cache fallback/update enabled.
        force_refresh = request.args.get('refresh', '').lower() == 'true'
        if force_refresh:
            logger.info("Project refresh requested; scanning live and updating cache")
        
        try:
            projects_data, error = _collect_projects_data(
                validated_project_dirs,
                use_cache=True,
            )
        except PROJECT_REPO_ERRORS as collect_err:
            logger.error(
                f"Error in _collect_projects_data: {collect_err}",
                exc_info=True,
            )
            return jsonify({
                'projects': [],
                'total': 0,
                'error': f'Error collecting projects: {str(collect_err)}',
            }), 500
        
        return jsonify({
            'projects': projects_data,
            'total': len(projects_data),
            'error': error,
        }), 200
    except PROJECT_REPO_ERRORS as e:
        logger.error(f"Error in projects_status endpoint: {e}", exc_info=True)
        return jsonify({
            'projects': [],
            'total': 0,
            'error': f'Error loading projects: {str(e)}',
        }), 500
