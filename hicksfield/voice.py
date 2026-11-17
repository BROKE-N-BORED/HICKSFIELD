"""Voice: a local offline voice reads the narration.

Piper turns text into speech on CPU, with no network and no account.
The voice model is downloaded once, from inside the app, with progress.
"""

import hashlib
import logging
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
    DownloadError,
    EngineNotReadyError,
    HicksFieldError,
)
from hicksfield.steps import run_logged, run_steps, run_timed_out

logger = logging.getLogger("hicksfield.voice")

VOICE_ID = "en_US-lessac-medium"
# The voices the Voice window offers. Each entry: a short label, a
# plain-words description of what it sounds like, whether it is the
# recommended one, where its files live, and the files themselves
# with their published sizes in bytes and SHA-256 checksums where
# published. Sizes and checksums are taken from the published voice
# files (rhasspy/piper-voices on HuggingFace, checked 2026-11-15):
# a download that finishes at any other size, or with another
# checksum, is refused, so a truncated download fails here instead
# of confusingly at speak time. No checksum is published for the
# small settings files, so they are checked by size only.
VOICES = {
    "en_US-lessac-medium": {
        "label": "Lessac",
        "description": "A clear American voice.",
        "recommended": True,
        "base_url": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "en/en_US/lessac/medium/"
        ),
        "files": {
            "en_US-lessac-medium.onnx": {
                "size": 63201294,
                "sha256": (
                    "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16"
                    "b1b46eeaaf019f"
                ),
            },
            "en_US-lessac-medium.onnx.json": {
                "size": 4885,
                "sha256": None,
            },
        },
    },
    "en_US-ryan-high": {
        "label": "Ryan",
        "description": "A clear American voice that sounds the "
                       "sharpest. A bigger download.",
        "recommended": False,
        "base_url": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "en/en_US/ryan/high/"
        ),
        "files": {
            "en_US-ryan-high.onnx": {
                "size": 120786792,
                "sha256": (
                    "b3990d7606e183ec8dbfba70a4607074f162de1a0c412e018"
                    "0d1ff60bb154eca"
                ),
            },
            "en_US-ryan-high.onnx.json": {
                "size": 4166,
                "sha256": None,
            },
        },
    },
    "en_GB-alan-medium": {
        "label": "Alan",
        "description": "A British voice.",
        "recommended": False,
        "base_url": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "en/en_GB/alan/medium/"
        ),
        "files": {
            "en_GB-alan-medium.onnx": {
                "size": 63201294,
                "sha256": (
                    "0a309668932205e762801f1efc2736cd4b0120329622adf6"
                    "2be09e56339d3330"
                ),
            },
            "en_GB-alan-medium.onnx.json": {
                "size": 4888,
                "sha256": None,
            },
        },
    },
}
# The default voice's files, sizes, checksum, and download address,
# kept as plain names for the callers that only ever meant this one.
VOICE_FILES = list(VOICES[VOICE_ID]["files"])
VOICE_FILE_SIZES = {
    name: info["size"]
    for name, info in VOICES[VOICE_ID]["files"].items()
}
VOICE_MODEL_SHA256 = VOICES[VOICE_ID]["files"][VOICE_FILES[0]]["sha256"]
VOICE_BASE_URL = VOICES[VOICE_ID]["base_url"]
PIPER_PACKAGE = "piper-tts"
SYNTH_TIMEOUT_S = 300

# Checksum results cached per run, keyed on path, size, and mtime, so
# a status check does not re-hash the 60 MB model every time.
_CHECKSUM_CACHE = {}


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


def voice_dir(voice_id=VOICE_ID):
    override = os.environ.get("HICKSFIELD_VOICE_DIR")
    if override:
        # The override names one voice's folder (used by tests): it
        # is the default voice's folder, and other voices sit beside
        # it under their own names.
        if voice_id == VOICE_ID:
            return Path(override)
        return Path(override).parent / voice_id
    return paths.voices_dir() / voice_id


def _voice_entry(voice_id):
    """The VOICES entry for `voice_id`, or None when unknown."""
    return VOICES.get(voice_id)


def _voice_file_info(voice_id, name):
    entry = _voice_entry(voice_id)
    if entry is None:
        return None
    return entry["files"].get(name)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checksum_ok(path, expected_sha256):
    try:
        stat = path.stat()
    except OSError:
        return False
    key = (str(path), stat.st_size, stat.st_mtime_ns, expected_sha256)
    if key in _CHECKSUM_CACHE:
        return _CHECKSUM_CACHE[key]
    try:
        ok = _sha256_file(path) == expected_sha256
    except OSError:
        ok = False
    _CHECKSUM_CACHE[key] = ok
    return ok


def voice_file_ok(name, voice_id=VOICE_ID):
    """Whether a voice file on disk is whole: right size, and for the
    model file, the right checksum too."""
    path = voice_dir(voice_id) / name
    if not path.exists():
        return False
    size = path.stat().st_size
    if size <= 0:
        return False
    info = _voice_file_info(voice_id, name)
    if info is None:
        return False
    expected = info.get("size")
    if expected is not None and size != expected:
        return False
    checksum = info.get("sha256")
    if checksum is not None:
        return _checksum_ok(path, checksum)
    return True


def voice_files(voice_id=VOICE_ID):
    entry = _voice_entry(voice_id)
    if entry is None:
        return []
    return [(name, entry["base_url"] + name)
            for name in entry["files"]]


def voice_status(voice_id=VOICE_ID):
    have = [name for name, _url in voice_files(voice_id)
            if voice_file_ok(name, voice_id)]
    return len(have), len(voice_files(voice_id))


def is_ready(voice_id=VOICE_ID):
    """Return (ready, plain-words status)."""
    if _voice_entry(voice_id) is None:
        return False, "That voice is not one HicksField knows."
    if find_piper() is None:
        return False, "Not set up yet: the voice program is missing."
    have, total = voice_status(voice_id)
    if have < total:
        return False, (
            f"Set up halfway: the voice is {have} of {total} files "
            "downloaded."
        )
    return True, "Ready."


def setup(report=None, cancel_event=None, voice_id=VOICE_ID):
    steps = [
        (30, "Getting the voice program", _setup_piper),
        (70, "Downloading the voice",
         lambda progress, cancel: _setup_voice_files(
             progress, cancel, voice_id)),
    ]
    run_steps(steps, report=report, cancel_event=cancel_event)


def _setup_piper(progress, cancel_event):
    if find_piper() is not None:
        progress(1.0, "already there")
        return
    progress(None, "installing")
    # run_logged, not subprocess.run: a pip install can take minutes,
    # and Cancel must land while it runs (the child is killed).
    code, tail = run_logged(
        [sys.executable, "-m", "pip", "install", "--quiet", PIPER_PACKAGE],
        timeout_s=900, cancel_event=cancel_event,
    )
    if code != 0 or find_piper() is None:
        if run_timed_out(code, tail):
            raise HicksFieldError(
                "Installing the voice program took too long and was "
                "stopped.",
                "Check the internet connection and try again.",
            )
        raise HicksFieldError(
            "Could not install the voice program.",
            "Check the internet connection and try again.",
        )
    progress(1.0, "")


def _setup_voice_files(progress, cancel_event, voice_id=VOICE_ID):
    if _voice_entry(voice_id) is None:
        raise HicksFieldError(
            "That voice is not one HicksField knows.",
            "Pick another voice in the Voice window.",
        )
    have, total = voice_status(voice_id)
    if have >= total:
        progress(1.0, "already there")
        return
    files = voice_files(voice_id)
    for index, (name, url) in enumerate(files):
        dest = voice_dir(voice_id) / name
        if voice_file_ok(name, voice_id):
            progress((index + 1) / len(files), name)
            continue

        def _cb(done, total_bytes, _index=index):
            if total_bytes:
                base = _index / len(files)
                progress(base + (done / total_bytes) / len(files), name)

        # A file that is here but not whole (wrong size, or the model
        # file with a wrong checksum) must go first: the downloader
        # takes a right-sized file as done, so a corrupt model would
        # otherwise fail its checksum check on every retry.
        if dest.exists():
            dest.unlink()
        info = _voice_file_info(voice_id, name)
        download(url, dest, expected_size=info["size"] if info else None,
                 progress_cb=_cb, cancel_event=cancel_event)
        if not voice_file_ok(name, voice_id):
            raise DownloadError(
                "The voice file downloaded but does not match its "
                "checksum.",
                "Try the download again.",
            )
    have, total = voice_status(voice_id)
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


# Silence for a scene with no spoken line: one second, at the rate
# the voice itself uses.
SILENCE_SECONDS = 1.0
SILENCE_RATE = 22050


def _write_silence(out_path, seconds=SILENCE_SECONDS):
    """A silent WAV of `seconds`, for a scene with no spoken line.
    Returns (path, duration_seconds)."""
    frames = int(SILENCE_RATE * seconds)
    with wave.open(str(out_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SILENCE_RATE)
        handle.writeframes(b"\x00\x00" * frames)
    return Path(out_path), seconds


def synthesize(text, out_path, cancel_event=None,
               voice_id=VOICE_ID):
    """Speak `text` into a WAV file with the voice `voice_id`.
    Returns (path, duration_seconds). A line with no words is
    silence: a short silent file, with no voice needed, so a scene
    left quiet on purpose can never break the job."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not text or not text.strip():
        # HowTo promises a scene with no spoken line stays silent:
        # the voice program is never started for an empty line (real
        # piper exits 1 on empty input), and no setup is needed.
        logger.info("Empty line: silence")
        return _write_silence(out_path)
    ready, status = is_ready(voice_id)
    if not ready:
        raise EngineNotReadyError(
            f"The voice is not ready: {status}",
            "Set it up from the Voice window first.",
        )
    logger.info("Speaking a line")
    piper = find_piper()
    model = voice_dir(voice_id) / voice_files(voice_id)[0][0]
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
        # A traceback from the voice program is never shown to the
        # user: it reads like a programmer's error, not a fix.
        if err.strip() and "Traceback" not in err:
            hint = f"Last words from the voice: {err.strip()[-300:]}"
        else:
            hint = "Try again."
        raise HicksFieldError(
            "The voice could not speak that line.",
            hint,
        )
    duration = wav_duration(out_path)
    if duration is None or duration <= 0:
        raise HicksFieldError(
            "The voice finished but the sound file is empty.",
            "Try again.",
        )
    logger.info("Line spoken (%.1f seconds)", duration)
    return out_path, duration
