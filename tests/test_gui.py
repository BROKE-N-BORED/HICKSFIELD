import os
import unittest
from unittest import mock

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QMessageBox, QWidget
from PySide6.QtTest import QTest

from hicksfield import scenes as scenes_mod
from hicksfield.gui import (
    AppState,
    DebugReportWindow,
    ExportWindow,
    HowToWindow,
    MainWindow,
    ScenesWindow,
    StillsWindow,
    VoiceWindow,
)
from hicksfield.single import SingleInstance
from hicksfield.workers import JobLock, Worker
from tests.helpers import TempEnvTestCase, app, require_qt, wait_for


def visible_texts(widget):
    """Every user-visible string in a window: widget text, placeholders,
    window titles, and menu action texts."""
    texts = []
    for child in widget.findChildren(QWidget):
        for getter in ("text", "placeholderText", "windowTitle",
                       "toolTip"):
            method = getattr(child, getter, None)
            if callable(method):
                try:
                    value = method()
                except (RuntimeError, TypeError):
                    continue
                if value:
                    texts.append(value)
    if hasattr(widget, "menuBar"):
        for action in widget.menuBar().actions():
            texts.append(action.text())
            menu = action.menu()
            if menu is not None:
                for sub in menu.actions():
                    texts.append(sub.text())
    return texts


class GuiBase(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        require_qt(self)
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
            # Schedule real destruction, not just hiding: signal
            # connections hold the windows alive past close(), and
            # only destroying them releases the connections.
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


class MainWindowTests(GuiBase):
    def test_default_size(self):
        # Offscreen clamps windows to its 800x600 screen, so the
        # intended default size is a class constant, checked here.
        self.assertEqual(MainWindow.DEFAULT_WIDTH, 1100)
        self.assertEqual(MainWindow.DEFAULT_HEIGHT, 700)

    def test_geometry_remembered(self):
        main = self._show(MainWindow)
        main.resize(700, 650)
        main.close()
        QTest.qWait(100)
        again = MainWindow(self.state)
        self.windows.append(again)
        self.assertEqual(again.width(), 700)
        self.assertEqual(again.height(), 650)

    def test_idea_saved(self):
        main = self._show(MainWindow)
        main.idea_edit.setPlainText("A stool in a workshop.")
        main._save_idea()
        self.assertEqual(self.state.project.idea,
                         "A stool in a workshop.")
        project_file = self.state.project_dir / "project.json"
        self.assertTrue(project_file.exists())

    def test_idea_saved_without_a_project(self):
        # Typing the first idea creates the project; the idea must
        # survive the creation (new_project replaces the project
        # object, so saving the idea first throws it away).
        state = AppState()
        self.assertIsNone(state.project_dir)
        main = MainWindow(state)
        self.windows.append(main)
        main.show()
        QTest.qWait(50)
        main.idea_edit.setPlainText("A stool in a workshop.")
        main._save_idea()
        self.assertEqual(state.project.idea, "A stool in a workshop.")
        self.assertIsNotNone(state.project_dir)
        self.assertTrue(
            (state.project_dir / "project.json").exists())
        # And the edit box still shows it after the refresh.
        self.assertEqual(main.idea_edit.toPlainText(),
                         "A stool in a workshop.")

    def test_tiles_count_scenes(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a", narration="b"),
            scenes_mod.Scene(prompt="c", narration="d"),
        ]
        self.state.project_changed.emit()
        main = self._show(MainWindow)
        self.assertEqual(main.scenes_tile.number_label.text(), "2")
        self.assertEqual(main.stills_tile.number_label.text(), "0/2")
        self.assertEqual(main.voice_tile.number_label.text(), "0/2")
        self.assertEqual(main.film_tile.number_label.text(), "Not yet")


class ScenesWindowTests(GuiBase):
    def test_add_save_remove_scene(self):
        window = self._show(ScenesWindow)
        window._add_scene()
        self.assertEqual(window.scene_list.count(), 1)
        window.prompt_edit.setPlainText("a wooden stool")
        window.narration_edit.setPlainText("A stool sits here.")
        window._save_scene()
        scene = self.state.project.scenes[0]
        self.assertEqual(scene.prompt, "a wooden stool")
        self.assertEqual(scene.narration, "A stool sits here.")
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.Yes):
            window._remove_scene()
        self.assertEqual(len(self.state.project.scenes), 0)

    def test_remove_cancelled_by_default_no(self):
        window = self._show(ScenesWindow)
        window._add_scene()
        with mock.patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.No):
            window._remove_scene()
        self.assertEqual(len(self.state.project.scenes), 1)

    def test_ollama_unreachable_message(self):
        window = self._show(ScenesWindow)
        QTest.qWait(1500)
        self.assertFalse(window._ollama_ok)
        self.assertIn("not running", window.ollama_label.text())


class StillsWindowTests(GuiBase):
    def test_cards_show_not_ready(self):
        window = self._show(StillsWindow)
        self.assertIn("Not set up", window.fast_card.status.text())
        self.assertIn("missing", window.steady_card.status.text())

    def test_list_shows_missing_pictures(self):
        self.state.project.scenes = [
            scenes_mod.Scene(prompt="a", narration="b")]
        self.state.project_changed.emit()
        window = self._show(StillsWindow)
        self.assertEqual(window.still_list.count(), 1)
        self.assertIn("no picture yet",
                      window.still_list.item(0).text())


class VoiceWindowTests(GuiBase):
    def test_voice_status_shown(self):
        from hicksfield import voice
        if voice.find_piper() is not None:
            self.skipTest("piper is installed in this environment")
        window = self._show(VoiceWindow)
        self.assertIn("missing", window.voice_status.text().lower())


class ExportWindowTests(GuiBase):
    def test_build_buttons_disabled_without_engine(self):
        window = self._show(ExportWindow)
        self.assertFalse(window.fast_build.isEnabled())
        self.assertFalse(window.steady_build.isEnabled())

    def test_ffmpeg_status_shown(self):
        window = self._show(ExportWindow)
        self.assertIn("ffmpeg", window.ffmpeg_label.text().lower())


class VisibleTextTests(GuiBase):
    def test_no_em_dash_or_ampersand_anywhere(self):
        windows = [
            self._show(MainWindow),
            self._show(ScenesWindow),
            self._show(StillsWindow),
            self._show(VoiceWindow),
            self._show(ExportWindow),
            self._show(HowToWindow),
            self._show(DebugReportWindow),
        ]
        QTest.qWait(500)
        for window in windows:
            for text in visible_texts(window):
                self.assertNotIn("\u2014", text,
                                 f"{window.windowTitle()}: {text!r}")
                self.assertNotIn("&", text,
                                 f"{window.windowTitle()}: {text!r}")

    def test_howto_topics_all_present(self):
        from hicksfield.howto import HOWTO_TOPICS
        window = self._show(HowToWindow)
        self.assertEqual(window.topic_list.count(), len(HOWTO_TOPICS))
        window.topic_list.setCurrentRow(3)
        self.assertIn("Stills", window.text.toPlainText())

    def test_howto_text_clean(self):
        from hicksfield.howto import HOWTO_TOPICS
        for title, text in HOWTO_TOPICS:
            self.assertNotIn("\u2014", title + text, title)
            self.assertNotIn("&", title + text, title)

    def test_source_files_clean(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "hicksfield"
        for name in ("gui.py", "howto.py", "style.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("\u2014", text, name)
            self.assertNotIn("&", text, name)
        for name in ("errors.py", "ollama.py", "stills.py", "voice.py",
                     "video.py", "pipeline.py", "downloader.py",
                     "machine.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("\u2014", text, name)


class JobLockTests(unittest.TestCase):
    def test_one_job_at_a_time(self):
        lock = JobLock()
        self.assertTrue(lock.try_start("first"))
        self.assertFalse(lock.try_start("second"))
        self.assertEqual(lock.owner, "first")
        lock.finish()
        self.assertTrue(lock.try_start("second"))


class WorkerTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        require_qt(self)
        self.app = app()

    def test_worker_returns_result(self):
        worker = Worker(lambda report, cancel: 42)
        done = []
        worker.done.connect(done.append)
        worker.start()
        self.assertTrue(wait_for(lambda: len(done) == 1, 5000))
        self.assertEqual(done, [42])
        worker.wait_finished()

    def test_worker_forwards_error(self):
        def _fail(_report, _cancel):
            raise ValueError("boom")

        worker = Worker(_fail)
        errors = []
        worker.error.connect(errors.append)
        worker.start()
        self.assertTrue(wait_for(lambda: len(errors) == 1, 5000))
        self.assertIsInstance(errors[0], ValueError)
        worker.wait_finished()

    def test_worker_cancel(self):
        import time

        def _spin(_report, cancel):
            while not cancel.is_set():
                time.sleep(0.05)
            from hicksfield.errors import CancelledError
            raise CancelledError()

        worker = Worker(_spin)
        errors = []
        worker.error.connect(errors.append)
        worker.start()
        QTest.qWait(200)
        worker.cancel()
        self.assertTrue(wait_for(lambda: len(errors) == 1, 5000))
        self.assertEqual(errors[0].__class__.__name__, "CancelledError")
        worker.wait_finished()


class SingleInstanceTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        require_qt(self)
        self.app = app()

    def test_second_instance_refused_and_raises_first(self):
        lock_path = self.tmp / "hicksfield.lock"
        raised = []
        first = SingleInstance(lock_path, lambda: raised.append(True))
        self.assertTrue(first.try_acquire())
        second = SingleInstance(lock_path, lambda: None)
        self.assertFalse(second.try_acquire())
        self.assertTrue(wait_for(lambda: len(raised) == 1, 3000))
        first.release()
        second.release()
        QTest.qWait(100)


if __name__ == "__main__":
    unittest.main()
