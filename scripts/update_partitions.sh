#!/bin/bash
# Script to update partitions list from sinfo -s

# Ensure logs directory exists
mkdir -p logs

# Run sinfo -s and save to file
sinfo -s > logs/partitions.txt 2>&1
