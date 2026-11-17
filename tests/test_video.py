import shutil
import subprocess
import unittest
from unittest import mock

from hicksfield import video
from hicksfield.errors import FFmpegMissingError, HicksFieldError
from tests.helpers import TempEnvTestCase, write_png


def _has_ffmpeg():
    return video.find_ffmpeg() is not None


def _probe_duration(path):
    """Length of a media file in seconds, or None when ffprobe is
    missing or cannot read it. Test-only helper: the app no longer
    probes durations."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _has_subtitles_filter():
    ffmpeg = video.find_ffmpeg()
    if ffmpeg is None:
        return False
    proc = subprocess.run([str(ffmpeg), "-hide_banner", "-filters"],
                          capture_output=True, text=True, timeout=30)
    return " subtitles " in proc.stdout


class VideoPureTests(unittest.TestCase):
    def test_motion_cycles(self):
        self.assertEqual(video.motion_for_scene(0), "zoomin")
        self.assertEqual(video.motion_for_scene(1), "zoomout")
        self.assertEqual(video.motion_for_scene(2), "panleft")
        self.assertEqual(video.motion_for_scene(3), "panright")
        self.assertEqual(video.motion_for_scene(4), "zoomin")

    def test_zoompan_filter(self):
        filt = video.zoompan_filter("zoomin", 5)
        self.assertIn("zoompan", filt)
        self.assertIn("fps=30", filt)
        self.assertIn("s=1280x720", filt)
        self.assertIn("d=150", filt)
        self.assertIn("min(zoom+", video.zoompan_filter("zoomin", 5))
        self.assertIn("max(", video.zoompan_filter("zoomout", 5))
        self.assertIn("1-on/", video.zoompan_filter("panleft", 5))
        self.assertIn("on/", video.zoompan_filter("panright", 5))

    def test_scene_filter_scales_then_moves(self):
        filt = video.build_scene_filter("zoomin", 5)
        self.assertIn("scale=1920:1080", filt)
        self.assertIn("crop=1920:1080", filt)
        self.assertIn("zoompan", filt)

    def test_format_srt_time(self):
        self.assertEqual(video.format_srt_time(0), "00:00:00,000")
        self.assertEqual(video.format_srt_time(61.5), "00:01:01,500")
        self.assertEqual(video.format_srt_time(3661.25), "01:01:01,250")

    def test_write_srt(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "film.srt"
            video.write_srt([(0, 3, "First line"), (3, 6, "Second line")],
                            path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("1\n00:00:00,000 --> 00:00:03,000\nFirst line",
                      text)
        self.assertIn("2\n00:00:03,000 --> 00:00:06,000\nSecond line",
                      text)


class VideoFFmpegTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        if not _has_ffmpeg():
            self.skipTest("ffmpeg not installed")
        self.still = write_png(self.tmp / "still.png")

    def test_make_scene_clip(self):
        clip = self.tmp / "clip.mp4"
        video.make_scene_clip(self.still, clip, 1, "zoomin")
        self.assertTrue(clip.exists())
        self.assertGreater(clip.stat().st_size, 0)
        duration = _probe_duration(clip)
        if duration is not None:
            self.assertAlmostEqual(duration, 1.0, delta=0.2)

    def test_make_scene_clip_still(self):
        clip = self.tmp / "clip-still.mp4"
        video.make_scene_clip(self.still, clip, 1, "still")
        self.assertTrue(clip.exists())
        self.assertGreater(clip.stat().st_size, 0)
        duration = _probe_duration(clip)
        if duration is not None:
            self.assertAlmostEqual(duration, 1.0, delta=0.2)

    def test_make_scene_clip_with_audio(self):
        from tests.helpers import write_wav
        audio = write_wav(self.tmp / "line.wav", seconds=1.0)
        clip = self.tmp / "clip.mp4"
        video.make_scene_clip(self.still, clip, 3, "panleft",
                              audio_path=audio)
        self.assertTrue(clip.exists())
        duration = _probe_duration(clip)
        if duration is not None:
            self.assertAlmostEqual(duration, 3.0, delta=0.3)

    def test_assemble_two_clips(self):
        clip1 = self.tmp / "c1.mp4"
        clip2 = self.tmp / "c2.mp4"
        video.make_scene_clip(self.still, clip1, 1, "zoomin")
        video.make_scene_clip(self.still, clip2, 1, "zoomout")
        film = self.tmp / "film.mp4"
        video.assemble([clip1, clip2], film, total_seconds=2)
        self.assertTrue(film.exists())
        duration = _probe_duration(film)
        if duration is not None:
            self.assertAlmostEqual(duration, 2.0, delta=0.3)

    def test_assemble_with_burned_captions(self):
        if not _has_subtitles_filter():
            self.skipTest("ffmpeg has no subtitles filter")
        font = video.find_caption_font()
        if font is None:
            self.skipTest("no caption font on this machine")
        clip = self.tmp / "c1.mp4"
        video.make_scene_clip(self.still, clip, 1, "zoomin")
        srt = self.tmp / "film.srt"
        video.write_srt([(0, 1, "Hello film")], srt)
        film = self.tmp / "film.mp4"
        video.assemble([clip], film, srt_path=srt, font_path=font,
                       total_seconds=1)
        self.assertTrue(film.exists())

    def test_ffmpeg_failure_is_plain(self):
        with self.assertRaises(HicksFieldError):
            video.run_ffmpeg(
                [str(video.find_ffmpeg()), "-y", "-i",
                 str(self.tmp / "missing.png"),
                 str(self.tmp / "out.mp4")],
                total_seconds=1)

    def test_require_ffmpeg(self):
        # ffmpeg exists here, so this must not raise.
        video.require_ffmpeg()
        self.assertIsNotNone(video.ffmpeg_version())
        self.assertTrue(video.ffmpeg_version().startswith("ffmpeg"))


class VideoMissingTests(unittest.TestCase):
    def test_require_ffmpeg_raises_when_missing(self):
        with mock.patch.object(video, "find_ffmpeg", return_value=None):
            with self.assertRaises(FFmpegMissingError):
                video.require_ffmpeg()


class MotionChoiceTests(unittest.TestCase):
    """Item 13: a scene's own camera move wins over the rotation."""

    def test_chosen_move_wins(self):
        self.assertEqual(video.motion_for_scene(0, "panleft"),
                         "panleft")
        self.assertEqual(video.motion_for_scene(1, "still"), "still")
        self.assertEqual(video.motion_for_scene(2, "zoomout"),
                         "zoomout")
        self.assertEqual(video.motion_for_scene(3, "panright"),
                         "panright")

    def test_auto_and_empty_rotate_by_scene_number(self):
        self.assertEqual(video.motion_for_scene(0, ""), "zoomin")
        self.assertEqual(video.motion_for_scene(1, ""), "zoomout")
        self.assertEqual(video.motion_for_scene(0, "auto"), "zoomin")
        self.assertEqual(video.motion_for_scene(3, "auto"), "panright")
        self.assertEqual(video.motion_for_scene(4, "auto"), "zoomin")

    def test_unknown_move_falls_back_to_rotation(self):
        self.assertEqual(video.motion_for_scene(0, "spin"), "zoomin")
        self.assertEqual(video.motion_for_scene(2, "spin"), "panleft")

    def test_still_filter_holds_the_frame(self):
        filt = video.zoompan_filter("still", 5)
        self.assertIn("zoompan", filt)
        self.assertIn("z='1.0'", filt)
        self.assertNotIn("on/", filt)
        self.assertNotIn("zoom+", filt)


class PosterTests(TempEnvTestCase):
    """Item 14: the first frame, taken with ffmpeg."""

    def setUp(self):
        super().setUp()
        if not _has_ffmpeg():
            self.skipTest("ffmpeg not installed")
        self.still = write_png(self.tmp / "still.png")
        self.film = self.tmp / "film.mp4"
        video.make_scene_clip(self.still, self.film, 1, "zoomin")

    def test_extract_first_frame(self):
        out = self.tmp / "poster.png"
        result = video.extract_first_frame(self.film, out)
        self.assertEqual(result, out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 0)
        self.assertEqual(out.read_bytes()[:8],
                         b"\x89PNG\r\n\x1a\n")

    def test_extract_first_frame_missing_film_returns_none(self):
        out = self.tmp / "poster.png"
        self.assertIsNone(video.extract_first_frame(
            self.tmp / "nope.mp4", out))
        self.assertFalse(out.exists())

    def test_extract_first_frame_broken_film_returns_none(self):
        broken = self.tmp / "broken.mp4"
        broken.write_bytes(b"not a film")
        out = self.tmp / "poster.png"
        self.assertIsNone(video.extract_first_frame(broken, out))

    def test_extract_first_frame_without_ffmpeg_returns_none(self):
        with mock.patch.object(video, "find_ffmpeg",
                               return_value=None):
            self.assertIsNone(video.extract_first_frame(
                self.film, self.tmp / "poster.png"))


if __name__ == "__main__":
    unittest.main()
