"""v0.2 redesign tests: task list items 3-20."""

import json
import time
import unittest
from unittest import mock

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from hicksfield import ollama, scenes as scenes_mod, stills, video
from hicksfield.errors import CancelledError, OllamaUnreachableError
from hicksfield.gui import (
    ExportWindow,
    HowToWindow,
    MainWindow,
    ScenesWindow,
    StillsWindow,
    VoiceWindow,
    HAVE_MULTIMEDIA,
)
from hicksfield.text import plural
from tests.helpers import (
    TempEnvTestCase,
    app,
    require_qt,
    wait_for,
    write_png,
    write_wav,
)
from tests.test_gui import GuiBase


class PluralTests(unittest.TestCase):
    def test_singular_and_plural(self):
        self.assertEqual(plural(0, "scene"), "0 scenes")
        self.assertEqual(plural(1, "scene"), "1 scene")
        self.assertEqual(plural(2, "scene"), "2 scenes")
        self.assertEqual(plural(1, "spoken line"), "1 spoken line")
        self.assertEqual(plural(2, "spoken line"), "2 spoken lines")
        self.assertEqual(plural(1, "picture"), "1 picture")
        self.assertEqual(plural(3, "picture"), "3 pictures")


class FfmpegShortTests(TempEnvTestCase):
    def test_short_version_parses(self):
        with mock.patch.object(
                video, "ffmpeg_version",
                return_value="ffmpeg version 8.1.2 Copyright (c) "
                "2000-2026 the FFmpeg developers"):
            self.assertEqual(video.ffmpeg_version_short(), "8.1.2")

    def test_short_version_none_when_missing(self):
        with mock.patch.object(video, "ffmpeg_version",
                               return_value=None):
            self.assertIsNone(video.ffmpeg_version_short())

    def test_short_version_live(self):
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        short = video.ffmpeg_version_short()
        self.assertIsNotNone(short)
        self.assertNotIn("Copyright", short)


class RecommendedEngineTests(TempEnvTestCase):
    def test_neither_ready_recommends_fast(self):
        with mock.patch.object(
                stills.FastSDCPUEngine, "is_ready",
                return_value=(False, "Not set up yet.")), \
                mock.patch.object(
                    stills.SDCppEngine, "is_ready",
                    return_value=(False, "Not set up yet.")):
            engine, reason = stills.recommended_engine()
            self.assertEqual(engine.key, "fastsd")
            self.assertIn("Fast", reason)

    def test_only_steady_ready_recommends_steady(self):
        with mock.patch.object(
                stills.FastSDCPUEngine, "is_ready",
                return_value=(False, "Not set up yet.")), \
                mock.patch.object(
                    stills.SDCppEngine, "is_ready",
                    return_value=(True, "Ready.")):
            engine, reason = stills.recommended_engine()
            self.assertEqual(engine.key, "sdcpp")
            self.assertIn("Steady", reason)

    def test_fast_ready_recommends_fast(self):
        with mock.patch.object(
                stills.FastSDCPUEngine, "is_ready",
                return_value=(True, "Ready.")), \
                mock.patch.object(
                    stills.SDCppEngine, "is_ready",
                    return_value=(True, "Ready.")):
            engine, _reason = stills.recommended_engine()
            self.assertEqual(engine.key, "fastsd")


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


class StreamingTests(TempEnvTestCase):
    def test_scenes_stream_in_one_by_one(self):
        lines = [
            _chunk('[{"prompt": "a stool", "narration": "A stool."}, '),
            _chunk('{"prompt": "a chair", "narration": "A chair."}]'),
            _chunk("", done=True),
        ]
        seen = []
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_FakeStream(lines)):
            scenes, dropped, _raw = ollama.break_into_scenes_streaming(
                "an idea", "fake-model", seen.append)
        self.assertEqual(len(scenes), 2)
        self.assertEqual(seen, scenes)
        self.assertEqual(scenes[0]["prompt"], "a stool")
        self.assertEqual(scenes[1]["prompt"], "a chair")
        self.assertEqual(dropped, 0)

    def test_split_object_waits_for_the_rest(self):
        lines = [
            _chunk('[{"prompt": "a stool with a'),
            _chunk(' long seat", "narration": "A stool."}]'),
            _chunk("", done=True),
        ]
        seen = []
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_FakeStream(lines)):
            scenes, _dropped, _raw = (
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", seen.append))
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["prompt"],
                         "a stool with a long seat")

    def test_cancel_stops_the_stream(self):
        import threading
        cancel = threading.Event()
        lines = [_chunk('[{"prompt": "a", "narration": "b"}, ')] + [
            _chunk('{"prompt": "c", "narration": "d"}')] * 50 + [
            _chunk("", done=True)]
        seen = []

        def _on_scene(scene):
            seen.append(scene)
            cancel.set()

        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_FakeStream(lines)):
            with self.assertRaises(CancelledError):
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", _on_scene,
                    cancel_event=cancel)
        self.assertEqual(len(seen), 1)

    def test_unreachable_raises(self):
        import urllib.error
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                side_effect=urllib.error.URLError("nope")):
            with self.assertRaises(OllamaUnreachableError):
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None)


class Item3Tests(GuiBase):
    def test_stills_default_size_and_whole_rows(self):
        window = self._show(StillsWindow)
        self.assertEqual(window._default_size, (950, 700))
        # Three whole rows of 115 px rows need 345 px of viewport.
        self.assertGreaterEqual(window.still_list.minimumHeight(), 347)

    def test_scenes_default_height_raised(self):
        window = self._show(ScenesWindow)
        self.assertEqual(window._default_size, (900, 700))

    def test_scenes_list_fits_three_film_frames(self):
        # Three 64 px rows need 194 px; without a minimum the
        # layout squeezes the list down to a single row.
        window = self._show(ScenesWindow)
        self.assertGreaterEqual(window.scene_list.minimumHeight(),
                                194)


class Item4Tests(GuiBase):
    def _two_scenes(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool", narration="A stool."),
            scenes_mod.Scene(prompt="a chair", narration="A chair."),
        ]
        self.state.project_changed.emit()

    def test_no_default_selection(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        self.assertEqual(window.scene_list.currentRow(), -1)
        self.assertFalse(window.remove_button.isEnabled())
        self.assertIn("pick a scene above",
                      window.edit_label.text())

    def test_choosing_a_scene_enables_remove(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(0)
        QTest.qWait(50)
        self.assertTrue(window.remove_button.isEnabled())
        self.assertEqual(window.edit_label.text(), "The chosen scene:")
        self.assertEqual(window.prompt_edit.toPlainText(), "a stool")

    def test_add_selects_the_new_scene(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window._add_scene()
        self.assertEqual(window.scene_list.currentRow(), 2)

    def test_remove_with_no_selection_says_so(self):
        self._two_scenes()
        window = self._show(ScenesWindow)
        window._remove_scene()
        self.assertIn("Pick a scene first",
                      window.status_label.text())

    def test_howto_selects_nothing(self):
        window = self._show(HowToWindow)
        self.assertEqual(window.topic_list.currentRow(), -1)
        self.assertIn("Pick a topic on the left",
                      window.text.toPlainText())
        window.topic_list.setCurrentRow(0)
        self.assertIn("HicksField turns an idea",
                      window.text.toPlainText())


class Item5Tests(GuiBase):
    def _ready_window(self):
        self.state.project.idea = "A stool in a workshop."
        window = self._show(ScenesWindow)
        # Let the Ollama check finish FIRST: its handler clears the
        # model list, so a fake model added before it lands would
        # be wiped and the breakdown would refuse to start.
        self.assertTrue(wait_for(
            lambda: "not running" in window.ollama_label.text(),
            5000))
        window.model_combo.addItem("fake-model")
        window.model_combo.setCurrentIndex(0)
        return window

    def _fake_stream(self, scenes):
        def _run(idea, model, on_scene, report=None,
                 cancel_event=None, timeout=180, scene_length="medium"):
            for scene in scenes:
                on_scene(scene)
                if report is not None:
                    report("Breaking the idea into scenes", None,
                           f"{len(scenes)} scenes so far")
            return scenes, 0, "raw"

        return _run

    def test_breakdown_ui_exists(self):
        window = self._show(ScenesWindow)
        self.assertFalse(window.breakdown_progress.isVisible())
        self.assertFalse(window.breakdown_cancel.isVisible())

    def test_breakdown_streams_and_commits(self):
        window = self._ready_window()
        scenes = [
            {"prompt": "a stool", "narration": "A stool."},
            {"prompt": "a chair", "narration": "A chair."},
        ]
        with mock.patch.object(
                ollama, "break_into_scenes_streaming",
                side_effect=self._fake_stream(scenes)):
            window._break_into_scenes()
            self.assertTrue(wait_for(
                lambda: len(self.state.project.scenes) == 2, 5000))
        self.assertFalse(window.breakdown_progress.isVisible())
        self.assertFalse(window.breakdown_cancel.isVisible())
        self.assertIn("Made 2 scenes",
                      window.status_label.text())

    def test_breakdown_cancel_keeps_partial_scenes(self):
        import threading
        window = self._ready_window()
        started = threading.Event()

        def _run(idea, model, on_scene, report=None,
                 cancel_event=None, timeout=180, scene_length="medium"):
            on_scene({"prompt": "a stool", "narration": "A stool."})
            started.set()
            deadline = time.monotonic() + 10
            while not cancel_event.is_set():
                if time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            raise CancelledError()

        with mock.patch.object(
                ollama, "break_into_scenes_streaming",
                side_effect=_run):
            window._break_into_scenes()
            self.assertTrue(started.wait(5))
            # Skeletons show while the breakdown is still coming.
            skeleton = sum(
                1 for row in range(window.scene_list.count())
                if window.scene_list.item(row).flags()
                == Qt.NoItemFlags)
            self.assertEqual(skeleton, 3)
            self.assertTrue(window.breakdown_cancel.isVisible())
            QTest.mouseClick(window.breakdown_cancel, Qt.LeftButton)
            self.assertTrue(wait_for(
                lambda: not window._breakdown_running, 5000))
        self.assertEqual(len(self.state.project.scenes), 1)
        self.assertIn("Kept the 1 scene",
                      window.status_label.text())

    def test_replace_declined_keeps_scenes(self):
        from PySide6.QtWidgets import QMessageBox
        self.state.project.idea = "A stool in a workshop."
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="old", narration="Old.")]
        window = self._ready_window()
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.No):
            window._break_into_scenes()
        self.assertIsNone(window._worker)
        self.assertEqual(len(self.state.project.scenes), 1)
        self.assertIn("Kept the current scenes",
                      window.status_label.text())


class Item6Tests(GuiBase):
    def test_focus_rules_in_stylesheet(self):
        from hicksfield.style import HICKSFIELD_STYLESHEET
        self.assertIn("QPushButton:focus", HICKSFIELD_STYLESHEET)
        self.assertIn('QPushButton[role="primary"]:focus',
                      HICKSFIELD_STYLESHEET)
        self.assertIn('QPushButton[role="curtain"]:focus',
                      HICKSFIELD_STYLESHEET)

    def test_main_tab_order(self):
        main = self._show(MainWindow)
        chain = []
        widget = main.idea_edit
        for _ in range(80):
            widget = widget.nextInFocusChain()
            chain.append(widget)
            if widget is main.idea_edit:
                break
        order = [main.scenes_tile, main.stills_tile, main.voice_tile,
                 main.film_tile, main.scenes_button]
        positions = [chain.index(w) for w in order]
        self.assertEqual(positions, sorted(positions))


class Item7Tests(GuiBase):
    def test_edit_boxes_have_labels(self):
        window = self._show(ScenesWindow)
        from PySide6.QtWidgets import QLabel
        texts = [child.text() for child in
                 window.findChildren(QLabel)]
        self.assertIn("Picture prompt:", texts)
        self.assertIn("Spoken line:", texts)
        self.assertTrue(any("Pick the model that breaks your idea "
                            "into scenes" in text
                            for text in texts))


class Item8Tests(GuiBase):
    def _ten_scenes(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt=f"scene {n}",
                             narration=f"Line {n}.")
            for n in range(10)
        ]
        self.state.project_changed.emit()

    def test_scenes_list_keeps_scroll(self):
        self._ten_scenes()
        window = self._show(ScenesWindow)
        bar = window.scene_list.verticalScrollBar()
        QTest.qWait(100)
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        QTest.qWait(50)
        saved = bar.value()
        self.state.project_changed.emit()
        QTest.qWait(50)
        self.assertEqual(bar.value(), saved)

    def test_stills_list_keeps_scroll(self):
        self._ten_scenes()
        window = self._show(StillsWindow)
        bar = window.still_list.verticalScrollBar()
        QTest.qWait(100)
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        QTest.qWait(50)
        saved = bar.value()
        self.state.project_changed.emit()
        QTest.qWait(50)
        self.assertEqual(bar.value(), saved)

    def test_voice_list_keeps_scroll(self):
        self._ten_scenes()
        window = self._show(VoiceWindow)
        bar = window.line_list.verticalScrollBar()
        QTest.qWait(100)
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        QTest.qWait(50)
        saved = bar.value()
        self.state.project_changed.emit()
        QTest.qWait(50)
        self.assertEqual(bar.value(), saved)


class Item9Tests(GuiBase):
    def test_export_summary_uses_plurals(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a", narration="b")]
        self.state.project_changed.emit()
        window = self._show(ExportWindow)
        self.assertEqual(
            window.summary_label.text(),
            "1 scene. 0 pictures ready. 0 spoken lines ready. "
            "0 moving clips ready.")

    def test_ffmpeg_line_is_plain_words(self):
        if video.find_ffmpeg() is None:
            self.skipTest("ffmpeg not installed")
        window = self._show(ExportWindow)
        text = window.ffmpeg_label.text()
        self.assertTrue(text.startswith("ffmpeg is ready (version "))
        self.assertNotIn("Copyright", text)

    def test_make_done_messages_use_plurals(self):
        stills_window = self._show(StillsWindow)
        stills_window._on_make_done(1)
        self.assertIn("Made 1 picture.",
                      stills_window.status_label.text())
        stills_window._on_make_done(2)
        self.assertIn("Made 2 pictures.",
                      stills_window.status_label.text())
        voice_window = self._show(VoiceWindow)
        voice_window._on_make_done(1)
        self.assertIn("Spoke 1 line.",
                      voice_window.status_label.text())


class Item10Tests(GuiBase):
    def test_four_tiles_and_film_built(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a", narration="b"),
            scenes_mod.Scene(prompt="c", narration="d"),
        ]
        self.state.project_changed.emit()
        main = self._show(MainWindow)
        self.assertEqual(main.scenes_tile.number_label.text(), "2")
        self.assertEqual(main.film_tile.number_label.text(), "Not yet")
        film = self.state.project_dir / scenes_mod.FILM_FILE
        film.write_bytes(b"fake film")
        self.state.project_changed.emit()
        QTest.qWait(50)
        self.assertEqual(main.film_tile.number_label.text(), "Built")

    def test_tile_opens_its_window(self):
        main = self._show(MainWindow)
        QTest.mouseClick(main.scenes_tile, Qt.LeftButton)
        self.assertTrue(wait_for(
            lambda: "scenes" in main._windows, 3000))
        self.assertIsInstance(main._windows["scenes"], ScenesWindow)
        main._windows["scenes"].close()

    def test_tiles_are_full_height(self):
        # QPushButton sizes from its own (empty) text, so a tile
        # without a sizeHint override is crushed to one line and
        # its labels pile up. The tile must stand tall enough for
        # the number, the name, and the line under it. The real
        # app always runs styled, so the test styles the app first.
        from hicksfield.style import apply_style
        apply_style(self.app)
        main = self._show(MainWindow)
        QTest.qWait(100)
        for tile in (main.scenes_tile, main.stills_tile,
                     main.voice_tile, main.film_tile):
            self.assertGreaterEqual(tile.sizeHint().height(), 90)
            self.assertGreaterEqual(tile.height(), 90)


class Item11Tests(GuiBase):
    def test_checklist_shows_first_run(self):
        main = self._show(MainWindow)
        self.assertTrue(main.checklist_card.isVisible())
        if video.find_ffmpeg() is not None:
            _mark, text = main._checklist_rows["ffmpeg"]
            self.assertIn("ffmpeg is ready", text.text())

    def test_checklist_hides_and_stays_hidden(self):
        from hicksfield.gui import settings
        main = self._show(MainWindow)
        hide = None
        from PySide6.QtWidgets import QPushButton
        for button in main.checklist_card.findChildren(QPushButton):
            if button.text() == "Hide this list":
                hide = button
        self.assertIsNotNone(hide)
        QTest.mouseClick(hide, Qt.LeftButton)
        self.assertTrue(main.checklist_card.isHidden())
        self.assertEqual(
            settings().value("checklist/dismissed", 0, type=int), 1)
        again = MainWindow(self.state)
        self.windows.append(again)
        again.show()
        QTest.qWait(50)
        self.assertTrue(again.checklist_card.isHidden())
        again._show_checklist()
        self.assertTrue(again.checklist_card.isVisible())

    def test_ollama_row_reports_not_running(self):
        main = self._show(MainWindow)
        _mark, text = main._checklist_rows["ollama"]
        self.assertTrue(wait_for(
            lambda: "not running" in text.text(), 5000))

    def test_checklist_buttons_are_not_squeezed(self):
        # The home layout once ran over capacity and the checklist
        # buttons were squeezed to one text line tall, clipping
        # their labels. They must get their full height.
        from PySide6.QtWidgets import QPushButton
        main = self._show(MainWindow)
        QTest.qWait(100)
        for button in main.checklist_card.findChildren(QPushButton):
            self.assertEqual(button.property("role"), "small")
            self.assertGreaterEqual(
                button.height(), button.sizeHint().height() - 1)


class Item12Tests(GuiBase):
    def _three_scenes(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a", narration="A."),
            scenes_mod.Scene(prompt="b", narration="B."),
            scenes_mod.Scene(prompt="c", narration="C."),
        ]
        self.state.project_changed.emit()

    def _prompts(self):
        return [s.prompt for s in self.state.project.scenes]

    def test_drag_mode_is_internal_move(self):
        from PySide6.QtWidgets import QAbstractItemView
        window = self._show(ScenesWindow)
        self.assertEqual(window.scene_list.dragDropMode(),
                         QAbstractItemView.InternalMove)

    def test_model_move_reorders_the_project(self):
        self._three_scenes()
        window = self._show(ScenesWindow)
        model = window.scene_list.model()
        moved = model.moveRows(QModelIndex(), 0, 1, QModelIndex(), 3)
        self.assertTrue(moved)
        self.assertTrue(wait_for(
            lambda: self._prompts() == ["b", "c", "a"], 3000))

    def test_move_buttons_swap_scenes(self):
        self._three_scenes()
        window = self._show(ScenesWindow)
        window.scene_list.setCurrentRow(1)
        QTest.qWait(50)
        self.assertTrue(window.move_up_button.isEnabled())
        QTest.mouseClick(window.move_up_button, Qt.LeftButton)
        self.assertEqual(self._prompts(), ["b", "a", "c"])
        self.assertEqual(window.scene_list.currentRow(), 0)
        self.assertFalse(window.move_up_button.isEnabled())
        QTest.mouseClick(window.move_down_button, Qt.LeftButton)
        self.assertEqual(self._prompts(), ["a", "b", "c"])
        window.scene_list.setCurrentRow(2)
        QTest.qWait(50)
        self.assertFalse(window.move_down_button.isEnabled())


class Item13Tests(GuiBase):
    def test_preview_shows_the_picture_large(self):
        paths = scenes_mod.ensure_project_dirs(self.state.project_dir)
        still = write_png(paths["stills"] / "scene-01.png", size=64)
        scene = scenes_mod.Scene(prompt="a stool", narration="A stool.")
        scene.still = f"{scenes_mod.STILLS_DIR}/{still.name}"
        self.state.project.scenes = [scene]
        self.state.project_changed.emit()
        window = self._show(StillsWindow)
        window.still_list.setCurrentRow(0)
        QTest.qWait(100)
        pixmap = window.preview.pixmap()
        self.assertIsNotNone(pixmap)
        self.assertFalse(pixmap.isNull())
        self.assertIn("Scene 1: the picture.",
                      window.preview_caption.text())

    def test_preview_without_a_picture(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a stool", narration="A stool.")]
        self.state.project_changed.emit()
        window = self._show(StillsWindow)
        window.still_list.setCurrentRow(0)
        QTest.qWait(50)
        pixmap = window.preview.pixmap()
        self.assertTrue(pixmap is None or pixmap.isNull())
        self.assertIn("no picture yet",
                      window.preview_caption.text())


class Item14Tests(GuiBase):
    def _scene_with_audio(self):
        paths = scenes_mod.ensure_project_dirs(self.state.project_dir)
        audio = write_wav(paths["audio"] / "scene-01.wav", seconds=0.5)
        scene = scenes_mod.Scene(prompt="a", narration="A stool.")
        scene.audio = f"{scenes_mod.AUDIO_DIR}/{audio.name}"
        self.state.project.scenes = [
            scene,
            scenes_mod.Scene(prompt="b", narration="A chair."),
        ]
        self.state.project_changed.emit()
        return audio

    def test_play_button_per_line(self):
        self._scene_with_audio()
        window = self._show(VoiceWindow)
        self.assertEqual(len(window._play_buttons), 2)
        self.assertEqual(window._play_buttons[0].text(), "Play")
        self.assertTrue(window._play_buttons[0].isEnabled())
        self.assertFalse(window._play_buttons[1].isEnabled())

    def test_rows_are_tall_enough_for_the_button(self):
        # The row widget's sizeHint collapsed to one text line,
        # squeezing the Play button to half height. Rows are a
        # fixed 44 px now, and the button gets its full height.
        self._scene_with_audio()
        window = self._show(VoiceWindow)
        QTest.qWait(100)
        item = window.line_list.item(0)
        self.assertEqual(item.sizeHint().height(), 44)
        self.assertGreaterEqual(
            window._play_buttons[0].height(), 24)

    def test_play_sets_the_source_and_stops(self):
        if not HAVE_MULTIMEDIA:
            self.skipTest("QtMultimedia not available")
        audio = self._scene_with_audio()
        window = self._show(VoiceWindow)
        # The sandbox has no audio device; the no-device guard
        # (item 30) would refuse to play. Pretend one is present
        # so this test exercises the play path.
        with mock.patch("hicksfield.gui._audio_output_available",
                        return_value=True):
            QTest.mouseClick(window._play_buttons[0], Qt.LeftButton)
            QTest.qWait(200)
        source = window._player.source().toLocalFile()
        self.assertEqual(source, str(audio))
        window._stop_line()
        self.assertIsNone(window._playing_row)
        self.assertEqual(window._play_buttons[0].text(), "Play")


class Item15Tests(GuiBase):
    def _make_film(self):
        import subprocess
        ffmpeg = video.find_ffmpeg()
        if ffmpeg is None:
            self.skipTest("ffmpeg not installed")
        film = self.state.project_dir / scenes_mod.FILM_FILE
        proc = subprocess.run(
            [str(ffmpeg), "-y", "-f", "lavfi",
             "-i", "color=c=black:s=320x240:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(film)],
            capture_output=True, timeout=60)
        if proc.returncode != 0:
            self.skipTest("ffmpeg could not make a test film")
        return film

    def test_player_wired_to_the_film(self):
        if not HAVE_MULTIMEDIA:
            self.skipTest("QtMultimedia not available")
        film = self._make_film()
        self.state.project_changed.emit()
        window = self._show(ExportWindow)
        self.assertTrue(window.play_button.isEnabled())
        self.assertTrue(window.scrub.isEnabled())
        self.assertEqual(window._player.source().toLocalFile(),
                         str(film))
        if wait_for(lambda: window._duration > 0, 5000):
            self.assertGreater(window.scrub.maximum(), 0)
            self.assertIn(" / ", window.time_label.text())

    def test_play_button_does_not_crash(self):
        if not HAVE_MULTIMEDIA:
            self.skipTest("QtMultimedia not available")
        self._make_film()
        self.state.project_changed.emit()
        window = self._show(ExportWindow)
        QTest.mouseClick(window.play_button, Qt.LeftButton)
        QTest.qWait(300)
        QTest.mouseClick(window.play_button, Qt.LeftButton)
        QTest.qWait(100)
        self.assertTrue(window.play_button.isEnabled())

    def test_no_film_means_no_play(self):
        window = self._show(ExportWindow)
        self.assertFalse(window.play_button.isEnabled())
        self.assertFalse(window.scrub.isEnabled())


class Item16Tests(GuiBase):
    def _menu_actions(self, main, menu_title):
        for action in main.menuBar().actions():
            if action.text() == menu_title:
                return action.menu().actions()
        return []

    def test_tooltips_present(self):
        scenes = self._show(ScenesWindow)
        self.assertTrue(scenes.model_combo.toolTip())
        stills_window = self._show(StillsWindow)
        self.assertTrue(stills_window.make_button.toolTip())
        main = self._show(MainWindow)
        self.assertTrue(main.scenes_tile.toolTip())
        self.assertTrue(main.idea_edit.toolTip())

    def test_menu_shortcuts(self):
        main = self._show(MainWindow)
        file_actions = {a.text(): a
                        for a in self._menu_actions(main, "File")}
        self.assertEqual(
            file_actions["New film"].shortcut().toString(), "Ctrl+N")
        self.assertEqual(
            file_actions["Open film"].shortcut().toString(), "Ctrl+O")
        self.assertEqual(
            file_actions["Save"].shortcut().toString(), "Ctrl+S")
        help_actions = {a.text(): a
                        for a in self._menu_actions(main, "Help")}
        self.assertEqual(
            help_actions["How to Use"].shortcut().toString(), "F1")
        self.assertIn("Keyboard shortcuts", help_actions)

    def test_shortcuts_dialog_lists_them(self):
        main = self._show(MainWindow)
        dialog = main._shortcuts_dialog()
        from PySide6.QtWidgets import QLabel
        texts = [child.text() for child in dialog.findChildren(QLabel)]
        joined = "\n".join(texts)
        for key in ("Ctrl+N", "Ctrl+O", "Ctrl+S", "F1", "?"):
            self.assertIn(key, joined)
        dialog.close()

    def test_question_mark_guarded_in_text_boxes(self):
        main = self._show(MainWindow)
        main.idea_edit.setFocus()
        QTest.qWait(50)
        with mock.patch.object(
                MainWindow, "_show_shortcuts") as show:
            main._maybe_show_shortcuts()
            show.assert_not_called()
        main.scenes_button.setFocus()
        QTest.qWait(50)
        with mock.patch.object(
                MainWindow, "_show_shortcuts") as show:
            main._maybe_show_shortcuts()
            show.assert_called_once()


class Item17Tests(GuiBase):
    def test_stills_marks_the_recommended_engine(self):
        window = self._show(StillsWindow)
        self.assertTrue(window.fast_card.badge.isVisible())
        self.assertFalse(window.steady_card.badge.isVisible())
        self.assertIn("Fast is the one to start with",
                      window.fast_card.badge.toolTip())

    def test_steady_recommended_when_it_is_the_ready_one(self):
        with mock.patch.object(
                stills.FastSDCPUEngine, "is_ready",
                return_value=(False, "Not set up yet.")), \
                mock.patch.object(
                    stills.SDCppEngine, "is_ready",
                    return_value=(True, "Ready.")):
            window = self._show(StillsWindow)
            self.assertFalse(window.fast_card.badge.isVisible())
            self.assertTrue(window.steady_card.badge.isVisible())

    def test_export_marks_the_recommended_button(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a", narration="b")]
        self.state.project_changed.emit()
        window = self._show(ExportWindow)
        self.assertIn("No picture engine is set up yet",
                      window.engine_note.text())
        self.assertIn("(recommended)", window.fast_build.text())
        self.assertNotIn("(recommended)", window.steady_build.text())


class Item18Tests(GuiBase):
    def test_machine_strip_shows_real_numbers(self):
        main = self._show(MainWindow)
        text = main.machine_label.text()
        self.assertIn("Free memory", text)
        self.assertIn("Free disk", text)
        self.assertTrue("GB" in text or "MB" in text)


class Item19Tests(GuiBase):
    def _scenes(self, with_assets=False):
        scene = scenes_mod.Scene(prompt="a stool", narration="A stool.")
        if with_assets:
            paths = scenes_mod.ensure_project_dirs(
                self.state.project_dir)
            still = write_png(paths["stills"] / "scene-01.png",
                              size=32)
            audio = write_wav(paths["audio"] / "scene-01.wav",
                              seconds=0.2)
            scene.still = f"{scenes_mod.STILLS_DIR}/{still.name}"
            scene.audio = f"{scenes_mod.AUDIO_DIR}/{audio.name}"
        self.state.project.scenes = [
            scene,
            scenes_mod.Scene(prompt="a chair", narration="A chair."),
        ]
        self.state.project_changed.emit()

    def _row_frame(self, window, row):
        return window.scene_list.itemWidget(
            window.scene_list.item(row))

    def test_rows_are_film_frames(self):
        self._scenes()
        window = self._show(ScenesWindow)
        frame = self._row_frame(window, 0)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.property("role"), "scene")
        from PySide6.QtWidgets import QLabel
        texts = [child.text() for child in frame.findChildren(QLabel)]
        joined = " ".join(texts)
        self.assertIn("Scene 1: a stool", joined)
        self.assertIn("No picture yet", joined)
        self.assertIn("No voice yet", joined)

    def test_both_ready_earns_the_done_frame(self):
        self._scenes(with_assets=True)
        window = self._show(ScenesWindow)
        frame = self._row_frame(window, 0)
        self.assertEqual(frame.property("role"), "scene-done")
        from PySide6.QtWidgets import QLabel
        texts = [child.text() for child in frame.findChildren(QLabel)]
        joined = " ".join(texts)
        self.assertIn("Picture ready", joined)
        self.assertIn("Voice ready", joined)
        thumbs = [child for child in frame.findChildren(QLabel)
                  if child.pixmap() is not None
                  and not child.pixmap().isNull()]
        self.assertTrue(thumbs)
        second = self._row_frame(window, 1)
        self.assertEqual(second.property("role"), "scene")


class Item20Tests(GuiBase):
    def test_motion_checkbox_toggles_the_setting(self):
        from hicksfield import style
        from hicksfield.gui import settings
        window = self._show(HowToWindow)
        self.assertTrue(window.motion_check.isChecked())
        window.motion_check.setChecked(False)
        QTest.qWait(50)
        self.assertFalse(style.motion_enabled())
        self.assertEqual(
            settings().value("motion/enabled", 1, type=int), 0)
        window.motion_check.setChecked(True)
        QTest.qWait(50)
        self.assertTrue(style.motion_enabled())
        self.assertEqual(
            settings().value("motion/enabled", 1, type=int), 1)

    def test_finish_badge_skips_animation_when_motion_off(self):
        from hicksfield import style
        from hicksfield.style import FinishBadge
        badge = FinishBadge()
        style.set_motion_enabled(False)
        try:
            badge.play()
            self.assertAlmostEqual(badge._effect.opacity(), 1.0)
        finally:
            style.set_motion_enabled(True)


class VersionTests(unittest.TestCase):
    def test_version_is_0_5_0(self):
        from hicksfield import __version__
        self.assertEqual(__version__, "0.5.0")


if __name__ == "__main__":
    unittest.main()
