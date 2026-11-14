#!/usr/bin/env bash
# HicksField terminal installer. Runs the same install as the GUI.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 installer.py --text
