#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BUILD_PYTHON=${RELAY_BUILD_PYTHON:-python3}
exec "$BUILD_PYTHON" "$SCRIPT_DIR/build.py" "$@"
