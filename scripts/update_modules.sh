#!/bin/bash
# Script to update modules list from module spider

module -t spider > logs/modules.txt 2>&1
