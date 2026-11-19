"""v0.3 tests: task list 2 items 1-2, 3-4, 5 (GUI part), 9, 10, 15,
7, 8, 11, 12, 13, 14.

Items 1-2: the scene breakdown gives up plainly when the model goes
quiet or runs too long, reports a heartbeat while waiting, and keeps
Cancel working throughout; the model picker says whether a model fits
this PC and can chat, recommends one, and warns (never refuses) when
the picked model may not work. Items 3-4, 9, 10, 15: scene edits
autosave, stale assets are remade and marked, one picture can be
remade with a new seed, removing or editing a scene can be undone,
the Make buttons say when there is nothing to make, and ffmpeg can
be installed with one button. Items 7, 8, 11-14: the machine strip
refreshes while a job runs, home has one call to action, the Voice
window offers three voices, the breakdown offers Short, Medium, or
Long, each scene picks its camera move, and the Export player shows
the film's first frame while idle.
"""

import json
import socket
import threading
import time
import unittest
from unittest import mock

from PySide6.QtWidgets import QLabel, QMessageBox

from hicksfield import machine, ollama, scenes as scenes_mod, stills
from hicksfield import sysdeps, video, voice
from hicksfield.errors import CancelledError, HicksFieldError
from hicksfield.gui import (
    ExportWindow,
    MainWindow,
    ScenesWindow,
    StillsWindow,
    VoiceWindow,
)
from tests.helpers import (
    TempEnvTestCase,
    wait_for,
    write_png,
    write_wav,
)
from tests.test_gui import GuiBase
from tests.test_pipeline import FakeEngine

GB = 1024 * 1024 * 1024


class ModelInfoTests(TempEnvTestCase):
    def test_list_model_info_parses_tags(self):
        payload = {"models": [
            {"name": "small:2b", "size": 1500000000,
             "details": {"parameter_size": "2.0B"}},
            {"name": "big:7b", "size": 7000000000,
             "details": {"parameter_size": "7.0B"}},
            {"name": "bare"},
            {"size": 123},
            {"name": ""},
        ]}
        with mock.patch.object(ollama, "_request_json",
                               return_value=payload) as req:
            infos = ollama.list_model_info()
        req.assert_called_once_with("GET", "/api/tags", timeout=10)
        self.assertEqual([info["name"] for info in infos],
                         ["small:2b", "big:7b", "bare"])
        self.assertEqual(infos[0]["size"], 1500000000)
        self.assertEqual(infos[0]["parameter_size"], "2.0B")
        self.assertEqual(infos[1]["size"], 7000000000)
        self.assertEqual(infos[1]["parameter_size"], "7.0B")
        self.assertEqual(infos[2]["size"], 0)
        self.assertEqual(infos[2]["parameter_size"], "")

    def test_model_capabilities_parses_show(self):
        with mock.patch.object(
                ollama, "_request_json",
                return_value={"capabilities": ["chat", "completion"]}
                ) as req:
            caps = ollama.model_capabilities("small:2b")
        req.assert_called_once_with("POST", "/api/show",
                                    {"model": "small:2b"}, timeout=5)
        self.assertEqual(caps, ["chat", "completion"])

    def test_model_capabilities_unknown_on_error(self):
        with mock.patch.object(
                ollama, "_request_json",
                side_effect=HicksFieldError("nope")):
            self.assertEqual(ollama.model_capabilities("small:2b"), [])

    def test_model_capabilities_missing_key_is_unknown(self):
        with mock.patch.object(ollama, "_request_json",
                               return_value={}):
            self.assertEqual(ollama.model_capabilities("small:2b"), [])


class ModelFitsTests(unittest.TestCase):
    def test_small_model_fits(self):
        self.assertTrue(ollama.model_fits(1500000000, 8192))

    def test_big_model_does_not_fit(self):
        self.assertFalse(ollama.model_fits(5000000000, 8192))

    def test_exactly_half_fits(self):
        half = (8192 // 2) * 1024 * 1024
        self.assertTrue(ollama.model_fits(half, 8192))
        self.assertFalse(ollama.model_fits(half + 1, 8192))

    def test_unknown_size_fits(self):
        self.assertTrue(ollama.model_fits(0, 8192))
        self.assertTrue(ollama.model_fits(None, 8192))

    def test_unknown_machine_fits(self):
        self.assertTrue(ollama.model_fits(5000000000, 0))


class ModelCanChatTests(unittest.TestCase):
    def test_chat_can(self):
        self.assertTrue(ollama.model_can_chat(["chat"]))

    def test_completion_only_cannot(self):
        self.assertFalse(ollama.model_can_chat(["completion"]))

    def test_unknown_counts_as_able(self):
        self.assertTrue(ollama.model_can_chat([]))


class RecommendedModelTests(unittest.TestCase):
    SMALL = {"name": "small:2b", "size": 1500000000,
             "capabilities": ["chat"]}
    BIG = {"name": "big:7b", "size": 7000000000,
           "capabilities": ["chat"]}
    TINY_COMPLETION = {"name": "tiny:1b", "size": 500000000,
                       "capabilities": ["completion"]}

    def test_smallest_fitting_chat_model_wins(self):
        best = ollama.recommended_model(
            [self.BIG, self.SMALL, self.TINY_COMPLETION], 8192)
        self.assertEqual(best["name"], "small:2b")

    def test_completion_only_excluded(self):
        self.assertIsNone(ollama.recommended_model(
            [self.TINY_COMPLETION], 8192))

    def test_none_when_nothing_qualifies(self):
        self.assertIsNone(ollama.recommended_model(
            [self.BIG, self.TINY_COMPLETION], 8192))
        self.assertIsNone(ollama.recommended_model([], 8192))

    def test_unknown_capabilities_count_as_able(self):
        unknown = {"name": "mystery:2b", "size": 1000000000}
        # Unknown still counts as able: alone, it is recommended.
        best = ollama.recommended_model([unknown], 8192)
        self.assertEqual(best["name"], "mystery:2b")
        # But the best model that fits wins over an unknown one.
        best = ollama.recommended_model([unknown, self.SMALL], 8192)
        self.assertEqual(best["name"], "small:2b")


class _FakeStream:
    """A stand-in for an Ollama streaming response."""

    def __init__(self, lines, on_readline=None):
        self._lines = list(lines)
        self._on_readline = on_readline
        self.closed = False

    def readline(self):
        if self._on_readline is not None:
            self._on_readline()
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def close(self):
        self.closed = True


def _chunk(content, done=False):
    return (json.dumps(
        {"message": {"content": content}, "done": done})
        + "\n").encode("utf-8")


class _TimeoutStream:
    """A stream that never answers: every read times out."""

    def __init__(self):
        self.closed = False

    def readline(self):
        raise socket.timeout("timed out")

    def close(self):
        self.closed = True


class _EmptyChunkStream:
    """A stream that answers forever with no content at all."""

    def __init__(self):
        self.closed = False

    def readline(self):
        return _chunk("")

    def close(self):
        self.closed = True


class _SlowStartStream:
    """Times out a few times (after a sleep each time), then delivers."""

    def __init__(self, lines, timeouts=2, sleep_s=0.15):
        self._lines = list(lines)
        self._timeouts = timeouts
        self._sleep_s = sleep_s
        self.closed = False

    def readline(self):
        if self._timeouts > 0:
            self._timeouts -= 1
            time.sleep(self._sleep_s)
            raise socket.timeout("timed out")
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def close(self):
        self.closed = True


class StreamingDeadlineTests(TempEnvTestCase):
    def test_silence_fails_plainly(self):
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_TimeoutStream()):
            with self.assertRaises(HicksFieldError) as caught:
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None,
                    silence_limit_s=0.3)
        self.assertIn("went quiet", str(caught.exception))

    def test_total_limit_fails_plainly(self):
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_EmptyChunkStream()):
            with self.assertRaises(HicksFieldError) as caught:
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None,
                    total_limit_s=0.3)
        self.assertIn("too long", str(caught.exception))

    def test_heartbeat_reports_while_waiting(self):
        lines = [
            _chunk('[{"prompt": "a stool", "narration": "A stool."}]'),
            _chunk("", done=True),
        ]
        reports = []
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_SlowStartStream(lines)):
            scenes, _dropped, _raw = (
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None,
                    report=lambda _label, _frac, detail: reports.append(
                        detail),
                    heartbeat_s=0.1))
        self.assertEqual(len(scenes), 1)
        self.assertTrue(any("waiting" in detail for detail in reports),
                        reports)

    def test_cancel_answers_quickly(self):
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        try:
            started = time.monotonic()
            with mock.patch.object(
                    ollama.urllib.request, "urlopen",
                    return_value=_TimeoutStream()):
                with self.assertRaises(CancelledError):
                    ollama.break_into_scenes_streaming(
                        "an idea", "fake-model", lambda s: None,
                        cancel_event=cancel)
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            timer.cancel()


INFOS = [
    {"name": "small:2b", "size": int(1.5 * GB),
     "parameter_size": "2.0B", "capabilities": ["chat"]},
    {"name": "big:7b", "size": 7 * GB,
     "parameter_size": "7.0B", "capabilities": ["chat"]},
    {"name": "complete:1b", "size": 500 * 1024 * 1024,
     "parameter_size": "1.0B", "capabilities": ["completion"]},
]


class ModelPickerGuiTests(GuiBase):
    def setUp(self):
        super().setUp()
        # Pin the machine to 8 GB so "fits this PC" is about the
        # test data, not whatever machine runs the tests.
        self._mem_patch = mock.patch.object(
            machine, "mem_total_mb", return_value=8192)
        self._mem_patch.start()
        self.addCleanup(self._mem_patch.stop)
        self._mem_free_patch = mock.patch.object(
            machine, "mem_available_mb", return_value=8192)
        self._mem_free_patch.start()
        self.addCleanup(self._mem_free_patch.stop)

    def _ready_window(self):
        self.state.project.idea = "A stool in a workshop."
        window = self._show(ScenesWindow)
        # Let the Ollama check finish FIRST: its handler clears the
        # model list, so fake models added before it lands would
        # be wiped and the breakdown would refuse to start.
        self.assertTrue(wait_for(
            lambda: "not running" in window.ollama_label.text(),
            5000))
        window._on_models([dict(info) for info in INFOS])
        return window

    def _fake_stream(self, scenes):
        def _run(idea, model, on_scene, report=None,
                 cancel_event=None, timeout=180,
                 scene_length="medium"):
            for scene in scenes:
                on_scene(scene)
            return scenes, 0, "raw"

        return _run

    def test_models_listed_and_small_recommended(self):
        window = self._ready_window()
        self.assertEqual(window.model_combo.count(), 3)
        self.assertIn("Recommended", window.model_note.text())
        self.assertIn("small:2b", window.model_note.text())

    def test_big_model_note_says_too_big(self):
        window = self._ready_window()
        window.model_combo.setCurrentIndex(1)
        self.assertIn("too big for this PC", window.model_note.text())

    def test_small_model_note_says_fits(self):
        window = self._ready_window()
        window.model_combo.setCurrentIndex(0)
        self.assertIn("fits this PC", window.model_note.text())

    def test_completion_only_note_says_it_cannot(self):
        window = self._ready_window()
        window.model_combo.setCurrentIndex(2)
        self.assertIn("cannot break an idea into scenes",
                      window.model_note.text())

    def test_big_model_declined_starts_nothing(self):
        window = self._ready_window()
        window.model_combo.setCurrentIndex(1)
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.No):
            window._break_into_scenes()
        self.assertIsNone(window._worker)
        self.assertIn("Left it", window.status_label.text())

    def test_big_model_accepted_starts_breakdown(self):
        window = self._ready_window()
        window.model_combo.setCurrentIndex(1)
        scenes = [{"prompt": "a stool", "narration": "A stool."}]
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.Yes), \
                mock.patch.object(
                    ollama, "break_into_scenes_streaming",
                    side_effect=self._fake_stream(scenes)):
            window._break_into_scenes()
            self.assertTrue(wait_for(
                lambda: len(self.state.project.scenes) == 1, 5000))
        self.assertIn("Made 1 scene", window.status_label.text())

    def test_suitable_model_starts_without_any_question(self):
        window = self._ready_window()
        window.model_combo.setCurrentIndex(0)
        scenes = [{"prompt": "a stool", "narration": "A stool."}]

        def _no_question(*args, **kwargs):
            raise AssertionError("no question dialog should appear")

        with mock.patch.object(
                QMessageBox, "question",
                side_effect=_no_question), \
                mock.patch.object(
                    ollama, "break_into_scenes_streaming",
                    side_effect=self._fake_stream(scenes)):
            window._break_into_scenes()
            self.assertTrue(wait_for(
                lambda: len(self.state.project.scenes) == 1, 5000))
        self.assertIn("Made 1 scene", window.status_label.text())


class SceneAutosaveGuiTests(GuiBase):
    def _two_scenes(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here."),
            scenes_mod.Scene(prompt="a hammer",
                             narration="A hammer waits."),
        ]
        self.state.project_changed.emit()

    def test_edit_survives_scene_switch(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("a changed stool")
        window.narration_edit.setPlainText("A changed line.")
        window.scene_list.setCurrentRow(1)
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "a changed stool")
        self.assertEqual(self.state.project.scenes[0].narration,
                         "A changed line.")
        window.scene_list.setCurrentRow(0)
        self.assertEqual(window.prompt_edit.toPlainText(),
                         "a changed stool")
        self.assertEqual(window.narration_edit.toPlainText(),
                         "A changed line.")

    def test_edit_survives_refresh(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("a changed stool")
        window._refresh()
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "a changed stool")
        self.assertEqual(window.prompt_edit.toPlainText(),
                         "a changed stool")

    def test_edit_is_saved_to_disk_on_switch(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("a changed stool")
        window.scene_list.setCurrentRow(1)
        loaded = scenes_mod.load_project(self.state.project_dir)
        self.assertEqual(loaded.scenes[0].prompt, "a changed stool")

    def test_breakdown_commit_does_not_save_old_words(self):
        # The boxes may hold words typed before the breakdown; when
        # the breakdown replaces the scene list, those words must
        # not be autosaved into a brand new scene.
        self._two_scenes()
        self.state.project.idea = "A stool in a workshop."
        window = self._show(ScenesWindow)
        self.assertTrue(wait_for(
            lambda: "not running" in window.ollama_label.text(),
            5000))
        window._on_models([dict(info) for info in INFOS])
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("typed while waiting")
        window.model_combo.setCurrentIndex(0)
        scenes = [{"prompt": "a streamed stool",
                   "narration": "A streamed line."}]

        def _run(idea, model, on_scene, report=None,
                 cancel_event=None, timeout=180,
                 scene_length="medium"):
            for scene in scenes:
                on_scene(scene)
            return scenes, 0, "raw"

        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.Yes), \
                mock.patch.object(
                    ollama, "break_into_scenes_streaming",
                    side_effect=_run):
            window._break_into_scenes()
            self.assertTrue(wait_for(
                lambda: len(self.state.project.scenes) == 1, 5000))
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "a streamed stool")
        self.assertEqual(self.state.project.scenes[0].narration,
                         "A streamed line.")


class SceneUndoGuiTests(GuiBase):
    def _two_scenes(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here."),
            scenes_mod.Scene(prompt="a hammer",
                             narration="A hammer waits."),
        ]
        self.state.project_changed.emit()

    def test_undo_button_disabled_at_first(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        self.assertFalse(window.undo_button.isEnabled())

    def test_undo_shortcut_is_ctrl_z(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        self.assertEqual(window._undo_shortcut.key().toString(),
                         "Ctrl+Z")

    def test_undo_remove_restores_scene_and_assets(self):
        self._two_scenes()
        paths = scenes_mod.ensure_project_dirs(
            self.state.project_dir)
        write_png(paths["stills"] / "scene-01.png")
        write_wav(paths["audio"] / "scene-01.wav")
        scene = self.state.project.scenes[0]
        scene.still = "stills/scene-01.png"
        scene.audio = "audio/scene-01.wav"
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.Yes):
            window._remove_scene()
        self.assertEqual(len(self.state.project.scenes), 1)
        self.assertTrue(window.undo_button.isEnabled())
        window.undo_button.click()
        self.assertEqual(len(self.state.project.scenes), 2)
        restored = self.state.project.scenes[0]
        self.assertEqual(restored.prompt, "a stool")
        self.assertEqual(restored.still, "stills/scene-01.png")
        self.assertEqual(restored.audio, "audio/scene-01.wav")
        self.assertTrue(
            (self.state.project_dir / restored.still).exists())
        self.assertTrue(
            (self.state.project_dir / restored.audio).exists())
        self.assertFalse(window.undo_button.isEnabled())

    def test_undo_save_restores_words(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("a changed stool")
        window._save_scene()
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "a changed stool")
        window._undo()
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "a stool")
        self.assertEqual(window.prompt_edit.toPlainText(), "a stool")

    def test_undo_autosave_restores_words(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        window.prompt_edit.setPlainText("a changed stool")
        window.scene_list.setCurrentRow(1)
        window._undo()
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "a stool")

    def test_undo_stack_caps_at_twenty(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        for step in range(25):
            window.prompt_edit.setPlainText(f"edit {step}")
            window._save_scene()
        self.assertEqual(len(window._undo_stack), 20)
        window._undo()
        self.assertEqual(self.state.project.scenes[0].prompt,
                         "edit 23")


class AssetGuiBase(GuiBase):
    """GuiBase plus a project whose scenes have real asset files."""

    def _make_assets(self, scenes, provenance=True):
        project_dir = self.state.project_dir
        paths = scenes_mod.ensure_project_dirs(project_dir)
        for index, scene in enumerate(scenes):
            still_name = scenes_mod.scene_still_name(index)
            audio_name = scenes_mod.scene_audio_name(index)
            write_png(paths["stills"] / still_name)
            write_wav(paths["audio"] / audio_name)
            scene.still = f"stills/{still_name}"
            scene.audio = f"audio/{audio_name}"
            if provenance:
                scene.still_prompt = scene.prompt
                scene.voice_text = scene.narration
        self.state.project.scenes = scenes
        self.state.save()

    def _two_scenes(self, **kwargs):
        scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here."),
            scenes_mod.Scene(prompt="a hammer",
                             narration="A hammer waits."),
        ]
        self._make_assets(scenes, **kwargs)
        return scenes


class StaleGuiTests(AssetGuiBase):
    def test_stills_row_shows_out_of_date_after_prompt_edit(self):
        scenes = self._two_scenes()
        window = self._show(StillsWindow)
        self.assertIn("picture ready",
                      window.still_list.item(0).text())
        scenes[0].prompt = "a red stool"
        self.state.project_changed.emit()
        self.assertIn("picture out of date",
                      window.still_list.item(0).text())
        self.assertIn("picture ready",
                      window.still_list.item(1).text())

    def test_voice_row_shows_out_of_date_after_line_edit(self):
        scenes = self._two_scenes()
        window = self._show(VoiceWindow)
        widget = window.line_list.itemWidget(
            window.line_list.item(0))
        label = widget.findChildren(QLabel)[0]
        self.assertIn("[spoken]", label.text())
        scenes[0].narration = "A different line."
        self.state.project_changed.emit()
        widget = window.line_list.itemWidget(
            window.line_list.item(0))
        label = widget.findChildren(QLabel)[0]
        self.assertIn("[out of date]", label.text())

    def test_stills_make_button_disabled_when_all_made(self):
        self._two_scenes()
        window = self._show(StillsWindow)
        self.assertFalse(window.make_button.isEnabled())
        self.assertEqual(window.make_note.text(),
                         "All pictures are already made.")

    def test_stills_make_button_enabled_when_stale(self):
        scenes = self._two_scenes()
        window = self._show(StillsWindow)
        scenes[0].prompt = "a red stool"
        self.state.project_changed.emit()
        self.assertTrue(window.make_button.isEnabled())
        self.assertEqual(window.make_note.text(), "")

    def test_voice_make_button_disabled_when_all_made(self):
        self._two_scenes()
        window = self._show(VoiceWindow)
        self.assertFalse(window.make_button.isEnabled())
        self.assertEqual(window.make_note.text(),
                         "All spoken lines are already made.")

    def test_voice_make_button_enabled_when_stale(self):
        scenes = self._two_scenes()
        window = self._show(VoiceWindow)
        scenes[1].narration = "A different line."
        self.state.project_changed.emit()
        self.assertTrue(window.make_button.isEnabled())
        self.assertEqual(window.make_note.text(), "")

    def test_make_missing_with_nothing_to_do_says_so(self):
        self._two_scenes()
        window = self._show(StillsWindow)
        window._make_missing()
        self.assertIn("All pictures are already made.",
                      window.status_label.text())
        self.assertIsNone(window._worker)

    def test_make_lines_with_nothing_to_do_says_so(self):
        self._two_scenes()
        window = self._show(VoiceWindow)
        window._make_lines()
        self.assertIn("All spoken lines are already made.",
                      window.status_label.text())
        self.assertIsNone(window._worker)

    def test_export_counts_stale_as_to_make(self):
        scenes = self._two_scenes()
        window = self._show(ExportWindow)
        self.assertIn("2 pictures ready",
                      window.summary_label.text())
        self.assertIn("2 spoken lines ready",
                      window.summary_label.text())
        scenes[0].prompt = "a red stool"
        scenes[1].narration = "A different line."
        self.state.project_changed.emit()
        self.assertIn("1 picture ready",
                      window.summary_label.text())
        self.assertIn("1 spoken line ready",
                      window.summary_label.text())


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


class RemakeGuiTests(AssetGuiBase):
    def test_remake_button_disabled_without_engine(self):
        self._two_scenes()
        window = self._show(StillsWindow)
        window.still_list.setCurrentRow(0)
        self.assertFalse(window.remake_button.isEnabled())

    def test_remake_button_disabled_without_selection(self):
        self._two_scenes()
        engine = RecordingEngine()
        with mock.patch.object(stills, "preferred_engine",
                               return_value=engine):
            window = self._show(StillsWindow)
            self.assertFalse(window.remake_button.isEnabled())
            window.still_list.setCurrentRow(0)
            self.assertTrue(window.remake_button.isEnabled())

    def test_remake_regenerates_with_new_seed(self):
        scenes = self._two_scenes()
        scenes[0].still_seed = 111
        self.state.save()
        engine = RecordingEngine()
        with mock.patch.object(stills, "preferred_engine",
                               return_value=engine):
            window = self._show(StillsWindow)
            window.still_list.setCurrentRow(0)
            self.assertIn("Seed 111",
                          window.preview_caption.text())
            window.remake_button.click()
            self.assertTrue(wait_for(
                lambda: "Remade the picture for scene 1" in
                window.status_label.text(), 5000))
        scene = self.state.project.scenes[0]
        self.assertNotEqual(scene.still_seed, 111)
        self.assertGreater(scene.still_seed, 0)
        self.assertEqual(scene.still_prompt, "a stool")
        self.assertEqual(engine.calls, [("a stool", scene.still_seed)])
        self.assertTrue(
            (self.state.project_dir / scene.still).exists())
        self.assertIn(f"Seed {scene.still_seed}",
                      window.preview_caption.text())
        loaded = scenes_mod.load_project(self.state.project_dir)
        self.assertEqual(loaded.scenes[0].still_seed,
                         scene.still_seed)

    def test_stale_remake_keeps_recorded_seed(self):
        scenes = self._two_scenes()
        scenes[0].still_seed = 777
        scenes[0].prompt = "a red stool"
        engine = RecordingEngine()
        with mock.patch.object(stills, "preferred_engine",
                               return_value=engine):
            window = self._show(StillsWindow)
            window._make_missing()
            self.assertTrue(wait_for(
                lambda: "Made 1 picture" in
                window.status_label.text(), 5000))
        self.assertEqual(engine.calls, [("a red stool", 777)])
        self.assertEqual(scenes[0].still_seed, 777)
        self.assertEqual(scenes[0].still_prompt, "a red stool")


class FfmpegInstallGuiTests(GuiBase):
    def _missing_ffmpeg(self):
        return (mock.patch.object(video, "find_ffmpeg",
                                  return_value=None),
                mock.patch.object(video, "ffmpeg_version_short",
                                  return_value=None))

    def test_install_button_shown_when_ffmpeg_missing(self):
        find_patch, version_patch = self._missing_ffmpeg()
        with find_patch, version_patch:
            window = self._show(ExportWindow)
            self.assertTrue(
                window.install_ffmpeg_button.isVisible())
            self.assertIn("ffmpeg is missing",
                          window.ffmpeg_label.text())
            for text in (window.ffmpeg_label.text(),
                         window.status_label.text()):
                self.assertNotIn("sudo", text)

    def test_install_button_hidden_when_ffmpeg_present(self):
        with mock.patch.object(video, "ffmpeg_version_short",
                               return_value="7.0"):
            window = self._show(ExportWindow)
            self.assertFalse(
                window.install_ffmpeg_button.isVisible())
            self.assertIn("ffmpeg is ready",
                          window.ffmpeg_label.text())

    def test_install_button_runs_install_and_reports_done(self):
        find_patch, version_patch = self._missing_ffmpeg()
        with find_patch, version_patch:
            window = self._show(ExportWindow)
            calls = {}

            def _fake_install(packages, progress_cb=None,
                              done_cb=None, error_cb=None):
                calls["packages"] = packages
                if progress_cb is not None:
                    progress_cb("Asking for permission.")
                if done_cb is not None:
                    done_cb()
                return mock.Mock()

            with mock.patch.object(
                    sysdeps, "install_packages_async",
                    side_effect=_fake_install):
                window.install_ffmpeg_button.click()
            self.assertEqual(calls["packages"], ["ffmpeg"])
            self.assertIn("ffmpeg is installed",
                          window.status_label.text())

    def test_install_declined_reports_plainly(self):
        find_patch, version_patch = self._missing_ffmpeg()
        with find_patch, version_patch:
            window = self._show(ExportWindow)

            def _fake_install(packages, progress_cb=None,
                              done_cb=None, error_cb=None):
                if error_cb is not None:
                    error_cb("the permission step was cancelled")
                return mock.Mock()

            with mock.patch.object(
                    sysdeps, "install_packages_async",
                    side_effect=_fake_install):
                window.install_ffmpeg_button.click()
            self.assertIn("ffmpeg was not installed",
                          window.status_label.text())
            self.assertIn("permission step was cancelled",
                          window.status_label.text())
            self.assertTrue(
                window.install_ffmpeg_button.isEnabled())

    def test_home_checklist_points_at_export(self):
        find_patch, version_patch = self._missing_ffmpeg()
        with find_patch, version_patch:
            window = self._show(MainWindow)
            _mark, label = window._checklist_rows["ffmpeg"]
            self.assertIn("Open Export", label.text())
            self.assertNotIn("sudo", label.text())


class MachineStripTimerTests(GuiBase):
    """Item 7: the machine strip refreshes while a job runs."""

    def test_timer_idle_at_first(self):
        main = self._show(MainWindow)
        self.assertFalse(main._machine_timer.isActive())

    def test_timer_runs_while_a_job_runs_and_stops_after(self):
        main = self._show(MainWindow)
        self.state.job_changed.emit("Making pictures")
        self.assertTrue(main._machine_timer.isActive())
        self.state.job_changed.emit(None)
        self.assertFalse(main._machine_timer.isActive())

    def test_strip_refreshes_on_timer(self):
        main = self._show(MainWindow)
        with mock.patch.object(machine, "mem_available_mb",
                               return_value=100), \
                mock.patch.object(machine, "disk_free_mb",
                                  return_value=200):
            main._refresh_machine()
        first = main.machine_label.text()
        with mock.patch.object(machine, "mem_available_mb",
                               return_value=300), \
                mock.patch.object(machine, "disk_free_mb",
                                  return_value=400):
            main._machine_timer.timeout.emit()
        second = main.machine_label.text()
        self.assertNotEqual(first, second)
        self.assertIn("Free memory", second)
        self.assertIn("Free disk", second)


class HomeSingleCtaTests(GuiBase):
    """Item 8: home has one call to action, not two controls per
    destination."""

    def test_row_has_only_the_break_call_to_action(self):
        main = self._show(MainWindow)
        self.assertEqual(main.scenes_button.text(), "Break into scenes")
        self.assertFalse(hasattr(main, "stills_button"))
        self.assertFalse(hasattr(main, "voice_button"))
        self.assertFalse(hasattr(main, "export_button"))

    def test_tiles_still_open_their_windows(self):
        main = self._show(MainWindow)
        for tile, key in ((main.scenes_tile, "scenes"),
                          (main.stills_tile, "stills"),
                          (main.voice_tile, "voice"),
                          (main.film_tile, "export")):
            tile.click()
            self.assertIn(key, main._windows)
            self.windows.append(main._windows[key])


class VoicePickerGuiTests(AssetGuiBase):
    """Item 11: three voices to pick from, with plain descriptions."""

    def test_picker_lists_three_voices_with_descriptions(self):
        window = self._show(VoiceWindow)
        self.assertEqual(window.voice_combo.count(), 3)
        labels = [window.voice_combo.itemText(index)
                  for index in range(window.voice_combo.count())]
        self.assertTrue(any("Lessac" in text
                            and "recommended" in text
                            for text in labels), labels)
        self.assertTrue(any("Ryan" in text for text in labels),
                        labels)
        self.assertTrue(any("Alan" in text for text in labels),
                        labels)
        ids = [window.voice_combo.itemData(index)
               for index in range(window.voice_combo.count())]
        self.assertEqual(ids, list(voice.VOICES))
        self.assertIn("clear American voice",
                      window.voice_note.text())
        self.assertIn("Download is about",
                      window.voice_note.text())

    def test_picker_shows_the_project_voice(self):
        self.state.project.voice = "en_GB-alan-medium"
        window = self._show(VoiceWindow)
        self.assertEqual(window.voice_combo.currentData(),
                         "en_GB-alan-medium")
        self.assertIn("British", window.voice_note.text())

    def test_switching_voices_marks_lines_stale(self):
        scenes = self._two_scenes()
        for scene in scenes:
            scene.voice_id = "en_US-lessac-medium"
        self.state.save()
        window = self._show(VoiceWindow)
        self.assertFalse(window.make_button.isEnabled())
        index = window.voice_combo.findData("en_US-ryan-high")
        # List 5 item 1: the switch asks first now, on every path.
        # Answer Yes, as the user confirming the switch would.
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.Yes):
            window.voice_combo.setCurrentIndex(index)
        self.assertEqual(self.state.project.voice,
                         "en_US-ryan-high")
        widget = window.line_list.itemWidget(
            window.line_list.item(0))
        label = widget.findChildren(QLabel)[0]
        self.assertIn("[out of date]", label.text())
        self.assertTrue(window.make_button.isEnabled())
        self.assertEqual(window.make_note.text(), "")
        loaded = scenes_mod.load_project(self.state.project_dir)
        self.assertEqual(loaded.voice, "en_US-ryan-high")

    def test_unknown_voice_falls_back_to_the_usual_one(self):
        self.state.project.voice = "no-such-voice"
        window = self._show(VoiceWindow)
        self.assertEqual(self.state.project.voice,
                         "en_US-lessac-medium")
        self.assertEqual(window.voice_combo.currentData(),
                         "en_US-lessac-medium")


class SceneLengthGuiTests(GuiBase):
    """Item 12: Short, Medium, or Long for the breakdown."""

    def setUp(self):
        super().setUp()
        # Pin the machine to 8 GB so "fits this PC" is about the
        # test data, not whatever machine runs the tests.
        self._mem_patch = mock.patch.object(
            machine, "mem_total_mb", return_value=8192)
        self._mem_patch.start()
        self.addCleanup(self._mem_patch.stop)
        self._mem_free_patch = mock.patch.object(
            machine, "mem_available_mb", return_value=8192)
        self._mem_free_patch.start()
        self.addCleanup(self._mem_free_patch.stop)

    def _ready_window(self):
        self.state.project.idea = "A stool in a workshop."
        window = self._show(ScenesWindow)
        self.assertTrue(wait_for(
            lambda: "not running" in window.ollama_label.text(),
            5000))
        window._on_models([dict(info) for info in INFOS])
        return window

    def test_length_choice_lists_short_medium_long(self):
        window = self._show(ScenesWindow)
        labels = [window.length_combo.itemText(index)
                  for index in range(window.length_combo.count())]
        self.assertEqual(window.length_combo.count(), 3)
        self.assertTrue(any("Short" in text and "3 to 5" in text
                            for text in labels), labels)
        self.assertTrue(any("Medium" in text and "3 to 8" in text
                            for text in labels), labels)
        self.assertTrue(any("Long" in text and "8 to 12" in text
                            for text in labels), labels)
        self.assertEqual(window.length_combo.currentData(), "medium")

    def _run_breakdown(self, window, length):
        captured = {}
        scenes = [{"prompt": "a stool", "narration": "A stool."}]

        def _run(idea, model, on_scene, report=None,
                 cancel_event=None, timeout=180,
                 scene_length="medium"):
            captured["scene_length"] = scene_length
            for scene in scenes:
                on_scene(scene)
            return scenes, 0, "raw"

        if length is not None:
            index = window.length_combo.findData(length)
            window.length_combo.setCurrentIndex(index)
        with mock.patch.object(
                ollama, "break_into_scenes_streaming",
                side_effect=_run):
            window._break_into_scenes()
            self.assertTrue(wait_for(
                lambda: len(self.state.project.scenes) == 1, 5000))
        return captured

    def test_long_choice_changes_the_prompt_sent(self):
        window = self._ready_window()
        window.model_combo.setCurrentIndex(0)
        captured = self._run_breakdown(window, "long")
        self.assertEqual(captured["scene_length"], "long")

    def test_medium_keeps_the_usual_prompt(self):
        window = self._ready_window()
        window.model_combo.setCurrentIndex(0)
        captured = self._run_breakdown(window, None)
        self.assertEqual(captured["scene_length"], "medium")


class MotionPickerGuiTests(GuiBase):
    """Item 13: each scene picks its camera move."""

    def _two_scenes(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool",
                             narration="A stool sits here."),
            scenes_mod.Scene(prompt="a hammer",
                             narration="A hammer waits."),
        ]
        self.state.project_changed.emit()

    def test_picker_lists_the_moves(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        labels = [window.motion_combo.itemText(index)
                  for index in range(window.motion_combo.count())]
        self.assertEqual(window.motion_combo.count(), 6)
        self.assertIn("Auto", labels[0])
        for name in ("Push in", "Pull out", "Pan left", "Pan right",
                     "Still"):
            self.assertTrue(any(name in text for text in labels),
                            labels)
        values = [window.motion_combo.itemData(index)
                  for index in range(window.motion_combo.count())]
        self.assertEqual(values,
                         ["", "zoomin", "zoomout", "panleft",
                          "panright", "still"])

    def test_picker_shows_the_chosen_scene_move(self):
        self._two_scenes()
        self.state.project.scenes[0].motion = "panleft"
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        self.assertEqual(window.motion_combo.currentData(), "panleft")
        window.scene_list.setCurrentRow(1)
        self.assertEqual(window.motion_combo.currentData(), "")

    def test_motion_choice_is_saved_with_the_scene(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        index = window.motion_combo.findData("panleft")
        window.motion_combo.setCurrentIndex(index)
        window.scene_list.setCurrentRow(1)
        self.assertEqual(self.state.project.scenes[0].motion,
                         "panleft")
        loaded = scenes_mod.load_project(self.state.project_dir)
        self.assertEqual(loaded.scenes[0].motion, "panleft")

    def test_motion_choice_can_be_undone(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        index = window.motion_combo.findData("panleft")
        window.motion_combo.setCurrentIndex(index)
        window._save_scene()
        self.assertEqual(self.state.project.scenes[0].motion,
                         "panleft")
        window._undo()
        self.assertEqual(self.state.project.scenes[0].motion, "")
        self.assertEqual(window.motion_combo.currentData(), "")


class MotionStaleGuiTests(AssetGuiBase):
    """Item 13: changing a scene's move makes its clip out of date."""

    def test_motion_change_marks_clip_out_of_date(self):
        scenes = self._two_scenes()
        paths = scenes_mod.ensure_project_dirs(
            self.state.project_dir)
        for index, scene in enumerate(scenes):
            clip_name = scenes_mod.scene_clip_name(index)
            (paths["clips"] / clip_name).write_bytes(b"fake mp4")
            scene.clip = f"clips/{clip_name}"
            scene.clip_motion = video.motion_for_scene(
                index, scene.motion)
            scene.voice_id = "en_US-lessac-medium"
        self.state.save()
        window = self._show(ExportWindow)
        self.assertIn("2 moving clips ready",
                      window.summary_label.text())
        scenes[0].motion = "panleft"
        self.state.project_changed.emit()
        self.assertIn("1 moving clip ready",
                      window.summary_label.text())


class PosterGuiTests(GuiBase):
    """Item 14: the Export player shows the first frame while idle."""

    def _film(self):
        paths = scenes_mod.ensure_project_dirs(
            self.state.project_dir)
        still = write_png(self.tmp / "still.png", size=64)
        film = self.state.project_dir / scenes_mod.FILM_FILE
        video.make_scene_clip(still, film, 1, "zoomin")
        return film

    def test_poster_visible_when_film_loaded_and_idle(self):
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        self._film()
        window = self._show(ExportWindow)
        self.assertIsNotNone(window._poster_pixmap)
        self.assertFalse(window._poster_pixmap.isNull())
        self.assertIs(window._player_stack.currentWidget(),
                      window.poster_label)

    def test_poster_hidden_on_play_and_back_on_end(self):
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        from hicksfield.gui import HAVE_MULTIMEDIA
        if not HAVE_MULTIMEDIA:
            self.skipTest("no media pieces in this build")
        from PySide6.QtMultimedia import QMediaPlayer
        self._film()
        window = self._show(ExportWindow)
        self.assertIs(window._player_stack.currentWidget(),
                      window.poster_label)
        with mock.patch.object(window._player, "play"), \
                mock.patch.object(
                    window._player, "playbackState",
                    return_value=QMediaPlayer.PausedState):
            window._toggle_play()
        self.assertIs(window._player_stack.currentWidget(),
                      window.video_widget)
        window._on_media_status(QMediaPlayer.EndOfMedia)
        self.assertIs(window._player_stack.currentWidget(),
                      window.poster_label)
        self.assertEqual(window.play_button.text(), "Play")

    def test_poster_extracted_once_per_film(self):
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        self._film()
        calls = []
        real = video.extract_first_frame

        def _counting(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        with mock.patch.object(video, "extract_first_frame",
                               side_effect=_counting):
            window = self._show(ExportWindow)
            self.state.project_changed.emit()
            self.state.project_changed.emit()
        self.assertEqual(len(calls), 1)
        self.assertIs(window._player_stack.currentWidget(),
                      window.poster_label)

    def test_rebuilt_film_gets_a_fresh_poster(self):
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        film = self._film()
        window = self._show(ExportWindow)
        first = window._poster_pixmap
        still = write_png(self.tmp / "still2.png", size=64)
        video.make_scene_clip(still, film, 1, "zoomout")
        self.state.project_changed.emit()
        self.assertIsNotNone(window._poster_pixmap)
        self.assertIsNot(window._poster_pixmap, first)
        self.assertIs(window._player_stack.currentWidget(),
                      window.poster_label)

    def test_failed_extraction_shows_a_note(self):
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        self._film()
        with mock.patch.object(video, "extract_first_frame",
                               return_value=None):
            window = self._show(ExportWindow)
        self.assertIsNone(window._poster_pixmap)
        self.assertIs(window._player_stack.currentWidget(),
                      window.poster_note)
        self.assertIn("No preview frame", window.poster_note.text())
        self.assertEqual(window.status_label.text(), "Ready.")
