"""Voice: a local offline voice reads the narration.

Piper turns text into speech on CPU, with no network and no account.
The voice model is downloaded once, from inside the app, with progress.
"""

import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from hicksfield import paths
from hicksfield.downloader import download
from hicksfield.errors import (
    CancelledError,
    EngineNotReadyError,
    HicksFieldError,
)
from hicksfield.steps import run_steps

VOICE_ID = "en_US-lessac-medium"
VOICE_FILES = [
    "en_US-lessac-medium.onnx",
    "en_US-lessac-medium.onnx.json",
]
VOICE_BASE_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "en/en_US/lessac/medium/"
)
PIPER_PACKAGE = "piper-tts"
SYNTH_TIMEOUT_S = 300


def find_piper():
    """The piper program: next to this Python first, then PATH.

    The installer puts piper-tts in the app venv, whose bin folder is
    next to sys.executable, not on PATH.
    """
    override = os.environ.get("HICKSFIELD_PIPER")
    if override and Path(override).exists():
        return Path(override)
    beside = Path(sys.executable).parent / "piper"
    if beside.exists():
        return beside
    found = shutil.which("piper")
    return Path(found) if found else None


def voice_dir():
    override = os.environ.get("HICKSFIELD_VOICE_DIR")
    if override:
        return Path(override)
    return paths.voices_dir() / VOICE_ID


def voice_files():
    return [(name, VOICE_BASE_URL + name) for name in VOICE_FILES]


def voice_status():
    have = [name for name, _url in voice_files()
            if (voice_dir() / name).exists()
            and (voice_dir() / name).stat().st_size > 0]
    return len(have), len(VOICE_FILES)


def is_ready():
    """Return (ready, plain-words status)."""
    if find_piper() is None:
        return False, "Not set up yet: the voice program is missing."
    have, total = voice_status()
    if have < total:
        return False, (
            f"Set up halfway: the voice is {have} of {total} files "
            "downloaded."
        )
    return True, "Ready."


def setup(report=None, cancel_event=None):
    steps = [
        (30, "Getting the voice program", _setup_piper),
        (70, "Downloading the voice", _setup_voice_files),
    ]
    run_steps(steps, report=report, cancel_event=cancel_event)


def _setup_piper(progress, cancel_event):
    if find_piper() is not None:
        progress(1.0, "already there")
        return
    progress(None, "installing")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", PIPER_PACKAGE],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0 or find_piper() is None:
        raise HicksFieldError(
            "Could not install the voice program.",
            "Check the internet connection and try again.",
        )
    progress(1.0, "")


def _setup_voice_files(progress, cancel_event):
    have, total = voice_status()
    if have >= total:
        progress(1.0, "already there")
        return
    files = voice_files()
    for index, (name, url) in enumerate(files):
        dest = voice_dir() / name
        if dest.exists() and dest.stat().st_size > 0:
            progress((index + 1) / len(files), name)
            continue

        def _cb(done, total_bytes, _index=index):
            if total_bytes:
                base = _index / len(files)
                progress(base + (done / total_bytes) / len(files), name)

        download(url, dest, progress_cb=_cb, cancel_event=cancel_event)
    have, total = voice_status()
    if have < total:
        raise HicksFieldError(
            "The voice is still missing files.",
            "Try the download again. It picks up where it left off.",
        )
    progress(1.0, "")


def wav_duration(path):
    """Length of a WAV file in seconds, or None when unreadable."""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            if rate <= 0:
                return None
            return frames / rate
    except (wave.Error, OSError):
        return None


def synthesize(text, out_path, cancel_event=None):
    """Speak `text` into a WAV file. Returns (path, duration_seconds)."""
    ready, status = is_ready()
    if not ready:
        raise EngineNotReadyError(
            f"The voice is not ready: {status}",
            "Set it up from the Voice window first.",
        )
    piper = find_piper()
    model = voice_dir() / VOICE_FILES[0]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            [str(piper), "--model", str(model),
             "--output_file", str(out_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
    except OSError as exc:
        raise HicksFieldError(
            f"Could not start the voice program: {exc}.",
            "Try setting up the voice again from the Voice window.",
        ) from exc
    try:
        _out, err = proc.communicate(input=text, timeout=SYNTH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise HicksFieldError(
            "The voice took too long to speak.",
            "Try a shorter line.",
        )
    if cancel_event is not None and cancel_event.is_set():
        out_path.unlink(missing_ok=True)
        raise CancelledError()
    if proc.returncode != 0:
        raise HicksFieldError(
            "The voice could not speak that line.",
            f"Last words from the voice: {err.strip()[-300:]}"
            if err.strip() else "Try again.",
        )
    duration = wav_duration(out_path)
    if duration is None or duration <= 0:
        raise HicksFieldError(
            "The voice finished but the sound file is empty.",
            "Try again.",
        )
    return out_path, duration
