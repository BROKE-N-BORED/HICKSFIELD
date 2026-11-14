#!/usr/bin/env bash
# Run the installed HicksField.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
if [ ! -x "$DIR/venv/bin/python" ]; then
  echo "HicksField is not installed yet. Run bash install.sh first." >&2
  exit 1
fi
exec "$DIR/venv/bin/python" -m hicksfield "$@"
