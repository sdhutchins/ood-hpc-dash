# Standard library imports
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Third-party imports
from flask import Blueprint, jsonify, render_template, request

jobs_bp = Blueprint('jobs', __name__, url_prefix='/jobs')
logger = logging.getLogger(__name__.capitalize())

PARTITIONS_FILE = Path('logs/partitions.txt')
SLURM_LOAD_FILE = Path('logs/slurm_load.txt')
PARTITION_METADATA_FILE = Path('config/partition_metadata.json')

# Common absolute paths for SLURM binaries
SINFO_PATHS = [
    '/cm/shared/apps/slurm/18.08.9/bin/sinfo',
    '/usr/bin/sinfo',
    '/opt/slurm/bin/sinfo',
    '/usr/local/bin/sinfo',
]
SQUEUE_PATHS = [
    '/cm/shared/apps/slurm/18.08.9/bin/squeue',
    '/usr/bin/squeue',
    '/opt/slurm/bin/squeue',
    '/usr/local/bin/squeue',
]
SACCT_PATHS = [
    '/cm/shared/apps/slurm/18.08.9/bin/sacct',
    '/usr/bin/sacct',
    '/opt/slurm/bin/sacct',
    '/usr/local/bin/sacct',
]
SEFF_PATHS = [
    '/cm/shared/apps/slurm/18.08.9/bin/seff',
    '/usr/bin/seff',
    '/opt/slurm/bin/seff',
    '/usr/local/bin/seff',
]


def _find_binary(paths: List[str]) -> Optional[str]:
    """Find first existing binary from list of absolute paths."""
    for path in paths:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return None


def _call_sinfo() -> Tuple[Optional[str], Optional[str]]:
    """
    Call sinfo -s with absolute path and explicit environment.
    
    Returns:
        Tuple of (output, error_message)
    """
    sinfo_path = _find_binary(SINFO_PATHS)
    if not sinfo_path:
        return None, "sinfo binary not found in standard locations"
    
    try:
        result = subprocess.run(
            [sinfo_path, '-s'],
            capture_output=True,
            text=True,
            timeout=30,
            env={'PATH': '/usr/bin:/bin'},
            cwd=Path.cwd(),
        )
        if result.returncode == 0:
            return result.stdout, None
        return None, f"sinfo failed: {result.stderr}"
    except subprocess.TimeoutExpired:
        return None, "sinfo command timed out"
    except Exception as e:
        return None, f"Error calling sinfo: {str(e)}"


def _call_squeue(user: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Call squeue with absolute path and explicit environment.
    
    Args:
        user: Optional username to filter jobs. If None, shows all jobs.
    
    Returns:
        Tuple of (output, error_message)
    """
    squeue_path = _find_binary(SQUEUE_PATHS)
    if not squeue_path:
        return None, "squeue binary not found in standard locations"
    
    cmd = [squeue_path, '--Format=JobID,Name,State,Partition,TimeUsed,TimeLimit,User']
    if user:
        cmd.extend(['-u', user])
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env={'PATH': '/usr/bin:/bin'},
            cwd=Path.cwd(),
        )
        if result.returncode == 0:
            return result.stdout, None
        return None, f"squeue failed: {result.stderr}"
    except subprocess.TimeoutExpired:
        return None, "squeue command timed out"
    except Exception as e:
        return None, f"Error calling squeue: {str(e)}"


def _call_seff(job_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Call seff to get detailed job efficiency report.
    
    Args:
        job_id: Job ID to get efficiency report for
    
    Returns:
        Tuple of (output, error_message)
    """
    seff_path = _find_binary(SEFF_PATHS)
    if not seff_path:
        return None, "seff binary not found in standard locations"
    
    try:
        result = subprocess.run(
            [seff_path, job_id],
            capture_output=True,
            text=True,
            timeout=10,
            env={'PATH': '/usr/bin:/bin'},
            cwd=Path.cwd(),
        )
        if result.returncode == 0:
            return result.stdout, None
        return None, f"seff failed: {result.stderr}"
    except subprocess.TimeoutExpired:
        return None, "seff command timed out"
    except Exception as e:
        return None, f"Error calling seff: {str(e)}"


def _call_sacct(user: Optional[str] = None, max_jobs: int = 100) -> Tuple[Optional[str], Optional[str]]:
    """
    Call sacct to get job history with efficiency metrics.
    
    Args:
        user: Optional username to filter jobs. If None, uses current user.
        max_jobs: Maximum number of jobs to retrieve (default 100)
    
    Returns:
        Tuple of (output, error_message)
    """
    sacct_path = _find_binary(SACCT_PATHS)
    if not sacct_path:
        return None, "sacct binary not found in standard locations"
    
    if not user:
        user = os.environ.get('USER', '')
    
    # Calculate date 90 days ago in YYYY-MM-DD format
    start_date = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
    
    # Format: JobID,JobName,State,Partition,Start,End,Elapsed,TotalCPU,ReqCPUS,MaxRSS,AllocCPUS,CPUTime
    # Note: CPUUtilization is not available in all SLURM versions, so we calculate it from TotalCPU
    # Note: sacct doesn't have a -n option to limit results, so we limit in Python after parsing
    # Use --allocations to show only job-level entries (not individual steps)
    cmd = [
        sacct_path,
        '--format=JobID,JobName,State,Partition,Start,End,Elapsed,TotalCPU,ReqCPUS,MaxRSS,AllocCPUS,CPUTime',
        '--parsable2',
        '--noheader',
        '--units=M',  # Memory in MB
        '--allocations',  # Only show job allocations, not steps
        '-u', user,
        '--starttime', start_date,  # Last 90 days
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env={'PATH': '/usr/bin:/bin'},
            cwd=Path.cwd(),
        )
        if result.returncode == 0:
            return result.stdout, None
        return None, f"sacct failed: {result.stderr}"
    except subprocess.TimeoutExpired:
        return None, "sacct command timed out"
    except Exception as e:
        return None, f"Error calling sacct: {str(e)}"


def _load_partition_metadata() -> Dict[str, Any]:
    """Load partition metadata from JSON file."""
    if not PARTITION_METADATA_FILE.exists():
        return {}
    try:
        with PARTITION_METADATA_FILE.open('r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_sinfo_output(output: str) -> List[Dict[str, Any]]:
    """
    Parse sinfo -s output into structured partition data.
    
    Expected format:
    PARTITION          AVAIL  TIMELIMIT   NODES(A/I/O/T)  NODELIST
    interactive           up    2:00:00       71/24/4/99  c[0136-0149,0151-0235]
    
    Returns:
        List of dictionaries with partition information.
    """
    partitions = []
    lines = output.strip().split('\n')
    
    # Load metadata once
    metadata = _load_partition_metadata()
    
    # Skip header line
    if len(lines) < 2:
        return partitions
    
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        
        # Parse line: partition name, avail, timelimit, nodes, nodelist
        # Use regex to handle variable whitespace
        match = re.match(
            r'^(\S+)\s+(\S+)\s+(\S+)\s+(\d+)/(\d+)/(\d+)/(\d+)\s+(.+)$',
            line
        )
        if not match:
            continue
        
        partition_name = match.group(1)
        avail = match.group(2)
        timelimit = match.group(3)
        allocated = int(match.group(4))
        idle = int(match.group(5))
        other = int(match.group(6))
        total = int(match.group(7))
        nodelist = match.group(8).strip()
        
        # Calculate availability percentage (idle / total)
        availability_pct = (idle / total * 100) if total > 0 else 0.0
        
        # Get partition category from metadata
        category = "Other"
        lookup_name = partition_name.rstrip('*')
        if lookup_name in metadata:
            category = metadata[lookup_name].get('category', 'Other')
        
        partitions.append({
            'name': partition_name,
            'avail': avail,
            'timelimit': timelimit,
            'allocated': allocated,
            'idle': idle,
            'other': other,
            'total': total,
            'nodelist': nodelist,
            'availability_pct': round(availability_pct, 1),
            'category': category,
        })
    
    # Sort by availability percentage (descending), then by name
    partitions.sort(key=lambda x: (-x['availability_pct'], x['name']))
    
    return partitions


def _get_partition_info() -> Tuple[Optional[List[Dict]], Optional[str]]:
    """
    Get partition data by calling sinfo directly.
    
    Returns:
        Tuple of (partitions_list, error_message)
    """
    output, error = _call_sinfo()
    if error:
        logger.warning(f"Error calling sinfo: {error}")
        return None, error
    
    if not output or not output.strip():
        return None, "sinfo returned empty output"
    
    try:
        partitions = _parse_sinfo_output(output)
        if not partitions:
            return None, "No partition data found in sinfo output."
        return partitions, None
    except Exception as e:
        error_msg = f"Error parsing sinfo output: {str(e)}"
        logger.warning(error_msg, exc_info=True)
        return None, error_msg


def _parse_slurm_load() -> Optional[Dict[str, Any]]:
    """Parse slurm-load output and return structured data."""
    if not SLURM_LOAD_FILE.exists():
        return None
    
    try:
        # Check if file is stale (older than 10 minutes)
        file_age = time.time() - SLURM_LOAD_FILE.stat().st_mtime
        if file_age > 600:  # 10 minutes
            return None
        
        with SLURM_LOAD_FILE.open('r', encoding='utf-8') as f:
            content = f.read().strip()
        
        if not content:
            return None
        
        # Parse the output
        load_data = {}
        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if 'Allocated nodes:' in line:
                load_data['allocated_nodes'] = int(line.split(':')[1].strip())
            elif 'Idle nodes:' in line:
                load_data['idle_nodes'] = int(line.split(':')[1].strip())
            elif 'Total CPU cores:' in line:
                load_data['total_cores'] = int(line.split(':')[1].strip())
            elif 'Allocated cores:' in line:
                load_data['allocated_cores'] = int(line.split(':')[1].strip())
            elif 'Idle cores:' in line:
                load_data['idle_cores'] = int(line.split(':')[1].strip())
            elif 'Running/Pending jobs:' in line:
                jobs_part = line.split(':')[1].strip()
                if '/' in jobs_part:
                    parts = jobs_part.split('/')
                    load_data['running_jobs'] = int(parts[0].strip())
                    load_data['pending_jobs'] = int(parts[1].strip())
            elif '% of used cores' in line:
                pct = line.split(':')[1].strip().replace('%', '')
                load_data['cores_pct'] = float(pct)
            elif '% of used nodes' in line:
                pct = line.split(':')[1].strip().replace('%', '')
                load_data['nodes_pct'] = float(pct)
        
        return load_data if load_data else None
        
    except Exception as e:
        logger.warning(f"Error parsing slurm-load data: {e}", exc_info=True)
        return None


def _format_time_limit(timelimit: str) -> str:
    """Format SLURM time limit to readable format."""
    # Handle formats like "2:00:00", "2-02:00:00", "6-06:00:00"
    if '-' in timelimit:
        # Format: days-hours:minutes:seconds
        parts = timelimit.split('-')
        days = int(parts[0])
        time_parts = parts[1].split(':')
        hours = int(time_parts[0])
        if days > 0:
            if hours > 0:
                return f"{days} days, {hours} hours"
            return f"{days} days"
        return f"{hours} hours"
    else:
        # Format: hours:minutes:seconds
        time_parts = timelimit.split(':')
        hours = int(time_parts[0])
        if hours < 24:
            return f"{hours} hours"
        days = hours // 24
        remaining_hours = hours % 24
        if remaining_hours > 0:
            return f"{days} days, {remaining_hours} hours"
        return f"{days} days"


def _generate_partition_reference_data(partitions: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Generate structured partition reference data grouped by category."""
    if not PARTITION_METADATA_FILE.exists():
        return {}
    
    try:
        with PARTITION_METADATA_FILE.open('r', encoding='utf-8') as f:
            metadata = json.load(f)
    except Exception as e:
        logger.warning(f"Error loading partition metadata: {e}")
        return {}
    
    # Create a dict mapping partition names to partition data
    partition_dict = {p['name'].rstrip('*'): p for p in partitions}
    
    # Group partitions by category
    categories = {}
    for part_name, part_data in partition_dict.items():
        if part_name in metadata:
            category = metadata[part_name]['category']
            if category not in categories:
                categories[category] = []
            
            nodes_per_researcher = metadata[part_name]['nodes_per_researcher']
            priority_tier = metadata[part_name]['priority_tier']
            
            categories[category].append({
                'name': part_name,
                'nodes': part_data['total'],
                'nodes_per_researcher': nodes_per_researcher if isinstance(nodes_per_researcher, str) else str(nodes_per_researcher),
                'priority_tier': priority_tier,
            })
    
    # Sort partitions within each category
    for category in categories:
        categories[category].sort(key=lambda x: x['name'])
    
    return categories


def _parse_sacct_output(output: str) -> List[Dict[str, Any]]:
    """
    Parse sacct output and calculate efficiency metrics.
    
    Args:
        output: sacct output (parsable2 format)
    
    Returns:
        List of job dictionaries with efficiency metrics
    """
    jobs = []
    lines = output.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Parsable2 format uses | as delimiter
        parts = line.split('|')
        if len(parts) < 12:
            continue
        
        job_id = parts[0]
        job_name = parts[1] if len(parts) > 1 else 'N/A'
        state = parts[2] if len(parts) > 2 else 'UNKNOWN'
        partition = parts[3] if len(parts) > 3 else 'N/A'
        start = parts[4] if len(parts) > 4 else 'N/A'
        end = parts[5] if len(parts) > 5 else 'N/A'
        elapsed = parts[6] if len(parts) > 6 else '00:00:00'
        total_cpu = parts[7] if len(parts) > 7 else '00:00:00'
        req_cpus = parts[8] if len(parts) > 8 else '0'
        max_rss = parts[9] if len(parts) > 9 else '0M'
        alloc_cpus = parts[10] if len(parts) > 10 else '0'
        cpu_time = parts[11] if len(parts) > 11 else '00:00:00'
        
        # Calculate CPU efficiency from TotalCPU / (Elapsed * AllocCPUS)
        cpu_efficiency = 0.0
        try:
            elapsed_sec = _parse_time_to_seconds(elapsed)
            alloc_cpus_int = int(alloc_cpus) if alloc_cpus and alloc_cpus.isdigit() else 1
            total_cpu_sec = _parse_time_to_seconds(total_cpu)
            if elapsed_sec > 0 and alloc_cpus_int > 0:
                cpu_efficiency = (total_cpu_sec / (elapsed_sec * alloc_cpus_int)) * 100
        except (ValueError, ZeroDivisionError):
            cpu_efficiency = 0.0
        
        # Parse memory (format: "1234M" or "1.2G")
        memory_mb = 0.0
        try:
            if max_rss and max_rss != 'N/A':
                if max_rss.endswith('M'):
                    memory_mb = float(max_rss[:-1])
                elif max_rss.endswith('G'):
                    memory_mb = float(max_rss[:-1]) * 1024
                elif max_rss.endswith('K'):
                    memory_mb = float(max_rss[:-1]) / 1024
        except (ValueError, AttributeError):
            memory_mb = 0.0
        
        jobs.append({
            'id': job_id,
            'name': job_name,
            'state': state,
            'partition': partition,
            'start': start,
            'end': end,
            'elapsed': elapsed,
            'req_cpus': req_cpus,
            'alloc_cpus': alloc_cpus,
            'max_rss': max_rss,
            'cpu_efficiency': round(cpu_efficiency, 1),
            'memory_mb': round(memory_mb, 1),
        })
    
    return jobs


def _parse_time_to_seconds(time_str: str) -> int:
    """Parse SLURM time format (HH:MM:SS or DD-HH:MM:SS) to seconds."""
    if not time_str or time_str == 'N/A':
        return 0
    
    try:
        if '-' in time_str:
            # Format: DD-HH:MM:SS
            parts = time_str.split('-')
            days = int(parts[0])
            time_parts = parts[1].split(':')
            hours = int(time_parts[0])
            minutes = int(time_parts[1]) if len(time_parts) > 1 else 0
            seconds = int(time_parts[2]) if len(time_parts) > 2 else 0
            return days * 86400 + hours * 3600 + minutes * 60 + seconds
        else:
            # Format: HH:MM:SS
            time_parts = time_str.split(':')
            hours = int(time_parts[0])
            minutes = int(time_parts[1]) if len(time_parts) > 1 else 0
            seconds = int(time_parts[2]) if len(time_parts) > 2 else 0
            return hours * 3600 + minutes * 60 + seconds
    except (ValueError, IndexError):
        return 0


@jobs_bp.route('/')
def jobs():
    """Render the jobs page with partition information."""
    partitions, error = _get_partition_info()
    slurm_load_data = _parse_slurm_load()
    
    # Get current user
    username = os.environ.get('USER', '')
    
    # Get user's running/queued jobs
    user_jobs = []
    user_jobs_error = None
    if username:
        output, error_msg = _call_squeue(user=username)
        if not error_msg and output:
            lines = output.strip().split('\n')
            if len(lines) > 1:
                for line in lines[1:]:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 6:
                        user_jobs.append({
                            'id': parts[0],
                            'name': parts[1],
                            'state': parts[2],
                            'partition': parts[3],
                            'time_used': parts[4],
                            'time_limit': parts[5] if len(parts) > 5 else 'N/A',
                        })
        else:
            user_jobs_error = error_msg
    
    # Get job history with pagination
    job_history = []
    total_history = 0
    history_error = None
    current_page = int(request.args.get('page', 1))
    per_page = 10
    
    if username:
        output, error_msg = _call_sacct(user=username, max_jobs=1000)  # Get more jobs for pagination
        if not error_msg and output:
            all_jobs = _parse_sacct_output(output)
            total_history = len(all_jobs)
            # Apply pagination
            offset = (current_page - 1) * per_page
            job_history = all_jobs[offset:offset + per_page]
        else:
            history_error = error_msg
    
    # Generate partition reference data
    partition_reference = {}
    if partitions:
        partition_reference = _generate_partition_reference_data(partitions)
    
    # Calculate summary statistics
    summary = None
    if partitions:
        summary = {
            'total_partitions': len(partitions),
            'total_nodes': sum(p['total'] for p in partitions),
            'available_nodes': sum(p['idle'] for p in partitions),
            'allocated_nodes': sum(p['allocated'] for p in partitions),
        }
        # Add slurm_load data to summary if available
        if slurm_load_data:
            summary.update({
                'total_cores': slurm_load_data.get('total_cores'),
                'allocated_cores': slurm_load_data.get('allocated_cores'),
                'idle_cores': slurm_load_data.get('idle_cores'),
                'running_jobs': slurm_load_data.get('running_jobs'),
                'pending_jobs': slurm_load_data.get('pending_jobs'),
                'cores_pct': slurm_load_data.get('cores_pct'),
                'nodes_pct': slurm_load_data.get('nodes_pct'),
            })
    
    # Calculate pagination info
    total_pages = (total_history + per_page - 1) // per_page if total_history > 0 else 0
    
    return render_template(
        'jobs.html',
        partitions=partitions,
        summary=summary,
        partition_reference=partition_reference,
        error=error,
        user_jobs=user_jobs,
        user_jobs_error=user_jobs_error,
        job_history=job_history,
        total_history=total_history,
        history_error=history_error,
        username=username,
        current_page=current_page,
        total_pages=total_pages,
        per_page=per_page,
    )


@jobs_bp.route('/status')
def jobs_status():
    """
    Return JSON with job status from squeue.
    
    Returns:
        JSON response with running/pending jobs and partition utilization.
    """
    username = os.environ.get('USER', '')
    output, error = _call_squeue(user=username)
    if error:
        return jsonify({'error': error}), 500
    
    if not output:
        return jsonify({
            'jobs': [],
            'running': 0,
            'pending': 0,
        })
    
    # Parse squeue output
    lines = output.strip().split('\n')
    if len(lines) < 2:
        return jsonify({
            'jobs': [],
            'running': 0,
            'pending': 0,
        })
    
    jobs = []
    running = 0
    pending = 0
    
    # Skip header line
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        
        parts = line.split()
        if len(parts) < 6:
            continue
        
        job_id = parts[0]
        name = parts[1]
        state = parts[2]
        partition = parts[3]
        time_used = parts[4]
        time_limit = parts[5] if len(parts) > 5 else 'N/A'
        
        jobs.append({
            'id': job_id,
            'name': name,
            'state': state,
            'partition': partition,
            'time_used': time_used,
            'time_limit': time_limit,
        })
        
        if state in ('RUNNING', 'R'):
            running += 1
        elif state in ('PENDING', 'PD'):
            pending += 1
    
    # Get partition info for utilization
    partitions, _ = _get_partition_info()
    partition_util = {}
    if partitions:
        for p in partitions:
            partition_util[p['name']] = {
                'allocated': p['allocated'],
                'idle': p['idle'],
                'total': p['total'],
            }
    
    return jsonify({
        'jobs': jobs,
        'running': running,
        'pending': pending,
        'partitions': partition_util,
    })


@jobs_bp.route('/history')
def jobs_history():
    """
    Return JSON with job history (paginated).
    
    Query params:
        page: Page number (default 1)
        per_page: Jobs per page (default 10)
    """
    username = os.environ.get('USER', '')
    if not username:
        return jsonify({'error': 'User not found'}), 400
    
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    offset = (page - 1) * per_page
    
    output, error = _call_sacct(user=username, max_jobs=1000)  # Get more jobs for pagination
    if error:
        return jsonify({'error': error}), 500
    
    if not output:
        return jsonify({
            'jobs': [],
            'total': 0,
            'page': page,
            'per_page': per_page,
        })
    
    # Parse all jobs
    all_jobs = _parse_sacct_output(output)
    total = len(all_jobs)
    
    # Apply pagination
    jobs = all_jobs[offset:offset + per_page]
    
    return jsonify({
        'jobs': jobs,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page,
    })


@jobs_bp.route('/efficiency/<job_id>')
def job_efficiency(job_id: str):
    """
    Return JSON with seff efficiency report for a specific job.
    
    Args:
        job_id: Job ID to get efficiency report for
    
    Returns:
        JSON response with seff output
    """
    output, error = _call_seff(job_id)
    if error:
        return jsonify({'error': error}), 500
    
    if not output:
        return jsonify({'error': 'No output from seff'}), 500
    
    # Parse seff output into structured data
    seff_data = {
        'raw_output': output,
        'parsed': {}
    }
    
    # Try to parse key metrics from seff output
    lines = output.split('\n')
    for line in lines:
        line = line.strip()
        if ':' in line:
            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip()
            
            # Extract key metrics
            if 'CPU Efficiency' in key:
                seff_data['parsed']['cpu_efficiency'] = value
            elif 'Memory Efficiency' in key:
                seff_data['parsed']['memory_efficiency'] = value
            elif 'CPU Utilized' in key:
                seff_data['parsed']['cpu_utilized'] = value
            elif 'Memory Utilized' in key:
                seff_data['parsed']['memory_utilized'] = value
            elif 'Job Wall-clock time' in key:
                seff_data['parsed']['wall_clock_time'] = value
            elif 'State' in key:
                seff_data['parsed']['state'] = value
            elif 'Nodes' in key and 'parsed' not in seff_data.get('parsed', {}).get('nodes', ''):
                seff_data['parsed']['nodes'] = value
            elif 'Cores per node' in key:
                seff_data['parsed']['cores_per_node'] = value
    
    return jsonify(seff_data)
