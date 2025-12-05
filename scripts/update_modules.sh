#!/bin/bash
# Script to update modules list from module spider

# Ensure logs directory exists
mkdir -p logs

# Source lmod initialization script - try common locations
if [ -f /usr/share/lmod/lmod/init/bash ]; then
    source /usr/share/lmod/lmod/init/bash
elif [ -f /etc/profile.d/modules.sh ]; then
    source /etc/profile.d/modules.sh
fi

# Run module spider command
module -t spider > logs/modules.txt 2>&1
