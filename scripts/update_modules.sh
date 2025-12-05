#!/bin/bash
# Script to update modules list from module spider

# Ensure logs directory exists
mkdir -p logs

# Source lmod initialization script
if [ -f /usr/share/lmod/lmod/init/bash ]; then
    source /usr/share/lmod/lmod/init/bash
elif [ -f /etc/profile.d/modules.sh ]; then
    source /etc/profile.d/modules.sh
fi

# Use LMOD_CMD directly if module function isn't available, otherwise use module function
if type module >/dev/null 2>&1; then
    module -t spider > logs/modules.txt 2>&1
else
    # Fallback: use LMOD_CMD directly
    ${LMOD_CMD:-/usr/share/lmod/lmod/libexec/lmod} bash -t spider > logs/modules.txt 2>&1
fi
