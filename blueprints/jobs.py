# Standard library imports
import json
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
SLURM_LOAD_FILE = Path('logs/slurm_load.txt')
PARTITION_METADATA_FILE = Path('config/partition_metadata.json')


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


def _get_slurm_load() -> Optional[str]:
    """Read slurm-load output from file."""
    if not SLURM_LOAD_FILE.exists():
        return None
    
    try:
        # Check if file is stale (older than 10 minutes)
        file_age = time.time() - SLURM_LOAD_FILE.stat().st_mtime
        if file_age > 600:  # 10 minutes
            return None
        
        with SLURM_LOAD_FILE.open('r', encoding='utf-8') as f:
            content = f.read().strip()
        
        return content if content else None
        
    except Exception as e:
        logger.warning(f"Error reading slurm-load data: {e}", exc_info=True)
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


def _generate_formatted_partition_table(partitions: List[Dict[str, Any]]) -> str:
    """Generate formatted partition table in markdown-style format."""
    if not PARTITION_METADATA_FILE.exists():
        return ""
    
    try:
        with PARTITION_METADATA_FILE.open('r', encoding='utf-8') as f:
            metadata = json.load(f)
    except Exception as e:
        logger.warning(f"Error loading partition metadata: {e}")
        return ""
    
    # Create a dict mapping partition names to partition data
    partition_dict = {p['name'].rstrip('*'): p for p in partitions}
    
    # Group partitions by category
    categories = {}
    for part_name, part_data in partition_dict.items():
        if part_name in metadata:
            category = metadata[part_name]['category']
            if category not in categories:
                categories[category] = []
            categories[category].append((part_name, part_data, metadata[part_name]))
    
    # Build the table
    lines = []
    lines.append("-------------------    Available Slurm Partitions    --------------------------")
    lines.append("")
    lines.append("| Partition          | Nodes | Nodes Per Researcher |       Time Limit | Priority Tier |")
    lines.append("| :----------------- | ----: | -------------------: | ---------------: | ------------: |")
    
    # Output by category
    category_order = ["CPU", "GPU", "Large mem"]
    for category in category_order:
        if category not in categories:
            continue
        
        lines.append(f"| **{category}**            |       |                      |                  |               |")
        
        # Sort partitions within category
        cat_partitions = sorted(categories[category], key=lambda x: x[0])
        
        for part_name, part_data, meta in cat_partitions:
            nodes = part_data['total']
            nodes_per_researcher = meta['nodes_per_researcher']
            time_limit = _format_time_limit(part_data['timelimit'])
            priority_tier = meta['priority_tier']
            
            # Format nodes per researcher
            if isinstance(nodes_per_researcher, str):
                nodes_per_researcher_str = nodes_per_researcher
            else:
                nodes_per_researcher_str = str(nodes_per_researcher)
            
            lines.append(
                f"| {part_name:<18} | {nodes:>5} | {nodes_per_researcher_str:>20} | {time_limit:>15} | {priority_tier:>13} |"
            )
        
        lines.append("|                    |       |                      |                  |               |")
    
    lines.append("+--------------------+-------+----------------------+------------------+---------------+")
    
    return "\n".join(lines)


@jobs_bp.route('/')
def jobs():
    """Render the jobs page with partition information."""
    partitions, error = _get_partition_info()
    slurm_load = _get_slurm_load()
    
    # Generate formatted partition table
    formatted_table = ""
    if partitions:
        formatted_table = _generate_formatted_partition_table(partitions)
    
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
        slurm_load=slurm_load,
        formatted_table=formatted_table,
        error=error,
    )
