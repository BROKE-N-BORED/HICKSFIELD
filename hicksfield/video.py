"""Motion and assembly with ffmpeg.

Each still becomes a short moving clip (a slow push-in or pan), the clips
are cut together with the spoken lines, captions go on top, and the
finished film is an MP4. All real ffmpeg work; nothing here pretends.

Progress comes from ffmpeg itself: the progress file reports the encoded
time, so the bar shows real work, not a guess.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from hicksfield.errors import CancelledError, FFmpegMissingError, HicksFieldError

CLIP_WIDTH = 1280
CLIP_HEIGHT = 720
CLIP_FPS = 30
ZOOM_IN_RATE = 0.0015
ZOOM_MAX = 1.5
PAN_ZOOM = 1.25
PRESCALE_WIDTH = 1920
PRESCALE_HEIGHT = 1080

MOTIONS = ("zoomin", "zoomout", "panleft", "panright")

CAPTION_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

_TIME_RE = re.compile(r"^time=(\d+):(\d+):(\d+)\.(\d+)", re.MULTILINE)


def find_ffmpeg():
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def find_ffprobe():
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def ffmpeg_version():
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return None
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-version"], capture_output=True, text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = proc.stdout.splitlines()[0] if proc.stdout else ""
    return first.strip() or None


def ffmpeg_version_short():
    """The ffmpeg version number only, e.g. "8.1.2", or None.

    The full first line reads "ffmpeg version 8.1.2 Copyright (c)
    2000-2026 the FFmpeg developers"; the Export window shows the
    number in plain words instead.
    """
    full = ffmpeg_version()
    if not full:
        return None
    match = re.search(r"ffmpeg version (\S+)", full)
    return match.group(1) if match else None


def require_ffmpeg():
    if find_ffmpeg() is None:
        raise FFmpegMissingError()


def motion_for_scene(index):
    """The camera move for a scene: fixed by scene number, so a rebuild
    of the same film looks the same."""
    return MOTIONS[index % len(MOTIONS)]


def zoompan_filter(motion, duration, fps=CLIP_FPS,
                   width=CLIP_WIDTH, height=CLIP_HEIGHT):
    """The zoompan filter for one camera move."""
    frames = max(1, int(round(duration * fps)))
    if motion == "zoomout":
        zoom = f"max({ZOOM_MAX}-{ZOOM_IN_RATE}*on,1.0)"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "panleft":
        zoom = str(PAN_ZOOM)
        x = f"(iw-iw/zoom)*(1-on/{frames})"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "panright":
        zoom = str(PAN_ZOOM)
        x = f"(iw-iw/zoom)*on/{frames}"
        y = "ih/2-(ih/zoom/2)"
    else:  # zoomin
        zoom = f"min(zoom+{ZOOM_IN_RATE},{ZOOM_MAX})"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    return (
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}"
        f":s={width}x{height}:fps={fps}"
    )


def build_scene_filter(motion, duration):
    """Full video filter for one scene clip: cover-scale, then move."""
    prescale = (
        f"scale={PRESCALE_WIDTH}:{PRESCALE_HEIGHT}"
        ":force_original_aspect_ratio=increase,"
        f"crop={PRESCALE_WIDTH}:{PRESCALE_HEIGHT}"
    )
    return f"{prescale},{zoompan_filter(motion, duration)},setsar=1,format=yuv420p"


def _parse_progress_time(text):
    """Last encoded time in seconds from -progress output, or None."""
    matches = _TIME_RE.findall(text)
    if not matches:
        return None
    hours, minutes, seconds, centis = matches[-1]
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            + int(centis) / 100.0)


def _tail_lines(text, count=12):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-count:]


def run_ffmpeg(cmd, total_seconds=None, cancel_event=None,
               progress_cb=None, timeout_s=1800):
    """Run ffmpeg, reading real progress from its progress file.

    Progress and errors go to temp files, never pipes, so a long run
    cannot deadlock. Raises HicksFieldError with the last ffmpeg lines
    when ffmpeg fails, CancelledError when cancelled.
    """
    progress_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".txt", delete=False
    )
    error_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".txt", delete=False
    )
    progress_path = progress_file.name
    error_path = error_file.name
    progress_file.close()
    error_file.close()
    full_cmd = list(cmd) + ["-progress", progress_path, "-nostats"]
    try:
        with open(error_path, "w") as err_handle:
            try:
                proc = subprocess.Popen(
                    full_cmd, stdout=subprocess.DEVNULL,
                    stderr=err_handle, text=True,
                )
            except OSError as exc:
                raise HicksFieldError(
                    f"Could not start ffmpeg: {exc}.",
                    "Check ffmpeg is installed and try again.",
                ) from exc
            deadline = time.monotonic() + timeout_s
            last_fraction = 0.0
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.wait()
                    raise CancelledError()
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raise HicksFieldError(
                        "ffmpeg ran past its time limit.",
                        "Try again with fewer scenes.",
                    )
                if progress_cb is not None and total_seconds:
                    try:
                        text = Path(progress_path).read_text(
                            encoding="utf-8", errors="ignore"
                        )
                    except OSError:
                        text = ""
                    done = _parse_progress_time(text)
                    if done is not None:
                        fraction = max(0.0, min(1.0, done / total_seconds))
                        if fraction > last_fraction:
                            last_fraction = fraction
                            progress_cb(fraction)
                time.sleep(0.3)
        if proc.returncode != 0:
            try:
                err_text = Path(error_path).read_text(
                    encoding="utf-8", errors="ignore")
            except OSError:
                # The error file is gone (a cleaner swept /tmp, or the
                # disk filled): report the failure without its tail
                # rather than crashing on the missing file.
                err_text = ""
            tail = "\n".join(_tail_lines(err_text))
            raise HicksFieldError(
                "ffmpeg stopped with an error.",
                f"Last lines: {tail}" if tail else "Try again.",
            )
        if progress_cb is not None:
            progress_cb(1.0)
    finally:
        for path in (progress_path, error_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def make_scene_clip(still_path, out_clip, duration, motion,
                    audio_path=None, cancel_event=None, progress_cb=None):
    """Turn one still into a moving clip of `duration` seconds."""
    require_ffmpeg()
    ffmpeg = find_ffmpeg()
    out_clip = Path(out_clip)
    out_clip.parent.mkdir(parents=True, exist_ok=True)
    filt = build_scene_filter(motion, duration)
    cmd = [str(ffmpeg), "-y", "-loop", "1", "-i", str(still_path)]
    if audio_path is not None:
        cmd += ["-i", str(audio_path)]
    cmd += ["-filter_complex", f"[0:v]{filt}[v]",
            "-map", "[v]", "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p"]
    if audio_path is not None:
        cmd += ["-map", "1:a", "-af",
                f"apad=whole_dur={duration:.3f}",
                "-c:a", "aac", "-b:a", "128k", "-shortest"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(out_clip)]
    run_ffmpeg(cmd, total_seconds=duration, cancel_event=cancel_event,
               progress_cb=progress_cb)
    if not out_clip.exists() or out_clip.stat().st_size == 0:
        raise HicksFieldError(
            "ffmpeg finished but the scene clip is missing.",
            "Try again.",
        )
    return out_clip


def format_srt_time(seconds):
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3600 * 1000)
    minutes, millis = divmod(millis, 60 * 1000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(entries, out_path):
    """Write captions. entries: [(start_s, end_s, text)]."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    for index, (start, end, text) in enumerate(entries, start=1):
        clean = " ".join(str(text).split())
        parts.append(
            f"{index}\n{format_srt_time(start)} --> "
            f"{format_srt_time(end)}\n{clean}\n"
        )
    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path


def find_caption_font():
    for candidate in CAPTION_FONTS:
        if Path(candidate).exists():
            return candidate
    return None


def _escape_subtitles_path(path):
    text = str(path)
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def assemble(clips, film_path, srt_path=None, font_path=None,
             total_seconds=None, cancel_event=None, progress_cb=None):
    """Cut scene clips together into the finished film."""
    require_ffmpeg()
    ffmpeg = find_ffmpeg()
    film_path = Path(film_path)
    film_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = film_path.with_suffix(".concat.txt")
    lines = []
    for clip in clips:
        safe = str(clip).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        cmd = [str(ffmpeg), "-y", "-f", "concat", "-safe", "0",
               "-i", str(list_path)]
        if srt_path is not None and font_path is not None:
            style = (
                "FontSize=22,PrimaryColour=&HFFFFFF&,"
                "OutlineColour=&H80000000&,BorderStyle=1,Outline=2,"
                "Alignment=2"
            )
            filt = (
                "subtitles=" + _escape_subtitles_path(srt_path)
                + ":fontsdir=" + _escape_subtitles_path(
                    str(Path(font_path).parent))
                + ":force_style='" + style + "'"
            )
            cmd += ["-vf", filt]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(film_path)]
        run_ffmpeg(cmd, total_seconds=total_seconds,
                   cancel_event=cancel_event, progress_cb=progress_cb)
    finally:
        list_path.unlink(missing_ok=True)
    if not film_path.exists() or film_path.stat().st_size == 0:
        raise HicksFieldError(
            "ffmpeg finished but the film file is missing.",
            "Try again.",
        )
    return film_path


def probe_duration(path):
    """Length of a media file in seconds, or None when unknown."""
    ffprobe = find_ffprobe()
    if ffprobe is None:
        return None
    try:
        proc = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None
