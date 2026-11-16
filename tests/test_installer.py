import shutil
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import installer  # noqa: E402


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(__import__("tempfile").mkdtemp())
        self.tmp = self._tmp

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_desktop_file_bakes_paths(self):
        target = self.tmp / "hicksfield-app"
        target.mkdir()
        messages = []
        with mock.patch("pathlib.Path.home", return_value=self.tmp):
            installer.write_desktop(target, messages.append)
        desktop = (self.tmp / ".local" / "share" / "applications"
                   / "hicksfield.desktop")
        self.assertTrue(desktop.exists())
        text = desktop.read_text(encoding="utf-8")
        self.assertIn(f"Exec={target / 'run.sh'}", text)
        self.assertNotIn("%k", text)
        self.assertNotIn('"', text)
        self.assertIn("Icon=hicksfield", text)
        self.assertIn("StartupWMClass=HicksField", text)
        self.assertIn("Terminal=false", text)

    def test_render_script_compiles(self):
        compile(installer.RENDER_SCRIPT, "render", "exec")

    def test_copy_tree_skips_cache(self):
        src = self.tmp / "src"
        (src / "hicksfield" / "__pycache__").mkdir(parents=True)
        (src / "hicksfield" / "gui.py").write_text("x", encoding="utf-8")
        (src / "hicksfield" / "__pycache__" / "gui.pyc").write_bytes(b"x")
        (src / "old.zip").write_bytes(b"x")
        target = self.tmp / "target"
        installer.copy_tree(src, target, lambda m: None)
        self.assertTrue((target / "hicksfield" / "gui.py").exists())
        self.assertFalse((target / "hicksfield" / "__pycache__").exists())
        self.assertFalse((target / "old.zip").exists())

    def test_install_system_deps_attempts_missing_packages(self):
        messages = []
        warnings = []
        with mock.patch.object(installer.sysdeps, "missing_packages",
                               return_value=["ffmpeg"]), \
                mock.patch.object(installer.sysdeps, "install_packages",
                                  return_value=None) as install:
            installer.install_system_deps(messages.append, warnings)
        install.assert_called_once()
        self.assertEqual(install.call_args[0][0], ["ffmpeg"])
        self.assertEqual(warnings, [])
        self.assertTrue(any("ffmpeg" in m for m in messages))

    def test_install_system_deps_skips_when_nothing_missing(self):
        messages = []
        warnings = []
        with mock.patch.object(installer.sysdeps, "missing_packages",
                               return_value=[]), \
                mock.patch.object(installer.sysdeps, "install_packages") \
                as install:
            installer.install_system_deps(messages.append, warnings)
        install.assert_not_called()
        self.assertEqual(warnings, [])

    def test_install_system_deps_declined_warns_and_continues(self):
        messages = []
        warnings = []
        with mock.patch.object(installer.sysdeps, "missing_packages",
                               return_value=["ffmpeg",
                                             "python3.12-venv"]), \
                mock.patch.object(
                    installer.sysdeps, "install_missing_packages",
                    return_value=["ffmpeg and the Python venv package "
                                  "could not be installed: the "
                                  "permission step was cancelled."]):
            # Must not raise: a declined permission step never fails
            # the install.
            installer.install_system_deps(messages.append, warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("ffmpeg", warnings[0])
        self.assertNotIn("sudo", warnings[0])

    def test_install_continues_when_system_deps_declined(self):
        target = self.tmp / "hicksfield-app"
        messages = []
        with mock.patch.object(installer.sysdeps, "missing_packages",
                               return_value=["ffmpeg"]), \
                mock.patch.object(
                    installer.sysdeps, "install_missing_packages",
                    return_value=["ffmpeg could not be installed: the "
                                  "permission step was cancelled."]), \
                mock.patch.object(installer, "copy_tree"), \
                mock.patch.object(installer, "make_venv",
                                  return_value=self.tmp / "python"), \
                mock.patch.object(installer, "install_deps"), \
                mock.patch.object(installer, "render_icons"), \
                mock.patch.object(installer, "write_desktop"), \
                mock.patch.object(installer, "verify"):
            warnings = installer.install(
                target, messages.append, lambda f: None)
        self.assertEqual(len(warnings), 1)
        self.assertIn("ffmpeg", warnings[0])

    def test_make_venv_error_has_no_terminal_command(self):
        stderr = ("ensurepip is not available\n"
                  "On Debian/Ubuntu systems, you need to install the "
                  "python3-venv package using the following command.\n"
                  "    apt install python3.12-venv\n")
        with mock.patch.object(installer, "run",
                               return_value=(1, "", stderr)):
            with self.assertRaises(installer.InstallError) as ctx:
                installer.make_venv(self.tmp / "target",
                                    lambda m: None)
        text = ctx.exception.plain_text()
        self.assertNotIn("sudo", text)
        self.assertNotIn("apt install", text)
        self.assertIn("permission step", text)

    def test_zip_builder_modes(self):
        tree = Path(__file__).resolve().parent.parent
        copy = self.tmp / "tree"
        shutil.copytree(tree, copy,
                        ignore=shutil.ignore_patterns("__pycache__"))
        proc = subprocess.run(
            [sys.executable, "build_zip.py"], cwd=str(copy),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        zips = list(self.tmp.glob("hicksfield-v*.zip"))
        self.assertEqual(len(zips), 1)
        with zipfile.ZipFile(zips[0]) as archive:
            infos = {info.filename: info for info in archive.infolist()}
        for name in ("HicksField/install.sh", "HicksField/run.sh",
                     "HicksField/installer.py", "HicksField/build_zip.py"):
            mode = infos[name].external_attr >> 16
            self.assertEqual(mode, 0o100755, name)
        mode = infos["HicksField/hicksfield/gui.py"].external_attr >> 16
        self.assertEqual(mode, 0o100644)
        self.assertIn("HicksField/hicksfield/__init__.py", infos)
        self.assertIn("HicksField/tests/test_gui.py", infos)
        self.assertIn("HicksField/icons/hicksfield.svg", infos)


if __name__ == "__main__":
    unittest.main()
