#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
if [ -x .venv/bin/python ]; then
    BUILD_PYTHON=.venv/bin/python
else
    BUILD_PYTHON=python3
fi
"$BUILD_PYTHON" -m build --no-isolation
"$BUILD_PYTHON" tools/package_linux.py
