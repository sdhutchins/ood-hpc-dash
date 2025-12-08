#!/bin/bash
# Script to get disk quota information

# Ensure logs directory exists
mkdir -p logs

# Try different quota commands and format output
{
    echo "=== Disk Quota Report ==="
    echo ""
    
    # Try quota -s first (most common)
    if command -v quota >/dev/null 2>&1; then
        quota -s 2>/dev/null || echo "quota command not available"
    fi
    
    # Also try df -h for home and scratch directories
    echo ""
    echo "=== Disk Usage (df -h) ==="
    if [ -d "$HOME" ]; then
        df -h "$HOME" 2>/dev/null | tail -n +2
    fi
    if [ -d "/gpfs/scratch/$USER" ]; then
        df -h "/gpfs/scratch/$USER" 2>/dev/null | tail -n +2
    fi
} > logs/disk_quota.txt 2>&1
