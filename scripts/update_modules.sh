#!/bin/bash
# Script to update modules list from module spider

source /usr/share/lmod/lmod/init/bash
module -t spider > logs/modules.txt 2>&1
