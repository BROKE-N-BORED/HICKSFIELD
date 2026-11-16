import subprocess
import unittest
from unittest import mock

from hicksfield import sysdeps


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class SysDepsTests(unittest.TestCase):
    def test_ffmpeg_installed(self):
        with mock.patch("hicksfield.sysdeps.shutil.which",
                        return_value="/usr/bin/ffmpeg"):
            self.assertTrue(sysdeps.ffmpeg_installed())
        with mock.patch("hicksfield.sysdeps.shutil.which",
                        return_value=None):
            self.assertFalse(sysdeps.ffmpeg_installed())

    def test_venv_available(self):
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        return_value=_proc(0)) as run:
            self.assertTrue(sysdeps.venv_available())
            command = run.call_args[0][0]
            self.assertIn("-c", command)
            self.assertIn("import ensurepip", command)
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        return_value=_proc(1)):
            self.assertFalse(sysdeps.venv_available())
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        side_effect=OSError("no python")):
            self.assertFalse(sysdeps.venv_available())

    def test_missing_packages(self):
        with mock.patch.object(sysdeps, "ffmpeg_installed",
                               return_value=True), \
                mock.patch.object(sysdeps, "venv_available",
                                  return_value=True):
            self.assertEqual(sysdeps.missing_packages(), [])
        with mock.patch.object(sysdeps, "ffmpeg_installed",
                               return_value=False), \
                mock.patch.object(sysdeps, "venv_available",
                                  return_value=False):
            missing = sysdeps.missing_packages()
            self.assertIn("ffmpeg", missing)
            self.assertIn(sysdeps.venv_package_name(), missing)

    def test_install_packages_uses_pkexec_apt(self):
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        return_value=_proc(0)) as run:
            error = sysdeps.install_packages(["ffmpeg"])
        self.assertIsNone(error)
        command = run.call_args[0][0]
        self.assertEqual(command[:4],
                         ["pkexec", "apt-get", "install", "-y"])
        self.assertIn("ffmpeg", command)
        self.assertNotIn("sudo", command)

    def test_install_packages_reports_progress(self):
        messages = []
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        return_value=_proc(0)):
            error = sysdeps.install_packages(
                ["ffmpeg"], progress_cb=messages.append)
        self.assertIsNone(error)
        self.assertTrue(messages)
        self.assertIn("permission", messages[0])

    def test_install_packages_declined_is_plain_error(self):
        # pkexec exit 126: the user dismissed the password window.
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        return_value=_proc(126)):
            error = sysdeps.install_packages(["ffmpeg"])
        self.assertIsNotNone(error)
        self.assertIn("permission", error)
        self.assertNotIn("sudo", error)

    def test_install_packages_pkexec_missing_is_plain_error(self):
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        side_effect=FileNotFoundError("pkexec")):
            error = sysdeps.install_packages(["ffmpeg"])
        self.assertIsNotNone(error)
        self.assertIn("permission", error)

    def test_install_packages_apt_failure_is_plain_error(self):
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        return_value=_proc(
                            100, "",
                            "E: Unable to locate package ffmpeg\n")):
            error = sysdeps.install_packages(["ffmpeg"])
        self.assertIsNotNone(error)
        self.assertIn("did not finish", error)

    def test_install_missing_packages_nothing_missing(self):
        with mock.patch.object(sysdeps, "missing_packages",
                               return_value=[]):
            self.assertEqual(sysdeps.install_missing_packages(), [])

    def test_install_missing_packages_declined_warns(self):
        with mock.patch.object(sysdeps, "missing_packages",
                               return_value=["ffmpeg"]), \
                mock.patch.object(sysdeps, "install_packages",
                                  return_value="the permission step "
                                  "was cancelled"):
            warnings = sysdeps.install_missing_packages()
        self.assertEqual(len(warnings), 1)
        self.assertIn("ffmpeg", warnings[0])
        self.assertIn("installer again", warnings[0])
        self.assertNotIn("sudo", warnings[0])

    def test_install_packages_async_calls_done(self):
        calls = []
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        return_value=_proc(0)):
            thread = sysdeps.install_packages_async(
                ["ffmpeg"], done_cb=lambda: calls.append("done"),
                error_cb=lambda m: calls.append(f"error: {m}"))
            thread.join(timeout=5)
        self.assertEqual(calls, ["done"])

    def test_install_packages_async_calls_error(self):
        calls = []
        with mock.patch("hicksfield.sysdeps.subprocess.run",
                        return_value=_proc(126)):
            thread = sysdeps.install_packages_async(
                ["ffmpeg"], done_cb=lambda: calls.append("done"),
                error_cb=lambda m: calls.append(f"error: {m}"))
            thread.join(timeout=5)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("error:"))
        self.assertIn("permission", calls[0])


if __name__ == "__main__":
    unittest.main()
