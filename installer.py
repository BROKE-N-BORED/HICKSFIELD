#!/usr/bin/env python3
"""HicksField installer.

Double-click for the window, or run `python3 installer.py --text`
(or install.sh) for the terminal version. Both run the same install:
install ffmpeg and the Python venv package first (one permission
window asks for the password), then copy the app into
~/.local/share/hicksfield-app, make a venv, install PySide6, render
the icons from the SVG source, write the app-menu entry, and verify
the install works before reporting success.

Engines and voices are NOT set up here. The app sets them up in its
own windows, with real progress and cancel, the first time they are
needed.
"""

import argparse
import hashlib
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# sysdeps uses only the standard library, so it is safe to import
# before the app's own Python home exists.
from hicksfield import sysdeps

APP_NAME = "HicksField"
INSTALL_DIR_NAME = "hicksfield-app"
ICON_SIZES = (16, 32, 48, 64, 128, 256)
PIP_TIMEOUT_S = 900

# Midnight Cinema, kept in step with hicksfield/style.py. The
# installer cannot import the app's style module: it runs before the
# app's Python home exists, so the palette is copied here.
BG = "#171210"
PANEL = "#221b17"
PANEL_LIGHT = "#2b211b"
BORDER = "#3d3129"
TEXT = "#f2e9d8"
MUTED = "#b3a48e"
BRASS = "#d9a441"
BRASS_HOVER = "#e8b95c"
BRASS_TEXT = "#1d150a"
LOG_BG = "#1a130f"

# The installed files that must be executable: the menu entry runs
# run.sh, and the installer itself must double-click.
EXECUTABLE_FILES = ("install.sh", "run.sh", "installer.py")

# System libraries the window toolkit needs, and the apt package
# that provides each one on Debian/Ubuntu. Only libraries the
# installer knows how to provide are listed: an unknown library
# gets a plain warning instead of a guessed package name.
QT_LIB_PACKAGES = {
    "libEGL.so": "libegl1",
    "libGL.so": "libgl1",
    "libxkbcommon.so": "libxkbcommon0",
    "libdbus-1.so": "libdbus-1-3",
    "libfontconfig.so": "libfontconfig1",
    "libglib-2.0.so": "libglib2.0-0",
    "libstdc++.so": "libstdc++6",
    "libgcc_s.so": "libgcc-s1",
}


class InstallCancelled(Exception):
    """The user pressed Cancel. Not an error: the GUI reports it
    plainly and the Install button stays available."""


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InstallCancelled()


def _qt_lib_package(lib_name):
    """The apt package for a missing library, or None when the
    installer does not know which package provides it."""
    base = lib_name.split(".so")[0] + ".so"
    return QT_LIB_PACKAGES.get(base)


def _display_path(path):
    """A path for the window: home shown as ~, so the default install
    path fits on one line instead of wrapping mid-word."""
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


class InstallError(Exception):
    def __init__(self, message, hint=""):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def plain_text(self):
        if self.hint:
            return f"{self.message} {self.hint}"
        return self.message


def source_dir():
    return Path(__file__).resolve().parent


def default_target():
    return Path.home() / ".local" / "share" / INSTALL_DIR_NAME


def run(cmd, cwd=None, timeout=300, env=None, cancel_event=None):
    if cancel_event is None:
        proc = subprocess.run(
            [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    # Cancellable run: poll the process so Cancel can kill it
    # instead of waiting out a minutes-long step.
    proc = subprocess.Popen(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env,
    )
    deadline = time.monotonic() + timeout
    while True:
        if cancel_event.is_set():
            proc.kill()
            proc.wait()
            raise InstallCancelled()
        try:
            out, err = proc.communicate(timeout=0.2)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            raise subprocess.TimeoutExpired(cmd, timeout)


def copy_tree(src, target, log):
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    def _ignore(_dir, names):
        skipped = []
        for name in names:
            if name in ("__pycache__", ".git", "venv", ".mypy_cache"):
                skipped.append(name)
            elif name.endswith((".pyc", ".zip")):
                skipped.append(name)
        return skipped

    shutil.copytree(src, target, dirs_exist_ok=True, ignore=_ignore)
    count = sum(1 for _ in target.rglob("*") if _.is_file())
    log(f"Copied {count} files to {target}.")


def set_installed_modes(target, log=None):
    """Make the installed scripts executable.

    The repo cannot be relied on to carry exec bits (a clone or a
    source archive may arrive 644), so the installer sets them on
    the installed copies: the menu entry runs run.sh, and the
    installer itself must double-click.
    """
    made = 0
    for name in EXECUTABLE_FILES:
        path = Path(target) / name
        if not path.exists():
            continue
        path.chmod(path.stat().st_mode | 0o111)
        made += 1
    if made and log is not None:
        log(f"Made {made} installed scripts executable.")


def install_system_deps(log, warnings):
    """Install ffmpeg and the Python venv package, if missing.

    Non-fatal: if the user says no to the permission window, or the
    install fails, the rest of the install goes on and the miss is
    reported as a warning at the end. The venv step below will still
    fail plainly if the venv package is the missing one, because the
    app cannot be installed without it.
    """
    missing = sysdeps.missing_packages()
    if not missing:
        log("System packages are already there (ffmpeg, Python venv).")
        return
    log("Installing missing system packages: "
        f"{sysdeps.describe_packages(missing)}. A permission window "
        "will ask for your password.")
    warnings.extend(sysdeps.install_missing_packages(progress_cb=log))


def make_venv(target, log, cancel_event=None):
    venv_dir = target / "venv"
    code, _out, _err = run(
        [sys.executable, "-m", "venv", str(venv_dir)], timeout=300,
        cancel_event=cancel_event)
    python = venv_dir / "bin" / "python"
    if code != 0 or not python.exists():
        # The stderr tail is not shown: on a missing venv package it
        # suggests a terminal command, and this installer never asks
        # the user to use a terminal.
        raise InstallError(
            "Could not make the app's Python home.",
            "The installer could not add the Python venv package. Run "
            "the installer again and allow the permission step when "
            "it asks.",
        )
    log("Made the app's Python home.")
    return python


def _pip_failure_hint(err):
    """The plain-words hint for a failed toolkit install, naming
    the failure family the log actually shows: a full disk is not
    an internet problem, and the headline must not send the user
    to check the wrong thing."""
    tail = err.strip().splitlines()[-1:] if err.strip() else []
    text = err or ""
    lowered = text.lower()
    if "no space left" in lowered or "errno 28" in lowered:
        family = ("The disk is full. Free some space, then run "
                  "the installer again.")
    elif "do not match the hashes" in lowered or "hash" in lowered \
            and "mismatch" in lowered:
        family = ("The download did not match what was expected, "
                  "so nothing was installed. Run the installer "
                  "again.")
    elif "permission denied" in lowered or "errno 13" in lowered:
        family = ("The installer was not allowed to write where "
                  "it needs to. Run the installer again and allow "
                  "the permission step when it asks.")
    elif any(word in lowered for word in (
            "connection", "name or service not known",
            "name resolution", "timed out", "timeout",
            "network is unreachable", "ssl", "certificate",
            "remote end closed", "download")):
        family = ("Check the internet connection and try again.")
    else:
        family = "Run the installer again and try again."
    if tail:
        return f"{family} Last lines: {tail}"
    return family


def install_deps(venv_python, log, cancel_event=None):
    log("Installing the window toolkit (PySide6). This takes a bit.")
    code, _out, err = run(
        [venv_python, "-m", "pip", "install", "--quiet",
         "PySide6>=6.6"],
        timeout=PIP_TIMEOUT_S, cancel_event=cancel_event,
    )
    if code != 0:
        raise InstallError(
            "Could not install the window toolkit.",
            _pip_failure_hint(err),
        )
    log("Window toolkit installed.")


RENDER_SCRIPT = """
import sys
from pathlib import Path
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
app = QGuiApplication(sys.argv)
svg = Path(sys.argv[1])
out_root = Path(sys.argv[2])
renderer = QSvgRenderer(str(svg))
if not renderer.isValid():
    print("SVG did not load", file=sys.stderr)
    sys.exit(1)
for size in (16, 32, 48, 64, 128, 256):
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(0x00000000)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    target = out_root / f"{size}x{size}" / "apps"
    target.mkdir(parents=True, exist_ok=True)
    image.save(str(target / "hicksfield.png"))
print("icons ok")
"""


def render_icons(venv_python, src, log, warnings, cancel_event=None):
    """Render the PNG icons. Non-fatal: a failed render only warns."""
    svg = src / "icons" / "hicksfield.svg"
    if not svg.exists():
        warnings.append("The icon source was missing, so the menu entry "
                        "has no picture.")
        return
    icons_root = Path.home() / ".local" / "share" / "icons" / "hicolor"
    try:
        code, _out, err = run(
            [venv_python, "-c", RENDER_SCRIPT, str(svg), str(icons_root)],
            timeout=120,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            cancel_event=cancel_event,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"Could not render the icons ({exc}). The menu "
                        "entry has no picture.")
        return
    if code != 0:
        warnings.append(
            "Could not render the icons "
            f"({err.strip().splitlines()[-1] if err.strip() else ''}). "
            "The menu entry has no picture.")
        return
    log("Icons rendered.")


def write_desktop(target, log):
    apps_dir = Path.home() / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    desktop = apps_dir / "hicksfield.desktop"
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=HicksField\n"
        "Comment=A free local film studio\n"
        f"Exec={target / 'run.sh'}\n"
        "Icon=hicksfield\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Video;Graphics;\n"
        "StartupWMClass=HicksField\n"
    )
    desktop.write_text(content, encoding="utf-8")
    log("Menu entry written.")


# A missing library name, without trailing punctuation: the old
# `lib\S+\.so\S*` captured the colon in "libEGL.so.1: cannot open",
# so the warning read "libEGL.so.1: is missing".
_MISSING_LIB_RE = re.compile(r"(lib\S+?\.so(?:\.\d+)*)")


def _check_window(target, venv_python, cancel_event=None):
    """Run the window toolkit smoke test. Returns ("ok", "") when the
    toolkit starts, ("failed", stderr) when it fails, and
    ("uncheckable", reason) when the check itself could not run."""
    smoke = (
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "print('qt ok')\n"
    )
    try:
        code, _out, err = run(
            [venv_python, "-c", smoke], cwd=target, timeout=120,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            cancel_event=cancel_event,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "uncheckable", str(exc)
    if code == 0:
        return "ok", ""
    return "failed", err or ""


def verify(target, venv_python, log, warnings, cancel_event=None):
    code, out, err = run(
        [venv_python, "-m", "hicksfield", "--version"],
        cwd=target, timeout=120, cancel_event=cancel_event,
    )
    if code != 0 or "HicksField" not in out:
        raise InstallError(
            "The install did not work: the app would not start.",
            f"Last lines: {(err or out).strip().splitlines()[-1:]}",
        )
    log(f"App starts: {out.strip()}")
    status, detail = _check_window(target, venv_python, cancel_event)
    if status == "ok":
        log("Window toolkit check passed.")
        return
    if status == "uncheckable":
        warnings.append(f"Could not check the window toolkit ({detail}).")
        return
    match = _MISSING_LIB_RE.search(detail)
    if match is None:
        warnings.append(
            "The app is installed, but the window check failed. "
            f"Last lines: {detail.strip().splitlines()[-1:]}")
        return
    lib = match.group(1)
    package = _qt_lib_package(lib)
    if package is None:
        # No known package for this library: say so plainly instead
        # of guessing a name or sending the user to a terminal.
        warnings.append(
            "The app is installed, but its window may not open on "
            "this computer: the system library "
            f"{lib} is missing. The installer does not know which "
            "system package provides it on this Linux version, so "
            "it could not install it.")
        return
    # A missing Qt system library is fixed in place: install it
    # (one permission window) and check the window once more.
    log(f"The window check needs the system library {lib}. "
        "Installing it now. A permission window will ask for your "
        "password.")
    error = sysdeps.install_packages([package], progress_cb=log)
    if error is not None:
        warnings.append(
            "The app is installed, but its window may not open on "
            "this computer: the system library "
            f"{lib} is missing, and it could not be installed: "
            f"{error}. Run the installer again and allow the "
            "permission step when it asks.")
        return
    status, detail = _check_window(target, venv_python, cancel_event)
    if status == "ok":
        log("Window toolkit check passed.")
        return
    match = _MISSING_LIB_RE.search(detail)
    if match is not None:
        lib = match.group(1)
    warnings.append(
        "The app is installed, but its window may not open on this "
        f"computer: the system library {lib} is still missing. Run "
        "the installer again and try again.")


def install(target, log, progress, cancel_event=None):
    """Run the whole install. Returns a list of warnings (possibly
    empty). Raises InstallError on failure, InstallCancelled when
    the user cancels."""
    warnings = []
    src = source_dir()
    progress(0.05)
    install_system_deps(log, warnings)
    _check_cancel(cancel_event)
    progress(0.1)
    copy_tree(src, target, log)
    set_installed_modes(target, log)
    _check_cancel(cancel_event)
    progress(0.25)
    venv_python = make_venv(target, log, cancel_event)
    progress(0.3)
    install_deps(venv_python, log, cancel_event)
    progress(0.7)
    render_icons(venv_python, src, log, warnings, cancel_event)
    _check_cancel(cancel_event)
    progress(0.85)
    write_desktop(target, log)
    verify(target, venv_python, log, warnings, cancel_event)
    progress(1.0)
    return warnings


def run_text(target):
    def _log(message):
        print(message, flush=True)

    def _progress(fraction):
        print(f"[{fraction * 100:.0f}%]", flush=True)

    print(f"Installing HicksField into {target}", flush=True)
    try:
        warnings = install(target, _log, _progress)
    except InstallError as exc:
        print(f"FAILED: {exc.plain_text()}", file=sys.stderr, flush=True)
        return 1
    for warning in warnings:
        print(f"WARNING: {warning}", flush=True)
    print("Installed. Find HicksField in the app menu.", flush=True)
    return 0


def run_gui(target):
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    messages = queue.Queue()
    cancel_event = threading.Event()
    # Mutable holder: True while the worker is installing.
    running = {"installing": False}

    def _log(message):
        messages.put(("log", message))

    def _progress(fraction):
        messages.put(("progress", fraction))

    def _worker():
        try:
            warnings = install(target, _log, _progress,
                               cancel_event=cancel_event)
        except InstallCancelled:
            messages.put(("cancelled", None))
            return
        except InstallError as exc:
            messages.put(("failed", exc.plain_text()))
            return
        except Exception as exc:  # noqa: BLE001 - shown to the user
            messages.put(("failed", str(exc)))
            return
        messages.put(("done", warnings))

    root = tk.Tk()
    root.title("Install HicksField")
    root.geometry("560x440")
    root.resizable(False, False)
    root.configure(background=BG)

    # Midnight Cinema: dark room, warm projector light, brass.
    # The clam theme takes colours predictably; without it the
    # defaults show and the window still works.
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Title.TLabel", background=BG, foreground=BRASS,
                    font=("TkDefaultFont", 18, "bold"))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("TButton", background=PANEL_LIGHT, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=PANEL_LIGHT,
                    darkcolor=PANEL_LIGHT, padding=6)
    style.map("TButton",
              background=[("active", BORDER)],
              foreground=[("disabled", MUTED)])
    style.configure("Brass.TButton", background=BRASS,
                    foreground=BRASS_TEXT, bordercolor=BRASS,
                    lightcolor=BRASS, darkcolor=BRASS, padding=6)
    style.map("Brass.TButton",
              background=[("active", BRASS_HOVER),
                          ("disabled", PANEL_LIGHT)],
              foreground=[("disabled", MUTED)])
    style.configure("TProgressbar", background=BRASS,
                    troughcolor=PANEL_LIGHT, bordercolor=BORDER,
                    lightcolor=BRASS, darkcolor=BRASS)

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(frame, text="HicksField",
              style="Title.TLabel").pack(anchor=tk.W)
    ttk.Label(frame, text="A free local film studio",
              style="Muted.TLabel").pack(anchor=tk.W)
    ttk.Label(frame, text=f"Installs into {_display_path(target)}",
              style="Muted.TLabel",
              wraplength=520).pack(anchor=tk.W, pady=(4, 10))
    bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
    bar.pack(fill=tk.X)
    log_box = scrolledtext.ScrolledText(
        frame, height=12, state=tk.DISABLED, bg=LOG_BG, fg=TEXT,
        insertbackground=TEXT, selectbackground=BORDER,
        font="TkFixedFont", relief=tk.FLAT, wrap=tk.WORD,
        highlightthickness=1, highlightbackground=BORDER)
    log_box.pack(fill=tk.BOTH, expand=True, pady=10)
    buttons = ttk.Frame(frame)
    buttons.pack(fill=tk.X)
    status = ttk.Label(buttons, text="Ready.")
    status.pack(side=tk.LEFT)

    def _append(message):
        log_box.configure(state=tk.NORMAL)
        log_box.insert(tk.END, message + "\n")
        log_box.see(tk.END)
        log_box.configure(state=tk.DISABLED)

    def _finish():
        running["installing"] = False
        cancel_button.configure(state=tk.DISABLED)
        close_button.configure(state=tk.NORMAL)

    def _poll():
        try:
            while True:
                kind, payload = messages.get_nowait()
                if kind == "log":
                    _append(payload)
                elif kind == "progress":
                    bar["value"] = payload * 100
                elif kind == "done":
                    bar["value"] = 100
                    status.configure(text="Installed.")
                    for warning in payload:
                        _append(f"WARNING: {warning}")
                    _append("Installed. Find HicksField in the app menu.")
                    install_button.configure(state=tk.DISABLED)
                    _finish()
                    return
                elif kind == "cancelled":
                    status.configure(text="Install cancelled.")
                    _append("Install cancelled. Press Install to try "
                            "again.")
                    install_button.configure(state=tk.NORMAL)
                    _finish()
                    return
                elif kind == "failed":
                    status.configure(text="Install failed.")
                    _append(f"FAILED: {payload}")
                    install_button.configure(state=tk.NORMAL)
                    _finish()
                    messagebox.showerror("Install failed", payload)
                    return
        except queue.Empty:
            pass
        root.after(100, _poll)

    def _start():
        cancel_event.clear()
        running["installing"] = True
        install_button.configure(state=tk.DISABLED)
        cancel_button.configure(state=tk.NORMAL)
        close_button.configure(state=tk.DISABLED)
        status.configure(text="Installing.")
        threading.Thread(target=_worker, daemon=True).start()
        root.after(100, _poll)

    def _cancel():
        # The worker notices between steps and inside long commands,
        # then reports back through the queue: the window never
        # blocks waiting for it.
        cancel_event.set()
        cancel_button.configure(state=tk.DISABLED)
        status.configure(text="Cancelling.")

    def _close():
        # Closing mid-install cancels instead of leaving a half
        # installed tree behind.
        if running["installing"]:
            _cancel()
            return
        root.destroy()

    install_button = ttk.Button(buttons, text="Install",
                                style="Brass.TButton", command=_start)
    install_button.pack(side=tk.RIGHT, padx=(6, 0))
    cancel_button = ttk.Button(buttons, text="Cancel", command=_cancel)
    cancel_button.pack(side=tk.RIGHT, padx=(6, 0))
    cancel_button.configure(state=tk.DISABLED)
    close_button = ttk.Button(buttons, text="Close", command=_close)
    close_button.pack(side=tk.RIGHT)
    root.protocol("WM_DELETE_WINDOW", _close)
    root.mainloop()
    return 0


def main(argv):
    parser = argparse.ArgumentParser(
        description="Install HicksField.")
    parser.add_argument("--text", action="store_true",
                        help="Install in the terminal, no window.")
    parser.add_argument("--target", default=None,
                        help="Where to install (default: "
                        "~/.local/share/hicksfield-app).")
    args = parser.parse_args(argv)
    target = Path(args.target) if args.target else default_target()
    if args.text:
        return run_text(target)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("The installer window needs tkinter, which is missing.",
              file=sys.stderr)
        print("Run the terminal install instead: bash install.sh",
              file=sys.stderr)
        return 1
    return run_gui(target)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
