"""Tests for HicksField v0.4.0, Group D (Voice and Export).

Task List 3 items 6, 13, 15, 20, 26, 30, 36, 37, 44, 45.
"""

import os
import time
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QMessageBox

from hicksfield import pipeline, scenes as scenes_mod, video, voice
from hicksfield.errors import EngineNotReadyError, HicksFieldError
from hicksfield.gui import AppState, ExportWindow, VoiceWindow
from hicksfield.stills import StillEngine
from tests.helpers import (
    TempEnvTestCase,
    app,
    require_qt,
    wait_for,
    write_png,
    write_wav,
)


def _fake_synthesize(text, out_path, cancel_event=None,
                     voice_id=None):
    path = write_wav(out_path, seconds=1.0)
    return path, 1.0


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


class RecordingEngine(FakeEngine):
    """A fake engine that records every prompt it is given."""

    def __init__(self):
        self.calls = []

    def generate(self, prompt, out_path, seed=-1, cancel_event=None,
                 force=False):
        self.calls.append(prompt)
        return super().generate(prompt, out_path, seed=seed,
                                cancel_event=cancel_event, force=force)


class NotReadyEngine(StillEngine):
    """An engine that is not set up: generate must never be called."""

    key = "stub"
    label = "Stub stills"
    description = "A test engine that is not set up."

    def __init__(self):
        self.calls = []

    @property
    def required_ram_mb(self):
        return 0

    def is_ready(self):
        return False, "Not set up yet."

    def setup(self, report=None, cancel_event=None):
        return None

    def generate(self, prompt, out_path, seed=-1, cancel_event=None,
                 force=False):
        self.calls.append(prompt)
        raise AssertionError("generate must not be called")


class EmptyLineTests(TempEnvTestCase):
    """Item 6: a scene with no spoken line stays silent, and an
    empty line never reaches the voice program."""

    def test_empty_line_is_silence_without_piper(self):
        # No piper anywhere: the empty line must not need one.
        os.environ["HICKSFIELD_PIPER"] = str(self.tmp / "no-piper")
        out = self.tmp / "out.wav"
        path, duration = voice.synthesize("", out)
        self.assertEqual(path, out)
        self.assertAlmostEqual(duration, 1.0, places=2)
        self.assertTrue(out.exists())
        self.assertAlmostEqual(voice.wav_duration(out), 1.0, places=2)

    def test_whitespace_only_line_is_silence(self):
        os.environ["HICKSFIELD_PIPER"] = str(self.tmp / "no-piper")
        out = self.tmp / "out.wav"
        _path, duration = voice.synthesize("   \n  ", out)
        self.assertAlmostEqual(duration, 1.0, places=2)
        self.assertTrue(out.exists())

    def test_empty_line_never_starts_piper(self):
        with mock.patch("subprocess.Popen") as popen:
            voice.synthesize("", self.tmp / "out.wav")
        popen.assert_not_called()

    def test_nonempty_line_still_needs_the_voice(self):
        with mock.patch.object(voice, "find_piper", return_value=None):
            with self.assertRaises(EngineNotReadyError):
                voice.synthesize("hello", self.tmp / "out.wav")

    def test_traceback_from_piper_is_never_shown(self):
        # The real piper failure for empty input is a traceback on
        # stderr; even when a traceback does arrive, the user sees
        # plain words, never the traceback.
        fake_piper = self.tmp / "piper"
        fake_piper.write_text("x", encoding="utf-8")
        os.environ["HICKSFIELD_PIPER"] = str(fake_piper)
        voice_dir = self.tmp / "voice"
        os.environ["HICKSFIELD_VOICE_DIR"] = str(voice_dir)
        folder = voice.voice_dir()
        folder.mkdir(parents=True, exist_ok=True)
        for name, info in voice.VOICES[voice.VOICE_ID]["files"].items():
            with open(folder / name, "wb") as handle:
                handle.truncate(info["size"])
        proc = mock.Mock()
        proc.communicate.return_value = (
            "", "Traceback (most recent call last):\n"
            '  File "piper", line 1, in <module>\n'
            "wave.Error: # channels not specified\n")
        proc.returncode = 1
        with mock.patch.object(voice, "_sha256_file",
                               return_value=voice.VOICE_MODEL_SHA256), \
                mock.patch("subprocess.Popen", return_value=proc):
            with self.assertRaises(HicksFieldError) as caught:
                voice.synthesize("hello", self.tmp / "out.wav")
        shown = caught.exception.plain_text()
        self.assertNotIn("Traceback", shown)
        self.assertNotIn("wave.Error", shown)
        self.assertEqual(caught.exception.hint, "Try again.")


class LegacyVoiceTests(TempEnvTestCase):
    """Item 15: a line with no recorded voice counts as the usual
    voice, so it goes out of date when the voice changes."""

    def setUp(self):
        super().setUp()
        self.project_dir = self.tmp / "film"
        paths = scenes_mod.ensure_project_dirs(self.project_dir)
        (paths["stills"] / "scene-01.png").write_bytes(b"fake png")
        (paths["audio"] / "scene-01.wav").write_bytes(b"fake wav")
        (paths["clips"] / "scene-01.mp4").write_bytes(b"fake mp4")
        self.scene = scenes_mod.Scene(
            prompt="a stool", narration="A stool sits here.",
            still="stills/scene-01.png", audio="audio/scene-01.wav",
            clip="clips/scene-01.mp4", still_prompt="a stool",
            voice_text="A stool sits here.", clip_motion="zoomin")

    def test_missing_voice_is_fresh_under_the_usual_voice(self):
        self.assertFalse(scenes_mod.voice_is_stale(
            self.scene, self.project_dir, "en_US-lessac-medium"))

    def test_missing_voice_is_stale_under_another_voice(self):
        self.assertTrue(scenes_mod.voice_is_stale(
            self.scene, self.project_dir, "en_US-ryan-high"))

    def test_missing_voice_makes_the_clip_stale_too(self):
        self.assertTrue(scenes_mod.clip_is_stale(
            self.scene, self.project_dir, "en_US-ryan-high", "zoomin"))
        self.assertFalse(scenes_mod.clip_is_stale(
            self.scene, self.project_dir, "en_US-lessac-medium",
            "zoomin"))

    def test_no_voice_given_keeps_old_behavior(self):
        self.assertFalse(scenes_mod.voice_is_stale(
            self.scene, self.project_dir))


class PipelineVoiceExportTests(TempEnvTestCase):
    """Items 6, 15, 26 at the pipeline level."""

    def setUp(self):
        super().setUp()
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        self.project_dir = self.tmp / "film"

    def _build(self, project, engine=None, synth=None):
        synth = synth or _fake_synthesize
        with mock.patch.object(voice, "synthesize",
                               side_effect=synth):
            return pipeline.build_film(
                project, self.project_dir, engine or FakeEngine(),
                save_cb=lambda p: scenes_mod.save_project(
                    p, self.project_dir))

    def test_silent_film_builds_with_the_real_voice_module(self):
        # No piper, no voice setup: empty lines are silence, and the
        # film still builds end to end.
        os.environ["HICKSFIELD_PIPER"] = str(self.tmp / "no-piper")
        project = scenes_mod.Project(
            idea="Quiet.",
            scenes=[
                scenes_mod.Scene(prompt="a stool", narration=""),
                scenes_mod.Scene(prompt="a hammer", narration=""),
            ])
        result = pipeline.build_film(
            project, self.project_dir, FakeEngine(),
            save_cb=lambda p: scenes_mod.save_project(
                p, self.project_dir))
        self.assertEqual(result["voice_made"], 2)
        self.assertEqual(result["durations"], [3, 3])
        film = self.project_dir / "film.mp4"
        self.assertTrue(film.exists())
        self.assertGreater(film.stat().st_size, 0)
        # A second build keeps the silent lines: they are recorded
        # as spoken, not re-made every time.
        result = pipeline.build_film(
            project, self.project_dir, FakeEngine(),
            save_cb=lambda p: scenes_mod.save_project(
                p, self.project_dir))
        self.assertEqual(result["voice_made"], 0)
        self.assertEqual(result["voice_skipped"], 2)

    def test_empty_prompt_is_a_plain_error_and_keeps_partial_work(self):
        engine = RecordingEngine()
        project = scenes_mod.Project(
            idea="A stool.",
            scenes=[
                scenes_mod.Scene(prompt="a stool",
                                 narration="A stool sits here."),
                scenes_mod.Scene(prompt="",
                                 narration="A hammer waits."),
            ])
        with self.assertRaises(HicksFieldError) as caught:
            self._build(project, engine)
        shown = caught.exception.plain_text()
        self.assertIn("Scene 2 has no picture words yet", shown)
        self.assertIn("Scenes window", shown)
        self.assertNotIn("Traceback", shown)
        # The empty prompt never reached the engine.
        self.assertEqual(engine.calls, ["a stool"])
        # The picture made before the empty line is kept on disk.
        loaded = scenes_mod.load_project(self.project_dir)
        self.assertTrue(loaded.scenes[0].still)
        self.assertTrue(
            (self.project_dir / loaded.scenes[0].still).exists())

    def test_empty_prompt_alone_never_reaches_the_engine(self):
        engine = RecordingEngine()
        project = scenes_mod.Project(
            idea="Nothing.",
            scenes=[scenes_mod.Scene(prompt="",
                                     narration="A line.")])
        with self.assertRaises(HicksFieldError):
            self._build(project, engine)
        self.assertEqual(engine.calls, [])

    def test_legacy_line_is_respoken_after_a_voice_change(self):
        paths = scenes_mod.ensure_project_dirs(self.project_dir)
        write_wav(paths["audio"] / "scene-01.wav")
        project = scenes_mod.Project(
            idea="A stool.", voice="en_US-ryan-high",
            scenes=[scenes_mod.Scene(
                prompt="a stool", narration="A stool sits here.",
                audio="audio/scene-01.wav",
                voice_text="A stool sits here.")])
        result = self._build(project)
        self.assertEqual(result["voice_made"], 1)
        self.assertEqual(project.scenes[0].voice_id,
                         "en_US-ryan-high")


class GuiBase(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        require_qt(self)
        from PySide6.QtCore import QSettings
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                          str(self.tmp / "qsettings"))
        os.environ["HICKSFIELD_FASTSD_DIR"] = str(self.tmp / "fast")
        os.environ["HICKSFIELD_SDCPP_DIR"] = str(self.tmp / "steady")
        os.environ["HICKSFIELD_OLLAMA_URL"] = "http://127.0.0.1:1"
        self.app = app()
        self.state = AppState()
        self.state.new_project("test film")
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.windows = []
        QTest.qWait(200)
        super().tearDown()

    def _show(self, cls):
        window = cls(self.state)
        self.windows.append(window)
        window.show()
        QTest.qWait(50)
        return window

    def _two_scenes(self, narration1="A stool sits here.",
                    narration2="A hammer waits.", audio=True,
                    stills=True, voice_id="en_US-lessac-medium"):
        """Two scenes with real asset files on disk."""
        scenes = [
            scenes_mod.Scene(prompt="a stool", narration=narration1),
            scenes_mod.Scene(prompt="a hammer", narration=narration2),
        ]
        project_dir = self.state.project_dir
        paths = scenes_mod.ensure_project_dirs(project_dir)
        for index, scene in enumerate(scenes):
            if stills:
                still_name = scenes_mod.scene_still_name(index)
                write_png(paths["stills"] / still_name)
                scene.still = f"stills/{still_name}"
                scene.still_prompt = scene.prompt
            if audio:
                audio_name = scenes_mod.scene_audio_name(index)
                write_wav(paths["audio"] / audio_name)
                scene.audio = f"audio/{audio_name}"
                scene.voice_text = scene.narration
                scene.voice_id = voice_id
        self.state.project.scenes = scenes
        self.state.save()
        return scenes

    def _row_label(self, window, row=0):
        widget = window.line_list.itemWidget(
            window.line_list.item(row))
        return widget.findChildren(QLabel)[0].text()


class VoiceSwitchWarningTests(GuiBase):
    """Item 36: switching voice warns when spoken lines exist."""

    def test_popup_pick_warns_and_no_keeps_the_voice(self):
        self._two_scenes()
        window = self._show(VoiceWindow)
        combo = window.voice_combo
        ryan = combo.findData("en_US-ryan-high")
        combo.showPopup()
        QTest.qWait(50)
        self.assertTrue(combo.view().isVisible())
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.No) as asked:
            combo.setCurrentIndex(ryan)
        asked.assert_called_once()
        text = asked.call_args[0][2]
        self.assertIn("2 spoken lines", text)
        self.assertIn("out of date", text)
        self.assertEqual(self.state.project.voice,
                         "en_US-lessac-medium")
        self.assertEqual(combo.currentData(), "en_US-lessac-medium")
        combo.hidePopup()

    def test_popup_pick_warns_and_yes_switches(self):
        scenes = self._two_scenes()
        window = self._show(VoiceWindow)
        combo = window.voice_combo
        ryan = combo.findData("en_US-ryan-high")
        combo.showPopup()
        QTest.qWait(50)
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.Yes):
            combo.setCurrentIndex(ryan)
        combo.hidePopup()
        self.assertEqual(self.state.project.voice, "en_US-ryan-high")
        self.assertIn("[out of date]", self._row_label(window, 0))
        self.assertTrue(window.make_button.isEnabled())
        self.assertEqual(scenes[0].voice_id, "en_US-lessac-medium")

    def test_switch_without_lines_asks_nothing(self):
        window = self._show(VoiceWindow)
        combo = window.voice_combo
        ryan = combo.findData("en_US-ryan-high")
        combo.showPopup()
        QTest.qWait(50)
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.Yes) as asked:
            combo.setCurrentIndex(ryan)
        asked.assert_not_called()
        self.assertEqual(self.state.project.voice, "en_US-ryan-high")
        combo.hidePopup()

    def test_programmatic_switch_asks_nothing(self):
        # A change made by code, with the picker closed, is not a
        # person about to lose work: no dialog, and the switch lands.
        self._two_scenes()
        window = self._show(VoiceWindow)
        combo = window.voice_combo
        ryan = combo.findData("en_US-ryan-high")
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.No) as asked:
            combo.setCurrentIndex(ryan)
        asked.assert_not_called()
        self.assertEqual(self.state.project.voice, "en_US-ryan-high")

    def test_keyboard_switch_warns_and_no_keeps_the_voice(self):
        self._two_scenes()
        window = self._show(VoiceWindow)
        combo = window.voice_combo
        combo.setFocus()
        QTest.qWait(50)
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.No) as asked:
            QTest.keyClick(combo, Qt.Key_Down)
        asked.assert_called_once()
        self.assertEqual(combo.currentIndex(), 0)
        self.assertEqual(self.state.project.voice,
                         "en_US-lessac-medium")
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.Yes):
            QTest.keyClick(combo, Qt.Key_Down)
        self.assertEqual(self.state.project.voice, "en_US-ryan-high")


class LegacyVoiceGuiTests(GuiBase):
    """Item 15 in the Voice window."""

    def test_legacy_line_goes_out_of_date_after_a_switch(self):
        self._two_scenes(voice_id="")
        window = self._show(VoiceWindow)
        self.assertIn("[spoken]", self._row_label(window, 0))
        self.assertFalse(window.make_button.isEnabled())
        ryan = window.voice_combo.findData("en_US-ryan-high")
        window.voice_combo.setCurrentIndex(ryan)
        self.assertIn("[out of date]", self._row_label(window, 0))
        self.assertTrue(window.make_button.isEnabled())


class VoicePlayNoDeviceTests(GuiBase):
    """Item 30: Play with no audio device fails plainly."""

    def test_play_with_no_device_says_so_and_never_starts(self):
        self._two_scenes()
        window = self._show(VoiceWindow)
        with mock.patch("hicksfield.gui._audio_output_available",
                        return_value=False):
            window._play_line(0)
        self.assertIn("No speaker or headphones",
                      window.status_label.text())
        self.assertIsNone(window._playing_row)

    def test_play_with_a_device_starts(self):
        self._two_scenes()
        window = self._show(VoiceWindow)
        window._player = mock.Mock()
        window._audio = mock.Mock()
        with mock.patch("hicksfield.gui._audio_output_available",
                        return_value=True):
            window._play_line(0)
        window._player.play.assert_called_once()
        self.assertEqual(window._playing_row, 0)
        self.assertIn("Playing scene 1", window.status_label.text())


class VoiceMakeSilentTests(GuiBase):
    """Items 6 and 26 in the Voice window: empty lines are made
    into silence with no voice set up."""

    def test_make_lines_with_only_empty_lines_needs_no_voice(self):
        self._two_scenes(narration1="", narration2="", audio=False,
                         stills=False)
        window = self._show(VoiceWindow)
        # The voice is definitively not ready: the gate must still
        # let the silent lines through.
        with mock.patch.object(
                voice, "is_ready",
                return_value=(False, "Not set up yet.")):
            window._make_lines()
        self.assertIsNotNone(window._worker)
        self.assertTrue(wait_for(lambda: window._worker is None,
                                 30000))
        for scene in self.state.project.scenes:
            self.assertTrue(scene.audio)
            self.assertTrue(
                (self.state.project_dir / scene.audio).exists())
        self.assertIn("Spoke 2 lines", window.status_label.text())


class ExportNoEngineTests(GuiBase):
    """Items 20 and 37: no engine ready means direction, not dead
    buttons, and every greyed button says why."""

    def test_no_engine_note_names_stills_and_opens_it(self):
        self._two_scenes(audio=False, stills=False)
        window = self._show(ExportWindow)
        self.assertEqual(
            window.engine_note.text(),
            "No picture engine is set up yet. Open Stills and press "
            "Set up.")
        self.assertFalse(window.open_stills_button.isHidden())
        opened = []
        self.state.open_window_requested.connect(opened.append)
        window.open_stills_button.click()
        self.assertEqual(opened, ["stills"])

    def test_build_buttons_explain_the_missing_engine(self):
        self._two_scenes(audio=False, stills=False)
        window = self._show(ExportWindow)
        self.assertFalse(window.fast_build.isEnabled())
        self.assertFalse(window.steady_build.isEnabled())
        self.assertIn("not set up yet",
                      window.fast_build.toolTip())
        self.assertIn("Stills", window.fast_build.toolTip())
        self.assertIn("steady engine",
                      window.steady_build.toolTip())

    def test_no_scenes_button_says_there_are_no_scenes(self):
        window = self._show(ExportWindow)
        self.assertFalse(window.fast_build.isEnabled())
        self.assertIn("no scenes yet",
                      window.fast_build.toolTip().lower())

    def test_pictures_ready_builds_without_an_engine(self):
        self._two_scenes(audio=False, stills=True)
        window = self._show(ExportWindow)
        self.assertTrue(window.fast_build.isEnabled())
        self.assertTrue(window.steady_build.isEnabled())
        self.assertEqual(
            window.fast_build.toolTip(),
            "Build the film, making pictures with the fast engine.")
        self.assertIn("every picture is already made",
                      window.engine_note.text())
        self.assertFalse(window.open_stills_button.isHidden())

    def test_refresh_mid_build_leaves_the_buttons_alone(self):
        self._two_scenes(audio=False, stills=True)
        window = self._show(ExportWindow)
        self.assertTrue(window.fast_build.isEnabled())
        window.fast_build.setEnabled(False)
        window.steady_build.setEnabled(False)
        window._worker = mock.Mock()
        window._refresh()
        self.assertFalse(window.fast_build.isEnabled())
        self.assertFalse(window.steady_build.isEnabled())
        self.assertEqual(window.fast_build.toolTip(),
                         "A build is already running.")
        window._worker = None
        window._refresh()
        self.assertTrue(window.fast_build.isEnabled())


class ExportProgressSummaryTests(GuiBase):
    """Item 13: the summary moves as the build lands pieces."""

    def test_progress_refreshes_the_summary(self):
        scenes = self._two_scenes(audio=False, stills=False)
        window = self._show(ExportWindow)
        self.assertIn("0 pictures ready",
                      window.summary_label.text())
        paths = scenes_mod.ensure_project_dirs(
            self.state.project_dir)
        for index, scene in enumerate(scenes):
            still_name = scenes_mod.scene_still_name(index)
            write_png(paths["stills"] / still_name)
            scene.still = f"stills/{still_name}"
            scene.still_prompt = scene.prompt
        window._on_progress("Making pictures", 0.5, "scene 2 of 2")
        self.assertIn("2 pictures ready",
                      window.summary_label.text())


class ExportFinishedLabelsTests(GuiBase):
    """Item 44: after a build, the elapsed time is past tense and
    the step label clears."""

    def test_finish_ui_reports_how_long_it_took(self):
        window = self._show(ExportWindow)
        window._started = time.time() - 65
        window.step_label.setText("Cutting the film together")
        window._finish_ui()
        self.assertEqual(window.elapsed_label.text(), "Took 1m 05s.")
        self.assertEqual(window.step_label.text(), "")
        self.assertIsNone(window._started)

    def test_finish_ui_after_an_error_says_stopped(self):
        window = self._show(ExportWindow)
        window._started = time.time() - 12
        window.step_label.setText("Making pictures")
        window._finish_ui("Stopped after")
        self.assertEqual(window.elapsed_label.text(),
                         "Stopped after 0m 12s.")
        self.assertEqual(window.step_label.text(), "")


class ExportPosterTests(GuiBase):
    """Item 45: a poster failure says so in the player area."""

    def test_failed_poster_shows_a_note_not_black(self):
        film = self.state.project_dir / scenes_mod.FILM_FILE
        film.write_bytes(b"not a film at all")
        window = self._show(ExportWindow)
        self.assertTrue(window._poster_failed)
        self.assertIs(window._player_stack.currentWidget(),
                      window.poster_note)
        self.assertEqual(window.poster_note.text(),
                         "No preview frame. The film itself is fine.")

    def test_no_film_shows_no_poster_note(self):
        window = self._show(ExportWindow)
        self.assertFalse(window._poster_failed)
        self.assertIs(window._player_stack.currentWidget(),
                      window.video_widget)


class ExportSilentFilmTests(GuiBase):
    """Items 20 and 26 end to end: a silent film with its pictures
    ready builds with no engine and no voice set up."""

    def test_silent_film_builds_with_no_engine_and_no_voice(self):
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        self._two_scenes(narration1="", narration2="", audio=False,
                         stills=True)
        window = self._show(ExportWindow)
        engine = NotReadyEngine()
        # The voice is definitively not ready: the gates must still
        # let the silent film through.
        with mock.patch.object(
                voice, "is_ready",
                return_value=(False, "Not set up yet.")):
            window._build(engine)
        self.assertIsNotNone(window._worker)
        self.assertEqual(window.status_label.text(),
                         "Building the film.")
        self.assertTrue(wait_for(lambda: window._worker is None,
                                 90000))
        film = self.state.project_dir / scenes_mod.FILM_FILE
        self.assertTrue(film.exists())
        self.assertGreater(film.stat().st_size, 0)
        self.assertEqual(window.status_label.text(), "Film ready.")
        self.assertEqual(engine.calls, [])
        # The finished labels read as finished (item 44).
        self.assertTrue(window.elapsed_label.text().startswith("Took"))
        self.assertEqual(window.step_label.text(), "")
        # The summary counted the finished pieces (item 13).
        self.assertIn("2 pictures ready",
                      window.summary_label.text())
        self.assertIn("2 spoken lines ready",
                      window.summary_label.text())
        self.assertIn("2 moving clips ready",
                      window.summary_label.text())


if __name__ == "__main__":
    unittest.main()
