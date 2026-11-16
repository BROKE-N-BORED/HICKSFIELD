"""System packages HicksField needs: ffmpeg and the Python venv package.

The installer runs this before anything else, and the app can offer the
same fix with one click later: the install runs in a background thread
and reports progress, success, or a plain-words failure through
callbacks, so no window ever blocks on it.

Permission comes from pkexec, which asks for the password in a window,
not a terminal. If the user says no, or the system cannot ask, the
install is declined plainly and the caller goes on without it: nothing
here ever blocks an install or a job on a declined permission step.

Only the standard library is used, so the installer can import this
before the app's own Python home exists.
"""

import os
import shutil
import subprocess
import sys
import threading

# pkexec exit codes: 126 means the user dismissed the password window,
# 127 means the permission step itself failed. Any other nonzero code
# comes from apt-get.
PKEXEC_DECLINED = 126
PKEXEC_FAILED = 127
INSTALL_TIMEOUT_S = 900


def ffmpeg_installed():
    return shutil.which("ffmpeg") is not None


def venv_package_name():
    """The apt package that provides the venv module for this Python."""
    return f"python3.{sys.version_info.minor}-venv"


def venv_available():
    """Whether this Python can make a venv (python3-venv installed)."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import ensurepip"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def missing_packages():
    """Apt package names still needed, in install order."""
    missing = []
    if not ffmpeg_installed():
        missing.append("ffmpeg")
    if not venv_available():
        missing.append(venv_package_name())
    return missing


def describe_packages(packages):
    """Plain-words names for a package list, for messages."""
    names = []
    for package in packages:
        if package == "ffmpeg":
            names.append("ffmpeg")
        elif package.endswith("-venv"):
            names.append("the Python venv package")
        else:
            names.append(package)
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def install_packages(packages, progress_cb=None):
    """Install apt packages, asking permission in a window.

    Blocking: the caller runs this on a worker thread (see
    install_packages_async). Returns None on success, or a
    plain-words error message when the install was declined or
    failed. Never raises for a declined or failed install.
    """
    packages = list(packages)
    if not packages:
        return None
    if progress_cb is not None:
        progress_cb(
            "Asking for permission to install "
            f"{describe_packages(packages)}.")
    command = ["pkexec", "apt-get", "install", "-y"] + packages
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True,
            timeout=INSTALL_TIMEOUT_S,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
    except FileNotFoundError:
        return ("this computer could not ask for permission "
                "(the permission helper is missing)")
    except subprocess.TimeoutExpired:
        return "the install took too long"
    if proc.returncode == 0:
        if progress_cb is not None:
            progress_cb(f"Installed {describe_packages(packages)}.")
        return None
    if proc.returncode in (PKEXEC_DECLINED, PKEXEC_FAILED):
        return "the permission step was cancelled"
    detail = ""
    output = (proc.stderr or proc.stdout or "").strip()
    if output:
        detail = f" ({output.splitlines()[-1][:200]})"
    return f"the install did not finish{detail}"


def install_packages_async(packages, progress_cb=None, done_cb=None,
                           error_cb=None):
    """Install apt packages in a background thread.

    progress_cb(message) reports plain-words progress, done_cb() runs
    on success, error_cb(message) runs when the install was declined
    or failed. Returns the thread, so the caller can wait on it.
    """
    def _run():
        error = install_packages(packages, progress_cb=progress_cb)
        if error is None:
            if done_cb is not None:
                done_cb()
        elif error_cb is not None:
            error_cb(error)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def install_missing_packages(progress_cb=None):
    """Install whatever system packages are missing.

    Returns a list of plain-words warnings (empty when everything was
    already there or the install worked). A declined or failed install
    is a warning, never an exception: the caller goes on without the
    missing packages.
    """
    missing = missing_packages()
    if not missing:
        return []
    error = install_packages(missing, progress_cb=progress_cb)
    if error is None:
        return []
    return [
        f"{describe_packages(missing)} could not be installed: {error}. "
        "Run the HicksField installer again and allow the permission "
        "step when it asks."
    ]
