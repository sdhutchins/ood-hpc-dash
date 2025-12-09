#!/bin/bash
# Script to get disk quota information

# Ensure logs directory exists
mkdir -p logs

# Get df -h output for home and scratch in a parseable format
{
    # Home directory
    if [ -d "$HOME" ]; then
        df -h "$HOME" 2>/dev/null | tail -n +2 | awk '{print "HOME|" $1 "|" $2 "|" $3 "|" $4 "|" $5 "|" $6}'
    fi
    
    # Scratch directory
    if [ -d "/gpfs/scratch/$USER" ]; then
        df -h "/gpfs/scratch/$USER" 2>/dev/null | tail -n +2 | awk '{print "SCRATCH|" $1 "|" $2 "|" $3 "|" $4 "|" $5 "|" $6}'
    fi
} > logs/disk_quota.txt 2>&1
