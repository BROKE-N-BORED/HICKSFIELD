import shutil
import subprocess
import threading
import unittest
from unittest import mock

from hicksfield import pipeline, scenes as scenes_mod, video, voice
from hicksfield.errors import CancelledError, HicksFieldError
from hicksfield.stills import StillEngine
from tests.helpers import TempEnvTestCase, write_png, write_wav


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


class FakeEngine(StillEngine):
    key = "fake"
    label = "Fake stills"
    description = "A test engine."

    @property
    def required_ram_mb(self):
        return 0

    def is_ready(self):
        return True, "Ready."

    def setup(self, report=None, cancel_event=None):
        return None

    def generate(self, prompt, out_path, seed=-1, cancel_event=None,
                 force=False):
        write_png(out_path)
        return out_path


def _fake_synthesize(text, out_path, cancel_event=None,
                     voice_id=None):
    path = write_wav(out_path, seconds=1.0)
    return path, 1.0


class PipelineTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        self.project_dir = self.tmp / "film"
        self.project = scenes_mod.Project(
            name="Test", idea="A stool.",
            scenes=[
                scenes_mod.Scene(prompt="a stool",
                                 narration="A stool sits here."),
                scenes_mod.Scene(prompt="a hammer",
                                 narration="A hammer waits."),
            ])
        self._font_patch = None
        ffmpeg = video.find_ffmpeg()
        proc = subprocess.run([str(ffmpeg), "-hide_banner", "-filters"],
                              capture_output=True, text=True, timeout=30)
        if " subtitles " not in proc.stdout:
            self._font_patch = mock.patch.object(
                video, "find_caption_font", return_value=None)
            self._font_patch.start()

    def tearDown(self):
        if self._font_patch is not None:
            self._font_patch.stop()
        super().tearDown()

    def test_full_build(self):
        with mock.patch.object(voice, "synthesize",
                               side_effect=_fake_synthesize):
            result = pipeline.build_film(
                self.project, self.project_dir, FakeEngine(),
                save_cb=lambda p: scenes_mod.save_project(
                    p, self.project_dir))
        self.assertEqual(result["stills_made"], 2)
        self.assertEqual(result["voice_made"], 2)
        self.assertEqual(result["clips_made"], 2)
        self.assertEqual(result["durations"], [3, 3])
        self.assertEqual(result["total_seconds"], 6)
        film = self.project_dir / "film.mp4"
        self.assertTrue(film.exists())
        self.assertGreater(film.stat().st_size, 0)
        captions = self.project_dir / "film.srt"
        self.assertTrue(captions.exists())
        self.assertIn("A stool sits here.",
                      captions.read_text(encoding="utf-8"))
        duration = _probe_duration(film)
        if duration is not None:
            self.assertAlmostEqual(duration, 6.0, delta=0.5)
        # The project file records the assets.
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual(loaded.scenes[0].still, "stills/scene-01.png")
        self.assertEqual(loaded.scenes[0].audio, "audio/scene-01.wav")
        self.assertEqual(loaded.scenes[0].clip, "clips/scene-01.mp4")

    def test_state_save_works_as_save_cb(self):
        """Regression: the Export window passes AppState.save straight
        to build_film as save_cb, and the pipeline calls
        save_cb(project). A no-arg save() crashed the film build with
        "AppState.save() takes 1 positional argument but 2 were
        given", so the film was never created."""
        from tests.helpers import app, require_qt
        require_qt(self)
        app()
        from hicksfield.gui import AppState
        state = AppState()
        state.project = self.project
        state.project_dir = self.project_dir
        with mock.patch.object(voice, "synthesize",
                               side_effect=_fake_synthesize):
            result = pipeline.build_film(
                self.project, self.project_dir, FakeEngine(),
                save_cb=state.save)
        self.assertEqual(result["stills_made"], 2)
        film = self.project_dir / "film.mp4"
        self.assertTrue(film.exists())
        # The state saved the project as the pieces landed.
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual(loaded.scenes[0].still, "stills/scene-01.png")
        self.assertEqual(loaded.scenes[0].audio, "audio/scene-01.wav")
        self.assertEqual(loaded.scenes[0].clip, "clips/scene-01.mp4")

    def test_second_build_skips_everything(self):
        with mock.patch.object(voice, "synthesize",
                               side_effect=_fake_synthesize):
            pipeline.build_film(
                self.project, self.project_dir, FakeEngine(),
                save_cb=lambda p: scenes_mod.save_project(
                    p, self.project_dir))
            result = pipeline.build_film(
                self.project, self.project_dir, FakeEngine(),
                save_cb=lambda p: scenes_mod.save_project(
                    p, self.project_dir))
        self.assertEqual(result["stills_made"], 0)
        self.assertEqual(result["voice_made"], 0)
        self.assertEqual(result["clips_made"], 0)
        self.assertEqual(result["stills_skipped"], 2)
        self.assertEqual(result["voice_skipped"], 2)
        self.assertEqual(result["clips_skipped"], 2)
        self.assertEqual(len(result["skipped"]), 6)

    def test_empty_project_is_plain_error(self):
        empty = scenes_mod.Project(idea="nothing")
        with self.assertRaises(HicksFieldError):
            pipeline.build_film(empty, self.project_dir, FakeEngine())

    def test_cancel_before_start(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(CancelledError):
            pipeline.build_film(self.project, self.project_dir,
                                FakeEngine(), cancel_event=event)


class RecordingEngine(FakeEngine):
    """A fake engine that records the seed each picture was made
    with."""

    def __init__(self):
        self.calls = []

    def generate(self, prompt, out_path, seed=-1, cancel_event=None,
                 force=False):
        self.calls.append((prompt, seed))
        return super().generate(prompt, out_path, seed=seed,
                                cancel_event=cancel_event, force=force)


class StalePipelineTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        self.project_dir = self.tmp / "film"
        self.project = scenes_mod.Project(
            name="Test", idea="A stool.",
            scenes=[
                scenes_mod.Scene(prompt="a stool",
                                 narration="A stool sits here."),
                scenes_mod.Scene(prompt="a hammer",
                                 narration="A hammer waits."),
            ])
        self._font_patch = None
        ffmpeg = video.find_ffmpeg()
        proc = subprocess.run([str(ffmpeg), "-hide_banner", "-filters"],
                              capture_output=True, text=True, timeout=30)
        if " subtitles " not in proc.stdout:
            self._font_patch = mock.patch.object(
                video, "find_caption_font", return_value=None)
            self._font_patch.start()

    def tearDown(self):
        if self._font_patch is not None:
            self._font_patch.stop()
        super().tearDown()

    def _build(self, engine=None):
        with mock.patch.object(voice, "synthesize",
                               side_effect=_fake_synthesize):
            return pipeline.build_film(
                self.project, self.project_dir,
                engine or FakeEngine(),
                save_cb=lambda p: scenes_mod.save_project(
                    p, self.project_dir))

    def test_provenance_recorded_after_build(self):
        self._build()
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual(loaded.scenes[0].still_prompt, "a stool")
        self.assertEqual(loaded.scenes[0].voice_text,
                         "A stool sits here.")
        self.assertEqual(loaded.scenes[1].still_prompt, "a hammer")
        self.assertEqual(loaded.scenes[1].voice_text, "A hammer waits.")

    def test_edited_prompt_remakes_still_and_clip(self):
        self._build()
        self.project.scenes[0].prompt = "a red stool"
        result = self._build()
        self.assertEqual(result["stills_made"], 1)
        self.assertEqual(result["stills_skipped"], 1)
        self.assertEqual(result["voice_made"], 0)
        self.assertEqual(result["voice_skipped"], 2)
        self.assertEqual(result["clips_made"], 1)
        self.assertEqual(result["clips_skipped"], 1)
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual(loaded.scenes[0].still_prompt, "a red stool")
        self.assertEqual(loaded.scenes[1].still_prompt, "a hammer")

    def test_edited_narration_remakes_line_and_clip(self):
        self._build()
        self.project.scenes[1].narration = "A hammer waits still."
        result = self._build()
        self.assertEqual(result["stills_made"], 0)
        self.assertEqual(result["stills_skipped"], 2)
        self.assertEqual(result["voice_made"], 1)
        self.assertEqual(result["voice_skipped"], 1)
        self.assertEqual(result["clips_made"], 1)
        self.assertEqual(result["clips_skipped"], 1)
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual(loaded.scenes[1].voice_text,
                         "A hammer waits still.")

    def test_legacy_no_provenance_skips(self):
        """A project from before provenance was kept has files but
        no recorded words: the rebuild keeps everything."""
        self._build()
        for scene in self.project.scenes:
            scene.still_prompt = ""
            scene.voice_text = ""
        scenes_mod.save_project(self.project, self.project_dir)
        result = self._build()
        self.assertEqual(result["stills_made"], 0)
        self.assertEqual(result["voice_made"], 0)
        self.assertEqual(result["clips_made"], 0)
        self.assertEqual(result["stills_skipped"], 2)
        self.assertEqual(result["voice_skipped"], 2)
        self.assertEqual(result["clips_skipped"], 2)

    def test_stale_remake_keeps_recorded_seed(self):
        engine = RecordingEngine()
        self._build(engine)
        # A remake recorded this seed for scene 1 earlier.
        self.project.scenes[0].still_seed = 4242
        self.project.scenes[0].prompt = "a red stool"
        engine.calls.clear()
        self._build(engine)
        seeds = dict(engine.calls)
        self.assertEqual(seeds.get("a red stool"), 4242)
        # Scene 2 was not remade, so it was never generated.
        self.assertNotIn("a hammer", seeds)


class VoiceMotionPipelineTests(TempEnvTestCase):
    """Items 11, 13: the build speaks in the project's voice and
    moves each scene with its own camera move."""

    def setUp(self):
        super().setUp()
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        self.project_dir = self.tmp / "film"
        self.project = scenes_mod.Project(
            name="Test", idea="A stool.",
            scenes=[
                scenes_mod.Scene(prompt="a stool",
                                 narration="A stool sits here."),
                scenes_mod.Scene(prompt="a hammer",
                                 narration="A hammer waits."),
            ])
        self._font_patch = None
        ffmpeg = video.find_ffmpeg()
        proc = subprocess.run([str(ffmpeg), "-hide_banner", "-filters"],
                              capture_output=True, text=True, timeout=30)
        if " subtitles " not in proc.stdout:
            self._font_patch = mock.patch.object(
                video, "find_caption_font", return_value=None)
            self._font_patch.start()

    def tearDown(self):
        if self._font_patch is not None:
            self._font_patch.stop()
        super().tearDown()

    def _build(self, engine=None):
        with mock.patch.object(voice, "synthesize",
                               side_effect=_fake_synthesize):
            return pipeline.build_film(
                self.project, self.project_dir,
                engine or FakeEngine(),
                save_cb=lambda p: scenes_mod.save_project(
                    p, self.project_dir))

    def test_provenance_records_voice_and_motion(self):
        self._build()
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual(loaded.scenes[0].voice_id,
                         "en_US-lessac-medium")
        self.assertEqual(loaded.scenes[1].voice_id,
                         "en_US-lessac-medium")
        self.assertEqual(loaded.scenes[0].clip_motion, "zoomin")
        self.assertEqual(loaded.scenes[1].clip_motion, "zoomout")

    def test_synthesize_gets_the_project_voice(self):
        calls = []

        def _recording(text, out_path, cancel_event=None,
                       voice_id=None):
            calls.append(voice_id)
            return _fake_synthesize(text, out_path, cancel_event,
                                    voice_id)

        self.project.voice = "en_GB-alan-medium"
        with mock.patch.object(voice, "synthesize",
                               side_effect=_recording):
            pipeline.build_film(
                self.project, self.project_dir, FakeEngine(),
                save_cb=lambda p: scenes_mod.save_project(
                    p, self.project_dir))
        self.assertEqual(calls, ["en_GB-alan-medium",
                                 "en_GB-alan-medium"])

    def test_voice_change_remakes_lines_and_clips(self):
        self._build()
        self.project.voice = "en_US-ryan-high"
        result = self._build()
        self.assertEqual(result["stills_made"], 0)
        self.assertEqual(result["stills_skipped"], 2)
        self.assertEqual(result["voice_made"], 2)
        self.assertEqual(result["voice_skipped"], 0)
        self.assertEqual(result["clips_made"], 2)
        self.assertEqual(result["clips_skipped"], 0)
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual(loaded.scenes[0].voice_id,
                         "en_US-ryan-high")
        self.assertEqual(loaded.scenes[1].voice_id,
                         "en_US-ryan-high")

    def test_motion_change_remakes_only_that_clip(self):
        self._build()
        self.project.scenes[0].motion = "panleft"
        result = self._build()
        self.assertEqual(result["stills_made"], 0)
        self.assertEqual(result["stills_skipped"], 2)
        self.assertEqual(result["voice_made"], 0)
        self.assertEqual(result["voice_skipped"], 2)
        self.assertEqual(result["clips_made"], 1)
        self.assertEqual(result["clips_skipped"], 1)
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual(loaded.scenes[0].clip_motion, "panleft")
        self.assertEqual(loaded.scenes[1].clip_motion, "zoomout")

    def test_still_motion_builds_a_clip(self):
        self.project.scenes[0].motion = "still"
        result = self._build()
        self.assertEqual(result["clips_made"], 2)
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual(loaded.scenes[0].clip_motion, "still")


if __name__ == "__main__":
    unittest.main()
