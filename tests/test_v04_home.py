"""v0.4.0 Group E: Home, first run, installer.

Task List 3 items 3, 9, 10, 12, 21, 22, 23, 24, 25.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import installer  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

from hicksfield import scenes as scenes_mod  # noqa: E402
from hicksfield import stills  # noqa: E402
from hicksfield.errors import HicksFieldError  # noqa: E402
from hicksfield.gui import (  # noqa: E402
    AppState,
    MainWindow,
    ScenesWindow,
    _load_recent,
    _ollama_reachable,
    _recent_label,
    settings,
)
from hicksfield.howto import topic_text  # noqa: E402
from tests.helpers import (  # noqa: E402
    TempEnvTestCase,
    app,
    require_qt,
    write_png,
    write_wav,
)

TREE = Path(__file__).resolve().parent.parent


class HomeBase(TempEnvTestCase):
    """A MainWindow/ScenesWindow on a fresh temp project, Qt offscreen."""

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

    def _two_scenes_one_stale(self):
        """Two scenes with real files: scene 1 fresh, scene 2 stale."""
        project_dir = self.state.project_dir
        write_png(project_dir / "stills" / "scene-01.png")
        write_png(project_dir / "stills" / "scene-02.png")
        write_wav(project_dir / "audio" / "scene-01.wav")
        write_wav(project_dir / "audio" / "scene-02.wav")
        self.state.project.scenes = [
            scenes_mod.Scene(
                prompt="a stool", narration="A stool sits here.",
                still="stills/scene-01.png", still_prompt="a stool",
                audio="audio/scene-01.wav",
                voice_text="A stool sits here."),
            scenes_mod.Scene(
                prompt="a chair now", narration="A chair now.",
                still="stills/scene-02.png",
                still_prompt="a chair before",
                audio="audio/scene-02.wav",
                voice_text="A chair before."),
        ]
        self.state.project_changed.emit()


class RecentFilmsTests(HomeBase):
    """Item 3: a 1-item recent list reads back as a bare string."""

    def test_load_recent_wraps_a_lone_string(self):
        folder = self.tmp / "films" / "dusk"
        folder.mkdir(parents=True)
        settings().setValue("recent", str(folder))
        self.assertEqual(_load_recent(), [str(folder)])

    def test_load_recent_drops_a_non_list_value(self):
        settings().setValue("recent", 42)
        self.assertEqual(_load_recent(), [])

    def test_remember_recent_survives_a_bare_string(self):
        main = self._show(MainWindow)
        first = self.tmp / "films" / "first"
        first.mkdir(parents=True)
        # QSettings stored the 1-item list as a bare string.
        settings().setValue("recent", str(first))
        second = self.tmp / "films" / "second"
        second.mkdir(parents=True)
        # Used to crash: 'str' object has no attribute 'insert'.
        main._remember_recent(second)
        self.assertEqual(_load_recent(), [str(second), str(first)])

    def test_recent_menu_does_not_iterate_characters(self):
        main = self._show(MainWindow)
        folder = self.tmp / "films" / "dusk"
        folder.mkdir(parents=True)
        settings().setValue("recent", str(folder))
        main._refresh_recent_menu()
        actions = main._recent_menu.actions()
        # Used to be one one-character entry per character.
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text(), "films/dusk")


class RecentMenuTests(HomeBase):
    """Item 25: tell same-named films apart, drop deleted folders."""

    def test_recent_label_shows_parent(self):
        self.assertEqual(_recent_label("/home/u/Films/dusk"),
                         "Films/dusk")
        self.assertEqual(_recent_label("/dusk"), "dusk")

    def test_menu_tells_same_named_films_apart(self):
        main = self._show(MainWindow)
        first = self.tmp / "reels-a" / "dusk"
        second = self.tmp / "reels-b" / "dusk"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        settings().setValue("recent", [str(first), str(second)])
        main._refresh_recent_menu()
        actions = main._recent_menu.actions()
        self.assertEqual([a.text() for a in actions],
                         ["reels-a/dusk", "reels-b/dusk"])
        self.assertEqual([a.toolTip() for a in actions],
                         [str(first), str(second)])

    def test_menu_drops_deleted_folders(self):
        main = self._show(MainWindow)
        gone = self.tmp / "gone" / "film"
        kept = self.tmp / "kept" / "film"
        kept.mkdir(parents=True)
        settings().setValue("recent", [str(gone), str(kept)])
        main._refresh_recent_menu()
        actions = main._recent_menu.actions()
        self.assertEqual([a.text() for a in actions], ["kept/film"])
        # The pruned list is saved, not just hidden.
        self.assertEqual(_load_recent(), [str(kept)])


class HomeTilesTests(HomeBase):
    """Item 12: home tiles count stale assets as to make, not ready."""

    def test_tiles_count_stale_assets_as_not_ready(self):
        self._two_scenes_one_stale()
        main = self._show(MainWindow)
        self.assertEqual(main.stills_tile.number_label.text(), "1/2")
        self.assertEqual(main.voice_tile.number_label.text(), "1/2")

    def test_tiles_count_fresh_assets_as_ready(self):
        project_dir = self.state.project_dir
        write_png(project_dir / "stills" / "scene-01.png")
        write_wav(project_dir / "audio" / "scene-01.wav")
        self.state.project.scenes = [
            scenes_mod.Scene(
                prompt="a stool", narration="A stool sits here.",
                still="stills/scene-01.png", still_prompt="a stool",
                audio="audio/scene-01.wav",
                voice_text="A stool sits here."),
        ]
        self.state.project_changed.emit()
        main = self._show(MainWindow)
        self.assertEqual(main.stills_tile.number_label.text(), "1/1")
        self.assertEqual(main.voice_tile.number_label.text(), "1/1")


class SceneRowTests(HomeBase):
    """Item 12: Scenes rows earn the green edge only when fresh."""

    def test_stale_scene_has_no_green_edge(self):
        self._two_scenes_one_stale()
        window = self._show(ScenesWindow)
        self.assertEqual(window.scene_list.count(), 2)
        fresh = window.scene_list.itemWidget(
            window.scene_list.item(0))
        stale = window.scene_list.itemWidget(
            window.scene_list.item(1))
        self.assertEqual(fresh.property("role"), "scene-done")
        self.assertEqual(stale.property("role"), "scene")
        marks = " ".join(label.text()
                         for label in stale.findChildren(QLabel))
        self.assertIn("Picture out of date", marks)
        self.assertIn("Voice out of date", marks)


class HomeButtonTests(HomeBase):
    """Item 9: the home button breaks into scenes when ready."""

    def test_button_starts_breakdown_when_ready(self):
        self.state.project.idea = "A stool in a workshop."
        main = self._show(MainWindow)
        window = main.open_window("scenes")
        self.windows.append(window)
        QTest.qWait(200)  # let the model check finish and clear
        window.model_combo.addItem("test-model")
        window.model_combo.setCurrentIndex(0)
        started = []

        def _fake_break():
            window._breakdown_running = True
            started.append(True)

        with mock.patch.object(window, "_break_into_scenes",
                               side_effect=_fake_break):
            main._break_from_home()
        self.assertEqual(started, [True])

    def test_button_only_opens_when_no_model_picked(self):
        self.state.project.idea = "A stool in a workshop."
        main = self._show(MainWindow)
        window = main.open_window("scenes")
        self.windows.append(window)
        QTest.qWait(200)  # let the model check finish
        self.assertEqual(window.model_combo.currentText(), "")
        with mock.patch.object(window, "_break_into_scenes") as fake:
            main._break_from_home()
        fake.assert_not_called()
        self.assertTrue(window.isVisible())

    def test_button_only_opens_when_no_idea(self):
        main = self._show(MainWindow)
        window = main.open_window("scenes")
        self.windows.append(window)
        window.model_combo.addItem("test-model")
        window.model_combo.setCurrentIndex(0)
        with mock.patch.object(window, "_break_into_scenes") as fake:
            main._break_from_home()
        fake.assert_not_called()

    def test_start_breakdown_skips_when_already_running(self):
        window = self._show(ScenesWindow)
        self.state.project.idea = "A stool in a workshop."
        window.model_combo.addItem("test-model")
        window.model_combo.setCurrentIndex(0)
        window._breakdown_running = True
        with mock.patch.object(window, "_break_into_scenes") as fake:
            self.assertFalse(window.start_breakdown_if_ready())
        fake.assert_not_called()

    def test_button_tooltip_says_what_it_does(self):
        main = self._show(MainWindow)
        self.assertIn("breakdown starts straight away",
                      main.scenes_button.toolTip())


class HowToTests(unittest.TestCase):
    """Item 9: the HowTo topic matches the button."""

    def test_your_idea_matches_home_button(self):
        text = topic_text("Your idea")
        self.assertIn("Break into scenes", text)
        self.assertIn("breakdown starts straight away", text)


class OllamaCheckTests(unittest.TestCase):
    """Item 24: the checklist check retries once, with more time."""

    def test_retries_once_after_a_miss(self):
        with mock.patch("hicksfield.gui.ollama.is_reachable",
                        side_effect=[False, True]) as reachable:
            self.assertTrue(_ollama_reachable())
        self.assertEqual(reachable.call_count, 2)
        reachable.assert_called_with(10)

    def test_down_after_two_misses(self):
        with mock.patch("hicksfield.gui.ollama.is_reachable",
                        side_effect=[False, False]) as reachable:
            self.assertFalse(_ollama_reachable())
        self.assertEqual(reachable.call_count, 2)

    def test_no_retry_when_up(self):
        with mock.patch("hicksfield.gui.ollama.is_reachable",
                        return_value=True) as reachable:
            self.assertTrue(_ollama_reachable())
        self.assertEqual(reachable.call_count, 1)


class EnsureToolsTests(TempEnvTestCase):
    """Item 21: missing git/cmake are installed in place, no terminal."""

    def _which_that_finds_after_install(self, missing, state):
        def _which(name):
            if name in missing and not state["installed"]:
                return None
            return f"/usr/bin/{name}"

        return _which

    def _install_that_marks(self, state):
        def _install(packages, progress_cb=None):
            state["installed"] = True
            return None

        return _install

    def test_installs_missing_tool_and_continues(self):
        state = {"installed": False}
        with mock.patch("hicksfield.stills.shutil.which",
                        side_effect=self._which_that_finds_after_install(
                            {"git"}, state)), \
                mock.patch("hicksfield.stills.sysdeps.install_packages",
                           side_effect=self._install_that_marks(state)) \
                as install:
            stills._ensure_tools(
                ["git"], ["git"], "the image tool comes from git",
                lambda _f, _d: None)
        install.assert_called_once()
        self.assertEqual(install.call_args[0][0], ["git"])

    def test_present_tools_install_nothing(self):
        with mock.patch("hicksfield.stills.shutil.which",
                        return_value="/usr/bin/git"), \
                mock.patch("hicksfield.stills.sysdeps.install_packages") \
                as install:
            stills._ensure_tools(
                ["git"], ["git"], "the image tool comes from git",
                lambda _f, _d: None)
        install.assert_not_called()

    def test_declined_install_has_no_terminal_command(self):
        with mock.patch("hicksfield.stills.shutil.which",
                        return_value=None), \
                mock.patch("hicksfield.stills.sysdeps.install_packages",
                           return_value="the permission step was "
                           "cancelled"):
            with self.assertRaises(HicksFieldError) as ctx:
                stills._ensure_tools(
                    ["git"], ["git"], "the image tool comes from git",
                    lambda _f, _d: None)
        text = ctx.exception.plain_text()
        self.assertIn("permission step", text)
        self.assertNotIn("sudo", text)
        self.assertNotIn("apt install", text)

    def test_setup_checkout_installs_git_then_clones(self):
        engine = stills.SDCppEngine(base_dir=self.tmp / "steady")
        state = {"installed": False}

        def _run_logged(cmd, timeout_s=None, cwd=None,
                        cancel_event=None):
            engine.checkout.mkdir(parents=True, exist_ok=True)
            return 0, ""

        with mock.patch("hicksfield.stills.shutil.which",
                        side_effect=self._which_that_finds_after_install(
                            {"git"}, state)), \
                mock.patch("hicksfield.stills.sysdeps.install_packages",
                           side_effect=self._install_that_marks(state)) \
                as install, \
                mock.patch("hicksfield.stills.run_logged",
                           side_effect=_run_logged) as clone:
            engine._setup_checkout(lambda _f, _d: None, None)
        install.assert_called_once()
        self.assertEqual(install.call_args[0][0], ["git"])
        clone.assert_called_once()
        self.assertTrue(engine.checkout.exists())

    def test_setup_build_installs_cmake_and_gpp(self):
        engine = stills.SDCppEngine(base_dir=self.tmp / "steady")
        engine.checkout.mkdir(parents=True, exist_ok=True)
        state = {"installed": False}

        def _run_logged(cmd, timeout_s=None, cwd=None,
                        cancel_event=None):
            engine.binary_path.parent.mkdir(parents=True, exist_ok=True)
            engine.binary_path.write_bytes(b"x")
            return 0, ""

        with mock.patch("hicksfield.stills.shutil.which",
                        side_effect=self._which_that_finds_after_install(
                            {"cmake", "g++"}, state)), \
                mock.patch("hicksfield.stills.sysdeps.install_packages",
                           side_effect=self._install_that_marks(state)) \
                as install, \
                mock.patch("hicksfield.stills.run_logged",
                           side_effect=_run_logged):
            engine._setup_build(lambda _f, _d: None, None)
        install.assert_called_once()
        self.assertEqual(install.call_args[0][0], ["cmake", "g++"])
        self.assertTrue(engine.binary_path.exists())


class InstallerLibTests(unittest.TestCase):
    """Item 22: the libEGL warning, and the Qt system libs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _verify(self, runs, install_result=None):
        messages = []
        warnings = []
        with mock.patch.object(installer, "run",
                               side_effect=runs), \
                mock.patch.object(
                    installer.sysdeps, "install_packages",
                    return_value=install_result) as install:
            installer.verify(self.tmp, self.tmp / "python",
                             messages.append, warnings)
        return messages, warnings, install

    def test_missing_lib_regex_drops_trailing_colon(self):
        match = installer._MISSING_LIB_RE.search(
            "libEGL.so.1: cannot open shared object file")
        self.assertEqual(match.group(1), "libEGL.so.1")
        match = installer._MISSING_LIB_RE.search("(libGL.so.1)")
        self.assertEqual(match.group(1), "libGL.so.1")

    def test_verify_installs_missing_qt_lib_and_retries(self):
        runs = [
            (0, "HicksField 0.6.0\n", ""),
            (1, "", "error while loading shared libraries: "
             "libEGL.so.1: cannot open shared object file"),
            (0, "qt ok\n", ""),
        ]
        messages, warnings, install = self._verify(runs)
        install.assert_called_once()
        self.assertEqual(install.call_args[0][0], ["libegl1"])
        self.assertEqual(warnings, [])
        self.assertTrue(any("Window toolkit check passed" in m
                            for m in messages))

    def test_verify_unknown_lib_warns_plainly(self):
        runs = [
            (0, "HicksField 0.6.0\n", ""),
            (1, "", "libSomething.so.3: cannot open shared object"),
        ]
        _messages, warnings, install = self._verify(runs)
        install.assert_not_called()
        self.assertEqual(len(warnings), 1)
        self.assertIn("libSomething.so.3 is missing", warnings[0])
        self.assertNotIn("libSomething.so.3:", warnings[0])
        self.assertIn("does not know which system package", warnings[0])

    def test_verify_declined_qt_install_warns(self):
        runs = [
            (0, "HicksField 0.6.0\n", ""),
            (1, "", "libEGL.so.1: cannot open shared object file"),
        ]
        _messages, warnings, install = self._verify(
            runs, install_result="the permission step was cancelled")
        install.assert_called_once()
        self.assertEqual(len(warnings), 1)
        self.assertIn("libEGL.so.1 is missing", warnings[0])
        self.assertNotIn("libEGL.so.1:", warnings[0])
        self.assertIn("could not be installed", warnings[0])
        self.assertIn("permission step", warnings[0])
        self.assertNotIn("sudo", warnings[0])

    def test_display_path_shortens_home(self):
        with mock.patch("pathlib.Path.home",
                        return_value=Path("/home/tester")):
            self.assertEqual(
                installer._display_path(
                    Path("/home/tester/.local/share/hicksfield-app")),
                "~/.local/share/hicksfield-app")
            self.assertEqual(installer._display_path(Path("/opt/x")),
                             "/opt/x")


class InstallerModesTests(unittest.TestCase):
    """Item 10: the installer sets +x on the installed copies."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_set_installed_modes_makes_scripts_executable(self):
        target = self.tmp / "app"
        target.mkdir()
        for name in ("install.sh", "run.sh", "installer.py"):
            path = target / name
            path.write_text("x", encoding="utf-8")
            path.chmod(0o644)
        installer.set_installed_modes(target)
        for name in ("install.sh", "run.sh", "installer.py"):
            mode = stat.S_IMODE((target / name).stat().st_mode)
            self.assertEqual(mode, 0o755, name)

    def test_install_makes_installed_scripts_executable(self):
        target = self.tmp / "hicksfield-app"

        def _fake_copy(src, tgt, log):
            tgt.mkdir(parents=True, exist_ok=True)
            for name in ("install.sh", "run.sh", "installer.py"):
                path = tgt / name
                path.write_text("x", encoding="utf-8")
                path.chmod(0o644)

        with mock.patch.object(installer.sysdeps, "missing_packages",
                               return_value=[]), \
                mock.patch.object(installer, "copy_tree",
                                  side_effect=_fake_copy), \
                mock.patch.object(installer, "make_venv",
                                  return_value=self.tmp / "python"), \
                mock.patch.object(installer, "install_deps"), \
                mock.patch.object(installer, "render_icons"), \
                mock.patch.object(installer, "write_desktop"), \
                mock.patch.object(installer, "verify"):
            warnings = installer.install(target, lambda m: None,
                                         lambda f: None)
        self.assertEqual(warnings, [])
        for name in ("install.sh", "run.sh", "installer.py"):
            mode = (target / name).stat().st_mode
            self.assertTrue(mode & 0o111, name)

    def test_readme_documents_allow_launching(self):
        text = (TREE / "README.md").read_text(encoding="utf-8")
        self.assertIn("Allow Launching", text)
        self.assertIn("Right-click", text)
        self.assertIn("text editor", text)

    def test_install_sh_needs_no_exec_bits(self):
        text = (TREE / "install.sh").read_text(encoding="utf-8")
        self.assertIn("python3 installer.py", text)
        self.assertNotIn("./installer.py", text)


class InstallerCancelTests(unittest.TestCase):
    """Item 22: Cancel stops the install."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_cancelled_between_steps(self):
        event = threading.Event()
        event.set()
        with mock.patch.object(installer.sysdeps, "missing_packages",
                               return_value=[]):
            with self.assertRaises(installer.InstallCancelled):
                installer.install(self.tmp / "app", lambda m: None,
                                  lambda f: None, cancel_event=event)

    def test_run_cancel_kills_a_running_command(self):
        event = threading.Event()
        timer = threading.Timer(0.3, event.set)
        timer.start()
        try:
            start = time.monotonic()
            with self.assertRaises(installer.InstallCancelled):
                installer.run([sys.executable, "-c",
                               "import time; time.sleep(30)"],
                              timeout=60, cancel_event=event)
            self.assertLess(time.monotonic() - start, 10)
        finally:
            timer.cancel()

    def test_run_timeout_still_applies_with_cancel(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            installer.run([sys.executable, "-c",
                           "import time; time.sleep(30)"],
                          timeout=1,
                          cancel_event=threading.Event())


class RunShTests(unittest.TestCase):
    """Item 23: a failed launch shows a dialog, not silence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.app_dir = self.tmp / "hicksfield-app"
        self.app_dir.mkdir()
        shutil.copy2(TREE / "run.sh", self.app_dir / "run.sh")
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.log = self.tmp / "zenity.log"
        zenity = self.bin_dir / "zenity"
        zenity.write_text(
            "#!/bin/sh\n"
            "echo \"$@\" >> \"$ZENITY_LOG\"\n"
            "exit ${ZENITY_EXIT:-1}\n", encoding="utf-8")
        zenity.chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = (str(self.bin_dir) + os.pathsep
                            + self.env["PATH"])
        self.env["ZENITY_LOG"] = str(self.log)
        self.env.pop("DISPLAY", None)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(self.app_dir / "run.sh")],
            capture_output=True, text=True, timeout=120, env=env)

    def _fake_venv(self, script):
        python = self.app_dir / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n" + script + "\n",
                          encoding="utf-8")
        python.chmod(0o755)

    def _dialog_log(self):
        if not self.log.exists():
            return ""
        return self.log.read_text(encoding="utf-8")

    def test_missing_venv_shows_dialog(self):
        proc = self._run({"ZENITY_EXIT": "1"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not installed yet", proc.stderr)
        log = self._dialog_log()
        self.assertIn("--question", log)
        self.assertIn("not installed yet", log)
        self.assertIn("Install again", log)

    def test_missing_venv_reinstall_runs_installer(self):
        marker = self.tmp / "installer-ran"
        (self.app_dir / "installer.py").write_text(
            "import pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8")
        # A python3 that claims tkinter exists, then runs the real
        # one: the reinstall path takes the GUI installer.
        fake_python = self.bin_dir / "python3"
        fake_python.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-c\" ] && [ \"$2\" = \"import tkinter\" ]; "
            "then\n"
            "  exit 0\n"
            "fi\n"
            f"exec {sys.executable} \"$@\"\n", encoding="utf-8")
        fake_python.chmod(0o755)
        proc = self._run({"ZENITY_EXIT": "0"})
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(marker.exists())

    def test_missing_venv_without_dialog_tools_uses_stderr(self):
        # No zenity, no kdialog: PATH holds only a python3, and no
        # display, so the tkinter dialog cannot open either.
        lone_bin = self.tmp / "lone-bin"
        lone_bin.mkdir()
        (lone_bin / "python3").symlink_to(sys.executable)
        env = dict(os.environ)
        env["PATH"] = str(lone_bin)
        env.pop("DISPLAY", None)
        proc = subprocess.run(
            [shutil.which("bash"), str(self.app_dir / "run.sh")],
            capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not installed yet", proc.stderr)

    def test_fast_failure_shows_dialog(self):
        self._fake_venv("exit 3")
        proc = self._run({"ZENITY_EXIT": "1"})
        self.assertEqual(proc.returncode, 3)
        log = self._dialog_log()
        self.assertIn("--question", log)
        self.assertIn("did not start", log)

    def test_clean_exit_shows_no_dialog(self):
        self._fake_venv("exit 0")
        proc = self._run()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self._dialog_log(), "")

    def test_slow_failure_shows_no_dialog(self):
        # A crash after the app has been running a while is not a
        # launch failure: no reinstall offer.
        self._fake_venv("sleep 11; exit 3")
        proc = self._run()
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self._dialog_log(), "")


if __name__ == "__main__":
    unittest.main()
