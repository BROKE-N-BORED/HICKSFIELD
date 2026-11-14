"""Resumable downloads with progress and cancel.

Downloads go to a `.part` file first. If the download is interrupted, the
next run picks up where it left off (U24). A download that finishes is
checked against its expected size when one is known, then moved into place.
"""

import os
import urllib.error
import urllib.request
from pathlib import Path

from hicksfield.errors import CancelledError, DownloadError

CHUNK_SIZE = 1024 * 1024


def download(url, dest, expected_size=None, progress_cb=None,
             cancel_event=None, timeout=60):
    """Download `url` to `dest` (a Path). Resumes a partial download.

    progress_cb(downloaded, total_or_None) is called after each chunk.
    If cancel_event is set, raises CancelledError and keeps the partial
    file for a later resume. Returns the destination Path.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and expected_size is not None:
        if dest.stat().st_size == expected_size:
            if progress_cb is not None:
                progress_cb(expected_size, expected_size)
            return dest
    part = dest.with_suffix(dest.suffix + ".part")
    start = part.stat().st_size if part.exists() else 0
    request = urllib.request.Request(url)
    if start:
        request.add_header("Range", f"bytes={start}-")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if start and exc.code == 416:
            # Server says the range is past the end: the part file is
            # already the whole file. Verify and move it into place.
            return _finish_part(part, dest, expected_size, progress_cb)
        raise DownloadError(
            f"Could not download from {url}: HTTP {exc.code}.",
            "Check the internet connection and try again.",
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DownloadError(
            f"Could not download from {url}: {exc}.",
            "Check the internet connection and try again.",
        ) from exc
    with response:
        if start and response.status != 206:
            # Server ignored the range: start over.
            start = 0
            if part.exists():
                part.unlink()
        total = response.headers.get("Content-Length")
        total = int(total) + start if total is not None else None
        if expected_size is not None:
            total = expected_size
        downloaded = start
        if progress_cb is not None:
            progress_cb(downloaded, total)
        mode = "ab" if start else "wb"
        try:
            with open(part, mode) as handle:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise CancelledError()
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None:
                        progress_cb(downloaded, total)
        except CancelledError:
            raise
        except OSError as exc:
            raise DownloadError(
                f"Could not save the download: {exc}.",
                "Check there is enough free disk space and try again.",
            ) from exc
    return _finish_part(part, dest, expected_size, progress_cb)


def _finish_part(part, dest, expected_size, progress_cb):
    if expected_size is not None:
        actual = part.stat().st_size if part.exists() else 0
        if actual != expected_size:
            raise DownloadError(
                f"The download finished at {actual} bytes but should be "
                f"{expected_size} bytes.",
                "Try the download again. It picks up where it left off.",
            )
    os.replace(part, dest)
    size = dest.stat().st_size
    if progress_cb is not None:
        progress_cb(size, expected_size if expected_size is not None else size)
    return dest


def format_bytes(num):
    """Plain-words byte size: 512 B, 3.2 MB, 2.1 GB."""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
