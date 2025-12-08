#!/bin/bash
# Script to update partitions list from sinfo -s and slurm-load

# Ensure logs directory exists
mkdir -p logs

# Run sinfo -s and save to file
sinfo -s > logs/partitions.txt 2>&1

# Run slurm-load and save to file
# Try full path first, then fallback to command in PATH
if [ -x "/share/apps/rc/bin/slurm-load" ]; then
    /share/apps/rc/bin/slurm-load > logs/slurm_load.txt 2>&1
elif command -v slurm-load >/dev/null 2>&1; then
    slurm-load > logs/slurm_load.txt 2>&1
else
    echo "slurm-load command not found" > logs/slurm_load.txt 2>&1
fi
