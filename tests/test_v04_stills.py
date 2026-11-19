"""v0.4.0 Group C — Stills and Pipeline (Task List 3 items 4, 17, 18,
19, 27, 28, 29, 35, 41, 47)."""

import base64
import os
import subprocess
import sys
import tarfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hicksfield import pipeline, scenes as scenes_mod, stills, video, voice
from hicksfield.errors import CancelledError, DownloadError, HicksFieldError
from hicksfield.steps import RUN_TIMEOUT_TAIL, run_logged, run_timed_out
from tests.helpers import (
    TINY_PNG_B64,
    TempEnvTestCase,
    wait_for,
    write_png,
    write_wav,
)
from tests.test_gui import GuiBase
from tests.test_pipeline import FakeEngine

TINY_PNG = base64.b64decode(TINY_PNG_B64)


class RecordingEngine(FakeEngine):
    """A fake engine that records the seed each picture was made with."""

    def __init__(self):
        self.calls = []

    def generate(self, prompt, out_path, seed=-1, cancel_event=None,
                 force=False):
        self.calls.append((prompt, seed))
        return super().generate(prompt, out_path, seed=seed,
                                cancel_event=cancel_event, force=force)


def _fake_synthesize(text, out_path, cancel_event=None,
                     voice_id=None):
    path = write_wav(out_path, seconds=1.0)
    return path, 1.0


# --- Item 4: stable asset identity ---------------------------------------


class AssetIdTests(TempEnvTestCase):
    def test_legacy_scene_takes_tag_from_positional_filename(self):
        scene = scenes_mod.Scene.from_dict(
            {"prompt": "a stool", "still": "stills/scene-02.png",
             "audio": "audio/scene-02.wav"})
        self.assertEqual(scene.asset_id, "02")
        self.assertEqual(scenes_mod.scene_still_name_for(scene),
                         "scene-02.png")
        self.assertEqual(scenes_mod.scene_audio_name_for(scene),
                         "scene-02.wav")
        self.assertEqual(scenes_mod.scene_clip_name_for(scene),
                         "scene-02.mp4")

    def test_fresh_film_gets_numbered_ids(self):
        scenes = [scenes_mod.Scene(prompt="a"),
                  scenes_mod.Scene(prompt="b")]
        scenes_mod.ensure_asset_ids(scenes)
        self.assertEqual([s.asset_id for s in scenes], ["01", "02"])
        self.assertEqual(scenes_mod.scene_still_name_for(scenes[0]),
                         "scene-01.png")

    def test_inserted_scene_gets_a_free_id_not_a_collision(self):
        scenes = [scenes_mod.Scene(prompt="new"),
                  scenes_mod.Scene(prompt="a", asset_id="01"),
                  scenes_mod.Scene(prompt="b", asset_id="02")]
        scenes_mod.ensure_asset_ids(scenes)
        self.assertEqual(scenes[0].asset_id, "03")
        self.assertEqual(len({s.asset_id for s in scenes}), 3)

    def test_duplicate_ids_are_reassigned(self):
        scenes = [scenes_mod.Scene(prompt="a", asset_id="01"),
                  scenes_mod.Scene(prompt="b", asset_id="01")]
        scenes_mod.ensure_asset_ids(scenes)
        self.assertEqual(scenes[0].asset_id, "01")
        self.assertNotEqual(scenes[1].asset_id, "01")

    def test_ids_survive_save_and_load(self):
        project = scenes_mod.Project(
            scenes=[scenes_mod.Scene(prompt="a", asset_id="07"),
                    scenes_mod.Scene(prompt="b")])
        target = self.tmp / "film"
        scenes_mod.save_project(project, target)
        loaded = scenes_mod.load_project(target)
        self.assertEqual(loaded.scenes[0].asset_id, "07")
        self.assertEqual(loaded.scenes[1].asset_id, "02")

    def test_v03_project_loads_with_positional_files_intact(self):
        # A v0.3 project.json has no asset_id at all: its scenes take
        # their tags from their recorded files, and a remake writes
        # to the same filenames.
        import json
        target = self.tmp / "film"
        paths = scenes_mod.ensure_project_dirs(target)
        write_png(paths["stills"] / "scene-02.png")
        (target / "project.json").write_text(json.dumps({
            "version": 1, "name": "Old", "idea": "",
            "scenes": [
                {"prompt": "a stool", "narration": "",
                 "still": "stills/scene-02.png", "clip": "", "audio": "",
                 "still_prompt": "a stool", "voice_text": "",
                 "still_seed": 0, "voice_id": "", "motion": "",
                 "clip_motion": ""},
            ],
            "created": "", "updated": "",
            "voice": "en_US-lessac-medium",
        }), encoding="utf-8")
        loaded = scenes_mod.load_project(target)
        scene = loaded.scenes[0]
        self.assertEqual(scene.asset_id, "02")
        self.assertEqual(scenes_mod.scene_still_name_for(scene, 0),
                         "scene-02.png")


class ReorderGuiTests(GuiBase):
    """Item 4, proven live: reorder + remake must not touch the other
    scene's picture."""

    def _two_scenes_with_pictures(self):
        from hicksfield.gui import StillsWindow
        scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here."),
            scenes_mod.Scene(prompt="a hammer",
                             narration="A hammer waits."),
        ]
        paths = scenes_mod.ensure_project_dirs(self.state.project_dir)
        write_png(paths["stills"] / "scene-01.png")
        second = paths["stills"] / "scene-02.png"
        write_png(second)
        # A different file, so a write to the wrong file is visible.
        with open(second, "ab") as handle:
            handle.write(b"SECOND")
        for index, scene in enumerate(scenes):
            scene.still = f"stills/scene-0{index + 1}.png"
            scene.still_prompt = scene.prompt
        self.state.project.scenes = scenes
        self.state.save()
        window = self._show(StillsWindow)
        return window, scenes

    def test_reorder_then_remake_writes_to_the_moved_scenes_file(self):
        window, scenes = self._two_scenes_with_pictures()
        stills_dir = self.state.project_dir / "stills"
        first_before = (stills_dir / "scene-01.png").read_bytes()
        # Reorder: the hammer scene moves to the top.
        self.state.project.scenes = [scenes[1], scenes[0]]
        self.state.save()
        engine = RecordingEngine()
        cancel = threading.Event()
        window._remake_one(engine, 0, 999, lambda *a: None, cancel)
        # The hammer scene's own file (scene-02.png) holds the new
        # picture; the stool scene's file is untouched.
        self.assertEqual((stills_dir / "scene-01.png").read_bytes(),
                         first_before)
        self.assertEqual((stills_dir / "scene-02.png").read_bytes(),
                         TINY_PNG)
        self.assertEqual(self.state.project.scenes[0].still,
                         "stills/scene-02.png")
        self.assertEqual(self.state.project.scenes[1].still,
                         "stills/scene-01.png")
        self.assertEqual(engine.calls, [("a hammer", 999)])

    def test_reorder_then_voice_writes_to_the_moved_scenes_file(self):
        from hicksfield.gui import VoiceWindow
        window, scenes = self._two_scenes_with_pictures()
        window.close()
        paths = scenes_mod.ensure_project_dirs(self.state.project_dir)
        write_wav(paths["audio"] / "scene-01.wav")
        write_wav(paths["audio"] / "scene-02.wav")
        for index, scene in enumerate(scenes):
            scene.audio = f"audio/scene-0{index + 1}.wav"
            scene.voice_text = scene.narration
            scene.voice_id = self.state.project.voice
        # Reorder, then change the moved scene's line.
        self.state.project.scenes = [scenes[1], scenes[0]]
        self.state.project.scenes[0].narration = "A hammer waits here."
        self.state.save()
        voice_window = self._show(VoiceWindow)
        made = []
        with mock.patch.object(voice, "synthesize",
                               side_effect=_fake_synthesize) as synth:
            made.append(voice_window._make_all(
                lambda *a: None, threading.Event()))
            self.assertEqual(made, [1])
            out_path = Path(synth.call_args[0][1])
        self.assertEqual(out_path.name, "scene-02.wav")


# --- Item 17: remake keeps the old picture -------------------------------


class ArchiveTests(TempEnvTestCase):
    def test_archive_versions_the_old_file(self):
        path = self.tmp / "scene-01.png"
        path.write_bytes(b"first")
        first = scenes_mod.archive_previous_asset(path)
        self.assertEqual(first.name, "scene-01.v1.png")
        self.assertEqual(first.read_bytes(), b"first")
        path.write_bytes(b"second")
        second = scenes_mod.archive_previous_asset(path)
        self.assertEqual(second.name, "scene-01.v2.png")
        self.assertEqual(second.read_bytes(), b"second")
        self.assertEqual(path.read_bytes(), b"second")

    def test_archive_of_missing_file_is_none(self):
        self.assertIsNone(scenes_mod.archive_previous_asset(
            self.tmp / "scene-09.png"))


class RemakeKeepsOldPictureTests(GuiBase):
    def _one_scene_with_picture(self):
        from hicksfield.gui import StillsWindow
        scene = scenes_mod.Scene(prompt="a stool",
                                 narration="A stool sits here.")
        paths = scenes_mod.ensure_project_dirs(self.state.project_dir)
        old = paths["stills"] / "scene-01.png"
        write_png(old)
        with open(old, "ab") as handle:
            handle.write(b"OLD-GOOD")
        scene.still = "stills/scene-01.png"
        scene.still_prompt = "a stool"
        self.state.project.scenes = [scene]
        self.state.save()
        window = self._show(StillsWindow)
        return window, scene, old

    def test_remake_keeps_the_old_picture_aside(self):
        window, _scene, old = self._one_scene_with_picture()
        engine = RecordingEngine()
        window._remake_one(engine, 0, 123, lambda *a: None,
                           threading.Event())
        archived = old.with_name("scene-01.v1.png")
        self.assertTrue(archived.exists())
        self.assertTrue(archived.read_bytes().endswith(b"OLD-GOOD"))
        self.assertEqual(old.read_bytes(), TINY_PNG)

    def test_stale_make_keeps_the_old_picture_aside(self):
        window, scene, old = self._one_scene_with_picture()
        scene.prompt = "a red stool"
        engine = RecordingEngine()
        made = window._make_all(engine, lambda *a: None,
                                threading.Event())
        self.assertEqual(made, 1)
        archived = old.with_name("scene-01.v1.png")
        self.assertTrue(archived.exists())
        self.assertTrue(archived.read_bytes().endswith(b"OLD-GOOD"))


# --- Item 18: FastSD never reuses a previous run's picture ---------------


class FastSDFallbackTests(TempEnvTestCase):
    def _ready_engine(self):
        engine = stills.FastSDCPUEngine(
            base_dir=self.tmp / "fast",
            model_dir=self.tmp / "model")
        engine.cli_script.parent.mkdir(parents=True, exist_ok=True)
        engine.cli_script.write_text("x", encoding="utf-8")
        engine.cli_python.parent.mkdir(parents=True, exist_ok=True)
        engine.cli_python.write_text("x", encoding="utf-8")
        for name, _url in engine.model_files():
            dest = engine.model_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
        engine.results_dir.mkdir(parents=True, exist_ok=True)
        return engine

    def test_previous_runs_picture_is_not_accepted(self):
        engine = self._ready_engine()
        old = write_png(engine.results_dir / "old.png")
        # The previous run's picture, a minute old.
        old_time = time.time() - 60
        os.utime(old, (old_time, old_time))
        out = self.tmp / "out.png"
        with mock.patch.object(stills, "run_logged",
                               return_value=(0, "")):
            with self.assertRaises(HicksFieldError) as ctx:
                engine.generate("a stool", out, seed=1, force=True)
        self.assertIn("no picture appeared",
                      ctx.exception.plain_text())
        self.assertFalse(out.exists())

    def test_a_file_this_run_produced_is_accepted(self):
        engine = self._ready_engine()
        out = self.tmp / "out.png"

        def _fake_run(*args, **kwargs):
            write_png(engine.results_dir / "fresh.png")
            return 0, ""

        with mock.patch.object(stills, "run_logged",
                               side_effect=_fake_run):
            result = engine.generate("a stool", out, seed=1, force=True)
        self.assertEqual(result, out)
        self.assertTrue(out.exists())

    def test_a_rewritten_file_counts_as_this_runs(self):
        engine = self._ready_engine()
        old = write_png(engine.results_dir / "same.png")
        old_time = time.time() - 60
        os.utime(old, (old_time, old_time))
        out = self.tmp / "out.png"

        def _fake_run(*args, **kwargs):
            # Same name, new bytes: the run rewrote it.
            with open(engine.results_dir / "same.png", "ab") as handle:
                handle.write(b"MORE")
            return 0, ""

        with mock.patch.object(stills, "run_logged",
                               side_effect=_fake_run):
            engine.generate("a stool", out, seed=1, force=True)
        self.assertTrue(out.exists())


# --- Item 19: cancel lands inside long sub-steps -------------------------


class RunLoggedTests(unittest.TestCase):
    def test_cancel_kills_a_long_child(self):
        cancel = threading.Event()
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        try:
            started = time.monotonic()
            with self.assertRaises(CancelledError):
                run_logged([sys.executable, "-c",
                            "import time; time.sleep(30)"],
                           timeout_s=60, cancel_event=cancel)
            self.assertLess(time.monotonic() - started, 10)
        finally:
            timer.cancel()

    def test_timeout_returns_the_timeout_tail(self):
        code, tail = run_logged(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_s=1)
        self.assertIsNone(code)
        self.assertEqual(tail, RUN_TIMEOUT_TAIL)
        self.assertTrue(run_timed_out(code, tail))

    def test_run_timed_out_only_matches_timeouts(self):
        self.assertFalse(run_timed_out(1, "boom"))
        self.assertFalse(run_timed_out(0, RUN_TIMEOUT_TAIL))
        self.assertFalse(run_timed_out(
            None, "could not start the command: nope"))
        self.assertTrue(run_timed_out(None, RUN_TIMEOUT_TAIL))

    def test_stills_reexports_run_logged(self):
        self.assertIs(stills.run_logged, run_logged)


class CancellableSetupTests(TempEnvTestCase):
    def test_setup_piper_passes_cancel_to_run_logged(self):
        seen = {}

        def _fake_run(cmd, timeout_s=None, cancel_event=None, **kwargs):
            seen["cancel_event"] = cancel_event
            seen["timeout_s"] = timeout_s
            raise CancelledError()

        cancel = threading.Event()
        with mock.patch.object(voice, "find_piper", return_value=None), \
                mock.patch.object(voice, "run_logged",
                                  side_effect=_fake_run):
            with self.assertRaises(CancelledError):
                voice._setup_piper(lambda *a: None, cancel)
        self.assertIs(seen["cancel_event"], cancel)
        self.assertEqual(seen["timeout_s"], 900)

    def test_setup_piper_timeout_says_it_timed_out(self):
        with mock.patch.object(voice, "find_piper", return_value=None), \
                mock.patch.object(voice, "run_logged",
                                  return_value=(None, RUN_TIMEOUT_TAIL)):
            with self.assertRaises(HicksFieldError) as ctx:
                voice._setup_piper(lambda *a: None, threading.Event())
        self.assertIn("took too long", ctx.exception.plain_text())

    def test_git_clone_gets_the_cancel_event(self):
        engine = stills.SDCppEngine(base_dir=self.tmp / "sdcpp")
        seen = {}

        def _fake_run(cmd, timeout_s=None, cancel_event=None, **kwargs):
            seen["cancel_event"] = cancel_event
            seen["cmd"] = cmd
            return 0, ""

        cancel = threading.Event()
        with mock.patch("shutil.which", return_value="/usr/bin/git"), \
                mock.patch.object(stills, "run_logged",
                                  side_effect=_fake_run):
            # The clone is faked, so the checkout never appears and
            # setup reports the failure — but the cancel event must
            # have reached the clone first.
            with self.assertRaises(DownloadError):
                engine._setup_checkout(lambda *a: None, cancel)
        self.assertIs(seen["cancel_event"], cancel)
        self.assertEqual(seen["cmd"][1], "clone")


# --- Item 27: first-made pictures record their seed -----------------------


class FirstMakeSeedTests(GuiBase):
    def test_make_all_records_a_seed_for_a_first_picture(self):
        from hicksfield.gui import StillsWindow
        scene = scenes_mod.Scene(prompt="a stool",
                                 narration="A stool sits here.")
        self.state.project.scenes = [scene]
        self.state.save()
        window = self._show(StillsWindow)
        engine = RecordingEngine()
        made = window._make_all(engine, lambda *a: None,
                                threading.Event())
        self.assertEqual(made, 1)
        self.assertGreater(scene.still_seed, 0)
        self.assertEqual(engine.calls, [("a stool", scene.still_seed)])
        loaded = scenes_mod.load_project(self.state.project_dir)
        self.assertEqual(loaded.scenes[0].still_seed, scene.still_seed)

    def test_stale_remake_still_keeps_the_recorded_seed(self):
        from hicksfield.gui import StillsWindow
        scene = scenes_mod.Scene(prompt="a stool",
                                 narration="A stool sits here.",
                                 still_seed=777)
        paths = scenes_mod.ensure_project_dirs(self.state.project_dir)
        write_png(paths["stills"] / "scene-01.png")
        scene.still = "stills/scene-01.png"
        scene.still_prompt = "a stool"
        scene.prompt = "a red stool"
        self.state.project.scenes = [scene]
        self.state.save()
        window = self._show(StillsWindow)
        engine = RecordingEngine()
        window._make_all(engine, lambda *a: None, threading.Event())
        self.assertEqual(engine.calls, [("a red stool", 777)])
        self.assertEqual(scene.still_seed, 777)


class PipelineSeedTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        self.project_dir = self.tmp / "film"
        self.project = scenes_mod.Project(
            name="Test", idea="A stool.",
            scenes=[scenes_mod.Scene(prompt="a stool",
                                     narration="A stool sits here."),
                    scenes_mod.Scene(prompt="a hammer",
                                     narration="A hammer waits.")])
        # As in test_pipeline: without the subtitles filter, a found
        # caption font would break the assembly step.
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

    def test_build_records_seeds_for_first_pictures(self):
        engine = RecordingEngine()
        with mock.patch.object(voice, "synthesize",
                               side_effect=_fake_synthesize):
            pipeline.build_film(
                self.project, self.project_dir, engine,
                save_cb=lambda p: scenes_mod.save_project(
                    p, self.project_dir))
        seeds = [s.still_seed for s in self.project.scenes]
        self.assertTrue(all(seed > 0 for seed in seeds))
        self.assertEqual(len(set(seeds)), 2)
        for prompt, seed in engine.calls:
            scene = next(s for s in self.project.scenes
                         if s.prompt == prompt)
            self.assertEqual(seed, scene.still_seed)
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertEqual([s.still_seed for s in loaded.scenes], seeds)


# --- Item 28: a seed can be typed on the remake path ----------------------


class ParseSeedTests(unittest.TestCase):
    def test_parse_seed(self):
        from hicksfield.gui import StillsWindow
        self.assertIsNone(StillsWindow._parse_seed(""))
        self.assertIsNone(StillsWindow._parse_seed("   "))
        self.assertEqual(StillsWindow._parse_seed("42"), 42)
        self.assertEqual(StillsWindow._parse_seed("  4242  "), 4242)
        with self.assertRaises(ValueError):
            StillsWindow._parse_seed("abc")
        with self.assertRaises(ValueError):
            StillsWindow._parse_seed("-3")
        with self.assertRaises(ValueError):
            StillsWindow._parse_seed("4.5")


class TypedSeedGuiTests(GuiBase):
    def _one_scene_with_picture(self):
        from hicksfield.gui import StillsWindow
        scene = scenes_mod.Scene(prompt="a stool",
                                 narration="A stool sits here.")
        paths = scenes_mod.ensure_project_dirs(self.state.project_dir)
        write_png(paths["stills"] / "scene-01.png")
        scene.still = "stills/scene-01.png"
        scene.still_prompt = "a stool"
        self.state.project.scenes = [scene]
        self.state.save()
        window = self._show(StillsWindow)
        return window, scene

    def test_typed_seed_is_used_for_the_remake(self):
        window, scene = self._one_scene_with_picture()
        engine = RecordingEngine()
        with mock.patch.object(stills, "preferred_engine",
                               return_value=engine):
            # Select inside the mock: the Remake button only enables
            # when an engine is ready.
            window.still_list.setCurrentRow(0)
            window.seed_edit.setText("4242")
            window.remake_button.click()
            self.assertTrue(wait_for(
                lambda: "Remade the picture for scene 1" in
                window.status_label.text(), 5000))
        self.assertEqual(engine.calls, [("a stool", 4242)])
        self.assertEqual(scene.still_seed, 4242)

    def test_junk_seed_is_refused_plainly_and_no_job_starts(self):
        window, _scene = self._one_scene_with_picture()
        engine = RecordingEngine()
        with mock.patch.object(stills, "preferred_engine",
                               return_value=engine):
            window.still_list.setCurrentRow(0)
            window.seed_edit.setText("abc")
            window.remake_button.click()
            self.assertIn("not a number", window.status_label.text())
            self.assertEqual(engine.calls, [])
            self.assertIsNone(window._worker)

    def test_preview_shows_the_current_seed_as_a_hint(self):
        window, scene = self._one_scene_with_picture()
        scene.still_seed = 777
        window._show_preview(0)
        self.assertIn("777", window.seed_edit.placeholderText())


# --- Item 29: the Stills RAM line is live ---------------------------------


class RamLineTests(GuiBase):
    def test_ram_line_uses_the_machine_strip_format(self):
        from hicksfield.gui import StillsWindow
        from hicksfield import machine
        window = self._show(StillsWindow)
        with mock.patch.object(machine, "mem_available_mb",
                               return_value=407):
            window._refresh_ram()
            self.assertIn("about 407 MB", window._ram_label.text())
            self.assertNotIn("0 GB", window._ram_label.text())
        with mock.patch.object(machine, "mem_available_mb",
                               return_value=2048):
            window._refresh_ram()
            self.assertIn("about 2 GB", window._ram_label.text())

    def test_ram_line_refreshes_with_the_window(self):
        from hicksfield.gui import StillsWindow
        from hicksfield import machine
        window = self._show(StillsWindow)
        with mock.patch.object(machine, "mem_available_mb",
                               return_value=512):
            window._refresh()
            self.assertIn("about 512 MB", window._ram_label.text())

    def test_ram_timer_is_running(self):
        from hicksfield.gui import StillsWindow
        window = self._show(StillsWindow)
        self.assertTrue(window._ram_timer.isActive())


# --- Item 35: thumbnails are cached ---------------------------------------


class ThumbnailCacheTests(GuiBase):
    def _two_scenes_with_pictures(self):
        from hicksfield.gui import StillsWindow
        scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here."),
            scenes_mod.Scene(prompt="a hammer",
                             narration="A hammer waits."),
        ]
        paths = scenes_mod.ensure_project_dirs(self.state.project_dir)
        for index, scene in enumerate(scenes):
            write_png(paths["stills"] / f"scene-0{index + 1}.png")
            scene.still = f"stills/scene-0{index + 1}.png"
            scene.still_prompt = scene.prompt
        self.state.project.scenes = scenes
        self.state.save()
        window = self._show(StillsWindow)
        # List 5 item 23: thumbnails decode off the GUI thread and
        # land in the cache as they finish. Wait for both before
        # asserting on the cache.
        from tests.helpers import wait_for
        self.assertTrue(wait_for(
            lambda: len(window._thumb_cache) == 2, timeout_ms=10000))
        return window

    def test_refresh_reuses_cached_thumbnails(self):
        window = self._two_scenes_with_pictures()
        window._refresh()
        first_icons = dict(window._thumb_cache)
        self.assertEqual(len(first_icons), 2)
        window._refresh()
        self.assertEqual(len(window._thumb_cache), 2)
        for key, icon in first_icons.items():
            self.assertIs(window._thumb_cache[key], icon)

    def test_a_changed_picture_gets_a_fresh_thumbnail(self):
        from tests.helpers import wait_for
        window = self._two_scenes_with_pictures()
        window._refresh()
        first_icons = dict(window._thumb_cache)
        path = self.state.project_dir / "stills" / "scene-01.png"
        with open(path, "ab") as handle:
            handle.write(b"CHANGED")
        os.utime(path, (time.time() + 2, time.time() + 2))
        window._refresh()
        # The changed picture's new thumbnail decodes off-thread;
        # wait for it to land.
        self.assertTrue(wait_for(
            lambda: len(window._thumb_cache) == 2, timeout_ms=10000))
        changed = [icon for key, icon in window._thumb_cache.items()
                   if "scene-01.png" in key[0]]
        self.assertEqual(len(changed), 1)
        old = [icon for key, icon in first_icons.items()
               if "scene-01.png" in key[0]]
        self.assertIsNot(changed[0], old[0])
        # The untouched picture kept its cached icon.
        kept = [icon for key, icon in window._thumb_cache.items()
                if "scene-02.png" in key[0]]
        old_kept = [icon for key, icon in first_icons.items()
                    if "scene-02.png" in key[0]]
        self.assertIs(kept[0], old_kept[0])

    def test_removed_pictures_leave_the_cache(self):
        window = self._two_scenes_with_pictures()
        window._refresh()
        self.assertEqual(len(window._thumb_cache), 2)
        self.state.project.scenes = self.state.project.scenes[:1]
        window._refresh()
        self.assertEqual(len(window._thumb_cache), 1)


# --- Item 41: the FastSD tarball download is verified, with progress ------


class TarballDownloadTests(TempEnvTestCase):
    def _engine(self):
        return stills.FastSDCPUEngine(
            base_dir=self.tmp / "fast",
            model_dir=self.tmp / "model")

    def _write_tarball(self, dest):
        inner = self.tmp / "inner"
        src = inner / f"fastsdcpu-{stills.FASTSD_CPU_COMMIT}" / "src"
        src.mkdir(parents=True)
        (src / "app.py").write_text("x", encoding="utf-8")
        backend = (inner / f"fastsdcpu-{stills.FASTSD_CPU_COMMIT}"
                   / "src" / "backend" / "pipelines")
        backend.mkdir(parents=True)
        (backend / "lcm.py").write_text(
            stills.PIPELINE_VARIANT_PATCHED, encoding="utf-8")
        (src / "app.py").write_text(stills.CLI_SAVE_PATCHED,
                                    encoding="utf-8")
        with tarfile.open(dest, "w:gz") as archive:
            archive.add(inner / f"fastsdcpu-{stills.FASTSD_CPU_COMMIT}",
                        arcname=f"fastsdcpu-{stills.FASTSD_CPU_COMMIT}")
        return dest.stat().st_size

    def test_checkout_download_is_checked_and_cancellable(self):
        engine = self._engine()
        size = self._write_tarball(self.tmp / "real.tar.gz")
        seen = {}

        def _fake_download(url, dest, expected_size=None,
                           progress_cb=None, cancel_event=None):
            seen["url"] = url
            seen["expected_size"] = expected_size
            seen["cancel_event"] = cancel_event
            seen["progress_cb"] = progress_cb is not None
            data = (self.tmp / "real.tar.gz").read_bytes()
            Path(dest).write_bytes(data)
            if progress_cb is not None:
                progress_cb(len(data), len(data))

        progress_calls = []
        cancel = threading.Event()
        with mock.patch.object(stills, "head_content_length",
                               return_value=size), \
                mock.patch.object(stills, "download",
                                  side_effect=_fake_download):
            engine._setup_checkout(
                lambda *a: progress_calls.append(a), cancel)
        self.assertEqual(seen["url"], stills.FASTSD_CPU_TARBALL)
        self.assertEqual(seen["expected_size"], size)
        self.assertIs(seen["cancel_event"], cancel)
        self.assertTrue(seen["progress_cb"])
        self.assertTrue(engine.cli_script.exists())
        fractions = [call[0] for call in progress_calls
                     if call[0] is not None]
        self.assertTrue(fractions)
        self.assertLessEqual(max(fractions), 1.0)

    def test_checkout_download_can_be_cancelled(self):
        engine = self._engine()

        def _fake_download(*args, **kwargs):
            raise CancelledError()

        with mock.patch.object(stills, "head_content_length",
                               return_value=100), \
                mock.patch.object(stills, "download",
                                  side_effect=_fake_download):
            with self.assertRaises(CancelledError):
                engine._setup_checkout(lambda *a: None,
                                       threading.Event())

    def test_damaged_tarball_is_refused(self):
        engine = self._engine()

        def _fake_download(url, dest, **kwargs):
            Path(dest).write_bytes(b"not a tarball at all")

        with mock.patch.object(stills, "head_content_length",
                               return_value=None), \
                mock.patch.object(stills, "download",
                                  side_effect=_fake_download):
            with self.assertRaises(DownloadError) as ctx:
                engine._setup_checkout(lambda *a: None,
                                       threading.Event())
        self.assertIn("damaged", ctx.exception.plain_text())
        self.assertFalse((engine.base_dir / "fastsdcpu.tar.gz").exists())


# --- Item 47: a timeout says it timed out ---------------------------------


class TimeoutMessageTests(TempEnvTestCase):
    def _ready_fast_engine(self):
        engine = stills.FastSDCPUEngine(
            base_dir=self.tmp / "fast",
            model_dir=self.tmp / "model")
        engine.cli_script.parent.mkdir(parents=True, exist_ok=True)
        engine.cli_script.write_text("x", encoding="utf-8")
        engine.cli_python.parent.mkdir(parents=True, exist_ok=True)
        engine.cli_python.write_text("x", encoding="utf-8")
        for name, _url in engine.model_files():
            dest = engine.model_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
        engine.results_dir.mkdir(parents=True, exist_ok=True)
        return engine

    def test_fast_generate_timeout_says_it_timed_out(self):
        engine = self._ready_fast_engine()
        with mock.patch.object(stills, "run_logged",
                               return_value=(None, RUN_TIMEOUT_TAIL)):
            with self.assertRaises(HicksFieldError) as ctx:
                engine.generate("a stool", self.tmp / "out.png",
                                seed=1, force=True)
        text = ctx.exception.plain_text()
        self.assertIn("took too long", text)
        self.assertNotIn("stopped with an error", text)

    def test_fast_generate_error_still_says_error(self):
        engine = self._ready_fast_engine()
        with mock.patch.object(stills, "run_logged",
                               return_value=(1, "boom")):
            with self.assertRaises(HicksFieldError) as ctx:
                engine.generate("a stool", self.tmp / "out.png",
                                seed=1, force=True)
        self.assertIn("stopped with an error",
                      ctx.exception.plain_text())

    def test_steady_generate_timeout_says_it_timed_out(self):
        engine = stills.SDCppEngine(base_dir=self.tmp / "sdcpp")
        model = self.tmp / "model.gguf"
        model.write_bytes(b"x")
        with mock.patch.object(engine, "find_binary",
                               return_value=Path("/bin/true")), \
                mock.patch.object(engine, "find_model",
                                  return_value=(model, True)), \
                mock.patch.object(stills, "run_logged",
                                  return_value=(None, RUN_TIMEOUT_TAIL)):
            with self.assertRaises(HicksFieldError) as ctx:
                engine.generate("a stool", self.tmp / "out.png",
                                seed=1, force=True)
        text = ctx.exception.plain_text()
        self.assertIn("took too long", text)
        self.assertNotIn("stopped with an error", text)

    def test_steady_build_configure_timeout_says_it_timed_out(self):
        engine = stills.SDCppEngine(base_dir=self.tmp / "sdcpp")
        with mock.patch.object(stills.shutil, "which",
                               return_value="/usr/bin/cmake"), \
                mock.patch.object(stills, "run_logged",
                                  return_value=(None, RUN_TIMEOUT_TAIL)):
            with self.assertRaises(HicksFieldError) as ctx:
                engine._setup_build(lambda *a: None,
                                    threading.Event())
        text = ctx.exception.plain_text()
        self.assertIn("took too long", text)
        self.assertNotIn("could not be set up", text)

    def test_steady_build_compile_timeout_says_it_timed_out(self):
        engine = stills.SDCppEngine(base_dir=self.tmp / "sdcpp")
        with mock.patch.object(stills.shutil, "which",
                               return_value="/usr/bin/cmake"), \
                mock.patch.object(
                    stills, "run_logged",
                    side_effect=[(0, ""),
                                 (None, RUN_TIMEOUT_TAIL)]):
            with self.assertRaises(HicksFieldError) as ctx:
                engine._setup_build(lambda *a: None,
                                    threading.Event())
        text = ctx.exception.plain_text()
        self.assertIn("took too long", text)
        self.assertNotIn("did not build", text)

    def test_steady_build_error_still_says_error(self):
        engine = stills.SDCppEngine(base_dir=self.tmp / "sdcpp")
        with mock.patch.object(stills.shutil, "which",
                               return_value="/usr/bin/cmake"), \
                mock.patch.object(stills, "run_logged",
                                  return_value=(1, "boom")):
            with self.assertRaises(HicksFieldError) as ctx:
                engine._setup_build(lambda *a: None,
                                    threading.Event())
        text = ctx.exception.plain_text()
        self.assertIn("could not be set up", text)
        self.assertNotIn("took too long", text)


class EmptyPromptGuardTests(GuiBase):
    """Item 6 (Stills half): an empty prompt never reaches the engine."""

    def test_make_all_refuses_an_empty_prompt(self):
        from hicksfield.errors import HicksFieldError
        from hicksfield.gui import StillsWindow
        scene = scenes_mod.Scene(prompt="   ",
                                 narration="A stool sits here.")
        self.state.project.scenes = [scene]
        self.state.save()
        window = self._show(StillsWindow)
        engine = RecordingEngine()
        with self.assertRaises(HicksFieldError) as caught:
            window._make_all(engine, lambda *a: None, threading.Event())
        self.assertIn("no picture words", caught.exception.message)
        self.assertEqual(engine.calls, [])

    def test_remake_refuses_an_empty_prompt(self):
        from hicksfield.errors import HicksFieldError
        from hicksfield.gui import StillsWindow
        scene = scenes_mod.Scene(prompt="",
                                 narration="A stool sits here.")
        self.state.project.scenes = [scene]
        self.state.save()
        window = self._show(StillsWindow)
        engine = RecordingEngine()
        with self.assertRaises(HicksFieldError) as caught:
            window._remake_one(engine, 0, 42, lambda *a: None,
                               threading.Event())
        self.assertIn("no picture words", caught.exception.message)
        self.assertEqual(engine.calls, [])


if __name__ == "__main__":
    unittest.main()
