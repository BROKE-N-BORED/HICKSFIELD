"""Measure the PC, quietly, and pick settings that fit it.

The backend configures itself from these numbers (U35): worker threads stay
low enough that the desktop stays usable, and stills generation checks RAM
before it starts. The user never sets any of this by hand.
"""

import os
import shutil

# Worker threads for heavy jobs. 2 on his 2-core PC leaves the desktop
# usable while a job runs (U25).
MAX_WORKER_THREADS = 2


def parse_meminfo(text):
    """Parse /proc/meminfo text into {name: kilobytes}."""
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            out[name.strip()] = int(parts[0])
        except ValueError:
            continue
    return out


def read_meminfo(path="/proc/meminfo"):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return parse_meminfo(handle.read())
    except OSError:
        return {}


def mem_total_mb(path="/proc/meminfo"):
    info = read_meminfo(path)
    return info.get("MemTotal", 0) // 1024


def mem_available_mb(path="/proc/meminfo"):
    info = read_meminfo(path)
    return info.get("MemAvailable", 0) // 1024


def cpu_count():
    return os.cpu_count() or 1


def disk_free_mb(path):
    try:
        usage = shutil.disk_usage(path)
        return usage.free // (1024 * 1024)
    except OSError:
        return 0


def worker_threads():
    """Threads for heavy work: capped so the desktop stays usable."""
    return max(1, min(MAX_WORKER_THREADS, cpu_count()))


def ram_check(required_mb, available_mb=None):
    """Check free RAM against a requirement.

    Returns (ok, available_mb, message). The message is plain words with
    real numbers, for the confirm dialog.
    """
    if available_mb is None:
        available_mb = mem_available_mb()
    if available_mb >= required_mb:
        return True, available_mb, (
            f"{available_mb} MB free, {required_mb} MB needed."
        )
    return False, available_mb, (
        f"Only {available_mb} MB of RAM is free, and this step needs about "
        f"{required_mb} MB. It may run slowly or fail."
    )


def machine_summary():
    total = mem_total_mb()
    available = mem_available_mb()
    free_disk = disk_free_mb(os.path.expanduser("~"))
    return (
        f"RAM: {total} MB total, {available} MB free. "
        f"CPUs: {cpu_count()}. "
        f"Disk free in home: {free_disk} MB. "
        f"Worker threads: {worker_threads()}."
    )
