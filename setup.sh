#!/bin/bash
set -eo pipefail
IFS=$'\n\t'

# Load Python module (use Python 3.11 or earlier for Passenger compatibility)
# The 'imp' module was removed in Python 3.12, which breaks Passenger's wsgi-loader
# Check available versions: module avail Python
module load Python/3.11.5-GCCcore-13.2.0
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create bin directory if it doesn't exist
mkdir -p bin

# Remove existing bin/python if it exists (symlink or file)
rm -f bin/python

PYTHON_LIB_PATH=$(python -c "import sysconfig; print(sysconfig.get_config_var('LIBDIR'))" 2>/dev/null || echo "")

# Create Python wrapper script for Passenger
# Passenger looks for bin/python in the app directory and uses it instead of system Python.
# This wrapper activates the venv so Passenger uses the venv's Python with Flask installed.
# See: https://osc.github.io/ood-documentation/latest/tutorials/tutorials-passenger-apps/phusion-passenger.html
cat > bin/python << EOF
#!/usr/bin/env bash

SCRIPT_DIR=\$( cd -- "\$( dirname -- "\${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
APP_DIR=\$( cd -- "\$SCRIPT_DIR/.." &> /dev/null && pwd )

# Load Python module to set up library paths
module load Python/3.11.5-GCCcore-13.2.0 2>/dev/null || true

# Set LD_LIBRARY_PATH if Python library path was found during setup
if [ -n "$PYTHON_LIB_PATH" ] && [ -d "$PYTHON_LIB_PATH" ]; then
    export LD_LIBRARY_PATH="$PYTHON_LIB_PATH\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
fi

# Activate venv (venv is in app root directory)
source "\$APP_DIR/venv/bin/activate"

exec python "\$@"
EOF

# Make it executable
chmod +x bin/python