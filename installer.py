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
from pathlib import Path

# sysdeps uses only the standard library, so it is safe to import
# before the app's own Python home exists.
from hicksfield import sysdeps

APP_NAME = "HicksField"
INSTALL_DIR_NAME = "hicksfield-app"
ICON_SIZES = (16, 32, 48, 64, 128, 256)
PIP_TIMEOUT_S = 900


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


def run(cmd, cwd=None, timeout=300, env=None):
    proc = subprocess.run(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


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


def make_venv(target, log):
    venv_dir = target / "venv"
    code, _out, _err = run(
        [sys.executable, "-m", "venv", str(venv_dir)], timeout=300)
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


def install_deps(venv_python, log):
    log("Installing the window toolkit (PySide6). This takes a bit.")
    code, _out, err = run(
        [venv_python, "-m", "pip", "install", "--quiet",
         "PySide6>=6.6"],
        timeout=PIP_TIMEOUT_S,
    )
    if code != 0:
        raise InstallError(
            "Could not install the window toolkit.",
            "Check the internet connection and try again. "
            f"Last lines: {err.strip().splitlines()[-1:]}",
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


def render_icons(venv_python, src, log, warnings):
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


_MISSING_LIB_RE = re.compile(r"(lib\S+\.so\S*)")


def verify(target, venv_python, log, warnings):
    code, out, err = run(
        [venv_python, "-m", "hicksfield", "--version"],
        cwd=target, timeout=120,
    )
    if code != 0 or "HicksField" not in out:
        raise InstallError(
            "The install did not work: the app would not start.",
            f"Last lines: {(err or out).strip().splitlines()[-1:]}",
        )
    log(f"App starts: {out.strip()}")
    smoke = (
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "print('qt ok')\n"
    )
    try:
        code, _out, err = run(
            [venv_python, "-c", smoke], cwd=target, timeout=120,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"Could not check the window toolkit ({exc}).")
        return
    if code != 0:
        match = _MISSING_LIB_RE.search(err or "")
        if match:
            warnings.append(
                "The app is installed, but its window may not open on "
                "this computer: the system library "
                f"{match.group(1)} is missing. Install the Qt system "
                "libraries for your Linux version and try again.")
        else:
            warnings.append(
                "The app is installed, but the window check failed. "
                f"Last lines: {err.strip().splitlines()[-1:]}")
        return
    log("Window toolkit check passed.")


def install(target, log, progress):
    """Run the whole install. Returns a list of warnings (possibly
    empty). Raises InstallError on failure."""
    warnings = []
    src = source_dir()
    progress(0.05)
    install_system_deps(log, warnings)
    progress(0.1)
    copy_tree(src, target, log)
    progress(0.25)
    venv_python = make_venv(target, log)
    progress(0.3)
    install_deps(venv_python, log)
    progress(0.7)
    render_icons(venv_python, src, log, warnings)
    progress(0.85)
    write_desktop(target, log)
    verify(target, venv_python, log, warnings)
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

    def _log(message):
        messages.put(("log", message))

    def _progress(fraction):
        messages.put(("progress", fraction))

    def _worker():
        try:
            warnings = install(target, _log, _progress)
        except InstallError as exc:
            messages.put(("failed", exc.plain_text()))
            return
        except Exception as exc:  # noqa: BLE001 - shown to the user
            messages.put(("failed", str(exc)))
            return
        messages.put(("done", warnings))

    root = tk.Tk()
    root.title("Install HicksField")
    root.geometry("560x420")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(frame, text="HicksField",
              font=("TkDefaultFont", 18, "bold")).pack(anchor=tk.W)
    ttk.Label(frame, text=f"Installs into {target}",
              wraplength=520).pack(anchor=tk.W, pady=(4, 10))
    bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
    bar.pack(fill=tk.X)
    log_box = scrolledtext.ScrolledText(frame, height=12, state=tk.DISABLED)
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
                    close_button.configure(state=tk.NORMAL)
                    return
                elif kind == "failed":
                    status.configure(text="Install failed.")
                    _append(f"FAILED: {payload}")
                    install_button.configure(state=tk.NORMAL)
                    close_button.configure(state=tk.NORMAL)
                    messagebox.showerror("Install failed", payload)
                    return
        except queue.Empty:
            pass
        root.after(100, _poll)

    def _start():
        install_button.configure(state=tk.DISABLED)
        close_button.configure(state=tk.DISABLED)
        status.configure(text="Installing.")
        threading.Thread(target=_worker, daemon=True).start()
        root.after(100, _poll)

    install_button = ttk.Button(buttons, text="Install",
                                command=_start)
    install_button.pack(side=tk.RIGHT, padx=(6, 0))
    close_button = ttk.Button(buttons, text="Close", command=root.destroy)
    close_button.pack(side=tk.RIGHT)
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
