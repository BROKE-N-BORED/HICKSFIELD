"""v0.4.0 Group F polish: copy pass (42), debug Refresh (43),
dead code and dead fields (48)."""

import os
import unittest
from pathlib import Path
from unittest import mock

from hicksfield import ollama, scenes as scenes_mod, video
from hicksfield.errors import FFmpegMissingError
from hicksfield.howto import topic_text
from tests.helpers import TempEnvTestCase, app, require_qt

ROOT = Path(__file__).resolve().parent.parent


class CopyPassTests(unittest.TestCase):
    def test_howto_shortcuts_mentions_ctrl_z(self):
        text = topic_text("Keyboard shortcuts")
        self.assertIn("Ctrl+Z", text)
        self.assertIn("undoes", text)

    def test_howto_trouble_mentions_install_ffmpeg(self):
        text = topic_text("If something goes wrong")
        self.assertIn("Install ffmpeg", text)
        self.assertIn("Export window", text)
        self.assertNotIn("installer again", text)

    def test_howto_debug_report_opens_a_window(self):
        text = topic_text("If something goes wrong")
        self.assertIn("opens a window", text)
        self.assertIn("Copy the report", text)
        self.assertNotIn("copies a report about", text)

    def test_ffmpeg_missing_hint_points_at_export(self):
        hint = FFmpegMissingError().hint
        self.assertIn("Install ffmpeg", hint)
        self.assertIn("Export window", hint)
        self.assertNotIn("installer again", hint)

    def test_readme_names_three_voices_and_sizes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Lessac", readme)
        self.assertIn("Ryan", readme)
        self.assertIn("Alan", readme)
        self.assertIn("about 120 MB for Ryan", readme)
        self.assertIn("About 60 MB for Lessac", readme)
        self.assertNotIn("About 60 MB.\n", readme)


class DeadCodeTests(unittest.TestCase):
    def test_parse_scenes_reply_is_gone(self):
        self.assertFalse(hasattr(ollama, "parse_scenes_reply"))

    def test_probe_duration_is_gone(self):
        self.assertFalse(hasattr(video, "probe_duration"))

    def test_find_ffprobe_is_gone_too(self):
        # Its only caller was probe_duration; leaving it would
        # leave the dead code half-dead.
        self.assertFalse(hasattr(video, "find_ffprobe"))

    def test_scenes_new_project_keeps_name(self):
        project = scenes_mod.new_project("My Film", idea="A stool.")
        self.assertEqual(project.name, "My Film")
        self.assertEqual(project.idea, "A stool.")


class FilmDatesTextTests(unittest.TestCase):
    def test_both_dates(self):
        from hicksfield.gui import film_dates_text
        project = scenes_mod.Project(
            created="2026-11-15T10:00:00+00:00",
            updated="2026-11-16T16:00:00+00:00")
        self.assertEqual(film_dates_text(project),
                         "Started 15 Nov 2026. Last saved 16 Nov 2026.")

    def test_same_day_shows_started_only(self):
        from hicksfield.gui import film_dates_text
        project = scenes_mod.Project(
            created="2026-11-16T10:00:00+00:00",
            updated="2026-11-16T16:00:00+00:00")
        self.assertEqual(film_dates_text(project),
                         "Started 16 Nov 2026.")

    def test_no_dates_is_empty(self):
        from hicksfield.gui import film_dates_text
        self.assertEqual(film_dates_text(scenes_mod.Project()), "")

    def test_bad_date_is_empty(self):
        from hicksfield.gui import film_dates_text
        project = scenes_mod.Project(created="not a date",
                                     updated="also not")
        self.assertEqual(film_dates_text(project), "")


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
        from hicksfield.gui import AppState
        self.state = AppState()
        self.state.new_project("test film")
        self.windows = []

    def tearDown(self):
        from PySide6.QtTest import QTest
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.windows = []
        QTest.qWait(200)
        super().tearDown()

    def _show(self, cls):
        from PySide6.QtTest import QTest
        window = cls(self.state)
        self.windows.append(window)
        window.show()
        QTest.qWait(50)
        return window


class ProjectNameTests(GuiBase):
    def test_new_project_keeps_the_name(self):
        self.assertEqual(self.state.project.name, "test film")
        loaded = scenes_mod.load_project(self.state.project_dir)
        self.assertEqual(loaded.name, "test film")

    def test_project_name_prefers_the_film_name(self):
        self.assertEqual(self.state.project_name(), "test film")

    def test_project_name_falls_back_to_folder(self):
        # A film saved before names were kept still says
        # "Untitled film": the folder name is more useful.
        self.state.project.name = "Untitled film"
        self.assertEqual(self.state.project_name(),
                         self.state.project_dir.name)

    def test_project_name_without_a_project(self):
        from hicksfield.gui import AppState
        self.assertEqual(AppState().project_name(), "No film yet")


class MainWindowFilmTests(GuiBase):
    def test_home_shows_film_name_and_dates(self):
        from hicksfield.gui import MainWindow
        main = self._show(MainWindow)
        self.assertEqual(main.project_label.text(), "Film: test film")
        dates = main.project_dates_label.text()
        self.assertTrue(dates.startswith("Started "),
                        f"dates label was {dates!r}")
        self.assertTrue(main.project_dates_label.isVisible())

    def test_home_falls_back_to_folder_name(self):
        from hicksfield.gui import MainWindow
        self.state.project.name = "Untitled film"
        main = self._show(MainWindow)
        self.assertEqual(main.project_label.text(),
                         f"Film: {self.state.project_dir.name}")


class TooltipTests(GuiBase):
    def test_stills_make_tooltip_mentions_remake(self):
        from hicksfield.gui import StillsWindow
        window = self._show(StillsWindow)
        tip = window.make_button.toolTip()
        self.assertIn("have none yet", tip)
        self.assertIn("remake", tip)

    def test_voice_make_tooltip_mentions_speak_again(self):
        from hicksfield.gui import VoiceWindow
        window = self._show(VoiceWindow)
        tip = window.make_button.toolTip()
        self.assertIn("have none yet", tip)
        self.assertIn("speak again", tip)


class ShortcutsDialogTests(GuiBase):
    def test_dialog_lists_ctrl_z(self):
        from PySide6.QtWidgets import QLabel
        from hicksfield.gui import MainWindow
        main = self._show(MainWindow)
        dialog = main._shortcuts_dialog()
        self.windows.append(dialog)
        texts = [label.text()
                 for label in dialog.findChildren(QLabel)]
        self.assertTrue(any("Ctrl+Z" in text for text in texts),
                        f"no Ctrl+Z in {texts!r}")


class ModelNoteTests(GuiBase):
    def test_no_fitting_model_points_below_picker(self):
        from hicksfield.gui import ScenesWindow
        window = self._show(ScenesWindow)
        window._model_infos = {
            "huge:70b": {"name": "huge:70b", "size": 10 ** 13,
                         "capabilities": ["chat"]},
        }
        window._show_model_recommendation()
        note = window.model_note.text()
        self.assertIn("below the picker", note)
        self.assertNotIn("under a model", note)


class DebugRefreshTests(GuiBase):
    def test_refresh_button_exists(self):
        from PySide6.QtWidgets import QPushButton
        from hicksfield.gui import DebugReportWindow
        window = self._show(DebugReportWindow)
        labels = [button.text()
                  for button in window.findChildren(QPushButton)]
        self.assertIn("Refresh", labels)
        self.assertIn("Copy the report", labels)
        self.assertIn("Close", labels)

    def test_refresh_rebuilds_the_report(self):
        from PySide6.QtWidgets import QPushButton
        from hicksfield import debuglog
        from hicksfield.gui import DebugReportWindow
        window = self._show(DebugReportWindow)
        before = window.text.toPlainText()
        self.assertIn("debug report", before)
        refresh = next(
            button for button in window.findChildren(QPushButton)
            if button.text() == "Refresh")
        with mock.patch.object(debuglog, "build_debug_report",
                               return_value="fresh report") as build:
            refresh.click()
            build.assert_called_once()
        self.assertEqual(window.text.toPlainText(), "fresh report")
        self.assertEqual(window.status_label.text(), "Refreshed.")


if __name__ == "__main__":
    unittest.main()
