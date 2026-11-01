#!/bin/sh
# Start the checkout's GUI without depending on the current directory or Conda.
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LAUNCHER="$PROJECT_DIR/.venv/bin/drivepak-manager"

if [ ! -x "$LAUNCHER" ]; then
    MESSAGE="DrivePak Manager is not set up in this project directory yet.

Open a terminal in $PROJECT_DIR and run:
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .

Then run start-drivepak.sh again."
    printf '%s\n' "$MESSAGE" >&2
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v kdialog >/dev/null 2>&1; then
        kdialog --error "$MESSAGE" || :
    fi
    exit 1
fi

exec "$LAUNCHER" "$@"
