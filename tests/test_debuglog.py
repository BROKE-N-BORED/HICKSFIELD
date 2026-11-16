import logging
import unittest
from unittest import mock

from hicksfield import debuglog
from tests.helpers import TempEnvTestCase, write_png, write_wav


class _StubEngine:
    """A stills engine that writes a tiny picture, for logging tests."""

    label = "Stub stills"

    def check_ram(self, force=False):
        return 0

    def generate(self, prompt, out_path, seed=-1, cancel_event=None,
                 force=False):
        write_png(out_path)
        return out_path


def _fake_synthesize(text, out_path, cancel_event=None,
                     voice_id=None):
    path = write_wav(out_path, seconds=1.0)
    return path, 1.0


class DebugLogTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        self._reset_logger()
        debuglog.clear_ring()

    def tearDown(self):
        self._reset_logger()
        super().tearDown()

    @staticmethod
    def _reset_logger():
        # The logger is global; its file handler points at whichever
        # temp dir was set up first, and that dir is deleted by the
        # owning test's tearDown. Drop all handlers so each test gets
        # a fresh handler in its own temp dir.
        logger = logging.getLogger(debuglog.LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def test_scrub_replaces_home(self):
        text = debuglog.scrub_text("/home/joshua/film/x", home="/home/joshua")
        self.assertEqual(text, "~/film/x")

    def test_scrub_redacts_secrets(self):
        for key in ("api_key", "token", "password", "secret"):
            text = debuglog.scrub_text(f"login {key}=abc123")
            self.assertNotIn("abc123", text, key)
            self.assertIn("<redacted>", text, key)

    def test_ring_keeps_last_200(self):
        logger = debuglog.setup_debug_log(self.tmp / "logs")
        for index in range(250):
            logger.info("line %d", index)
        records = debuglog.ring_records()
        self.assertEqual(len(records), 200)
        self.assertIn("line 249", records[-1])
        self.assertNotIn("line 0", "\n".join(records))

    def test_file_log_created(self):
        logger = debuglog.setup_debug_log(self.tmp / "logs")
        logger.info("hello log")
        log_file = self.tmp / "logs" / "hicksfield.log"
        self.assertTrue(log_file.exists())
        self.assertIn("hello log", log_file.read_text(encoding="utf-8"))

    def test_debug_report_scrubs_and_names_version(self):
        logger = debuglog.setup_debug_log(self.tmp / "logs")
        logger.info("api_key=should-not-appear")
        report = debuglog.build_debug_report(
            "0.2.0", "RAM: 8000 MB total.")
        self.assertIn("HicksField 0.2.0", report)
        self.assertIn("RAM: 8000 MB total.", report)
        self.assertNotIn("should-not-appear", report)
        self.assertIn("<redacted>", report)

    def test_pipeline_build_logs_to_ring(self):
        from hicksfield import pipeline, scenes as scenes_mod, video, voice
        debuglog.setup_debug_log(self.tmp / "logs")
        project = scenes_mod.Project(
            name="Test", idea="A stool.",
            scenes=[scenes_mod.Scene(prompt="a stool",
                                     narration="A stool sits here.")])
        project_dir = self.tmp / "film"
        with mock.patch.object(voice, "synthesize",
                               side_effect=_fake_synthesize), \
                mock.patch.object(video, "make_scene_clip",
                                  return_value=project_dir / "clips"
                                  / "scene-01.mp4"), \
                mock.patch.object(video, "assemble",
                                  return_value=project_dir / "film.mp4"):
            pipeline.build_film(project, project_dir, _StubEngine())
        records = "\n".join(debuglog.ring_records())
        self.assertIn("Building film", records)
        self.assertIn("Step started: Making pictures", records)
        self.assertIn("Step finished: Making pictures", records)
        self.assertIn("Step started: Speaking the lines", records)
        self.assertIn("Step started: Moving the pictures", records)
        self.assertIn("Step started: Cutting the film together", records)
        self.assertIn("Film built", records)
        report = debuglog.build_debug_report("0.3.0", "RAM: 8000 MB.")
        self.assertNotIn("(log is empty)", report)
        self.assertIn("Building film", report)

    def test_skipped_assets_are_logged(self):
        from hicksfield import pipeline, scenes as scenes_mod, video, voice
        debuglog.setup_debug_log(self.tmp / "logs")
        project_dir = self.tmp / "film"
        paths = scenes_mod.ensure_project_dirs(project_dir)
        write_png(paths["stills"] / "scene-01.png")
        write_wav(paths["audio"] / "scene-01.wav", seconds=1.0)
        (paths["clips"] / "scene-01.mp4").write_bytes(b"fake clip")
        project = scenes_mod.Project(
            name="Test", idea="A stool.",
            scenes=[scenes_mod.Scene(
                prompt="a stool", narration="A stool sits here.",
                still="stills/scene-01.png", audio="audio/scene-01.wav",
                clip="clips/scene-01.mp4")])
        with mock.patch.object(video, "assemble",
                               return_value=project_dir / "film.mp4"):
            pipeline.build_film(project, project_dir, _StubEngine())
        records = "\n".join(debuglog.ring_records())
        self.assertIn("kept its picture", records)
        self.assertIn("kept its spoken line", records)
        self.assertIn("kept its moving clip", records)

    def test_job_lock_logs_acquire_and_release(self):
        from tests.helpers import require_qt
        require_qt(self)
        from hicksfield.workers import JobLock
        debuglog.setup_debug_log(self.tmp / "logs")
        lock = JobLock()
        self.assertTrue(lock.try_start("Building the film"))
        self.assertFalse(lock.try_start("Making pictures"))
        lock.finish()
        records = "\n".join(debuglog.ring_records())
        self.assertIn("Job lock taken by Building the film", records)
        self.assertIn("Job refused", records)
        self.assertIn("Job lock released by Building the film", records)


if __name__ == "__main__":
    unittest.main()
