# Standard library imports
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Third-party imports
from flask import Blueprint, render_template

jobs_bp = Blueprint('jobs', __name__, url_prefix='/jobs')
logger = logging.getLogger(__name__.capitalize())

PARTITIONS_FILE = Path('logs/partitions.txt')


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
        })
    
    # Sort by availability percentage (descending), then by name
    partitions.sort(key=lambda x: (-x['availability_pct'], x['name']))
    
    return partitions


def _get_partition_info() -> Tuple[Optional[List[Dict]], Optional[str]]:
    """
    Read partition data from file (updated by background script).
    
    Returns:
        Tuple of (partitions_list, error_message)
    """
    if not PARTITIONS_FILE.exists():
        return None, "Partition data not available. Please wait for data to be collected."
    
    try:
        # Check if file is stale (older than 10 minutes)
        file_age = time.time() - PARTITIONS_FILE.stat().st_mtime
        if file_age > 600:  # 10 minutes
            return None, "Partition data is stale. Please refresh the page."
        
        # Read and parse the file
        with PARTITIONS_FILE.open('r', encoding='utf-8') as f:
            content = f.read()
        
        if not content.strip():
            return None, "Partition data file is empty."
        
        partitions = _parse_sinfo_output(content)
        if not partitions:
            return None, "No partition data found in file."
        
        return partitions, None
        
    except Exception as e:
        error_msg = f"Error reading partition data: {str(e)}"
        logger.warning(error_msg, exc_info=True)
        return None, error_msg


@jobs_bp.route('/')
def jobs():
    """Render the jobs page with partition information."""
    partitions, error = _get_partition_info()
    
    # Calculate summary statistics
    summary = None
    if partitions:
        summary = {
            'total_partitions': len(partitions),
            'total_nodes': sum(p['total'] for p in partitions),
            'available_nodes': sum(p['idle'] for p in partitions),
            'allocated_nodes': sum(p['allocated'] for p in partitions),
        }
    
    return render_template(
        'jobs.html',
        partitions=partitions,
        summary=summary,
        error=error,
    )
