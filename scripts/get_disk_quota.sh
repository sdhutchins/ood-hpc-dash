#!/bin/bash
# Script to get disk quota information

# Ensure logs directory exists
mkdir -p logs

# Run the quota report script
python3 /share/apps/rc/bin/gpfs5-quota-report.py > logs/disk_quota.txt 2>&1
