#!/usr/bin/env bash
# Run the installed HicksField.
#
# A failed launch must be visible: from the app menu there is no
# terminal, so an error printed to stderr would vanish and the icon
# would silently do nothing. Failures are shown in a window instead,
# with the choice to run the installer again.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

TITLE="HicksField"

# Show a message in a window. Tries zenity, kdialog, then a tkinter
# message box, then stderr as a last resort.
show_error() {
  local message="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="$TITLE" --text="$message" 2>/dev/null \
      && return 0
  fi
  if command -v kdialog >/dev/null 2>&1; then
    kdialog --title "$TITLE" --error "$message" 2>/dev/null \
      && return 0
  fi
  if python3 -c "import tkinter" 2>/dev/null; then
    python3 -c "import sys, tkinter
from tkinter import messagebox
root = tkinter.Tk()
root.withdraw()
messagebox.showerror('HicksField', sys.argv[1])" \
      "$message" 2>/dev/null && return 0
  fi
  echo "$message" >&2
}

# Ask whether to run the installer again. Returns 0 when the user
# said yes, 1 otherwise (including when no dialog tool is available).
ask_reinstall() {
  local message="$1"
  if command -v zenity >/dev/null 2>&1; then
    if zenity --question --title="$TITLE" --text="$message" \
        --ok-label="Install again" --cancel-label="Close" \
        2>/dev/null; then
      return 0
    fi
    return 1
  fi
  if command -v kdialog >/dev/null 2>&1; then
    if kdialog --title "$TITLE" --yesno "$message" \
        --yes-label "Install again" --no-label "Close" \
        2>/dev/null; then
      return 0
    fi
    return 1
  fi
  if python3 -c "import tkinter" 2>/dev/null; then
    if python3 -c "import sys, tkinter
from tkinter import messagebox
root = tkinter.Tk()
root.withdraw()
sys.exit(0 if messagebox.askyesno('HicksField', sys.argv[1]) else 1)" \
        "$message" 2>/dev/null; then
      return 0
    fi
    return 1
  fi
  return 1
}

# Run the installer again: its window when possible, else the
# terminal install in a terminal window. Only returns when the
# installer could not be started at all.
run_installer() {
  if [ -f "$DIR/installer.py" ]; then
    if python3 -c "import tkinter" 2>/dev/null; then
      exec python3 "$DIR/installer.py"
    fi
    for term in gnome-terminal konsole; do
      if command -v "$term" >/dev/null 2>&1; then
        exec "$term" -- bash -c \
          "bash \"$DIR/install.sh\"; echo; read -rp 'Press Enter to close.'"
      fi
    done
    if command -v xterm >/dev/null 2>&1; then
      exec xterm -e bash -c \
        "bash \"$DIR/install.sh\"; echo; read -rp 'Press Enter to close.'"
    fi
  fi
  show_error "HicksField could not start its installer. Run the installer again from the folder you downloaded HicksField into."
  return 1
}

if [ ! -x "$DIR/venv/bin/python" ]; then
  echo "HicksField is not installed yet. Run bash install.sh first." >&2
  if ask_reinstall "HicksField is not installed yet, or the install is broken. Install it again?"; then
    run_installer
  fi
  exit 1
fi

start=$SECONDS
code=0
"$DIR/venv/bin/python" -m hicksfield "$@" || code=$?
elapsed=$((SECONDS - start))
if [ "$code" -ne 0 ] && [ "$elapsed" -lt 10 ]; then
  # The app died almost at once: the launch itself failed, not the
  # film. Say so in a window, and offer the installer again.
  if ask_reinstall "HicksField did not start. Install it again?"; then
    run_installer
  fi
fi
exit "$code"
